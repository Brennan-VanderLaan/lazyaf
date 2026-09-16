"""The 12.7 exit gate, end to end - T3 (R7).

The design named this file and it was not delivered, so the debug re-run loop
had no tier the ratchet gates: T3 stayed at 19 while the whole feature shipped
behind a Playwright spec (the R8 surface, which no tier runs). This is that
file, and it drives the loop the phase exists for, on the real stack:

    a run FAILS  ->  debug re-run with a breakpoint  ->  the CLI attaches a
    real terminal  ->  the developer fixes the workspace from the shell  ->
    @resume  ->  the pipeline COMPLETES

Nothing in the middle is a double. The pipeline runs on real containers, the
workspace is a real named volume, the sidecar is a real
`lazyaf-debug-sidecar:dev` container, the terminal is a REAL WebSocket to a
REAL uvicorn serving the real app, and the client on the other end of it is
the SHIPPED CLI BINARY - `lazyaf debug attach <sid> --token <t> --server
<url>`, driven as a subprocess with stdin piped (upcoming/go-cli.md §3.6) -
not a test harness that happens to speak the same JSON. That last point is
the reason this test earns its runtime: it is the only place where the CLI's
codec and the server's codec meet over a socket rather than in a contract
test.

The binary comes from `$LAZYAF_CLI_BIN`, default `<repo>/cli/bin/lazyaf[.exe]`
(what the TG tier's preflight, `bash scripts/build_cli.sh --host-only`, leaves
on the run's workspace volume - §10.4). Absent, this file FAILS naming that
command; it never skips (R4): a skipped exit gate is the fake green the
tier floors exist to catch.

With stdin a pipe the client runs line-buffered (it says so on stderr) and
the escape decoder is still applied to the raw bytes, so `\x1dr` followed by
a newline is `@resume` as a `command` frame and then one stdin frame
carrying the newline. The server may already be closing when that second
frame arrives - harmless, and it is not asserted against (§13.4).

The ONE stub is workspace population: the git clone needs the backend's git
server reachable from the container network, which is the `e2e-lane` skip
already baselined for the population suite. Everything the clone would have
produced, step 0 writes itself - and the assertion that matters is that the
SIDECAR and the RESUMED STEP see the same bytes.

`local_exec` is deliberate: it opts this test out of the root conftest's
Docker-free stubs, so the local execution path here is the production one.
T3 already carries the `build_images.py --check` preflight, so a missing
sidecar image is a loud preflight failure, never a skip (R4).
"""
import asyncio
import json
import os
import socket as socket_mod
import sys
from pathlib import Path
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.main import app
from app.models import Pipeline, PipelineRun, Repo
from app.models.pipeline import RunStatus
from app.routers.debug import get_debug_session_factory, read_join_token
from app.services.execution import debug_terminal as terminal
from app.services.execution.debug_session_service import debug_session_service
from app.services.execution.debug_state import DebugState
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.e2e, pytest.mark.local_exec]

STEP_IMAGE = "python:3.12-slim"
MARKER = "bytes-written-by-the-first-step"
FIX_FILE = "/workspace/repo/fix.txt"

#: The shipped binary (§3.6). $LAZYAF_CLI_BIN, else what build_cli.sh
#: --host-only leaves under cli/bin.
LAZYAF_CLI_BIN = Path(
    os.environ.get("LAZYAF_CLI_BIN")
    or REPO_ROOT / "cli" / "bin" / ("lazyaf.exe" if os.name == "nt" else "lazyaf")
)
BUILD_REMEDY = "bash scripts/build_cli.sh --host-only"


# -----------------------------------------------------------------------------
# The pipeline: a step that fails for a reason the developer can fix from a
# shell. That is what makes "resume" a loop and not just an unpause.
# -----------------------------------------------------------------------------

#: A two-step LINEAR GRAPH. 12.8 retires the v1 array, so the breakpoints
#: below name their step by graph `step_id` ("build" / "verify") instead of by
#: position ("0" / "1"), and the ids are what the paused session reports.
STEPS_GRAPH = {
    "version": 2,
    "entry_points": ["build"],
    "steps": {
        "build": {
            "name": "build",
            "type": "script",
            "config": {
                "image": STEP_IMAGE,
                # 0777 on purpose and stated: the step containers run as root,
                # the sidecar execs as uid 1000 (it must, or every file it
                # creates is root-owned and the resumed step trips over it).
                # Without this the developer could read the workspace and not
                # write to it.
                "command": (
                    f"mkdir -p /workspace/repo && chmod 0777 /workspace/repo && "
                    f"echo {MARKER} > /workspace/repo/marker.txt && echo built"
                ),
            },
        },
        "verify": {
            "name": "verify",
            "type": "script",
            "config": {
                "image": STEP_IMAGE,
                "command": (
                    f"if [ -f {FIX_FILE} ]; then echo verify-passed; else "
                    f"echo 'missing {FIX_FILE}'; exit 1; fi"
                ),
            },
        },
    },
    "edges": [
        {
            "id": "edge_0_success",
            "from_step": "build",
            "to_step": "verify",
            "condition": "success",
        }
    ],
}


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


def free_port() -> int:
    with socket_mod.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def cli_bin() -> str:
    """The built `lazyaf` binary - loud when absent (R4), never a skip.

    The TG tier's preflight builds it before T3 runs (§10.4); a developer
    running T3 alone builds it once with the named command.
    """
    if not LAZYAF_CLI_BIN.is_file():
        pytest.fail(
            f"the lazyaf binary is missing at {LAZYAF_CLI_BIN} (override with "
            f"$LAZYAF_CLI_BIN). This test drives the SHIPPED CLI over a real "
            f"socket, so build it first:\n    {BUILD_REMEDY}"
        )
    return str(LAZYAF_CLI_BIN)


@pytest.fixture
def docker_client():
    """Loud on a down daemon (R4) - never a skip."""
    client = docker_sdk.from_env(timeout=180)
    client.ping()
    return client


@pytest.fixture(autouse=True)
def images(docker_client):
    try:
        docker_client.images.get(STEP_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        docker_client.images.pull(STEP_IMAGE)
    sidecar = terminal.sidecar_image()
    try:
        docker_client.images.get(sidecar)
    except docker_sdk.errors.ImageNotFound as exc:
        raise AssertionError(
            f"the debug sidecar image {sidecar!r} is missing. It is a T3 "
            f"preflight requirement (scripts/run_tier.py runs "
            f"`build_images.py --check`). Build it with:\n"
            f"    python scripts/build_images.py"
        ) from exc
    return sidecar


@pytest_asyncio.fixture
async def stack(async_engine, monkeypatch, docker_client):
    """A REAL server for the terminal socket, bound to the TEST engine.

    HTTP goes through the ASGI client the rest of the e2e tier uses; the
    WebSocket cannot (an ASGI transport has no socket), so uvicorn serves the
    same `app` object in the same process on a loopback port. Both paths see
    one app, one set of dependency overrides and one database.

    `lifespan="off"`: the app's startup hook runs migrations and recovery
    sweeps against the CONFIGURED database. A test server must not touch it.
    """
    import uvicorn

    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    # The endpoint takes its session factory as a DEPENDENCY precisely so a
    # test can bind it to its own engine - the app's global factory points at
    # the configured database, where this run does not exist.
    app.dependency_overrides[get_debug_session_factory] = lambda: factory

    port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while not server.started:
        if serve_task.done() or loop.time() > deadline:
            serve_task.cancel()
            raise RuntimeError("the terminal test server never started")
        await asyncio.sleep(0.05)

    run_ids: list[str] = []
    state = type(
        "Stack",
        (),
        {
            "factory": factory,
            "ws_base": f"ws://127.0.0.1:{port}",
            # What the binary's --server takes: it derives ws:// itself
            # (terminal.TerminalURL) and refuses a schemeless URL (§5).
            "http_base": f"http://127.0.0.1:{port}",
            "run_ids": run_ids,
            "docker": docker_client,
        },
    )()
    try:
        yield state
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=15)
        except asyncio.TimeoutError:
            serve_task.cancel()
            raise RuntimeError("the terminal test server did not shut down")
        app.dependency_overrides.pop(get_debug_session_factory, None)
        await debug_session_service.reset()
        await terminal.debug_terminal_service.reset()
        for run_id in run_ids:
            try:
                docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
            except docker_sdk.errors.NotFound:
                pass
        for container in docker_client.containers.list(
            all=True,
            filters={"label": f"{terminal.LABEL_TYPE}={terminal.LABEL_TYPE_VALUE}"},
        ):
            if (container.labels or {}).get(terminal.LABEL_RUN) in run_ids:
                container.remove(force=True)


@pytest_asyncio.fixture
async def repo_and_pipeline(async_engine):
    """A repo + a two-step pipeline, inserted directly.

    Ingestion has its own suites; what this test is the gate for starts at
    "a run failed", so the rows are seeded rather than driven through the
    ingest API.
    """
    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="debug-rerun-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="debug-rerun-pipeline",
            steps_graph=json.dumps(STEPS_GRAPH),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        return {"repo_id": repo.id, "pipeline_id": pipeline.id}


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


async def wait_until(predicate, timeout=120.0, message="condition never held"):
    """Poll a coroutine predicate; a blown deadline is a LOUD failure naming
    the last value seen - never a silent pass."""
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await predicate()
        if last:
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(f"{message} (last value: {last!r})")


async def run_status(api_client, run_id: str) -> dict:
    response = await api_client.get(f"/api/pipeline-runs/{run_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def wait_for_run(api_client, run_id: str, *statuses) -> dict:
    async def _done():
        run = await run_status(api_client, run_id)
        return run if run["status"] in statuses else None

    return await wait_until(
        _done, message=f"run {run_id[:8]} never reached {statuses}"
    )


class AttachedCLI:
    """The shipped binary, attached, with the TEST at the keyboard.

    `lazyaf debug attach <sid> --token <t> --server <url>` as a subprocess:
    stdin is the pipe the test types into, stdout is the sidecar shell's
    byte stream (the client writes it byte-exact), stderr is the client's
    own chatter (the READ-WRITE banner, the console mode, the final reason).
    Everything between the pipe and the container - the escape decoder, the
    frame codec, the websocket, the endpoint, the exec'd shell - is
    production code, in the production binary.
    """

    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.output = bytearray()
        self.stderr = bytearray()
        self._wakeup = asyncio.Event()
        self._pumps = [
            asyncio.create_task(self._pump(proc.stdout, self.output)),
            asyncio.create_task(self._pump(proc.stderr, self.stderr)),
        ]

    @classmethod
    async def start(cls, cli_bin: str, server_url: str, session_id: str, token: str) -> "AttachedCLI":
        proc = await asyncio.create_subprocess_exec(
            cli_bin,
            "--server", server_url,
            "debug", "attach", session_id, "--token", token,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "NO_COLOR": "1"},
        )
        return cls(proc)

    async def _pump(self, stream, sink: bytearray) -> None:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            sink += chunk
            self._wakeup.set()

    # -- the test's surface --------------------------------------------------

    def type(self, data: bytes) -> None:
        """Keystrokes. The client is line-buffered on a pipe, so a line at a
        time reaches the shell; stdin stays OPEN until close(), exactly as a
        keyboard does, so `local input reached EOF` can never be the way an
        attach ends here."""
        self.proc.stdin.write(data)

    async def wait_for_output(self, needle: bytes, timeout: float = 60.0) -> bytes:
        deadline = asyncio.get_running_loop().time() + timeout
        while needle not in self.output:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AssertionError(
                    f"{needle!r} never appeared on the terminal: "
                    f"{bytes(self.output)!r}\nclient stderr: {self.stderr_text()!r}"
                )
            if self.proc.returncode is not None:
                raise AssertionError(
                    f"the client exited {self.proc.returncode} before "
                    f"{needle!r} appeared: {bytes(self.output)!r}\n"
                    f"client stderr: {self.stderr_text()!r}"
                )
            self._wakeup.clear()
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                continue
        return bytes(self.output)

    async def wait(self, timeout: float = 60.0) -> int:
        """The client's exit code, once it ends on its own; every byte it
        wrote has been read before this returns."""
        code = await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        await asyncio.gather(*self._pumps)
        return code

    def stderr_text(self) -> str:
        return bytes(self.stderr).decode("utf-8", "replace")

    async def close(self) -> None:
        if self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        for pump in self._pumps:
            pump.cancel()
        await asyncio.gather(*self._pumps, return_exceptions=True)
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001 - a pipe the child already dropped
            pass


# -----------------------------------------------------------------------------
# The exit gate
# -----------------------------------------------------------------------------


class TestDebugRerunLoop:
    async def test_failed_run_debugged_fixed_from_the_shell_and_completed(
        self, api_client, stack, repo_and_pipeline, cli_bin
    ):
        pipeline_id = repo_and_pipeline["pipeline_id"]

        # --- 1. a run fails ------------------------------------------------
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        assert response.status_code == 200, response.text
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)

        failed = await wait_for_run(api_client, original_id, RunStatus.FAILED.value)
        # Keyed by graph step ID, which is what a step IS after 12.8 - and
        # what the breakpoint below names.
        steps = {s["step_id"]: s for s in failed["step_runs"]}
        assert steps["build"]["status"] == RunStatus.PASSED.value
        assert steps["verify"]["status"] == RunStatus.FAILED.value
        assert FIX_FILE in (steps["verify"]["logs"] or ""), (
            "the failing step must say what it could not find - that sentence "
            "is what a developer debugs from"
        )

        # --- 2. debug re-run, breakpointed before the failing step ---------
        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["verify"], "use_original_commit": True},
        )
        assert response.status_code == 200, response.text
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)
        assert rerun["join_command"] == f"lazyaf debug attach {session_id}"

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        info = await wait_until(_paused, message="the gate never paused")
        assert info["current_step"]["key"] == "verify"
        assert info["current_step"]["name"] == "verify"
        assert info["attach_available"] is True
        assert "token" not in info, (
            "C14: GET /api/debug/{id} must never carry the session's "
            "credential - a secret in a polled response is not a secret"
        )

        # --- 3. mint a credential and ATTACH a real terminal ---------------
        response = await api_client.post(f"/api/debug/{session_id}/join-token")
        assert response.status_code == 200, response.text
        token = response.json()["token"]
        assert read_join_token(token) == session_id

        # The binary, with the credential the server minted - the exact line
        # `join_command` prints once --token exists (§5): no second mint.
        console = await AttachedCLI.start(cli_bin, stack.http_base, session_id, token)
        try:
            # The sidecar mounts the paused run's workspace: step 0's bytes.
            console.type(b"cat /workspace/repo/marker.txt\n")
            await console.wait_for_output(MARKER.encode())

            # --- 4. fix it from the shell ------------------------------
            console.type(f"echo fixed > {FIX_FILE} && echo wrote-the-fix\n".encode())
            await console.wait_for_output(b"wrote-the-fix")
            console.type(b"ls /workspace/repo && echo listed\n")
            listing = await console.wait_for_output(b"listed")
            assert b"fix.txt" in listing

            # --- 5. @resume, over the wire, from the CLI's escape key ---
            # Ctrl-] r, then the newline the line-buffered client needs to
            # hand the chunk over (§13.4). stdin stays open: the attach
            # ends because the SERVER closes it with "resumed".
            console.type(b"\x1dr\n")
            exit_code = await console.wait(timeout=60)
        finally:
            await console.close()

        client_said = console.stderr_text()
        # C12, observed from outside the process: Ctrl-] r must reach the
        # server as a `command` frame, never as the bytes '@resume' sniffed
        # out of stdin. The Python driver exposed `result.commands` for this;
        # a subprocess exposes its EFFECT - the server, which only resumes on
        # a command frame, closed the terminal saying "resumed", and step 6
        # below sees the run complete. Typed bytes would have gone to the
        # shell (`^]r` echoed on stdout) and the gate would still be paused.
        # (The sidecar's own MOTD names the @-verbs, so the shell's output
        # cannot be grepped for '@resume' as a negative.)
        assert "resumed" in client_said, client_said
        assert exit_code == 0, client_said
        assert "READ-WRITE" in client_said, (
            "the banner must reach the operator on stderr: /workspace is "
            "read-write and the resumed step sees the edits"
        )
        assert "line-buffered" in client_said, (
            "a pipe is not a TTY; the client must SAY it is running "
            "line-buffered rather than pretend to be a raw terminal (R1)"
        )

        # --- 6. the pipeline completes ------------------------------------
        completed = await wait_for_run(api_client, run_id, RunStatus.PASSED.value)
        steps = {s["step_id"]: s for s in completed["step_runs"]}
        assert steps["verify"]["status"] == RunStatus.PASSED.value
        assert "verify-passed" in (steps["verify"]["logs"] or ""), (
            "THE loop: the step that failed on the first run passed on the "
            "re-run because of a file created from the debug shell"
        )
        assert "[debug] paused before step" in (steps["verify"]["logs"] or "")

        # The session ends with the run, saying why (R1), and everything it
        # created goes with it (C9: sidecar before volume).
        #
        # POLLED, not read once: "the run reached PASSED" and "the session
        # reached ENDED" are two commits inside `_complete_pipeline`, and the
        # API can answer the first before the second lands. Asserting
        # instantly here is a real race - it cost this file one red run in ten
        # before the wait went in.
        async def _ended():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.ENDED.value else None

        session = await wait_until(
            _ended, timeout=60, message="the session outlived its run"
        )
        assert session["end_reason"]
        assert session["attach_available"] is False
        assert session["attach_unavailable_reason"]

        async def _torn_down():
            containers = stack.docker.containers.list(
                all=True, filters={"label": f"{terminal.LABEL_RUN}={run_id}"}
            )
            if containers:
                return None
            try:
                stack.docker.volumes.get(generate_volume_name(run_id))
            except docker_sdk.errors.NotFound:
                return True
            return None

        await wait_until(
            _torn_down,
            timeout=60,
            message="the sidecar and the workspace volume outlived the session",
        )

    async def test_a_terminal_without_a_credential_is_refused_at_the_upgrade(
        self, api_client, stack, repo_and_pipeline, cli_bin
    ):
        """C14 over a real socket - and the fact the contract got wrong.

        `routers/debug.py` refuses BEFORE `accept()` and writes its reason
        into the close frame, on the stated assumption that "the reason
        travels in the close REASON". Against a real ASGI server it does not:
        Starlette turns a pre-accept `close()` into a plain **HTTP 403**
        during the handshake, and the sentence never leaves the process.
        Only a real socket could show that, which is exactly why this test is
        in a tier and not in a fixture.

        So the refusal is still LOUD and still terminal - it is simply not
        self-describing, and the CLI says so rather than inventing a reason
        (R1). The backend fix is in this agent's report as a requested edit.
        """
        pipeline_id = repo_and_pipeline["pipeline_id"]
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)
        await wait_for_run(api_client, original_id, RunStatus.FAILED.value)

        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["verify"], "use_original_commit": True},
        )
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        await wait_until(_paused, message="the gate never paused")

        # The binary with a credential that proves nothing: the session is
        # attachable (so the client gets as far as the socket), and the
        # upgrade is what refuses.
        console = await AttachedCLI.start(
            cli_bin, stack.http_base, session_id, "not-a-real-token"
        )
        try:
            exit_code = await console.wait(timeout=30)
        finally:
            await console.close()
        reason = console.stderr_text()

        assert exit_code == 1, f"a refused attach must not read as success: {reason}"
        assert "403" in reason, (
            "the CLI must name the status it actually got, not a close code "
            f"the handshake never carried: {reason}"
        )
        assert f"lazyaf debug status {session_id}" in reason, (
            "R1: when the reason is undeliverable, the CLI must say where it "
            f"CAN be read rather than guessing: {reason}"
        )
        # No sidecar was created for a refused upgrade.
        assert stack.docker.containers.list(
            all=True, filters={"label": f"{terminal.LABEL_RUN}={run_id}"}
        ) == []

        # Leave the run terminal rather than a paused gate holding a volume.
        assert (
            await api_client.post(f"/api/debug/{session_id}/abort")
        ).status_code == 200
        await wait_for_run(api_client, run_id, RunStatus.CANCELLED.value)


class TestDebugRerunIsNotTheOriginalRun:
    async def test_the_rerun_carries_only_branch_and_commit(
        self, api_client, stack, repo_and_pipeline, async_engine
    ):
        """C10 at the API surface: a debug re-run can never merge a branch and
        never moves a card, because the context it carries has nothing to act
        on. Asserted on the persisted row, not on a call list."""
        pipeline_id = repo_and_pipeline["pipeline_id"]
        response = await api_client.post(f"/api/pipelines/{pipeline_id}/run", json={})
        original_id = response.json()["id"]
        stack.run_ids.append(original_id)
        await wait_for_run(api_client, original_id, RunStatus.FAILED.value)

        response = await api_client.post(
            f"/api/pipeline-runs/{original_id}/debug-rerun",
            json={"breakpoints": ["build"], "use_original_commit": True},
        )
        rerun = response.json()
        session_id, run_id = rerun["debug_session_id"], rerun["run_id"]
        stack.run_ids.append(run_id)

        async def _paused():
            info = (await api_client.get(f"/api/debug/{session_id}")).json()
            return info if info["status"] == DebugState.WAITING_AT_BP.value else None

        await wait_until(_paused, message="the gate never paused")

        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as db:
            run = (
                await db.execute(select(PipelineRun).where(PipelineRun.id == run_id))
            ).scalar_one()
        context = json.loads(run.trigger_context or "{}")
        assert set(context) <= {"branch", "commit_sha"}, (
            f"a debug re-run's trigger_context must carry branch and commit "
            f"and nothing else (C10); got {sorted(context)}"
        )
        assert run.trigger_ref == original_id

        # Resume past the single breakpoint so the run reaches a terminal
        # state on its own rather than being torn down mid-pause.
        assert (
            await api_client.post(
                f"/api/debug/{session_id}/resume", json={"clear_remaining": True}
            )
        ).status_code == 200
        await wait_for_run(api_client, run_id, RunStatus.FAILED.value)
