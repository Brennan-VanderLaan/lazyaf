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
  7. (RETIRED) 12.5 asserted `GET /api/runners/status` reported
     queued_jobs == 0 - "nothing enqueues to the polling runners any more".
     That subsystem was deleted in 12.6, so the assertion was deleted WITH
     it, in the same commit, and replaced by 9 below. An assertion left
     pointing at a removed subsystem either 404s the gate or, worse, passes
     vacuously; leaving one behind is how a gate rots. The numbering is kept
     so 8-12 still mean what the design says they mean.
  8. (12.6) every step whose PIPELINE DEFINITION carries a `requires:` block
     ran on the REMOTE lane (executor == 'remote') and, like every other
     passed step, delivered non-marker logs. That second half is the whole
     claim of 12.6's channel split: the step container POSTs its own
     status/logs/test-results/usage to /api/steps/* over HTTP exactly as it
     does locally, and only the runner/assignment concerns travel the
     WebSocket. Non-marker logs on a remote step prove the control layer
     worked from another host with zero new server code.
  9. (12.6) GET /api/runners reports at least one runner with
     status in {'idle','busy'} AND connection == 'websocket'. `connection`
     is stamped from the registry's live socket table, not from the row, so
     an 'idle' row left behind by a crashed process cannot satisfy it. NO
     RUNNERS AT ALL IS A FAILURE, not a vacuous pass: "the fleet is empty"
     and "the fleet is fine" must not look the same to this gate.
 10. (12.6) every remote step's StepExecution carries a non-null runner_id,
     and that id is a runner in the snapshot from 9. `executor == 'remote'`
     alone only says which code path ran; this says a real, currently
     enrolled runner was actually assigned the work.
 11. (12.6) every step NOT carrying `requires:` still has
     executor == 'local'. A global accidental flip to the remote lane is as
     much a regression as a fallback to the legacy queue was, and it would
     otherwise pass 1-10 silently.
 12. (12.6) the agent step rode the remote lane too (it carries `requires:`,
     so 8 covers its routing) AND still has a StepUsage row with real token
     counts (verify_usage below). The usage channel crossing a host boundary
     is the one thing 12.5 could not prove.

A vacuous pass is a failure (R4) in four separate ways: no script/docker step
runs, no agent step run, no REMOTE step run, and no connected runner.

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
# 12.6 lane vocabulary. A step's LANE is decided by its pipeline definition,
# not by what happened: a `requires:` block routes remote (ExecutionRouter's
# "runner-pin" decision, one parser, all step types), everything else routes
# local. The gate re-derives the expectation from the definition and compares,
# so "every step flipped to remote" and "the remote step fell back to local"
# are both regressions rather than both green.
LOCAL_EXECUTOR = "local"
REMOTE_EXECUTOR = "remote"
# A runner in one of these states, holding a live socket, is a runner that can
# take work. `connection` is stamped by the registry from its own socket
# table - a row alone cannot say it (assertion 9).
LIVE_RUNNER_STATES = ("idle", "busy")
LIVE_RUNNER_CONNECTION = "websocket"
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


def step_requires_remote(step: dict) -> bool:
    """True iff this pipeline STEP DEFINITION pins itself to a runner.

    `requires:` lives under the step's `config` and is the single routing
    signal for the remote lane (a top-level `runner_type:` is sugar for
    `requires.runner_type` on script/docker steps and keeps its 12.5
    AI-flavor meaning on agent steps, so it deliberately does NOT route
    remote here). Key PRESENCE is the test, exactly as the router's is: the
    gate must not grow a second, drifting copy of the requirement grammar.
    """
    config = step.get("config") or {}
    return bool(config.get("requires"))


def expected_executor(step: dict) -> str:
    return REMOTE_EXECUTOR if step_requires_remote(step) else LOCAL_EXECUTOR


def step_runner_id(step_run: dict, usage_row: dict | None) -> str | None:
    """StepExecution.runner_id for a step run (assertion 10).

    `GET /api/pipeline-runs/{id}` lifts it onto each step run from that
    step's latest StepExecution; the usage rollup is read as a fallback so
    the gate is not brittle about WHICH read surface exposes the assignment,
    only that one of them does.
    """
    for source in (step_run, usage_row or {}):
        value = source.get("runner_id")
        if value:
            return value
    return None


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
    # 12.6: the EXPECTED lane per step index, re-derived from the pipeline
    # definition. Comparing against a derived expectation (rather than
    # against the constant "local") is what makes assertion 11 possible: a
    # run in which everything flipped to remote fails here just as loudly as
    # one in which the pinned step fell back to local.
    expected = {i: expected_executor(s) for i, s in enumerate(pipeline["steps"])}

    checked = 0
    agents_checked = 0
    remote_checked = 0
    bad = []
    silent = []
    for sr in run["step_runs"]:
        step_type = step_types.get(sr["step_index"], "script")
        if step_type == AGENT_STEP_TYPE:
            # 12.5: agent steps left the legacy queue and are lane-checked
            # exactly like script steps - the whole point of that phase was
            # that there is no longer a difference. 12.6: the dogfood agent
            # step now carries `requires:`, so its expectation is 'remote'
            # and the same comparison covers it (assertion 12).
            agents_checked += 1
        elif step_type not in EXECUTED_STEP_TYPES:
            continue
        else:
            checked += 1
        want = expected.get(sr["step_index"], LOCAL_EXECUTOR)
        if want == REMOTE_EXECUTOR:
            remote_checked += 1
        if sr["executor"] != want:
            bad.append(
                f"step {sr['step_index']} '{sr['step_name']}' ({step_type}) -> "
                f"executor={sr['executor']!r}, expected {want!r} "
                f"({'has' if want == REMOTE_EXECUTOR else 'no'} `requires:` "
                f"block in the pipeline definition)"
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
            "FAIL: steps did not run on the lane their definition asks for (`requires:` -> remote, otherwise local):\n  " + "\n  ".join(bad)
        )
    if not agents_checked:
        raise SystemExit(
            "FAIL: no agent step runs found in this pipeline. The 12.5 dogfood "
            "ratchet requires the zero-cost mock agent step (US-2 continuous "
            "coverage) - a missing agent step means the agent path is not "
            "exercised on push at all (vacuous pass = fail, R4)"
        )
    if not remote_checked:
        raise SystemExit(
            "FAIL: no step of this pipeline carries a `requires:` block, so "
            "the 12.6 REMOTE LANE was not exercised at all. Remote execution "
            "is covered continuously or not at all: without a pinned step "
            "here the WS protocol, the registry, the assignment CAS and the "
            "agent's own workspace provisioning can all break while every "
            "other assertion in this gate still passes (vacuous pass = fail, "
            "R4). Restore the `remote-probe` step - and the `requires:` block "
            "on `mock-agent` - in .lazyaf/pipelines/test-suite.yaml"
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

    # ONE rollup read, shared by both gates below: the usage channel gate
    # and the remote-lane gate ask different questions of the same rows.
    rollup = fetch_json(base_url, f"/api/pipeline-runs/{run['id']}/usage")
    usage_msg = verify_usage(
        base_url, run, step_types, self_index=self_index, rollup=rollup
    )
    remote_msg = verify_remote_lane(base_url, run, expected, rollup)

    return (
        f"OK: {checked} script step run(s) and {agents_checked} agent step "
        f"run(s) ran on the lane their definition asks for "
        f"({remote_checked} remote), passed steps delivered logs, no "
        f"manifest delivery problems, {usage_msg}, {remote_msg}"
    )


def verify_usage(
    base_url: str,
    run: dict,
    step_types: dict,
    self_index: int | None = None,
    rollup: dict | None = None,
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
    if rollup is None:
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


def verify_remote_lane(
    base_url: str, run: dict, expected: dict, rollup: dict
) -> str:
    """12.6 assertions 9 and 10: the remote lane is REAL, not just labelled.

    Assertion 8 (executor == 'remote' on every pinned step) is checked in
    verify_run against the lane derived from the pipeline definition. That
    proves which code path ran. It does NOT prove a runner existed: a
    RemoteExecutor that failed a step with "no runner matched" still writes
    executor='remote', so a fleet of zero would sail through assertion 8
    the moment such a step were made non-fatal.

    Two more things are read here, and both are about the FLEET rather than
    about the step:

      9. GET /api/runners must report at least one runner that is
         status in {idle, busy} AND connection == 'websocket'. `connection`
         is stamped by the registry from its own live-socket table, never
         from the DB row, precisely because a row left behind by a crashed
         backend process still says 'idle'. NO RUNNERS AT ALL IS A FAILURE:
         "the fleet is empty" must not be indistinguishable from "the fleet
         is fine" (R4).

     10. Every remote step's StepExecution carries a non-null runner_id, and
         that id is one of the runners in the snapshot. This is the link
         between "a remote code path ran" and "a specific, currently
         enrolled machine did the work" - the assignment CAS's own output,
         read back through the API.
    """
    snapshot = fetch_json(base_url, "/api/runners")
    if not isinstance(snapshot, list) or not snapshot:
        raise SystemExit(
            "FAIL: GET /api/runners reports NO runners at all. The dogfood "
            "stack runs a `runner-agent` service that enrolls over "
            "/ws/runner; an empty registry means it never connected (check "
            "`docker compose ps runner-agent` and its logs for auth or "
            "protocol-version errors) or that the WS endpoint is not "
            "mounted. An empty fleet is a FAILURE here, never a vacuous "
            "pass (R4)"
        )

    live = [
        r
        for r in snapshot
        if r.get("status") in LIVE_RUNNER_STATES
        and r.get("connection") == LIVE_RUNNER_CONNECTION
    ]
    if not live:
        raise SystemExit(
            "FAIL: no runner is both alive and socket-backed. GET /api/runners "
            f"reports {len(snapshot)} row(s), none with status in "
            f"{list(LIVE_RUNNER_STATES)} AND connection="
            f"'{LIVE_RUNNER_CONNECTION}'. A row that says 'idle' with "
            "connection='none' is a TOMBSTONE - the registry holds no socket "
            "for it - and dispatching to it is exactly the split-brain the "
            "connection field exists to make visible:" + "\n  "
            + "\n  ".join(
                f"{r.get('id')}: status={r.get('status')!r} "
                f"connection={r.get('connection')!r}"
                for r in snapshot
            )
        )

    known_ids = {r.get("id") for r in snapshot}
    # The usage rollup is the second place a step's runner_id can surface
    # (whichever projection carries it, the gate reads it from there).
    usage_by_step_run = {
        row["step_run_id"]: row
        for row in rollup.get("steps", [])
        if row.get("step_run_id")
    }

    unassigned = []
    strangers = []
    assigned_to = set()
    for sr in run["step_runs"]:
        if expected.get(sr["step_index"]) != REMOTE_EXECUTOR:
            continue
        runner_id = step_runner_id(sr, usage_by_step_run.get(sr["id"]))
        if not runner_id:
            unassigned.append(f"step {sr['step_index']} '{sr['step_name']}'")
        elif runner_id not in known_ids:
            strangers.append(
                f"step {sr['step_index']} '{sr['step_name']}' -> "
                f"runner_id={runner_id!r}"
            )
        else:
            assigned_to.add(runner_id)

    if unassigned:
        raise SystemExit(
            "FAIL: a remote step has no StepExecution.runner_id. The "
            "assignment compare-and-swap writes runner_id and status in ONE "
            "transaction, so a step that ran remotely without one means "
            "either the CAS was bypassed or the field never reached this "
            "API (it must appear on the pipeline-run step runs or on the "
            "usage rollup rows):" + "\n  "
            + "\n  ".join(unassigned)
        )
    if strangers:
        raise SystemExit(
            "FAIL: a remote step names a runner the registry has never heard "
            "of. That is a stale or forged assignment, not a completed one:"
            + "\n  "
            + "\n  ".join(strangers)
            + "\n" + f"(known runners: {sorted(i for i in known_ids if i)})"
        )

    return (
        f"{len(live)} socket-backed runner(s) live, remote steps assigned to "
        f"{sorted(assigned_to)}"
    )


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
