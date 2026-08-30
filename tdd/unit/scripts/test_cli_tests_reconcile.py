"""
Unit tests for `lazyaf tests reconcile` (cli/lazyaf/cli.py).

WHY THIS FILE EXISTS (12.4 adversarial finding): reconcile ORPHANS every
active TestRef absent from its input. The command used to DEFAULT its input
to a results manifest ($LAZYAF_TEST_RESULTS_PATH, then ./test_results.json)
— i.e. to the list of tests that one tier's run happened to execute. Running
it after a T1 lane silently orphaned every test T1 does not run. There is no
safe default, so the command must now REFUSE ambiguity:

- no source           -> refuse, naming the orphaning hazard
- both sources        -> refuse (--refs and --from-collect are exclusive)
- a results manifest  -> refuse unless --allow-results-manifest is explicit
- an empty declared set -> refuse (it would orphan everything)

`rich` is not installed in the backend test environment (the CLI ships its
own dependency set), so cli.py is imported behind a minimal `rich` stub that
prints plain, UNWRAPPED text to stdout. That does two things: the refusals
are genuinely exercised in the default suite rather than skipped, and the
assertions below match whole phrases that real rich would hard-wrap at the
terminal width. The stub is installed only for the duration of the import
(cli.py binds Console/Panel at import time) and then removed.
"""
import json
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CLI_DIR = REPO_ROOT / "cli"


def _rich_stub() -> dict:
    """Minimal rich stand-in: Console.print writes plain text to stdout."""
    class _Console:
        def __init__(self, *args, **kwargs):
            pass

        def print(self, *args, **kwargs):
            print(" ".join(str(a) for a in args))

    class _Panel(str):
        def __new__(cls, renderable="", *args, **kwargs):
            return super().__new__(cls, str(renderable))

        @classmethod
        def fit(cls, renderable="", *args, **kwargs):
            return cls(renderable)

    rich = types.ModuleType("rich")
    console_mod = types.ModuleType("rich.console")
    console_mod.Console = _Console
    panel_mod = types.ModuleType("rich.panel")
    panel_mod.Panel = _Panel
    rich.console = console_mod
    rich.panel = panel_mod
    return {"rich": rich, "rich.console": console_mod, "rich.panel": panel_mod}


pytest.importorskip("click", reason="cli/ requires click")

if str(CLI_DIR) not in sys.path:
    sys.path.insert(0, str(CLI_DIR))

from click.testing import CliRunner  # noqa: E402

# Import cli.py behind the stub, then put sys.modules back exactly as it was.
_saved = {name: sys.modules.get(name) for name in ("rich", "rich.console", "rich.panel")}
sys.modules.update(_rich_stub())
sys.modules.pop("lazyaf.cli", None)
try:
    from lazyaf.cli import cli  # noqa: E402
finally:
    for _name, _mod in _saved.items():
        if _mod is None:
            sys.modules.pop(_name, None)
        else:
            sys.modules[_name] = _mod


@pytest.fixture
def run():
    runner = CliRunner()

    def _invoke(*args, **kwargs):
        return runner.invoke(cli, ["tests", "reconcile", *args], **kwargs)

    return _invoke


@pytest.fixture
def no_http(monkeypatch):
    """Fail loudly if a refusal path ever reaches the network."""
    import lazyaf.cli as cli_module

    class _Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("reconcile must refuse BEFORE calling the API")

    monkeypatch.setattr(cli_module.httpx, "Client", _Boom)


# -----------------------------------------------------------------------------
# The core refusal: no source, and no default
# -----------------------------------------------------------------------------

class TestRefusesAmbiguousInput:
    def test_no_source_refuses(self, run, no_http):
        result = run("repo-123")
        assert result.exit_code == 1
        assert "Refusing to reconcile" in result.output

    def test_no_source_message_names_the_orphaning_hazard(self, run, no_http):
        output = run("repo-123").output
        assert "orphan" in output.lower()
        # Names BOTH escape hatches so the user can act on the message.
        assert "--from-collect" in output
        assert "--refs" in output

    def test_no_source_does_not_fall_back_to_env_var(
        self, run, no_http, tmp_path, monkeypatch
    ):
        """The old default. LAZYAF_TEST_RESULTS_PATH is a per-STEP path the
        control runtime injects; honoring it here reconciles one step's run
        against the whole repo."""
        manifest = tmp_path / "test_results.json"
        manifest.write_text(
            json.dumps({"version": 1, "results": [{"lazyaf_test_id": "a"}]}),
            encoding="utf-8",
        )
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(manifest))

        result = run("repo-123")

        assert result.exit_code == 1
        assert "Refusing to reconcile" in result.output

    def test_no_source_does_not_fall_back_to_cwd_manifest(
        self, run, no_http, tmp_path, monkeypatch
    ):
        """The other old default: ./test_results.json."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "test_results.json").write_text(
            json.dumps({"version": 1, "results": [{"lazyaf_test_id": "a"}]}),
            encoding="utf-8",
        )

        result = run("repo-123")

        assert result.exit_code == 1
        assert "Refusing to reconcile" in result.output

    def test_both_sources_refuses(self, run, no_http, tmp_path):
        refs = tmp_path / "refs.json"
        refs.write_text(json.dumps({"refs": []}), encoding="utf-8")

        result = run("repo-123", "--refs", str(refs), "--from-collect")

        assert result.exit_code == 1
        assert "mutually exclusive" in result.output


# -----------------------------------------------------------------------------
# A results manifest is not a declared set
# -----------------------------------------------------------------------------

class TestRefusesResultsManifest:
    @pytest.fixture
    def results_manifest(self, tmp_path):
        path = tmp_path / "test_results.json"
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "results": [
                        {
                            "lazyaf_test_id": "us1.ran",
                            "status": "passed",
                            "duration_ms": 3,
                            "file_path": "tdd/unit/test_x.py",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_results_manifest_refused_by_default(
        self, run, no_http, results_manifest
    ):
        result = run("repo-123", "--refs", str(results_manifest))
        assert result.exit_code == 1
        assert "RESULTS manifest" in result.output

    def test_refusal_explains_the_partial_run_problem(
        self, run, no_http, results_manifest
    ):
        output = run("repo-123", "--refs", str(results_manifest)).output
        assert "only the tests that RAN" in output
        assert "--allow-results-manifest" in output

    def test_manifest_alias_is_refused_the_same_way(
        self, run, no_http, results_manifest
    ):
        """--manifest is kept as an alias of --refs; it must not be a way
        back into the old behavior."""
        result = run("repo-123", "--manifest", str(results_manifest))
        assert result.exit_code == 1
        assert "RESULTS manifest" in result.output

    def test_explicit_opt_in_is_accepted(
        self, run, monkeypatch, results_manifest
    ):
        """--allow-results-manifest proceeds, but warns."""
        posted = {}

        import lazyaf.cli as cli_module

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"created": 1, "updated": 0, "orphaned": 0}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, json=None):
                posted["url"] = url
                posted["json"] = json
                return _Response()

        monkeypatch.setattr(cli_module.httpx, "Client", _Client)

        result = run(
            "repo-123", "--refs", str(results_manifest), "--allow-results-manifest"
        )

        assert result.exit_code == 0
        assert "Warning" in result.output
        assert posted["json"]["repo_id"] == "repo-123"
        assert posted["json"]["refs"] == [
            {"lazyaf_test_id": "us1.ran", "file_path": "tdd/unit/test_x.py"}
        ]


# -----------------------------------------------------------------------------
# An empty declared set would orphan everything
# -----------------------------------------------------------------------------

class TestRefusesEmptyDeclaredSet:
    def test_empty_refs_manifest_refused(self, run, no_http, tmp_path):
        refs = tmp_path / "refs.json"
        refs.write_text(json.dumps({"refs": []}), encoding="utf-8")

        result = run("repo-123", "--refs", str(refs))

        assert result.exit_code == 1
        assert "EMPTY" in result.output
        assert "orphan" in result.output.lower()

    def test_missing_manifest_refused(self, run, no_http, tmp_path):
        result = run("repo-123", "--refs", str(tmp_path / "nope.json"))
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_malformed_manifest_refused(self, run, no_http, tmp_path):
        refs = tmp_path / "refs.json"
        refs.write_text('{"totally": "wrong"}', encoding="utf-8")

        result = run("repo-123", "--refs", str(refs))

        assert result.exit_code == 1
        assert "refs" in result.output


# -----------------------------------------------------------------------------
# Manifest classification / normalization
# -----------------------------------------------------------------------------

class TestManifestHelpers:
    def test_classify(self):
        from lazyaf.cli import _classify_manifest

        assert _classify_manifest({"version": 1, "results": []}) == "results"
        assert _classify_manifest({"refs": []}) == "refs"
        assert _classify_manifest([]) == "list"
        assert _classify_manifest({"nope": 1}) == "unknown"
        assert _classify_manifest("string") == "unknown"

    def test_normalize_dedupes_and_keeps_first_path(self):
        from lazyaf.cli import _normalize_refs

        refs = _normalize_refs(
            [
                {"lazyaf_test_id": "a", "file_path": "x.py"},
                {"lazyaf_test_id": "a", "file_path": "y.py"},
                {"lazyaf_test_id": "", "file_path": "z.py"},
                {"no_id": True},
                "not-a-dict",
                {"lazyaf_test_id": "b"},
            ]
        )
        assert refs == [
            {"lazyaf_test_id": "a", "file_path": "x.py"},
            {"lazyaf_test_id": "b", "file_path": None},
        ]

    def test_bare_list_manifest_accepted(self, run, monkeypatch, tmp_path):
        refs = tmp_path / "refs.json"
        refs.write_text(
            json.dumps([{"lazyaf_test_id": "a", "file_path": "t.py"}]),
            encoding="utf-8",
        )

        import lazyaf.cli as cli_module

        sent = {}

        class _Response:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"created": 1, "updated": 0, "orphaned": 0}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, json=None):
                sent.update(json)
                return _Response()

        monkeypatch.setattr(cli_module.httpx, "Client", _Client)

        result = run("repo-123", "--refs", str(refs))

        assert result.exit_code == 0
        assert sent["refs"] == [{"lazyaf_test_id": "a", "file_path": "t.py"}]


# -----------------------------------------------------------------------------
# --from-collect: the full-collection mode
# -----------------------------------------------------------------------------

class TestFromCollect:
    def test_collect_finds_every_marked_test(self, tmp_path):
        """The declared set, not the executed set: an unmarked test is
        excluded and BOTH marked tests appear even though nothing ran."""
        from lazyaf.cli import _collect_refs

        suite = tmp_path / "suite"
        (suite / "tests").mkdir(parents=True)
        (suite / "tests" / "test_declared.py").write_text(
            "import pytest\n"
            "\n"
            "@pytest.mark.lazyaf_test_id('demo.alpha')\n"
            "def test_alpha():\n"
            "    assert True\n"
            "\n"
            "@pytest.mark.lazyaf_test_id('demo.beta')\n"
            "def test_beta():\n"
            "    raise AssertionError('never runs under --collect-only')\n"
            "\n"
            "def test_unmarked():\n"
            "    assert True\n",
            encoding="utf-8",
        )

        refs = _collect_refs(suite, ())

        assert [r["lazyaf_test_id"] for r in refs] == ["demo.alpha", "demo.beta"]
        assert all(r["file_path"] == "tests/test_declared.py" for r in refs)

    def test_collection_error_refuses(self, tmp_path):
        """A partial collection is the exact ambiguity this mode avoids."""
        from lazyaf.cli import _collect_refs

        suite = tmp_path / "suite"
        (suite / "tests").mkdir(parents=True)
        (suite / "tests" / "test_broken.py").write_text(
            "import nonexistent_module_xyz\n", encoding="utf-8"
        )

        with pytest.raises(SystemExit) as exc:
            _collect_refs(suite, ())
        assert exc.value.code == 1
