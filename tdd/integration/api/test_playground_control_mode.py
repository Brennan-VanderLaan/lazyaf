"""
Phase 12.5: the playground runs on the control layer.

Before 12.5 a playground test was an ephemeral ``QueuedJob`` with
``is_playground=True``: a polling runner picked it up, streamed logs back
through ``/api/playground/{id}/internal/log``, computed the diff inside its
own workspace and POSTed that back too. Three bespoke channels, none shared
with pipeline execution.

After 12.5 a playground test is an AD-HOC AGENT RUN - a hidden one-step
Pipeline plus a real PipelineRun - so it uses the SAME channels every other
step uses:

  * logs arrive at ``POST /api/steps/{id}/logs`` and reach the SSE stream
    through the WS manager's per-run observer registry (contract #8);
  * the diff is computed SERVER-SIDE from the internal git server, because
    the workspace volume is deleted the moment the run completes and reading
    it afterwards is a race the platform loses at random.

These tests drive the REAL routers, the REAL WS manager and the REAL git
server. The only double is the container itself: a stub LocalExecutor that
declares the control-layer capability and yields the executor's event shape,
so ``_prepare_control_mode`` really does mint a StepExecution row and a step
JWT, and the log POST below is really authenticated against it (R6).
"""
import asyncio
import json
import time

import pytest
from sqlalchemy import select

from app.models import Pipeline, PipelineRun, StepRun
from app.services import agent_run
from app.services.playground_service import playground_service

pytestmark = pytest.mark.asyncio


# -----------------------------------------------------------------------------
# Test doubles / helpers
# -----------------------------------------------------------------------------


class ControlModeStubExecutor:
    """Docker-free LocalExecutor stand-in that DOES support control mode.

    The T1 stub in tdd/conftest.py deliberately reports no control-layer
    label (stock-image semantics). Control mode is the whole subject here, so
    this one declares it - and then parks inside ``execute_step`` until the
    test releases it, which is where the test gets to act as the container:
    POSTing logs to /api/steps/{id}/logs with the step JWT the backend just
    minted.
    """

    def __init__(self):
        self.calls: list[tuple[dict, dict]] = []
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.exit_code = 0
        # Real containers do not always die the instant they are killed.
        # Setting this False keeps the step task parked after a cancel, so
        # assertions about what CANCEL wrote are not racing a straggler step
        # task that is busy overwriting the run it just cancelled.
        self.release_on_cancel = True

    async def image_supports_control_layer(self, image: str) -> bool:
        return True

    async def find_missing_images(self, images) -> list[str]:
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "preparing"}
        yield {"type": "status", "status": "running"}
        self.dispatched.set()
        await self.release.wait()
        yield {
            "type": "result",
            "status": "completed" if self.exit_code == 0 else "failed",
            "exit_code": self.exit_code,
            "error": None,
            "log_tail": [],
        }

    async def cancel_step(self, execution_key):
        if self.release_on_cancel:
            self.release.set()
        return True

    async def cancel_all(self):
        self.release.set()

    def reset(self):
        self.calls.clear()

    # --- what the test needs out of the dispatch -------------------------
    @property
    def context(self) -> dict:
        assert self.calls, "the step was never dispatched to the executor"
        return self.calls[-1][1]


class MissingImageStubExecutor(ControlModeStubExecutor):
    """Every image this executor is asked about is missing.

    That is the cheapest reproduction of the one thing that makes
    ``start_pipeline`` complete a run SYNCHRONOUSLY: image preflight fails,
    ``_complete_pipeline`` runs inline, and ``on_run_complete`` lands the
    session terminal BEFORE ``start_adhoc_agent_run`` has even returned.
    """

    async def find_missing_images(self, images) -> list[str]:
        return sorted(images)

    async def execute_step(self, step_config, execution_context):
        raise AssertionError(
            "preflight failed - no step should ever have been dispatched"
        )
        yield  # pragma: no cover - keeps this an async generator


def _install_executor(stub):
    from app.services.pipeline_executor import pipeline_executor

    previous = pipeline_executor._local_executor
    pipeline_executor._local_executor = stub
    try:
        yield stub
    finally:
        stub.release.set()
        pipeline_executor._local_executor = previous


@pytest.fixture
def control_executor():
    """Install the control-mode stub on the global pipeline executor."""
    yield from _install_executor(ControlModeStubExecutor())


@pytest.fixture
def missing_image_executor():
    """Install an executor whose images are all missing (preflight fails)."""
    yield from _install_executor(MissingImageStubExecutor())


async def wait_for(predicate, timeout=5.0, message="condition never became true"):
    """Poll a predicate on the running loop. Loud on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(message)


async def start_playground(client, repo, **overrides):
    payload = {
        "runner_type": "mock",
        "branch": repo["default_branch"],
    }
    payload.update(overrides)
    response = await client.post(
        f"/api/repos/{repo['id']}/playground/test", json=payload
    )
    assert response.status_code == 200, response.text
    return response.json()["session_id"]


def seed_branch(repo_manager, repo_id, base_branch, branch, path, content):
    """Commit one file onto `branch` off `base_branch` in the internal repo.

    This is what the agent container does at the end of a real run (commit +
    push to the internal git server). Doing it here lets the diff and
    branch-disposal assertions run against the REAL git server rather than a
    mocked one. `path` must be a TOP-LEVEL name: the tree is built by hand
    here, so a slash would produce one flat entry instead of a subtree.
    """
    from dulwich.objects import Blob, Commit, Tree

    repo = repo_manager.get_repo(repo_id)
    base_sha = repo_manager.get_branch_commit(repo_id, base_branch)
    base_commit = repo.object_store[base_sha.encode("ascii")]
    tree = repo.object_store[base_commit.tree]

    blob = Blob()
    blob.data = content.encode()
    repo.object_store.add_object(blob)

    new_tree = Tree()
    for entry in tree.items():
        new_tree.add(entry.path, entry.mode, entry.sha)
    new_tree.add(path.encode(), 0o100644, blob.id)
    repo.object_store.add_object(new_tree)

    commit = Commit()
    commit.tree = new_tree.id
    commit.parents = [base_sha.encode("ascii")]
    commit.author = commit.committer = b"LazyAF Agent <agent@lazyaf.local>"
    commit.author_time = commit.commit_time = int(time.time())
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"agent: add a file"
    repo.object_store.add_object(commit)

    repo.refs[f"refs/heads/{branch}".encode()] = commit.id
    return commit.id.decode("ascii")


# -----------------------------------------------------------------------------
# 1. The shape of an ad-hoc playground run
# -----------------------------------------------------------------------------


class TestPlaygroundStartsAnAdhocRun:
    async def test_run_is_created_with_playground_trigger(
        self, client, ingested_repo, db_session, control_executor
    ):
        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()

        result = await db_session.execute(
            select(PipelineRun).where(PipelineRun.trigger_ref == session_id)
        )
        run = result.scalar_one()
        assert run.trigger_type == agent_run.TRIGGER_PLAYGROUND

        session = playground_service.get_session(session_id)
        assert session.pipeline_run_id == run.id
        assert session.status == "running"
        assert session.work_branch == f"playground/{session_id[:8]}"

    async def test_exactly_one_agent_step_run(
        self, client, ingested_repo, db_session, control_executor
    ):
        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()

        run_id = playground_service.get_session(session_id).pipeline_run_id
        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run_id)
        )
        step_runs = list(result.scalars().all())
        assert len(step_runs) == 1
        assert step_runs[0].executor == "local"

        # The step really is an agent step running the mock agent.
        step_config = control_executor.calls[0][0]
        assert step_config["type"] == "agent"

    async def test_adhoc_pipeline_is_hidden_from_the_list(
        self, client, ingested_repo, db_session, control_executor
    ):
        """The RUN stays visible; the plumbing Pipeline row does not.

        One hidden pipeline row is created per playground start and per card
        start. Listed, they would bury a repo's real pipelines within a day.
        """
        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()

        run_id = playground_service.get_session(session_id).pipeline_run_id
        run = await db_session.get(PipelineRun, run_id)
        pipeline = await db_session.get(Pipeline, run.pipeline_id)
        assert agent_run.is_adhoc_pipeline_name(pipeline.name)

        listed = await client.get("/api/pipelines")
        assert listed.status_code == 200
        assert pipeline.id not in [p["id"] for p in listed.json()]

        scoped = await client.get(f"/api/repos/{ingested_repo['id']}/pipelines")
        assert pipeline.id not in [p["id"] for p in scoped.json()]

        # ... but the RUN is listed, because that is the point of using one.
        runs = await client.get("/api/pipeline-runs")
        assert run_id in [r["id"] for r in runs.json()]


# -----------------------------------------------------------------------------
# 2. Logs: POST /api/steps/{id}/logs -> WS manager observer -> SSE
# -----------------------------------------------------------------------------


class TestPlaygroundLogsArriveThroughTheControlChannel:
    async def test_sse_receives_a_line_posted_to_the_steps_router(
        self, client, ingested_repo, db_session, control_executor
    ):
        """The whole log path, end to end, with no fake in the middle.

        A real POST to the real /api/steps/{id}/logs route, authenticated
        with the real step JWT the backend minted at dispatch, fanned out by
        the real ConnectionManager, delivered to the real playground observer
        and read off the real SSE generator.
        """
        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()

        # Subscribe to the SSE stream the way the router does.
        collected: list[dict] = []

        async def collect():
            async for event in playground_service.stream_logs(session_id):
                collected.append(event)
                if event["type"] == "log":
                    return

        task = asyncio.create_task(collect())
        session = playground_service.get_session(session_id)
        await wait_for(
            lambda: bool(session.log_subscribers),
            message="the SSE generator never subscribed",
        )

        context = control_executor.context
        response = await client.post(
            f"/api/steps/{context['step_execution_id']}/logs",
            headers={"Authorization": f"Bearer {context['step_auth_token']}"},
            json={"lines": [{"content": "[agent] Analyzing the workspace...\n"}]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["lines_appended"] == 1

        await asyncio.wait_for(task, timeout=5)

        assert collected[-1]["type"] == "log"
        assert "Analyzing the workspace" in collected[-1]["data"]
        assert any(
            "Analyzing the workspace" in line for line in session.logs
        ), "the observer never appended the line to the session"

    async def test_observer_is_detached_on_completion(
        self, client, ingested_repo, db_session, control_executor
    ):
        """A finished session must not keep a callback on the WS manager.

        The manager is a process-wide singleton; a leaked observer per
        playground run is an unbounded leak that also keeps delivering log
        lines into a dead session's list.
        """
        from app.services.websocket import manager

        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()
        run_id = playground_service.get_session(session_id).pipeline_run_id
        assert run_id in manager._run_log_observers

        playground_service.detach_run(session_id)
        assert run_id not in manager._run_log_observers

    async def test_cancel_detaches_and_stops_the_run(
        self, client, ingested_repo, db_session, control_executor
    ):
        from app.services.websocket import manager

        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()
        run_id = playground_service.get_session(session_id).pipeline_run_id

        response = await client.post(f"/api/playground/{session_id}/cancel")
        assert response.status_code == 200, response.text

        assert playground_service.get_session(session_id).status == "cancelled"
        assert run_id not in manager._run_log_observers


# -----------------------------------------------------------------------------
# 2b. Cancellation: the RUN, not just the session
# -----------------------------------------------------------------------------


class TestPlaygroundCancelStopsTheRun:
    """The session status is bookkeeping; the RUN is what costs money.

    The original cancel test only asserted ``session.status == "cancelled"``,
    which is why a cancel that reached the executor through a lazily-loaded
    ``PipelineRun.step_runs`` - and therefore raised MissingGreenlet before
    killing anything, into a bare ``except`` - shipped looking green. Every
    assertion here is about the run and the container.
    """

    async def test_cancel_reaches_the_executor_and_cancels_the_run(
        self, client, ingested_repo, db_session, control_executor
    ):
        # Keep the "container" alive past the kill so these assertions are
        # about what CANCEL wrote, not about whether the straggler step task
        # got there first.
        control_executor.release_on_cancel = False

        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()
        run_id = playground_service.get_session(session_id).pipeline_run_id

        cancelled_keys: list[str] = []
        original_cancel_step = control_executor.cancel_step

        async def spy(execution_key):
            cancelled_keys.append(execution_key)
            return await original_cancel_step(execution_key)

        control_executor.cancel_step = spy

        response = await client.post(f"/api/playground/{session_id}/cancel")
        assert response.status_code == 200, response.text

        assert cancelled_keys, (
            "cancel never reached the executor - the container is still "
            "running and still spending money"
        )

        run = await db_session.get(PipelineRun, run_id)
        await db_session.refresh(run)
        assert run.status == "cancelled", (
            f"the RUN must reach cancelled, not just the session (got "
            f"{run.status!r})"
        )

        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run_id)
        )
        assert [sr.status for sr in result.scalars().all()] == ["cancelled"]

    async def test_a_cancel_that_cannot_cancel_surfaces(
        self, client, ingested_repo, db_session, control_executor, monkeypatch
    ):
        """A swallowed cancel answers 200 while the agent keeps running."""
        from app.services.pipeline_executor import pipeline_executor

        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()

        async def boom(db, run):
            raise RuntimeError("docker daemon went away")

        monkeypatch.setattr(pipeline_executor, "cancel_run", boom)

        response = await client.post(f"/api/playground/{session_id}/cancel")
        assert response.status_code == 503, response.text
        assert "docker daemon went away" in response.json()["detail"]

        assert playground_service.get_session(session_id).status == "running", (
            "a failed cancel must leave the session live so it can be retried "
            "- reporting 'cancelled' hides a container that is still running"
        )


# -----------------------------------------------------------------------------
# 3. Completion: server-side diff and branch disposal
# -----------------------------------------------------------------------------


class TestPlaygroundCompletion:
    async def _completed_session(
        self, client, ingested_repo, db_session, control_executor, clean_git_repos, **kw
    ):
        session_id = await start_playground(client, ingested_repo, **kw)
        await control_executor.dispatched.wait()
        session = playground_service.get_session(session_id)

        seed_branch(
            clean_git_repos,
            ingested_repo["id"],
            ingested_repo["default_branch"],
            session.work_branch,
            "agent_output.py",
            "# written by the agent\n",
        )

        run = await db_session.get(PipelineRun, session.pipeline_run_id)
        await agent_run.on_run_complete(db_session, run, success=True)
        return session

    async def test_diff_is_computed_from_the_git_server(
        self, client, ingested_repo, db_session, control_executor, clean_git_repos
    ):
        session = await self._completed_session(
            client, ingested_repo, db_session, control_executor, clean_git_repos
        )

        assert session.status == "completed"
        assert session.files_changed == ["agent_output.py"]
        assert "written by the agent" in session.diff

    async def test_throwaway_branch_is_deleted(
        self, client, ingested_repo, db_session, control_executor, clean_git_repos
    ):
        session = await self._completed_session(
            client, ingested_repo, db_session, control_executor, clean_git_repos
        )

        assert session.branch_saved is None
        branches = clean_git_repos.list_branches(ingested_repo["id"])
        assert session.work_branch not in branches, (
            "a playground run must not leave refs behind in the user's repo"
        )

    async def test_save_to_branch_keeps_the_work_under_the_requested_name(
        self, client, ingested_repo, db_session, control_executor, clean_git_repos
    ):
        session = await self._completed_session(
            client,
            ingested_repo,
            db_session,
            control_executor,
            clean_git_repos,
            save_to_branch="keep-this-one",
        )

        assert session.branch_saved == "keep-this-one"
        branches = clean_git_repos.list_branches(ingested_repo["id"])
        assert "keep-this-one" in branches
        assert session.work_branch not in branches

    async def test_failure_reports_the_step_error(
        self, client, ingested_repo, db_session, control_executor
    ):
        session_id = await start_playground(client, ingested_repo)
        await control_executor.dispatched.wait()
        session = playground_service.get_session(session_id)

        run = await db_session.get(PipelineRun, session.pipeline_run_id)
        result = await db_session.execute(
            select(StepRun).where(StepRun.pipeline_run_id == run.id)
        )
        step_run = result.scalars().first()
        step_run.error = "agent exited 1"
        await db_session.commit()

        await agent_run.on_run_complete(db_session, run, success=False)

        assert session.status == "failed"
        assert session.error == "agent exited 1"

    async def test_completion_is_idempotent(
        self, client, ingested_repo, db_session, control_executor, clean_git_repos
    ):
        """A duplicated hook call must not re-run branch disposal.

        The call site lives at the tail of _complete_pipeline; deleting the
        saved branch a second time (or failing on its absence) would be a
        nasty way to discover a double call.
        """
        session = await self._completed_session(
            client, ingested_repo, db_session, control_executor, clean_git_repos
        )
        run = await db_session.get(PipelineRun, session.pipeline_run_id)

        await agent_run.on_run_complete(db_session, run, success=True)

        assert session.status == "completed"
        assert session.files_changed == ["agent_output.py"]

    async def test_other_trigger_types_are_untouched(self, db_session):
        """on_run_complete is called for EVERY run; it must no-op on most."""
        run = PipelineRun(
            id="not-adhoc", pipeline_id="p", trigger_type="push", trigger_ref="main"
        )
        await agent_run.on_run_complete(db_session, run, success=True)  # no raise


# -----------------------------------------------------------------------------
# 4. A run that completes SYNCHRONOUSLY inside start_pipeline
# -----------------------------------------------------------------------------


class TestPlaygroundSynchronousStartFailure:
    """``start_pipeline`` does not always return a running run.

    Image preflight fails INLINE: ``_complete_pipeline`` -> ``on_run_complete``
    -> the session is already ``failed`` by the time
    ``start_adhoc_agent_run`` returns. A caller that then writes "running"
    resurrects it, and everything downstream waits on a terminal status that
    will never come again - ``stream_logs`` only leaves its loop on one, and
    the observer it registered is held by the process-wide WS manager until
    the 30-minute TTL sweeps the session.
    """

    async def test_session_ends_failed_not_running(
        self, client, ingested_repo, db_session, missing_image_executor
    ):
        session_id = await start_playground(client, ingested_repo)

        session = playground_service.get_session(session_id)
        assert session.status == "failed", (
            f"a run that failed preflight must leave the session failed, not "
            f"{session.status!r} - the 'running' write after start_pipeline "
            f"clobbered a terminal status"
        )
        assert session.error

        run = (
            await db_session.execute(
                select(PipelineRun).where(PipelineRun.trigger_ref == session_id)
            )
        ).scalar_one()
        assert run.status == "failed"

    async def test_the_start_response_reports_the_failure(
        self, client, ingested_repo, missing_image_executor
    ):
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/playground/test",
            json={"runner_type": "mock", "branch": ingested_repo["default_branch"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"

    async def test_stream_terminates_instead_of_pinging_forever(
        self, client, ingested_repo, missing_image_executor
    ):
        session_id = await start_playground(client, ingested_repo)

        events = []
        async def drain():
            async for event in playground_service.stream_logs(session_id):
                events.append(event)

        # No wait_for backstop that could pass by timing out: the generator
        # has to END on its own.
        await asyncio.wait_for(drain(), timeout=5)
        assert events[-1] == {
            "type": "complete",
            "data": "failed",
            "timestamp": events[-1]["timestamp"],
        }

    async def test_get_result_answers_failed(
        self, client, ingested_repo, missing_image_executor
    ):
        session_id = await start_playground(client, ingested_repo)

        response = await client.get(f"/api/playground/{session_id}/result")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]

    async def test_no_log_observer_is_left_on_the_ws_manager(
        self, client, ingested_repo, db_session, missing_image_executor
    ):
        from app.services.websocket import manager

        session_id = await start_playground(client, ingested_repo)
        run = (
            await db_session.execute(
                select(PipelineRun).where(PipelineRun.trigger_ref == session_id)
            )
        ).scalar_one()

        assert run.id not in manager._run_log_observers, (
            "an observer registered for a run that already finished is never "
            "detached - it leaks for the life of the process"
        )
