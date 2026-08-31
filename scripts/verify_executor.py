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
 13. (14.2) every `agent: openai-harness` StepRun has a StepUsage row with
     provider == 'openai-compatible' and token counts that were SUMMED ACROSS
     TURNS rather than taken from the last response. The mock endpoint reports
     growing per-turn usage (turn N declares 100*N prompt / 20*N completion
     tokens), so with T turns a summing accumulator lands on
     100*T*(T+1)/2 and a last-response-wins bug lands on exactly 100*T. The
     gate asserts the row is STRICTLY GREATER than the largest single turn.
     This is the only genuinely new accounting logic in the milestone and it
     has no other alarm: a harness that silently kept the last turn would
     under-report every self-hosted step forever and every other assertion
     here would still pass.
 14. (14.2/12.5) that row has cost_source == 'gpu-node' and a non-null
     cost_usd. The dogfood endpoints carry a real rate_usd_hour, so the
     gpu-node pricing branch - which 12.5 shipped and stated was "reached only
     by API tests with a hand-built manifest" - is exercised on every push.
 15. (14.2) `SCRAPE_FAILED_LOG_MARKER` appears in NO step's logs for this run.
     verify_run already refuses a run containing it; 15 restates it as a
     named, independently testable assertion because the harness is the first
     executor whose usage comes from N accumulated responses rather than one
     scraped report, and "no turn reported usage" is a shape only it can
     produce.
 16. (14.2) the FORCED-TEXT harness step succeeded and its usage row records
     raw.harness.mode == 'text' with a `malformed_responses` key present.
     The no-tools fallback protocol is the part most likely to rot, because
     nothing exercises it once a tool-capable model is plugged in.
 17. (14.1) GET /api/model-endpoints reports the endpoint each harness step
     named, the tools-mode one with probe_status == 'ok', all of them probed
     inside PROBE_TTL_SECONDS. An endpoint whose capability record went stale
     or unprobed is one a step should have refused to dispatch against.
 18. (14.2, section 5.2 invariant) NO StepUsage row in this run carries
     cost_source == 'cli-reported' together with provider ==
     'openai-compatible'. That value is what the board reads as "the provider
     billed us this amount", and no self-hosted endpoint can make that claim.

 19. (12.8) every StepRun's `step_index` equals its step's POSITION in the
     graph definition's `steps` mapping. `step_index` survives the v1 array
     retirement as the ADDRESS of a step: the execution key
     (`{run_id}:{step_index}:{step_run_id}`), LAZYAF_STEP_INDEX, the
     step_update / step_log websocket frames the UI renders by index, and the
     state machine's completion bookkeeping all key on it - while the
     executor DERIVES it from `list(steps_dict.keys()).index(step_id)`.
     Nothing else in the tree asserts the two agree, and a disagreement is
     silent in both directions: log lines land on the wrong step in the UI,
     and an idempotency key collides with a different step's.

A vacuous pass is a failure (R4) in seven separate ways: no graph step
definitions at all, no script/docker step runs, no agent step run, no REMOTE
step run, no connected runner, no HARNESS step run, and no forced-text
harness step run.

THE DEFINITION IS THE GRAPH (12.8). `PipelineRead.steps` - the v1 array - has
left the wire. The array survives only as the AUTHORING format at the
repo-YAML edge; `GET /api/pipelines/{id}` serves `steps_graph`, the definition
the executor actually ran. So every per-step expectation in this gate is a
lookup into `steps_graph["steps"]`, a MAPPING KEYED BY STEP ID, correlated
with the run's StepRuns through `StepRun.step_id`. The old
`enumerate(pipeline["steps"])` correlation could not survive the retirement:
a graph's steps have no array positions to enumerate, and an id is what the
author actually wrote.

Env contract (injected into every step container by LocalExecutor and by the
remote lane's `build_execute_step_config`; the backend URL default matches
settings.container_backend_url):
  LAZYAF_PIPELINE_RUN_ID  - required; the run to verify
  LAZYAF_STEP_ID          - optional; this step's own GRAPH NODE ID (exempt
                            from the log-delivery and usage checks). Absent
                            when the StepRun names no graph node, in which
                            case NOTHING is exempted and this gate fails on
                            its own still-streaming logs - loudly, which is
                            the correct direction for a missing identity.
  LAZYAF_BACKEND_URL      - optional; defaults to http://backend:8000

LAZYAF_STEP_INDEX is still injected and still means what it always meant, but
this gate no longer reads it: keying the gate's OWN identity on a graph's
`list(steps_dict.keys())` insertion order was the single most fragile thing in
the ratchet, and assertion 19 now watches that ordering from the outside
instead of depending on it.

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

# -----------------------------------------------------------------------------
# 14.x harness lane (assertions 13-18)
# -----------------------------------------------------------------------------
# The agent vocabulary for a self-hosted OpenAI-compatible endpoint. A step's
# lane is derived from its PIPELINE DEFINITION here exactly as the local/remote
# expectation is: `config.agent == 'openai-harness'`.
HARNESS_AGENT = "openai-harness"
# UsageProvider.OPENAI_COMPATIBLE - constant for this executor.
HARNESS_USAGE_PROVIDER = "openai-compatible"
# UsageCostSource values. `gpu-node` is what the 12.5 pricing path writes when
# a node rate resolves; `cli-reported` is the one a harness row may NEVER carry.
GPU_NODE_COST_SOURCE = "gpu-node"
CLI_REPORTED_COST_SOURCE = "cli-reported"
# Capability records older than this are stale (probe.PROBE_TTL_SECONDS).
PROBE_TTL_SECONDS = 86_400

# THE MOCK ENDPOINT'S PER-TURN TOKEN LAW. Turn N of any scenario in
# tdd/shared/mock_openai declares prompt_tokens = 100*N and
# completion_tokens = 20*N, so over T turns:
#
#     a SUMMING accumulator reports  100 * T*(T+1)/2   /  20 * T*(T+1)/2
#     a LAST-RESPONSE-WINS bug reports        100 * T   /          20 * T
#
# and the first is strictly larger for every T >= 2. Assertion 13 is that
# inequality. These two integers are DUPLICATED from
# tdd/shared/mock_openai/scenarios.py because this script is stdlib-only and
# runs in a bare step container that cannot import `tdd`; the duplication is
# pinned by tdd/unit/scripts/test_verify_executor.py, which imports BOTH and
# asserts they are equal (R3).
MOCK_PROMPT_TOKENS_PER_TURN = 100
MOCK_COMPLETION_TOKENS_PER_TURN = 20
# One turn is the degenerate case: summed and last-response are the same
# number, so the inequality proves nothing. The gate requires a real loop.
MIN_HARNESS_TURNS = 2


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


def graph_step_definitions(pipeline: dict) -> dict:
    """`{step_id: step definition}` from a pipeline's GRAPH definition.

    THE ONE READ of the pipeline definition (12.8). It returns the mapping
    every other expectation in this gate is derived from, and it is the only
    place that knows the shape `GET /api/pipelines/{id}` serves.

    AN EMPTY OR MISSING DEFINITION IS A FAILURE, NOT A DEFAULT. Every
    per-step lookup below carries a fallback - `.get(step_id, "script")`,
    `.get(step_id, LOCAL_EXECUTOR)` - because a definition the gate did not
    write may legitimately omit a key. Those fallbacks are safe only while
    the mapping is real: over an EMPTY mapping every step becomes "expected
    local, expected script", assertions 8 and 11 stop being able to fail, and
    the gate prints OK over any run at all. That is the exact vacuous pass R4
    forbids, and it is the shape the retirement of `PipelineRead.steps` could
    have introduced in silence - the field simply stops arriving and
    `pipeline.get("steps", [])` becomes `[]` forever. So the absence is
    checked here, once, and it is fatal.
    """
    graph = pipeline.get("steps_graph")
    steps = graph.get("steps") if isinstance(graph, dict) else None
    if not isinstance(steps, dict) or not steps:
        raise SystemExit(
            f"FAIL: pipeline {pipeline.get('id')!r} carries NO graph step "
            f"definitions (steps_graph={graph!r}). Every per-step "
            "expectation in this gate - the step's type, its lane, its "
            "harness mode, the endpoint it names - is derived from the "
            "definition, so an empty one would make the gate report 'all "
            "local, all script' and pass over ANY run whatsoever (vacuous "
            "pass = fail, R4). Either the pipeline never materialized, or "
            "its definition failed to convert and the row carries a "
            "`definition_error` - GET /api/pipelines/{id} reports it"
        )
    return steps


def step_label(step_run: dict) -> str:
    """How a StepRun is named in every failure message.

    The GRAPH NODE ID first, because that is the handle an author can act on
    (it is what they wrote in the YAML and what a breakpoint keys on), then
    the display name.
    """
    return f"step '{step_run.get('step_id')}' '{step_run.get('step_name')}'"


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


def step_harness_endpoint(step: dict) -> str | None:
    """The endpoint name an `agent: openai-harness` STEP DEFINITION names.

    Two legal spellings, both resolved by the ONE backend resolver
    (`resolve_step_endpoint`): an explicit `endpoint:` key, or the
    `model: "endpoint:<name>"` sugar every model picker already emits. The
    gate reads the definition, never the outcome, so "the step silently ran
    against a different endpoint" is a detectable regression.
    """
    config = step.get("config") or {}
    if (config.get("agent") or "") != HARNESS_AGENT:
        return None
    explicit = config.get("endpoint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    model = config.get("model")
    if isinstance(model, str) and model.startswith("endpoint:"):
        return model[len("endpoint:"):].strip() or None
    return None


def step_harness_mode(step: dict) -> str:
    """The harness LOOP SHAPE this step pins: 'tools', 'text' or 'auto'."""
    config = step.get("config") or {}
    harness = config.get("harness")
    if isinstance(harness, dict):
        mode = harness.get("mode")
        if isinstance(mode, str) and mode:
            return mode
    return "auto"


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


def verify_run(base_url: str, run_id: str, self_id: str | None = None) -> str:
    """Verify executor routing, log delivery and the usage channel.

    Returns an OK message on success; raises SystemExit with a FAIL
    message otherwise (vacuous pass = fail, R4).
    """
    run = fetch_json(base_url, f"/api/pipeline-runs/{run_id}")
    pipeline = fetch_json(base_url, f"/api/pipelines/{run['pipeline_id']}")
    steps_by_id = graph_step_definitions(pipeline)
    step_types = {
        step_id: s.get("type", "script") for step_id, s in steps_by_id.items()
    }
    # 12.6: the EXPECTED lane per step, re-derived from the pipeline
    # definition. Comparing against a derived expectation (rather than
    # against the constant "local") is what makes assertion 11 possible: a
    # run in which everything flipped to remote fails here just as loudly as
    # one in which the pinned step fell back to local.
    expected = {
        step_id: expected_executor(s) for step_id, s in steps_by_id.items()
    }
    # 12.8 assertion 19: a step's POSITION in the graph's steps mapping is
    # the number the executor stamps onto StepRun.step_index, and therefore
    # the number the execution key, LAZYAF_STEP_INDEX and every step_update /
    # step_log websocket frame address the step by.
    positions = {step_id: i for i, step_id in enumerate(steps_by_id)}

    checked = 0
    agents_checked = 0
    remote_checked = 0
    markers = 0
    bad = []
    silent = []
    misindexed = []
    strangers = []
    for sr in run["step_runs"]:
        step_id = sr.get("step_id")
        if step_id is None:
            # A MARKER row, not an executed step. `_trigger_card` records one
            # (step_id deliberately NULL, so a fix-card marker can never be
            # mistaken for the step that spawned it) and so does
            # `_verify_graph_coverage`. A marker names no definition, spawned
            # no container and therefore has no lane, no type and no usage
            # row to check it against. Counted, so it is visible in the OK
            # line rather than merely absent from it.
            markers += 1
            continue
        if step_id not in steps_by_id:
            # The run executed a step the CURRENT definition does not
            # contain. Every expectation below is a lookup into that
            # definition, so continuing would mean checking this step
            # against a fallback rather than against anything.
            strangers.append(step_label(sr))
            continue
        if sr.get("step_index") != positions[step_id]:
            misindexed.append(
                f"{step_label(sr)} ran at step_index="
                f"{sr.get('step_index')!r}, but its step is at position "
                f"{positions[step_id]} in the graph definition"
            )
        step_type = step_types.get(step_id, "script")
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
        want = expected.get(step_id, LOCAL_EXECUTOR)
        if want == REMOTE_EXECUTOR:
            remote_checked += 1
        if sr["executor"] != want:
            bad.append(
                f"{step_label(sr)} ({step_type}) -> "
                f"executor={sr['executor']!r}, expected {want!r} "
                f"({'has' if want == REMOTE_EXECUTOR else 'no'} `requires:` "
                f"block in the pipeline definition)"
            )
        # 12.3 control-path probe: passed steps (other than this one) must
        # have NON-marker logs on record - in control mode those only exist
        # if the in-container runtime's POST /api/steps/{id}/logs batches
        # landed. Backend-appended '[lazyaf] ' marker lines don't count.
        if (
            step_id != self_id
            and sr.get("status") == "passed"
            and not has_delivered_logs(sr.get("logs"))
        ):
            silent.append(
                f"{step_label(sr)} passed "
                f"with EMPTY logs (no non-marker log lines)"
            )

    if strangers:
        raise SystemExit(
            "FAIL: this run executed step(s) the pipeline's CURRENT graph "
            "definition does not contain. The gate correlates a StepRun to "
            "its definition through `StepRun.step_id`, so there is nothing "
            "left to check these against - and a definition that changed "
            "under a live run is itself the finding (a second push "
            "re-materialized the pipeline mid-run, or a step id was renamed "
            f"without a new run). Known step ids: {sorted(steps_by_id)}:\n  "
            + "\n  ".join(strangers)
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
    # 12.8 assertion 19, and it is a live regression like `bad` is, so it is
    # reported before the ratchet-completeness checks below.
    if misindexed:
        raise SystemExit(
            "FAIL: a StepRun's step_index does not match its step's POSITION "
            "in the graph definition. `step_index` survives the v1 array "
            "retirement as the ADDRESS of a step - the execution key "
            "('{run_id}:{step_index}:{step_run_id}'), LAZYAF_STEP_INDEX, the "
            "step_update / step_log websocket frames the UI renders, and the "
            "state machine's completion bookkeeping all key on it - while "
            "the executor DERIVES it from "
            "`list(steps_dict.keys()).index(step_id)`. When the two "
            "disagree, log lines land on a different step in the UI and an "
            "idempotency key collides with a different step's, and both are "
            "silent. Nothing else in the tree watches this:\n  "
            + "\n  ".join(misindexed)
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
                manifest_problems.append(f"{step_label(sr)}: {line.strip()}")
            elif SCRAPE_FAILED_LOG_MARKER in line:
                scrape_problems.append(f"{step_label(sr)}: {line.strip()}")
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
        base_url, run, step_types, self_id=self_id, rollup=rollup
    )
    remote_msg = verify_remote_lane(base_url, run, expected, rollup)
    harness_msg = verify_harness_lane(base_url, run, pipeline, rollup)

    return (
        f"OK: {checked} script step run(s) and {agents_checked} agent step "
        f"run(s) ran on the lane their definition asks for "
        f"({remote_checked} remote), each at its graph position, passed "
        f"steps delivered logs, no manifest delivery problems, {usage_msg}, "
        f"{remote_msg}, {harness_msg}"
        + (f" [{markers} marker step run(s) skipped]" if markers else "")
    )


def verify_usage(
    base_url: str,
    run: dict,
    step_types: dict,
    self_id: str | None = None,
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
    therefore cannot POST usage. It is exempted by its GRAPH NODE ID, taken
    from LAZYAF_STEP_ID - never by guesswork, and (from 12.8) never by an
    array position the graph no longer has.
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
        step_id = sr.get("step_id")
        if step_id is None:
            # Marker row (see verify_run): it spawned no container, so there
            # is no control runtime that could ever have POSTed usage for it.
            continue
        if step_id == self_id:
            continue  # stdout-mode gate step: no control runtime, no usage
        if sr.get("status") != "passed":
            continue  # still running / failed: its usage POST has not landed
        row = by_step_run.get(sr["id"])
        if row is None:
            missing.append(
                f"{step_label(sr)} ({step_types.get(step_id, 'script')})"
            )
            continue
        if step_types.get(step_id) == AGENT_STEP_TYPE:
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
                f"agent {step_label(sr)}: "
                f"{raw.get(RAW_SCRAPE_ERROR) or 'the scraper found no CLI report'}"
            )
        for field in ("input_tokens", "output_tokens"):
            if detail.get(field) is None:
                problems.append(f"agent {step_label(sr)}: {field} is null")
        if not detail.get("cost_source"):
            problems.append(f"agent {step_label(sr)}: cost_source is empty")
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
        if expected.get(sr.get("step_id")) != REMOTE_EXECUTOR:
            continue
        runner_id = step_runner_id(sr, usage_by_step_run.get(sr["id"]))
        if not runner_id:
            unassigned.append(step_label(sr))
        elif runner_id not in known_ids:
            strangers.append(f"{step_label(sr)} -> runner_id={runner_id!r}")
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


def _harness_record(detail: dict) -> dict:
    """`raw.harness` from a StepUsage detail, or {} when it is absent."""
    raw = detail.get("raw")
    if not isinstance(raw, dict):
        return {}
    record = raw.get("harness")
    return record if isinstance(record, dict) else {}


def verify_harness_lane(
    base_url: str, run: dict, pipeline: dict, rollup: dict
) -> str:
    """14.x assertions 13-18: the self-hosted lane is REAL and HONESTLY PRICED.

    Every question here is asked of the PIPELINE DEFINITION first (which steps
    claim to be `agent: openai-harness`, which endpoint each names, which one
    pins `harness.mode: text`) and only then of what happened. That is the same
    discipline assertion 11 uses, and it is what makes "the harness steps
    silently disappeared from the pipeline" a failure rather than a green run
    with nothing in it.
    """
    harness_steps = {
        step_id: step
        for step_id, step in graph_step_definitions(pipeline).items()
        if (step.get("config") or {}).get("agent") == HARNESS_AGENT
    }
    if not harness_steps:
        raise SystemExit(
            "FAIL: no `agent: openai-harness` step in this pipeline, so the "
            "14.2 AGENT HARNESS was not exercised at all. The harness is the "
            "only executor LazyAF drives itself - the loop, the tool sandbox, "
            "the token accumulator, the no-tools fallback and the gpu-node "
            "pricing path have no other continuous coverage, and a GPU is "
            "deliberately not required to run them (the mock endpoint is a "
            "compose service). A vacuous pass is a failure (R4). Restore the "
            "`harness-probe` and `harness-probe-notools` steps in "
            ".lazyaf/pipelines/test-suite.yaml"
        )

    # 15. The scrape marker must appear NOWHERE in this run. For the harness
    # this specifically means "no turn of any step reported a usage block",
    # which is the shape only an N-turn accumulator can produce.
    scrape_lines = [
        f"{step_label(sr)}: {line.strip()}"
        for sr in run["step_runs"]
        for line in (sr.get("logs") or "").splitlines()
        if SCRAPE_FAILED_LOG_MARKER in line
    ]
    if scrape_lines:
        raise SystemExit(
            "FAIL (13-18/15): a step announced a usage scrape failure. For an "
            "openai-harness step that means the endpoint returned no `usage` "
            "block on ANY turn, so the tokens recorded for it are an absence "
            "rather than a measurement:\n  " + "\n  ".join(scrape_lines)
        )

    by_step_run = {
        row["step_run_id"]: row
        for row in rollup.get("steps", [])
        if row.get("step_run_id")
    }

    # 18. Run-wide, every row - not just the harness ones. A self-hosted
    # provider row that claims `cli-reported` is claiming a bill nobody sent.
    liars = [
        f"step_run {row.get('step_run_id')} provider={row.get('provider')!r} "
        f"cost_source={row.get('cost_source')!r}"
        for row in rollup.get("steps", [])
        if row.get("provider") == HARNESS_USAGE_PROVIDER
        and row.get("cost_source") == CLI_REPORTED_COST_SOURCE
    ]
    if liars:
        raise SystemExit(
            "FAIL (18): a StepUsage row claims provider "
            f"'{HARNESS_USAGE_PROVIDER}' with cost_source "
            f"'{CLI_REPORTED_COST_SOURCE}'. That value means 'the provider "
            "billed us this amount' and no self-hosted endpoint can make that "
            "claim - a node-priced figure is an ESTIMATE from a rate and a "
            "duration, and the two must never be mistaken for each other on "
            "the board:\n  " + "\n  ".join(liars)
        )

    problems = []
    text_mode_verified = []
    endpoints_named = {}
    checked = 0

    # Sorted by step id for a stable report: the graph's mapping order is
    # insertion order, which is meaningful (assertion 19) but is not the
    # order a human scans a failure list in.
    for step_id, step in sorted(harness_steps.items()):
        name = step.get("name") or step_id
        label = f"step '{step_id}' '{name}'"
        endpoint_name = step_harness_endpoint(step)
        mode = step_harness_mode(step)
        if endpoint_name:
            endpoints_named.setdefault(endpoint_name, mode)

        runs = [sr for sr in run["step_runs"] if sr.get("step_id") == step_id]
        if not runs:
            problems.append(f"{label}: the harness step never ran")
            continue
        step_run = runs[-1]
        if step_run.get("status") != "passed":
            problems.append(
                f"{label}: status={step_run.get('status')!r}, "
                "expected 'passed'"
            )
            continue

        row = by_step_run.get(step_run["id"])
        if row is None:
            problems.append(
                f"{label}: no StepUsage row (the harness wrote no usage "
                "manifest, or it never reached POST /api/steps/{id}/usage)"
            )
            continue
        detail = fetch_json(
            base_url, f"/api/steps/{row['step_execution_id']}/usage"
        )

        # 13a. provider
        if detail.get("provider") != HARNESS_USAGE_PROVIDER:
            problems.append(
                f"{label}: provider="
                f"{detail.get('provider')!r}, expected "
                f"{HARNESS_USAGE_PROVIDER!r}"
            )

        record = _harness_record(detail)
        turns = record.get("turns")
        input_tokens = detail.get("input_tokens")
        output_tokens = detail.get("output_tokens")

        if not isinstance(turns, int) or turns < MIN_HARNESS_TURNS:
            problems.append(
                f"{label}: raw.harness.turns={turns!r}. The gate "
                f"needs at least {MIN_HARNESS_TURNS} turns for the summation "
                "check to mean anything - with one turn 'summed' and 'last "
                "response' are the same number"
            )
        elif input_tokens is None or output_tokens is None:
            problems.append(
                f"{label}: input_tokens={input_tokens!r} "
                f"output_tokens={output_tokens!r} - a harness step that ran "
                f"{turns} turns against a usage-reporting endpoint must have "
                "both"
            )
        else:
            # 13b. THE SUMMATION CHECK.
            largest_in = MOCK_PROMPT_TOKENS_PER_TURN * turns
            largest_out = MOCK_COMPLETION_TOKENS_PER_TURN * turns
            if input_tokens <= largest_in or output_tokens <= largest_out:
                problems.append(
                    f"{label}: usage was NOT summed across "
                    f"turns. Over {turns} turns the mock endpoint's largest "
                    f"single turn declares {largest_in} prompt / {largest_out} "
                    f"completion tokens; this row records {input_tokens} / "
                    f"{output_tokens}, which is at or below it. A summing "
                    f"accumulator would record "
                    f"{MOCK_PROMPT_TOKENS_PER_TURN * turns * (turns + 1) // 2}"
                    f" / "
                    f"{MOCK_COMPLETION_TOKENS_PER_TURN * turns * (turns + 1) // 2}"
                    ". Check the per-turn accumulator in "
                    "runner_common/harness (it must ADD every response's "
                    "usage, not replace)"
                )

        # 14. gpu-node pricing.
        if detail.get("cost_source") != GPU_NODE_COST_SOURCE:
            problems.append(
                f"{label}: cost_source="
                f"{detail.get('cost_source')!r}, expected "
                f"{GPU_NODE_COST_SOURCE!r}. The dogfood endpoints carry a real "
                "rate_usd_hour, so the 12.5 gpu-node pricing branch must have "
                "priced this row (check usage_pricing.resolve_node_rate and "
                "that gpu_node_id/gpu_fraction reached the container env)"
            )
        elif detail.get("cost_usd") is None:
            problems.append(
                f"{label}: cost_source is "
                f"{GPU_NODE_COST_SOURCE!r} but cost_usd is null - a priced "
                "source with no price is the one combination that means "
                "nothing"
            )

        # 16. the forced-text step recorded the fallback protocol.
        if mode == "text":
            if record.get("mode") != "text":
                problems.append(
                    f"{label}: pinned harness.mode='text' but "
                    f"raw.harness.mode={record.get('mode')!r}. The step did "
                    "not run the no-tools fallback protocol it was told to"
                )
            elif "malformed_responses" not in record:
                problems.append(
                    f"{label}: raw.harness has no "
                    "'malformed_responses' key. The fallback parser must "
                    "record its own miss count (0 is a fine value; the KEY "
                    "missing means the path did not account for itself)"
                )
            else:
                text_mode_verified.append(name)
        checked += 1

    if not text_mode_verified and not problems:
        problems.append(
            "no step of this pipeline pins `harness: {mode: text}`. The "
            "no-tools FALLBACK PROTOCOL is the part most likely to rot - "
            "nothing exercises it once a tool-capable model is plugged in - "
            "so it runs on every push or it is not covered at all (vacuous "
            "pass = fail, R4). Restore the `harness-probe-notools` step in "
            ".lazyaf/pipelines/test-suite.yaml"
        )

    # 17. the capability records the steps dispatched against.
    if endpoints_named:
        registry = fetch_json(base_url, "/api/model-endpoints")
        by_name = {
            e.get("name"): e for e in registry if isinstance(e, dict)
        }
        for endpoint_name, mode in sorted(endpoints_named.items()):
            endpoint = by_name.get(endpoint_name)
            if endpoint is None:
                problems.append(
                    f"endpoint '{endpoint_name}' is named by a harness step "
                    "but GET /api/model-endpoints does not report it "
                    f"(known: {sorted(n for n in by_name if n)})"
                )
                continue
            capabilities = endpoint.get("capabilities") or {}
            status = capabilities.get("probe_status") or endpoint.get("probe_status")
            age = capabilities.get("probe_age_seconds")
            if age is None:
                age = endpoint.get("probe_age_seconds")
            if mode != "text" and status != "ok":
                problems.append(
                    f"endpoint '{endpoint_name}': probe_status={status!r}, "
                    "expected 'ok' for the tools-mode dogfood endpoint (the "
                    "mock server answers the tool probe, reports usage and "
                    "streams, so anything else means the probe or the mock "
                    "regressed)"
                )
            elif status == "unprobed":
                problems.append(
                    f"endpoint '{endpoint_name}': probe_status is 'unprobed', "
                    "which dispatch is supposed to REFUSE - a step ran "
                    "against an endpoint whose capabilities are unknown"
                )
            if age is None or age >= PROBE_TTL_SECONDS:
                problems.append(
                    f"endpoint '{endpoint_name}': probe_age_seconds={age!r}, "
                    f"expected a fresh record under {PROBE_TTL_SECONDS}s"
                )

    if problems:
        raise SystemExit(
            "FAIL: the 14.x self-hosted harness lane did not behave the way "
            "the pipeline definition says it must:\n  " + "\n  ".join(problems)
        )

    return (
        f"{checked} openai-harness step run(s) summed their tokens across "
        f"turns, priced node-side, against {len(endpoints_named)} probed "
        f"endpoint(s) (forced-text: {', '.join(text_mode_verified) or 'none'})"
    )


def main() -> None:
    base_url = os.environ.get("LAZYAF_BACKEND_URL", DEFAULT_BACKEND_URL)
    run_id = os.environ.get("LAZYAF_PIPELINE_RUN_ID")
    if not run_id:
        raise SystemExit(
            "FAIL: LAZYAF_PIPELINE_RUN_ID is not set - the local "
            "execution path did not inject its env contract"
        )
    # 12.8: the gate identifies ITSELF by graph node id. LAZYAF_STEP_INDEX is
    # still injected and still addresses the step everywhere else, but this
    # one read moved: the index is derived from the graph's key insertion
    # order, and a gate whose self-exemption depends on that ordering is a
    # gate that silently starts exempting a different step when the order
    # changes. An empty or absent value is None - nothing is exempted, and
    # the gate fails on its own still-streaming logs rather than quietly
    # exempting nobody-knows-which step.
    self_id = os.environ.get("LAZYAF_STEP_ID") or None
    print(verify_run(base_url, run_id, self_id=self_id))


if __name__ == "__main__":
    main()
