"""
Integration tests for the specification layer API (Phase 12.2.5).

Feature / UserStory / AcceptanceCriterion / PromptTemplate CRUD, nesting,
and the idempotent Milestone-12 seed endpoint.

The spec router is registered in main.py; if that registration is ever
dropped, these tests fail loudly with 404s (no local re-registration).
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_not_found,
    assert_json_list_length,
)


@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for repo_ids validation tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="SpecTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def feature(client):
    """Create a feature for story/criterion tests."""
    response = await client.post(
        "/api/features",
        json={"title": "Test Feature", "description": "A feature under test"},
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def story(client, feature):
    """Create a user story under the feature fixture."""
    response = await client.post(
        f"/api/features/{feature['id']}/stories",
        json={"title": "Test Story", "narrative": "When X, then Y"},
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestFeatureCrud:
    """Tests for /api/features CRUD."""

    async def test_create_feature_returns_id(self, client):
        response = await client.post(
            "/api/features",
            json={"title": "Revoke API keys", "description": "Security feature"},
        )
        result = assert_created_response(response, {"title": "Revoke API keys"})
        assert len(result["id"]) == 36
        assert result["status"] == "draft"
        assert result["repo_ids"] == []

    async def test_feature_spans_multiple_repos(self, client):
        """repo_ids accepts a list, validated against existing repos."""
        r1 = (await client.post("/api/repos", json=repo_create_payload(name="RepoOne"))).json()
        r2 = (await client.post("/api/repos", json=repo_create_payload(name="RepoTwo"))).json()

        response = await client.post(
            "/api/features",
            json={"title": "Cross-repo feature", "repo_ids": [r1["id"], r2["id"]]},
        )
        result = assert_created_response(response)
        assert set(result["repo_ids"]) == {r1["id"], r2["id"]}

    async def test_create_feature_rejects_unknown_repo_ids(self, client):
        response = await client.post(
            "/api/features",
            json={"title": "Bad repos", "repo_ids": ["no-such-repo"]},
        )
        assert_status_code(response, 400)
        assert "no-such-repo" in response.json()["detail"]

    async def test_create_feature_rejects_invalid_status(self, client):
        response = await client.post(
            "/api/features",
            json={"title": "Bad status", "status": "shipped"},
        )
        assert_status_code(response, 422)

    async def test_list_features(self, client):
        await client.post("/api/features", json={"title": "F1"})
        await client.post("/api/features", json={"title": "F2"})
        response = await client.get("/api/features")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_get_feature(self, client, feature):
        response = await client.get(f"/api/features/{feature['id']}")
        assert_status_code(response, 200)
        assert response.json()["title"] == "Test Feature"

    async def test_get_feature_not_found(self, client):
        response = await client.get("/api/features/nonexistent")
        assert_not_found(response, "Feature")

    async def test_update_feature(self, client, feature, repo):
        response = await client.patch(
            f"/api/features/{feature['id']}",
            json={"status": "active", "repo_ids": [repo["id"]]},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "active"
        assert result["repo_ids"] == [repo["id"]]

    async def test_update_feature_rejects_unknown_repo_ids(self, client, feature):
        response = await client.patch(
            f"/api/features/{feature['id']}",
            json={"repo_ids": ["ghost-repo"]},
        )
        assert_status_code(response, 400)

    async def test_delete_feature_cascades_stories(self, client, feature, story):
        """Removing a feature removes its stories and their criteria."""
        criterion = (
            await client.post(
                f"/api/user-stories/{story['id']}/criteria",
                json={"text": "Must work"},
            )
        ).json()

        response = await client.delete(f"/api/features/{feature['id']}")
        assert_status_code(response, 204)

        assert (await client.get(f"/api/features/{feature['id']}")).status_code == 404
        assert (await client.get(f"/api/user-stories/{story['id']}")).status_code == 404
        assert (await client.get(f"/api/criteria/{criterion['id']}")).status_code == 404


class TestUserStoryCrud:
    """Tests for /api/user-stories CRUD and nesting."""

    async def test_story_requires_feature(self, client):
        """Cannot create a story without feature_id on the flat route."""
        response = await client.post(
            "/api/user-stories",
            json={"title": "Orphan story"},
        )
        assert_status_code(response, 400)
        assert "feature_id" in response.json()["detail"]

    async def test_story_unknown_feature_404(self, client):
        response = await client.post(
            "/api/user-stories",
            json={"feature_id": "no-such-feature", "title": "Ghost story"},
        )
        assert_not_found(response, "Feature")

    async def test_create_story_flat_route(self, client, feature):
        response = await client.post(
            "/api/user-stories",
            json={"feature_id": feature["id"], "title": "Flat story", "priority": 3},
        )
        result = assert_created_response(response, {"title": "Flat story"})
        assert result["feature_id"] == feature["id"]
        assert result["priority"] == 3
        assert result["status"] == "draft"

    async def test_create_story_nested_route(self, client, feature):
        """POST /api/features/{id}/stories fills feature_id from the path."""
        response = await client.post(
            f"/api/features/{feature['id']}/stories",
            json={"title": "Nested story"},
        )
        result = assert_created_response(response)
        assert result["feature_id"] == feature["id"]

    async def test_story_priority_nullable(self, client, feature):
        response = await client.post(
            f"/api/features/{feature['id']}/stories",
            json={"title": "No priority"},
        )
        assert response.json()["priority"] is None

    async def test_story_narrative_freeform(self, client, feature):
        """No gherkin enforcement — markdown narratives pass through intact."""
        narrative = "## When\nA user pushes\n\n## Then\n- pipeline runs\n- **logs stream**"
        response = await client.post(
            f"/api/features/{feature['id']}/stories",
            json={"title": "Markdown story", "narrative": narrative},
        )
        assert response.json()["narrative"] == narrative

    async def test_list_stories_for_feature(self, client, feature):
        await client.post(
            f"/api/features/{feature['id']}/stories", json={"title": "S1"}
        )
        await client.post(
            f"/api/features/{feature['id']}/stories", json={"title": "S2"}
        )
        response = await client.get(f"/api/features/{feature['id']}/stories")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_stories_filter_by_feature(self, client, feature):
        other = (await client.post("/api/features", json={"title": "Other"})).json()
        await client.post(f"/api/features/{feature['id']}/stories", json={"title": "Mine"})
        await client.post(f"/api/features/{other['id']}/stories", json={"title": "Theirs"})

        response = await client.get(f"/api/user-stories?feature_id={feature['id']}")
        stories = response.json()
        assert len(stories) == 1
        assert stories[0]["title"] == "Mine"

    async def test_update_story(self, client, story):
        response = await client.patch(
            f"/api/user-stories/{story['id']}",
            json={"status": "in_progress", "priority": 1},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"
        assert result["priority"] == 1

    async def test_story_invalid_status_422(self, client, story):
        response = await client.patch(
            f"/api/user-stories/{story['id']}",
            json={"status": "wontfix"},
        )
        assert_status_code(response, 422)

    async def test_delete_story_cascades_criteria(self, client, story):
        criterion = (
            await client.post(
                f"/api/user-stories/{story['id']}/criteria",
                json={"text": "Cascades away"},
            )
        ).json()

        response = await client.delete(f"/api/user-stories/{story['id']}")
        assert_status_code(response, 204)
        assert (await client.get(f"/api/user-stories/{story['id']}")).status_code == 404
        assert (await client.get(f"/api/criteria/{criterion['id']}")).status_code == 404


class TestCriterionCrud:
    """Tests for /api/criteria CRUD and nesting."""

    async def test_criterion_requires_story(self, client):
        response = await client.post(
            "/api/criteria",
            json={"text": "Orphan criterion"},
        )
        assert_status_code(response, 400)
        assert "user_story_id" in response.json()["detail"]

    async def test_criterion_unknown_story_404(self, client):
        response = await client.post(
            "/api/criteria",
            json={"user_story_id": "no-such-story", "text": "Ghost"},
        )
        assert_not_found(response, "User story")

    async def test_create_criterion_flat_route(self, client, story):
        response = await client.post(
            "/api/criteria",
            json={"user_story_id": story["id"], "text": "Returns 401", "required": False},
        )
        result = assert_created_response(response, {"text": "Returns 401"})
        assert result["user_story_id"] == story["id"]
        assert result["required"] is False
        assert result["notes"] is None

    async def test_create_criterion_nested_route_defaults_required(self, client, story):
        response = await client.post(
            f"/api/user-stories/{story['id']}/criteria",
            json={"text": "Defaults to required"},
        )
        result = assert_created_response(response)
        assert result["required"] is True

    async def test_list_criteria_for_story(self, client, story):
        await client.post(
            f"/api/user-stories/{story['id']}/criteria", json={"text": "C1"}
        )
        await client.post(
            f"/api/user-stories/{story['id']}/criteria", json={"text": "C2"}
        )
        response = await client.get(f"/api/user-stories/{story['id']}/criteria")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_update_criterion(self, client, story):
        criterion = (
            await client.post(
                f"/api/user-stories/{story['id']}/criteria",
                json={"text": "Old text"},
            )
        ).json()
        response = await client.patch(
            f"/api/criteria/{criterion['id']}",
            json={"text": "New text", "notes": "clarified"},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["text"] == "New text"
        assert result["notes"] == "clarified"

    async def test_delete_criterion(self, client, story):
        criterion = (
            await client.post(
                f"/api/user-stories/{story['id']}/criteria",
                json={"text": "Doomed"},
            )
        ).json()
        response = await client.delete(f"/api/criteria/{criterion['id']}")
        assert_status_code(response, 204)
        assert (await client.get(f"/api/criteria/{criterion['id']}")).status_code == 404

    async def test_criterion_can_have_no_tests(self, client, story):
        """A non-required criterion exists without TestRefs; since 12.2.6
        activated the blocks-done rule only REQUIRED criteria block, so the
        story can still reach 'done'."""
        await client.post(
            f"/api/user-stories/{story['id']}/criteria",
            json={"text": "Nice-to-have, unverified", "required": False},
        )
        response = await client.patch(
            f"/api/user-stories/{story['id']}",
            json={"status": "done"},
        )
        assert_status_code(response, 200)
        assert response.json()["status"] == "done"

    async def test_required_criterion_blocks_story_done_requires_testruns(
        self, client, story
    ):
        """Phase 12.2.6 activation: a required criterion lacking a passing
        TestRun blocks its story from 'done' (was xfail(strict) in 12.2.5).
        Deeper coverage lives in test_blocks_done_activation.py."""
        await client.post(
            f"/api/user-stories/{story['id']}/criteria",
            json={"text": "Must be verified by a passing TestRun", "required": True},
        )
        response = await client.patch(
            f"/api/user-stories/{story['id']}",
            json={"status": "done"},
        )
        assert response.status_code == 409


class TestPromptTemplates:
    """Tests for /api/prompt-templates CRUD."""

    @pytest.mark.lazyaf_test_id("us3.prompt-variants-storable")
    async def test_create_prompt_template(self, client):
        response = await client.post(
            "/api/prompt-templates",
            json={
                "name": "implement-story",
                "description": "Implements a story",
                "content": "Implement: {story_narrative}",
            },
        )
        result = assert_created_response(response, {"name": "implement-story"})
        assert result["content"] == "Implement: {story_narrative}"

    async def test_prompt_template_name_unique(self, client):
        await client.post("/api/prompt-templates", json={"name": "dup"})
        response = await client.post("/api/prompt-templates", json={"name": "dup"})
        assert_status_code(response, 409)

    async def test_list_prompt_templates(self, client):
        await client.post("/api/prompt-templates", json={"name": "t1"})
        await client.post("/api/prompt-templates", json={"name": "t2"})
        response = await client.get("/api/prompt-templates")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_get_update_delete_prompt_template(self, client):
        template = (
            await client.post("/api/prompt-templates", json={"name": "lifecycle"})
        ).json()

        response = await client.get(f"/api/prompt-templates/{template['id']}")
        assert_status_code(response, 200)

        response = await client.patch(
            f"/api/prompt-templates/{template['id']}",
            json={"content": "new body"},
        )
        assert response.json()["content"] == "new body"

        response = await client.delete(f"/api/prompt-templates/{template['id']}")
        assert_status_code(response, 204)
        assert (
            await client.get(f"/api/prompt-templates/{template['id']}")
        ).status_code == 404

    async def test_rename_prompt_template_to_existing_conflict(self, client):
        await client.post("/api/prompt-templates", json={"name": "taken"})
        other = (
            await client.post("/api/prompt-templates", json={"name": "renamer"})
        ).json()
        response = await client.patch(
            f"/api/prompt-templates/{other['id']}",
            json={"name": "taken"},
        )
        assert_status_code(response, 409)


class TestSeedMilestone12:
    """Tests for POST /api/features/seed-milestone12."""

    @pytest.mark.lazyaf_test_id("us3.criteria-queryable-for-scoring")
    async def test_seed_creates_the_north_star_stories(self, client):
        from app.routers.spec import MILESTONE12_STORIES

        response = await client.post("/api/features/seed-milestone12")
        assert_status_code(response, 200)
        body = response.json()
        assert body["created"] is True
        assert body["feature"]["title"] == "LazyAF Milestone 12"
        assert body["feature"]["status"] == "active"

        stories_response = await client.get(
            f"/api/features/{body['feature']['id']}/stories"
        )
        stories = stories_response.json()
        assert len(stories) == len(MILESTONE12_STORIES)
        assert {s["title"] for s in stories} == {
            s["title"] for s in MILESTONE12_STORIES
        }
        # US-4 is the Milestone 14 story; its absence is what this seed's
        # 2026-08-30 audit existed to fix, so pin it by name rather than count.
        assert "US-4 Agents run on self-hosted models" in {
            s["title"] for s in stories
        }

        # Every seeded criterion lands with its text, its required flag AND the
        # notes that say what the evidence for it is. A criterion seeded with
        # no notes is one whose status nobody can check from the UI.
        by_title = {s["title"]: s for s in MILESTONE12_STORIES}
        for s in stories:
            criteria = (
                await client.get(f"/api/user-stories/{s['id']}/criteria")
            ).json()
            expected = by_title[s["title"]]["criteria"]
            assert len(criteria) == len(expected)
            actual = {c["text"]: c for c in criteria}
            for criterion_def in expected:
                got = actual[criterion_def["text"]]
                assert got["required"] is criterion_def["required"]
                assert got["notes"] == criterion_def["notes"]
                assert got["notes"].strip(), "a criterion must carry evidence"

    async def test_every_criterion_declares_its_evidence(self):
        """Each seeded criterion's notes must open with one of the three
        sanctioned shapes. This is the seed's own honesty rule: a criterion
        may be verified, or covered-but-untied, or unmet — but it may not
        simply assert itself with no account of why anyone believes it."""
        from app.routers.spec import MILESTONE12_STORIES, MILESTONE12_TEST_REF_SEEDS

        tied = {(s["story"], s["criterion"]) for s in MILESTONE12_TEST_REF_SEEDS}
        for story_index, story_def in enumerate(MILESTONE12_STORIES):
            for criterion_index, criterion_def in enumerate(story_def["criteria"]):
                notes = criterion_def["notes"]
                where = f"{story_def['title']} #{criterion_index}"
                assert notes.startswith(
                    ("TIED BACK", "COVERED, NOT TIED BACK", "NOT MET")
                ), f"{where}: notes must declare evidence, got {notes[:40]!r}"

                # And the claim must match the map: only a criterion an actual
                # TestRef seed points at may call itself TIED BACK.
                is_tied = (story_index, criterion_index) in tied
                assert is_tied == notes.startswith("TIED BACK"), (
                    f"{where}: notes say {notes[:20]!r} but "
                    f"MILESTONE12_TEST_REF_SEEDS {'does' if is_tied else 'does not'} "
                    "point here"
                )

    async def test_seed_is_idempotent(self, client):
        from app.routers.spec import MILESTONE12_STORIES

        first = (await client.post("/api/features/seed-milestone12")).json()
        second = (await client.post("/api/features/seed-milestone12")).json()

        assert first["created"] is True
        assert second["created"] is False
        assert second["feature"]["id"] == first["feature"]["id"]

        # Still exactly one north-star feature, with one copy of each story
        features = (await client.get("/api/features")).json()
        milestone = [f for f in features if f["title"] == "LazyAF Milestone 12"]
        assert len(milestone) == 1
        stories = (
            await client.get(f"/api/features/{milestone[0]['id']}/stories")
        ).json()
        assert len(stories) == len(MILESTONE12_STORIES)

        # ...and one copy of each criterion. Reconciliation re-runs on every
        # seed, so a duplicate here is the bug it would introduce.
        for s in stories:
            criteria = (
                await client.get(f"/api/user-stories/{s['id']}/criteria")
            ).json()
            texts = [c["text"] for c in criteria]
            assert len(texts) == len(set(texts))

    async def test_seeded_stories_queryable_via_api(self, client):
        """The north-star stories are queryable through the flat API."""
        seeded = (await client.post("/api/features/seed-milestone12")).json()
        response = await client.get(
            f"/api/user-stories?feature_id={seeded['feature']['id']}"
        )
        assert_status_code(response, 200)
        narratives = " ".join(s["narrative"] for s in response.json())
        assert "push to the internal remote" in narratives
        assert "gating" in narratives
        assert "side-by-side" in narratives
        # US-4's narrative reaches the API too, not just the constants.
        assert "OpenAI-compatible model endpoint" in narratives

    async def _seeded_criterion_ids(self, client, feature_id):
        """All criterion ids under the seeded feature's stories."""
        stories = (
            await client.get(f"/api/features/{feature_id}/stories")
        ).json()
        criterion_ids = set()
        for s in stories:
            criteria = (
                await client.get(f"/api/user-stories/{s['id']}/criteria")
            ).json()
            criterion_ids |= {c["id"] for c in criteria}
        return criterion_ids

    async def test_seed_without_repo_creates_no_test_refs(self, client, db_session):
        """Without a repo_id there is nothing to hang TestRefs on (repo_id is
        NOT NULL); the spec seed still succeeds, refs are just skipped."""
        from sqlalchemy import select

        from app.models import TestRef

        response = await client.post("/api/features/seed-milestone12")
        assert_status_code(response, 200)
        result = await db_session.execute(select(TestRef))
        assert result.scalars().all() == []

    async def test_seed_upserts_starter_set_test_refs(self, client, db_session, repo):
        """Phase 12.2.6 seed linkage: seeding with a repo_id also upserts an
        ACTIVE TestRef for every starter-set lazyaf_test_id, each linked to
        the matching seeded criterion (map: spec.py MILESTONE12_TEST_REF_SEEDS)."""
        from sqlalchemy import select

        from app.models import TestRef
        from app.routers.spec import MILESTONE12_TEST_REF_SEEDS

        seeded = (
            await client.post(
                "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
            )
        ).json()
        criterion_ids = await self._seeded_criterion_ids(
            client, seeded["feature"]["id"]
        )

        result = await db_session.execute(select(TestRef))
        refs = {r.lazyaf_test_id: r for r in result.scalars().all()}
        expected_ids = {s["lazyaf_test_id"] for s in MILESTONE12_TEST_REF_SEEDS}
        assert expected_ids <= set(refs)
        for seed in MILESTONE12_TEST_REF_SEEDS:
            ref = refs[seed["lazyaf_test_id"]]
            assert ref.status == "active"
            assert ref.file_path == seed["file_path"]
            assert ref.criterion_id in criterion_ids

    async def test_every_seeded_tie_back_points_at_a_real_marker(self):
        """THE TIE-BACK GUARD.

        Every lazyaf_test_id in MILESTONE12_TEST_REF_SEEDS must correspond to
        a real @pytest.mark.lazyaf_test_id marker in the tdd suite, in the file
        the seed claims. A seeded ref whose marker has been renamed or deleted
        is worse than no ref at all: the criterion keeps a TestRef, the UI
        keeps showing it as measured, and no TestRun ever arrives — so it
        silently verifies nothing and never goes red to say so.

        Scanned from the source tree rather than the database, so this fails in
        T1 the moment somebody renames a marker, without needing a dogfood run.
        """
        import re

        from app.routers.spec import MILESTONE12_TEST_REF_SEEDS

        tdd_root = Path(__file__).parent.parent.parent
        marker = re.compile(r"""lazyaf_test_id\(\s*["']([^"']+)["']\s*\)""")

        declared: dict[str, set[str]] = {}
        for path in tdd_root.rglob("*.py"):
            # errors="replace": marker ids are ASCII, so a stray undecodable
            # byte in some unrelated test file cannot hide one — and must not
            # turn this guard red for a reason that has nothing to do with it.
            source = path.read_text(encoding="utf-8", errors="replace")
            for found in marker.findall(source):
                rel = path.relative_to(tdd_root.parent).as_posix()
                declared.setdefault(found, set()).add(rel)

        missing = [
            s["lazyaf_test_id"]
            for s in MILESTONE12_TEST_REF_SEEDS
            if s["lazyaf_test_id"] not in declared
        ]
        assert not missing, (
            "seeded tie-backs name markers that do not exist in tdd/: "
            f"{missing}. Either the marker was renamed/deleted (re-point or "
            "drop the seed entry) or the test was never written."
        )

        misfiled = [
            (s["lazyaf_test_id"], s["file_path"], sorted(declared[s["lazyaf_test_id"]]))
            for s in MILESTONE12_TEST_REF_SEEDS
            if s["file_path"] not in declared[s["lazyaf_test_id"]]
        ]
        assert not misfiled, (
            "seeded tie-backs name the wrong file for their marker "
            f"(id, seeded path, actual paths): {misfiled}"
        )

    async def test_reconcile_rewrites_retired_text_in_place(
        self, client, db_session, repo
    ):
        """An already-seeded database converges on the current constants.

        This is the whole point of reconciliation: the Specs UI renders a
        database that was seeded long ago, so a correction to a criterion's
        text has to reach THAT row. Rewriting in place (rather than orphaning
        the old row and inserting a new one) is what keeps the TestRefs
        already pointing at it attached.
        """
        from sqlalchemy import select

        from app.models import AcceptanceCriterion, TestRef
        from app.routers.spec import MILESTONE12_STORIES

        await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )

        # Find a criterion the constants declare as superseding older text, and
        # wind that row back to the retired wording, as an old database has it.
        story_def, criterion_def = next(
            (s, c)
            for s in MILESTONE12_STORIES
            for c in s["criteria"]
            if c.get("supersedes")
        )
        retired = criterion_def["supersedes"][0]

        row = (
            await db_session.execute(
                select(AcceptanceCriterion).where(
                    AcceptanceCriterion.text == criterion_def["text"]
                )
            )
        ).scalar_one()
        original_id = row.id
        row.text = retired
        row.notes = None
        await db_session.commit()

        response = await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )
        assert response.json()["created"] is False

        # Same row, corrected text, notes restored — and the retired wording
        # is gone rather than lingering beside its own replacement.
        rows = (
            await db_session.execute(
                select(AcceptanceCriterion).where(
                    AcceptanceCriterion.user_story_id == row.user_story_id
                )
            )
        ).scalars().all()
        texts = [c.text for c in rows]
        assert retired not in texts
        assert texts.count(criterion_def["text"]) == 1
        assert len(rows) == len(story_def["criteria"])

        corrected = next(c for c in rows if c.text == criterion_def["text"])
        assert corrected.id == original_id, "rewritten in place, not replaced"
        assert corrected.notes == criterion_def["notes"]

        # The TestRefs that pointed at that row still point at it.
        refs = (
            await db_session.execute(
                select(TestRef).where(TestRef.criterion_id == original_id)
            )
        ).scalars().all()
        assert refs, "the rewrite must not detach the criterion's TestRefs"

    async def test_reseeding_a_pre_audit_database_converges(self, client, db_session):
        """The scenario this whole reconciliation exists for.

        The owner's database was seeded before the 2026-08-30 audit: three
        stories, no notes on any criterion, the old feature description, and
        US-3's criterion still naming tdd/e2e/test_experiment_matrix.py — a
        file that has never existed. That database is what the Specs UI
        renders. Re-seeding must bring ALL of it forward, because a correction
        that only lands on fresh installs is not a correction.
        """
        from sqlalchemy import select

        from app.models import AcceptanceCriterion, UserStory
        from app.models.spec import Feature
        from app.routers.spec import (
            MILESTONE12_FEATURE_DESCRIPTION,
            MILESTONE12_STORIES,
        )

        seeded = (await client.post("/api/features/seed-milestone12")).json()
        feature_id = seeded["feature"]["id"]

        # --- wind the database back to its pre-audit shape -------------------
        stories = (
            await db_session.execute(
                select(UserStory).where(UserStory.feature_id == feature_id)
            )
        ).scalars().all()
        by_title = {s.title: s for s in stories}

        # US-4 did not exist before the audit.
        await db_session.delete(by_title["US-4 Agents run on self-hosted models"])
        # Nothing carried notes.
        for story in stories:
            for criterion in (
                await db_session.execute(
                    select(AcceptanceCriterion).where(
                        AcceptanceCriterion.user_story_id == story.id
                    )
                )
            ).scalars():
                criterion.notes = None
        # US-3 still named the file that never existed.
        us3 = by_title["US-3 Compare bench"]
        retired = next(
            c["supersedes"][0]
            for c in MILESTONE12_STORIES[2]["criteria"]
            if c.get("supersedes")
        )
        stale = (
            await db_session.execute(
                select(AcceptanceCriterion).where(
                    AcceptanceCriterion.user_story_id == us3.id,
                    AcceptanceCriterion.text.like("The comparison's inputs%"),
                )
            )
        ).scalar_one()
        stale.text = retired
        # ...and the feature description predated US-4 entirely.
        feature_row = (
            await db_session.execute(
                select(Feature).where(Feature.id == feature_id)
            )
        ).scalar_one()
        feature_row.description = "an old description"
        await db_session.commit()

        # --- re-seed ---------------------------------------------------------
        response = await client.post("/api/features/seed-milestone12")
        assert_status_code(response, 200)
        assert response.json()["created"] is False
        assert response.json()["feature"]["id"] == feature_id
        assert response.json()["feature"]["description"] == (
            MILESTONE12_FEATURE_DESCRIPTION
        )

        # US-4 is back, every story is present exactly once...
        stories = (
            await client.get(f"/api/features/{feature_id}/stories")
        ).json()
        titles = [s["title"] for s in stories]
        assert sorted(titles) == sorted(
            s["title"] for s in MILESTONE12_STORIES
        ), titles

        # ...every criterion matches the constants, notes and all, and the
        # claim about the nonexistent file is gone from the database.
        expected = {s["title"]: s["criteria"] for s in MILESTONE12_STORIES}
        all_texts = []
        for s in stories:
            criteria = (
                await client.get(f"/api/user-stories/{s['id']}/criteria")
            ).json()
            actual = {c["text"]: c for c in criteria}
            all_texts.extend(actual)
            assert len(criteria) == len(expected[s["title"]])
            for criterion_def in expected[s["title"]]:
                got = actual[criterion_def["text"]]
                assert got["notes"] == criterion_def["notes"]
                assert got["required"] is criterion_def["required"]

        assert retired not in all_texts
        assert not any("test_experiment_matrix.py" in t for t in all_texts)

    async def test_reconcile_leaves_hand_added_criteria_alone(self, client):
        """Reconciliation never deletes. A criterion the constants do not
        mention may be the owner's own, so re-seeding must not tidy it away."""
        seeded = (await client.post("/api/features/seed-milestone12")).json()
        stories = (
            await client.get(f"/api/features/{seeded['feature']['id']}/stories")
        ).json()
        target = stories[0]

        mine = (
            await client.post(
                f"/api/user-stories/{target['id']}/criteria",
                json={"text": "My own criterion", "required": False},
            )
        ).json()

        await client.post("/api/features/seed-milestone12")

        response = await client.get(f"/api/criteria/{mine['id']}")
        assert_status_code(response, 200)
        assert response.json()["text"] == "My own criterion"
        assert response.json()["required"] is False

    async def test_seed_scopes_refs_to_the_repo_it_was_given(
        self, client, db_session, repo
    ):
        """Contract #1 (models/testref.py): TestRef identity is the PAIR
        (repo_id, lazyaf_test_id). Seeding repo A then repo B must give BOTH
        repos their own starter-set refs — re-pointing repo A's rows would
        leave repo B's criteria measured by nothing while reporting success.
        """
        from sqlalchemy import select

        from app.models import TestRef
        from app.routers.spec import MILESTONE12_TEST_REF_SEEDS

        other = (
            await client.post(
                "/api/repos", json=repo_create_payload(name="SecondSpecRepo")
            )
        ).json()

        await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )
        await client.post(
            "/api/features/seed-milestone12", json={"repo_id": other["id"]}
        )

        expected = {s["lazyaf_test_id"] for s in MILESTONE12_TEST_REF_SEEDS}
        for repo_id in (repo["id"], other["id"]):
            rows = (
                await db_session.execute(
                    select(TestRef).where(TestRef.repo_id == repo_id)
                )
            ).scalars().all()
            assert {r.lazyaf_test_id for r in rows} == expected, (
                f"repo {repo_id} did not get its own starter-set refs"
            )
            assert all(r.status == "active" for r in rows)
            assert all(r.criterion_id is not None for r in rows)

    async def test_seed_ignores_the_same_marker_owned_by_another_repo(
        self, client, db_session, repo
    ):
        """A starter-set marker already registered under a DIFFERENT repo (as
        ingestion's auto-created orphan does) must not make the seed ambiguous.
        Looking the ref up by marker alone matches both rows and dies."""
        from sqlalchemy import select

        from app.models import TestRef
        from app.routers.spec import MILESTONE12_TEST_REF_SEEDS

        other = (
            await client.post(
                "/api/repos", json=repo_create_payload(name="ThirdSpecRepo")
            )
        ).json()
        marker = MILESTONE12_TEST_REF_SEEDS[0]["lazyaf_test_id"]
        db_session.add(
            TestRef(
                lazyaf_test_id=marker,
                repo_id=other["id"],
                file_path="somewhere/else.py",
                status="orphan",
            )
        )
        await db_session.commit()

        response = await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )
        assert_status_code(response, 200)

        # The other repo's row is untouched; ours is created and linked.
        theirs = (
            await db_session.execute(
                select(TestRef).where(
                    TestRef.repo_id == other["id"],
                    TestRef.lazyaf_test_id == marker,
                )
            )
        ).scalar_one()
        assert theirs.status == "orphan"
        assert theirs.file_path == "somewhere/else.py"
        assert theirs.criterion_id is None

        mine = (
            await db_session.execute(
                select(TestRef).where(
                    TestRef.repo_id == repo["id"],
                    TestRef.lazyaf_test_id == marker,
                )
            )
        ).scalar_one()
        assert mine.status == "active"
        assert mine.criterion_id is not None

    async def test_seed_test_refs_idempotent_and_repairs(
        self, client, db_session, repo
    ):
        """Re-seeding neither duplicates TestRefs nor leaves an orphaned
        starter-set ref orphaned — the upsert flips it back to active."""
        from sqlalchemy import select

        from app.models import TestRef
        from app.routers.spec import MILESTONE12_TEST_REF_SEEDS

        await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )

        result = await db_session.execute(select(TestRef))
        refs = result.scalars().all()
        first_count = len(refs)

        # Damage one starter-set ref, then re-seed
        victim_id = MILESTONE12_TEST_REF_SEEDS[0]["lazyaf_test_id"]
        victim = next(r for r in refs if r.lazyaf_test_id == victim_id)
        victim.status = "orphan"
        victim.criterion_id = None
        await db_session.commit()

        response = await client.post(
            "/api/features/seed-milestone12", json={"repo_id": repo["id"]}
        )
        assert response.json()["created"] is False

        result = await db_session.execute(select(TestRef))
        refs = result.scalars().all()
        assert len(refs) == first_count
        repaired = next(r for r in refs if r.lazyaf_test_id == victim_id)
        assert repaired.status == "active"
        assert repaired.criterion_id is not None
