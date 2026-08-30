"""
Integration: the 12.6.6 dispatch-side connection - spec rows reach the agent.

12.6.6 shipped an assembler (`services/spec_context.build_spec_context`), a
`spec_context` kwarg on the single producer
(`control_layer.workspace.generate_agent_config`), a top-level wire key, and a
container-side loader with a round-trip contract test pinning all of it. What
it did NOT ship was anything in production that CALLED the assembler. Every
piece existed, every piece was tested, and the whole lane was dark: a real
agent step got the same prompt it got before the spec layer existed.

This file is the test for the missing link. It starts from spec rows created
THROUGH THE API and runs the REAL `_attach_agent_payload` (R6 - no mocking of
the thing under test), asserting on the payload that
`generate_agent_config(**payload)` is handed verbatim on both the local
(`local_executor`) and remote (`_build_control_files`) lanes.

    POST /api/features -> /stories -> /criteria, PATCH /api/cards/{id}
        -> PipelineExecutor._attach_agent_payload          (THE CONNECTION)
        -> exec_config["agent"]["spec_context"]            (the wire)
        -> exec_config["agent"]["prompt"]                  (what the model reads)

`tdd/integration/api/test_spec_context_api.py` already covers assembler ->
producer -> file -> consumer over API-created rows. It stops one layer short
of dispatch, which is exactly where the gap was.
"""
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (
    REPO_ROOT / "backend",
    REPO_ROOT / "runner-common",
    REPO_ROOT / "tdd",
    REPO_ROOT,
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from shared.factories import card_create_payload, repo_create_payload  # noqa: E402

from app.models import Pipeline, PipelineRun, Repo, RunStatus, StepRun  # noqa: E402
from app.services.agent_prompt import (  # noqa: E402
    SPEC_CONTEXT_PLACEHOLDER,
    render_agent_prompt,
)
from app.services.control_layer.workspace import (  # noqa: E402
    generate_agent_config,
)
from app.services.pipeline_executor import (  # noqa: E402
    AGENT_WRAPPER_COMMAND,
    PipelineExecutor,
)
from app.services.spec_context import build_spec_context  # noqa: E402

from runner_common.agent_config import load_agent_config  # noqa: E402


# ---------------------------------------------------------------------------
# rows, all created the way a user creates them
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def repo(client):
    response = await client.post(
        "/api/repos", json=repo_create_payload(name="SpecDispatchRepo")
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def story(client):
    feature = (
        await client.post(
            "/api/features",
            json={
                "title": "Per-repo API rate limiting",
                "description": "Protect the public API from runaway clients.",
            },
        )
    ).json()
    story = (
        await client.post(
            f"/api/features/{feature['id']}/stories",
            json={
                "title": "Operator sets a per-repo request budget",
                "narrative": (
                    "As an operator I want to cap requests per repo per "
                    "minute so that one misbehaving integration cannot "
                    "starve the others."
                ),
            },
        )
    ).json()
    response = await client.post(
        f"/api/user-stories/{story['id']}/criteria",
        json={
            "text": "A repo over its budget receives HTTP 429.",
            "required": True,
            "notes": "per minute, not per hour",
        },
    )
    assert response.status_code == 201, response.text
    return {"feature": feature, "story": story, "criterion": response.json()}


@pytest_asyncio.fixture
async def linked_card(client, repo, story):
    card = (
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(
                title="Implement the per-repo budget", description=""
            ),
        )
    ).json()
    patched = await client.patch(
        f"/api/cards/{card['id']}", json={"user_story_id": story["story"]["id"]}
    )
    assert patched.status_code == 200, patched.text
    return patched.json()


@pytest_asyncio.fixture
async def unlinked_card(client, repo):
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(title="Bump the linter", description=""),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def attach(
    db,
    repo_row,
    *,
    step_config=None,
    trigger_context=None,
    step_index=0,
):
    """Run the REAL `_attach_agent_payload` and return the agent payload.

    Real rows, real session, real assembler - the only thing standing in for
    production here is that nothing spawns a container afterwards.
    """
    pipeline = Pipeline(
        id=str(uuid4()),
        repo_id=repo_row.id,
        name="agent pipeline",
        steps="[]",
    )
    db.add(pipeline)
    run = PipelineRun(
        id=str(uuid4()),
        pipeline_id=pipeline.id,
        status=RunStatus.RUNNING.value,
        trigger_context=json.dumps(trigger_context or {}),
        steps_total=1,
    )
    db.add(run)
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=step_index,
        step_name="implement",
        status=RunStatus.RUNNING.value,
    )
    db.add(step_run)
    await db.commit()

    exec_config = {"type": "agent", "command": AGENT_WRAPPER_COMMAND}
    await PipelineExecutor()._attach_agent_payload(
        db,
        run,
        pipeline,
        repo_row,
        step_run,
        {"agent": "mock", **(step_config or {})},
        exec_config,
    )
    return exec_config["agent"]


@pytest_asyncio.fixture
async def repo_row(db_session, repo):
    from sqlalchemy import select

    return (
        await db_session.execute(select(Repo).where(Repo.id == repo["id"]))
    ).scalar_one()


class TestTheBundleReachesTheAgent:
    async def test_a_linked_card_puts_a_bundle_on_the_wire(
        self, db_session, repo_row, linked_card, story
    ):
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )

        bundle = payload["spec_context"]
        assert bundle is not None, (
            "12.6.6's whole point: a card linked to a story dispatches with "
            "the curated brief, not with the pre-spec-layer prompt"
        )
        assert bundle["source"]["user_story_id"] == story["story"]["id"]
        assert bundle["source"]["feature_id"] == story["feature"]["id"]
        assert bundle["criteria_count"] == 1
        assert "A repo over its budget receives HTTP 429." in bundle["markdown"]

    async def test_the_prompt_carries_the_same_text_as_the_sidecar(
        self, db_session, repo_row, linked_card
    ):
        """The prompt is the channel the model reads; the sidecar file is the
        reference copy the agent can re-read. If they disagree, the file on
        disk is a lie."""
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )
        assert payload["spec_context"]["markdown"] in payload["prompt"]

    async def test_the_payload_survives_the_producer_and_the_consumer(
        self, db_session, repo_row, linked_card, tmp_path
    ):
        """ASSEMBLER -> DISPATCH -> PRODUCER -> a real file -> CONSUMER.

        Both lanes call `generate_agent_config(**agent_payload)`, so this is
        also the remote lane's proof: nothing in remote_executor,
        runner_protocol or the runner agent had to change."""
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )
        produced = generate_agent_config(**payload)
        config_path = tmp_path / "agent.exec-1.json"
        config_path.write_text(json.dumps(produced), encoding="utf-8")

        loaded = load_agent_config(config_path)

        assert loaded is not None
        assert loaded.has_spec_context is True
        assert loaded.spec_markdown == payload["spec_context"]["markdown"]
        assert loaded.spec_markdown in loaded.prompt

    async def test_the_bundle_matches_the_assembler_byte_for_byte(
        self, db_session, repo_row, linked_card
    ):
        """R3: dispatch does not curate, it calls the curator."""
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )
        direct = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo_row.id
        )
        assert payload["spec_context"]["markdown"] == direct["markdown"]

    async def test_a_step_template_can_place_the_bundle_itself(
        self, db_session, repo_row, linked_card
    ):
        payload = await attach(
            db_session,
            repo_row,
            step_config={
                "prompt_template": "HEAD\n{{spec_context}}\nTAIL",
            },
            trigger_context={"card_id": linked_card["id"]},
        )
        assert payload["prompt"].startswith("HEAD\n## Spec Context")
        assert payload["prompt"].endswith("TAIL")
        assert SPEC_CONTEXT_PLACEHOLDER not in payload["prompt"]


class TestNoLinksMeansNoBundleAndNoEmptySection:
    async def test_an_unlinked_card_puts_null_on_the_wire(
        self, db_session, repo_row, unlinked_card
    ):
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": unlinked_card["id"]},
        )
        assert payload["spec_context"] is None, (
            "None is the ONE spelling of 'no spec context' - not {}, not a "
            "bundle with empty markdown"
        )

    async def test_a_step_with_no_card_at_all_puts_null_on_the_wire(
        self, db_session, repo_row
    ):
        payload = await attach(db_session, repo_row, trigger_context={})
        assert payload["spec_context"] is None
        assert payload["card_id"] is None

    @pytest.mark.parametrize("card_key", ["unlinked", "none"])
    async def test_the_prompt_is_byte_identical_to_the_pre_12_6_6_one(
        self, db_session, repo_row, unlinked_card, card_key
    ):
        """THE no-op pin. A card with no spec links must produce no bundle AND
        no empty `## Spec Context` heading - the prompt has to be the exact
        string it was before this lane existed."""
        context = (
            {"card_id": unlinked_card["id"]} if card_key == "unlinked" else {}
        )
        payload = await attach(db_session, repo_row, trigger_context=context)

        assert payload["prompt"] == render_agent_prompt(
            card_title=payload["card_title"],
            card_description=payload["card_description"],
        )
        assert "Spec Context" not in payload["prompt"]
        assert SPEC_CONTEXT_PLACEHOLDER not in payload["prompt"]

    async def test_a_card_that_does_not_exist_puts_null_on_the_wire(
        self, db_session, repo_row
    ):
        payload = await attach(
            db_session, repo_row, trigger_context={"card_id": "no-such-card"}
        )
        assert payload["spec_context"] is None


class TestCurationIsObservableNotSilent:
    """R1. A curated brief that only becomes visible by reading a container's
    stdout after burning a run is dark - and a bundle that silently failed to
    assemble would be indistinguishable from a card with no spec links."""

    async def test_applying_curation_logs_what_was_applied(
        self, db_session, repo_row, linked_card, caplog
    ):
        with caplog.at_level("INFO", logger="app.services.pipeline_executor"):
            await attach(
                db_session,
                repo_row,
                trigger_context={"card_id": linked_card["id"]},
            )
        applied = [
            record.getMessage()
            for record in caplog.records
            if "spec context: APPLIED" in record.getMessage()
        ]
        assert len(applied) == 1, applied
        assert "1 criteria" in applied[0]
        assert "truncated=False" in applied[0]

    async def test_no_links_says_so_rather_than_saying_nothing(
        self, db_session, repo_row, unlinked_card, caplog
    ):
        with caplog.at_level("INFO", logger="app.services.pipeline_executor"):
            await attach(
                db_session,
                repo_row,
                trigger_context={"card_id": unlinked_card["id"]},
            )
        assert any(
            "no spec links" in record.getMessage() for record in caplog.records
        )

    async def test_disabling_curation_says_so(
        self, db_session, repo_row, linked_card, caplog
    ):
        """Disabled and no-links are the same `null` on the wire - truthfully,
        both are "no bundle" - and are distinguished in the LOG. The variant
        identity for a 12.6.5 A/B lives in the experiment's step config, which
        12.6.5 already records; a second wire field would be a second source
        of truth for it."""
        with caplog.at_level("INFO", logger="app.services.pipeline_executor"):
            payload = await attach(
                db_session,
                repo_row,
                step_config={"spec_context": False},
                trigger_context={"card_id": linked_card["id"]},
            )
        assert payload["spec_context"] is None
        assert any(
            "DISABLED by step config" in record.getMessage()
            for record in caplog.records
        )

    async def test_disabling_curation_leaves_the_prompt_untouched(
        self, db_session, repo_row, linked_card
    ):
        """The A/B lever has to change exactly one thing, or the experiment
        measures something else."""
        with_curation = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )
        without = await attach(
            db_session,
            repo_row,
            step_config={"spec_context": False},
            trigger_context={"card_id": linked_card["id"]},
        )
        assert without["prompt"] == render_agent_prompt(
            card_title=without["card_title"],
            card_description=without["card_description"],
        )
        assert with_curation["prompt"] != without["prompt"]


class TestWhereTheCardIdComesFrom:
    async def test_the_step_config_card_id_wins_over_the_trigger_context(
        self, db_session, repo_row, linked_card, unlinked_card
    ):
        payload = await attach(
            db_session,
            repo_row,
            step_config={"card_id": linked_card["id"]},
            trigger_context={"card_id": unlinked_card["id"]},
        )
        assert payload["card_id"] == linked_card["id"]
        assert payload["spec_context"] is not None

    async def test_the_trigger_context_card_id_is_used_when_the_step_has_none(
        self, db_session, repo_row, linked_card
    ):
        payload = await attach(
            db_session,
            repo_row,
            trigger_context={"card_id": linked_card["id"]},
        )
        assert payload["card_id"] == linked_card["id"]
