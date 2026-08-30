"""
Tests for the pytest-lazyaf manifest plugin (runner_common/pytest_lazyaf.py).

Exercised through pytest's own ``pytester`` fixture: each test spins up a
real inner pytest session with the plugin loaded the way production loads it
(explicit ``-p runner_common.pytest_lazyaf`` — there is deliberately no entry
point, see the plugin docstring), and asserts on the manifest file the inner
session leaves behind.

Contract under test (pinned 12.2.6 contract #1/#5):
- manifest: {"version": 1, "results": [{"lazyaf_test_id", "status",
  "duration_ms", "file_path"}]}
- statuses limited to passed/failed/skipped (errors map to failed)
- marker-annotated tests ONLY; unset env = pure no-op (marker still
  registered, so --strict-markers runs stay green)
"""
import json

# Enables the `pytester` fixture. Declared in the test MODULE (allowed) —
# a non-rootdir conftest.py may not declare pytest_plugins.
pytest_plugins = ["pytester"]

PLUGIN_ARGS = ("-p", "runner_common.pytest_lazyaf")


def _read_manifest(path):
    with open(path) as fh:
        return json.load(fh)


def _by_id(manifest):
    return {r["lazyaf_test_id"]: r for r in manifest["results"]}


class TestManifestEmission:
    def test_outcomes_collected_for_marked_tests(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_sample="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.passes")
            def test_passes():
                assert True

            @pytest.mark.lazyaf_test_id("demo.fails")
            def test_fails():
                assert False

            @pytest.mark.lazyaf_test_id("demo.skips")
            @pytest.mark.skip(reason="not today")
            def test_skips():
                pass
            """
        )
        result = pytester.runpytest(*PLUGIN_ARGS)
        result.assert_outcomes(passed=1, failed=1, skipped=1)

        manifest = _read_manifest(out)
        assert manifest["version"] == 1
        results = _by_id(manifest)
        assert results["demo.passes"]["status"] == "passed"
        assert results["demo.fails"]["status"] == "failed"
        assert results["demo.skips"]["status"] == "skipped"
        assert len(manifest["results"]) == 3

    def test_duration_and_file_path_populated(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_timed="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.timed")
            def test_timed():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=1)

        entry = _by_id(_read_manifest(out))["demo.timed"]
        assert isinstance(entry["duration_ms"], int)
        assert entry["duration_ms"] >= 0
        # Repo-root-relative (contract #3); the pytester tmp tree has no
        # .git of its own, so only the tail is pinned here — the exact
        # repo-root shape is pinned in TestRepoRootRelativeFilePath.
        assert entry["file_path"].endswith("test_timed.py")

    def test_skipped_test_has_null_duration(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_skip="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.skipped")
            @pytest.mark.skip(reason="nope")
            def test_skipped():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(skipped=1)

        entry = _by_id(_read_manifest(out))["demo.skipped"]
        assert entry["status"] == "skipped"
        assert entry["duration_ms"] is None

    def test_unannotated_tests_are_not_recorded(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_mixed="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.only_me")
            def test_marked():
                pass

            def test_unmarked():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=2)

        manifest = _read_manifest(out)
        assert [r["lazyaf_test_id"] for r in manifest["results"]] == ["demo.only_me"]

    def test_setup_error_maps_to_failed(self, pytester, monkeypatch):
        """Contract #1 has no 'error' status: an errored test is 'failed'."""
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_err="""
            import pytest

            @pytest.fixture
            def broken():
                raise RuntimeError("setup boom")

            @pytest.mark.lazyaf_test_id("demo.errors")
            def test_errors(broken):
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(errors=1)

        assert _by_id(_read_manifest(out))["demo.errors"]["status"] == "failed"

    def test_teardown_error_overrides_passed(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_td="""
            import pytest

            @pytest.fixture
            def leaky():
                yield
                raise RuntimeError("teardown boom")

            @pytest.mark.lazyaf_test_id("demo.teardown")
            def test_teardown(leaky):
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS)

        assert _by_id(_read_manifest(out))["demo.teardown"]["status"] == "failed"

    def test_class_level_marker_applies(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_cls="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.class_level")
            class TestGroup:
                def test_one(self):
                    pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=1)

        assert _by_id(_read_manifest(out))["demo.class_level"]["status"] == "passed"

    def test_no_marked_tests_writes_no_manifest(self, pytester, monkeypatch):
        """An unannotated run leaves NO file — the control runtime only POSTs
        when the file exists, so unannotated tiers cost nothing."""
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_plain="""
            def test_plain():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=1)

        assert not out.exists()

    def test_manifest_directory_is_created(self, pytester, monkeypatch):
        out = pytester.path / "deep" / "nested" / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_deep="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.deep")
            def test_deep():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=1)

        assert _read_manifest(out)["results"][0]["lazyaf_test_id"] == "demo.deep"


class TestNoOpWithoutEnv:
    def test_unset_env_writes_nothing_and_stays_green(self, pytester, monkeypatch):
        monkeypatch.delenv("LAZYAF_TEST_RESULTS_PATH", raising=False)
        pytester.makepyfile(
            test_noop="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.noop")
            def test_noop():
                pass
            """
        )
        result = pytester.runpytest(*PLUGIN_ARGS)
        result.assert_outcomes(passed=1)

        leftovers = [
            p for p in pytester.path.iterdir() if p.suffix in (".json", ".tmp")
        ]
        assert leftovers == []

    def test_marker_registered_under_strict_markers(self, pytester, monkeypatch):
        """--strict-markers errors on unregistered markers; the plugin's
        registration must make annotated collection green even with the env
        unset (contract #5)."""
        monkeypatch.delenv("LAZYAF_TEST_RESULTS_PATH", raising=False)
        pytester.makepyfile(
            test_strict="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.strict")
            def test_strict():
                pass
            """
        )
        result = pytester.runpytest(*PLUGIN_ARGS, "--strict-markers")
        result.assert_outcomes(passed=1)


class TestMarkerEdgeCases:
    def test_marker_without_args_is_ignored(self, pytester, monkeypatch):
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_noargs="""
            import pytest

            @pytest.mark.lazyaf_test_id
            def test_noargs():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS).assert_outcomes(passed=1)

        assert not out.exists()

    def test_manifest_is_valid_json_shape(self, pytester, monkeypatch):
        """Full contract-#1 shape pin: exactly the four documented keys."""
        out = pytester.path / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_shape="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.shape")
            def test_shape():
                pass
            """
        )
        pytester.runpytest(*PLUGIN_ARGS)

        manifest = _read_manifest(out)
        assert set(manifest.keys()) == {"version", "results"}
        entry = manifest["results"][0]
        assert set(entry.keys()) == {
            "lazyaf_test_id",
            "status",
            "duration_ms",
            "file_path",
        }


class TestRepoRootRelativeFilePath:
    """Contract #3: file_path is REPO-ROOT-relative, not rootdir-relative.

    The production shape this exists for: LazyAF's own suite runs as
    ``cd backend && pytest ../tdd`` with tdd/pytest.ini, so pytest's rootdir
    is ``tdd/`` and ``item.location[0]`` would emit ``unit/x/test_y.py`` —
    while the CLI/MCP seeds, the TestRef rows and every human reading them
    speak ``tdd/unit/x/test_y.py``.
    """

    @staticmethod
    def _make_repo_layout(pytester, marker_git=True):
        """A real rootdir!=repo-root layout: repo/.git + repo/tdd/pytest.ini."""
        repo = pytester.path
        if marker_git:
            (repo / ".git").mkdir()
        pkg = repo / "tdd" / "unit" / "control"
        pkg.mkdir(parents=True)
        (repo / "tdd" / "pytest.ini").write_text("[pytest]\n")
        (pkg / "test_deep.py").write_text(
            "import pytest\n\n"
            '@pytest.mark.lazyaf_test_id("demo.repo_rel")\n'
            "def test_deep():\n"
            "    pass\n"
        )
        return repo

    def test_file_path_is_relative_to_the_git_repo_root(
        self, pytester, monkeypatch
    ):
        repo = self._make_repo_layout(pytester)
        out = repo / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.delenv("LAZYAF_REPO_ROOT", raising=False)

        result = pytester.runpytest("tdd", *PLUGIN_ARGS)
        result.assert_outcomes(passed=1)
        # Prove the layout really reproduces the rootdir!=repo-root shape.
        result.stdout.fnmatch_lines(["rootdir: *tdd"])

        entry = _by_id(_read_manifest(out))["demo.repo_rel"]
        assert entry["file_path"] == "tdd/unit/control/test_deep.py"

    def test_env_override_wins_over_the_git_walk(self, pytester, monkeypatch):
        """LAZYAF_REPO_ROOT is the documented escape hatch for checkouts
        with no .git (exported tarballs)."""
        repo = self._make_repo_layout(pytester, marker_git=False)
        out = repo / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.setenv("LAZYAF_REPO_ROOT", str(repo))

        pytester.runpytest("tdd", *PLUGIN_ARGS).assert_outcomes(passed=1)

        entry = _by_id(_read_manifest(out))["demo.repo_rel"]
        assert entry["file_path"] == "tdd/unit/control/test_deep.py"

    def test_falls_back_to_rootdir_relative_when_root_is_unrelated(
        self, pytester, monkeypatch
    ):
        """A worse path beats no path: when the resolved root does not
        contain the test file, pytest's own relative location is emitted."""
        repo = self._make_repo_layout(pytester, marker_git=False)
        elsewhere = repo / "not-the-repo"
        elsewhere.mkdir()
        out = repo / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.setenv("LAZYAF_REPO_ROOT", str(elsewhere))

        pytester.runpytest("tdd", *PLUGIN_ARGS).assert_outcomes(passed=1)

        entry = _by_id(_read_manifest(out))["demo.repo_rel"]
        assert entry["file_path"] == "unit/control/test_deep.py"

    def test_file_path_never_contains_backslashes(self, pytester, monkeypatch):
        repo = self._make_repo_layout(pytester)
        out = repo / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        monkeypatch.delenv("LAZYAF_REPO_ROOT", raising=False)

        pytester.runpytest("tdd", *PLUGIN_ARGS)

        for entry in _read_manifest(out)["results"]:
            assert "\\" not in entry["file_path"]


class TestManifestWriteNeverFailsTheRun:
    """Telemetry must never turn a green suite red (producer side of the
    same never-crash rule the control runtime enforces on the consumer
    side): every write failure is a stderr line, not an exception out of
    pytest_sessionfinish and not a changed exit status."""

    def test_unwritable_path_keeps_a_green_run_green(self, pytester, monkeypatch):
        # Parent of the manifest path is a FILE: makedirs/mkstemp both blow up.
        blocker = pytester.path / "blocker"
        blocker.write_text("i am a file, not a directory")
        out = blocker / "manifest.json"
        monkeypatch.setenv("LAZYAF_TEST_RESULTS_PATH", str(out))
        pytester.makepyfile(
            test_green="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.unwritable")
            def test_green():
                assert True
            """
        )

        result = pytester.runpytest_subprocess(*PLUGIN_ARGS)

        assert result.ret == 0, result.stderr.str()
        result.assert_outcomes(passed=1)
        assert not out.exists()

    def test_write_failure_is_reported_on_stderr(self, pytester, monkeypatch):
        blocker = pytester.path / "blocker2"
        blocker.write_text("file")
        monkeypatch.setenv(
            "LAZYAF_TEST_RESULTS_PATH", str(blocker / "manifest.json")
        )
        pytester.makepyfile(
            test_loud="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.loud")
            def test_loud():
                pass
            """
        )

        result = pytester.runpytest_subprocess(*PLUGIN_ARGS)

        assert result.ret == 0
        combined = result.stderr.str() + result.stdout.str()
        assert "pytest-lazyaf" in combined and "manifest" in combined

    def test_failing_run_keeps_its_own_exit_status(self, pytester, monkeypatch):
        """A write failure must not launder a RED run into green either."""
        blocker = pytester.path / "blocker3"
        blocker.write_text("file")
        monkeypatch.setenv(
            "LAZYAF_TEST_RESULTS_PATH", str(blocker / "manifest.json")
        )
        pytester.makepyfile(
            test_red="""
            import pytest

            @pytest.mark.lazyaf_test_id("demo.red")
            def test_red():
                assert False
            """
        )

        result = pytester.runpytest_subprocess(*PLUGIN_ARGS)

        assert result.ret == 1
        result.assert_outcomes(failed=1)
