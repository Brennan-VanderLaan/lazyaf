"""
Integration: spec rows created THROUGH THE API become the curated agent brief.

The unit suite (`tdd/unit/services/test_spec_context_bundle.py`) builds ORM
rows by hand. This one starts where a user starts - `POST /api/features`,
`POST /api/features/{id}/stories`, `POST /api/user-stories/{id}/criteria`,
`POST /api/test-refs/reconcile`, `POST /api/repos/{id}/cards` - and follows
that data all the way onto the wire and back off it:

    spec API rows
        -> app.services.spec_context.build_spec_context   (ASSEMBLER)
        -> control_layer.workspace.generate_agent_config   (PRODUCER)
        -> a real /workspace/.control/agent.*.json file
        -> runner_common.agent_config.load_agent_config    (CONSUMER)

It exists because every layer in that chain is separately tested against a
fixture, and a fixture cannot catch the one failure that matters here: a field
the API writes differently from the way the assembler reads it. Notably
`AcceptanceCriterion.required` and `notes` (defaults applied by the pydantic
schema, not the model) and `TestRef.file_path` (normalised repo-root-relative
by the reconcile endpoint, contract #3).

The spec, test-ref and card routers are registered in main.py; if any of those
registrations is dropped these tests fail loudly with 404s - no local
re-registration.
"""
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

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

from app.models.testref import TestRef  # noqa: E402
from app.services.control_layer.workspace import (  # noqa: E402
    generate_agent_config,
)
from app.services.spec_context import build_spec_context  # noqa: E402

from runner_common.agent_config import load_agent_config  # noqa: E402

from tdd.unit.control_runtime.spec_context_contract import (  # noqa: E402
    assert_bundle_conforms,
)


@pytest_asyncio.fixture
async def repo(client):
    response = await client.post(
        "/api/repos", json=repo_create_payload(name="SpecContextRepo")
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def spec(client):
    """A feature, a story and three criteria, all through the public API."""
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
    criteria = []
    for text, required, notes in (
        ("A repo over its budget receives HTTP 429.", True, "per minute, not per hour"),
        ("The 429 body names the retry-after seconds.", True, None),
        ("Rate-limit headers are emitted on every response.", False, None),
    ):
        response = await client.post(
            f"/api/user-stories/{story['id']}/criteria",
            json={"text": text, "required": required, "notes": notes},
        )
        assert response.status_code == 201, response.text
        criteria.append(response.json())
    return {"feature": feature, "story": story, "criteria": criteria}


@pytest_asyncio.fixture
async def linked_card(client, repo, spec):
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(
            title="Implement the per-repo budget",
            description="",
        ),
    )
    assert response.status_code == 201, response.text
    card = response.json()
    patched = await client.patch(
        f"/api/cards/{card['id']}",
        json={"user_story_id": spec["story"]["id"]},
    )
    assert patched.status_code == 200, patched.text
    return patched.json()


async def _register_test(client, db_session, repo, criterion_id, test_id, file_path):
    """Register a test through the reconcile endpoint, then join it to a
    criterion the way the seeder/ingestion does (there is no public route for
    the join in 12.6.6)."""
    response = await client.post(
        "/api/test-refs/reconcile",
        json={
            "repo_id": repo["id"],
            "refs": [{"lazyaf_test_id": test_id, "file_path": file_path}],
        },
    )
    assert response.status_code == 200, response.text
    ref = (
        await db_session.execute(
            select(TestRef).where(
                TestRef.repo_id == repo["id"], TestRef.lazyaf_test_id == test_id
            )
        )
    ).scalar_one()
    ref.criterion_id = criterion_id
    await db_session.flush()
    return ref


class TestBundleFromApiCreatedRows:
    async def test_the_bundle_carries_what_the_api_was_given(
        self, client, db_session, repo, spec, linked_card
    ):
        payload = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )
        assert_bundle_conforms(payload, "ASSEMBLER (spec_context)")

        markdown = payload["markdown"]
        assert "Per-repo API rate limiting" in markdown
        assert "Protect the public API from runaway clients." in markdown
        assert "Operator sets a per-repo request budget" in markdown
        assert "cannot starve the others" in markdown
        for criterion in spec["criteria"]:
            assert criterion["text"] in markdown
        assert payload["criteria_count"] == 3
        assert payload["source"]["user_story_id"] == spec["story"]["id"]
        assert payload["source"]["feature_id"] == spec["feature"]["id"]

    async def test_required_and_notes_survive_the_schema_defaults(
        self, client, db_session, repo, spec, linked_card
    ):
        """`required` defaults True and `notes` defaults None in the pydantic
        schema, not the model - the assembler reads the model."""
        payload = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )
        markdown = payload["markdown"]

        assert "[required] " in markdown
        assert "[optional] " in markdown
        assert "note: per minute, not per hour" in markdown

    async def test_reconciled_test_paths_reach_the_bundle(
        self, client, db_session, repo, spec, linked_card
    ):
        """Contract #3: the reconcile endpoint normalises file_path to
        repo-root-relative, and the bundle surfaces it verbatim so
        `/workspace/repo/<file_path>` resolves."""
        await _register_test(
            client,
            db_session,
            repo,
            spec["criteria"][0]["id"],
            "rl-429",
            "tests/api/test_rate_limit.py",
        )

        payload = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )

        assert payload["test_ref_count"] == 1
        assert "tests/api/test_rate_limit.py" in payload["markdown"]
        assert 'lazyaf_test_id "rl-429", last run: never' in payload["markdown"]

    async def test_an_orphaned_ref_leaves_the_bundle(
        self, client, db_session, repo, spec, linked_card
    ):
        """A second reconcile that omits the id flips it to ORPHAN; the brief
        must stop naming a path the repo no longer declares."""
        await _register_test(
            client,
            db_session,
            repo,
            spec["criteria"][0]["id"],
            "rl-429",
            "tests/api/test_rate_limit.py",
        )
        await db_session.commit()

        response = await client.post(
            "/api/test-refs/reconcile", json={"repo_id": repo["id"], "refs": []}
        )
        assert response.status_code == 200, response.text

        payload = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )

        assert payload["test_ref_count"] == 0
        assert "test_rate_limit.py" not in payload["markdown"]


class TestUnlinkedCards:
    async def test_a_card_with_no_spec_links_has_no_bundle(
        self, client, db_session, repo
    ):
        card = (
            await client.post(
                f"/api/repos/{repo['id']}/cards",
                json=card_create_payload(title="Bump the linter", description=""),
            )
        ).json()

        payload = await build_spec_context(
            db_session, card_id=card["id"], repo_id=repo["id"]
        )

        assert payload is None, (
            "None is the ONE spelling of 'no spec context' - not {}, not an "
            "empty markdown string, and not an empty '## Spec Context' heading"
        )

    async def test_unlinking_a_card_removes_its_bundle(
        self, client, db_session, repo, spec, linked_card
    ):
        assert (
            await build_spec_context(
                db_session, card_id=linked_card["id"], repo_id=repo["id"]
            )
            is not None
        )

        response = await client.patch(
            f"/api/cards/{linked_card['id']}", json={"user_story_id": None}
        )
        assert response.status_code == 200, response.text

        assert (
            await build_spec_context(
                db_session, card_id=linked_card["id"], repo_id=repo["id"]
            )
            is None
        )

    async def test_an_unknown_card_has_no_bundle(self, db_session, repo):
        assert (
            await build_spec_context(
                db_session, card_id="no-such-card", repo_id=repo["id"]
            )
            is None
        )


class TestTheWholeChain:
    async def test_api_rows_reach_the_wrapper_intact(
        self, client, db_session, repo, spec, linked_card, tmp_path
    ):
        """ASSEMBLER -> PRODUCER -> a real file -> CONSUMER, in one test.

        This is the only place the three sides meet over data a USER created.
        """
        await _register_test(
            client,
            db_session,
            repo,
            spec["criteria"][0]["id"],
            "rl-429",
            "tests/api/test_rate_limit.py",
        )

        bundle = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )
        assert_bundle_conforms(bundle, "ASSEMBLER (spec_context)")

        produced = generate_agent_config(
            agent="claude-code",
            prompt="Implement the feature.\n\n" + bundle["markdown"],
            card_id=linked_card["id"],
            card_title=linked_card["title"],
            repo_id=repo["id"],
            spec_context=bundle,
        )
        config_path = tmp_path / "agent.exec-1.json"
        config_path.write_text(json.dumps(produced), encoding="utf-8")

        loaded = load_agent_config(config_path)

        assert loaded is not None
        assert_bundle_conforms(loaded.spec_context, "CONSUMER (agent config)")
        assert loaded.spec_markdown == bundle["markdown"]
        assert loaded.has_spec_context is True
        # The prompt is the channel; the sidecar is the reference copy. They
        # must be the same text, or the file the agent re-reads is a lie.
        assert loaded.spec_markdown in loaded.prompt

    async def test_no_links_puts_nothing_on_the_wire(
        self, client, db_session, repo, tmp_path
    ):
        card = (
            await client.post(
                f"/api/repos/{repo['id']}/cards",
                json=card_create_payload(title="Bump the linter", description=""),
            )
        ).json()

        bundle = await build_spec_context(
            db_session, card_id=card["id"], repo_id=repo["id"]
        )
        produced = generate_agent_config(
            agent="claude-code",
            prompt="Implement the feature.",
            card_id=card["id"],
            repo_id=repo["id"],
            spec_context=bundle,
        )
        config_path = tmp_path / "agent.exec-2.json"
        config_path.write_text(json.dumps(produced), encoding="utf-8")

        loaded = load_agent_config(config_path)

        assert produced["spec_context"] is None
        assert loaded is not None
        assert loaded.has_spec_context is False
        assert loaded.prompt == "Implement the feature."
