# Retiring the v1 Array Pipeline Format — Implementation Plan

**Status**: ready to brief · **Owner decision recorded**: 2026‑08‑30 · **Standing rules**: R1 nothing dark · R2 delete only after acceptance · R3 one source of truth per wire contract · R4 no fake green · R5 async‑first · R6 real seams · R7 dogfood ratchet · R8 UI ships a Playwright spec

Seven recon lanes reported. This plan resolves every open question they raised, in the direction stated below, and turns the result into an ordered, parallelisable wave. Where I depart from a lane's recommendation I say so and why.

---

## 1. The capability gap, resolved

### 1.1 The problem, restated exactly

`PipelineStepConfig` (v1) carries `on_success` / `on_failure` over a five‑member vocabulary:

```python
STEP_ACTIONS = ("next", "stop")
STEP_ACTION_PREFIXES = ("trigger:pipeline:", "trigger:", "merge:")
```

`PipelineStepV2` (the graph node) has neither field. The graph expresses control flow *only* through `PipelineEdge.condition ∈ {success, failure, always}`. `array_to_graph` therefore emits an edge **only** when the action is literally `"next"`, and silently discards `trigger:` and `merge:`. At the executor, `_handle_action` is the only implementation of `_trigger_card`, `_trigger_pipeline` and `_merge_branch`, and it is reachable only from the array branch.

Deleting the array by subtraction would silently remove card auto‑fix and pipeline chaining. That is the R1 violation the whole plan exists to avoid.

### 1.2 The decision

**The v1 vocabulary is two things wearing one coat: FLOW and EFFECT. Split them. Flow stays in edges. Effect becomes a list of terminal actions on the node, keyed by the same condition vocabulary the edges already use.**

Concretely, `next` and `stop` are flow and become (respectively) an edge and the absence of one. `merge:{branch}` and `trigger:{card_id}` are effects and become node actions. `trigger:pipeline:{id}` is **retired outright** (§1.5).

This was recon option (a) from lane 1 (F5), and it is the only option that both preserves the capability and removes the defect. I take lane 1's staged (c)→(a) advice but land (a) in the *first* phase rather than shipping a bare refusal first: a refusal with no target syntax to point at just tells the user their pipeline is now unrepresentable, and the phase ordering below (§2) means nothing is removed before the replacement exists, so the staging that (c) bought is bought by the phases instead.

### 1.3 The schema change, written out

In `backend/app/schemas/pipeline.py`, immediately after `EdgeCondition`:

```python
#: Terminal actions a NODE may fire when it completes. Deliberately NOT the
#: v1 vocabulary: `next` and `stop` are FLOW and flow lives on edges. What is
#: left is pure side effect, which is why these are a LIST - v1 could not say
#: "merge and also spawn a fix card", because it had one string for both the
#: effect and the continuation.
TERMINAL_ACTION_PREFIXES = ("trigger:", "merge:")

_TERMINAL_VOCABULARY = "'trigger:{card_id}' or 'merge:{branch}'"


def describe_terminal_action(action: Any) -> str | None:
    """None when `action` is a dispatchable NODE action, else why it is not.

    The graph twin of `describe_step_action` (pipeline_executor), and the
    SINGLE definition of the node-action vocabulary (R3): the schema
    validator below and the executor's dispatcher both call this, so a typo
    is a 422 at the boundary and a named run failure at run time - never a
    silent no-op.
    """
    if not isinstance(action, str):
        return (
            f"step action must be a string, got {type(action).__name__} "
            f"({action!r}); valid node actions are {_TERMINAL_VOCABULARY}"
        )
    if action in ("next", "stop"):
        return (
            f"{action!r} is control FLOW, not a node action; express it with "
            f"a graph edge (or the absence of one). Valid node actions are "
            f"{_TERMINAL_VOCABULARY}"
        )
    if action.startswith("trigger:pipeline:"):
        return (
            "'trigger:pipeline:' is retired (12.8; it had no users and no "
            "execution test). Chain pipelines with a card_complete or push "
            f"trigger. Valid node actions are {_TERMINAL_VOCABULARY}"
        )
    for prefix in TERMINAL_ACTION_PREFIXES:
        if action.startswith(prefix):
            if action[len(prefix):].strip():
                return None
            return (
                f"node action {action!r} names {prefix!r} with an empty "
                f"target; valid node actions are {_TERMINAL_VOCABULARY}"
            )
    return (
        f"unknown node action {action!r}; valid node actions are "
        f"{_TERMINAL_VOCABULARY}"
    )


class StepActions(BaseModel):
    """Side effects a node fires when it completes.

    Keyed by the SAME condition vocabulary the edges use (EdgeCondition), so
    the system has one notion of "when" (R3). Actions are effects ONLY: they
    never decide what runs next and they never complete the run. The
    executor fires them, then continues its normal fan-out and its normal
    `_check_all_steps_passed` verdict.
    """
    success: list[str] = []
    failure: list[str] = []
    always: list[str] = []

    @field_validator("success", "failure", "always")
    @classmethod
    def _closed_vocabulary(cls, v: list[str]) -> list[str]:
        for action in v:
            problem = describe_terminal_action(action)
            if problem is not None:
                raise ValueError(problem)
        return v
```

and on the node:

```python
class PipelineStepV2(BaseModel):
    id: str
    name: str
    type: StepType
    config: dict[str, Any] = {}
    position: Optional[PipelineNodePosition] = None
    timeout: int = 300
    continue_in_context: bool = False
    actions: StepActions = Field(default_factory=StepActions)   # NEW
```

**Field naming is deliberate and non‑negotiable.** `backend/app/routers/pipelines.py::export_pipeline_yaml` *already* writes `on_success` on graph steps meaning "the id of the node this success edge points at". Adding `on_success`/`on_failure` to `PipelineStepV2` would put two vocabularies behind one key on day one — an R3 violation baked in. Namespacing under `actions.` removes the collision entirely and makes the condition vocabulary shared with `EdgeCondition` rather than parallel to it.

### 1.4 The executor change, written out

In `_handle_graph_step_complete` (renamed `_handle_step_complete` at P5), between the completed/active bookkeeping commit and `get_downstream_edges`:

```python
        # Fire this node's terminal actions BEFORE fanning out. A `merge:`
        # must land before a downstream step reads the merged branch, and an
        # action that cannot be performed must fail the run rather than let
        # the graph carry on over a side effect that did not happen (R1).
        step_actions = (steps_dict.get(completed_step_id) or {}).get("actions") or {}
        condition = "success" if step_success else "failure"
        pending = list(step_actions.get(condition) or []) + list(step_actions.get("always") or [])
        for action in pending:
            ok = await self._run_terminal_action(
                db, pipeline_run, repo, completed_step_id, step_run, action
            )
            if not ok:
                return          # _run_terminal_action has already FAILED the run
```

`_run_terminal_action` validates with `describe_terminal_action`, then dispatches to `_trigger_card` / `_merge_branch` **re‑keyed off `(step_id, step_run)` instead of `(steps, current_step)`**. Three properties are mandatory and each is a test:

1. **An action never completes the pipeline.** The three unconditional `await self._complete_pipeline(..., success=True)` calls inside `_merge_branch` and `_execute_step`'s past‑the‑end branch are *deleted, not carried over*. Recon F7 identified these as three false greens the array path has and the graph path does not; copying `_merge_branch` wholesale would re‑import them. The verdict is `_check_all_steps_passed` + `_verify_graph_coverage`, always.
2. **A failed action fails the run, loudly, naming step id + action + reason.** This preserves `_merge_branch`'s existing "unresolvable branch fails the run loudly" behaviour (which has a test) and extends it to every action.
3. **`_resolve_merge_source_branch` loses its `.where(StepRun.step_index == current_step).first()` lookup** and takes the `step_run` it is already holding. That removes the reason `_trigger_card` had to write a deliberately job‑less marker StepRun ("a second row at this index claiming the step's job would poison `_resolve_merge_source_branch`"). The marker row stays — it is how a triggered card shows up in the run — but the comment explaining the hack goes with the hack.

`_trigger_card`'s documented KNOWN LIMITATION ("when the triggering step is the LAST one, continuing past it completes the run PASSED even though the action fired from `on_failure`") **evaporates** under this split, because continuation is now an edge and the verdict is `_check_all_steps_passed`.

### 1.5 What is retired, and the R2 acceptance note for it

**`trigger:pipeline:{pipeline_id}` is retired.** Evidence, from lane 1 F6 and lane 2 W1, verified: zero pipeline definitions in the tree use it; zero tests execute `_trigger_pipeline` (the two that mention it parametrize the *string* through vocabulary/dispatch mocks and never reach the body); no writer produces it. Porting an untested body to the graph path with no test to port is how a phase ships uncovered.

R2 requires acceptance before deletion. Acceptance here is: (a) the string is refused with a message naming the replacement (`describe_terminal_action`, above), (b) `PUBLIC_TRIGGER_TYPES` keeps `"pipeline"` so historical `PipelineRun.trigger_type` rows still read, (c) `backend/app/mcp/server.py` and `backend/app/schemas/lazyaf_yaml.py` stop advertising it in the same commit, (d) the retirement is written into PLAN.md's decision log. Chaining remains expressible through `card_complete` / `push` triggers, which are untouched.

**`merge:` and `trigger:` survive as node actions.** They are advertised to users in `mcp/server.py` and `lazyaf_yaml.py` Field descriptions and `frontend/src/lib/api/types.ts`; `trigger:{card_id}` is the pipeline‑driven "AI fixes the failing step" loop and has an executing test; `merge:{branch}` has three. Zero usage measures adoption, not intent — the UI has never been able to author either, so "nobody uses it" is largely "nobody could discover it."

**Note, so nobody deletes the wrong thing:** run‑level card merge (`TriggerConfig.on_pass = "merge" | "merge:{branch}"` → `_execute_trigger_action`, called from `_complete_pipeline`) is a *different feature sharing a spelling*. It is on the shared completion path, has no `is_graph` fork, and survives graph‑only completely untouched. Lane 2 W2 traced this end to end.

### 1.6 Three further boundary decisions the converter needs

**(a) Mid‑array `on_success: "stop"` → REFUSE.** Today it emits no edge, the following node becomes unreachable, `graph_definition_errors` flags it, and `_verify_graph_coverage` FAILS the run — a green v1 pipeline becomes a red v2 one, failing for the wrong reason. Silently truncating the orphaned tail deletes steps the author wrote (a quieter R1 violation). Refusing names the offending step and says the tail is unreachable. Zero pipelines in the tree do this, so it costs nothing today.

**(b) Author‑supplied step ids → HONOURED.** `PipelineStepConfig` gains `id: str | None = None`. `array_to_graph` uses `step.id` when present, `step_{i}` otherwise; duplicates and collisions with a generated `step_{i}` are refusals. Without this, converting `.lazyaf/pipelines/test-suite.yaml` renames `sync-deps`/`tier1`/`verify-executor` to `step_0..step_9`, which changes context‑directory naming, breakpoint keys, and the readability of the very graph the conversion is meant to prove.

**(c) Empty steps → REFUSE at the boundary.** `array_to_graph([])` already raises. Surfacing it at the YAML edge (§1.7) closes QA4‑08, the strict‑xfail recording that a stepless YAML "runs, does nothing, and reports PASSED".

### 1.7 Where a refusal SURFACES — the one net‑new schema surface

`sync_repo_pipelines` swallows every parse exception into a `logger.warning` and keeps the STALE definition, deliberately ("A broken CI file must not break the push"). A conversion refusal landing there is dark by construction, which would make this entire "refuse loudly" strategy an R1 violation.

**Decision: `pipelines` gains `definition_error: Text | None`**, added in the same migration that backfills (§2 P4, an additive `ALTER`, no rebuild). It is set by `upsert_materialized_pipeline` when conversion refuses, cleared on a successful sync, surfaced on `PipelineRead`, rendered as a badge on the pipeline card, and checked by `POST /api/pipelines/{id}/run` and `run_repo_pipeline` — a pipeline with a `definition_error` refuses to start rather than running a stale definition.

This distinguishes the two failure classes: *unparseable YAML* keeps today's keep‑stale + warning behaviour; *parsed but unconvertible* becomes visible. One column, one field, one badge, two guards — and without it the plan has no honest channel for any of its refusals.

### 1.8 The behaviour change this ships, stated not silent

`on_failure: "next"` on the array path completes the run **PASSED** even though a step failed. The test that pins it says so in a comment: *"Behavior-compat with main's legacy linear semantics… (graph pipelines DO check all steps)."* The graph path calls `_check_all_steps_passed` and ends **FAILED**.

**Decision: the graph verdict wins. A run containing a FAILED step ends FAILED.** This is the R4‑correct answer and it is the third of three false greens the retirement removes. It is a user‑visible change for anyone using `on_failure: next` (documented in README.md as "don't stop — carry on to the agent"), so it goes in PLAN.md's decision log and README in the same wave. The dogfood pipeline is unaffected (`on_failure: stop` on all ten steps).

---

## 2. Ordered phases — what is green at each step

The invariant that makes this safe: **`Pipeline.steps` stays on the ORM model, with its python‑side `default="[]"`, until the very last phase.** The column is `nullable=False` with **no server_default** (DDL in `0001_baseline.py` is a bare `sa.Column('steps', sa.Text(), nullable=False)`), so any state where the model has stopped declaring it while the column exists is a backend that cannot INSERT a pipeline. Keeping model and column together removes that hazard entirely — and that in turn is what lets the migration split into two revisions, which is what buys R2.

> **Departure from recon.** Lane 4 (M7) recommended ONE revision doing backfill + drop, on the grounds that the NOT NULL hazard makes the intermediate state broken. That hazard only bites if the model field is removed before the column. It is not, here. So the split is safe, and R2 — "delete only after acceptance" — wins: acceptance is a green dogfood run on real backfilled data, which happens *between* the two revisions.

### P1 — The graph gains the capability (purely additive)

`StepActions`, `describe_terminal_action`, `PipelineStepV2.actions`; `_run_terminal_action` and the dispatch call site in `_handle_graph_step_complete`; `_trigger_card` / `_merge_branch` / `_resolve_merge_source_branch` gain `step_id`/`step_run`‑keyed forms alongside the v1 ones.

Also lands here, because it is the same code and it is a **hard prerequisite**: executed tests for graph `failure` and `always` edges. Recon L28‑06 verified that **no test in any tier has ever executed a failure edge or an always edge** — `success` is the only condition ever dispatched. Retiring v1 makes edges the sole expression of failure routing; shipping that with zero coverage is not acceptable.

**Green because**: nothing is removed. The array path, `_handle_action`, `parse_steps` and the `is_graph` fork are all untouched and every existing test passes unmodified.

### P2 — `array_to_graph` becomes the faithful, refusing boundary converter

`PipelineStepConfig.id`; honour ids; emit `actions` from `merge:`/`trigger:`; emit the edge *and* the action for a non‑terminal `merge:`/`trigger:` (v1's `_merge_branch` and `_trigger_card` both continue to `current_step + 1`, so the faithful rendering is action **plus** edge); refuse on `trigger:pipeline:`, unknown actions, duplicate ids, empty input, and any conversion whose result has an unreachable node. `PipelineGraphModel.validate_graph_integrity` delegates to `graph_definition_errors` so cycles/self‑edges/orphans 422 at the boundary instead of failing at run time — the executor's own docstring already promises this.

`TestArrayToGraphConversion` grows from 10 tests to cover the full vocabulary, one test per action per direction. Note this class currently pins two behaviours that must **change**: `test_convert_on_success_stop_no_edge` (now a refusal) and the `step_N` id assertions in `test_convert_single_step` / `test_convert_three_step_chain` / `test_convert_auto_layout_vertical`.

**Green because**: `array_to_graph` still has zero production callers. Only its own unit suite moves.

### P3 — Every writer switches to graphs; every reader stops reading the array; `steps` leaves the wire

The seven writer statements (§4.4) write `steps_graph=`. `PipelineRead.steps` and its `parse_steps` validator are **deleted**; `pipeline_to_ws_dict` loses the key; `PipelineUpdate._reject_nulls` drops `"steps"`; `serialize_steps` goes. `verify_executor` re‑keys on `step_id`. The frontend loses `convertLegacyToGraph`, the `steps`‑based card render, and `debug.ts`'s index‑key branch. `definition_error` is threaded (as a schema field; the column arrives in P4 — until then the API reads the attribute the ORM does not yet have, so P3's `PipelineRead` field must land **with** P4's column: see the sequencing note in §4.3).

**Green because**: the column still exists and still holds `"[]"` for new rows (python default) and real arrays for old ones; the executor still *has* its array branch, reachable only by rows written before this phase. Nothing is deleted from the execution path.

### P4 — Migration `0012`: backfill + `definition_error`

Backfill `Pipeline.steps` → `steps_graph` for every row without a graph, using an **inlined, frozen** converter. Add `definition_error`. **No column is dropped and no table is rebuilt** — this revision is pure `UPDATE` + additive `ALTER`, which is the lowest‑risk shape available and is fully reversible.

**Green because**: after this, no row in any database lacks a graph.

### P5 — The executor fork is deleted

`_execute_step`, `_handle_action`, `_trigger_pipeline`, `_fail_run_on_undispatchable_action`, `parse_steps`, `describe_step_action`/`STEP_ACTIONS`/`STEP_ACTION_PREFIXES`, `Pipeline.has_graph_definition()`, the `else:` branch of `start_pipeline`, and the `is_graph`/`steps` parameters threaded through `_run_executor_step` / `_finish_local_step*` / `_load_local_step_context` / `LocalStepContextError`. `_execute_graph_step` → `_execute_step`; `_handle_graph_step_complete` → `_handle_step_complete`. `debug_session_service.resolve_step_keys` loses its `else` branch.

**The fork that gets missed**: `_on_step_complete_locked` (the JOB‑callback path, reached from `job_callback`, not from `_run_executor_step`) recomputes `graph` itself rather than receiving `is_graph`. Leaving its `else:` in place leaves `_handle_action` alive with one caller and no test pointing at it.

**Green because**: every row is a graph and every writer writes graphs.

### ⟶ ACCEPTANCE GATE (§5). Nothing below runs until it passes.

### P6 — Migration `0013`: drop the column; the tombstone lands

`op.batch_alter_table('pipelines')` + `drop_column('steps')`; `Pipeline.steps` removed from the model; `_adopt_unversioned` taught about retired columns; the no‑legacy guards added.

---

## 3. Wave split with strict file ownership

**Every file below is owned by exactly one agent.** No file appears twice. Anything not listed is out of scope for the wave.

### 3.1 CONTESTED — Milestone 14 is writing these RIGHT NOW

Verified against `git status` at plan time. **No agent may open a CONTESTED file until M14 has landed and the tree is committed.** Line numbers in these files are already stale; anchor on symbol names.

```
backend/app/schemas/pipeline.py            backend/app/services/pipeline_executor.py
backend/app/main.py                        backend/app/services/agent_run.py
backend/app/models/pipeline.py             backend/app/routers/test_api.py
backend/app/services/experiment_service.py backend/app/services/git_server.py
scripts/verify_executor.py                 tdd/unit/scripts/test_verify_executor.py
.lazyaf/pipelines/test-suite.yaml          tdd/integration/test_migrations.py
tdd/tier_floors.json                       tdd/skip_baseline.json
tdd/unit/services/test_pipeline_executor.py
tdd/unit/services/test_no_legacy_enqueue.py
tdd/integration/api/test_experiments_api.py
tdd/qa/test_graph_execution_qa4.py         tdd/qa/test_demo_polish_api.py
frontend/src/lib/api/types.ts              frontend/src/lib/api/client.ts
frontend/src/lib/pages/PipelinesPage.svelte
frontend/src/lib/stores/debug.ts           frontend/src/lib/stores/debug.test.ts
frontend/src/lib/components/graph/StepConfigModal.svelte
backend/alembic/versions/0011_model_endpoints.py   ← untracked; THE HEAD IS UNSTABLE
```

Agents **A1–A5** own only contested files and are **blocked**. Agents **B1–B6** own only uncontested files and **start immediately**.

### 3.2 Wave A — blocked on M14

| Agent | Phases | Exclusive files |
|---|---|---|
| **A1 · GRAPH‑SCHEMA** | P1, P2 | `backend/app/schemas/pipeline.py` · `tdd/unit/schemas/test_graph_pipeline_schemas.py` · `tdd/unit/schemas/test_pipeline_schemas.py` |
| **A2 · EXECUTOR** | P1, P5 | `backend/app/services/pipeline_executor.py` · `tdd/unit/services/test_pipeline_executor.py` · `tdd/unit/services/test_graph_coverage.py` · `tdd/unit/services/test_no_legacy_enqueue.py` · `backend/app/services/git_server.py` |
| **A3 · DOGFOOD‑GATE** | P3 | `scripts/verify_executor.py` · `tdd/unit/scripts/test_verify_executor.py` · `.lazyaf/pipelines/test-suite.yaml` · `backend/app/services/execution/local_executor.py` · `backend/app/services/execution/runner_protocol.py` |
| **A4 · FRONTEND‑WIRE** | P3 | `frontend/src/lib/api/types.ts` · `frontend/src/lib/api/client.ts` · `frontend/src/lib/pages/PipelinesPage.svelte` · `frontend/src/lib/stores/debug.ts` · `frontend/src/lib/stores/debug.test.ts` · `frontend/src/lib/stores/pipelines.ts` · `frontend/src/lib/components/debug/DebugRerunModal.svelte` |
| **A5 · AD‑HOC WRITERS** | P3 | `backend/app/services/agent_run.py` · `backend/app/services/experiment_service.py` · `backend/app/routers/test_api.py` · `backend/app/main.py` · `tdd/integration/api/test_experiments_api.py` |

### 3.3 Wave B — uncontested, start now

| Agent | Phases | Exclusive files |
|---|---|---|
| **B1 · BOUNDARIES** | P2, P3 | `backend/app/routers/pipelines.py` · `backend/app/services/trigger_service.py` · `backend/app/schemas/lazyaf_yaml.py` · `backend/app/routers/lazyaf_files.py` · `backend/app/mcp/server.py` · `tdd/integration/api/test_pipelines_api.py` · `tdd/integration/api/test_pipeline_sync_on_push.py` · `tdd/unit/services/test_repo_pipeline_sync.py` · `tdd/integration/api/test_lazyaf_files_api.py` · `tdd/qa/test_pipeline_export_qa4.py` · `tdd/qa/test_yaml_pipelines_qa4.py` |
| **B2 · TEST‑FIXTURES** | P3 | `tdd/shared/factories/pipelines.py` (new) · `tdd/shared/factories/__init__.py` · `tdd/shared/factories/models.py` · `tdd/qa/qa2_support.py` · `tdd/qa/qa3_support.py` · `tdd/unit/services/test_pipeline_local_dispatch.py` · `tdd/unit/services/test_control_mode_dispatch.py` · `tdd/integration/api/test_pipeline_execution_api.py` · all of `tdd/integration/services/**` · `tdd/e2e/test_us2_card_loop.py` · `tdd/demos/scenarios/test_pipeline_workflow.py` · `tdd/unit/models/test_pipeline.py` · the 24 empty‑array files listed in §3.5 |
| **B3 · MIGRATION** | P4, P6 | `backend/alembic/versions/0012_pipeline_steps_to_graph.py` (new) · `backend/alembic/versions/0013_drop_pipeline_steps.py` (new) · `backend/app/database.py` · `backend/app/models/pipeline.py` *(CONTESTED — see note)* · `tdd/integration/test_migrations.py` *(CONTESTED)* |
| **B4 · EDITOR & R8** | P3 | `frontend/src/lib/pages/PipelineEditorPage.svelte` · `frontend/src/lib/components/graph/**` *(StepConfigModal.svelte CONTESTED)* · all of `frontend/e2e/**` |
| **B5 · DEBUG KEYS** | P3, P5 | `backend/app/services/execution/debug_session_service.py` · `backend/app/services/execution/debug_state.py` · `tdd/unit/execution/test_debug_step_key.py` · `tdd/unit/execution/test_debug_gate.py` · `tdd/unit/execution/test_debug_session_service.py` · `tdd/integration/api/test_debug_api_contract.py` · `tdd/e2e/test_debug_rerun.py` · `cli/lazyaf/cli.py` |
| **B6 · GUARDS & DOCS** | P6 | `tdd/unit/services/test_no_legacy_code.py` · `tdd/e2e/test_graph_pipeline.py` · `tdd/skip_baseline.json` *(CONTESTED)* · `README.md` · `PLAN.md` |
| **INTEGRATOR** | gate | `tdd/tier_floors.json` *(CONTESTED)* — nobody else touches the floors |

**B3's two contested files** (`models/pipeline.py`, `test_migrations.py`) block only P4/P6, which are late phases. B3 starts by authoring the revision bodies against the schema contract in §4 and by writing the `_adopt_unversioned` change (`database.py` is uncontested), then waits.

**B4 must land its `frontend/e2e` fixes before B6 adds `frontend/e2e` to `SEARCH_ROOTS`** (§7.4), or T1 goes red on the six live `runner-mock-e2e` references.

### 3.4 Cross‑agent sequencing

```
M14 lands ──┬─> A1 (P1 schema) ──> A2 (P1 executor) ─┐
            │                                        ├─> A1 (P2 converter) ──> B1, A5, A3, A4, B2, B4, B5 (P3)
B1,B2,B4,B5 ┘  (uncontested prep starts immediately) │        │
                                                     │        └──> B3 (P4 = 0012) ──> A2 (P5 fork delete)
                                                     │                                        │
                                                     └────────────────── ACCEPTANCE GATE ◄────┘
                                                                                 │
                                                                                 └──> B3 (P6 = 0013) + B6 (tombstone)
```

### 3.5 The 24 files that are a one‑line deletion each

Lane 5's most useful correction: 29 of the 63 "array‑using" test files use **only** `steps="[]"` — the kwarg exists solely because `Pipeline.steps` is `nullable=False`, and the pipeline never executes a step. When the column is dropped the kwarg simply deletes. **Do not give these a graph fixture**; that invents execution semantics they never had.

`tdd/integration/api/`: `test_cards_api.py`, `test_endpoint_proxy.py`, `test_experiment_stamping.py`, `test_model_endpoints_api.py`, `test_spec_context_dispatch.py`, `test_test_ingestion.py`, `test_test_mode_api.py`, `test_usage_ingestion.py`, `test_wave6_seams.py`, `test_ws_runner_endpoint.py`, `test_wire_datetime_format.py` · `tdd/integration/services/test_parallel_control_steps.py` · `tdd/unit/execution/`: `test_recovery_split.py`, `test_remote_executor_contract.py`, `test_runner_dispatcher.py` · `tdd/unit/services/`: `control_layer/test_step_api_endpoints.py`, `execution/test_idempotency_keys.py`, `execution/test_recovery.py`, `test_endpoint_scheduler.py`, `test_experiment_budget.py`, `test_harness_step_dispatch.py`, `test_remote_step_dispatch.py`, `test_step_logs.py`, `experiment_rows.py`

### 3.6 Explicitly OUT of scope — do not sweep these up

- **`StepRun.step_index` survives.** It is not an array concept. `_execute_graph_step` already derives it (`list(steps_dict.keys()).index(step_id)`), and it is the key for the execution key `f"{run_id}:{step_index}:{step_run_id}"`, `LAZYAF_STEP_INDEX`, `manager.publish_step_update` / `publish_step_logs` (the frontend addresses steps by index), `execution/step_logs.py`'s inbound log route, `PipelineStateMachine.mark_step_completed`, and `verify_executor`'s self‑exemption. 61 test files reference it; **none are in scope**. Only the `.where(StepRun.step_index == current_step)` lookups inside the deleted v1 handlers go.
- **`is_graph` appears in zero test files.** Deleting the flag touches no test.
- **`continue_in_context` stays** on all three schemas, accepted‑and‑ignored, with its existing one‑time log. Removing it from the schema would silently drop it from user YAML (pydantic ignores unknown keys) — a fresh R1 violation. It gets a deprecation note, not a removal.
- **`array_to_graph` is not dead code.** It currently has zero production callers and becomes the boundary at P3. Nobody deletes it in the sweep.
- **`runner-common`, `runner-agent`, `images/base/control`** carry no pipeline shape at all (verified: no `on_success`/`steps_graph`/`is_graph` anywhere). Budget no runner work.

---

## 4. Pinned contracts between agents

These are the interfaces. An agent that needs one before its owner has landed it writes against **this text**, not against a guess.

### 4.1 `StepActions` — owned by A1

Exactly as written in §1.3. Wire shape on a node:

```json
{"id": "tier1", "name": "T1", "type": "script", "config": {…}, "timeout": 1800,
 "actions": {"success": [], "failure": ["trigger:card-abc"], "always": []}}
```

Absent `actions` is `{"success": [], "failure": [], "always": []}`. Every consumer reads it with `(step.get("actions") or {}).get(condition) or []` so a pre‑P1 graph dict is safe.

### 4.2 `array_to_graph` — owned by A1

```python
class ArrayConversionError(ValueError):
    """A v1 array the graph cannot faithfully hold. Carries every reason."""
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def array_to_graph(steps: list[PipelineStepConfig]) -> PipelineGraphModel:
    ...
```

Contract:
- ids: `step.id` when set, else `f"step_{i}"`. Duplicate, empty, or colliding‑with‑generated ⇒ `ArrayConversionError`.
- `on_success == "next"` ⇒ `PipelineEdge(id=f"edge_{i}_success", from_step, to_step=<next id>, condition=SUCCESS)`. Same for `on_failure == "next"` ⇒ `…_failure`/`FAILURE`.
- `on_success` starting `merge:`/`trigger:` ⇒ `actions.success.append(action)` **and** a SUCCESS edge to the next step (v1 continued after the effect). Same for `on_failure` ⇒ `actions.failure` + FAILURE edge. On the LAST step: action only, no edge. **The current `if i < len(steps) - 1:` guard must be lifted off the action branch** — today a `merge:` on the final step (the common shape) is not examined at all.
- `on_success == "stop"` on a non‑final step, or anything else producing an unreachable node ⇒ `ArrayConversionError` naming the step whose action orphaned the tail.
- `trigger:pipeline:*`, unknown actions, empty list ⇒ `ArrayConversionError`.
- `entry_points = [<id of steps[0]>]`. Positions `{"x": 100, "y": i * 150}`.

Callers adapt types: `array_to_graph([PipelineStepConfig.model_validate(s.model_dump()) for s in pipeline_yaml.steps])`. A YAML `type` outside `StepType` now refuses at the boundary instead of failing at run time — an improvement, and QA4's `bananatype.yaml` covers it.

### 4.3 `PipelineRead` — owned by A1, consumed by A3/A4/B1

```python
class PipelineRead(PipelineBase):
    id: str
    repo_id: str
    steps_graph: PipelineGraphModel                 # REQUIRED, no longer Optional
    definition_error: str | None = None             # NEW (§1.7)
    triggers: list[TriggerConfig] = []
    is_template: bool
    created_at: UTCDateTime
    updated_at: UTCDateTime
```

**`steps` is deleted, not made optional.** Its `= []` default is the R1 hazard of the whole retirement concentrated in one line: every failure becomes a quiet empty list. Deleting it makes `PipelinesPage`'s already‑live "0 steps for every graph pipeline" bug surface, and makes `verify_executor`'s vacuous‑pass hole impossible to leave unfixed. A derived array projection was considered and rejected — it can only exist for linear graphs, so a fan‑out would force it to lie or to refuse, and a wire field that conditionally refuses is worse than no field.

**`RepoPipelineResponse.steps` (from `GET /api/repos/{id}/lazyaf/pipelines`) KEEPS its array.** That endpoint serves the authoring *file*, not the execution definition. This is the R3‑clean split: array = authoring, graph = execution, different endpoints.

**Sequencing note for A1/B3**: the `definition_error` field on `PipelineRead` must land in the same integration step as B3's `0012` column, or `from_attributes` reads an attribute the ORM does not have.

### 4.4 The seven writer statements — owned by B1 (2) and A5 (4) and B1 (1)

| # | Site | Owner |
|---|---|---|
| 1 | `routers/pipelines.py::create_pipeline` — `steps=steps_json` | B1 |
| 2 | `routers/pipelines.py::update_pipeline` — the `serialize_steps` setattr branch | B1 |
| 3 | `services/trigger_service.py::upsert_materialized_pipeline` — `pipeline.steps = steps_json` (update branch) **and** `steps=steps_json` (create branch), **and the `pipeline.steps_graph = None` line, which inverts** | B1 |
| 4 | `routers/test_api.py::seed_state` — `steps=json.dumps([…])` | A5 |
| 5 | `services/agent_run.py::start_adhoc_agent_run` — `steps=json.dumps([step])` | A5 |
| 6 | `services/agent_run.py::start_endpoint_probe_run` — `steps=json.dumps([step])` | A5 |
| 7 | `services/experiment_service.py` cell launcher — `steps=json.dumps(steps)` | A5 |

Recon confirmed this set is exhaustive; nothing in `cli/`, `runner-*/`, `images/` writes the column. **Not one of the seven emits `merge:` or `trigger:`** — every persisted v1 step in this repo is `next`/`stop` — so `array_to_graph` is lossless for all of them.

**API boundary rule (B1)**: when `steps` is present ⇒ `steps_graph = array_to_graph(steps)`; when `steps_graph` is present ⇒ use it; **when BOTH are present ⇒ 422**. Today `PipelineCreate.validate_steps_definition` is an explicit no‑op and `update_pipeline` setattrs both independently, so `PATCH {"steps": […]}` on a graph pipeline writes a field the executor never reads and the user's edit silently does nothing. `trigger_service` already force‑NULLs `steps_graph` to dodge exactly this. Refusing is the R1 answer.

### 4.5 `_run_terminal_action` — owned by A2

```python
async def _run_terminal_action(
    self,
    db: AsyncSession,
    pipeline_run: PipelineRun,
    repo: Repo,
    step_id: str,
    step_run: StepRun,
    action: str,
) -> bool:
    """True when the action was performed; False having ALREADY failed the run.

    Never completes the pipeline: the verdict is _check_all_steps_passed's,
    always. A refusal names step_id, the action, and the reason.
    """
```

### 4.6 Shared graph test fixture helper — owned by B2

New module `tdd/shared/factories/pipelines.py`, exported from `tdd.shared.factories`:

```python
def linear_graph(
    steps: list[dict],
    *,
    ids: list[str] | None = None,
) -> dict:
    """A v2 graph dict from the SAME list[dict] the ten duplicated helpers
    already take. Pure - no DB, no executor, and deliberately NOT built on
    app.schemas.pipeline.array_to_graph: coupling every fixture in the tree
    to the converter means a converter defect silently makes every fixture
    wrong in the same direction, with the converter's own tests as the only
    thing left checking it.
    """

async def make_repo_and_graph_pipeline(
    factory, steps: list[dict], *, name: str = "test-pipeline",
) -> tuple[Repo, Pipeline]: ...

def graph_pipeline_payload(steps: list[dict], *, name: str) -> dict: ...
```

`linear_graph` is the load‑bearing piece: it means every one of the ten duplicated helpers keeps its **existing literal step list unchanged** and only the persist line moves. B2 replaces `make_repo_and_pipeline` (8 byte‑identical copies), `make_pipeline` (1), and `make_linear_pipeline` (1, with **44 call sites** in `test_pipeline_local_dispatch.py`).

**B2 converts `test_pipeline_local_dispatch.py` FIRST** — it already carries `make_linear_pipeline` and `make_graph_pipeline` side by side, so the helper's signature can be diffed against a working model before it is rolled out; and its 44 call sites are the best measure of whether the signature is right. **T2 converts LAST, one file at a time**: 28 of T2's 74 measured tests ride on this fixture and the floor margin is **two**, so one uncollectable file fails the tier.

### 4.7 Migration revision ids — owned by B3

Chain at plan time is linear, head `0011`. **`0011` is UNTRACKED and is the file M14 is authoring, so the head can move.**

- **Do not hardcode `down_revision` from this plan.** Re‑read `backend/alembic/versions/` at authoring time and parent off the actual childless head. If M14 has taken `0012`, take `0013`/`0014`.
- **`0008` is free but MUST NOT be used** — `0010`'s docstring records why: taking a released id off the wrong parent forks the chain into two heads and `command.upgrade(config, "head")` in `_run_migrations` then refuses, which is a dead backend, not a test failure.
- Add a structural guard nothing currently has: assert `ScriptDirectory.from_config(...).get_heads()` has length 1. The suite pins the head's *value* and never that there is only one.
- `tdd/integration/test_migrations.py` funnels every head assertion through `ALEMBIC_HEAD_REVISION`; bumping that one constant is the whole test‑side chain change.

**`0012_pipeline_steps_to_graph.py`** — additive + data only:
- `op.add_column('pipelines', sa.Column('definition_error', sa.Text(), nullable=True))`
- `SELECT id, name, steps, steps_graph FROM pipelines`, then per row: graph present ⇒ skip (match on `steps_graph IS NOT NULL AND steps_graph != ''` — the empty string matters, it is what dead `has_graph_definition()` tested); array present ⇒ convert and `UPDATE`; both empty ⇒ **leave `steps_graph` NULL** and count (there is no legal alternative: `validate_graph_integrity` rejects empty `entry_points` and `array_to_graph` rejects an empty list, so an "empty graph" is unrepresentable by construction); `steps` unparseable **and** no graph ⇒ collect into a refusal list.
- After the loop, a non‑empty refusal list raises `RuntimeError` naming every offending `id` and `name` plus the remedy, in the house style of `database.py`'s `_RECREATE_HINT`. Note that an unparseable `steps` is invisible today (`PipelineRead.parse_steps`, `parse_steps` and `parse_steps_graph` all swallow to `[]`/`None`), so this migration is the first thing that ever looks.
- Count `pipeline_runs` in status `pending`/`running` and log at WARNING: they pre‑date the cutover, their StepRuns carry `step_id = NULL`, and they will fail with "step definition not found" when they next dispatch. **Do not** backfill `step_runs.step_id = 'step_' || step_index` — it is only correct if the pipeline still has the steps it had when the run started, and writing one that does not match today's graph is the migration inventing history (the same objection `0007` raised when it refused to rewrite `executor='legacy'`).
- Log the converted and skipped counts at INFO, as `0007` logs its sweep.

**The converter is INLINED and FROZEN.** `backend/alembic/env.py` puts `app` on the path and imports it, so `from app.schemas.pipeline import array_to_graph` would *work* — the argument is not that it fails but that it rots. A migration is frozen in time; `array_to_graph` is not, and will keep changing to serve the two authoring edges. A revision that called it would produce a different graph for the same old row depending on which commit the operator ran it from, breaking the one property a migration must have. The chain's precedent is unanimous: zero `from app` imports across all ten revisions. Write a module‑level `_array_to_graph(steps: list[dict]) -> dict`, plain dict/json, no pydantic, mirroring §4.2 exactly, with a docstring saying it is a deliberate frozen copy that must **not** be refactored to import the live one and must keep working after the live one changes. Defaults matter when reading raw dicts: `on_success` → `"next"`, `on_failure` → `"stop"`, `timeout` → `300`, `continue_in_context` → `False`.

**`0013_drop_pipeline_steps.py`** — lands only after acceptance:
- `with op.batch_alter_table('pipelines') as b: b.drop_column('steps')`, guarded by a column‑presence check so the revision is re‑runnable (`0007`'s idiom).
- Lane 4 empirically verified this rebuild against a scratch SQLite 3.40.1 database with the real three‑table shape (`repos` / `pipelines` / `pipeline_runs`) and rows in all three, under both `PRAGMA legacy_alter_table=0` and `=1`: the rename succeeds, the inbound FK `pipeline_runs.pipeline_id → pipelines.id` still resolves, and rows survive. `pipelines` also has zero indexes to reconstruct. Cite this in the revision docstring — `0011` refused a rebuild of `step_executions` for the FK reason, and the next reader should not have to re‑derive why this table is different.
- **Downgrade**: `b.add_column(sa.Column('steps', sa.Text(), nullable=False, server_default='[]'))`. `nullable=False` is mandatory: two existing tests (`test_downgrade_to_baseline_matches_pure_0001_schema`, `test_roundtrip_restores_head_schema`) compare full schema snapshots against a fresh `0001`, which declares `steps` NOT NULL. `_schema_snapshot` records `(type, nullable, primary_key)` and **not** server_default, so the default is invisible to them — which is exactly what makes this work with no second rebuild. Note the deliberate divergence from `0001`'s bare column in the docstring. The downgrade restores the SHAPE, not the data; say so, in `0007`'s register.
- **Rejected**: stashing the source array in the graph JSON as `source_v1` to make the downgrade lossless. `PipelineGraphModel` declares no such field so it would not survive a round trip, and it would create a second source of truth for the step list (R3).

### 4.8 `_adopt_unversioned` — owned by B3, and it is a real hole

`backend/app/database.py::_adopt_unversioned` classifies an unversioned database by asking only what is **missing**: it iterates the expected column set and checks presence. A column present in the database but absent from the models is invisible to it.

So after P6, a pre‑alembic dev DB (built by the old `create_all`, no `alembic_version` — the case `test_unstamped_headshaped_db_is_stamped_at_head` exercises, and the case the `lazyaf-data` docker volume holds) has everything the models declare, so `missing_current` is empty, so it is `command.stamp(config, "head")`ed. The backfill never runs, the drop never runs, `pipelines.steps` survives as an orphan still NOT NULL with no server_default, and the next pipeline INSERT dies with `NOT NULL constraint failed: pipelines.steps` — from a database stamped as being at head.

**Fix**: a module‑level `_RETIRED_COLUMNS: dict[tuple[str, str], str]` mapping `("pipelines", "steps") -> "<the revision immediately BEFORE the drop>"`. Before stamping head, look for any retired column still present; if found, stamp *that* revision instead and let the caller's `command.upgrade(config, "head")` run the migration properly. This heals the dev DB rather than bricking it. Needs a test — none exists: build via `_create_all`, hand‑add `pipelines.steps` NOT NULL with `server_default='[]'`, drop `alembic_version`, run `_migrate`, assert the DB lands at head, `steps` is gone, and a seeded array pipeline came out as a graph.

### 4.9 `verify_executor` re‑keying — owned by A3

Three reads change from `enumerate(pipeline["steps"])` to `pipeline["steps_graph"]["steps"]` (a mapping keyed by step id), and the correlation key changes from `sr["step_index"]` to `sr["step_id"]`. `step_requires_remote`, `expected_executor`, `step_harness_endpoint`, `step_harness_mode` all take a bare step dict and need **no change** — only their lookup key does.

Two things that must survive the port intact:
1. **The vacuous‑pass guards.** `if not checked: … "vacuous pass = fail, R4"`, `if not agents_checked`, `if not remote_checked`, `if not harness_steps: raise SystemExit(…)`. The current `.get(i, LOCAL_EXECUTOR)` / `.get(i, "script")` defaults are the hole: with an empty definition every step is "expected local, expected script" and assertions 8 and 11 stop being able to fail. **Add a new negative test**: an empty or missing definition must make the gate FAIL, not default.
2. **The self‑exemption.** It reads `LAZYAF_STEP_INDEX`. Add `LAZYAF_STEP_ID` alongside it in `local_executor.py` and `runner_protocol.py` and exempt on that instead — keying the gate's own identity on `list(steps_dict.keys())` insertion order is the single most fragile thing in the ratchet.
3. The two mock‑usage constants are deliberately duplicated into `verify_executor.py` (it must be stdlib‑only, it runs in a bare `lazyaf-base:dev` container with `control: false`) and pinned by a test that imports both and refuses divergence. Move both halves together.

The dogfood YAML itself does **not** change shape: it stays an array (that is the authoring edge), the boundary converts it on every push, and honouring its ten ids (§1.6b) is what keeps `sync-deps` / `tier1` / `verify-executor` as the graph node keys.

### 4.10 Export YAML — owned by B1

`GET /api/pipelines/{id}/export/yaml` currently emits a **third dialect**: `steps` as a mapping keyed by step id, with edge *targets* written into `on_success`/`on_failure`/`on_always` (a bare id, or a list on fan‑out), plus top‑level `entry_points`. `PipelineYaml.steps` is `list[PipelineStepYaml]`, so the export cannot be committed to `.lazyaf/pipelines/` and re‑synced. `tdd/qa/test_pipeline_export_qa4.py` records this as two strict xfails ("export a graph pipeline, commit it, and it silently disappears from the repo's pipeline list"). After the retirement the legacy branch dies and the mapping branch becomes the *only* export — i.e. 100% unimportable.

**Decision: export emits the ARRAY authoring dialect, and REFUSES (409, naming the reason) on any graph the array cannot express** — fan‑out, fan‑in, `always` edges, multiple entry points, or more than one action per condition. This follows directly from the owner's decision that the array is the authoring format at the repo‑YAML edge; a `PipelineYaml` v2 branch would contradict the premise that a human writing five steps should not hand‑author nodes and edges.

The down‑conversion is *total* for a linear graph: a SUCCESS edge to the next node ⇒ `on_success: next`; no edge ⇒ `stop`; `actions.success == ["merge:main"]` + edge ⇒ `on_success: "merge:main"`. It refuses on everything else rather than flattening — a silent flatten would be a fresh R1 violation on the export side.

**Trap**: the four strict xfails in `test_pipeline_export_qa4.py` go **XPASS** when this lands, which fails the suite. They must convert to positive assertions in the same commit. `tdd/qa` runs in no tier, so this will only surface in a manual run — B1 runs it explicitly.

---

## 5. The acceptance gate

R2: deletion happens only after acceptance. **Acceptance is the gate below; `0013` (the column drop) and B6's tombstone land only after every line passes.**

### 5.1 Tests that must exist and pass

**Capability (A1, A2) — these did not exist before this wave:**
- `array_to_graph` round‑trips `merge:{branch}` and `trigger:{card_id}` into `actions` + edge, one test per action per direction (success/failure), plus one on the final step where there is no edge.
- `array_to_graph` **refuses**, naming the offender, on: `trigger:pipeline:*`, any unknown action, a mid‑array `stop` that orphans a step, duplicate/colliding ids, an empty list.
- A graph pipeline with `actions.failure = ["trigger:card-x"]` on a failing step spawns the fix card through `agent_run.start_card_work`, leaves the marker StepRun, **and the run ends FAILED** (not PASSED — the v1 KNOWN LIMITATION, inverted).
- A graph pipeline with `actions.success = ["merge:main"]` reaches `git_repo_manager.merge_branch` with the resolved branch; an unresolvable branch **fails the run loudly**; source == target skips the merge. (The three surviving `TestMergeActionBranchResolution` tests, rewritten against a graph.)
- A **failure edge** fires its target when the source fails, and the success edge does not. An **always edge** fires on both outcomes. A success edge does **not** fire on failure. Plus the T2 real‑container twin for the failure edge. *(Zero coverage exists today — L28‑06.)*
- A graph step's `step_index` equals its position in the graph's `steps` dict. *(Nothing asserts this today, and the WS frame, the state machine and the execution key all depend on it.)*

**Boundary (B1):**
- `.lazyaf/pipelines/test-suite.yaml` materialized verbatim yields a graph with one node per YAML step, node ids **equal to the YAML ids**, nine success edges in file order, one entry point, and no `graph_definition_errors`.
- `POST`/`PATCH /api/pipelines` with both `steps` and `steps_graph` ⇒ 422.
- A YAML whose conversion refuses ⇒ the materialized row carries a `definition_error`, `POST /run` refuses, and the stale definition is not silently run. *(Closes the Y5 dark channel.)*
- A stepless YAML does not report a green pass. *(Closes QA4‑08.)*
- Export of a linear graph produces YAML that `PipelineYaml` parses; export of a fan‑out graph refuses with a message naming the construct. *(Flips the four QA4 strict xfails to positive.)*

**Migration (B3):**
- `TestPipelineStepsBackfill` at `0012`: array converts (version 2, entry points, ids, one SUCCESS edge per consecutive `next`); a row with a pre‑existing graph keeps it byte‑for‑byte; both‑empty leaves `steps_graph` NULL and the row alive; unparseable `steps` with no graph raises and names the pipeline id; **a step carrying `merge:main` converts to `actions` or raises — never converts silently**.
- At `0013`: `"steps" not in snapshot["pipelines"]["columns"]`; `pipeline_runs` rows seeded before the rebuild survive and still join; after downgrade `steps` is back with `nullable is False`; `test_downgrade_to_baseline_matches_pure_0001_schema` and `test_roundtrip_restores_head_schema` pass **unmodified** (they are the real regression gate).
- The `_adopt_unversioned` retired‑column test (§4.8).
- A single‑head assertion over `ScriptDirectory.get_heads()`.

**Dogfood gate (A3):**
- All 72 tests in `test_verify_executor.py`, rewritten against graph‑shaped fixtures with `step_id` on each run, with **every assertion intact**.
- The new negative test: an empty/missing definition FAILS the gate.

**Debug (B5):**
- `debug_step_key` returns `step_run.step_id`; the surviving index fallback covers only the graph‑defect StepRun (`step_id=None`, `step_name="pipeline graph"`) that `pipeline_executor` still creates.
- A breakpoint keyed by a real graph step id **actually fires** — not "the run completed", which passes by not stopping.

**Frontend (A4, B4):**
- A pipeline card renders its real step count and type chips from `steps_graph`. *(Today every graph pipeline renders "0 steps" — a live bug this surfaces.)*
- R8: a Playwright spec that creates a pipeline **and updates it**, asserting `steps_graph.edges` and `entry_points` by reading `GET /api/pipelines/{id}`. This requires fixing the editor's `PUT` → `PATCH` (there is no PUT route; editing any existing pipeline currently 405s and no test catches it) and adding a keyboard/menu affordance for connecting nodes, since SvelteFlow's drag does not respond to Playwright. Nine currently‑skipped tests either become drivable or are **deleted** — nine standing `test.skip`s on the only authoring surface is fake coverage (R4).

### 5.2 Tiers that must be green

| Tier | Floor | Measured | Constraint |
|---|---|---|---|
| T1 | 3486 | 4134 (~4145 after M14) | ~600 tests of headroom; worst‑case deletions total ~54, and this wave is expected to **net up**. Do not touch the floor during the work; INTEGRATOR re‑measures and raises at the end on a green run, measured‑minus‑2%. |
| T2 | 72 | 74 | **Margin of two.** 28 of 74 ride on the array fixture. Conversion is count‑neutral so the floor holds — but one uncollectable file fails the tier. Convert last, one file at a time. |
| T3 | 21 | 22 | The three `TestGraphPipelineLegacyConversion` tombstones are `@pytest.mark.skip`, and `ci_gate.py` computes `executed = passed + failed`, so **deleting them costs zero executed tests.** T3 stays at 22 and needs no floor note. |

**No floor is lowered by this retirement.** If anyone finds themselves wanting to lower one, that is the signal that coverage was deleted where it should have been converted.

`tdd/qa` (139 tests) runs in **no tier** and has no floor. QA4 edits are ungated and need a manual run — B1 does that explicitly for the export xfails.

### 5.3 The dogfood ratchet

`.lazyaf/pipelines/test-suite.yaml` is pushed and the run goes **green through the graph executor, with `verify_executor` reporting all its assertions against `step_id`**, on a database whose pipeline row was produced by the P4 backfill. That run is the acceptance. It exercises: the boundary converter on every push (the strongest possible proof), the graph path end to end across three different container images on one workspace volume, and the re‑keyed gate.

### 5.4 The no‑legacy guard that keeps v1 dead — owned by B6, lands with `0013`

`tdd/unit/services/test_no_legacy_code.py` has two mechanisms: `test_legacy_module_is_gone` (`pytest.raises(ModuleNotFoundError)` over `GONE`) and `test_no_forbidden_references` (a grep over `SEARCH_ROOTS` for `FORBIDDEN`). v1 has no module of its own, so mechanism 1 must be extended **by shape**:

```python
# v1 array format, retired 12.8. There is no module to import, so the
# tombstone asserts ABSENCE OF SYMBOL - and, for the fork itself, absence of
# a PARAMETER, which is the only thing that can prove the two-way branch is
# actually gone rather than renamed.
def test_v1_array_symbols_are_gone():
    from app.services import pipeline_executor as pe
    for name in ("parse_steps", "STEP_ACTIONS", "STEP_ACTION_PREFIXES",
                 "describe_step_action", "_ACTION_VOCABULARY"):
        assert not hasattr(pe, name)
    for name in ("_handle_action", "_execute_graph_step", "_trigger_pipeline",
                 "_fail_run_on_undispatchable_action"):
        assert not hasattr(pe.PipelineExecutor, name)

def test_no_method_takes_an_is_graph_parameter():
    # the fork, not its name
    for name, fn in inspect.getmembers(pe.PipelineExecutor, inspect.isfunction):
        assert "is_graph" not in inspect.signature(fn).parameters

def test_pipelines_table_has_no_steps_column():
    assert "steps" not in {c.key for c in sa_inspect(Pipeline).columns}
```

`FORBIDDEN` additions, **scoped**: `is_graph`, `_handle_action`, `STEP_ACTION_PREFIXES`, `has_graph_definition`, `steps=json.dumps`, `.lazyaf-context`. **Do NOT add `on_success` / `on_failure`** — they legitimately survive in `PipelineStepYaml` and `PipelineStepConfig` at the authoring edge, and a blanket ban would forbid the thing the decision deliberately keeps.

`SEARCH_ROOTS` gains **`frontend/e2e`** (the R8 surface, currently unpoliced and currently harbouring six live `runner-mock-e2e` references — §7.4) and, explicitly decided, `cli/`. Add path‑existence assertions for the deleted `runner-claude`/`runner-gemini`/`runner-mock` directories: mechanism 2 cannot see a directory containing no forbidden token, and mechanism 1 cannot see a Dockerfile.

**Write the tombstone in the deletion commit, not after.** The module's own docstring is explicit that a removal test written after the removal is how the last attempt shipped fake green.

Delete the three `TestGraphPipelineLegacyConversion` tests **and** their `tdd/skip_baseline.json` entry (`"reason_prefix": "Legacy pipeline format removed"`) in the same commit — that file's own note demands it, and leaving the entry behind means a baseline prefix nothing can ever match. Two of the three POST to `/api/pipelines/{id}/convert-to-graph`, an endpoint that **does not exist**; do not implement it.

---

## 6. What could silently break, ranked

Ranked by (silence × blast radius). Each names the test that catches it.

**1. `verify_executor` passes vacuously.** With `steps` off the wire, `step_types` and `expected` become empty dicts and every `.get(i, LOCAL_EXECUTOR)` / `.get(i, "script")` returns its default — assertions 8 and 11 stop being able to fail while the gate reports green. This would land on the very run meant to prove the retirement. **Catch**: A3's new negative test — an empty/missing definition must FAIL the gate — plus keeping every `if not checked` / `if not agents_checked` / `if not remote_checked` guard intact.

**2. A breakpoint key that no longer matches.** `debug_step_key` returns `str(step_index)` for array runs and `str(step_id)` for graph ones, and `array_to_graph` now honours author ids, so a stored breakpoint named `"2"` becomes `"tier2"`. A breakpoint that never fires is silent, and a debug test asserting the run COMPLETED would still pass. **Catch**: B5's "a breakpoint keyed by a real graph step id actually fires" test. Mitigation: the CLI already refuses an unknown key, so a stale stored breakpoint surfaces as a refusal rather than a no‑fire; the migration counts and warns.

**3. `array_to_graph` silently dropping `merge:` / `trigger:`.** The current lossy branch is entirely uncovered — not one existing assertion passes either action to the converter. And the `if i < len(steps) - 1:` guard means an action on the LAST step (the common shape for "merge when this passes") is not examined at all: the conversion is 100% silent, the graph validates, the run goes green, the branch is never merged. **Catch**: the per‑action/per‑direction converter tests in §5.1, including the final‑step case.

**4. `_run_agent_name` returning None.** `agent_run.py` does `for step in steps: if step.get("type") == "agent": return (step.get("config") or {}).get("agent")` over the array. Failing over to None silently sets `card.completed_runner_type = None` and leaves `job.runner_type` unchanged — dark data loss on every card, with no error. **Catch**: A5 converts it to iterate `graph["steps"].values()` **in the same commit as the writer**, with a test asserting the agent name resolves from a graph.

**5. Author step ids replaced by `step_N`.** Nothing today tests that an authored id survives any conversion. It silently changes context‑directory names and breakpoint keys for the repo's own pipeline, and the symptom surfaces far from the cause. **Catch**: B1's "the dogfood YAML's ten ids are the graph's ten node keys" test.

**6. A failure edge that has never executed.** `_handle_graph_step_complete`'s failure branch has zero coverage in any tier; graph‑only makes it the sole expression of failure routing, and the dogfood pipeline (all `on_failure: stop`) cannot catch a defect there. **Catch**: A2's failure/always edge dispatch tests, T1 and T2.

**7. `_load_previous_step_output` presenting a parallel sibling as "previous".** It queries `StepRun.step_index == step_run.step_index - 1`, and on the graph path `step_index` is the node's position in the serialized `steps` dict. For an `array_to_graph` product that is the true predecessor; for a hand‑authored or UI‑edited graph it is arbitrary, and for a fan‑out it is a sibling that may never have run. Wrong context in an agent prompt produces plausible, wrong work with no error. **Catch**: resolve through `get_upstream_step_ids` on the graph path with an explicit rule for multiple upstreams; test that a fan‑out sibling's logs are NOT presented as previous. *(Pre‑existing, 12.5‑era. Assign to A2; coordinate with M14.)*

**8. A pre‑alembic dev DB stamped at head, unconverted, with an orphan NOT NULL column.** §4.8 in full. Silent and delayed: startup looks clean and the failure surfaces later as a NOT NULL error on pipeline creation, plus pipelines that quietly lost their definitions. **Catch**: B3's `_adopt_unversioned` test.

**9. The `on_failure: next` verdict flip.** PASSED → FAILED for any user pipeline using it. Loud at the run, silent as a *change* unless announced. **Catch**: an explicit pinned‑verdict test plus a PLAN.md decision‑log entry plus the README correction (§1.8).

**10. `StepConfigModal` wiping unknown config keys.** Its type‑change handler replaces `config` wholesale, destroying `requires`, `control`, `endpoint`, `agent`. It also writes `config.runner_type === 'openai-harness'` while the dogfood YAML and `verify_executor` use `config.agent`. Once the dogfood pipeline is a graph an operator can open in the editor, a re‑save silently drops the `requires:` block — which `verify_executor`'s lane assertion *would* catch, loudly, with an invisible cause. **Catch**: B4 preserves unknown keys across a type change (or warns naming what will be dropped) and reconciles the two spellings; add a component test.

**11. Strict xfails going XPASS.** Fixing export turns four `xfail(strict=True)` tests red. In a lane no tier runs, so it only appears in a manual QA run — and it invites reverting the correct fix rather than updating the test. **Catch**: B1 converts them in the same commit and runs `tdd/qa` explicitly.

**12. `PipelinesPage` showing "0 steps".** Already live today for every graph pipeline; the e2e test that creates one asserts only the card title. **Catch**: A4's step‑count render plus a Playwright assertion on the count.

---

## 7. The rest of Phase 12.8 — separate wave, separate sizing

Independent of the retirement. Sized so it can run alongside or after.

### 7.1 Phase 12.0 is DONE — prose only (trivial)

The PLAN.md claim that "runner images do not import runner-common" is **stale**. Verified: `images/agent-base/Dockerfile` does `COPY runner-common/` + `pip install /opt/runner-common` + a build‑time `import runner_common.agent_wrapper` check; `images/claude` and `images/gemini` are `FROM lazyaf-agent-base:dev`; `build_images.py` stages it from one source with no vendored copy; three tests in `test_image_contract.py` already police it; and `runner-claude/`, `runner-gemini/`, `runner-mock/` are deleted on disk (` D` in git status, uncommitted).

**Work**: commit the deletion; flip PLAN.md's 12.0 row to COMPLETE; delete the stale "still true from the January-era assessment" paragraph; close the decision‑log's OPEN item as *done at 12.5*. Consider adding `runner_common.harness` and `runner_common.endpoint_probe` to `SURVIVING_RUNNER_COMMON` (M14‑era modules, currently unasserted — coordinate, do not land unilaterally). *(`images/test-runner` deliberately does not install runner-common; it rides in via PYTHONPATH from `run_tier.py::_tier_env()` so `-p runner_common.pytest_lazyaf` resolves. Documented choice, not a gap.)*

### 7.2 The regression matrix, rewritten against reality (medium)

**Zero of the 35 names in PLAN.md's 12.8 matrix exist.** The matrix was aspirational; the coverage mostly exists and is better placed than one mega‑file would be. **Do not create `test_full_regression_suite.py`** — say that in the plan rather than leaving a checkbox implying otherwise. Replace the matrix in place with this table. `OK` = a CI tier runs it. `QA‑ONLY` = only `tdd/qa`, which no tier runs. `GAP` = nothing covers it. `OBSOLETE` = the row describes behaviour the architecture no longer has; keep the row, mark it, and give the reason.

| Matrix row | Verdict | Real coverage |
|---|---|---|
| test_old_entrypoints_removed | PARTIAL | `test_no_legacy_code::test_legacy_module_is_gone[runner_common.entrypoint/.job_helpers/.context_helpers]` + `test_no_forbidden_references[runner-claude/gemini/mock]`. **No path‑existence assertion** — a resurrected directory is invisible. §5.4 adds it. |
| test_runner_pool_removed | OK | `test_legacy_module_is_gone[app.services.runner_pool, .job_queue]` + `test_no_forbidden_references[runner_pool, job_queue, QueuedJob, /api/runners/register]` |
| test_no_docker_in_docker | **OBSOLETE** | The socket is now deliberate and allowlisted. Rewrite as *"the socket reaches a step ONLY through the `needs: [docker]` allowlist"*: `test_local_executor_hardening::test_non_allowlisted_bind_mount_fails_step_loudly`, `TestNeedsSugar::test_needs_docker_adds_socket_bind_mount`, `::test_unknown_needs_capability_fails_step_loudly` |
| test_pipeline_with_single_step_completes | OK | `TestLocalLifecycle::test_success_lifecycle_persists_and_broadcasts`; `TestRunPipeline::test_run_pipeline_creates_run` |
| test_pipeline_with_multiple_steps_sequential | OK (T2) | `TestTwoStepSharedWorkspace::test_two_step_pipeline_shares_volume_and_home`; `test_two_local_steps_share_one_workspace` |
| test_pipeline_on_success_next_continues | OK (v1 only) | `TestPipelineExecutorActionHandlers::test_handle_action_next_executes_next_step`. Graph analogue is a success edge — **dies with v1; strengthened by §5.1** |
| test_pipeline_on_success_stop_completes | OK | `test_handle_action_stop_success_marks_passed`; graph analogue `TestGraphLocalDispatch::test_graph_local_steps_fan_out_and_never_enqueue` |
| test_pipeline_on_failure_stop_halts | OK (T2) | `test_handle_action_stop_failure_marks_failed`; `TestLocalFailureCleanup::test_failing_step_fails_run_and_cleans_workspace` |
| test_pipeline_on_failure_next_continues | **v1 ONLY — top backfill** | `TestLocalFailurePaths::test_linear_on_failure_next_continues_past_failed_local_step`. **No graph equivalent exists anywhere.** §5.1 supplies it, and §1.8 records the verdict change |
| test_pipeline_cancel_stops_execution | OK | `TestPipelineExecutorCancelRun` (2); `test_cancel_running_pipeline`; `test_cancel_step_kills_container` |
| test_script_step_executes_command | OK (T2) | `test_local_executor_real_docker.py`; `test_pipeline_local_execution.py` |
| test_docker_step_uses_specified_image | **OBSOLETE wording** | 12.4 removed docker execution from runners; `docker` and `script` take an identical path, the only difference is the image override. Rewrite as *"a step's `image` config selects the container image"*: `test_step_image_override_wins_over_default`, `test_execution_router::test_docker_routes_local` |
| test_agent_step_invokes_ai_runner | **OBSOLETE wording** | No "AI runner" exists. `TestAgentStepRoundTrip::test_mock_agent_step_produces_logs_files_and_usage` (T2); `test_us2_card_loop::test_full_loop` (T3); `test_agent_step_dispatch.py` |
| test_step_timeout_enforced | OK (T2) | `test_timeout_kills_real_container`; `test_timeout_kills_container`; `test_timeout_kills_and_reports_124`; `test_exit_124_surfaces_as_timeout` |
| test_step_config_passed_to_executor | OK | `TestLocalLifecycle::test_local_execution_config_honours_contract` |
| test_local_executor_spawns_container | OK (T2) | `test_local_executor_real_docker.py`; `test_local_executor_contract.py` |
| test_remote_executor_pushes_via_websocket | OK | `test_remote_executor_contract.py`; `test_loopback_runner.py` (T2); `test_ws_runner_endpoint.py` |
| test_execution_router_selects_correct_executor | OK | `test_execution_router.py`; `test_execution_router_requires.py` |
| test_continue_in_context_preserves_workspace | **OBSOLETE** | Accepted‑and‑ignored since 12.2‑INT. Behaviour is now unconditional: `test_two_step_pipeline_shares_volume_and_home` (T2); the ignoring is pinned by `test_continue_in_context_ignored_with_one_time_info` |
| test_is_continuation_skips_cleanup | **OBSOLETE** | No per‑step cleanup exists; workspace is cleaned once per RUN: `test_workspace_acquire_release_cleanup_ordering`, `test_two_local_steps_share_one_workspace` (cleanup count 1 for 2 steps) |
| test_previous_step_logs_passed_to_next | PARTIAL | Prompt shape: `test_agent_prompt::test_previous_logs_are_appended_in_the_legacy_section_shape`; cap and config key pinned separately. **GAP**: no test where a real step N's logs reach step N+1's agent config end to end (see §6 item 7) |
| test_different_images_share_workspace | **GAP** | Not one test mixes two different images on one workspace volume. Covered only by the dogfood pipeline in production (`lazyaf-test-runner` → `lazyaf-base` → `lazyaf-agent-base` on one run volume). §7.3 |
| test_card_complete_trigger_fires | **GAP in every tier** | Only `tdd/qa/test_qa2_state_machine::test_repeated_approve_does_not_refire_card_complete_triggers`, which runs in no tier and asserts `after - before <= 1` — satisfied at zero. `test_cards_api`'s `trigger_spy` monkeypatches the service away. §7.3 |
| test_push_trigger_on_branch_match | OK | `TestSyncOnPushCreatesPipeline::test_push_event_materializes_pipeline_with_triggers` + `::test_push_to_other_branch_does_not_sync` — the shape the other two trigger rows should copy |
| test_trigger_disabled_does_not_fire | **GAP** | Only `test_repo_pipeline_sync::test_disabled_trigger_round_trips`, which asserts YAML parsing, not suppression. §7.3 |
| test_pipeline_run_status_broadcast | OK | `TestLocalLifecycle::test_success_lifecycle_persists_and_broadcasts` (real manager) |
| test_step_run_status_broadcast | OK | same test; `TestStatusBridge::test_running_broadcasts_step_update_frame` |
| test_card_updated_broadcast | OK | `TestCardUpdateWebSocketBroadcast`; `TestCardActionWebSocketBroadcasts`; `test_card_execute::test_websocket_receives_card_updates` |
| test_step_failure_captured_in_error_field | OK | `TestLocalFailurePaths` (3) |
| test_job_failure_updates_card_status | OK | `TestSynchronousStartFailure::test_card_ends_failed_with_a_terminal_job`; `test_us2_card_loop`; `test_card_execute::test_card_fails_on_executor_error` |
| test_tests_failed_marks_card_failed | OK | `TestCardOutcomeRespectsTests` (3); `TestTestResultGate` (5) |
| test_runner_death_requeues_step | OK | `TestRunnerDeath::test_on_runner_death_requeues_step`; `test_recovery_split` (2) |
| test_backend_restart_resumes_pipelines | **ROW IS WRONG** | The design deliberately does NOT resume: local orphans FAIL, remote go back to PENDING. Rewrite as *"a restart fails local orphans and requeues remote ones"*: `TestOrphanedExecutionRecovery`, `test_recovery_split.py`, `test_every_row_lands_disconnected_with_no_socket` |
| test_orphan_containers_cleaned_on_startup | OK (T2) | `TestOrphanSweepRealDocker` (4); `TestOrphanAudit` + `TestOrphanAuditTask`; `TestContainerCleanup` (2) |
| test_orphan_steps_marked_failed | OK | `TestOrphanedExecutionRecovery` (4); `TestBackendStartupRecovery` (4) |

**Tally**: 22 OK · 5 OBSOLETE‑or‑wrong‑as‑written · 3 hard GAPs · 2 PARTIAL · plus the graph‑edge gap tracked as a retirement prerequisite (§5.1).

### 7.3 The three gaps (small each)

- **Two images on one volume** (T2): extend `TestTwoStepSharedWorkspace` with step 1 on `lazyaf-test-runner:dev` and step 2 on `lazyaf-base:dev`, writing under `/workspace/repo` and `$HOME` and reading both back. Both images are already in T2's `build_images.py --check` preflight, so no new build cost. Guards cross‑image uid/ownership on a shared named volume — a class that has already bitten this repo twice (dogfood runs #5 and #9).
- **`card_complete` fires** (T1, integration/api — the executor is already faked in that lane): a card reaching `done` on a repo with an enabled `card_complete` trigger produces exactly one PipelineRun with `trigger_type == "card_complete"` and the card's branch/commit in `trigger_context`.
- **`enabled: false` suppresses** (T1): the same setup with `enabled: false` produces zero runs. Reuse `test_pipeline_sync_on_push.py`'s existing helpers rather than inventing a second idiom.

### 7.4 Untiered suites and unpoliced roots (medium)

**313 tests run in no tier and no `scripts/test.sh` lane.** `run_tier.py::TIERS` is the single source of tier selection and selects only `tdd/unit`, `tdd/demos`, `tdd/integration` (T1, minus `integration/services`), `runner-common/tests` (T1), `tdd/integration/services` (T2), `tdd/e2e -m "not slow"` (T3).

- **`runner-agent/tests` (174)** is the direct analogue of `runner-common/tests`, which 12.7 folded in for exactly this reason. Fold non‑docker tests into T1 and docker‑marked ones into T2, add the PYTHONPATH entry to `_tier_env()`, re‑measure and raise floors **from a green run**. `tdd/conftest.py`'s T1 no‑docker guard does not apply outside `tdd/`, so the docker‑marked tests must be *routed* to T2, not silently skipped in T1.
- **`tdd/qa` (139)** cannot join a tier as written — it drives HTTP against a live `BASE_URL` stack. Record it in PLAN.md and `tdd/README.md` as a **stated known exclusion**, the way the `@slow` e2e exclusion already is. Separately triage which QA4 graph‑pathology assertions deserve tier‑runnable twins now that graph is the only path — including the `xfail(strict=True)` QA4‑06 duplicate‑entry‑point defect, currently the only record of that bug.
- **`frontend/e2e` is not in `SEARCH_ROOTS`** and currently carries six live references to `runner-mock-e2e`, a compose service 12.6 deleted (the actual services are `backend-e2e` and `runner-agent-e2e`). The worst is `helpers.ts`, which prints that command as the **error message** to a developer whose e2e run just failed. B4 fixes all six; B6 adds the root in the same commit.

### 7.5 Dead code that falls out (small)

- `TestStepBranchingLogic` (5 tests, `test_pipeline_executor.py`) — every one is a test of `dict.get`: `step = {...}; on_success = step.get("on_success", "next"); assert on_success == "next"`. Delete.
- `.lazyaf-context/` — nothing writes it; `_handle_merge_action` still calls `delete_directory_from_branch(".lazyaf-context")` and `git_server.delete_directory_from_branch` exists to serve it. Both go with the v1 merge action (A2, B‑owner `git_server.py`).
- `Pipeline.has_graph_definition()` — zero callers repo‑wide, no test. Delete with the column.
- `_execute_step`'s four dead locals (`step_id`, `agent_file_ids`, `prompt_template`, and `previous_step_logs` assigned from a real DB query and never read) go with the function.
- `previous_runner_id` on `_execute_graph_step` and the `runner_id` threading in `_handle_graph_step_complete` — neither body uses it. *(`runner_id` on `StepExecution` is still meaningful — verify_executor assertion 10 — so do not remove the column or `StepRunRead._lift_runner_id`.)*
- `_reconcile_control_execution` reports `timeout_s = step.get("timeout", 300)` while dispatch uses `default_timeout_for(step_type)` (1800 for agent steps) — so an agent step that times out can report "timed out after 300s" having run for 1800. Make it use `default_timeout_for`.

### 7.6 Docs retirement (medium)

PLAN.md is 3855 lines. Lines **1486–3726** (`## Phase 12: Runner Architecture Refactor` through `### 12.7`) are **2241 lines, 58% of the file**, all COMPLETE. `historical-documents/` exists with an established convention: 12 files named `phase-NN-slug.md`, each opening `# Phase N: …` / `> **Status**: COMPLETE` / `> **Goal**: …`, with `phase-08.5-cicd-foundation.md` as the decimal precedent. `docs/milestone-13/` is the precedent for the other direction (detail out, 227‑line summary stays).

Move them to `historical-documents/phase-12-runner-architecture.md`, leaving only the Completed‑Phases row and the Current‑Status evidence lines.

**Three things are actively stale, not merely historical:**
- `### continue_in_context Semantics` (lines 1709–1831) tabulates `/workspace/.lazyaf-context/` as a preserved location. The flag is ignored and the directory is dead. **Delete outright**, do not move.
- Lines 565 / ~629: the 12.0 PARTIAL claim (§7.1).
- The 12.8 matrix itself (§7.2).
- `PLAN.md`'s "Step Requirements" example shows `requires:` as a **top‑level** step key; `PipelineStepYaml` has no such field so pydantic drops it silently, and `verify_executor` reads `step["config"]["requires"]`. The dogfood YAML uses the correct nested spelling. Fix the example.
- `upcoming/wave9-145-runner-images.md` writes `on_failure: continue`, which is outside the vocabulary. Its intent ("run this cleanup either way") is exactly `EdgeCondition.ALWAYS`.

**README.md** documents the array format and the full v1 vocabulary at lines 34–52, 86 and 151, including `trigger:{id}` and `merge:{branch}`, and defines a pipeline as "an ordered set of steps, or a DAG" — the exact ambiguity this decision removes. Rewrite the glossary to say the graph is the execution model and the array is an authoring convenience at two named edges; correct the vocabulary line; record the `on_failure: next` verdict change (§1.8). README is in `test_no_legacy_code`'s ALLOWLIST, so the grep will not force this — a human has to.

**Blocker**: PLAN.md's status table marks 12.6.5 / 12.6.6 / 12.7 IN PROGRESS while `tier_floors.json` records their suites as landed, measured and floored (T2 68→72, T3 18→21). **Settle that contradiction before moving anything** — burying in‑flight work in a directory named "historical" is worse than leaving it in place.

---

# ADVERSARIAL REVIEW OF THIS PLAN

Recon complete. Ranked defects, each verified against the tree.

**1. P3 needs `definition_error` before P4 creates it — the tree is RED between them.**
§1.7 has `upsert_materialized_pipeline` set `definition_error` and §4.3 puts it on `PipelineRead`, both at P3; the column only lands in `0012` at P4, and §3.4 orders P3→P4. SQLAlchemy emits the model's full column list in every `SELECT` against `pipelines`, so the whole window is `no such column: pipelines.definition_error`. **Correction:** split the revision — an additive-only `add_column('definition_error')` revision plus the ORM attribute lands *before* P3; the row backfill becomes a second revision after P3. §3.4's arrow inverts for that half.

**2. §1.4's dispatch snippet uses a `step_run` that does not exist.**
Verified signature: `_handle_graph_step_complete(self, db, pipeline_run, pipeline, repo, graph, completed_step_id, step_success, runner_id=None)`. No `StepRun` is loaded anywhere in its body. §4.5 pins `step_run: StepRun` as required and §1.4 claims `_resolve_merge_source_branch` "takes the `step_run` it is already holding" — it is holding none. **Correction:** the contract must specify the lookup and its ambiguity: `StepRun.step_id == completed_step_id` matches a retry's second row, and `_trigger_card`'s marker carries `step_id=None` today (deliberately, per its own comment) — pin an ordering and pin that the marker keeps `step_id=None`, or it enters `_graph_step_outcomes` and votes on the verdict.

**3. T3 goes red at P3 and stays red through the acceptance gate.**
`tdd/e2e/test_graph_pipeline.py::TestGraphPipelineYAMLExport::test_yaml_export_includes_parallel_branches` creates a fan-out graph (`start→branch_a`, `start→branch_b`), exports it, asserts 200 and both names. §4.10 makes export refuse 409 on fan-out at P3 (B1). The file is owned by **B6, whose only phase is P6 — after the gate**. **Correction:** hand that class to B1 for the P3 export commit and rewrite it to assert the 409 and the named construct.

**4. Nobody is scheduled to make the single most load-bearing P3 edit.**
§3.2 gives A1 "P1, P2" only, but §4.3 puts the `PipelineRead.steps` deletion (and the `steps_graph`-required change) in A1's `backend/app/schemas/pipeline.py`. `scripts/verify_executor.py` reads `pipeline["steps"]` off that exact response in three places — A3's P3 work cannot land until A1 does a P3 it is not assigned. **Correction:** add P3 to A1's row and make it the ordering barrier for A3/A4/B1.

**5. `pipeline_create_payload()` defaults to `steps: []`; §1.6(c)+§4.3 turn that into a 422 and an unserializable row.**
11 call sites pass no steps (`test_pipelines_api.py` ×8 including `test_create_minimal_pipeline`, `test_pipeline_workflow.py` ×3). Today `PipelineCreate.validate_steps_definition` explicitly permits both empty — its comment is *"Allow both to be empty for initial creation"* — and the editor's create-then-author flow depends on it. The plan removes that capability with no replacement named. **Correction:** decide it explicitly. Either `PipelineRead.steps_graph` stays nullable for a not-yet-authored pipeline (with `POST /run` refusing on null), or create-then-author is redesigned and every caller converted.

**6. Fourteen files that must change are owned by nobody.**
Not in any agent list and not among §3.5's 24: `tdd/shared/factories/api.py` (defines `pipeline_step_payload`/`pipeline_create_payload` with `on_success`/`on_failure` defaults — see #5), `tdd/qa/qa4_support.py`, `tdd/qa/test_graph_definition_qa4.py`, `tdd/qa/test_api_fuzz_findings.py`, `tdd/e2e/test_control_layer.py`, `tdd/unit/control_runtime/test_run.py`, `tdd/unit/services/test_agent_step_dispatch.py`, `tdd/unit/services/execution/test_local_executor_hardening.py`, `tdd/unit/services/test_workspace_service.py`, `tdd/unit/services/workspace/test_pipeline_state_machine.py`, `tdd/unit/services/workspace/test_workspace_state_machine.py`, `tdd/unit/services/test_git_repo_manager.py`. Two more — `tdd/qa/test_graph_execution_qa4.py` and `tdd/qa/test_demo_polish_api.py` — are listed CONTESTED in §3.1 and then assigned to no agent at all.

**7. Two owners for `test_pipeline_local_dispatch.py`.**
`TestMergeActionBranchResolution` (line 933) and `TestLocalFailurePaths::test_linear_on_failure_next_continues_past_failed_local_step` (line 770) are in that file, which §3.3 gives to **B2** exclusively; §5.1 and §7.2 assign their graph rewrites to **A2**. **Correction:** move both classes into `tdd/unit/services/test_pipeline_executor.py` (A2) inside B2's own P3 commit.

**8. §4.2's `on_success == "stop"` refusal is over-broad and refuses convertible arrays.**
The real predicate is "produces an unreachable node", which `graph_definition_errors` already computes. A step with `on_success: stop` *and* `on_failure: next` still reaches `i+1` over a FAILURE edge and is perfectly convertible. **Correction:** build the graph, run `graph_definition_errors` on the result, refuse only on a non-empty list (naming the action that orphaned the tail). Delete the standalone stop-on-non-final rule.

**9. `on_success: next` on the LAST step is unspecified — and the dogfood pipeline has it.**
`.lazyaf/pipelines/test-suite.yaml`'s tenth step `verify-executor` is `on_success: next` with nothing after it. Today the `if i < len(steps) - 1` guard silently swallows it; §4.2 lifts that guard for `merge:`/`trigger:` and lists "unknown actions" as refusals without exempting terminal `next`. An agent reading "faithful, refusing" literally makes the acceptance pipeline itself unconvertible. **Correction:** state that `next`/`stop` on the terminal node yield no edge and no refusal, and add that case to `TestArrayToGraphConversion`.

**10. A conversion refusal can roll back an entire push sync, and `ValidationError` is not in the refusal contract.**
`sync_repo_pipelines`'s swallow-and-continue `except Exception` wraps **only** `yaml.safe_load` + `PipelineYaml(**data)`. `upsert_materialized_pipeline` is called outside it, under `except Exception: await session.rollback(); raise` — one bad file discards every other pipeline that push synced. And `PipelineStepYaml.type` is a bare `str` while `PipelineStepConfig.type` is `StepType`, so conversion raises pydantic `ValidationError`, which §4.2 never names. **Correction:** `upsert_materialized_pipeline` catches `(ArrayConversionError, ValidationError)` itself, writes `definition_error`, returns normally; add the `bananatype` test asserting a `definition_error` row and an otherwise-intact sync.

**11. §4.10's export loses `timeout`, `continue_in_context` and `id`; the xfail claim is wrong.**
`test_export_preserves_step_timeout_and_continuation` (QA4-10c, strict) asserts `"777" in exported_yaml` and `"continue_in_context" in exported_yaml`. Nothing in §4.10 says the array export writes either — nor step `id`, without which the round trip renames every node and defeats §1.6(b). **Correction:** pin the export field set (`id, name, type, config, on_success, on_failure, timeout, continue_in_context`); correct §6 item 11 — three xfails flip, the fourth only if this is fixed.

**12. The acceptance gate is unsatisfiable by this wave as scoped: T1 is red today and its red test is M14's.**
`tdd/tier_floors.json` T1 note, measured 2026-08-30: *"the tier is RED"*, `executed=4134 (4132 passed, 2 FAILED)`, failure (1) being `test_agent_config_contract.py::TestAgentConfigRoundTrip::test_consumer_keys_superset_of_producer_keys`, explicitly *"NOT this lane's"*. §5.2 demands T1 green. **Correction:** name "M14 hands over a green T1" as an owned gate precondition, or the gate gets waived on first contact — which is exactly the fake-green R4 exists to stop.

**13. `_merge_branch`'s merge-failure path fails the run with no error written anywhere.**
`else: logger.error(...); await self._complete_pipeline(db, pipeline_run, success=False)` — no StepRun error, unlike the unresolvable-branch path six lines above which does write one. §1.4 property 2 promises "names step id + action + reason" but describes the port as a re-key of the existing body. **Correction:** `_run_terminal_action` writes the reason onto the step's StepRun on *every* False return; §5.1 adds the negative test (merge returns `success: False` ⇒ a named error on the step, not just a red run).

**14. Delegating `validate_graph_integrity` to `graph_definition_errors` drags the executor into the schema layer.**
`graph_definition_errors` lives in `pipeline_executor.py`; `app/schemas/pipeline.py` currently imports only `app.models` and `app.schemas.*`, and no service imports `app.schemas.pipeline` at module level. Making the schema import the executor pulls `websocket`, `git_server`, `workspace`, `debug_session_service` and `model_endpoints.scheduler` into every router's import graph. **Correction:** move `graph_definition_errors`, `_first_cycle` and `unreached_graph_steps` down into the schema module and have the executor import them — also the R3-correct direction, since the schema is the definition-time authority.

**15. A routing failure now fires terminal actions for a step that never started.**
`_execute_graph_step`'s `if route_error is not None:` calls `_handle_graph_step_complete(..., False, None)`; under P1 that runs `actions.failure`, spawning a fix card or performing a `merge:` for a container that never ran. It is parity with v1's `_handle_action` path, so not a regression — but the plan asserts action semantics without covering it. **Correction:** state the rule (fire, or skip when undispatched) and pin it.

**16. `step_id=None` has two producers, so B5's debug fallback claim is wrong.**
`_trigger_card`'s marker StepRun carries no `step_id` *and* a real step's `step_index`; `_verify_graph_coverage`'s synthetic row carries `step_id=None` with `step_index=len(step_ids)`. §5.1's B5 line says the index fallback "covers only the graph-defect StepRun (`step_id=None`, `step_name='pipeline graph'`)". The marker collides with a real graph step's key. **Correction:** state the marker's identity rule and test it.

**17. Small, but they ship verbatim.** §5.4's `test_no_method_takes_an_is_graph_parameter` references `pe` before it is imported (the import sits inside the previous test's body) — `NameError`, and it is the tombstone for the fork itself. §7.4/§5.4's `SEARCH_ROOTS += ["cli/"]` is inert: `cli/lazyaf/cli.py` contains no matching token. §4.4's header ("B1 (2) and A5 (4) and B1 (1)") disagrees with its own table (B1: 3, A5: 4). Two of the "six live `runner-mock-e2e` references" are in `README.md` and `compose.test-mode.yml`; only the `.yml`, `.ts` ones are reachable by the grep's `SEARCHABLE_SUFFIXES`.

Things the plan gets right that I checked and did not re-litigate: the `_on_step_complete_locked` hidden fork (`graph and step_run.step_id`, confirmed), zero executed coverage of failure/always edges (confirmed — every hit in `tdd/` is definition or persistence, never dispatch), the seven writers being exhaustive (confirmed), `has_graph_definition()` having zero callers, `delete_directory_from_branch` having zero tests, `ci_gate.executed = passed + failed` making the three T3 tombstone deletions free, `_adopt_unversioned`'s presence-only classification, `_baseline_columns()` running the real 0001 so the baseline path still heals, and `0011` being untracked with the head genuinely unstable.