#!/usr/bin/env python3
"""Single source of truth for the tiered dogfood CI suite (T1/TG/T2/T3).

Encodes, per tier, the EXACT test selection, the junitxml artifact, and the
scripts/ci_gate.py invocation (standing rule R4: no fake green). Every caller
runs tiers through this script - .lazyaf/pipelines/test-suite.yaml, the
scripts/test.sh and scripts/test.ps1 tier/all lanes, and developers by hand -
so a selection change lands in one place and no lane can drift into running a
different (or Docker-polluted) subset.

Two KINDS of tier, named by every spec's `kind` key:
  pytest  T1/T2/T3: `uv run pytest <selection>` from backend/; pytest writes
          the junitxml.
  go      TG: `go tool gotestsum` from the repo root over the Go CLI module
          (upcoming/go-cli.md §10.2); gotestsum writes the junit, with one
          <testcase> per test AND per subtest - the shape scripts/ci_gate.py
          already tallies. `go test` alone emits no junit, which is the whole
          reason gotestsum is in go.mod's `tool` directive.

Stdlib only: runs on the bare python3 of a Linux runner container and on a
Windows host alike. Paths are derived from this file's location, so the
current working directory does not matter.

SELECTION COVERAGE (12.7): every test directory in the repo that a tier can
host is now named by a tier. The last hold-out was `runner-common/tests`,
which lives outside tdd/ because it ships with the package - it joined T1.
The remaining unselected suites are the frontend's (vitest / Playwright, run
by their own lanes) and the @slow e2e tests below.

KNOWN EXCLUSION (stated per R4, not a silent cap): the @slow e2e tests
(control layer, real card execution, graph pipeline full-stack) run in NO
tier - they need the compose e2e stack, which the legacy runner cannot host.
Run them on the host via the scripts/test slow lane; they enter dogfood CI
when ephemeral execution can host the stack. The 12.5 US-2 card loop
(tdd/e2e/test_us2_card_loop.py) is deliberately NOT slow: it drives the whole
card -> agent -> gate -> review -> merge chain against the mock agent, so it
runs in T3 on every push.

Usage:
    python3 scripts/run_tier.py T1 [TG T2 T3 ...] [-- extra runner args]

Everything after a literal `--` goes to the tier's runner verbatim: pytest
for a pytest tier (`-- -k debug`), `go test` for the Go tier
(`-- -run TestContract -v`).
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
CI_GATE = REPO_ROOT / "scripts" / "ci_gate.py"

# runner-common is on the PYTHONPATH for TWO reasons now, and the second one
# is new at 12.7: the manifest plugin below is imported from it, AND T1's
# selection includes `../runner-common/tests` so the package's own 181 tests
# are gated by the ratchet instead of running in no tier at all.
#
# 12.2.6 test tie-back: every tier loads the manifest plugin explicitly
# (`-p runner_common.pytest_lazyaf` — DECISION: no pytest11 entry point, see
# runner-common/pyproject.toml). The backend uv env does NOT install
# runner-common, so the package rides in via PYTHONPATH from the checkout
# (uv run passes the environment through). The plugin is a pure no-op unless
# LAZYAF_TEST_RESULTS_PATH is set — the control runtime injects it per-step,
# so tier steps in a dogfood run emit manifests while host/local runs stay
# byte-identical green. Marker registration for plugin-less invocations
# (plain `uv run pytest ../tdd`) lives in tdd/conftest.py.
RUNNER_COMMON_DIR = REPO_ROOT / "runner-common"

#: The token a `kind: go` spec's argv carries where run_tier() puts the
#: absolute junit path. A literal, so the spec below is data a test can pin
#: rather than a lambda it has to call.
JUNIT_PLACEHOLDER = "<junit>"


def _tier_env() -> dict:
    """Environment for the tier pytest subprocess: runner-common importable."""
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{RUNNER_COMMON_DIR}{os.pathsep}{existing}"
        if existing
        else str(RUNNER_COMMON_DIR)
    )
    return env

# Tier definitions. pytest paths are relative to backend/ (the cwd every
# pytest selection runs from, matching `cd backend && uv run pytest ...`).
TIERS: dict[str, dict] = {
    "T1": {
        "name": "Unit + Demos + Integration + runner-common (no Docker)",
        "kind": "pytest",
        "pytest_args": [
            "../tdd/unit",
            "../tdd/demos",
            "../tdd/integration",
            # runner-common's OWN suite. It lives outside tdd/ because it
            # ships with the package (a user repo installs runner-common and
            # can run it), and until now that meant NO tier selected it: 181
            # tests - including the only package-local cover for the
            # spec-curated context consumer and for the 12.2.6 manifest
            # plugin - ran in no gate at all. It is pure-Python and touches no
            # Docker, so it belongs in T1 and the tier stays the no-Docker
            # tier.
            #
            # Two things make this work and both are already here: the
            # package is importable through the PYTHONPATH `_tier_env()` sets
            # for the manifest plugin, and pytest still resolves rootdir /
            # configfile from the FIRST argument (tdd/pytest.ini), so
            # asyncio_mode and the marker set are unchanged. Keep ../tdd
            # first for that reason.
            "../runner-common/tests",
            # runner-agent's OWN suite, folded in for the same reason
            # runner-common's was: 189 pure-Python tests, ~4s, no Docker, and
            # until now selected by NO tier - so the remote-dispatch client,
            # its backpressure and its CLI ran in no gate at all. PLAN.md's
            # L3-3 recommended this. Its conftest inserts its own sys.path and
            # the backend env already carries every dependency it imports.
            "../runner-agent/tests",
            # The whole services/ subtree is Docker-real (12.2-INT: workspace
            # lifecycle on named volumes, local pipeline execution, WS round
            # trips) and runs in T2 - T1 stays the no-Docker tier.
            "--ignore=../tdd/integration/services",
            "-m",
            "not slow",
        ],
        "junitxml": "junit-t1.xml",
    },
    "T2": {
        "name": "Docker-dependent integration",
        "kind": "pytest",
        "pytest_args": [
            "../tdd/integration/services",
        ],
        "junitxml": "junit-t2.xml",
        # Floor/baseline coherence: T2 tests REQUIRE the locally-built
        # lazyaf-*:dev step images. Verify them before pytest so a missing
        # or stale image is a loud preflight failure with the exact rebuild
        # command - never a skip the gate has to baseline. Runs through
        # `uv run` from backend/ (same env as pytest) for the docker SDK.
        "preflight": {
            "argv": ["uv", "run", "python", "../scripts/build_images.py", "--check"],
            "fix": "python scripts/build_images.py",
        },
    },
    "T3": {
        "name": "E2E quick tier",
        "kind": "pytest",
        "pytest_args": [
            "../tdd/e2e",
            "-m",
            "not slow",
        ],
        "junitxml": "junit-t3.xml",
        # 12.5: T3 gained the US-2 card loop (tdd/e2e/test_us2_card_loop.py),
        # which drives a real agent step. It therefore needs the same image
        # preflight T2 has - a missing or stale lazyaf-agent-base:dev must be
        # a loud failure naming the rebuild command, never a skip the gate has
        # to baseline (R4). Same invocation as T2 so the two cannot drift.
        "preflight": {
            "argv": ["uv", "run", "python", "../scripts/build_images.py", "--check"],
            "fix": "python scripts/build_images.py",
        },
    },
    # The Go CLI tier (upcoming/go-cli.md §10.2). Runs from the repo root,
    # where the ONLY go.mod lives (§2.1), over every package under it - and
    # there are no .go files outside cli/, so `./...` is the CLI module.
    #
    # PREFLIGHT builds the host binary with the real ldflags via THE build
    # definition (scripts/build_cli.sh, R3). Three reasons it is a preflight
    # and not a test: it is the cheapest "does the module compile" check; it
    # names the Go install remedy on a host without `go`; and it leaves
    # cli/bin/lazyaf[.exe] on the run's workspace volume, which T3's
    # tdd/e2e/test_debug_rerun.py drives from P2 (§3.6) - so TG must sit
    # before T3 in the pipeline (§10.4).
    #
    # RUNNER: `go tool gotestsum` - the version is pinned once, in go.mod's
    # `tool` directive, and never linked into the binary. Flags, each earning
    # its place: `--format testname` is one line per test in the step log;
    # `-count=1` defeats Go's test cache, because a cached PASS did not
    # execute and that is the fake green the gate's max-age rule exists for;
    # `-shuffle=on` because order dependence in a fresh suite is cheapest to
    # find now; `-mod=readonly` so a test can never rewrite go.sum. It is
    # Go's default since 1.16 and the image restates it in GOFLAGS, but a
    # dev box with `go env -w GOFLAGS=-mod=mod` (or the variable exported)
    # silently overrides BOTH the default and the image's setting, and then
    # a missing sum is written instead of refused; a flag on the command
    # line beats GOFLAGS, so it goes here, in the one argv every host runs
    # (verified: `GOFLAGS=-mod=mod go test ./...` wrote go.sum in a scratch
    # module; the same with `-mod=readonly` appended refused). NOT `-race`:
    # it needs CGO and the tier image is CGO_ENABLED=0 (§1.4).
    #
    # JUNIT SHAPE, verified on the first real run (§10.3) rather than assumed:
    # (a) a t.Skip reason DOES land in <skipped message="...">, but the
    #     message is the whole `=== RUN ... --- SKIP` transcript, so it starts
    #     with "=== RUN" and can never match a pytest-shaped baseline prefix -
    #     every Go skip is a gate violation by construction, which is the
    #     house rule (zero t.Skip; t.Fatal with the reason instead).
    # (b) a package that fails to COMPILE still gets the junit written: a
    #     <testcase name="TestMain"> with <failure>FAIL pkg [build failed]
    #     </failure> under that package's <testsuite>, and gotestsum exits 1 -
    #     so the red-stays-red return below fires before the gate ever reads
    #     it, and the gate would refuse the failure anyway.
    "TG": {
        "name": "Go CLI: contract, unit, install.sh (no Docker)",
        "kind": "go",
        "cwd": REPO_ROOT,
        "preflight": {
            "argv": ["bash", "scripts/build_cli.sh", "--host-only"],
            "cwd": REPO_ROOT,
            "fix": "bash scripts/build_cli.sh --host-only",
        },
        "argv": [
            "go",
            "tool",
            "gotestsum",
            "--junitfile",
            JUNIT_PLACEHOLDER,
            "--format",
            "testname",
            "--",
            "./...",
            "-count=1",
            "-shuffle=on",
            "-mod=readonly",
        ],
        "junitxml": "junit-tg.xml",
    },
}

#: What each kind needs on PATH - named in the refusal when an executable
#: cannot be started, so a host without it gets one remedy instead of a
#: FileNotFoundError traceback (R1).
_NEEDS: dict[str, str] = {
    "pytest": (
        "uv (https://docs.astral.sh/uv/) with the backend env synced: "
        "cd backend && uv sync --extra test"
    ),
    "go": (
        "go (https://go.dev/dl/ - any Go >= 1.21 bootstraps the go.mod "
        "toolchain once, ~70 MB) and bash (Git Bash on Windows)"
    ),
}

#: What a failed preflight means, per kind - the one line that used to be
#: hard-coded to the step-images check.
_PREFLIGHT_WHAT: dict[str, str] = {
    "pytest": "step images missing/stale",
    "go": "the host binary did not build",
}


def _git_bash_from_exec_path(exec_path: str) -> str | None:
    """Locate Git for Windows' bash.exe from what `git --exec-path` printed.

    Pure, so a test can walk a fake tree. Git for Windows puts the exec path
    at <root>/mingw64/libexec/git-core and bash at <root>/usr/bin/bash.exe
    (plus a <root>/bin/bash.exe launcher); every ancestor is tried because
    which root the exec path sits under depends on the git.exe that answered
    (cmd/git.exe from PowerShell, mingw64/bin/git.exe from inside Git Bash).
    """
    for ancestor in Path(exec_path).parents:
        for candidate in (
            ancestor / "usr" / "bin" / "bash.exe",
            ancestor / "bin" / "bash.exe",
        ):
            if candidate.is_file():
                return str(candidate)
    return None


def _resolve_bash() -> str:
    """The `bash` a spec's argv means.

    In the tier image (Linux) it is the one on PATH. On Windows the FIRST
    `bash` on a stock PATH is C:\\Windows\\System32\\bash.exe - WSL - which
    would run scripts/build_cli.sh inside a Linux distro (or refuse for lack
    of one) and leave a Linux ELF in cli/bin instead of lazyaf.exe. The build
    script is written for Git Bash (upcoming/go-cli.md §2.5), so ask git
    where it lives. No git, or a layout this does not recognise: fall back to
    PATH, and the preflight line prints whichever was chosen.
    """
    if sys.platform != "win32":
        return "bash"
    try:
        exec_path = subprocess.run(
            ["git", "--exec-path"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return "bash"
    return _git_bash_from_exec_path(exec_path) or "bash"


def _resolve_argv(argv: list[str]) -> list[str]:
    """The argv to start for the argv a spec wrote (only `bash` is mapped)."""
    if argv and argv[0] == "bash":
        return [_resolve_bash(), *argv[1:]]
    return list(argv)


def _run_preflight(tier: str, spec: dict) -> int:
    """Run the spec's preflight, if it has one. Returns 0, or the rc to stop with."""
    preflight = spec.get("preflight")
    if not preflight:
        return 0
    # Per-spec cwd: the pytest tiers' preflight runs `uv run ...` from
    # backend/ (same env as their pytest), the Go tier's build runs from the
    # repo root where go.mod lives. Defaults to the BACKEND_DIR that used to
    # be hard-coded so the pytest specs did not have to change.
    cwd = preflight.get("cwd", BACKEND_DIR)
    argv = _resolve_argv(preflight["argv"])
    # flush: the child writes straight to fd 1, so without it this line lands
    # AFTER the child's output in a piped step log - the "which bash" answer
    # would trail the failure it explains.
    print(f"[run_tier] {tier}: preflight: {' '.join(argv)} (cwd={cwd})", flush=True)
    try:
        rc = subprocess.run(argv, cwd=cwd).returncode
    except FileNotFoundError as exc:
        print(
            f"[run_tier] {tier}: PREFLIGHT FAILED - cannot start {argv[0]!r}: {exc}\n"
            f"[run_tier] {tier}: this tier needs {_NEEDS[spec['kind']]}",
            file=sys.stderr,
        )
        return 1
    if rc != 0:
        print(
            f"[run_tier] {tier}: PREFLIGHT FAILED (rc={rc}) - "
            f"{_PREFLIGHT_WHAT[spec['kind']]}.\n"
            f"[run_tier] {tier}: fix that, then re-run this tier:\n"
            f"[run_tier] {tier}:     {preflight['fix']}",
            file=sys.stderr,
        )
        return rc or 1
    return 0


def _go_env() -> dict:
    """Environment for the Go tier: name a Python that has pytest, if one exists.

    `lazyaf tests reconcile --from-collect` runs a collector INSIDE the user's
    pytest, and its test (reconcile.TestFromCollect) does the same for real.
    The Python CLI ran that collector with `sys.executable` - always a Python
    that had pytest, because it WAS the pytest process's interpreter. The Go
    binary has no such interpreter and resolves one (`--python`, then
    $LAZYAF_PYTHON, then $VIRTUAL_ENV, then python3/python); on a Windows dev
    box the first thing on PATH named `python3` is the Microsoft Store stub,
    which is why the resolver probes rather than trusts LookPath.

    So the tier sets LAZYAF_PYTHON to the backend venv's interpreter when that
    venv exists and nothing set the variable already. In the test-runner
    image there is no venv; the variable stays unset and the resolver finds
    the image's python3, which carries pytest. Never overrides a caller's
    explicit choice (R1: the chosen interpreter is named in the test output).
    """
    env = os.environ.copy()
    if not env.get("LAZYAF_PYTHON"):
        venv_py = BACKEND_DIR / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        if venv_py.exists():
            env["LAZYAF_PYTHON"] = str(venv_py)
    return env


def _runner(tier: str, spec: dict, junit_path: Path, extra_args: list[str]):
    """The (name, argv, cwd, env) that runs one tier's tests - the `kind` switch."""
    kind = spec["kind"]
    if kind == "pytest":
        cmd = [
            "uv",
            "run",
            "pytest",
            *spec["pytest_args"],
            # 12.2.6 manifest plugin (no-op without LAZYAF_TEST_RESULTS_PATH);
            # importable via the PYTHONPATH set in _tier_env().
            "-p",
            "runner_common.pytest_lazyaf",
            "-rs",
            f"--junitxml={junit_path}",
            *extra_args,
        ]
        return "pytest", cmd, BACKEND_DIR, _tier_env()
    if kind == "go":
        cmd = [
            str(junit_path) if arg == JUNIT_PLACEHOLDER else arg
            for arg in spec["argv"]
        ]
        # After the spec's own `-- ./... -count=1 -shuffle=on -mod=readonly`,
        # so extras are `go test` flags (`-run`, `-v`) exactly as pytest
        # extras are pytest's.
        return "gotestsum", [*cmd, *extra_args], spec["cwd"], _go_env()
    raise ValueError(
        f"tier {tier!r} has unknown kind {kind!r}; run_tier.py knows: pytest, go"
    )


def run_tier(tier: str, extra_args: list[str]) -> int:
    """Run one tier's selection through its runner, then gate its junitxml. Returns rc."""
    spec = TIERS[tier]
    junit_path = REPO_ROOT / spec["junitxml"]

    rc = _run_preflight(tier, spec)
    if rc != 0:
        return rc

    # Delete any previous report BEFORE the runner starts. A junitxml is
    # written at the END of a session (pytest and gotestsum alike), so a run
    # that never starts - a bad plugin, an import error, a stray `--help` -
    # leaves the PREVIOUS run's file sitting there, and ci_gate happily
    # reports it as this run's result. Measured: `run_tier.py T1 -- --help`
    # printed "CI GATE [T1]: OK - executed=4836" having executed nothing. The
    # gate refuses a stale file on its own now too; this is the other half,
    # because the freshest possible fix is not writing the trap in the first
    # place.
    junit_path.unlink(missing_ok=True)

    runner, cmd, cwd, env = _runner(tier, spec, junit_path, extra_args)
    print(f"[run_tier] {tier}: {spec['name']}")
    print(f"[run_tier] {tier}: {' '.join(cmd)} (cwd={cwd})", flush=True)
    try:
        rc = subprocess.run(cmd, cwd=cwd, env=env).returncode
    except FileNotFoundError as exc:
        print(
            f"[run_tier] {tier}: cannot start {cmd[0]!r}: {exc}\n"
            f"[run_tier] {tier}: this tier needs {_NEEDS[spec['kind']]}",
            file=sys.stderr,
        )
        return 1
    if rc != 0:
        # Red stays red - the gate never launders a failing tier.
        print(f"[run_tier] {tier}: {runner} failed (rc={rc})", file=sys.stderr)
        return rc

    gate_cmd = [sys.executable, str(CI_GATE), "--tier", tier, str(junit_path)]
    print(f"[run_tier] {tier}: {' '.join(gate_cmd)}", flush=True)
    return subprocess.run(gate_cmd, cwd=REPO_ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # Everything after a literal `--` is passed through to the runner verbatim.
    extra_args: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, extra_args = args[:split], args[split + 1 :]

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tiers",
        nargs="+",
        choices=sorted(TIERS),
        metavar="TIER",
        help=f"tier(s) to run, in order: {', '.join(sorted(TIERS))}",
    )
    parsed = parser.parse_args(args)

    for tier in parsed.tiers:
        rc = run_tier(tier, extra_args)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
