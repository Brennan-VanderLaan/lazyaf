"""
US-2 end to end: card -> agent -> gate -> review -> merge.

This is the 12.5 exit gate as a test. Before this phase the middle of that
chain was a polling runner: card start enqueued a job, a container that had
been sitting idle since boot picked it up, and the platform learned what
happened by being POSTed at. After 12.5 the agent runs in an EPHEMERAL
control-mode container, dispatched by the LocalExecutor as an ad-hoc
single-agent-step PipelineRun, and every link in the chain is a row the API
can be asked about.

The chain asserted here, link by link:

  1. POST /api/cards/{id}/start   -> an ad-hoc PipelineRun (trigger_type
                                     card_work), executor='local', NOTHING
                                     enqueued for a runner
  2. the agent commits and pushes -> the card's lazyaf/* branch exists on the
                                     internal git server
  3. the run completes            -> card.status == 'in_review' (written by
                                     agent_run.on_run_complete, routed off
                                     the persisted trigger_type)
  4. THE GATE                     -> the card_complete trigger fires and the
                                     verification pipeline runs
  5. POST /api/cards/{id}/approve -> the branch merges into the default
                                     branch and the card is 'done'

Not marked slow ON PURPOSE: it runs in T3 on every push (scripts/run_tier.py)
against the mock agent, at zero API cost. The one double is the container:
`AgentContainerStub` stands in for the agent's ephemeral container, doing
exactly what that container does at the end of a real run - commit the work
onto the card branch of the internal git server. Everything on either side of
it (routing, StepRun/StepExecution rows, card status, trigger matching, the
gating run, the merge) is the real code path.
"""
import asyncio
import time

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

AGENT_FILE = "rate_limit.py"
AGENT_CONTENT = "# implemented by the agent\nLIMIT = 100\n"


# -----------------------------------------------------------------------------
# The container double
# -----------------------------------------------------------------------------


class AgentContainerStub:
    """Stands in for the ephemeral agent container.

    Declares the control-layer capability (so the real ``_prepare_control_mode``
    mints a StepExecution row and a step JWT, exactly as it would for the real
    image) and yields the LocalExecutor's event shape. AGENT steps park until
    the test releases them, which is how the test gets to read the card's
    branch name and then act as the container: committing the work onto that
    branch of the internal git server, the way the wrapper's commit/push does.

    Script steps (the gating pipeline) run straight through - parking those
    would deadlock the gate this test exists to prove.
    """

    def __init__(self, report_status):
        self.calls: list[tuple[dict, dict]] = []
        self.agent_dispatched = asyncio.Event()
        self.release_agent = asyncio.Event()
        self.on_agent_release = None  # async callable(exec_context)
        # The control-runtime report. NOT optional theatre: a control-mode
        # step whose StepExecution never leaves PREPARING is FAILED on
        # purpose (_reconcile_control_execution) - "an image without a
        # working /control runtime must never read green". A double that
        # skipped the report would be testing that rule, not this loop.
        self._report_status = report_status

    async def image_supports_control_layer(self, image: str) -> bool:
        return True

    async def find_missing_images(self, images) -> list[str]:
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "preparing"}
        await self._report_status(execution_context, "running")
        yield {"type": "status", "status": "running"}
        if step_config.get("type") == "agent":
            self.agent_dispatched.set()
            await self.release_agent.wait()
            if self.on_agent_release is not None:
                await self.on_agent_release(execution_context)
        await self._report_status(execution_context, "completed", exit_code=0)
        yield {
            "type": "result",
            "status": "completed",
            "exit_code": 0,
            "error": None,
            "log_tail": [],
        }

    async def cancel_step(self, execution_key):
        self.release_agent.set()
        return True

    async def cancel_all(self):
        self.release_agent.set()

    def reset(self):
        self.calls.clear()

    @property
    def agent_steps(self) -> list[dict]:
        return [cfg for cfg, _ in self.calls if cfg.get("type") == "agent"]


@pytest.fixture
def agent_container(api_client):
    """Install the container double on the global pipeline executor.

    Its status reports go through the REAL POST /api/steps/{id}/status route
    with the REAL step JWT the backend minted at dispatch - the same channel
    images/base/control/run.py uses.
    """
    from app.services.pipeline_executor import pipeline_executor

    async def report_status(exec_context, status, exit_code=None):
        step_id = exec_context.get("step_execution_id")
        token = exec_context.get("step_auth_token")
        if not step_id or not token:
            return  # stdout-mode step: no control channel to report on
        payload = {"status": status}
        if exit_code is not None:
            payload["exit_code"] = exit_code
        response = await api_client.post(
            f"/api/steps/{step_id}/status",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        assert response.status_code == 200, response.text

    stub = AgentContainerStub(report_status)
    previous = pipeline_executor._local_executor
    pipeline_executor._local_executor = stub
    try:
        yield stub
    finally:
        stub.release_agent.set()
        pipeline_executor._local_executor = previous


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def wait_until(predicate, timeout=15.0, message="condition never held"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"{message} (last value: {last!r})")


def commit_onto_branch(repo_manager, repo_id, base_branch, branch, path, content):
    """What the agent's commit/push does, against the internal git server.

    `path` is a TOP-LEVEL name on purpose: this builds the tree object by
    hand, and a name containing a slash would become one flat entry rather
    than a nested subtree - readable by tree_changes, invisible to every
    path-walking reader (get_file_content, the frontend file browser).
    """
    from dulwich.objects import Blob, Commit, Tree

    repo = repo_manager.get_repo(repo_id)
    base_sha = repo_manager.get_branch_commit(repo_id, base_branch)
    assert base_sha, f"base branch {base_branch!r} has no commit"
    base_commit = repo.object_store[base_sha.encode("ascii")]
    base_tree = repo.object_store[base_commit.tree]

    blob = Blob()
    blob.data = content.encode()
    repo.object_store.add_object(blob)

    tree = Tree()
    for entry in base_tree.items():
        tree.add(entry.path, entry.mode, entry.sha)
    tree.add(path.encode(), 0o100644, blob.id)
    repo.object_store.add_object(tree)

    commit = Commit()
    commit.tree = tree.id
    commit.parents = [base_sha.encode("ascii")]
    commit.author = commit.committer = b"LazyAF Agent <agent@lazyaf.local>"
    commit.author_time = commit.commit_time = int(time.time())
    commit.author_timezone = commit.commit_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = b"feat: add rate limiting\n\nImplemented by LazyAF agent"
    repo.object_store.add_object(commit)

    repo.refs[f"refs/heads/{branch}".encode()] = commit.id


async def create_gate_pipeline(api_client, repo_id):
    """The verification pipeline a card entering review must pass."""
    response = await api_client.post(
        f"/api/repos/{repo_id}/pipelines",
        json={
            "name": "US-2 review gate",
            "description": "Runs when a card reaches in_review",
            "steps": [
                {
                    "name": "Verify the agent's work",
                    "type": "script",
                    "config": {"command": "python3 -m pytest -q"},
                    "on_success": "next",
                    "on_failure": "stop",
                }
            ],
            "triggers": [
                {
                    "type": "card_complete",
                    "enabled": True,
                    "config": {"status": "in_review"},
                    "on_pass": "nothing",
                    "on_fail": "nothing",
                }
            ],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_agent_card(api_client, repo_id):
    response = await api_client.post(
        f"/api/repos/{repo_id}/cards",
        json={
            "title": "Add rate limiting to /api/repos",
            "description": "Cap requests per client to 100/min.",
            "runner_type": "mock",
            "step_type": "agent",
            "step_config": {
                "mock_config": {
                    "response_mode": "streaming",
                    "delay_ms": 1,
                    "file_operations": [
                        {"action": "create", "path": AGENT_FILE, "content": AGENT_CONTENT}
                    ],
                    "output_events": [{"type": "complete", "text": "Done."}],
                    "exit_code": 0,
                }
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# -----------------------------------------------------------------------------
# The loop
# -----------------------------------------------------------------------------


class TestUS2CardLoop:
    """card -> agent -> gate -> review -> merge, on ephemeral containers."""

    async def test_full_loop(
        self, api_client, test_repo, clean_git_repos, agent_container
    ):
        repo_id = test_repo["id"]
        default_branch = test_repo["default_branch"]
        gate = await create_gate_pipeline(api_client, repo_id)
        card = await create_agent_card(api_client, repo_id)

        # --- 1. start -----------------------------------------------------
        response = await api_client.post(f"/api/cards/{card['id']}/start")
        assert response.status_code == 200, response.text
        started = response.json()
        assert started["status"] == "in_progress"
        assert started["branch_name"].startswith("lazyaf/")

        await asyncio.wait_for(agent_container.agent_dispatched.wait(), timeout=15)

        # The step really was dispatched as an agent step to the local
        # executor, in control mode, with the mock agent selected.
        assert agent_container.agent_steps, "no agent step reached the executor"
        _, exec_context = agent_container.calls[-1]
        assert exec_context.get("control_mode") is True, (
            "an agent step MUST run in control mode - the wrapper reads its "
            "instructions from the config file the control runtime delivers"
        )
        assert exec_context.get("step_execution_id")

        runs = (await api_client.get("/api/pipeline-runs")).json()
        adhoc = [r for r in runs if r["trigger_ref"] == card["id"]]
        assert len(adhoc) == 1, "card start must produce exactly one ad-hoc run"
        assert adhoc[0]["trigger_type"] == "card_work"

        run_detail = (
            await api_client.get(f"/api/pipeline-runs/{adhoc[0]['id']}")
        ).json()
        assert [sr["executor"] for sr in run_detail["step_runs"]] == ["local"], (
            "R1: the routing decision is recorded on the StepRun, and it must "
            "say local - agent steps left the runner queue at 12.5"
        )

        # --- 2. the agent commits and pushes ------------------------------
        async def agent_commits_and_pushes(_exec_context):
            commit_onto_branch(
                clean_git_repos,
                repo_id,
                default_branch,
                started["branch_name"],
                AGENT_FILE,
                AGENT_CONTENT,
            )

        agent_container.on_agent_release = agent_commits_and_pushes
        agent_container.release_agent.set()

        # --- 3. the run completes -> card enters review -------------------
        async def card_in_review():
            body = (await api_client.get(f"/api/cards/{card['id']}")).json()
            return body if body["status"] == "in_review" else None

        reviewed = await wait_until(
            card_in_review,
            message=(
                "the card never reached in_review - agent_run.on_run_complete "
                "did not run (check the hook at the end of _complete_pipeline)"
            ),
        )
        assert reviewed["completed_runner_type"] == "mock"
        assert started["branch_name"] in clean_git_repos.list_branches(repo_id)

        # --- 4. THE GATE --------------------------------------------------
        async def gate_ran():
            body = (await api_client.get(f"/api/pipelines/{gate['id']}/runs")).json()
            return body or None

        gate_runs = await wait_until(
            gate_ran,
            message=(
                "the card_complete trigger never fired - a card reaching "
                "in_review is what runs the verification pipeline (US-2)"
            ),
        )
        assert gate_runs[0]["trigger_ref"] == card["id"]

        async def gate_finished():
            body = (await api_client.get(f"/api/pipelines/{gate['id']}/runs")).json()
            return body[0] if body[0]["status"] in ("passed", "failed") else None

        finished = await wait_until(gate_finished, message="the gate never finished")
        assert finished["status"] == "passed", finished

        # --- 5. review -> merge -------------------------------------------
        response = await api_client.post(
            f"/api/cards/{card['id']}/approve", json={}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["card"]["status"] == "done"
        assert body["merge_result"]["success"], body["merge_result"]

        merged = clean_git_repos.get_file_content(repo_id, default_branch, AGENT_FILE)
        assert merged is not None, "the agent's file never reached the trunk"
        assert AGENT_CONTENT.encode() in merged

        # --- and every step of the chain ran on the control layer ---------
        # 12.5 asserted this by spying on the polling queue; 12.6 deleted the
        # queue, so the durable form of the same claim is the executor field
        # every StepRun records at dispatch (R1). A silent handoff to some
        # other path would show up here as a different value or as no
        # StepRun at all.
        runs = (await api_client.get("/api/pipeline-runs")).json()
        executors = {
            sr["executor"]
            for run in runs
            for sr in (await api_client.get(f"/api/pipeline-runs/{run['id']}")).json()[
                "step_runs"
            ]
        }
        assert executors <= {"local"}, (
            f"the US-2 loop ran a step somewhere unexpected: {executors}"
        )

    async def test_failed_agent_run_fails_the_card_without_gating(
        self, api_client, test_repo, clean_git_repos, agent_container
    ):
        """The other half of the gate: a failed agent must not reach review.

        A card that flipped to in_review on a failed run would fire the
        verification pipeline against a branch the agent never pushed - the
        review queue filling with work nobody did.
        """
        repo_id = test_repo["id"]
        gate = await create_gate_pipeline(api_client, repo_id)
        card = await create_agent_card(api_client, repo_id)

        report = agent_container._report_status

        async def fail_step(step_config, execution_context):
            agent_container.calls.append((dict(step_config), dict(execution_context)))
            yield {"type": "status", "status": "running"}
            await report(execution_context, "running")
            await report(execution_context, "failed", exit_code=1)
            yield {
                "type": "result",
                "status": "failed",
                "exit_code": 1,
                "error": "mock agent exited 1",
                "log_tail": [],
            }

        agent_container.execute_step = fail_step

        response = await api_client.post(f"/api/cards/{card['id']}/start")
        assert response.status_code == 200, response.text

        async def card_failed():
            body = (await api_client.get(f"/api/cards/{card['id']}")).json()
            return body if body["status"] == "failed" else None

        failed = await wait_until(
            card_failed, message="a failed agent run must fail the card"
        )
        assert failed["status"] == "failed"

        gate_runs = (
            await api_client.get(f"/api/pipelines/{gate['id']}/runs")
        ).json()
        assert gate_runs == [], (
            "the verification pipeline ran for a card that never reached "
            "in_review"
        )
