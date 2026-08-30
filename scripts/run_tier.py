#!/usr/bin/env python3
"""Single source of truth for the tiered dogfood CI suite (T1/T2/T3).

Encodes, per tier, the EXACT pytest selection, the junitxml artifact, and the
scripts/ci_gate.py invocation (standing rule R4: no fake green). Every caller
runs tiers through this script - .lazyaf/pipelines/test-suite.yaml, the
scripts/test.sh and scripts/test.ps1 tier/all lanes, and developers by hand -
so a selection change lands in one place and no lane can drift into running a
different (or Docker-polluted) subset.

Stdlib only: runs on the bare python3 of a Linux runner container and on a
Windows host alike. Paths are derived from this file's location, so the
current working directory does not matter.

KNOWN EXCLUSION (stated per R4, not a silent cap): the @slow e2e tests
(control layer, real card execution, graph pipeline full-stack) run in NO
tier - they need the compose e2e stack, which the legacy runner cannot host.
Run them on the host via the scripts/test slow lane; they enter dogfood CI
when ephemeral execution can host the stack. The 12.5 US-2 card loop
(tdd/e2e/test_us2_card_loop.py) is deliberately NOT slow: it drives the whole
card -> agent -> gate -> review -> merge chain against the mock agent, so it
runs in T3 on every push.

Usage:
    python3 scripts/run_tier.py T1 [T2 T3 ...] [-- extra pytest args]
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
CI_GATE = REPO_ROOT / "scripts" / "ci_gate.py"

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
# selection runs from, matching `cd backend && uv run pytest ...`).
TIERS: dict[str, dict] = {
    "T1": {
        "name": "Unit + Demos + Integration (no Docker)",
        "pytest_args": [
            "../tdd/unit",
            "../tdd/demos",
            "../tdd/integration",
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
}


def run_tier(tier: str, extra_pytest_args: list[str]) -> int:
    """Run one tier's pytest selection, then gate its junitxml. Returns rc."""
    spec = TIERS[tier]
    junit_path = REPO_ROOT / spec["junitxml"]

    preflight = spec.get("preflight")
    if preflight:
        print(f"[run_tier] {tier}: preflight: {' '.join(preflight['argv'])} (cwd={BACKEND_DIR})")
        rc = subprocess.run(preflight["argv"], cwd=BACKEND_DIR).returncode
        if rc != 0:
            print(
                f"[run_tier] {tier}: PREFLIGHT FAILED - step images missing/stale.\n"
                f"[run_tier] {tier}: build them, then re-run this tier:\n"
                f"[run_tier] {tier}:     {preflight['fix']}",
                file=sys.stderr,
            )
            return rc or 1

    pytest_cmd = [
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
        *extra_pytest_args,
    ]
    print(f"[run_tier] {tier}: {spec['name']}")
    print(f"[run_tier] {tier}: {' '.join(pytest_cmd)} (cwd={BACKEND_DIR})")
    rc = subprocess.run(pytest_cmd, cwd=BACKEND_DIR, env=_tier_env()).returncode
    if rc != 0:
        # Red pytest stays red - the gate never launders a failing tier.
        print(f"[run_tier] {tier}: pytest failed (rc={rc})", file=sys.stderr)
        return rc

    gate_cmd = [sys.executable, str(CI_GATE), "--tier", tier, str(junit_path)]
    print(f"[run_tier] {tier}: {' '.join(gate_cmd)}")
    return subprocess.run(gate_cmd, cwd=REPO_ROOT).returncode


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    # Everything after a literal `--` is passed through to pytest verbatim.
    extra_pytest_args: list[str] = []
    if "--" in args:
        split = args.index("--")
        args, extra_pytest_args = args[:split], args[split + 1 :]

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
        rc = run_tier(tier, extra_pytest_args)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
