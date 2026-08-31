"""Integration tests for the model endpoint registry API (M14.1, wave8 s1.5).

Endpoints under test:
- GET    /api/model-endpoints
- POST   /api/model-endpoints            (creates, then probes SYNCHRONOUSLY)
- GET    /api/model-endpoints/{id}
- PATCH  /api/model-endpoints/{id}
- DELETE /api/model-endpoints/{id}
- POST   /api/model-endpoints/{id}/probe
- POST   /api/model-endpoints/{id}/probe-result   (step JWT)
- GET    /api/model-endpoints/{id}/usage

THE THREE PROPERTIES THIS FILE EXISTS TO PIN:

1. **A no-auth endpoint is the DEFAULT PATH, not a special case.** LAN ollama
   and vLLM behind a firewall genuinely have no key; most of the tests here
   register one that way on purpose.
2. **The secret value never appears anywhere.** A sentinel is planted in the
   backend environment, echoed back by a hostile stub server, and grepped for
   across every response body, `probe_detail` and `last_error`.
3. **A probe is an OBSERVATION.** An endpoint that is down returns 200 with a
   red record - never a 502, which would make the operator's UI show a
   request error where it should show a red endpoint.

Router mounting note: `app/main.py`'s `include_router` line is the
integrator's edit (agent A's report asks for it). This module mounts the
router if it is not already mounted, so the suite is green both before and
after that line lands - and identical either way, because it mounts THE SAME
router object main.py will.
"""
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

repo_root = Path(__file__).parent.parent.parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app.main import app  # noqa: E402
from app.models.model_endpoint import ModelEndpoint  # noqa: E402
from app.models.pipeline import (  # noqa: E402
    Pipeline,
    PipelineRun,
    StepExecution,
    StepRun,
)
from app.models.repo import Repo  # noqa: E402
from app.models.usage import StepUsage  # noqa: E402
from app.routers import model_endpoints as model_endpoints_router  # noqa: E402
from app.services.control_layer.auth import generate_step_token  # noqa: E402
from app.services.model_endpoints import probe as probe_module  # noqa: E402
from app.services.model_endpoints.probe import (  # noqa: E402
    PROBE_TOOL_NAME,
    ProbeHTTP,
)
from tdd.shared.mock_openai import (  # noqa: E402
    MOCK_AUDIO_PROMPT_TOKENS,
    MOCK_IMAGE_PROMPT_TOKENS,
    MOCK_MODALITY_BASE_PROMPT_TOKENS,
    MOCK_MODELS,
    MockOpenAIServer,
)

API = "/api/model-endpoints"

if not any(
    getattr(route, "path", "").startswith(API) for route in app.routes
):  # pragma: no cover - depends on whether main.py has been wired yet
    app.include_router(model_endpoints_router.router)


# -----------------------------------------------------------------------------
# A stub OpenAI-compatible server, installed in place of the httpx transport
# -----------------------------------------------------------------------------

MODEL = "qwen2.5-coder:32b"


def _models_payload(max_model_len=None):
    entry = {"id": MODEL, "object": "model"}
    if max_model_len is not None:
        entry["max_model_len"] = max_model_len
    return {"object": "list", "data": [entry]}


def _tools_payload():
    return {
        "model": MODEL,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": PROBE_TOOL_NAME,
                                "arguments": '{"value": 7}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 61, "completion_tokens": 12},
    }


class StubServer:
    """Deterministic OpenAI-compatible responses, or an outage."""

    def __init__(self, *, down=False, echo=None, tools=True, context=32768):
        self.down = down
        self.echo = echo
        self.tools = tools
        self.context = context
        self.calls: list[str] = []

    async def request(
        self, method, url, *, json_body=None, timeout=None, stream=False, max_lines=64
    ):
        self.calls.append(url)
        if self.down:
            raise probe_module.ProbeTransportError(
                "ConnectError: [Errno 111] Connection refused"
            )
        if url.endswith("/models"):
            return ProbeHTTP(
                status=200,
                text=json.dumps(_models_payload()),
                payload=_models_payload(),
            )
        if url.endswith("/api/show"):
            payload = {"model_info": {"qwen2.context_length": self.context}}
            return ProbeHTTP(status=200, text=json.dumps(payload), payload=payload)
        if stream:
            return ProbeHTTP(
                status=200,
                lines=[
                    'data: {"choices": [{"delta": {"content": "hi"}}]}',
                    'data: {"choices": [], "usage": {"prompt_tokens": 4, '
                    '"completion_tokens": 1}}',
                    "data: [DONE]",
                ],
            )
        if self.echo is not None:
            # A hostile server that reflects the key back in a 401 body - the
            # exact failure mode that would otherwise put a secret in the DB.
            body = json.dumps({"error": f"invalid api key: {self.echo}"})
            return ProbeHTTP(status=401, text=body, payload=json.loads(body))
        if not self.tools:
            body = json.dumps({"error": "this model does not support tools"})
            return ProbeHTTP(status=400, text=body, payload=json.loads(body))
        return ProbeHTTP(
            status=200, text=json.dumps(_tools_payload()), payload=_tools_payload()
        )


@pytest.fixture
def stub_server(monkeypatch):
    """Install a stub in place of `HttpxProbeTransport` for one test."""
    holder = {}

    def _install(**kwargs) -> StubServer:
        server = StubServer(**kwargs)
        holder["server"] = server
        monkeypatch.setattr(
            probe_module, "HttpxProbeTransport", lambda headers=None: server
        )
        return server

    return _install


@pytest.fixture(scope="module")
def mock_server():
    """The REAL `tdd/shared/mock_openai` server, on a loopback port.

    Used by the modality section at the bottom of this file, and by nothing
    above it. The stub above is right for the auth and outage cases (it can
    echo a secret back and refuse a connection); it is wrong for the modality
    decision table, because that table is about how a SERVER reacts to a
    content-part request, and a stub written to match the probe would prove
    only that the probe agrees with itself.

    Bound to 127.0.0.1: this fixture is in-process, unlike the compose
    `mock-endpoint` service that serves the same scenarios to the e2e lane.
    """
    with MockOpenAIServer(host="127.0.0.1") as server:
        yield server


@pytest.fixture
def captured_frames(monkeypatch):
    """Record every WS frame the manager broadcasts during one test."""
    from app.services.websocket import manager

    frames: list[tuple] = []
    original = manager.broadcast

    async def _spy(message_type, payload):
        frames.append((message_type, payload))
        return await original(message_type, payload)

    monkeypatch.setattr(manager, "broadcast", _spy)
    return frames


# -----------------------------------------------------------------------------
# Payload helpers
# -----------------------------------------------------------------------------

def lan_ollama(name="local-4090", **overrides) -> dict:
    """The first-class case: a LAN ollama with NO auth at all."""
    payload = {
        "name": name,
        "base_url": "http://192.168.1.50:11434/v1",
        "model": MODEL,
        "server_kind": "ollama",
    }
    payload.update(overrides)
    return payload


async def _make_step(db_session, *, status="running", endpoint_id=None, runner_id=None):
    repo = Repo(id=str(uuid4()), name=f"ep-repo-{uuid4().hex[:8]}", is_ingested=True)
    db_session.add(repo)
    pipeline = Pipeline(id=str(uuid4()), repo_id=repo.id, name="ci", steps="[]")
    db_session.add(pipeline)
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    db_session.add(run)
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=0,
        step_name="agent",
        status="running",
        logs="",
    )
    db_session.add(step_run)
    execution = StepExecution(
        id=str(uuid4()),
        execution_key=f"{run.id}:0:1",
        step_run_id=step_run.id,
        status=status,
        model_endpoint_id=endpoint_id,
        runner_id=runner_id,
    )
    db_session.add(execution)
    await db_session.commit()
    token = generate_step_token(
        step_id=execution.id, execution_key=execution.execution_key
    )
    return {
        "execution": execution,
        "execution_id": execution.id,
        "pipeline_run_id": run.id,
        "step_run_id": step_run.id,
        "headers": {"Authorization": f"Bearer {token}"},
    }


# -----------------------------------------------------------------------------
# Registration
# -----------------------------------------------------------------------------

class TestRegistration:
    async def test_no_auth_endpoint_registers_and_probes_in_one_call(
        self, client, stub_server
    ):
        """The demo path, end to end: register a LAN ollama with no key and
        learn what it can do at the moment of registration - not at the first
        30-minute step."""
        stub_server()

        response = await client.post(API, json=lan_ollama())

        assert response.status_code == 201
        body = response.json()
        endpoint = body["endpoint"]
        assert endpoint["auth_style"] == "none"
        assert endpoint["auth_secret_ref"] is None
        assert endpoint["secret_present"] is True
        assert endpoint["capabilities"]["probe_status"] == "ok"
        assert endpoint["capabilities"]["supports_tools"] is True
        assert endpoint["capabilities"]["supports_streaming"] is True
        assert endpoint["capabilities"]["reports_usage"] is True
        assert endpoint["capabilities"]["context_window"] == 32768
        assert endpoint["capabilities"]["probed_from"] == "backend"
        assert endpoint["health"] == "healthy"
        # Defaults that carry the cost story.
        assert endpoint["gpu_node_id"] == "endpoint:local-4090"
        assert endpoint["pricing"]["gpu_fraction"] == 1.0
        assert endpoint["pricing"]["priced"] is False
        assert endpoint["max_concurrency"] == 1

    async def test_probe_false_leaves_it_unprobed_and_says_dispatch_will_refuse(
        self, client, stub_server
    ):
        server = stub_server()

        response = await client.post(f"{API}?probe=false", json=lan_ollama())

        body = response.json()
        assert body["endpoint"]["capabilities"]["probe_status"] == "unprobed"
        assert body["endpoint"]["capabilities"]["supports_tools"] is None
        assert body["endpoint"]["health"] == "unprobed"
        assert "refuse" in body["detail"]
        assert server.calls == []

    async def test_duplicate_name_is_409(self, client, stub_server):
        stub_server()
        await client.post(f"{API}?probe=false", json=lan_ollama())

        response = await client.post(f"{API}?probe=false", json=lan_ollama())

        assert response.status_code == 409
        assert "endpoint:local-4090" in response.json()["detail"]

    async def test_zero_rate_is_priced_and_null_rate_is_not(self, client, stub_server):
        """Decision 4's whole point: `$0.00/hr` (owned hardware, marginal cash
        cost) and `unpriced` must stay distinguishable."""
        stub_server()
        owned = await client.post(
            f"{API}?probe=false", json=lan_ollama("owned", rate_usd_hour="0.00")
        )
        unknown = await client.post(
            f"{API}?probe=false", json=lan_ollama("unpriced-node")
        )

        assert owned.json()["endpoint"]["priced"] is True
        assert owned.json()["endpoint"]["rate_usd_hour"] == "0.000000"
        assert unknown.json()["endpoint"]["priced"] is False
        assert unknown.json()["endpoint"]["rate_usd_hour"] is None

    async def test_runner_local_gets_a_default_label(self, client, stub_server):
        stub_server()
        response = await client.post(
            f"{API}?probe=false", json=lan_ollama(reach="runner-local")
        )
        assert response.json()["endpoint"]["runner_label"] == "endpoint:local-4090"

    async def test_base_url_without_v1_is_accepted_with_a_warning(
        self, client, stub_server
    ):
        """Stated, never rewritten: guessing at someone's reverse proxy layout
        is how a working endpoint becomes an unexplainable 404."""
        stub_server()
        response = await client.post(
            f"{API}?probe=false",
            json=lan_ollama(base_url="http://192.168.1.50:11434"),
        )
        assert response.status_code == 201
        assert "/v1" in response.json()["endpoint"]["warning"]

    async def test_unknown_vocabulary_values_are_422(self, client):
        for field, value in (
            ("reach", "carrier-pigeon"),
            ("auth_style", "basic"),
            ("server_kind", "skynet"),
        ):
            response = await client.post(
                f"{API}?probe=false", json=lan_ollama(**{field: value})
            )
            assert response.status_code == 422, field


# -----------------------------------------------------------------------------
# Secrets
# -----------------------------------------------------------------------------

class TestSecretRefs:
    @pytest.mark.parametrize(
        "ref", ["ANTHROPIC_API_KEY", "LAZYAF_STEP_AUTH_SECRET", "GEMINI_API_KEY"]
    )
    async def test_forbidden_refs_are_422_at_create(self, client, ref):
        """A stored config must not be an exfiltration route: without the
        allowlist, a row could point the platform's own credentials at a
        container the operator does not control."""
        response = await client.post(
            f"{API}?probe=false",
            json=lan_ollama(auth_style="bearer", auth_secret_ref=ref),
        )
        assert response.status_code == 422
        assert ref in json.dumps(response.json())

    async def test_bearer_without_a_ref_is_422(self, client):
        response = await client.post(
            f"{API}?probe=false", json=lan_ollama(auth_style="bearer")
        )
        assert response.status_code == 422

    async def test_header_style_requires_a_header_name(self, client):
        response = await client.post(
            f"{API}?probe=false",
            json=lan_ollama(
                auth_style="header", auth_secret_ref="LAZYAF_ENDPOINT_DEMO"
            ),
        )
        assert response.status_code == 422

    async def test_a_valid_ref_that_resolves_to_nothing_is_not_an_error(
        self, client, stub_server, monkeypatch
    ):
        """The operator may legitimately register before setting the variable.
        `secret_present: false` is how the UI says so IN RED, without the
        create call failing."""
        monkeypatch.delenv("LAZYAF_ENDPOINT_NOT_SET", raising=False)
        stub_server()

        response = await client.post(
            f"{API}?probe=false",
            json=lan_ollama(
                auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_NOT_SET"
            ),
        )

        assert response.status_code == 201
        assert response.json()["endpoint"]["secret_present"] is False
        assert response.json()["endpoint"]["auth_secret_ref"] == "LAZYAF_ENDPOINT_NOT_SET"

    async def test_the_sentinel_never_appears_on_any_surface(
        self, client, stub_server, monkeypatch, db_session
    ):
        """Plant a real value in the backend env, have a hostile server echo
        it back in a 401, then grep EVERY surface for it."""
        sentinel = "sk-sentinel-must-never-be-stored-0001"
        monkeypatch.setenv("LAZYAF_ENDPOINT_SENTINEL", sentinel)
        stub_server(echo=sentinel)

        created = await client.post(
            API,
            json=lan_ollama(
                auth_style="bearer", auth_secret_ref="LAZYAF_ENDPOINT_SENTINEL"
            ),
        )
        endpoint_id = created.json()["endpoint"]["id"]
        listing = await client.get(API)
        single = await client.get(f"{API}/{endpoint_id}")
        probed = await client.post(f"{API}/{endpoint_id}/probe?force=true")

        for response in (created, listing, single, probed):
            assert sentinel not in response.text, response.url

        row = (
            await db_session.execute(
                select(ModelEndpoint).where(ModelEndpoint.id == endpoint_id)
            )
        ).scalar_one()
        assert sentinel not in (row.probe_detail or "")
        assert sentinel not in (row.last_error or "")
        # ...and the scrubber left the shape visible, so the operator can see
        # that a 401 happened at all.
        assert "***" in row.probe_detail


# -----------------------------------------------------------------------------
# Probing
# -----------------------------------------------------------------------------

class TestProbe:
    async def test_unreachable_returns_200_with_a_red_record(
        self, client, stub_server
    ):
        stub_server(down=True)

        created = await client.post(API, json=lan_ollama())

        assert created.status_code == 201
        endpoint = created.json()["endpoint"]
        assert endpoint["capabilities"]["probe_status"] == "unreachable"
        assert endpoint["health"] == "unhealthy"
        assert endpoint["consecutive_failures"] == 1
        assert "Connection refused" in endpoint["last_error"]
        # Never probed AND unreachable -> we do not know how to drive it.
        assert endpoint["capabilities"]["supports_tools"] is None

    async def test_a_reboot_does_not_erase_a_good_capability_record(
        self, client, stub_server, monkeypatch
    ):
        """THE rule of section 2.3, over the API."""
        healthy = stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        assert created.json()["endpoint"]["capabilities"]["supports_tools"] is True

        healthy.down = True
        response = await client.post(f"{API}/{endpoint_id}/probe?force=true")

        assert response.status_code == 200
        caps = response.json()["endpoint"]["capabilities"]
        assert caps["probe_status"] == "unreachable"
        assert caps["supports_tools"] is True, "previous capabilities survive"
        assert caps["supports_streaming"] is True
        assert caps["context_window"] == 32768
        assert response.json()["endpoint"]["consecutive_failures"] == 1

    async def test_two_probes_inside_the_floor_make_one_upstream_call(
        self, client, stub_server
    ):
        server = stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        calls_after_create = len(server.calls)

        response = await client.post(f"{API}/{endpoint_id}/probe")

        assert response.status_code == 200
        assert response.json()["cached"] is True
        assert len(server.calls) == calls_after_create

    async def test_force_re_probes(self, client, stub_server):
        server = stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        before = len(server.calls)

        response = await client.post(f"{API}/{endpoint_id}/probe?force=true")

        assert response.json()["cached"] is False
        assert len(server.calls) > before

    async def test_a_tools_less_model_is_degraded_but_registered(
        self, client, stub_server
    ):
        """`degraded` is USABLE: `supports_tools=False` routes the no-tools
        fallback protocol, and the status exists so the UI can say why."""
        stub_server(tools=False)

        created = await client.post(API, json=lan_ollama())

        endpoint = created.json()["endpoint"]
        assert endpoint["capabilities"]["probe_status"] == "degraded"
        assert endpoint["capabilities"]["supports_tools"] is False
        assert endpoint["health"] == "degraded"
        assert endpoint["probe_detail"]["tools_reason"] == "http_400"

    async def test_runner_local_probe_does_not_probe_from_the_backend(
        self, client, stub_server
    ):
        """A runner-local endpoint is unreachable from the backend BY
        DEFINITION. Probing it from here would record a reachability fact
        about the wrong machine, so the API says what it did instead."""
        server = stub_server()
        created = await client.post(
            f"{API}?probe=false", json=lan_ollama(reach="runner-local")
        )
        endpoint_id = created.json()["endpoint"]["id"]

        response = await client.post(f"{API}/{endpoint_id}/probe")

        assert response.status_code == 200
        assert response.json()["cached"] is True
        detail = response.json()["detail"]
        assert "runner-local" in detail or "runner" in detail
        assert response.json()["endpoint"]["capabilities"]["probe_status"] == "unprobed"
        assert server.calls == []

    async def test_probe_publishes_a_model_endpoint_status_frame(
        self, client, stub_server, captured_frames
    ):
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        await client.post(f"{API}/{endpoint_id}/probe?force=true")

        endpoint_frames = [f for f in captured_frames if f[0] == "model_endpoint_status"]
        assert endpoint_frames, "the Endpoints page updates from a delta, not a poll"
        payload = endpoint_frames[-1][1]
        assert payload["id"] == endpoint_id
        assert payload["endpoint"]["capabilities"]["probe_status"] == "ok"


# -----------------------------------------------------------------------------
# Editing
# -----------------------------------------------------------------------------

class TestPatch:
    @pytest.mark.parametrize(
        "changes",
        [
            {"base_url": "http://192.168.1.51:11434/v1"},
            {"model": "llama3.1:8b"},
            {"server_kind": "vllm"},
            {"auth_style": "bearer", "auth_secret_ref": "LAZYAF_ENDPOINT_DEMO"},
        ],
    )
    async def test_capability_invalidating_changes_reset_the_record(
        self, client, stub_server, changes
    ):
        """A capability observed against a different model is not evidence
        about this one."""
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        response = await client.patch(f"{API}/{endpoint_id}", json=changes)

        assert response.status_code == 200
        caps = response.json()["capabilities"]
        assert caps["probe_status"] == "unprobed"
        assert caps["supports_tools"] is None
        assert caps["supports_streaming"] is None
        assert caps["reports_usage"] is None
        assert caps["probed_at"] is None
        assert response.json()["health"] == "unprobed"

    @pytest.mark.parametrize(
        "changes",
        [
            {"description": "the workshop box"},
            {"rate_usd_hour": "1.89"},
            {"max_concurrency": 2},
            {"enabled": False},
        ],
    )
    async def test_harmless_changes_keep_the_record(
        self, client, stub_server, changes
    ):
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        response = await client.patch(f"{API}/{endpoint_id}", json=changes)

        caps = response.json()["capabilities"]
        assert caps["probe_status"] == "ok"
        assert caps["supports_tools"] is True

    async def test_patch_cannot_assemble_a_refused_auth_combination(
        self, client, stub_server
    ):
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        response = await client.patch(f"{API}/{endpoint_id}", json={"auth_style": "bearer"})

        assert response.status_code == 422

    async def test_patch_cannot_reference_a_forbidden_variable(
        self, client, stub_server
    ):
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        response = await client.patch(
            f"{API}/{endpoint_id}",
            json={"auth_style": "bearer", "auth_secret_ref": "ANTHROPIC_API_KEY"},
        )

        assert response.status_code == 422

    async def test_context_window_override_beats_the_probe(
        self, client, stub_server
    ):
        stub_server()
        created = await client.post(API, json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        assert created.json()["endpoint"]["capabilities"]["context_window"] == 32768

        response = await client.patch(f"{API}/{endpoint_id}", json={"context_window": 8192})

        assert response.json()["capabilities"]["context_window"] == 8192
        assert response.json()["context_window_source"] == "override"


# -----------------------------------------------------------------------------
# Deletion
# -----------------------------------------------------------------------------

class TestDelete:
    async def test_delete_is_409_while_a_step_is_in_flight(
        self, client, stub_server, db_session
    ):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(db_session, status="running", endpoint_id=endpoint_id)

        response = await client.delete(f"{API}/{endpoint_id}")

        assert response.status_code == 409
        assert step["execution_id"] in response.json()["detail"]

    async def test_delete_nulls_the_reference_on_finished_steps(
        self, client, stub_server, db_session
    ):
        """Historical `step_usages` keep their `gpu_node_id` string and stay
        priceable - which is why the usage join never went through a FK."""
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(
            db_session, status="completed", endpoint_id=endpoint_id
        )

        response = await client.delete(f"{API}/{endpoint_id}")

        assert response.status_code == 204
        await db_session.refresh(step["execution"])
        assert step["execution"].model_endpoint_id is None
        assert (await client.get(f"{API}/{endpoint_id}")).status_code == 404

    async def test_in_flight_is_reported_on_the_row(
        self, client, stub_server, db_session
    ):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        await _make_step(db_session, status="running", endpoint_id=endpoint_id)

        response = await client.get(f"{API}/{endpoint_id}")

        assert response.json()["in_flight"] == 1


# -----------------------------------------------------------------------------
# Reads
# -----------------------------------------------------------------------------

class TestReads:
    async def test_list_and_fetch_by_id_or_name(self, client, stub_server):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]

        listing = await client.get(API)
        by_id = await client.get(f"{API}/{endpoint_id}")
        by_name = await client.get(f"{API}/local-4090")

        assert listing.status_code == 200
        assert [row["name"] for row in listing.json()] == ["local-4090"]
        assert by_id.json()["id"] == by_name.json()["id"] == endpoint_id

    async def test_unknown_endpoint_is_404(self, client):
        assert (await client.get(f"{API}/nope")).status_code == 404

    async def test_usage_rollup_joins_through_gpu_node_id(
        self, client, stub_server, db_session
    ):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(db_session, status="completed", endpoint_id=endpoint_id)

        db_session.add(
            StepUsage(
                id=str(uuid4()),
                step_execution_id=step["execution_id"],
                step_run_id=step["step_run_id"],
                pipeline_run_id=step["pipeline_run_id"],
                provider="openai-compatible",
                model=MODEL,
                input_tokens=1200,
                output_tokens=340,
                cost_usd=Decimal("0.000000"),
                cost_source="gpu-node",
                wall_clock_ms=42_000,
                container_seconds=41.5,
                gpu_node_id="endpoint:local-4090",
                gpu_fraction=1.0,
                determinism="{}",
            )
        )
        await db_session.commit()

        response = await client.get(f"{API}/{endpoint_id}/usage")

        body = response.json()
        assert body["gpu_node_id"] == "endpoint:local-4090"
        assert body["steps"] == 1
        assert body["input_tokens"] == 1200
        assert body["output_tokens"] == 340
        assert body["by_source"]["gpu-node"] == 1
        assert body["by_source"]["cli-reported"] == 0
        assert body["cost_coverage"] == 1.0
        assert body["median_wall_clock_ms"] == 42_000

    async def test_usage_rollup_reports_null_tokens_not_zero(
        self, client, stub_server, db_session
    ):
        """A zero is a claim; a null is an absence. The cost-coverage story
        depends on keeping them apart."""
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(db_session, status="completed", endpoint_id=endpoint_id)
        db_session.add(
            StepUsage(
                id=str(uuid4()),
                step_execution_id=step["execution_id"],
                provider="openai-compatible",
                cost_source="unknown",
                wall_clock_ms=1000,
                gpu_node_id="endpoint:local-4090",
                determinism="{}",
            )
        )
        await db_session.commit()

        body = (await client.get(f"{API}/{endpoint_id}/usage")).json()

        assert body["input_tokens"] is None
        assert body["output_tokens"] is None
        assert body["cost_usd"] is None
        assert body["cost_coverage"] == 0.0


# -----------------------------------------------------------------------------
# probe-result (the runner-local report)
# -----------------------------------------------------------------------------

RUNNER_RESULT = {
    "reachable": True,
    "probe_status": "ok",
    "model_listed": True,
    "supports_tools": True,
    "supports_streaming": True,
    "reports_usage": True,
    "context_window": 32768,
    "context_window_source": "ollama",
    "detail": {"probe_status": "ok"},
    "elapsed_ms": 1234,
}


class TestProbeResult:
    async def test_a_runner_can_report_a_capability_record(
        self, client, stub_server, db_session
    ):
        stub_server()
        created = await client.post(
            f"{API}?probe=false", json=lan_ollama(reach="runner-local")
        )
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(
            db_session, endpoint_id=endpoint_id, runner_id="workshop-1"
        )

        response = await client.post(
            f"{API}/{endpoint_id}/probe-result?step_id={step['execution_id']}",
            json=RUNNER_RESULT,
            headers=step["headers"],
        )

        assert response.status_code == 200
        caps = response.json()["endpoint"]["capabilities"]
        assert caps["probe_status"] == "ok"
        assert caps["supports_tools"] is True
        assert caps["context_window"] == 32768
        # Stamped SERVER-SIDE from the step's runner, never from the payload.
        assert caps["probed_from"] == "runner:workshop-1"

    async def test_no_token_is_401(self, client, stub_server, db_session):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(db_session, endpoint_id=endpoint_id)

        response = await client.post(
            f"{API}/{endpoint_id}/probe-result?step_id={step['execution_id']}",
            json=RUNNER_RESULT,
        )

        assert response.status_code == 401

    async def test_a_step_probing_another_endpoint_is_403(
        self, client, stub_server, db_session
    ):
        """The split-brain fence, borrowed from 12.6: a token minted for one
        step cannot rewrite another endpoint's capability record."""
        stub_server()
        mine = await client.post(f"{API}?probe=false", json=lan_ollama("mine"))
        theirs = await client.post(f"{API}?probe=false", json=lan_ollama("theirs"))
        step = await _make_step(
            db_session, endpoint_id=mine.json()["endpoint"]["id"]
        )

        response = await client.post(
            f"{API}/{theirs.json()['endpoint']['id']}/probe-result"
            f"?step_id={step['execution_id']}",
            json=RUNNER_RESULT,
            headers=step["headers"],
        )

        assert response.status_code == 403

    async def test_a_malformed_report_is_422(self, client, stub_server, db_session):
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        endpoint_id = created.json()["endpoint"]["id"]
        step = await _make_step(db_session, endpoint_id=endpoint_id)

        response = await client.post(
            f"{API}/{endpoint_id}/probe-result?step_id={step['execution_id']}",
            json={"probe_status": "definitely-fine"},
            headers=step["headers"],
        )

        assert response.status_code == 422


class TestRunnerVisibility:
    """R1: a `runner-local` endpoint whose label nobody carries is visible as
    `runners: 0` BEFORE a step is dispatched to it - which is the same fact
    the step would otherwise discover at NO_RUNNER_TIMEOUT, 300 seconds late."""

    async def test_runner_local_with_no_labelled_runner_reads_zero(
        self, client, stub_server
    ):
        stub_server()
        created = await client.post(
            f"{API}?probe=false", json=lan_ollama(reach="runner-local")
        )
        assert created.json()["endpoint"]["runner_count"] == 0

    async def test_a_connected_labelled_runner_is_counted(
        self, client, stub_server, db_session, monkeypatch
    ):
        from app.models.runner import Runner
        from app.services.execution.runner_registry import runner_registry

        runner = Runner(id="workshop-1", status="idle")
        runner.set_labels({"arch": "amd64", "has": ["docker", "endpoint:local-4090"]})
        db_session.add(runner)
        # An UNLABELLED runner on the same socket table must not be counted.
        other = Runner(id="laptop-2", status="idle")
        other.set_labels({"has": ["docker"]})
        db_session.add(other)
        await db_session.commit()
        monkeypatch.setattr(
            runner_registry, "_connections", {"workshop-1": object(), "laptop-2": object()}
        )

        stub_server()
        created = await client.post(
            f"{API}?probe=false", json=lan_ollama(reach="runner-local")
        )

        assert created.json()["endpoint"]["runner_count"] == 1

    async def test_a_direct_endpoint_reports_none_not_zero(self, client, stub_server):
        """None means "not applicable"; 0 would read as "nothing can run it"."""
        stub_server()
        created = await client.post(f"{API}?probe=false", json=lan_ollama())
        assert created.json()["endpoint"]["runner_count"] is None


# -----------------------------------------------------------------------------
# Modalities, against the REAL mock server (M14.6)
#
# Everything above this line drives a stub installed in place of the httpx
# transport. This section does not: it starts the actual
# `tdd/shared/mock_openai` server and lets the real probe make real HTTP
# requests at it, because the whole point of the modality decision table is
# how it reacts to a SERVER, and R4 forbids buying green by asserting against
# a stub written to match the code. The same seven scenarios run in compose as
# the `mock-endpoint` service, so what is asserted here is what the e2e lane
# and the seeded endpoints see.
# -----------------------------------------------------------------------------

class TestModalitiesAgainstTheMockServer:
    @staticmethod
    def _payload(mock_server, scenario, *, server_kind, name=None, model=None):
        return {
            "name": name or scenario.replace("_", "-"),
            "base_url": mock_server.base_url(scenario),
            "model": model or MOCK_MODELS[0],
            "server_kind": server_kind,
        }

    async def _register(self, client, mock_server, scenario, *, server_kind, **kw):
        response = await client.post(
            API, json=self._payload(mock_server, scenario, server_kind=server_kind, **kw)
        )
        assert response.status_code == 201, response.text
        return response.json()["endpoint"]

    @staticmethod
    def _states(endpoint) -> dict:
        return {m["modality"]: m["state"] for m in endpoint["capabilities"]["modalities"]}

    @staticmethod
    def _row(endpoint, name) -> dict:
        return next(
            m for m in endpoint["capabilities"]["modalities"] if m["modality"] == name
        )

    # -- the free ollama path ------------------------------------------------

    async def test_ollama_capabilities_answers_vision_for_free(
        self, client, mock_server
    ):
        """`vision_ollama` REFUSES image parts on the wire and advertises
        `vision` in `/api/show`. A `supported` here therefore proves the free
        path ran AND that the paid wire probe was skipped - which is exactly
        why the scenario contradicts itself."""
        endpoint = await self._register(
            client, mock_server, "vision_ollama", server_kind="ollama"
        )

        assert endpoint["capabilities"]["supports_images"] is True
        row = self._row(endpoint, "images")
        assert row["state"] == "supported"
        assert row["source"] == "ollama_capabilities"
        assert endpoint["probe_detail"]["ollama_capabilities"] == [
            "completion",
            "tools",
            "vision",
        ]

    async def test_an_ollama_array_without_vision_is_a_real_false(
        self, client, mock_server
    ):
        """`vision_blind_ollama` ACCEPTS image parts on the wire, so a False
        here can only have come from the capability array."""
        endpoint = await self._register(
            client, mock_server, "vision_blind_ollama", server_kind="ollama"
        )

        assert endpoint["capabilities"]["supports_images"] is False
        row = self._row(endpoint, "images")
        assert (row["state"], row["source"]) == ("unsupported", "ollama_capabilities")
        assert row["reason"] == "not_in_capabilities"

    async def test_an_old_ollama_falls_through_to_the_wire_probe(
        self, client, mock_server
    ):
        """`vision_ollama_old` omits the `capabilities` key entirely, which is
        every ollama before v0.6. Absent must mean "ask properly", never
        "record False"."""
        endpoint = await self._register(
            client, mock_server, "vision_ollama_old", server_kind="ollama"
        )

        assert "ollama_capabilities" not in endpoint["probe_detail"]
        assert endpoint["probe_detail"]["images_free_path_reason"] == (
            "api_show_has_no_capabilities_field"
        )
        row = self._row(endpoint, "images")
        assert (row["state"], row["source"]) == ("supported", "wire_probe")

    # -- the wire probe ------------------------------------------------------

    async def test_a_vision_capable_server_is_proven_by_the_token_delta(
        self, client, mock_server
    ):
        endpoint = await self._register(
            client, mock_server, "vision_wire", server_kind="vllm"
        )

        assert endpoint["capabilities"]["supports_images"] is True
        detail = endpoint["probe_detail"]
        assert detail["images_source"] == "wire_probe"
        assert (
            detail["images_prompt_tokens"]
            == MOCK_MODALITY_BASE_PROMPT_TOKENS + MOCK_IMAGE_PROMPT_TOKENS
        )
        assert detail["images_control_tokens"] == MOCK_MODALITY_BASE_PROMPT_TOKENS
        # Both numbers are recorded, not just the verdict, so the arithmetic
        # is auditable by a human reading the record.
        assert detail["images_prompt_tokens"] > detail["images_control_tokens"]

    async def test_a_400_on_an_image_part_is_unsupported_with_the_body(
        self, client, mock_server
    ):
        endpoint = await self._register(
            client, mock_server, "vision_refuses", server_kind="vllm"
        )

        assert endpoint["capabilities"]["supports_images"] is False
        row = self._row(endpoint, "images")
        assert row["state"] == "unsupported"
        assert row["reason"] == "http_400"
        assert "does not support image input" in row["evidence"]

    async def test_a_server_that_ignores_the_image_is_undetectable(
        self, client, mock_server
    ):
        """THE nastiest case, and it LOOKS LIKE SUCCESS: 200 OK, a normal
        assistant reply, and `prompt_tokens` identical to the control. The
        image went nowhere, and calling that `supported` would let a step run
        on a prompt that quietly lost half its input."""
        endpoint = await self._register(
            client, mock_server, "vision_silent_drop", server_kind="vllm"
        )

        assert endpoint["capabilities"]["supports_images"] is None
        row = self._row(endpoint, "images")
        assert row["state"] == "undetectable"
        assert row["reason"] == "no_prompt_token_delta"
        detail = endpoint["probe_detail"]
        assert detail["images_prompt_tokens"] == detail["images_control_tokens"]
        # The endpoint is otherwise perfectly healthy - which is the point.
        assert endpoint["capabilities"]["probe_status"] == "ok"
        assert endpoint["health"] == "healthy"

    async def test_an_audio_capable_server_is_detected_independently(
        self, client, mock_server
    ):
        """`audio_wire` hears but does not see. The two answers are
        independent columns and must not be inferred from one another."""
        endpoint = await self._register(
            client, mock_server, "audio_wire", server_kind="vllm"
        )

        assert endpoint["capabilities"]["supports_audio"] is True
        assert endpoint["capabilities"]["supports_images"] is False
        assert self._states(endpoint) == {
            "text": "supported",
            "images": "unsupported",
            "audio": "supported",
            "video": "unrepresentable",
        }
        assert (
            endpoint["probe_detail"]["audio_prompt_tokens"]
            == MOCK_MODALITY_BASE_PROMPT_TOKENS + MOCK_AUDIO_PROMPT_TOKENS
        )

    async def test_an_audio_refusal_is_a_true_false_at_zero_tokens(
        self, client, mock_server
    ):
        """Every non-audio scenario 400s on `input_audio`, which is what real
        ollama does (its OpenAI layer knows text and image_url only). That is
        a genuine negative for this endpoint, and the rejection precedes
        inference, so it is free."""
        endpoint = await self._register(
            client, mock_server, "vision_wire", server_kind="vllm"
        )

        assert endpoint["capabilities"]["supports_audio"] is False
        row = self._row(endpoint, "audio")
        assert row["state"] == "unsupported"
        assert "input_audio" in row["evidence"]

    # -- the shape every endpoint carries ------------------------------------

    async def test_video_is_unrepresentable_on_every_endpoint(
        self, client, mock_server
    ):
        """Including one that sees and hears. This is a property of the wire
        format, so no probe result can ever change it."""
        endpoint = await self._register(
            client, mock_server, "audio_wire", server_kind="vllm"
        )

        row = self._row(endpoint, "video")
        assert row["state"] == "unrepresentable"
        assert row["source"] == "wire_format"
        assert row["reason"] == "wire_format_has_no_video_content_part"

    async def test_all_four_modalities_are_broken_out_on_every_endpoint(
        self, client, mock_server
    ):
        """Consistently broken out: the human reads the same four rows on
        every endpoint and a different STATE, rather than inferring meaning
        from which chips are missing."""
        endpoint = await self._register(
            client, mock_server, "happy_tools", server_kind="vllm"
        )
        assert [m["modality"] for m in endpoint["capabilities"]["modalities"]] == [
            "text",
            "images",
            "audio",
            "video",
        ]

    async def test_a_probe_never_makes_the_endpoint_degraded_for_being_text_only(
        self, client, mock_server
    ):
        """Almost every endpoint on this platform is a text model. Folding a
        missing modality into `probe_status` would paint the whole registry
        amber."""
        endpoint = await self._register(
            client, mock_server, "happy_tools", server_kind="vllm"
        )
        assert endpoint["capabilities"]["probe_status"] == "ok"
        assert endpoint["health"] == "healthy"
        assert self._states(endpoint)["images"] == "unsupported"

    async def test_re_probing_updates_the_modality_record(self, client, mock_server):
        endpoint = await self._register(
            client, mock_server, "vision_wire", server_kind="vllm"
        )
        response = await client.post(f"{API}/{endpoint['id']}/probe")
        assert response.status_code == 200
        again = response.json()["endpoint"]
        assert again["capabilities"]["supports_images"] is True


class TestCapabilityResetCoversEveryCapability:
    """The drift guard for `_reset_capability_record`.

    A capability observed against one model is not evidence about another, so
    swapping `base_url` or `model` must null EVERY observed capability. The
    modality wave shipped with images and audio missing from that list, and
    the result was a row reading `probe_status="unprobed"` and
    `supports_images=True` at the same time - with `source: null`, so nothing
    justified it - which `endpoint_modality_refusal` then passed, sending an
    image to a server that refuses them.

    This derives the expected set FROM THE MODEL rather than restating it, so
    adding a `supports_video` column later breaks this test until the reset
    learns about it. A hand-maintained second list would drift exactly the way
    the first one did.
    """

    def test_reset_nulls_every_supports_column_on_the_model(self):
        from app.models.model_endpoint import ModelEndpoint
        from app.routers.model_endpoints import _reset_capability_record

        capability_columns = sorted(
            c.name for c in ModelEndpoint.__table__.columns
            if c.name.startswith("supports_") or c.name == "reports_usage"
        )
        assert capability_columns, "no capability columns found - has the model moved?"

        endpoint = ModelEndpoint(name="drift-guard", base_url="http://x/v1", model="m")
        for name in capability_columns:
            setattr(endpoint, name, True)
        endpoint.probe_status = "ok"

        _reset_capability_record(endpoint)

        still_claimed = [
            name for name in capability_columns if getattr(endpoint, name) is not None
        ]
        assert not still_claimed, (
            f"_reset_capability_record left {still_claimed} standing after a "
            "model swap. The row now claims a capability nothing probed. Add "
            "them to the reset in backend/app/routers/model_endpoints.py."
        )
        assert endpoint.probe_status == "unprobed"


class TestPreExistingEndpointsReadUnprobed:
    """0013 backfills NOTHING, and this is what that means on the wire."""

    async def test_a_row_probed_before_m146_says_not_probed_not_unsupported(
        self, client, db_session
    ):
        """THE headline honesty requirement. A row whose `supports_tools` was
        established long ago but whose modality columns are NULL must read
        `unprobed` - because "we never asked" and "it said no" are different
        facts, and only one of them was observed."""
        endpoint = ModelEndpoint(
            id=str(uuid4()),
            name="legacy-4090",
            base_url="http://192.168.1.50:11434/v1",
            model=MODEL,
            server_kind="ollama",
            auth_style="none",
            reach="direct",
            gpu_node_id="endpoint:legacy-4090",
            max_concurrency=1,
            request_timeout_seconds=300,
            supports_tools=True,
            supports_streaming=True,
            reports_usage=True,
            probe_status="ok",
            probe_detail=json.dumps({"probe_status": "ok"}),
            probed_at=datetime.utcnow(),
            probed_from="backend",
            consecutive_failures=0,
            enabled=True,
        )
        db_session.add(endpoint)
        await db_session.commit()

        response = await client.get(f"{API}/{endpoint.id}")
        assert response.status_code == 200
        body = response.json()

        assert body["capabilities"]["supports_images"] is None
        assert body["capabilities"]["supports_audio"] is None
        states = {m["modality"]: m["state"] for m in body["capabilities"]["modalities"]}
        assert states["images"] == "unprobed"
        assert states["audio"] == "unprobed"
        assert "unsupported" not in (states["images"], states["audio"])
        # ...and the endpoint is still perfectly usable for text work.
        assert body["health"] == "healthy"
