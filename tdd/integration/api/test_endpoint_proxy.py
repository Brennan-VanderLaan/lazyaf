"""The `reach=proxy` broker (M14 s6.3), design test contract 7.

`reach="proxy"` is the mode where the BACKEND makes the inference call on the
container's behalf. It exists for one deployment shape (the model server is
reachable from the backend and not from the step container) and it is opt-in,
never a default.

**THE ONE GENUINE ADVANTAGE, and the thing most of this file pins: no endpoint
secret ever reaches the container in proxy mode.** The container authenticates
with the step JWT it already holds; the upstream key is injected server-side
and appears in no response body and no response header.

**IT IS NOT A GENERAL EGRESS HOLE**, and four independent gates say so - the
endpoint must be `reach="proxy"`, the calling step must be the step that holds
this endpoint's slot, the path must be in a four-entry allowlist, and the body
is capped.
"""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.main import app  # noqa: E402
from app.models.model_endpoint import ModelEndpoint  # noqa: E402
from app.models.pipeline import (  # noqa: E402
    Pipeline,
    PipelineRun,
    StepExecution,
    StepRun,
)
from app.models.repo import Repo  # noqa: E402
from app.routers import model_endpoints as model_endpoints_router  # noqa: E402
from app.routers.model_endpoints import (  # noqa: E402
    PROXY_ALLOWED_PATHS,
    PROXY_MAX_BODY_BYTES,
)
from app.services.control_layer.auth import generate_step_token  # noqa: E402

API = "/api/model-endpoints"

if not any(getattr(route, "path", "").startswith(API) for route in app.routes):
    app.include_router(model_endpoints_router.router)

SENTINEL = "sk-planted-proxy-key-do-not-leak-00000"
UPSTREAM = "http://vllm.internal:8000/v1"


# -----------------------------------------------------------------------------
# A stub upstream, installed in place of httpx's transport
# -----------------------------------------------------------------------------

class _StubUpstream:
    """Records what the broker actually sent, and answers however the test
    asks. R6: a real `httpx.AsyncClient` driving a real handler - the only
    thing replaced is the socket."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.body = json.dumps(
            {
                "id": "chatcmpl-1",
                "model": "qwen2.5-coder:32b",
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            }
        )
        self.stream_lines = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.stream_lines is not None:
            lines = list(self.stream_lines)

            async def _frames():
                for line in lines:
                    yield line

            return httpx.Response(
                self.status,
                content=_frames(),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            self.status, content=self.body,
            headers={"content-type": "application/json"},
        )


@pytest.fixture
def upstream(monkeypatch):
    stub = _StubUpstream()
    real_init = httpx.AsyncClient.__init__

    def _init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(stub.handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return stub


@pytest.fixture(autouse=True)
def _fresh_semaphores():
    model_endpoints_router._proxy_semaphores.clear()
    yield
    model_endpoints_router._proxy_semaphores.clear()


async def _make_endpoint(db, *, reach="proxy", auth_style="bearer",
                         max_concurrency=1, name=None):
    name = name or f"proxy-{uuid4().hex[:6]}"
    endpoint = ModelEndpoint(
        id=str(uuid4()),
        name=name,
        base_url=UPSTREAM,
        model="qwen2.5-coder:32b",
        server_kind="vllm",
        auth_style=auth_style,
        auth_secret_ref="LAZYAF_ENDPOINT_PROXY" if auth_style != "none" else None,
        reach=reach,
        gpu_node_id=f"endpoint:{name}",
        max_concurrency=max_concurrency,
        request_timeout_seconds=30,
        probe_status="ok",
        probe_detail="{}",
        supports_tools=True,
        supports_streaming=True,
        reports_usage=True,
        enabled=True,
    )
    db.add(endpoint)
    await db.commit()
    await db.refresh(endpoint)
    return endpoint


async def _make_step(db, *, endpoint_id=None, status="running"):
    repo = Repo(id=str(uuid4()), name=f"r-{uuid4().hex[:6]}", is_ingested=True)
    db.add(repo)
    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci", steps="[]")
    db.add(pipeline)
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    db.add(run)
    step_run = StepRun(id=str(uuid4()), pipeline_run_id=run.id, step_index=0,
                       step_name="agent", status="running", logs="")
    db.add(step_run)
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run.id}:0:{uuid4().hex[:6]}",
        step_run_id=step_run.id,
        status=status,
        model_endpoint_id=endpoint_id,
    )
    db.add(execution)
    await db.commit()
    token = generate_step_token(
        step_id=execution.id, execution_key=execution.execution_key
    )
    return execution.id, {"Authorization": f"Bearer {token}"}


def _chat(endpoint, **overrides):
    body = {"model": "qwen2.5-coder:32b", "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return f"{API}/{endpoint.id}/proxy/v1/chat/completions", body


# -----------------------------------------------------------------------------
# The happy path
# -----------------------------------------------------------------------------

class TestForwarding:
    async def test_a_chat_completion_is_forwarded_and_answered(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "ok"
        assert len(upstream.requests) == 1
        assert str(upstream.requests[0].url) == f"{UPSTREAM}/chat/completions"

    async def test_the_upstream_key_is_injected_SERVER_side(
        self, client, db_session, upstream, monkeypatch
    ):
        """The whole point of the mode: the container never holds the key."""
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        await client.post(url, json=body, headers=headers)

        sent = upstream.requests[0]
        assert sent.headers["authorization"] == f"Bearer {SENTINEL}"

    async def test_the_steps_own_token_is_never_forwarded_upstream(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        await client.post(url, json=body, headers=headers)

        sent_auth = upstream.requests[0].headers["authorization"]
        assert headers["Authorization"] not in sent_auth

    async def test_the_key_appears_in_no_response_header_or_body(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert SENTINEL not in response.text
        assert SENTINEL not in json.dumps(dict(response.headers))

    async def test_a_401_body_that_echoes_the_key_back_is_SCRUBBED(
        self, client, db_session, upstream, monkeypatch
    ):
        """A hostile (or merely careless) upstream echoing the key is a real
        failure mode, and it must not be the thing that leaks it."""
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        upstream.status = 401
        upstream.body = json.dumps(
            {"error": {"message": f"invalid api key: {SENTINEL}"}}
        )
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 401
        assert SENTINEL not in response.text
        assert "***" in response.text

    async def test_an_upstream_transport_failure_is_a_502_naming_the_url(
        self, client, db_session, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)

        def _boom(request):
            raise httpx.ConnectError("connection refused")

        real_init = httpx.AsyncClient.__init__

        def _init(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_boom)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 502
        assert "vllm.internal" in response.text
        assert SENTINEL not in response.text

    async def test_a_no_auth_proxy_endpoint_forwards_with_no_auth_header(
        self, client, db_session, upstream
    ):
        endpoint = await _make_endpoint(db_session, auth_style="none")
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 200
        assert "authorization" not in upstream.requests[0].headers

    async def test_a_missing_backend_variable_is_a_502_naming_the_variable(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.delenv("LAZYAF_ENDPOINT_PROXY", raising=False)
        monkeypatch.delenv("LAZYAF_ENDPOINT_PROXY_FILE", raising=False)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 502
        assert "LAZYAF_ENDPOINT_PROXY" in response.text
        assert upstream.requests == []


# -----------------------------------------------------------------------------
# The four gates
# -----------------------------------------------------------------------------

class TestGates:
    @pytest.mark.parametrize("reach", ["direct", "runner-local"])
    async def test_a_non_proxy_endpoint_has_no_broker_at_all(
        self, client, db_session, upstream, reach
    ):
        endpoint = await _make_endpoint(db_session, reach=reach)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 404
        assert "reach='proxy'" in response.text
        assert upstream.requests == []

    async def test_a_step_running_on_a_DIFFERENT_endpoint_is_403(
        self, client, db_session, upstream, monkeypatch
    ):
        """12.6's split-brain fence: a token minted for one step cannot drive
        another endpoint's broker."""
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session, name="proxy-a")
        other = await _make_endpoint(db_session, name="proxy-b")
        _step, headers = await _make_step(db_session, endpoint_id=other.id)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 403
        assert upstream.requests == []

    async def test_a_step_holding_no_endpoint_is_403(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=None)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body, headers=headers)
        assert response.status_code == 403

    async def test_no_token_at_all_is_401(self, client, db_session, upstream):
        endpoint = await _make_endpoint(db_session)
        url, body = _chat(endpoint)

        response = await client.post(url, json=body)
        assert response.status_code == 401

    async def test_a_forged_token_is_403(self, client, db_session, upstream, monkeypatch):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        step_id, _headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        response = await client.post(
            f"{url}?step_id={step_id}",
            json=body,
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        assert response.status_code in (401, 403)
        assert upstream.requests == []

    @pytest.mark.parametrize(
        "path", ["embeddings", "admin", "../../etc/passwd", "chat/completions/../x"]
    )
    async def test_only_the_allowlisted_paths_are_brokered(
        self, client, db_session, upstream, monkeypatch, path
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)

        response = await client.post(
            f"{API}/{endpoint.id}/proxy/v1/{path}", json={}, headers=headers
        )

        if path in PROXY_ALLOWED_PATHS:
            assert response.status_code == 200
        else:
            assert response.status_code == 404
            assert upstream.requests == []

    async def test_an_oversized_body_is_413(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, _body = _chat(endpoint)
        huge = {"model": "m", "messages": [{"role": "user",
                                           "content": "x" * (PROXY_MAX_BODY_BYTES + 64)}]}

        response = await client.post(url, json=huge, headers=headers)

        assert response.status_code == 413
        assert upstream.requests == []


# -----------------------------------------------------------------------------
# Streaming and concurrency
# -----------------------------------------------------------------------------

class TestStreamingAndConcurrency:
    async def test_a_streaming_request_is_passed_through(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        upstream.stream_lines = [
            b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        endpoint = await _make_endpoint(db_session)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint, stream=True)

        response = await client.post(url, json=body, headers=headers)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # Every frame arrived, in order, unbuffered - which is what makes
        # `supports_streaming` mean something on this path.
        assert response.text.count("data: ") == 3
        assert '"content":"he"' in response.text
        assert '"content":"llo"' in response.text
        assert response.text.rstrip().endswith("[DONE]")

    async def test_over_concurrency_waits_then_503s_with_retry_after(
        self, client, db_session, upstream, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        monkeypatch.setattr(model_endpoints_router, "PROXY_QUEUE_TIMEOUT", 0.05)
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        # Hold the endpoint's only slot from outside, exactly as an in-flight
        # request would.
        semaphore = model_endpoints_router._proxy_semaphore(endpoint)
        await semaphore.acquire()
        try:
            response = await client.post(url, json=body, headers=headers)
        finally:
            semaphore.release()

        assert response.status_code == 503
        assert response.headers["Retry-After"] == "0.05"
        assert response.json()["error"]["type"] == "endpoint_busy"
        assert upstream.requests == []

    async def test_the_slot_is_released_after_a_successful_call(
        self, client, db_session, upstream, monkeypatch
    ):
        """A leaked semaphore permit would wedge the endpoint after one
        request, which is the failure mode a `finally` exists to prevent."""
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        for _ in range(3):
            assert (await client.post(url, json=body, headers=headers)).status_code == 200
        assert len(upstream.requests) == 3

    async def test_the_slot_is_released_after_an_upstream_failure(
        self, client, db_session, monkeypatch
    ):
        monkeypatch.setenv("LAZYAF_ENDPOINT_PROXY", SENTINEL)

        def _boom(request):
            raise httpx.ConnectError("nope")

        real_init = httpx.AsyncClient.__init__

        def _init(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_boom)
            real_init(self, *args, **kwargs)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
        endpoint = await _make_endpoint(db_session, max_concurrency=1)
        _step, headers = await _make_step(db_session, endpoint_id=endpoint.id)
        url, body = _chat(endpoint)

        for _ in range(2):
            assert (await client.post(url, json=body, headers=headers)).status_code == 502

    async def test_raising_the_cap_takes_effect_without_a_restart(
        self, client, db_session, upstream
    ):
        endpoint = await _make_endpoint(db_session, auth_style="none",
                                        max_concurrency=1)
        first = model_endpoints_router._proxy_semaphore(endpoint)
        endpoint.max_concurrency = 4
        second = model_endpoints_router._proxy_semaphore(endpoint)
        assert first is not second
