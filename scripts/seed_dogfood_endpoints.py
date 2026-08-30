#!/usr/bin/env python3
"""
seed_dogfood_endpoints.py - register the two M14 mock model endpoints.

The dogfood pipeline runs two `agent: openai-harness` steps on every push
(wave8 s8.2). Those steps name endpoints BY NAME, so the endpoints have to
exist in the backend the pipeline is running against. This script creates
them, idempotently, and probes them - and it fails LOUDLY if the capability
record does not come back the way the gate expects, because a harness step
against an unprobed endpoint is refused at dispatch and a harness step
against a `degraded` tools endpoint would silently run the fallback protocol
instead of the tools loop.

WHY A PIPELINE STEP RATHER THAN THE TEST-MODE SEED: `POST /api/test/seed`
requires LAZYAF_TEST_MODE, which the DEV stack deliberately does not set. The
dogfood lane runs against the dev backend, so it seeds through the ordinary
public API - the same two POSTs a human would make, which is also why this
script doubles as the copy-pasteable registration in
`upcoming/m14-testing.md`. The test-mode seed registers the same two rows for
the e2e lane; both spellings produce the same rows.

Stdlib-only: it runs inside a bare `lazyaf-base:dev` step container.

Env contract:
  LAZYAF_BACKEND_URL        default http://backend:8000
  LAZYAF_MOCK_ENDPOINT_URL  default http://mock-endpoint:8099 - the URL that is
                            REGISTERED, written in the terms of whoever makes
                            the inference call (the step container, for
                            `reach: direct`)
  LAZYAF_MOCK_HEALTH_URL    default = LAZYAF_MOCK_ENDPOINT_URL - the URL THIS
                            PROCESS polls for readiness. They differ when this
                            script runs on the host: the endpoint has to be
                            registered as `http://mock-endpoint:8099` (a
                            compose service name only containers resolve) while
                            readiness is checked on `http://localhost:8099` (the
                            published port). Two URLs because there are honestly
                            two network positions, not because either is a
                            fallback for the other.
  LAZYAF_SEED_TIMEOUT       default 60 (seconds to wait for the mock server)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_BACKEND_URL = "http://backend:8000"
DEFAULT_MOCK_URL = "http://mock-endpoint:8099"

#: The two dogfood endpoints, and why each one exists.
#:
#: `rate_usd_hour` is set on BOTH on purpose. It puts the 12.5 `gpu-node`
#: pricing branch on the dogfood lane, closing that phase's stated gap that
#: the branch "is reached only by API tests with a hand-built manifest". The
#: number is fiction (it is a mock server on a laptop); what is being tested
#: is that a rate resolves and produces `cost_source == "gpu-node"` with a
#: real `cost_usd`, which assertion 14 checks.
ENDPOINTS = [
    {
        "name": "dogfood-mock",
        "description": (
            "M14 dogfood: the tool-calling mock OpenAI server "
            "(tdd/shared/mock_openai, scenario happy_tools). No GPU."
        ),
        "scenario": "happy_tools",
        "model": "mock-model",
        "server_kind": "vllm",
        "auth_style": "none",
        "reach": "direct",
        "rate_usd_hour": "0.010000",
        "max_concurrency": 1,
        "request_timeout_seconds": 60,
        "expect_probe_status": "ok",
    },
    {
        "name": "dogfood-mock-notools",
        "description": (
            "M14 dogfood: a model that CANNOT tool-call (scenario happy_text). "
            "Probes `degraded`, which is USABLE - it routes the no-tools "
            "fallback protocol."
        ),
        "scenario": "happy_text",
        "model": "mock-model-notools",
        "server_kind": "vllm",
        "auth_style": "none",
        "reach": "direct",
        "rate_usd_hour": "0.010000",
        "max_concurrency": 1,
        "request_timeout_seconds": 60,
        # `degraded` is the CORRECT outcome here: the server answers the tool
        # probe with prose. Expecting "ok" would be expecting the mock to lie.
        "expect_probe_status": "degraded",
    },
]


def _request(method, url, payload=None, timeout=90.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {"detail": body.decode(errors="replace")}


def wait_for_mock(mock_url, timeout):
    """Block until the mock server answers /health, or fail naming the URL."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{mock_url}/health", timeout=5) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001 - readiness poll, any error retries
            last = exc
        time.sleep(1)
    raise SystemExit(
        f"FAIL: the mock OpenAI server at {mock_url} never answered /health "
        f"within {timeout}s (last error: {last!r}). It is the compose service "
        f"`mock-endpoint`; start it with `docker compose up -d mock-endpoint`. "
        f"Without it the harness steps have nothing to talk to."
    )


def body_for(spec, mock_url):
    return {
        "name": spec["name"],
        "description": spec["description"],
        "base_url": f"{mock_url}/{spec['scenario']}/v1",
        "model": spec["model"],
        "server_kind": spec["server_kind"],
        "auth_style": spec["auth_style"],
        "reach": spec["reach"],
        "rate_usd_hour": spec["rate_usd_hour"],
        "max_concurrency": spec["max_concurrency"],
        "request_timeout_seconds": spec["request_timeout_seconds"],
        "enabled": True,
    }


def seed_one(backend_url, spec, mock_url):
    """Create-or-update one endpoint and probe it. Returns the read model."""
    payload = body_for(spec, mock_url)
    status, created = _request(
        "POST", f"{backend_url}/api/model-endpoints?probe=true", payload
    )
    if status == 201:
        return created.get("endpoint") or created

    if status != 409:
        raise SystemExit(
            f"FAIL: POST /api/model-endpoints for '{spec['name']}' returned "
            f"{status}: {json.dumps(created)[:600]}"
        )

    # Already registered. PATCH it back to the shape this lane needs - a
    # previous run may have left a different base_url or rate behind - and
    # re-probe. PATCHing base_url/model resets the capability record to
    # `unprobed` by design, which is exactly why the probe below is not
    # optional.
    patch = {
        key: payload[key]
        for key in ("description", "base_url", "model", "server_kind", "rate_usd_hour",
                    "max_concurrency", "request_timeout_seconds", "enabled")
    }
    status, updated = _request(
        "PATCH", f"{backend_url}/api/model-endpoints/{spec['name']}", patch
    )
    if status >= 400:
        raise SystemExit(
            f"FAIL: PATCH /api/model-endpoints/{spec['name']} returned "
            f"{status}: {json.dumps(updated)[:600]}"
        )
    status, probed = _request(
        "POST", f"{backend_url}/api/model-endpoints/{spec['name']}/probe", {}
    )
    if status >= 400:
        raise SystemExit(
            f"FAIL: POST /api/model-endpoints/{spec['name']}/probe returned "
            f"{status}: {json.dumps(probed)[:600]}"
        )
    return (probed or {}).get("endpoint") or updated


def main():
    backend_url = os.environ.get("LAZYAF_BACKEND_URL", DEFAULT_BACKEND_URL).rstrip("/")
    mock_url = os.environ.get("LAZYAF_MOCK_ENDPOINT_URL", DEFAULT_MOCK_URL).rstrip("/")
    health_url = os.environ.get("LAZYAF_MOCK_HEALTH_URL", mock_url).rstrip("/")
    timeout = float(os.environ.get("LAZYAF_SEED_TIMEOUT", "60"))

    wait_for_mock(health_url, timeout)
    if health_url != mock_url:
        print(
            f"[seed] readiness checked on {health_url}; registering {mock_url} "
            f"(the address the step container will use)"
        )

    problems = []
    for spec in ENDPOINTS:
        endpoint = seed_one(backend_url, spec, mock_url)
        capabilities = (endpoint or {}).get("capabilities") or {}
        status = capabilities.get("probe_status") or (endpoint or {}).get("probe_status")
        print(
            f"[seed] {spec['name']}: base_url={endpoint.get('base_url')} "
            f"model={endpoint.get('model')} probe_status={status} "
            f"tools={capabilities.get('supports_tools')} "
            f"usage={capabilities.get('reports_usage')} "
            f"ctx={capabilities.get('context_window')}"
        )
        if status != spec["expect_probe_status"]:
            problems.append(
                f"{spec['name']}: probe_status={status!r}, expected "
                f"{spec['expect_probe_status']!r}. A wrong capability record "
                f"here is not cosmetic - dispatch REFUSES an unprobed "
                f"endpoint, and a tools endpoint that probes `degraded` would "
                f"silently run the fallback protocol instead of the tools loop"
            )
    if problems:
        raise SystemExit("FAIL: endpoint seeding produced wrong capabilities:\n  " + "\n  ".join(problems))
    print(f"[seed] {len(ENDPOINTS)} model endpoint(s) registered and probed")


if __name__ == "__main__":
    sys.exit(main())
