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
    async def test_seed_creates_feature_with_three_stories(self, client):
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
        assert len(stories) == 3
        titles = {s["title"] for s in stories}
        assert titles == {
            "US-1 Commits land, AI workflows run",
            "US-2 Card dev loop",
            "US-3 Compare bench",
        }

        # Every north-star story carries required acceptance criteria
        for s in stories:
            criteria_response = await client.get(
                f"/api/user-stories/{s['id']}/criteria"
            )
            criteria = criteria_response.json()
            assert len(criteria) >= 3
            assert all(c["required"] is True for c in criteria)

    async def test_seed_is_idempotent(self, client):
        first = (await client.post("/api/features/seed-milestone12")).json()
        second = (await client.post("/api/features/seed-milestone12")).json()

        assert first["created"] is True
        assert second["created"] is False
        assert second["feature"]["id"] == first["feature"]["id"]

        # Still exactly one Milestone 12 feature and three stories
        features = (await client.get("/api/features")).json()
        milestone = [f for f in features if f["title"] == "LazyAF Milestone 12"]
        assert len(milestone) == 1
        stories = (
            await client.get(f"/api/features/{milestone[0]['id']}/stories")
        ).json()
        assert len(stories) == 3

    async def test_seeded_stories_queryable_via_api(self, client):
        """The three north-star stories are queryable through the flat API."""
        seeded = (await client.post("/api/features/seed-milestone12")).json()
        response = await client.get(
            f"/api/user-stories?feature_id={seeded['feature']['id']}"
        )
        assert_status_code(response, 200)
        narratives = " ".join(s["narrative"] for s in response.json())
        assert "push to the internal remote" in narratives
        assert "gating" in narratives
        assert "side-by-side" in narratives

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
