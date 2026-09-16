"""
Tests for scripts/run_tier.py - the single source of tier selection - and
the Go tier it gained at upcoming/go-cli.md §10.2.

The script grew a `kind` switch (pytest | go) so the Go CLI's tests run
through the same preflight -> unlink stale junit -> runner -> ci_gate spine
the pytest tiers use. Two things are pinned here and nowhere else:

  * the TG spec's PROPERTIES (§10.2): gotestsum from go.mod's tool
    directive, `--junitfile`, `-count=1` (a cached PASS did not execute),
    `-shuffle=on`, `-mod=readonly` (a test can never rewrite go.sum, whatever
    GOFLAGS says on the host), no `-race`, the host-binary build as
    preflight, the repo root as cwd - each of which is a fake-green door if
    it drifts;
  * that the three pytest tiers came through the switch UNTOUCHED - their
    selections are pinned verbatim, so "we only added a kind" outlives the
    diff review that checked it.

The switch is driven with subprocess.run replaced by a recorder (R6: the
seam is the real stdlib call; the tier runners are minutes long and belong
to the tiers themselves). The script's real CLI is run once for its
argument grammar, the way the pipeline invokes it.
"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
RUN_TIER = REPO_ROOT / "scripts" / "run_tier.py"
DOGFOOD_PIPELINE = REPO_ROOT / ".lazyaf" / "pipelines" / "test-suite.yaml"


@pytest.fixture(scope="module")
def run_tier():
    """The script imported as a module (stdlib-only, so this is cheap)."""
    spec = importlib.util.spec_from_file_location("lazyaf_run_tier", RUN_TIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    """Stands in for subprocess.run: records every call, answers by position.

    An entry in ``outcomes`` is an int returncode or an exception instance to
    raise (a missing executable is a FileNotFoundError from subprocess.run,
    not a returncode). ``watch`` is a path whose existence is sampled at every
    call, so a test can prove WHEN the stale junit was unlinked.
    """

    def __init__(self, outcomes, watch: Path | None = None):
        self.outcomes = list(outcomes)
        self.watch = watch
        self.calls: list[tuple[list[str], dict]] = []
        self.watched: list[bool] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.watch is not None:
            self.watched.append(self.watch.exists())
        outcome = self.outcomes.pop(0) if self.outcomes else 0
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(argv, outcome)


@pytest.fixture
def recorded(run_tier, monkeypatch, tmp_path):
    """Route the script at tmp_path (junit lands there, never in the repo)
    and at a recorder; the bash resolver is pinned so the recorded argv is
    the spec's own, not this host's Git Bash path."""
    monkeypatch.setattr(run_tier, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(run_tier, "_resolve_bash", lambda: "bash")

    def install(outcomes, watch=None):
        recorder = Recorder(outcomes, watch)
        monkeypatch.setattr(run_tier.subprocess, "run", recorder)
        return recorder

    return install


class TestTheGoTierSpec:
    """§10.2, as data. Every property here is one the gate cannot see."""

    def test_kind_is_go_and_it_is_the_only_one(self, run_tier):
        kinds = {tier: spec["kind"] for tier, spec in run_tier.TIERS.items()}
        assert kinds == {"T1": "pytest", "T2": "pytest", "T3": "pytest", "TG": "go"}

    def test_runs_from_the_repo_root_where_the_only_go_mod_lives(self, run_tier):
        """§2.1: ONE module, at the root; `./...` from there is the CLI."""
        tg = run_tier.TIERS["TG"]
        assert tg["cwd"] == run_tier.REPO_ROOT
        assert (run_tier.REPO_ROOT / "go.mod").is_file()

    def test_preflight_is_the_one_build_definition_host_only(self, run_tier):
        """scripts/build_cli.sh is THE build (R3); --host-only leaves
        cli/bin/lazyaf[.exe] for T3's debug loop (§3.6), so it runs from the
        repo root and the fix names the identical command."""
        preflight = run_tier.TIERS["TG"]["preflight"]
        assert preflight["argv"] == ["bash", "scripts/build_cli.sh", "--host-only"]
        assert preflight["cwd"] == run_tier.REPO_ROOT
        assert preflight["fix"] == "bash scripts/build_cli.sh --host-only"

    def test_gotestsum_is_the_go_mod_tool_and_writes_the_junit(self, run_tier):
        """`go tool gotestsum` resolves through go.mod's `tool` directive
        (pinned in ONE file, never linked into the binary). The junit path is
        the placeholder the runner substitutes, and the file name follows the
        junit-t*.xml convention the gate and .gitignore already know."""
        tg = run_tier.TIERS["TG"]
        argv = tg["argv"]
        assert argv[:3] == ["go", "tool", "gotestsum"]
        assert argv[argv.index("--junitfile") + 1] == run_tier.JUNIT_PLACEHOLDER
        assert argv[argv.index("--format") + 1] == "testname"
        assert tg["junitxml"] == "junit-tg.xml"

    def test_go_test_flags_defeat_the_cache_and_shuffle_every_package(self, run_tier):
        """-count=1: Go's test cache would otherwise replay a PASS that did
        not execute - the fake green the gate's max-age rule exists for.
        -shuffle=on: order dependence is cheapest to find in a fresh suite.
        -mod=readonly: a test can never rewrite go.sum. Go's default and the
        image's GOFLAGS both say so, but a host `GOFLAGS=-mod=mod` overrides
        both silently; only a command-line flag beats GOFLAGS, so the spec
        carries it in the argv every host runs, not only in the image's ENV.
        ./...: every package, so a new one cannot land ungated."""
        argv = run_tier.TIERS["TG"]["argv"]
        go_test_flags = argv[argv.index("--") + 1 :]
        assert "./..." in go_test_flags
        assert "-count=1" in go_test_flags
        assert "-shuffle=on" in go_test_flags
        assert "-mod=readonly" in go_test_flags

    def test_no_race_detector(self, run_tier):
        """§1.4: -race needs CGO; the tier image is CGO_ENABLED=0. A flag
        that cannot run there must not be in the spec that runs there."""
        assert "-race" not in run_tier.TIERS["TG"]["argv"]


class TestTheKindSwitch:
    def test_go_tier_builds_then_tests_then_gates(self, run_tier, recorded, tmp_path):
        stale = tmp_path / "junit-tg.xml"
        stale.write_text("<testsuites/>", encoding="utf-8")
        recorder = recorded([0, 0, 0], watch=stale)

        assert run_tier.run_tier("TG", []) == 0

        (preflight, preflight_kw), (go, go_kw), (gate, gate_kw) = recorder.calls
        assert preflight == ["bash", "scripts/build_cli.sh", "--host-only"]
        assert preflight_kw["cwd"] == run_tier.TIERS["TG"]["preflight"]["cwd"]

        assert go[:3] == ["go", "tool", "gotestsum"]
        assert go[go.index("--junitfile") + 1] == str(stale)
        assert run_tier.JUNIT_PLACEHOLDER not in go
        assert go_kw["cwd"] == run_tier.TIERS["TG"]["cwd"]

        assert gate == [sys.executable, str(run_tier.CI_GATE), "--tier", "TG", str(stale)]
        assert gate_kw["cwd"] == tmp_path

        # The stale junit was there for the preflight and GONE before
        # gotestsum started - a run that never starts must leave no report
        # for the gate to mistake for this run's.
        assert recorder.watched == [True, False, False]

    def test_a_failed_build_stops_before_gotestsum_and_names_the_fix(
        self, run_tier, recorded, capsys
    ):
        recorder = recorded([1])

        assert run_tier.run_tier("TG", []) == 1

        assert len(recorder.calls) == 1
        err = capsys.readouterr().err
        assert "PREFLIGHT FAILED" in err
        assert "bash scripts/build_cli.sh --host-only" in err

    def test_red_gotestsum_stays_red_and_never_reaches_the_gate(
        self, run_tier, recorded, capsys
    ):
        recorder = recorded([0, 1])

        assert run_tier.run_tier("TG", []) == 1

        assert [argv[0] for argv, _ in recorder.calls] == ["bash", "go"]
        assert "gotestsum failed (rc=1)" in capsys.readouterr().err

    def test_a_host_without_go_is_told_what_to_install(
        self, run_tier, recorded, capsys
    ):
        """subprocess.run raises FileNotFoundError for a missing executable;
        a traceback is not a refusal (R1)."""
        recorded([0, FileNotFoundError(2, "No such file or directory", "go")])

        assert run_tier.run_tier("TG", []) == 1

        err = capsys.readouterr().err
        assert "cannot start 'go'" in err
        assert "https://go.dev/dl/" in err

    def test_a_host_without_bash_is_told_at_the_preflight(
        self, run_tier, recorded, capsys
    ):
        recorded([FileNotFoundError(2, "No such file or directory", "bash")])

        assert run_tier.run_tier("TG", []) == 1

        err = capsys.readouterr().err
        assert "PREFLIGHT FAILED - cannot start 'bash'" in err
        assert "Git Bash on Windows" in err

    def test_extra_args_after_the_dash_dash_are_go_test_flags(
        self, run_tier, recorded
    ):
        recorder = recorded([0, 0, 0])

        run_tier.run_tier("TG", ["-run", "TestContract", "-v"])

        go, _ = recorder.calls[1]
        assert go[go.index("--") + 1 :] == [
            "./...",
            "-count=1",
            "-shuffle=on",
            "-mod=readonly",
            "-run",
            "TestContract",
            "-v",
        ]

    def test_pytest_tiers_still_run_uv_from_backend_with_runner_common(
        self, run_tier, recorded, tmp_path
    ):
        recorder = recorded([0, 0])

        assert run_tier.run_tier("T1", ["-k", "nothing"]) == 0

        (pytest_argv, pytest_kw), (gate, _) = recorder.calls
        assert pytest_argv[:3] == ["uv", "run", "pytest"]
        assert pytest_argv[3 : 3 + len(run_tier.TIERS["T1"]["pytest_args"])] == (
            run_tier.TIERS["T1"]["pytest_args"]
        )
        assert pytest_argv[-4:] == [
            "-rs",
            f"--junitxml={tmp_path / 'junit-t1.xml'}",
            "-k",
            "nothing",
        ]
        assert pytest_argv[pytest_argv.index("-p") + 1] == "runner_common.pytest_lazyaf"
        assert pytest_kw["cwd"] == run_tier.BACKEND_DIR
        assert pytest_kw["env"]["PYTHONPATH"].startswith(str(run_tier.RUNNER_COMMON_DIR))
        assert gate[-3:] == ["--tier", "T1", str(tmp_path / "junit-t1.xml")]

    def test_pytest_preflights_still_run_from_backend(self, run_tier, recorded):
        """The preflight cwd became per-spec for the Go tier; the pytest
        tiers never wrote one, so they must still get the BACKEND_DIR that
        was hard-coded - `uv run` needs the backend env."""
        recorder = recorded([0, 0, 0])

        run_tier.run_tier("T2", [])

        preflight, preflight_kw = recorder.calls[0]
        assert preflight == ["uv", "run", "python", "../scripts/build_images.py", "--check"]
        assert preflight_kw["cwd"] == run_tier.BACKEND_DIR

    def test_an_unknown_kind_is_refused_by_name(self, run_tier, recorded, monkeypatch):
        recorded([])
        monkeypatch.setitem(
            run_tier.TIERS,
            "TX",
            {"name": "made up", "kind": "rust", "junitxml": "junit-tx.xml"},
        )

        with pytest.raises(ValueError, match="'rust'"):
            run_tier.run_tier("TX", [])


class TestThePytestTiersAreUntouched:
    """Byte-for-byte: the Go tier landed by ADDING a kind. These are the
    three selections as they stood before it, plus the one key each gained."""

    def test_t1(self, run_tier):
        assert run_tier.TIERS["T1"] == {
            "name": "Unit + Demos + Integration + runner-common (no Docker)",
            "kind": "pytest",
            "pytest_args": [
                "../tdd/unit",
                "../tdd/demos",
                "../tdd/integration",
                "../runner-common/tests",
                "../runner-agent/tests",
                "--ignore=../tdd/integration/services",
                "-m",
                "not slow",
            ],
            "junitxml": "junit-t1.xml",
        }

    def test_t2(self, run_tier):
        assert run_tier.TIERS["T2"] == {
            "name": "Docker-dependent integration",
            "kind": "pytest",
            "pytest_args": ["../tdd/integration/services"],
            "junitxml": "junit-t2.xml",
            "preflight": {
                "argv": ["uv", "run", "python", "../scripts/build_images.py", "--check"],
                "fix": "python scripts/build_images.py",
            },
        }

    def test_t3(self, run_tier):
        assert run_tier.TIERS["T3"] == {
            "name": "E2E quick tier",
            "kind": "pytest",
            "pytest_args": ["../tdd/e2e", "-m", "not slow"],
            "junitxml": "junit-t3.xml",
            "preflight": {
                "argv": ["uv", "run", "python", "../scripts/build_images.py", "--check"],
                "fix": "python scripts/build_images.py",
            },
        }


class TestTheBashResolver:
    """`bash` in a spec means Git Bash on Windows (§2.5), not WSL's
    System32\\bash.exe, which is FIRST on a stock Windows PATH and would run
    the build inside a Linux distro. The tier image is Linux and untouched."""

    def test_finds_git_for_windows_bash_beside_the_exec_path(self, run_tier, tmp_path):
        root = tmp_path / "Git"
        exec_path = root / "mingw64" / "libexec" / "git-core"
        exec_path.mkdir(parents=True)
        bash = root / "usr" / "bin" / "bash.exe"
        bash.parent.mkdir(parents=True)
        bash.write_bytes(b"")

        assert run_tier._git_bash_from_exec_path(str(exec_path)) == str(bash)

    def test_an_unrecognised_layout_yields_none(self, run_tier, tmp_path):
        exec_path = tmp_path / "somewhere" / "libexec" / "git-core"
        exec_path.mkdir(parents=True)

        assert run_tier._git_bash_from_exec_path(str(exec_path)) is None

    def test_resolves_on_this_host(self, run_tier):
        """Platform-conditional ASSERTION, not a skip: Linux keeps the spec's
        `bash`; Windows must come back with a Git bash.exe and never the
        WSL launcher."""
        resolved = run_tier._resolve_bash()
        if sys.platform == "win32":
            assert resolved.lower().endswith("bash.exe"), resolved
            assert "system32" not in resolved.lower(), resolved
            assert Path(resolved).is_file()
        else:
            assert resolved == "bash"

    def test_only_bash_is_mapped(self, run_tier, monkeypatch):
        monkeypatch.setattr(run_tier, "_resolve_bash", lambda: "/opt/git/bash.exe")
        assert run_tier._resolve_argv(["bash", "x.sh"]) == ["/opt/git/bash.exe", "x.sh"]
        assert run_tier._resolve_argv(["go", "tool", "gotestsum"]) == ["go", "tool", "gotestsum"]


class TestTheRealCLI:
    """The script as the pipeline and the shell lanes invoke it."""

    def test_tg_is_a_choice(self):
        result = subprocess.run(
            [sys.executable, str(RUN_TIER), "--help"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "TG" in result.stdout

    def test_an_unknown_tier_is_a_usage_error(self):
        result = subprocess.run(
            [sys.executable, str(RUN_TIER), "TX"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2
        assert "invalid choice" in result.stderr


class TestTheDogfoodStep:
    """§10.4: `tier-go` sits AFTER tier1 (T1 holds the corpus-freshness test,
    whose "re-run the generator" is a clearer first failure than TG's byte
    diff), BEFORE tier2 (no Docker socket: it must not sit in the
    DooD-allowlisted block) and BEFORE tier3 (test_debug_rerun.py reads the
    cli/bin/lazyaf the TG preflight leaves on the run's workspace volume)."""

    @pytest.fixture(scope="class")
    def steps(self):
        pipeline = yaml.safe_load(DOGFOOD_PIPELINE.read_text(encoding="utf-8"))
        return {step["id"]: step for step in pipeline["steps"]}, [
            step["id"] for step in pipeline["steps"]
        ]

    def test_between_tier1_and_tier2(self, steps):
        _, order = steps
        assert order.index("tier1") < order.index("tier-go") < order.index("tier2")
        assert order.index("tier-go") < order.index("tier3")

    def test_runs_the_single_sourced_tier_on_the_test_runner_image(self, steps):
        by_id, _ = steps
        step = by_id["tier-go"]
        assert step["type"] == "script"
        assert step["config"]["image"] == "lazyaf-test-runner:dev"
        assert step["config"]["command"].strip() == "python3 scripts/run_tier.py TG"
        assert step["on_failure"] == "stop"

    def test_needs_no_docker_socket(self, steps):
        """The tier is no-Docker by construction; a `needs: [docker]` here
        would hand the host socket to a step that has no use for it."""
        by_id, _ = steps
        assert "needs" not in by_id["tier-go"]["config"]
