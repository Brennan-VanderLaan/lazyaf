"""Pure-HTTP helpers for the QA-2 lane (illegal state transitions / lifecycle abuse).

Speaks only HTTP to a running LazyAF stack, so nothing here imports the
backend or needs a local database.

Base URL comes from ``LAZYAF_QA_BASE_URL`` and defaults to the isolated QA
stack on http://localhost:8790.

ISOLATION POLICY - deliberately does NOT call ``POST /api/test/reset``.
The QA stack is shared by several concurrent QA lanes; a global reset would
delete another lane's fixtures mid-test. Every test here allocates its OWN
repo via ``POST /api/test/seed`` (which is additive: it creates a fresh
ingested repo + git storage + pipeline + two cards and resets nothing) and
only ever asserts on ids it created itself. No test may assert on the SIZE
or CONTENT of a global collection (``GET /api/repos``, ``GET
/api/pipeline-runs``) for the same reason.
"""

import json
import os
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("LAZYAF_QA_BASE_URL", "http://localhost:8790").rstrip("/")

# The QA stack shares one SQLite file across every concurrent QA lane, so a
# write can lose a lock race and 500. Retry the *fixture* calls only - never
# the call a test is actually asserting on.
_TRANSIENT = ("database is locked", "QueuePool limit", "Internal Server Error")


def api(method, path, body=None, timeout=90):
    """Call the stack. Returns (status_code, parsed_body_or_text)."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        BASE_URL + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, (json.loads(raw) if raw else None)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, (json.loads(raw) if raw else None)
        except ValueError:
            return exc.code, raw


def api_retry(method, path, body=None, attempts=6, delay=5.0):
    """``api`` with retries for the shared-SQLite transients. Fixtures only."""
    status, payload = api(method, path, body)
    for _ in range(attempts - 1):
        text = payload if isinstance(payload, str) else json.dumps(payload or {})
        if status < 500 or not any(t in text for t in _TRANSIENT):
            return status, payload
        time.sleep(delay)
        status, payload = api(method, path, body)
    return status, payload


# ---------------------------------------------------------------------------
# Fixtures-as-functions
# ---------------------------------------------------------------------------

def seed_repo():
    """Allocate a private, ingested, git-initialised repo. Never resets."""
    status, body = api_retry("POST", "/api/test/seed")
    assert status == 200, f"seed failed: {status} {body}"
    return body["repo"]["id"], body


def mock_agent_config(*, seconds=None, files=None, exit_code=0):
    """A MockExecutor script.

    ``seconds`` emits one streamed event per second, which is how a test
    holds a card in ``in_progress`` long enough to abuse it.
    ``files`` makes the agent actually commit something, so the card gets a
    real pushed branch that ``approve`` can merge.
    """
    events = [{"type": "content", "text": f"working {i}"} for i in range(seconds or 1)]
    cfg = {
        "response_mode": "streaming" if seconds else "batch",
        "delay_ms": 1000 if seconds else 10,
        "output_events": events,
        "exit_code": exit_code,
    }
    if files:
        cfg["file_operations"] = [
            {"action": "create", "path": path, "content": content}
            for path, content in files.items()
        ]
    return cfg


def make_card(repo_id, title, *, seconds=None, files=None, **extra):
    body = {
        "title": f"qa2-{title}",
        "description": "QA-2 state-machine probe",
        "step_type": "agent",
        "runner_type": "mock",
    }
    if seconds or files:
        body["step_config"] = {
            "mock_config": mock_agent_config(seconds=seconds, files=files)
        }
    body.update(extra)
    status, card = api("POST", f"/api/repos/{repo_id}/cards", body)
    assert status == 201, f"card create failed: {status} {card}"
    return card["id"]


def get_card(card_id):
    status, body = api("GET", f"/api/cards/{card_id}")
    return status, body


def card_status(card_id):
    status, body = api("GET", f"/api/cards/{card_id}")
    return body.get("status") if status == 200 else f"HTTP{status}"


def get_job(job_id):
    status, body = api("GET", f"/api/jobs/{job_id}")
    return body if status == 200 else {"status": f"HTTP{status}"}


def get_run(run_id):
    status, body = api("GET", f"/api/pipeline-runs/{run_id}")
    return body if status == 200 else {"status": f"HTTP{status}"}


def wait_for_card(card_id, wanted, timeout=180):
    """Poll until the card reaches one of ``wanted``. Returns the card dict."""
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        status, body = api("GET", f"/api/cards/{card_id}")
        if status == 200 and body.get("status") in wanted:
            return body
        time.sleep(1.5)
    raise AssertionError(f"card {card_id} never reached {wanted}: {body}")


def wait_for_job(job_id, wanted, timeout=120):
    deadline = time.time() + timeout
    job = None
    while time.time() < deadline:
        job = get_job(job_id)
        if job.get("status") in wanted:
            return job
        time.sleep(1.5)
    return job


def wait_for_run(run_id, wanted, timeout=180):
    deadline = time.time() + timeout
    run = None
    while time.time() < deadline:
        run = get_run(run_id)
        if run.get("status") in wanted:
            return run
        time.sleep(2)
    return run


def start_card(card_id):
    status, body = api("POST", f"/api/cards/{card_id}/start")
    assert status == 200, f"start failed: {status} {body}"
    return body


def branch_names(repo_id):
    status, body = api("GET", f"/api/repos/{repo_id}/branches")
    if status != 200:
        return []
    return sorted(b["name"] for b in body.get("branches", []))


def make_pipeline(repo_id, name, command, triggers=None):
    payload = {
        "name": f"qa2-{name}",
        "steps": [{"name": "s", "type": "script", "config": {"command": command}}],
    }
    if triggers is not None:
        payload["triggers"] = triggers
    status, body = api("POST", f"/api/repos/{repo_id}/pipelines", payload)
    assert status == 201, f"pipeline create failed: {status} {body}"
    return body["id"]


def pipeline_run_count(pipeline_id):
    status, body = api("GET", f"/api/pipelines/{pipeline_id}/runs")
    assert status == 200, f"runs list failed: {status} {body}"
    return len(body)


def concurrent(fn, n=2):
    """Fire ``fn`` from ``n`` threads at once; return the list of results."""
    import threading

    results = []
    lock = threading.Lock()

    def worker():
        out = fn()
        with lock:
            results.append(out)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
