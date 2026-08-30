"""Executor contract tests for RemoteExecutor (Phase 12.6).

The gate on the whole phase is one sentence: **dispatch must not special-case
remote**. `pipeline_executor._consume_local_events` and `_finish_local_step`
are untouched by 12.6, and the only way to know that is to drive BOTH
executors and compare what comes out.

So the first class here runs `LocalExecutor.execute_step` and
`RemoteExecutor.execute_step` over the same step and asserts the yielded
event-type SEQUENCE and the result-dict KEY SET are identical. If either
executor ever needs the consumer to learn something new about it, this test
goes red before the consumer does.

Written against the PUBLIC API only. The salvage audit's verdict on
failure_01's contract tests was "right scenarios, wrong coupling - pokes
private dicts, patches privates", and a contract test that reaches inside is
testing an implementation, not a contract. The two seams used here are the
docker client injected into LocalExecutor's constructor (its documented
parameter) and the WebSocket transport handed to the registry - both real
collaborator slots, not monkeypatched internals.
"""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.pipeline import (
    Pipeline,
    PipelineRun,
    StepExecution,
    StepExecutionStatus,
    StepRun,
)
from app.models.repo import Repo
from app.models.runner import Runner
from app.services.execution.job_recovery import JobRecoveryService
from app.services.execution.local_executor import LocalExecutor
from app.services.execution.remote_executor import RemoteExecutor
from app.services.execution.runner_dispatcher import RunnerDispatcher
from app.services.execution.runner_protocol import RegisterMessage
from app.services.execution.runner_registry import RunnerRegistry
from app.services.execution.runner_state import RunnerState


# -----------------------------------------------------------------------------
# Harness: a docker daemon that isn't, and an agent that is only a socket
# -----------------------------------------------------------------------------

class FakeContainer:
    """Just enough container for LocalExecutor's control-mode sequence."""

    def __init__(self, exit_code: int = 0, log_lines: list[bytes] | None = None):
        self.id = "fake-container"
        self._exit_code = exit_code
        self._log_lines = log_lines or []
        self.started = False
        self.removed = False
        self.archives: list[tuple[str, bytes]] = []

    def put_archive(self, path, data):
        self.archives.append((path, data))
        return True

    def start(self):
        self.started = True

    def logs(self, stream=False, follow=False):
        return iter(self._log_lines)

    def wait(self, timeout=None):
        return {"StatusCode": self._exit_code}

    def remove(self, force=False):
        self.removed = True

    def kill(self):
        pass


class FakeDocker:
    """A docker client stand-in. LocalExecutor takes one by constructor."""

    class _Networks:
        def get(self, name):
            return object()

    class _Containers:
        def __init__(self, container):
            self._container = container
            self.created: list[tuple[str, dict]] = []

        def create(self, image, **kwargs):
            self.created.append((image, kwargs))
            return self._container

        def run(self, image, **kwargs):  # pragma: no cover - control mode only
            self.created.append((image, kwargs))
            return self._container

    def __init__(self, container: FakeContainer):
        self.containers = self._Containers(container)
        self.networks = self._Networks()


class AgentSocket:
    """A runner agent reduced to the frames it puts on the wire.

    Not an AsyncMock (R6): it parses what the backend actually sent, and its
    replies go back through the dispatcher's PUBLIC notify_* surface - the
    same one `ws_runners.py` calls after the step gate.
    """

    def __init__(
        self,
        dispatcher,
        runner_id: str,
        *,
        exit_code: int = 0,
        error: str | None = None,
        ack: bool = True,
        complete: bool = True,
    ):
        self.frames: list[dict] = []
        self.closed = False
        self._dispatcher = dispatcher
        self._runner_id = runner_id
        self._exit_code = exit_code
        self._error = error
        self._ack = ack
        self._complete = complete

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.frames.append(frame)
        if frame.get("type") == "execute_step":
            asyncio.get_running_loop().create_task(self._respond(frame["step_id"]))

    async def close(self, code: int = 1000) -> None:
        self.closed = True

    def of_type(self, message_type: str) -> list[dict]:
        return [f for f in self.frames if f.get("type") == message_type]

    async def _respond(self, step_id: str) -> None:
        await asyncio.sleep(0)
        if not self._ack:
            return
        self._dispatcher.notify_ack(step_id, self._runner_id)
        if not self._complete:
            return
        await asyncio.sleep(0)
        self._dispatcher.notify_complete(
            step_id, self._runner_id, self._exit_code, self._error
        )


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fast_budgets(monkeypatch):
    """Shrink the two wall-clock budgets so T1 stays a fast tier.

    ACK_TIMEOUT (5s) and NO_RUNNER_TIMEOUT (300s) are real protocol values
    and the behavior under test is "the budget is enforced at all", not its
    magnitude. Patching the module constants keeps every failure path
    exercised in under a second - and a test that quietly waited out the real
    300s would be a five-minute unit test nobody would run twice.
    """
    monkeypatch.setattr("app.services.execution.remote_executor.ACK_TIMEOUT", 0.3)
    monkeypatch.setattr("app.services.execution.remote_executor.NO_RUNNER_TIMEOUT", 0.5)


@pytest_asyncio.fixture
async def sessions(async_engine):
    return async_sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def registry():
    reg = RunnerRegistry()
    yield reg
    await reg.reset()


@pytest_asyncio.fixture
async def dispatcher(registry, sessions):
    disp = RunnerDispatcher(
        registry=registry, recovery=JobRecoveryService(), session_factory=sessions
    )
    disp.install_hooks()
    yield disp
    registry.set_dispatch_waker(None)
    disp.recovery.set_requeue_hook(None)
    await disp.reset()


@pytest_asyncio.fixture
async def remote(registry, dispatcher, sessions):
    executor = RemoteExecutor(
        registry=registry,
        dispatcher=dispatcher,
        recovery=dispatcher.recovery,
        session_factory=sessions,
    )
    yield executor
    executor.reset()


@pytest_asyncio.fixture
async def chain(db_session):
    repo = Repo(id=str(uuid4()), name="contract-repo")
    pipeline = Pipeline(
        id=str(uuid4()), repo_id=repo.id, name="contract-pipeline", steps="[]"
    )
    run = PipelineRun(id=str(uuid4()), pipeline_id=pipeline.id, status="running")
    step_run = StepRun(
        id=str(uuid4()),
        pipeline_run_id=run.id,
        step_index=0,
        step_name="remote step",
        status="running",
    )
    db_session.add_all([repo, pipeline, run, step_run])
    await db_session.commit()
    return {"run": run, "step_run": step_run}


async def make_execution(db, chain) -> StepExecution:
    execution = StepExecution(
        id=str(uuid4()),
        step_run_id=chain["step_run"].id,
        execution_key=f"{chain['run'].id}:0:{uuid4()}",
        status=StepExecutionStatus.PENDING.value,
    )
    db.add(execution)
    await db.commit()
    return execution


def step_config() -> dict:
    return {
        "type": "script",
        "command": "echo hello",
        "timeout": 300,
        "image": "lazyaf-base:dev",
    }


def exec_context(chain, execution, **extra) -> dict:
    context = {
        "pipeline_run_id": chain["run"].id,
        "step_run_id": chain["step_run"].id,
        "step_index": 0,
        "execution_key": execution.execution_key,
        "workspace_volume": f"lazyaf-ws-{chain['run'].id[:8]}",
        "control_mode": True,
        "step_execution_id": execution.id,
        "step_auth_token": "step-jwt-for-tests",
    }
    context.update(extra)
    return context


async def connect(registry, db, runner_id, socket, labels=None):
    return await registry.connect(
        db,
        socket,
        RegisterMessage(
            runner_id=runner_id,
            name=runner_id,
            runner_type="generic",
            labels=labels or {},
        ),
    )


async def drain(generator) -> list[dict]:
    return [event async for event in generator]


# -----------------------------------------------------------------------------
# Cross-agent contract #3: THE gate on "dispatch does not special-case remote"
# -----------------------------------------------------------------------------

class TestExecutorContractParity:
    """LocalExecutor and RemoteExecutor produce the same stream.

    Both are driven in CONTROL MODE, which is the configuration the remote
    lane actually runs: the step container POSTs its own status/logs/
    heartbeat/test-results/usage to /api/steps/* with the step JWT, on either
    host, so the executor stream carries lifecycle and the terminal outcome
    and nothing else.
    """

    @pytest_asyncio.fixture
    async def local(self):
        return LocalExecutor(FakeDocker(FakeContainer(exit_code=0)))

    async def _remote_events(self, remote, registry, dispatcher, sessions, chain):
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "runner-a", AgentSocket(dispatcher, "runner-a"))
        return await drain(
            remote.execute_step(step_config(), exec_context(chain, execution))
        )

    async def test_event_type_sequence_is_identical(
        self, local, remote, registry, dispatcher, sessions, db_session, chain
    ):
        execution = await make_execution(db_session, chain)
        local_events = await drain(
            local.execute_step(step_config(), exec_context(chain, execution))
        )
        remote_events = await self._remote_events(
            remote, registry, dispatcher, sessions, chain
        )

        assert [e["type"] for e in local_events] == [e["type"] for e in remote_events]
        assert [e["type"] for e in local_events] == [
            "status",
            "status",
            "status",
            "result",
        ]

    async def test_status_values_are_identical(
        self, local, remote, registry, dispatcher, sessions, db_session, chain
    ):
        execution = await make_execution(db_session, chain)
        local_events = await drain(
            local.execute_step(step_config(), exec_context(chain, execution))
        )
        remote_events = await self._remote_events(
            remote, registry, dispatcher, sessions, chain
        )

        statuses = [e.get("status") for e in local_events]
        assert statuses == [e.get("status") for e in remote_events]
        assert statuses == ["preparing", "running", "completed", "completed"]

    async def test_result_key_set_is_identical(
        self, local, remote, registry, dispatcher, sessions, db_session, chain
    ):
        """The KEY SET, not just the values.

        An extra key on one side (a `cached` flag, a `runner_id`, a
        `log_tail`) is exactly the kind of drift that makes a consumer grow
        an `if remote:` branch six months later.
        """
        execution = await make_execution(db_session, chain)
        local_result = (
            await drain(local.execute_step(step_config(), exec_context(chain, execution)))
        )[-1]
        remote_result = (
            await self._remote_events(remote, registry, dispatcher, sessions, chain)
        )[-1]

        assert set(local_result) == set(remote_result)
        assert set(remote_result) == {"type", "status", "exit_code"}

    async def test_failing_step_result_key_set_is_identical(
        self, remote, registry, dispatcher, sessions, db_session, chain
    ):
        """A non-zero exit produces the same shape on both paths.

        Exit code is ground truth for step outcome in both modes: on the
        local path it comes from `container.wait()`, on the remote path from
        `step_complete`. Same datum, same place in the result.
        """
        local = LocalExecutor(FakeDocker(FakeContainer(exit_code=7)))
        execution = await make_execution(db_session, chain)
        local_result = (
            await drain(local.execute_step(step_config(), exec_context(chain, execution)))
        )[-1]

        async with sessions() as db:
            remote_execution = await make_execution(db, chain)
            await connect(
                registry,
                db,
                "runner-a",
                AgentSocket(dispatcher, "runner-a", exit_code=7),
            )
        remote_result = (
            await drain(
                remote.execute_step(
                    step_config(), exec_context(chain, remote_execution)
                )
            )
        )[-1]

        assert set(local_result) == set(remote_result)
        assert local_result["status"] == remote_result["status"] == "failed"
        assert local_result["exit_code"] == remote_result["exit_code"] == 7


# -----------------------------------------------------------------------------
# The assignment, and the "closed out before result" invariant
# -----------------------------------------------------------------------------

class TestAssignmentLifecycle:

    async def test_running_is_yielded_on_ack_not_on_send(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """A runner that never ACKs never reaches `running`.

        `running` means "a machine accepted this work", which is the remote
        analogue of the container having started. Yielding it on send would
        make a step that was never picked up look like a step that is running.
        """
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(
                registry,
                db,
                "silent",
                AgentSocket(dispatcher, "silent", ack=False),
            )

        events = await drain(
            remote.execute_step(step_config(), exec_context(chain, execution))
        )

        assert [e.get("status") for e in events if e["type"] == "status"] == [
            "preparing",
            "failed",
        ]

    async def test_step_execution_records_the_runner(
        self, remote, registry, dispatcher, sessions, chain, db_session
    ):
        """R1: which machine ran this step is an observable fact, not a guess."""
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "runner-a", AgentSocket(dispatcher, "runner-a"))

        await drain(remote.execute_step(step_config(), exec_context(chain, execution)))

        async with sessions() as db:
            row = await db.get(StepExecution, execution.id)
            assert row.runner_id == "runner-a"
            assert row.assigned_at is not None

    async def test_runner_is_idle_and_free_before_the_result_event(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """The remote half of LocalExecutor's "container removed before result".

        A consumer that stops at the result event must observe a fully
        closed-out assignment - otherwise the next step of the same run can
        race the previous one's teardown, which is exactly the class of bug
        fix 6 removed from the local path.
        """
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "runner-a", AgentSocket(dispatcher, "runner-a"))

        observed: dict = {}
        async for event in remote.execute_step(
            step_config(), exec_context(chain, execution)
        ):
            if event["type"] == "result":
                async with sessions() as db:
                    row = await db.get(Runner, "runner-a")
                    observed = {
                        "status": row.status,
                        "step": row.current_step_execution_id,
                    }
                break

        assert observed == {"status": RunnerState.IDLE.value, "step": None}

    async def test_late_frames_after_the_result_are_inert(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """Once the result is yielded, no further wire frame for this step is
        honored - the rendezvous is closed."""
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "runner-a", AgentSocket(dispatcher, "runner-a"))

        await drain(remote.execute_step(step_config(), exec_context(chain, execution)))

        assert dispatcher.notify_complete(execution.id, "runner-a", 1, "too late") is False
        assert dispatcher.notify_ack(execution.id, "runner-a") is False

    async def test_a_runner_reported_outcome_leaves_the_execution_row_alone(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """`_reconcile_control_execution` owns the row when the runner spoke.

        A row still sitting at ASSIGNED after a `step_complete` is the signal
        "the container never ran a working control runtime" - the pipeline
        executor turns that into a loud failure even on exit code 0, and
        forcing the row terminal from out here would paper over it.
        """
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "runner-a", AgentSocket(dispatcher, "runner-a"))

        await drain(remote.execute_step(step_config(), exec_context(chain, execution)))

        async with sessions() as db:
            row = await db.get(StepExecution, execution.id)
            assert row.status == StepExecutionStatus.ASSIGNED.value

    async def test_an_executor_determined_failure_forces_the_row_terminal(
        self, remote, sessions, chain
    ):
        """No runner ever touched the step, so nothing else will close it.

        Leaving it non-terminal would let a leaked step token keep writing to
        a step nobody is watching.
        """
        async with sessions() as db:
            execution = await make_execution(db, chain)

        await drain(
            remote.execute_step(
                step_config(),
                exec_context(chain, execution, runner_requirements={"arch": "arm64"}),
            )
        )

        async with sessions() as db:
            row = await db.get(StepExecution, execution.id)
            assert row.status == StepExecutionStatus.FAILED.value
            assert "no runner matched" in (row.error or "")


# -----------------------------------------------------------------------------
# The attempt budget
# -----------------------------------------------------------------------------

class TestAckTimeoutAndBudget:

    async def test_ack_timeout_reassigns_to_a_second_runner(
        self, remote, registry, dispatcher, sessions, chain
    ):
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(
                registry, db, "aaa-silent", AgentSocket(dispatcher, "aaa-silent", ack=False)
            )
            await connect(
                registry, db, "zzz-healthy", AgentSocket(dispatcher, "zzz-healthy")
            )

        events = await drain(
            remote.execute_step(step_config(), exec_context(chain, execution))
        )

        assert events[-1]["status"] == "completed"
        async with sessions() as db:
            row = await db.get(StepExecution, execution.id)
            assert row.runner_id == "zzz-healthy"
            silent = await db.get(Runner, "aaa-silent")
            assert silent.status == RunnerState.DEAD.value

    async def test_the_budget_is_bounded_and_names_the_last_runner(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """Three failures fail the step. Without the budget, a fleet of
        broken agents requeues one step forever."""
        async with sessions() as db:
            execution = await make_execution(db, chain)
            for name in ("mute-1", "mute-2", "mute-3", "mute-4"):
                await connect(
                    registry, db, name, AgentSocket(dispatcher, name, ack=False)
                )

        events = await drain(
            remote.execute_step(step_config(), exec_context(chain, execution))
        )

        result = events[-1]
        assert result["status"] == "failed"
        assert "requeued 3 times" in result["error"]
        assert "last runner: mute-" in result["error"]

    async def test_no_matching_runner_names_the_requirements_and_the_fleet(
        self, remote, registry, dispatcher, sessions, chain
    ):
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(
                registry,
                db,
                "amd-box",
                AgentSocket(dispatcher, "amd-box"),
                labels={"arch": "amd64"},
            )

        events = await drain(
            remote.execute_step(
                step_config(),
                exec_context(chain, execution, runner_requirements={"arch": "arm64"}),
            )
        )

        result = events[-1]
        assert result["status"] == "failed"
        assert "arm64" in result["error"]
        assert "amd-box" in result["error"]
        assert result["exit_code"] is None


# -----------------------------------------------------------------------------
# A runner dying mid-step must not surface as a step failure
# -----------------------------------------------------------------------------

class TestMidStepDeath:

    async def test_the_same_generator_completes_on_a_second_runner(
        self, remote, registry, dispatcher, sessions, chain
    ):
        """No intermediate `result` event.

        The generator OWNS the step until terminal, exactly as LocalExecutor
        owns its container until `wait()` returns. That is the whole reason
        `pipeline_executor` can stay ignorant of remoteness: a mid-step death
        is a re-dispatch, not an outcome.
        """
        dying = AgentSocket(dispatcher, "aaa-dying", complete=False)
        async with sessions() as db:
            execution = await make_execution(db, chain)
            await connect(registry, db, "aaa-dying", dying)
            await connect(
                registry, db, "zzz-survivor", AgentSocket(dispatcher, "zzz-survivor")
            )

        events: list[dict] = []

        async def consume():
            async for event in remote.execute_step(
                step_config(), exec_context(chain, execution)
            ):
                events.append(event)

        consumer = asyncio.create_task(consume())
        await wait_until(lambda: bool(dying.of_type("execute_step")))
        await wait_until(lambda: any(e.get("status") == "running" for e in events))

        async with sessions() as db:
            runner = await db.get(Runner, "aaa-dying")
            await dispatcher.recovery.on_runner_death(db, runner)

        await asyncio.wait_for(consumer, timeout=10)

        assert [e["type"] for e in events] == ["status", "status", "status", "result"]
        assert [e.get("status") for e in events] == [
            "preparing",
            "running",
            "completed",
            "completed",
        ]
        async with sessions() as db:
            row = await db.get(StepExecution, execution.id)
            assert row.runner_id == "zzz-survivor"


# -----------------------------------------------------------------------------
# Guards
# -----------------------------------------------------------------------------

class TestGuards:

    async def test_missing_step_execution_id_fails_loudly(self, remote, chain):
        """Without a row there is nothing to compare-and-swap, nothing for
        the step JWT to authenticate, and no way home for the container."""
        context = {
            "pipeline_run_id": chain["run"].id,
            "step_run_id": chain["step_run"].id,
            "step_index": 0,
            "execution_key": "k",
            "workspace_volume": "vol",
            "control_mode": True,
        }

        events = await drain(remote.execute_step(step_config(), context))

        assert events[-1]["status"] == "failed"
        assert "step_execution_id" in events[-1]["error"]

    async def test_image_helpers_do_not_pretend_to_inspect_a_remote_host(
        self, remote
    ):
        """The image lives on the RUNNER's daemon.

        `image_declares_label` keeps the three-valued contract - None means
        "the inspection did not happen", which is a different fact from "the
        image does not declare it" and must not be collapsed.
        """
        assert await remote.image_supports_control_layer("lazyaf-base:dev") is True
        assert await remote.image_declares_label("lazyaf-base:dev", "x") is None
        assert await remote.find_missing_images(["lazyaf-base:dev"]) == []

    async def test_cancel_step_for_an_unknown_key_is_false_not_an_error(
        self, remote
    ):
        assert await remote.cancel_step("nothing-in-flight") is False
        assert await remote.cancel_all() == 0
