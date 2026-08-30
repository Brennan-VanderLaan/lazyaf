#!/usr/bin/env python3
"""
verify_executor.py - dogfood exit-gate ratchet (R1), 12.5 edition.

Asserts, for the CURRENT pipeline run:
  1. (12.2-INT) every script/docker StepRun was executed by the
     LocalExecutor (executor == 'local'); and
  2. (12.3) every ALREADY-PASSED script/docker StepRun delivered logs.
     The dogfood steps run on control-layer images, where the ONLY writer
     of StepRun.logs is the in-container control runtime POSTing to
     /api/steps/{id}/logs (the stdout consumer drops log events in control
     mode) - so a passed step with empty logs means the control-layer
     reporting path silently failed. Backend-appended '[lazyaf] ' marker
     lines (e.g. '[lazyaf] exit code: 0') do NOT count: they are written
     by the backend itself, so a marker-only log field is exactly the
     clobber shape a broken reporting path produces. At least one
     NON-marker log byte is required. The step running this script is
     exempt (its own logs are still streaming).
  3. (12.2.6) no step reported a test-results manifest delivery problem.
  4. (12.5) every AGENT StepRun was executed by the LocalExecutor too - the
     mock-agent step of this pipeline is US-2 continuous coverage, and a
     silent fall back to the legacy runner queue would still go green
     without this.
  5. (12.5) that agent step has a StepUsage row carrying real token counts:
     GET /api/steps/{step_execution_id}/usage must report non-null
     input_tokens, output_tokens and cost_source. A dark usage channel is
     M13's entire cost axis missing, and it fails a step in no other way.
  5b.(12.5, F3.1) no step recorded a usage SCRAPE FAILURE. "the provider
     reported nothing" and "we could not read what the provider reported"
     are different facts, and only the second one means a vendor changed
     its output from under us. The wrapper records it two ways - a
     '_scrape_failed' marker inside the stored usage row's `raw`, and a
     stable '[agent] WARNING: usage scrape failed' line on the step log
     stream - and BOTH are checked here, because a scrape failure never
     fails a step and so has nowhere else to surface. Left unwatched it
     would record every future agent step as costing nothing.
  6. (12.5) EVERY passed step of the run - script steps included - has a
     StepUsage row in GET /api/pipeline-runs/{run_id}/usage.
     cost_source == 'unknown' is accepted there ("the provider told us
     nothing" is a recorded fact); a MISSING row is not.
  7. (12.5) GET /api/runners/status reports queued_jobs == 0: after 12.5 no
     default path enqueues to the polling runners, and the runners sitting
     idle is asserted, never assumed.

A vacuous pass (no script/docker step runs found) is a failure (R4).

Env contract (injected into every step container by LocalExecutor; the
backend URL default matches settings.container_backend_url):
  LAZYAF_PIPELINE_RUN_ID  - required; the run to verify
  LAZYAF_STEP_INDEX       - optional; this step's own index (exempt from
                            the log-delivery and usage checks)
  LAZYAF_BACKEND_URL      - optional; defaults to http://backend:8000

Stdlib-only on purpose: this runs inside a bare lazyaf-base step container.
"""
import json
import os
import urllib.request

DEFAULT_BACKEND_URL = "http://backend:8000"
EXECUTED_STEP_TYPES = ("script", "docker")
AGENT_STEP_TYPE = "agent"
# Lines the BACKEND appends to StepRun.logs itself (e.g. '[lazyaf] exit
# code: 0'). They prove nothing about the in-container reporting path, so
# the log-delivery probe ignores them.
LOG_MARKER_PREFIX = "[lazyaf] "
# Stable marker the agent wrapper prints when a usage scraper could not find
# the CLI's own report (runner_common.usage.SCRAPE_FAILED_LOG_MARKER). Not
# imported: this script is stdlib-only and runs in a bare step container that
# does not have runner_common installed.
SCRAPE_FAILED_LOG_MARKER = "[agent] WARNING: usage scrape failed"
# Marker the wrapper stamps into the manifest's `raw` object, which run.py
# forwards verbatim and the server stores
# (runner_common.usage.RAW_SCRAPE_FAILED).
RAW_SCRAPE_FAILED = "_scrape_failed"
RAW_SCRAPE_ERROR = "_scrape_error"


def has_delivered_logs(logs) -> bool:
    """True iff logs contain at least one non-blank, NON-marker line.

    A logs field that is empty, whitespace, or made up solely of backend
    '[lazyaf] ' marker lines means the control runtime's POST
    /api/steps/{id}/logs batches never landed - a vacuous pass stays a
    failure.
    """
    for line in (logs or "").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith(LOG_MARKER_PREFIX.strip()):
            return True
    return False


def fetch_json(base_url: str, path: str, timeout: float = 30.0):
    """GET base_url+path and decode the JSON response body."""
    with urllib.request.urlopen(base_url + path, timeout=timeout) as resp:
        return json.load(resp)


def verify_run(base_url: str, run_id: str, self_index: int | None = None) -> str:
    """Verify executor routing, log delivery and the usage channel.

    Returns an OK message on success; raises SystemExit with a FAIL
    message otherwise (vacuous pass = fail, R4).
    """
    run = fetch_json(base_url, f"/api/pipeline-runs/{run_id}")
    pipeline = fetch_json(base_url, f"/api/pipelines/{run['pipeline_id']}")
    step_types = {
        i: s.get("type", "script") for i, s in enumerate(pipeline["steps"])
    }

    checked = 0
    agents_checked = 0
    bad = []
    silent = []
    for sr in run["step_runs"]:
        step_type = step_types.get(sr["step_index"], "script")
        if step_type == AGENT_STEP_TYPE:
            # 12.5: agent steps left the legacy queue. They are checked for
            # executor='local' exactly like script steps - the whole point
            # of the phase is that there is no longer a difference.
            agents_checked += 1
        elif step_type not in EXECUTED_STEP_TYPES:
            continue
        else:
            checked += 1
        if sr["executor"] != "local":
            bad.append(
                f"step {sr['step_index']} '{sr['step_name']}' ({step_type}) -> "
                f"executor={sr['executor']!r}"
            )
        # 12.3 control-path probe: passed steps (other than this one) must
        # have NON-marker logs on record - in control mode those only exist
        # if the in-container runtime's POST /api/steps/{id}/logs batches
        # landed. Backend-appended '[lazyaf] ' marker lines don't count.
        if (
            sr["step_index"] != self_index
            and sr.get("status") == "passed"
            and not has_delivered_logs(sr.get("logs"))
        ):
            silent.append(
                f"step {sr['step_index']} '{sr['step_name']}' passed "
                f"with EMPTY logs (no non-marker log lines)"
            )

    if not checked:
        raise SystemExit(
            "FAIL: no script/docker step runs found (vacuous pass = fail, R4)"
        )
    # Order matters: a step on the WRONG executor is a live regression and
    # must be reported before the ratchet-completeness check below, which is
    # about the pipeline definition rather than about this run's behavior.
    if bad:
        raise SystemExit(
            "FAIL: steps not executed by LocalExecutor:\n  " + "\n  ".join(bad)
        )
    if not agents_checked:
        raise SystemExit(
            "FAIL: no agent step runs found in this pipeline. The 12.5 dogfood "
            "ratchet requires the zero-cost mock agent step (US-2 continuous "
            "coverage) - a missing agent step means the agent path is not "
            "exercised on push at all (vacuous pass = fail, R4)"
        )
    if silent:
        raise SystemExit(
            "FAIL: control-layer reporting path delivered no logs "
            "(POST /api/steps/*/logs never landed?):\n  " + "\n  ".join(silent)
        )
    # 12.2.6 ratchet (R7): manifest delivery is deliberately non-fatal to the
    # STEP - a test-results POST that 404s must never fail a green suite. That
    # is correct, and it is exactly why it can rot unnoticed: a dogfood run
    # once shipped three manifests into 404s and still reported a clean gate,
    # because nothing here looked. The runtime announces every delivery
    # problem on stdout with a stable marker, so the GATE breaks that silence.
    manifest_problems = []
    scrape_problems = []
    for sr in run["step_runs"]:
        for line in (sr.get("logs") or "").splitlines():
            if "[control] WARNING: test results manifest" in line:
                manifest_problems.append(
                    f"step {sr.get('step_index')} '{sr.get('step_name')}': {line.strip()}"
                )
            elif SCRAPE_FAILED_LOG_MARKER in line:
                scrape_problems.append(
                    f"step {sr.get('step_index')} '{sr.get('step_name')}': {line.strip()}"
                )
    if manifest_problems:
        raise SystemExit(
            "FAIL: test-result manifests did not reach the backend (12.2.6 "
            "tie-back is dark - check the /api/steps/*/test-results route and "
            "that migrations are applied):\n  " + "\n  ".join(manifest_problems)
        )
    if scrape_problems:
        raise SystemExit(
            "FAIL: a usage scraper could not find the agent CLI's own report "
            "(12.5 F3.1). That is a VENDOR OUTPUT CHANGE, not a free step: "
            "left unwatched it records every future step of that agent at "
            "zero cost and quietly destroys M13's cost axis. Capture the "
            "CLI's stdout and fix the scraper in "
            "runner-common/runner_common/usage.py:\n  "
            + "\n  ".join(scrape_problems)
        )

    usage_msg = verify_usage(base_url, run, step_types, self_index=self_index)
    idle_msg = verify_runners_idle(base_url)

    return (
        f"OK: {checked} script step run(s) and {agents_checked} agent step "
        f"run(s) all have executor='local', passed steps delivered logs, no "
        f"manifest delivery problems, {usage_msg}, {idle_msg}"
    )


def verify_usage(
    base_url: str, run: dict, step_types: dict, self_index: int | None = None
) -> str:
    """12.5: the usage channel must have written a row for every step.

    `docs/milestone-13/api-surface.md` section 2 is BINDING and the whole
    reason the channel ships in this phase rather than in M13. Telemetry
    never fails a STEP by design - so if nothing here looked, a usage channel
    that silently stopped writing would go unnoticed until M13's cost board
    was already built on top of the gap.

    THE ONE EXEMPTION, stated rather than silent: this script's own step runs
    with `control: false` (gate independence - the gate must not depend on
    the runtime it verifies). A stdout-mode step has no control runtime and
    therefore cannot POST usage. It is exempted by index, not by guesswork.
    """
    rollup = fetch_json(base_url, f"/api/pipeline-runs/{run['id']}/usage")
    by_step_run = {
        row["step_run_id"]: row
        for row in rollup.get("steps", [])
        if row.get("step_run_id")
    }

    missing = []
    agent_rows = []
    for sr in run["step_runs"]:
        if sr["step_index"] == self_index:
            continue  # stdout-mode gate step: no control runtime, no usage
        if sr.get("status") != "passed":
            continue  # still running / failed: its usage POST has not landed
        row = by_step_run.get(sr["id"])
        if row is None:
            missing.append(
                f"step {sr['step_index']} '{sr['step_name']}' "
                f"({step_types.get(sr['step_index'], 'script')})"
            )
            continue
        if step_types.get(sr["step_index"]) == AGENT_STEP_TYPE:
            agent_rows.append((sr, row))

    if missing:
        raise SystemExit(
            "FAIL: the usage channel dropped rows - no StepUsage for:\n  "
            + "\n  ".join(missing)
            + "\n(check POST /api/steps/{id}/usage, run.py's ship_usage, and "
            "that migration 0005 is applied. A 'cost_source: unknown' row is "
            "fine here; a MISSING row is the channel being dark.)"
        )
    if not agent_rows:
        raise SystemExit(
            "FAIL: the agent step produced no StepUsage row - the agent "
            "wrapper's usage manifest never reached the backend (M13's entire "
            "cost axis is written by this path)"
        )

    # The agent row is the only one that can carry real numbers: script steps
    # have no CLI to report tokens, so run.py posts them a cost_source
    # 'unknown' fallback record. Hold the agent row to the real bar.
    problems = []
    scrape_failures = []
    for sr, row in agent_rows:
        detail = fetch_json(
            base_url, f"/api/steps/{row['step_execution_id']}/usage"
        )
        # F3.1, the STORED half of the scrape-failure check. The log-line
        # half in verify_run() catches it too, but only while the step's
        # logs are still on record; this one reads the accounting row
        # itself, which is the artifact M13 is actually built on.
        raw = detail.get("raw")
        if isinstance(raw, dict) and raw.get(RAW_SCRAPE_FAILED):
            scrape_failures.append(
                f"agent step {sr['step_index']} '{sr['step_name']}': "
                f"{raw.get(RAW_SCRAPE_ERROR) or 'the scraper found no CLI report'}"
            )
        for field in ("input_tokens", "output_tokens"):
            if detail.get(field) is None:
                problems.append(
                    f"agent step {sr['step_index']} '{sr['step_name']}': "
                    f"{field} is null"
                )
        if not detail.get("cost_source"):
            problems.append(
                f"agent step {sr['step_index']} '{sr['step_name']}': "
                "cost_source is empty"
            )
    if scrape_failures:
        raise SystemExit(
            "FAIL: an agent step's usage row is stamped as a SCRAPE FAILURE "
            "(12.5 F3.1) - the wrapper could not find the CLI's own usage "
            "report, so the recorded cost is not a measurement. A row that "
            "says 'unknown' because the provider reported nothing is fine; "
            "this one says the scraper broke:\n  "
            + "\n  ".join(scrape_failures)
        )
    if problems:
        raise SystemExit(
            "FAIL: the agent step's usage row is empty of numbers (the "
            "wrapper wrote a manifest with no token counts - check "
            "runner_common.usage and the executor's ExecutorResult.usage):\n  "
            + "\n  ".join(problems)
        )

    return (
        f"{len(by_step_run)} StepUsage row(s) incl. "
        f"{len(agent_rows)} agent row(s) with token counts and no scrape "
        "failures"
    )


def verify_runners_idle(base_url: str) -> str:
    """12.5: nothing enqueues to the polling runners any more.

    The runners keep their compose services and replica counts (setting them
    to 0 would be deletion-by-config and would make 12.6's acceptance
    untestable). They sit IDLE - and idleness is asserted here rather than
    assumed, because a silent fallback to the legacy queue is
    indistinguishable from success everywhere else (R1).
    """
    status = fetch_json(base_url, "/api/runners/status")
    queued = status.get("queued_jobs")
    if queued:
        raise SystemExit(
            f"FAIL: {queued} job(s) are sitting in the legacy runner queue. "
            "After 12.5 no default path enqueues - card start/retry, the "
            "playground and agent pipeline steps all run on the control "
            "layer. A queued job means something fell back to the polling "
            "runners (check ExecutionRouter and app/services/agent_run.py)."
        )
    return "runner queue idle (queued_jobs=0)"


def main() -> None:
    base_url = os.environ.get("LAZYAF_BACKEND_URL", DEFAULT_BACKEND_URL)
    run_id = os.environ.get("LAZYAF_PIPELINE_RUN_ID")
    if not run_id:
        raise SystemExit(
            "FAIL: LAZYAF_PIPELINE_RUN_ID is not set - the local "
            "execution path did not inject its env contract"
        )
    raw_index = os.environ.get("LAZYAF_STEP_INDEX")
    self_index = int(raw_index) if raw_index is not None else None
    print(verify_run(base_url, run_id, self_index=self_index))


if __name__ == "__main__":
    main()
