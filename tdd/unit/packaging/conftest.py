"""
Shared helpers for the packaging tests.

Two tiers of assertion live in this directory:

  * test_wheel_metadata.py - FAST, no build. Reads cli/pyproject.toml and
    asserts the declarations that *determine* what a wheel will contain.
    Runs in T1 on every push.
  * test_wheel_build.py    - @slow. Actually builds a wheel and an sdist in a
    throwaway copy of cli/ and inspects the archives. Marked slow because a
    real PEP 517 build takes seconds, so it runs outside the tiered lanes
    (`pytest ../tdd -m slow`, and in the release workflow).

Nothing here touches the repo working tree: the build fixture copies cli/ into
tmp_path first, which doubles as proof that the package builds WITHOUT the
surrounding monorepo (no `../LICENSE`, no root pyproject, no workspace).
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

try:  # Python 3.11+ (containers run 3.12)
    import tomllib
except ModuleNotFoundError:  # Python 3.10 host
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_DIR = REPO_ROOT / "cli"
PYPROJECT = CLI_DIR / "pyproject.toml"
INIT_PY = CLI_DIR / "lazyaf" / "__init__.py"

DIST_NAME = "lazyaf-cli"
# Wheel/sdist filenames normalize '-' to '_' (PEP 427/625).
DIST_FILENAME_STEM = "lazyaf_cli"
IMPORT_NAME = "lazyaf"
CONSOLE_SCRIPT = "lazyaf"

# The EXACT build command CI runs. Kept here so the test and the release
# workflow cannot drift: uv drives the PEP 517 backend in an isolated env.
BUILD_ARGV = ["uv", "build", "--out-dir", "<outdir>", "<projectdir>"]


def read_pyproject() -> dict:
    """Parse cli/pyproject.toml."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def declared_version() -> str:
    """The single source of truth: __version__ in cli/lazyaf/__init__.py."""
    for line in INIT_PY.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise AssertionError(f"no __version__ assignment found in {INIT_PY}")


def build_distributions(outdir: Path) -> tuple[Path, Path]:
    """Build a wheel + sdist from a pristine COPY of cli/ and return them.

    Copying first keeps the repo clean (setuptools drops build/ and
    *.egg-info/ next to the project) and proves the package is
    self-contained.
    """
    project = outdir / "src"
    shutil.copytree(
        CLI_DIR,
        project,
        ignore=shutil.ignore_patterns("__pycache__", "*.egg-info", "build", "dist"),
    )
    dist = outdir / "dist"

    if shutil.which("uv"):
        argv = ["uv", "build", "--out-dir", str(dist), str(project)]
    else:  # pragma: no cover - fallback for uv-less environments
        try:
            import build  # noqa: F401
        except ModuleNotFoundError:
            pytest.skip("packaging-build: no build frontend (neither `uv` nor `build`)")
        argv = [sys.executable, "-m", "build", "--outdir", str(dist), str(project)]

    proc = subprocess.run(argv, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"build failed ({' '.join(argv)}):\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )

    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {wheels}"
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"
    return wheels[0], sdists[0]


@pytest.fixture(scope="session")
def built_dists(tmp_path_factory) -> tuple[Path, Path]:
    """Session-scoped (wheel, sdist) pair - built once for the whole module."""
    return build_distributions(tmp_path_factory.mktemp("lazyaf_cli_build"))
