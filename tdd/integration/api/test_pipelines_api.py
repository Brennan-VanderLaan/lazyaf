"""
Integration tests for Pipelines API endpoints.

These tests verify the full request/response cycle for pipeline management,
including CRUD operations, validation, and error handling.
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

from shared.factories import (
    repo_create_payload,
    pipeline_create_payload,
    pipeline_update_payload,
    pipeline_step_payload,
)
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for pipeline tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="PipelineTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def pipeline(client, repo):
    """Create a pipeline for tests that need one."""
    steps = [
        pipeline_step_payload(name="Test", step_type="script", config={"command": "npm test"}),
    ]
    response = await client.post(
        f"/api/repos/{repo['id']}/pipelines",
        json=pipeline_create_payload(name="Test Pipeline", steps=steps),
    )
    return response.json()


class TestListPipelines:
    """Tests for GET /api/pipelines and /api/repos/{repo_id}/pipelines endpoints."""

    async def test_list_pipelines_empty(self, client, repo):
        """Returns empty list when repo has no pipelines."""
        response = await client.get(f"/api/repos/{repo['id']}/pipelines")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_pipelines_with_data(self, client, repo):
        """Returns all pipelines for a repo."""
        # Create pipelines
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Pipeline 1"),
        )
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Pipeline 2"),
        )

        response = await client.get(f"/api/repos/{repo['id']}/pipelines")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_pipelines_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/pipelines")
        assert_not_found(response, "Repo")

    async def test_list_all_pipelines(self, client, repo):
        """GET /api/pipelines returns all pipelines across repos."""
        # Create a pipeline
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Test Pipeline"),
        )

        response = await client.get("/api/pipelines")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert len(pipelines) >= 1

    async def test_list_all_pipelines_filter_by_repo(self, client, repo):
        """GET /api/pipelines with repo_id filter."""
        # Create a pipeline
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Filtered Pipeline"),
        )

        response = await client.get(f"/api/pipelines?repo_id={repo['id']}")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert all(p["repo_id"] == repo["id"] for p in pipelines)


class TestCreatePipeline:
    """Tests for POST /api/repos/{repo_id}/pipelines endpoint."""

    async def test_create_pipeline_minimal(self, client, repo):
        """Creates pipeline with minimal required fields."""
        payload = pipeline_create_payload(name="Minimal Pipeline")

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = assert_created_response(response, {"name": "Minimal Pipeline"})
        assert result["repo_id"] == repo["id"]
        # Create-then-author (12.8 §4.4): an EMPTY `steps` is not a
        # definition, so the row is created with none and is simply not
        # runnable until one arrives. It is not a 422 - the editor creates the
        # pipeline first and authors the graph in a follow-up PATCH.
        assert result["steps_graph"] is None
        assert result["definition_error"] is None
        assert result["is_template"] is False

    async def test_create_pipeline_without_steps_cannot_run(
        self, client, ingested_repo
    ):
        """...and the run endpoint says so, rather than passing vacuously."""
        created = await client.post(
            f"/api/repos/{ingested_repo['id']}/pipelines",
            json=pipeline_create_payload(name="Unauthored Pipeline"),
        )
        response = await client.post(
            f"/api/pipelines/{created.json()['id']}/run", json={}
        )
        assert_status_code(response, 400)
        assert "no steps" in response.json()["detail"].lower()

    async def test_create_pipeline_with_steps(self, client, repo):
        """Creates pipeline with step definitions."""
        steps = [
            pipeline_step_payload(
                name="Lint",
                step_type="script",
                config={"command": "npm run lint"},
            ),
            pipeline_step_payload(
                name="Test",
                step_type="script",
                config={"command": "npm test"},
            ),
        ]
        payload = pipeline_create_payload(name="CI Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["name"] == "CI Pipeline"

        # The array is converted at the boundary; the row holds a GRAPH, and
        # that graph is the only definition the executor will ever read.
        graph = result["steps_graph"]
        assert graph is not None, result
        assert [node["name"] for node in graph["steps"].values()] == ["Lint", "Test"]
        assert list(graph["steps"]) == ["step_0", "step_1"]
        assert graph["entry_points"] == ["step_0"]
        assert [
            (e["from_step"], e["to_step"], e["condition"]) for e in graph["edges"]
        ] == [("step_0", "step_1", "success")]

    async def test_create_pipeline_with_docker_step(self, client, repo):
        """Creates pipeline with docker step type."""
        steps = [
            pipeline_step_payload(
                name="Build",
                step_type="docker",
                config={"image": "node:20", "command": "npm run build"},
            ),
        ]
        payload = pipeline_create_payload(name="Docker Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        node = response.json()["steps_graph"]["steps"]["step_0"]
        assert node["type"] == "docker"
        assert node["config"]["image"] == "node:20"

    async def test_create_pipeline_with_agent_step(self, client, repo):
        """Creates pipeline with agent step type."""
        steps = [
            pipeline_step_payload(
                name="Implement Feature",
                step_type="agent",
                config={
                    "runner_type": "claude-code",
                    "title": "Add login",
                    "description": "Implement OAuth login",
                },
            ),
        ]
        payload = pipeline_create_payload(name="Agent Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        node = response.json()["steps_graph"]["steps"]["step_0"]
        assert node["type"] == "agent"
        assert node["config"]["runner_type"] == "claude-code"

    async def test_create_pipeline_with_branching(self, client, repo):
        """on_success/on_failure EFFECTS survive as node actions.

        v1 carried flow and effect in one string. The graph splits them:
        `merge:` / `trigger:` are effects and land in `actions`, keyed by the
        same condition vocabulary the edges use. Until 12.8 this conversion
        dropped both on the floor - a `merge:` on the final step (the common
        "merge when this passes" shape) was not even examined.
        """
        steps = [
            pipeline_step_payload(
                name="Test",
                step_type="script",
                config={"command": "npm test"},
                on_success="merge:main",
                on_failure="trigger:fix-card-123",
            ),
        ]
        payload = pipeline_create_payload(name="Branching Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        graph = response.json()["steps_graph"]
        assert graph["steps"]["step_0"]["actions"] == {
            "success": ["merge:main"],
            "failure": ["trigger:fix-card-123"],
            "always": [],
        }
        # Sole step: the effects fire, and there is nothing to continue to.
        assert graph["edges"] == []

    async def test_create_pipeline_as_template(self, client, repo):
        """Creates pipeline as a template."""
        payload = pipeline_create_payload(name="Template Pipeline", is_template=True)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["is_template"] is True

    async def test_create_pipeline_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/api/repos/nonexistent-repo/pipelines",
            json=pipeline_create_payload(),
        )
        assert_not_found(response, "Repo")

    async def test_create_pipeline_missing_name_fails(self, client, repo):
        """Fails without required name field."""
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={"description": "No name"},
        )
        assert_status_code(response, 422)


class TestGetPipeline:
    """Tests for GET /api/pipelines/{pipeline_id} endpoint."""

    async def test_get_pipeline_exists(self, client, pipeline):
        """Returns pipeline when it exists."""
        response = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": pipeline["id"], "name": pipeline["name"]})

    async def test_get_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.get("/api/pipelines/nonexistent-pipeline-id")
        assert_not_found(response, "Pipeline")

    async def test_get_pipeline_returns_all_fields(self, client, pipeline):
        """Returns pipeline with complete field set."""
        response = await client.get(f"/api/pipelines/{pipeline['id']}")
        result = response.json()

        assert "id" in result
        assert "repo_id" in result
        assert "name" in result
        assert "description" in result
        assert "steps_graph" in result
        assert "definition_error" in result
        # The v1 array left the wire at 12.8 P3. Pinned so nobody restores it
        # "for compatibility": its `= []` default made an unparseable row, a
        # graph pipeline and a pipeline with no definition at all look
        # identical on screen.
        assert "steps" not in result
        assert "is_template" in result
        assert "created_at" in result
        assert "updated_at" in result


class TestUpdatePipeline:
    """Tests for PATCH /api/pipelines/{pipeline_id} endpoint."""

    async def test_update_pipeline_name(self, client, pipeline):
        """Updates pipeline name only."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"name": "Updated Name"},
        )
        result = assert_updated_response(response, {"name": "Updated Name"})
        assert result["id"] == pipeline["id"]

    async def test_update_pipeline_description(self, client, pipeline):
        """Updates pipeline description."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"description": "Updated description"},
        )
        assert response.json()["description"] == "Updated description"

    async def test_update_pipeline_steps(self, client, pipeline):
        """A PATCH carrying the array replaces the GRAPH.

        Before 12.8 this wrote `pipelines.steps`, a column the executor never
        reads on a graph pipeline - so the edit landed in the database and
        changed nothing about what ran.
        """
        new_steps = [
            pipeline_step_payload(name="New Step", step_type="script", config={"command": "echo hello"}),
        ]
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"steps": new_steps},
        )
        graph = response.json()["steps_graph"]
        assert [node["name"] for node in graph["steps"].values()] == ["New Step"]
        assert graph["entry_points"] == ["step_0"]

        # ...and the row the next run reads agrees.
        reread = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert reread.json()["steps_graph"] == graph

    async def test_update_pipeline_is_template(self, client, pipeline):
        """Updates pipeline is_template flag."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"is_template": True},
        )
        assert response.json()["is_template"] is True

    async def test_update_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.patch(
            "/api/pipelines/nonexistent-id",
            json={"name": "New Name"},
        )
        assert_not_found(response, "Pipeline")


class TestDeletePipeline:
    """Tests for DELETE /api/pipelines/{pipeline_id} endpoint."""

    async def test_delete_pipeline_exists(self, client, pipeline):
        """Deletes pipeline when it exists."""
        response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert_deleted_response(response)

        # Verify pipeline is gone
        get_response = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert_not_found(get_response, "Pipeline")

    async def test_delete_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.delete("/api/pipelines/nonexistent-id")
        assert_not_found(response, "Pipeline")


class TestPipelineStepsValidation:
    """Tests for pipeline step validation."""

    async def test_step_with_custom_timeout(self, client, repo):
        """Steps can have custom timeout values."""
        steps = [
            pipeline_step_payload(name="Long Step", step_type="script", timeout=600),
        ]
        payload = pipeline_create_payload(name="Timeout Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        node = response.json()["steps_graph"]["steps"]["step_0"]
        assert node["timeout"] == 600

    async def test_step_defaults_are_applied(self, client, repo):
        """The v1 defaults (`next` / `stop`) survive the conversion.

        On a two-step array they are the whole point: `on_success: next`
        becomes the SUCCESS edge to the following step, and `on_failure: stop`
        becomes the absence of a FAILURE edge. Asserting them on the graph is
        what proves the default was carried rather than lost - the array field
        is gone from the wire.
        """
        steps = [
            {"name": "Basic Step", "type": "script", "config": {"command": "echo test"}},
            {"name": "Second Step", "type": "script", "config": {"command": "echo two"}},
        ]
        payload = pipeline_create_payload(name="Defaults Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        graph = response.json()["steps_graph"]
        assert [
            (e["from_step"], e["to_step"], e["condition"]) for e in graph["edges"]
        ] == [("step_0", "step_1", "success")], "on_success: next must be a SUCCESS edge"
        assert graph["steps"]["step_0"]["timeout"] == 300
        assert graph["steps"]["step_0"]["actions"] == {
            "success": [], "failure": [], "always": [],
        }, "next/stop are FLOW, and flow lives on edges - never in actions"


class TestPipelineIsolation:
    """Tests verifying pipelines are properly isolated between repos."""

    async def test_pipelines_isolated_by_repo(self, client):
        """Pipelines are only returned for their specific repo."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Repo2"))
        repo1_id = resp1.json()["id"]
        repo2_id = resp2.json()["id"]

        # Create pipelines in each repo
        await client.post(
            f"/api/repos/{repo1_id}/pipelines",
            json=pipeline_create_payload(name="Repo1 Pipeline"),
        )
        await client.post(
            f"/api/repos/{repo2_id}/pipelines",
            json=pipeline_create_payload(name="Repo2 Pipeline"),
        )

        # Verify isolation
        response = await client.get(f"/api/repos/{repo1_id}/pipelines")
        pipelines = response.json()
        assert len(pipelines) == 1
        assert pipelines[0]["name"] == "Repo1 Pipeline"


class TestDeletePipelineWithLiveRun:
    """DELETE must refuse while a run is still in flight (QA2-07).

    Pipeline.runs cascades, so deleting mid-run does not stop the run - it
    erases the row the executor and /cancel steer by, leaving the step
    container behind exited. The endpoint refuses with 409 instead.
    """

    async def _make_run(self, db_session, pipeline_id, status):
        from app.models import PipelineRun

        run = PipelineRun(pipeline_id=pipeline_id, status=status)
        db_session.add(run)
        await db_session.commit()
        return run

    async def test_delete_refused_while_a_run_is_running(
        self, client, db_session, pipeline
    ):
        """409, and BOTH the run and the pipeline survive."""
        run = await self._make_run(db_session, pipeline["id"], "running")

        response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert_status_code(response, 409)

        detail = response.json()["detail"]
        assert run.id in detail, f"refusal does not name the live run: {detail}"
        assert "cancel" in detail.lower(), f"refusal gives no way forward: {detail}"

        # The run is still reachable - so /cancel can still reach it.
        assert_status_code(await client.get(f"/api/pipeline-runs/{run.id}"), 200)
        assert_status_code(await client.get(f"/api/pipelines/{pipeline['id']}"), 200)

    async def test_delete_refused_while_a_run_is_pending(
        self, client, db_session, pipeline
    ):
        """A queued run owns the pipeline too - pending blocks the delete."""
        run = await self._make_run(db_session, pipeline["id"], "pending")

        response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert_status_code(response, 409)
        assert run.id in response.json()["detail"]

    @pytest.mark.parametrize("status", ["passed", "failed", "cancelled"])
    async def test_finished_runs_do_not_block_delete(
        self, client, db_session, pipeline, status
    ):
        """The guard is about live work only, not about history."""
        await self._make_run(db_session, pipeline["id"], status)

        response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert_deleted_response(response)


class TestExportContentDisposition:
    """The export header is built from a user-supplied name (RFC 6266)."""

    async def _pipeline_named(self, client, repo, name):
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name=name),
        )
        assert response.status_code in (200, 201), response.text
        return response.json()["id"]

    async def test_export_with_a_unicode_name(self, client, repo):
        """A non-Latin-1 name used to 500 on header encoding."""
        pipeline_id = await self._pipeline_named(
            client, repo, "Café 日本語 Pipeline"
        )

        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 200)

        disposition = response.headers["content-disposition"]
        # Headers are latin-1 on the wire; this is the encode that used to blow up.
        disposition.encode("latin-1")
        assert "filename*=UTF-8''" in disposition
        assert "%C3%A9" in disposition, disposition
        assert disposition.endswith(".yaml")

    async def test_export_with_control_characters_in_the_name(self, client, repo):
        """CR/LF/NUL in a name made h11 refuse the response entirely."""
        pipeline_id = await self._pipeline_named(
            client, repo, "Bad\r\nName\x00Here"
        )

        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 200)

        disposition = response.headers["content-disposition"]
        assert not any(c in disposition for c in "\r\n\x00"), repr(disposition)
        disposition.encode("latin-1")
        assert disposition.startswith("attachment; filename=")

    async def test_export_keeps_an_ascii_filename_fallback(self, client, repo):
        """Old clients read `filename=`; it must stay plain ASCII."""
        pipeline_id = await self._pipeline_named(client, repo, "My Nice Pipeline")

        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 200)
        assert (
            'filename="My_Nice_Pipeline.yaml"'
            in response.headers["content-disposition"]
        )


# =============================================================================
# 12.8 — the API door: one definition dialect, converted at the boundary
# =============================================================================

def _linear_graph_payload(*, entry: str = "a") -> dict:
    """A two-node linear graph, hand-authored the way the editor sends one."""
    return {
        "steps": {
            "a": {
                "id": "a", "name": "Alpha", "type": "script",
                "config": {"command": "echo a"}, "timeout": 777,
                "continue_in_context": True,
            },
            "b": {
                "id": "b", "name": "Beta", "type": "script",
                "config": {"command": "echo b"},
            },
        },
        "edges": [
            {"id": "e1", "from_step": "a", "to_step": "b", "condition": "success"}
        ],
        "entry_points": [entry],
        "version": 2,
    }


class TestDefinitionDialectBoundary:
    """`steps` is the authoring array; `steps_graph` is the definition.

    §4.4: exactly one of them reaches the column, and it is always the graph.
    Before 12.8 the router setattr'd both independently, so a PATCH carrying
    `steps` against a graph pipeline wrote a field the executor never reads -
    the user's edit landed in the database and changed nothing about the run.
    """

    async def test_both_dialects_on_create_is_refused(self, client, repo):
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={
                "name": "Two Dialects",
                "steps": [pipeline_step_payload(name="S", step_type="script")],
                "steps_graph": _linear_graph_payload(),
            },
        )
        assert_status_code(response, 422)
        assert "not both" in response.text

    async def test_both_dialects_on_update_is_refused(self, client, pipeline):
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={
                "steps": [pipeline_step_payload(name="S", step_type="script")],
                "steps_graph": _linear_graph_payload(),
            },
        )
        assert_status_code(response, 422)
        assert "not both" in response.text

    async def test_empty_steps_beside_a_graph_is_not_a_conflict(self, client, repo):
        """`{"steps": [], "steps_graph": {...}}` is what real callers send.

        `steps` was a NOT NULL column, so the editor and
        `tdd/qa/qa3_support.graph_pipeline` both name it even when the graph
        is the definition. An empty array is not a second definition.
        """
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={
                "name": "Graph With Empty Array",
                "steps": [],
                "steps_graph": _linear_graph_payload(),
            },
        )
        assert_status_code(response, 201)
        assert set(response.json()["steps_graph"]["steps"]) == {"a", "b"}

    async def test_authored_step_ids_survive_the_conversion(self, client, repo):
        """An `id:` the author wrote is the graph's node key (§1.6b).

        Node ids are the context-directory names and the debug breakpoint
        keys, so renaming them to `step_0..step_N` changes behaviour far from
        the cause. Nothing tested this before 12.8.
        """
        steps = [
            {"id": "tier1", "name": "T1", "type": "script", "config": {"command": "a"}},
            {"id": "tier2", "name": "T2", "type": "script", "config": {"command": "b"}},
        ]
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Authored Ids", steps=steps),
        )
        assert_status_code(response, 201)
        graph = response.json()["steps_graph"]
        assert list(graph["steps"]) == ["tier1", "tier2"]
        assert graph["entry_points"] == ["tier1"]
        assert graph["edges"][0]["from_step"] == "tier1"
        assert graph["edges"][0]["to_step"] == "tier2"

    async def test_mid_array_stop_that_orphans_the_tail_is_refused(self, client, repo):
        """A step that continues on neither outcome makes the rest dead.

        v1 let this through: it simply stopped, and the steps after it never
        ran. As a graph they are unreachable, `_verify_graph_coverage` FAILS
        the run, and a pipeline that was green becomes red for the wrong
        reason. Refuse at the door and name the step responsible.
        """
        steps = [
            pipeline_step_payload(name="First", on_success="stop", on_failure="stop"),
            pipeline_step_payload(name="Orphan"),
        ]
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Orphaned Tail", steps=steps),
        )
        assert_status_code(response, 422)
        detail = response.text
        assert "step_0" in detail and "unreachable" in detail, detail

    async def test_stop_on_the_final_step_is_fine(self, client, repo):
        """The same word on the LAST step orphans nothing."""
        steps = [
            pipeline_step_payload(name="Only", on_success="stop", on_failure="stop"),
        ]
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Terminal Stop", steps=steps),
        )
        assert_status_code(response, 201)
        assert response.json()["steps_graph"]["edges"] == []

    async def test_retired_trigger_pipeline_action_is_refused_by_name(
        self, client, repo
    ):
        """`trigger:pipeline:` names its replacement rather than vanishing."""
        steps = [
            pipeline_step_payload(name="Chain", on_success="trigger:pipeline:abc123"),
        ]
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Retired Action", steps=steps),
        )
        assert_status_code(response, 422)
        detail = response.text
        assert "retired" in detail, detail
        assert "card_complete" in detail, detail

    async def test_unknown_action_is_refused_naming_the_step(self, client, repo):
        steps = [pipeline_step_payload(name="Typo", on_success="nextt")]
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Typo Pipeline", steps=steps),
        )
        assert_status_code(response, 422)
        assert "'nextt'" in response.text, response.text


class TestRunRefusesADefinitionError:
    """A row carrying `definition_error` must not run (§1.7, the Y5 channel).

    The row KEEPS the graph it had before a refused sync, so without this
    guard a broken CI file re-runs yesterday's definition under today's name
    and reports green.
    """

    async def test_run_is_refused_and_names_the_reason(
        self, client, db_session, pipeline
    ):
        from app.models import Pipeline

        row = await db_session.get(Pipeline, pipeline["id"])
        row.definition_error = "step 'step_0' declares on_success='banana'"
        await db_session.commit()

        response = await client.post(
            f"/api/pipelines/{pipeline['id']}/run", json={}
        )
        assert_status_code(response, 400)
        assert "banana" in response.json()["detail"]

        runs = await client.get(f"/api/pipelines/{pipeline['id']}/runs")
        assert runs.json() == [], "a refused pipeline must not have started"

    async def test_the_reason_is_visible_on_the_row(
        self, client, db_session, pipeline
    ):
        """The badge channel: a refusal a user can see without reading logs."""
        from app.models import Pipeline

        row = await db_session.get(Pipeline, pipeline["id"])
        row.definition_error = "two steps declare the id 'tier1'"
        await db_session.commit()

        response = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert response.json()["definition_error"] == "two steps declare the id 'tier1'"


class TestExportEmitsTheAuthoringDialect:
    """Export writes the array `.lazyaf/pipelines/*.yaml` shape (§4.10).

    It used to emit a THIRD dialect - `steps` as a mapping keyed by step id,
    with edge TARGETS written into `on_success` - which `PipelineYaml` cannot
    validate, so LazyAF could not import its own export.
    """

    async def _graph_pipeline(self, client, repo, name, graph):
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={"name": name, "steps_graph": graph},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def test_linear_graph_round_trips_through_the_importer(
        self, client, repo
    ):
        """Export -> PipelineYaml -> array_to_graph reproduces the graph."""
        import yaml as yaml_module

        from app.schemas.lazyaf_yaml import PipelineYaml, pipeline_yaml_to_graph

        pipeline_id = await self._graph_pipeline(
            client, repo, "Round Trip", _linear_graph_payload()
        )
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 200)

        document = yaml_module.safe_load(response.text)
        assert isinstance(document["steps"], list), document

        reimported = pipeline_yaml_to_graph(PipelineYaml(**document))
        assert list(reimported.steps) == ["a", "b"]
        assert reimported.entry_points == ["a"]
        assert [(e.from_step, e.to_step, e.condition.value) for e in reimported.edges] == [
            ("a", "b", "success")
        ]

    async def test_export_preserves_id_timeout_and_continuation(
        self, client, repo
    ):
        """Pinned field set (§4.10): dropping any of these is silent damage.

        Without `id` every node is renamed on re-import; without `timeout`
        every step silently resets to 300s; without `continue_in_context` the
        workspace continuation is lost.
        """
        import yaml as yaml_module

        pipeline_id = await self._graph_pipeline(
            client, repo, "Preserve Fields", _linear_graph_payload()
        )
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        first = yaml_module.safe_load(response.text)["steps"][0]

        assert first["id"] == "a"
        assert first["timeout"] == 777
        assert first["continue_in_context"] is True
        assert first["on_success"] == "next"
        assert first["on_failure"] == "stop"

    async def test_export_writes_actions_as_the_v1_word(self, client, repo):
        """`actions.success == ['merge:main']` comes back as `on_success`."""
        import yaml as yaml_module

        graph = {
            "steps": {
                "only": {
                    "id": "only", "name": "Only", "type": "script",
                    "config": {"command": "echo x"},
                    "actions": {"success": ["merge:main"], "failure": [], "always": []},
                }
            },
            "edges": [],
            "entry_points": ["only"],
            "version": 2,
        }
        pipeline_id = await self._graph_pipeline(client, repo, "Merge Export", graph)
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 200)
        assert yaml_module.safe_load(response.text)["steps"][0]["on_success"] == "merge:main"

    async def test_fan_out_export_is_refused_naming_the_construct(
        self, client, repo
    ):
        graph = {
            "steps": {
                "start": {"id": "start", "name": "Start", "type": "script", "config": {}},
                "a": {"id": "a", "name": "A", "type": "script", "config": {}},
                "b": {"id": "b", "name": "B", "type": "script", "config": {}},
            },
            "edges": [
                {"id": "e1", "from_step": "start", "to_step": "a", "condition": "success"},
                {"id": "e2", "from_step": "start", "to_step": "b", "condition": "success"},
            ],
            "entry_points": ["start"],
            "version": 2,
        }
        pipeline_id = await self._graph_pipeline(client, repo, "Fan Out", graph)
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 409)
        detail = response.json()["detail"]
        assert "fan-out" in detail, detail
        assert "'start'" in detail, detail

    async def test_always_edge_export_is_refused_naming_the_construct(
        self, client, repo
    ):
        graph = _linear_graph_payload()
        graph["edges"][0]["condition"] = "always"
        pipeline_id = await self._graph_pipeline(client, repo, "Always Edge", graph)
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 409)
        assert "'always' edge" in response.json()["detail"]

    async def test_multiple_entry_points_export_is_refused(self, client, repo):
        graph = _linear_graph_payload()
        graph["entry_points"] = ["a", "b"]
        pipeline_id = await self._graph_pipeline(client, repo, "Two Entries", graph)
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 409)
        assert "entry points" in response.json()["detail"]

    async def test_fan_in_export_is_refused_naming_the_construct(self, client, repo):
        graph = {
            "steps": {
                "a": {"id": "a", "name": "A", "type": "script", "config": {}},
                "b": {"id": "b", "name": "B", "type": "script", "config": {}},
                "join": {"id": "join", "name": "Join", "type": "script", "config": {}},
            },
            "edges": [
                {"id": "e1", "from_step": "a", "to_step": "join", "condition": "success"},
                {"id": "e2", "from_step": "b", "to_step": "join", "condition": "success"},
            ],
            "entry_points": ["a"],
            "version": 2,
        }
        pipeline_id = await self._graph_pipeline(client, repo, "Fan In", graph)
        response = await client.get(f"/api/pipelines/{pipeline_id}/export/yaml")
        assert_status_code(response, 409)
        assert "fan-in" in response.json()["detail"]
