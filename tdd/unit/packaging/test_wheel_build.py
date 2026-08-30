"""
SLOW packaging proof: build the real wheel/sdist and inspect the archives.

Marked @slow because a PEP 517 build (plus, for the install test, a fresh
virtualenv) takes seconds - so these run outside the tiered lanes:

    cd backend && uv run pytest ../tdd/unit/packaging -m slow

The release workflow runs this same selection before publishing. The fast
declaration guards that run on EVERY push live in test_wheel_metadata.py.

What is proven here:
  * the wheel contains the lazyaf package and NOTHING else - no tests, no
    backend source, no .env, no monorepo
  * its metadata carries the version from cli/lazyaf/__init__.py
  * `lazyaf --help` and `lazyaf --version` work from a clean venv with the
    wheel as the only thing installed
"""
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from .conftest import (
    DIST_FILENAME_STEM,
    IMPORT_NAME,
    declared_version,
)

pytestmark = pytest.mark.slow

# Anything matching these must never appear in a published artifact.
FORBIDDEN_PATH_PATTERNS = [
    re.compile(r"(^|/)\.env"),
    re.compile(r"(^|/)tdd/"),
    re.compile(r"(^|/)backend/"),
    re.compile(r"(^|/)frontend/"),
    re.compile(r"(^|/)images/"),
    re.compile(r"(^|/)runner[-_]"),
    re.compile(r"(^|/)scripts/"),
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]*\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.pyc$"),
    re.compile(r"(^|/)__pycache__/"),
    re.compile(r"(^|/)\.git"),
    re.compile(r"\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)docker-compose"),
]


def assert_no_forbidden_paths(names: list[str], what: str):
    offenders = [
        n for n in names for pat in FORBIDDEN_PATH_PATTERNS if pat.search(n)
    ]
    assert offenders == [], f"{what} leaks repo content: {sorted(set(offenders))}"


class TestWheelContents:
    def test_wheel_contains_exactly_the_package_and_metadata(self, built_dists):
        wheel, _ = built_dists
        version = declared_version()
        distinfo = f"{DIST_FILENAME_STEM}-{version}.dist-info"

        names = set(zipfile.ZipFile(wheel).namelist())
        expected = {
            f"{IMPORT_NAME}/__init__.py",
            f"{IMPORT_NAME}/cli.py",
            f"{distinfo}/METADATA",
            f"{distinfo}/WHEEL",
            f"{distinfo}/RECORD",
            f"{distinfo}/entry_points.txt",
            f"{distinfo}/top_level.txt",
            f"{distinfo}/licenses/LICENSE",
        }
        assert names == expected, (
            "wheel contents changed.\n"
            f"  unexpected: {sorted(names - expected)}\n"
            f"  missing:    {sorted(expected - names)}\n"
            "If this is intentional (a new module, new package data), update "
            "this list deliberately - it is the guard that keeps the repo out "
            "of a public artifact."
        )

    def test_wheel_is_pure_python_and_named_for_the_declared_version(self, built_dists):
        wheel, _ = built_dists
        assert wheel.name == f"{DIST_FILENAME_STEM}-{declared_version()}-py3-none-any.whl"

    def test_no_repo_content_in_the_wheel(self, built_dists):
        wheel, _ = built_dists
        assert_no_forbidden_paths(zipfile.ZipFile(wheel).namelist(), "wheel")

    def test_wheel_metadata_matches_the_single_source_of_truth(self, built_dists):
        wheel, _ = built_dists
        distinfo = f"{DIST_FILENAME_STEM}-{declared_version()}.dist-info"
        raw = zipfile.ZipFile(wheel).read(f"{distinfo}/METADATA").decode("utf-8")
        headers = raw.split("\n\n", 1)[0]

        assert f"Version: {declared_version()}" in headers
        assert "Name: lazyaf-cli" in headers
        assert "License-Expression: MIT" in headers
        assert "License-File: LICENSE" in headers
        assert "Requires-Python: >=3.10" in headers
        assert "Description-Content-Type: text/markdown" in headers
        assert "Project-URL: Homepage, https://github.com/Brennan-VanderLaan/lazyaf" in headers
        for dep in ("click", "httpx", "rich"):
            assert re.search(rf"^Requires-Dist: {dep}", headers, re.MULTILINE), (
                f"{dep} missing from wheel metadata"
            )
        # The long description made it in (PyPI renders this).
        assert "pip install lazyaf-cli" in raw

    def test_console_script_is_declared_in_the_wheel(self, built_dists):
        wheel, _ = built_dists
        distinfo = f"{DIST_FILENAME_STEM}-{declared_version()}.dist-info"
        entry_points = (
            zipfile.ZipFile(wheel).read(f"{distinfo}/entry_points.txt").decode("utf-8")
        )
        assert "[console_scripts]" in entry_points
        assert re.search(r"^lazyaf\s*=\s*lazyaf\.cli:cli$", entry_points, re.MULTILINE)


class TestSdistContents:
    def test_sdist_carries_the_project_only(self, built_dists):
        _, sdist = built_dists
        root = f"{DIST_FILENAME_STEM}-{declared_version()}"
        names = [
            m.name[len(root) + 1 :]
            for m in tarfile.open(sdist).getmembers()
            if m.isfile() and m.name.startswith(root + "/")
        ]
        assert_no_forbidden_paths(names, "sdist")
        assert f"{IMPORT_NAME}/cli.py" in names
        assert "pyproject.toml" in names
        assert "README.md" in names
        assert "LICENSE" in names


class TestFreshVenvInstall:
    """The end-user experience: nothing but the wheel in an empty venv."""

    @pytest.fixture(scope="class")
    def installed_cli(self, built_dists, tmp_path_factory) -> Path:
        wheel, _ = built_dists
        if not shutil.which("uv"):  # pragma: no cover
            pytest.skip("packaging-venv: uv is required to create the throwaway venv")

        venv = tmp_path_factory.mktemp("lazyaf_cli_venv") / "venv"
        subprocess.run(["uv", "venv", str(venv)], check=True, capture_output=True)
        bindir = venv / ("Scripts" if sys.platform == "win32" else "bin")
        python = bindir / ("python.exe" if sys.platform == "win32" else "python")

        proc = subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheel)],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"install failed:\n{proc.stdout}\n{proc.stderr}"

        script = bindir / ("lazyaf.exe" if sys.platform == "win32" else "lazyaf")
        assert script.exists(), f"console script not installed at {script}"
        return script

    def test_help_works(self, installed_cli):
        proc = subprocess.run([str(installed_cli), "--help"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        for command in ("ingest", "land", "list", "branches", "tests"):
            assert command in proc.stdout, f"`{command}` missing from --help"

    def test_version_matches_the_package(self, installed_cli):
        proc = subprocess.run([str(installed_cli), "--version"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert declared_version() in proc.stdout

    def test_the_venv_got_the_cli_and_not_the_repo(self, installed_cli):
        """No backend, no runner-common, no tdd rode in on the wheel."""
        python = installed_cli.parent / ("python.exe" if sys.platform == "win32" else "python")
        probe = (
            "import importlib.util as u, json;"
            "print(json.dumps({m: u.find_spec(m) is not None for m in "
            "['lazyaf', 'app', 'runner_common', 'tdd', 'lazyaf_runner']}))"
        )
        # cwd MUST be neutral: pytest runs from backend/, whose `app/` package
        # would otherwise be importable via the cwd entry on sys.path and read
        # as a leak.
        proc = subprocess.run(
            [str(python), "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(installed_cli.parent.parent),
        )
        assert proc.returncode == 0, proc.stderr
        import json

        found = json.loads(proc.stdout)
        assert found["lazyaf"] is True
        for module in ("app", "runner_common", "tdd", "lazyaf_runner"):
            assert found[module] is False, f"{module} leaked into the wheel's install"
