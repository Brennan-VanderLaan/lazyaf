"""
FAST packaging guards for the publishable `lazyaf-cli` wheel (no build).

These run in T1 on every push. They assert the DECLARATIONS that decide what
a wheel will contain and what PyPI will show, so a regression is caught at
unit speed rather than at publish time:

  * the version has exactly one source of truth (cli/lazyaf/__init__.py)
  * the metadata a public index needs is present and non-placeholder
  * the console script points at the real entry point
  * dependencies are bounded on both ends
  * the package list is EXPLICIT, so the repo can never be swept into a wheel
  * nothing secret-shaped sits in the directory that gets packaged

The build-and-inspect-the-real-archive half lives in test_wheel_build.py
(@slow, because a PEP 517 build takes seconds).
"""
import re

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

from .conftest import (
    CLI_DIR,
    CONSOLE_SCRIPT,
    DIST_NAME,
    IMPORT_NAME,
    declared_version,
    read_pyproject,
)

# Value-shaped credential patterns. Deliberately matches LIVE key SHAPES, not
# variable names: code that redacts or documents ANTHROPIC_API_KEY is fine,
# code carrying an actual key is not.
SECRET_VALUE_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{24,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


class TestVersionSingleSource:
    def test_version_is_dynamic_from_the_package(self):
        """pyproject must NOT restate the version; it reads __init__.py."""
        cfg = read_pyproject()
        assert "version" not in cfg["project"], (
            "cli/pyproject.toml declares a literal version. The version has one "
            "source of truth (cli/lazyaf/__init__.py) and pyproject must read it "
            'via dynamic = ["version"].'
        )
        assert cfg["project"]["dynamic"] == ["version"]
        assert cfg["tool"]["setuptools"]["dynamic"]["version"] == {
            "attr": f"{IMPORT_NAME}.__version__"
        }

    def test_declared_version_is_pep440(self):
        Version(declared_version())  # raises InvalidVersion if malformed


class TestPublishableMetadata:
    def test_core_fields_present(self):
        project = read_pyproject()["project"]
        assert project["name"] == DIST_NAME
        assert len(project["description"]) > 20
        assert project["readme"] == "README.md"
        assert project["requires-python"] == ">=3.10"
        assert project["authors"]
        assert project["keywords"]

    def test_license_is_an_spdx_expression_with_the_file(self):
        project = read_pyproject()["project"]
        assert project["license"] == "MIT"
        assert project["license-files"] == ["LICENSE"]
        assert (CLI_DIR / "LICENSE").is_file(), (
            "cli/LICENSE is missing. license-files is resolved relative to the "
            "project dir, so the wheel needs its own copy - the repo-root "
            "LICENSE is not reachable from a build of cli/ alone."
        )

    def test_no_license_classifier(self):
        """PEP 639 forbids mixing a license expression with license
        classifiers; setuptools>=77 hard-errors on the combination."""
        classifiers = read_pyproject()["project"]["classifiers"]
        assert not [c for c in classifiers if c.startswith("License ::")]

    def test_build_backend_supports_pep639(self):
        cfg = read_pyproject()["build-system"]
        assert cfg["build-backend"] == "setuptools.build_meta"
        (req,) = [Requirement(r) for r in cfg["requires"]]
        assert req.name == "setuptools"
        assert req.specifier.contains("77.0.3"), (
            "PEP 639 license metadata needs setuptools>=77; keep the floor in "
            "step with the license/license-files keys."
        )

    def test_urls_point_at_the_public_repo(self):
        urls = read_pyproject()["project"]["urls"]
        assert {"Homepage", "Repository", "Issues"} <= set(urls)
        for name, url in urls.items():
            assert url.startswith("https://github.com/Brennan-VanderLaan/lazyaf"), (
                f"{name} does not point at the public repo: {url}"
            )

    def test_readme_exists_and_is_substantial(self):
        readme = CLI_DIR / "README.md"
        assert readme.is_file(), "cli/README.md is the PyPI long description"
        text = readme.read_text(encoding="utf-8")
        assert len(text) > 500
        assert "pip install lazyaf-cli" in text

    def test_readme_has_no_repo_relative_links(self):
        """Relative links 404 on PyPI - the long description has no repo
        around it."""
        text = (CLI_DIR / "README.md").read_text(encoding="utf-8")
        bad = [
            target
            for target in re.findall(r"\]\(([^)]+)\)", text)
            if not target.startswith(("http://", "https://", "#"))
        ]
        assert bad == [], f"relative links break on PyPI: {bad}"


class TestEntryPoint:
    def test_console_script(self):
        scripts = read_pyproject()["project"]["scripts"]
        assert scripts == {CONSOLE_SCRIPT: f"{IMPORT_NAME}.cli:cli"}

    def test_entry_point_target_exists(self):
        """The target module and the `cli` group it names are real."""
        source = (CLI_DIR / IMPORT_NAME / "cli.py").read_text(encoding="utf-8")
        assert re.search(r"^def cli\(", source, re.MULTILINE), (
            "cli/lazyaf/cli.py no longer defines `cli` - the console script "
            "entry point lazyaf = lazyaf.cli:cli would fail at install time."
        )


class TestDependencies:
    def test_every_dependency_is_bounded_on_both_ends(self):
        deps = [Requirement(d) for d in read_pyproject()["project"]["dependencies"]]
        assert {d.name for d in deps} == {"click", "httpx", "rich"}, (
            "The CLI's dependency set changed. Adding a dependency to a "
            "published wheel is a distribution decision - update this test "
            "deliberately."
        )
        for dep in deps:
            ops = {spec.operator for spec in dep.specifier}
            assert ">=" in ops, f"{dep.name} has no lower bound"
            assert ops & {"<", "<=", "=="}, (
                f"{dep.name} has no upper bound; an unpinned major can break "
                f"installs of an already-published wheel"
            )

    def test_no_dependency_on_the_monorepo(self):
        """The wheel must install from PyPI alone - no path/git deps, no
        backend, no runner-common."""
        raw = read_pyproject()["project"]["dependencies"]
        for dep in raw:
            assert "@" not in dep, f"direct-reference dependency is not publishable: {dep}"
            assert "lazyaf" not in dep.lower(), f"intra-repo dependency: {dep}"


class TestPackagesAreExplicit:
    def test_only_the_lazyaf_package_ships(self):
        setuptools_cfg = read_pyproject()["tool"]["setuptools"]
        assert setuptools_cfg["packages"] == [IMPORT_NAME], (
            "Package discovery must stay EXPLICIT. Auto-discovery from a "
            "monorepo subdirectory is how tests and stray modules end up in a "
            "published wheel."
        )
        assert setuptools_cfg["include-package-data"] is False

    def test_no_setuptools_find_directives(self):
        """`[tool.setuptools.packages.find]` parses as packages = {find = ...};
        a list is the only shape that means "these, and only these"."""
        packages = read_pyproject()["tool"]["setuptools"]["packages"]
        assert isinstance(packages, list), (
            f"auto-discovery is back on: packages = {packages!r}"
        )


class TestPackagedTreeIsClean:
    """cli/ is the entire build context. Whatever sits here can reach a wheel."""

    def packaged_files(self):
        return [
            p
            for p in CLI_DIR.rglob("*")
            if p.is_file()
            and "__pycache__" not in p.parts
            and ".egg-info" not in str(p)
            and "dist" not in p.relative_to(CLI_DIR).parts
            and "build" not in p.relative_to(CLI_DIR).parts
            # A local virtualenv is not the build context and is gitignored,
            # but it DOES sit under cli/ the moment anyone runs `uv run` or
            # `python -m venv` there - and site-packages is full of `test_*.py`
            # and `.env`-shaped files, so without this line a developer's own
            # tooling turns T1 red for reasons that have nothing to do with
            # what ships. (Observed: `cd cli && uv run ...` failed
            # test_no_env_or_credential_files and test_no_tests_in_the_build_context.)
            and ".venv" not in p.relative_to(CLI_DIR).parts
            and "venv" not in p.relative_to(CLI_DIR).parts
            and "node_modules" not in p.relative_to(CLI_DIR).parts
        ]

    def test_no_env_or_credential_files(self):
        forbidden = {".env", ".env.local", ".npmrc", ".pypirc", "credentials", "id_rsa"}
        forbidden_suffixes = {".pem", ".key", ".p12", ".pfx"}
        offenders = [
            str(p.relative_to(CLI_DIR))
            for p in self.packaged_files()
            if p.name in forbidden or p.suffix in forbidden_suffixes or p.name.startswith(".env")
        ]
        assert offenders == [], f"credential-shaped files inside the build context: {offenders}"

    def test_no_tests_in_the_build_context(self):
        offenders = [
            str(p.relative_to(CLI_DIR))
            for p in self.packaged_files()
            if p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"
        ]
        assert offenders == [], (
            f"tests live in tdd/, never inside the packaged CLI: {offenders}"
        )

    @pytest.mark.parametrize("pattern", SECRET_VALUE_PATTERNS, ids=lambda p: p.pattern[:24])
    def test_no_live_key_shaped_strings(self, pattern):
        """Nothing that ships may CARRY a credential.

        Shape-based on purpose: mentioning ANTHROPIC_API_KEY (to redact it, to
        document it) is fine; embedding a value that looks like a live key is
        not. AI provider keys belong to the server's environment and must
        never reach a public wheel.
        """
        offenders = []
        for path in self.packaged_files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if pattern.search(text):
                offenders.append(str(path.relative_to(CLI_DIR)))
        assert offenders == [], f"credential-shaped value in packaged files: {offenders}"
