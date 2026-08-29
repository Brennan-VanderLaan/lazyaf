"""
Phase 12.3 HOME-persistence contract, on the REAL lazyaf-base:dev image (R6).

The PLAN 12.3 contract: HOME=/workspace/home lives on the run's NAMED
workspace volume, the base image bakes the user-install env block
(PIP_USER / PYTHONUSERBASE / NPM_CONFIG_PREFIX / PATH with
/workspace/home/.local/bin), and the root entrypoint chowns the volume and
drops to the uid-1000 `lazyaf` user - so a tool installed by step 1 is
runnable BY NAME in step 2, a DIFFERENT container.

Runs through the full real dispatch stack (PipelineExecutor ->
WorkspaceService named volume -> LocalExecutor real containers -> StepRun
rows), the same seams as test_pipeline_local_execution.py.

Mode note: these steps set `control: false` (the documented per-step debug
escape hatch for labeled images). This environment has no backend serving
/api/steps/* for the test's own DB, so control-mode POSTs could not land;
stdout mode keeps the log assertions real while ALSO covering the escape
hatch and the entrypoint's stock-degradation path (LAZYAF_CONTROL unset ->
`exec gosu lazyaf "$@"` CMD passthrough). The control-mode round trip is
covered by the steps-router tests and the slow e2e (tdd/e2e/
test_control_layer.py) against a live backend.

Skip policy (R4): Docker being down fails LOUDLY (never baselined). Only the
image being unbuilt skips, with the "12.3-images:" reason - a skip that is
UNREACHABLE under the CI gate because `scripts/run_tier.py T2` preflights
`python scripts/build_images.py --check` and exits loudly first (the reason
is deliberately NOT in tdd/skip_baseline.json: if it ever fires under the
gate, that is a wiring bug and must fail). Build via
`python scripts/build_images.py` (or `scripts/test.sh images`).
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import docker as docker_sdk
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import Base
from app.models import Pipeline, PipelineRun, Repo
from app.models.pipeline import RunStatus
from app.services.pipeline_executor import PipelineExecutor
from app.services.workspace.state_machine import generate_volume_name
import app.services.workspace_service as workspace_service_module

pytestmark = [pytest.mark.integration, pytest.mark.local_exec]

CONTROL_IMAGE = "lazyaf-base:dev"
CONTROL_LAYER_LABEL = "lazyaf.control-layer"
CONTENT_HASH_LABEL = "lazyaf.content-hash"
BUILD_HINT = (
    "run `python scripts/build_images.py` (or `scripts/test.sh images`)"
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

# docker_client comes from the shared tdd/integration/conftest.py (from_env
# + ping: Docker down fails loudly there, R4).


@pytest.fixture(scope="module", autouse=True)
def control_image(docker_client):
    """The REAL locally-built base image - or a host-dev-only skip.

    :dev tags are built locally, never pulled (no phantom :latest). Absence
    means the build script has not run on this daemon. In CI this skip is
    unreachable: run_tier.py T2 preflights `build_images.py --check` and
    fails loudly first, so the reason is intentionally NOT baselined.
    """
    try:
        return docker_client.images.get(CONTROL_IMAGE)
    except docker_sdk.errors.ImageNotFound:
        pytest.skip(
            f"12.3-images: {CONTROL_IMAGE} not built on this daemon - "
            f"{BUILD_HINT}"
        )


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch, docker_client):
    """Real-seam environment: file DB, fresh executor, stubbed population.

    Same shape as test_pipeline_local_execution.py: the named volume
    lifecycle is fully real; only the git clone (which needs a live backend
    git server) is stubbed.
    """
    db_path = (tmp_path / "home_persist.db").as_posix()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _fake_populate(volume_name, repo_id, branch, commit_sha, **kwargs):
        return None

    monkeypatch.setattr(workspace_service_module, "populate_workspace", _fake_populate)

    executor = PipelineExecutor()
    run_ids: list[str] = []

    yield SimpleNamespace(
        factory=factory,
        executor=executor,
        run_ids=run_ids,
        docker=docker_client,
    )

    # Defensive: remove any volume a failing test leaked.
    for run_id in run_ids:
        try:
            docker_client.volumes.get(generate_volume_name(run_id)).remove(force=True)
        except docker_sdk.errors.NotFound:
            pass
    await engine.dispose()


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def control_step(name: str, command: str, **extra) -> dict:
    """A script step on the real base image, forced to stdout mode (see
    module docstring for why `control: false` here)."""
    step = {
        "name": name,
        "type": "script",
        "config": {
            "command": command,
            "image": CONTROL_IMAGE,
            "control": False,
        },
    }
    step.update(extra)
    return step


async def make_repo_and_pipeline(factory, steps: list[dict]):
    async with factory() as db:
        repo = Repo(
            id=str(uuid4()),
            name="home-persist-repo",
            default_branch="main",
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=repo.id,
            name="home-persist-pipeline",
            steps=json.dumps(steps),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        await db.refresh(repo)
        await db.refresh(pipeline)
        return repo, pipeline


async def run_pipeline(env, steps: list[dict]) -> PipelineRun:
    repo, pipeline = await make_repo_and_pipeline(env.factory, steps)
    async with env.factory() as db:
        run = await env.executor.start_pipeline(db=db, pipeline=pipeline, repo=repo)
        run_id = run.id
    env.run_ids.append(run_id)
    await env.executor.wait_for_run(run_id)
    async with env.factory() as db:
        result = await db.execute(
            select(PipelineRun)
            .where(PipelineRun.id == run_id)
            .options(selectinload(PipelineRun.step_runs))
        )
        return result.scalar_one()


def logs_of(run: PipelineRun, step_index: int) -> str:
    by_index = {sr.step_index: sr for sr in run.step_runs}
    return by_index[step_index].logs or ""


# -----------------------------------------------------------------------------
# Image contract (the cheap docker-inspect assertions, design section 4)
# -----------------------------------------------------------------------------

class TestBaseImageContract:
    def test_image_declares_control_layer_capability_label(self, control_image):
        """Mode selection is an EXPLICIT image-author declaration (R6): the
        base image carries LABEL lazyaf.control-layer=1."""
        labels = control_image.labels or {}
        assert labels.get(CONTROL_LAYER_LABEL) == "1", (
            f"{CONTROL_IMAGE} must declare LABEL {CONTROL_LAYER_LABEL}=1 "
            f"(got labels: {labels}) - stale image? {BUILD_HINT}"
        )

    def test_image_carries_content_hash_label(self, control_image):
        """build_images.py stamps lazyaf.content-hash for staleness checks."""
        labels = control_image.labels or {}
        assert labels.get(CONTENT_HASH_LABEL), (
            f"{CONTROL_IMAGE} must carry LABEL {CONTENT_HASH_LABEL} "
            f"(got labels: {labels}) - built outside build_images.py? "
            f"{BUILD_HINT}"
        )


# -----------------------------------------------------------------------------
# The HOME persistence contract (PLAN 12.3)
# -----------------------------------------------------------------------------

class TestHomePersistenceContract:
    async def test_baked_env_and_tool_persist_to_second_container(self, env):
        """Step 1 verifies the baked env contract and installs a tool into
        $HOME/.local/bin; step 2 - a DIFFERENT container on the same named
        volume - runs it BY BARE NAME (baked PATH, no exports)."""
        run = await run_pipeline(env, [
            control_step(
                "Verify env + install tool",
                'echo "HOME=$HOME"\n'
                'test "$HOME" = "/workspace/home"\n'
                'test "$(id -u)" = "1000"\n'
                'test "$PIP_USER" = "1"\n'
                'test "$PYTHONUSERBASE" = "/workspace/home/.local"\n'
                'case ":$PATH:" in\n'
                '  *":/workspace/home/.local/bin:"*) echo baked-path-ok ;;\n'
                '  *) echo "PATH is missing /workspace/home/.local/bin: $PATH"; exit 1 ;;\n'
                'esac\n'
                'mkdir -p "$HOME/.local/bin"\n'
                'printf \'#!/bin/sh\\necho tool-from-step-one\\n\' > "$HOME/.local/bin/lazyaf-proof"\n'
                'chmod +x "$HOME/.local/bin/lazyaf-proof"\n'
                'echo step-one-done',
                continue_in_context=True,
            ),
            control_step(
                "Run tool by name in fresh container",
                'lazyaf-proof\n'
                'echo step-two-done',
            ),
        ])

        assert run.status == RunStatus.PASSED.value, (
            f"pipeline failed; step logs: "
            f"{[(sr.step_index, sr.error, sr.logs) for sr in run.step_runs]}"
        )
        assert run.steps_completed == 2
        for sr in run.step_runs:
            assert sr.executor == "local"
            assert sr.status == RunStatus.PASSED.value

        step1 = logs_of(run, 0)
        assert "HOME=/workspace/home" in step1
        assert "baked-path-ok" in step1
        assert "step-one-done" in step1

        # The proof: the tool written by container 1 executed by bare name
        # in container 2, through the named volume + baked PATH.
        step2 = logs_of(run, 1)
        assert "tool-from-step-one" in step2
        assert "step-two-done" in step2

    @pytest.mark.slow
    async def test_pip_user_install_persists_to_second_container(self, env):
        """The real-tool variant (design section 5): `pip install --user`
        lands in /workspace/home/.local via the baked PIP_USER/PYTHONUSERBASE
        and the package imports and runs in the next step's container.
        Needs network to PyPI; marked slow, still executed by tier T2."""
        run = await run_pipeline(env, [
            control_step(
                "pip install --user cowsay",
                'pip install --user cowsay\n'
                'test -d /workspace/home/.local/lib\n'
                'echo install-landed-in-home',
                continue_in_context=True,
            ),
            control_step(
                "Use cowsay in fresh container",
                "python -m cowsay -t 'cross-step-persistence-proof'\n"
                "python -c \"import cowsay; print('cowsay-import-ok')\"",
            ),
        ])

        assert run.status == RunStatus.PASSED.value, (
            f"pipeline failed; step logs: "
            f"{[(sr.step_index, sr.error, sr.logs) for sr in run.step_runs]}"
        )
        assert run.steps_completed == 2
        assert "install-landed-in-home" in logs_of(run, 0)
        step2 = logs_of(run, 1)
        assert "cross-step-persistence-proof" in step2
        assert "cowsay-import-ok" in step2
