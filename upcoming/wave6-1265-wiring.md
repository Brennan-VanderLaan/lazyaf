# Wave 6 - Phase 12.6.5 Wiring Design: Experiments, matrix fan-out, and the leaderboard

Status: DESIGN - implementers build from this verbatim.

Inputs read: `PLAN.md` roadmap entry (line 946) + the detailed section (line 3141) + the
`Experiment` / `TestRun` / `PromptTemplate` / `StepUsage` model sketches (lines 150-300) +
R1-R8; `docs/milestone-13/api-surface.md` sections 4.1-4.3 and 6;
`docs/milestone-13/phase-specs-and-metrics.md` Part 1 (M1-M3) and Part 2 (variance,
separability, controls); `backend/app/services/pipeline_executor.py`
(`start_pipeline`, `_complete_pipeline`, `_notify_agent_run_complete`,
`_build_local_execution_config`'s agent payload); `backend/app/services/agent_run.py`
(the whole ad-hoc-run mechanism); `backend/app/services/usage_ingestion.py` +
`models/usage.py`; `backend/app/services/test_ingestion.py` + `models/testref.py`;
`backend/app/routers/spec.py` + `models/spec.py`; `backend/app/services/trigger_service.py`;
`runner-common/runner_common/executors/mock.py`; `runner-common/runner_common/pytest_lazyaf.py`;
`images/base/control/run.py`; `tdd/integration/test_migrations.py`;
`frontend/src/lib/stores/websocket.test.ts`; `upcoming/wave4-125-wiring.md` and
`wave5-126-wiring.md` for house style.

---

## 0. Ground truth found during recon (read this before arguing with the design)

**0.1 THE MIGRATION NUMBER IS ALREADY TAKEN. 12.6.5 uses `0008`.**
The wave brief pre-assigned `0007` to this lane, but Phase 12.6's deletion commit already
authored `backend/alembic/versions/0007_drop_polling_runner_columns.py` (`revision='0007'`,
`down_revision='0006'`), and `tdd/integration/test_migrations.py` already pins
`ALEMBIC_HEAD_REVISION = "0007"`. The slot is spent. **This design uses `0008`,
`down_revision='0007'`.**

Checked against the two sibling lane designs written in this same wave, so the chain is
unambiguous for the integrator: `upcoming/wave6-1266-wiring.md` section 8 explicitly claims
**no migration** and releases `0008` back to the pool, and `upcoming/wave6-127-wiring.md`
claims `0009` with `down_revision = '0008' if 12.6.6 lands one, otherwise the real head`.
With 12.6.6 landing none, the resulting chain is `0007 -> 0008 (12.6.5) -> 0009 (12.7)` and
12.7's conditional resolves to `'0008'`. If that changes, the only places the number appears
here are the filename, `revision`, `down_revision`, and the head constant in the parity test
(contract #9).

**0.2 The ad-hoc run mechanism is exactly the right seam and does not need
`pipeline_executor.py` touched.** `agent_run.start_adhoc_agent_run` creates a hidden
ephemeral `Pipeline` (name prefixed `__lazyaf_adhoc__`, filtered out of
`GET /api/pipelines` by `is_adhoc_pipeline_name`) plus a real, visible `PipelineRun`, and
calls `pipeline_executor.start_pipeline` verbatim. That buys the workspace volume, StepRun,
StepExecution + step JWT, control mode, streamed logs, `/test-results`, `/usage`, the
watchdog and cancellation with zero new machinery. An experiment cell is one of those runs.

**0.3 Completion already has exactly one durable hook.**
`_complete_pipeline` calls `_notify_agent_run_complete` -> `agent_run.on_run_complete`,
which routes on the PERSISTED `PipelineRun.trigger_type` / `trigger_ref` columns and no-ops
for every trigger type it does not own. Adding `"experiment"` to that dispatch is a
four-line edit - it is the registration-line pattern, not a rewrite (section 12.4).

**0.4 `start_pipeline` can complete a run SYNCHRONOUSLY** (image preflight failure, empty
step list, no graph entry points all call `_complete_pipeline` inline). `agent_run` documents
this at length because it burned the card path. Consequence for us: the cell -> run link
must be written BEFORE `start_pipeline` is called, which is why `trigger_ref = ExperimentRun.id`
is the load-bearing link and `experiment_runs.pipeline_run_id` is only a convenience mirror
(sections 2.5, 3.1).

**0.5 `TestRun` already has `model` and `prompt_template_id`, both NULL, both annotated
"reserved for Phase 12.6.5", and `GET /api/criteria/{id}/history` already returns them**
(`routers/test_results.py:144`). They are ours to fill. `prompt_version` does not exist yet.

**0.6 `StepUsage.pipeline_run_id` is denormalized, server-derived and indexed**
(`ix_step_usages_pipeline_run_id_role`, leading column). Cost per cell is therefore a join
on a column that already exists, with an index that already exists. No new usage columns.

**0.7 `PromptTemplate` has NO versions.** It is `{id, name, description, content}` with an
`updated_at`. PLAN sketches a `PromptVersion`; nothing implements it. A leaderboard that
groups by `(template, version, model)` while the template body is silently mutable is a
leaderboard that merges two different prompts into one row. Section 1.3 fixes this with the
smallest honest table.

**0.8 A push-triggered pipeline with no `branches:` pattern matches EVERY branch**
(`trigger_service.on_push`, line 423-433: "If no branches specified, match all"). A 20-cell
matrix that pushes 20 branches would fan out 20 CI runs that no experiment cap covers.
Section 2.4.

**0.9 The agent step's own test gate is already wired.**
`pipeline_executor._finish_local_step_locked` demotes a step whose ingested `TestRun` rows
contain a failure. So a cell whose suite came back red lands as a FAILED run without us
writing a line of gate code - and that failure is the measurement, not an error (section 3.2).

**0.10 The mock agent can already produce everything an experiment needs except a test
manifest.** `MockExecutor._usage` emits deterministic `provider="self-hosted"`,
`model="mock"`, `cost_usd="0.000000"`, `cost_source="cli-reported"`, and
`input_tokens = len(prompt)//4` - so two prompt variants produce genuinely different token
counts, which is real evidence that the prompt reached the container.
`LAZYAF_TEST_RESULTS_PATH` is injected into every step's environment by
`images/base/control/run.py:718`, and the mock executor runs in-process inside the wrapper,
so it can write the manifest there. One ~25-line addition to `MockExecutor` (section 9.3)
gives the exit gate scriptable per-variant outcomes with no second container.

**0.11 `frontend/src/lib/stores/websocket.test.ts` greps `backend/app` for every
`.broadcast("literal")` and fails in BOTH directions** if the frontend union disagrees. Any
new frame is a hard two-agent contract landing in one wave (contract #1). It also means the
service can call `manager.broadcast("experiment_status", ...)` directly - no edit to the
shared `websocket.py` is required, and this design takes that option to keep ownership clean.

**0.12 `tdd/integration/test_migrations.py` snapshots columns AND indexes** and asserts
migration-built parity with `Base.metadata.create_all`. The migration must mirror the models
exactly - same index names, same nullability, same types.

---

## 1. DATA MODEL + MIGRATION 0008

New file `backend/app/models/experiment.py`. Vocabularies are plain string columns validated
in the pydantic schemas (Card/spec/TestRef idiom). Money is `Numeric(18, 6)` and is `Decimal`
in Python, string on the wire (12.5 convention, `docs/milestone-13/api-surface.md` s0).

### 1.1 `experiments`

```python
class ExperimentStatus(str, Enum):
    DRAFT = "draft"                        # created, matrix editable, nothing dispatched
    RUNNING = "running"
    COMPLETE = "complete"                  # every cell terminal
    ABORTED = "aborted"
    BUDGET_EXHAUSTED = "budget_exhausted"  # cap stopped dispatch with cells left pending


class Experiment(Base):
    __tablename__ = "experiments"
    __table_args__ = (
        Index("ix_experiments_status_created_at", "status", "created_at"),
        Index("ix_experiments_target_type_target_id", "target_type", "target_id"),
    )

    id: str36 pk
    name: String(255) not null
    description: Text not null default ""
    target_type: String(32) not null          # "card" | "user_story"
    target_id: String(36) not null            # NOT an FK: an experiment's provenance
                                              # must survive its target being deleted
    repo_id: String(36) FK repos.id not null  # resolved at CREATE; cells need a repo
    matrix: Text not null                     # JSON, frozen at launch (section 2.2)
    verify: Text | null                       # JSON {image, command, timeout} or NULL
    budget_usd: Numeric(18,6) not null        # HARD cap, required, > 0
    max_concurrency: Integer not null default 2
    cell_timeout: Integer not null default 1800
    push_branches: Boolean not null default False
    status: String(24) not null default "draft"
    estimated_cost_usd: Numeric(18,6) | null  # what the dry run said AT LAUNCH
    estimate_basis: String(24) | null         # "historical-median" | "partial" | "no-history"
    budget_overrun_usd: Numeric(18,6) not null default 0   # recorded, never hidden
    created_by: String(255) | null
    created_at / updated_at / launched_at / completed_at
```

`budget_overrun_usd` exists for the same reason M13's `Trial.budget_overrun_usd` does: the
cap bounds *dispatch*, and spend already in flight when it trips is recorded rather than
quietly absorbed (section 5.3).

### 1.2 `experiment_runs` (the matrix cell)

```python
class ExperimentRunStatus(str, Enum):
    PENDING = "pending"
    DISPATCHING = "dispatching"   # CAS-claimed, run not yet created
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"             # ran, measured, did not satisfy the suite
    ERROR = "error"               # ran, measured NOTHING (infra/agent crash)
    CANCELLED = "cancelled"       # experiment aborted before dispatch
    SKIPPED_BUDGET = "skipped_budget"


class ExperimentRun(Base):
    __tablename__ = "experiment_runs"
    __table_args__ = (
        # The board's primary scan, and the pump's "next pending cell" read.
        Index("ix_experiment_runs_experiment_id_cell_index",
              "experiment_id", "cell_index", unique=True),
        Index("ix_experiment_runs_experiment_id_status", "experiment_id", "status"),
        # Reverse lookup from a run detail view. NOT unique: a future retry lane
        # would break a unique constraint and gain nothing today.
        Index("ix_experiment_runs_pipeline_run_id", "pipeline_run_id"),
    )

    id: str36 pk
    experiment_id: String(36) FK experiments.id ondelete CASCADE, not null
    cell_index: Integer not null              # deterministic: models x prompts x repeat
    variant_index: Integer not null           # cell_index // repeat - repeats share it
    agent: String(32) not null                # "mock" | "claude-code" | "gemini"
    model: String(128) | null                 # NULL = the CLI's own default
    prompt_template_id: String(36) FK prompt_templates.id | null   # NULL = platform default
    prompt_version_id: String(36) FK prompt_versions.id | null
    prompt_version: Integer | null            # denormalized int, frozen at launch
    label: String(128) | null                 # human name for the variant, from the matrix
    repeat_index: Integer not null            # 0-based
    pipeline_run_id: String(36) | null        # convenience mirror; NOT the link (0.4)
    status: String(24) not null default "pending"
    error: Text | null
    started_at / completed_at / created_at
```

**No `cost_usd`, no `tests_passed/failed/skipped` columns.** `StepUsage` and `TestRun` are
the source of truth for both; a materialized copy is a second writer that will drift (R3).
A matrix is capped at 200 cells (section 5.2), so live aggregation is a few hundred indexed
rows - measured in section 4.4, not assumed.

### 1.3 `prompt_versions` (the smallest honest fix for 0.7)

```python
class PromptVersion(Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        Index("ix_prompt_versions_template_id_version", "template_id", "version", unique=True),
        Index("ix_prompt_versions_template_id_content_hash",
              "template_id", "content_hash", unique=True),
    )
    id: str36 pk
    template_id: String(36) FK prompt_templates.id ondelete CASCADE, not null
    version: Integer not null                 # 1-based, per template
    body: Text not null                       # the FROZEN text that actually ran
    content_hash: String(64) not null         # sha256 hex of `body`
    created_at: DateTime
```

**Ownership split, stated once so it cannot drift:** `PromptTemplate.content` is the
EDITABLE DRAFT and stays owned by `routers/spec.py`. `PromptVersion` is the IMMUTABLE RECORD
OF WHAT RAN, created get-or-create-by-content-hash by the experiment launcher and never
updated. That is not two sources of truth for one datum - it is one source of truth for two
different data. `routers/spec.py` is NOT edited by this phase.

Versions are resolved for the WHOLE matrix once, at launch, before any cell dispatches, so a
template edited mid-experiment cannot split one variant across two prompt bodies.
`version = 1 + COALESCE(MAX(version), 0)` per template; the `(template_id, version)` unique
index makes a concurrent insert an `IntegrityError`, absorbed with the codebase's
rollback/re-select idiom (`app/services/execution/idempotency.py`, mirrored in
`test_ingestion._resolve_refs`).

### 1.4 Additions to `test_runs`

```python
# models/testref.py - TestRun
experiment_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
prompt_version:    Mapped[int | None] = mapped_column(Integer, nullable=True)
# plus the new index:
Index("ix_test_runs_experiment_run_id_test_ref_id", "experiment_run_id", "test_ref_id")
```

Deliberately NOT foreign keys, for exactly the reason `TestRun.pipeline_run_id` is not one
(its own comment): runs are provenance records that must survive pruning. The index shape
mirrors M13's `test_runs (trial_iteration_id, test_ref_id)`.

`model` and `prompt_template_id` already exist and start being written (section 3.3).

**Why `experiment_run_id` and NOT `experiment_id`:** one link, not two. Every
experiment-scoped read joins `experiment_runs`, which is indexed on
`(experiment_id, status)` and `(experiment_id, cell_index)`. A second denormalized column
would be a second thing to keep true. (M13 may add one when `trials` arrives; it will have
its own writer.)

### 1.5 What this phase deliberately does NOT add

| Not added | Why |
|---|---|
| `pipeline_runs.experiment_id` (PLAN sketch) | `trigger_type="experiment"` + `trigger_ref=<cell id>` are already-persisted columns that say the same thing, and they are written at run CREATION, which the sketch's column could not beat (0.4). PLAN's `test_pipeline_run_carries_experiment_id` becomes `test_cell_run_carries_trigger_ref` - same claim, durable link. |
| Experiment coordinates on `step_usages` | The join key (`pipeline_run_id`) and its index already exist (0.6). M13 adds `(trial_id, role)` when `trials` exists. |
| Cost / test-count columns on `experiment_runs` | R3. See 1.2. |
| `target_type="feature"` | Not implementable shallowly: a feature spans repos and has no single task text. `422` naming it (R1: refused loudly, not silently degraded to a story). |
| Bootstrap CIs, KM curves, `separable`, ranking | 13.4 owns them. Section 4.5 is the exact boundary. |

### 1.6 The migration

`backend/alembic/versions/0008_experiments.py`, `revision='0008'`, `down_revision='0007'`.

Follows the 0004/0005/0006 convention verbatim: module docstring stating the scope decisions,
`inspector = sa.inspect(op.get_bind())`, every `create_table` guarded by
`inspector.has_table`, every index guarded by name. Creates `experiments`,
`experiment_runs`, `prompt_versions`; adds two columns + one index to `test_runs`.

- The `test_runs` additions carry NO foreign keys, so plain `op.add_column` works and
  `op.batch_alter_table` (the SQLite rebuild 0006 needed) is not required.
- `downgrade()` drops in reverse: index, two columns, then the three tables.
- Parity (0.12): the model definitions and this file must produce byte-identical snapshots.
  Index NAMES are part of that snapshot - copy them from section 1 exactly.

---

## 2. HOW A CELL EXECUTES

### 2.1 One cell = one ad-hoc agent run

```
Experiment.launch()
  -> N ExperimentRun rows (all coordinates frozen)
  -> pump: for each claimed cell
        ephemeral Pipeline  name = adhoc_pipeline_name("experiment", cell.id)
                            steps = [agent step] (+ [verify script step] if experiment.verify)
        pipeline_executor.start_pipeline(
            trigger_type="experiment",
            trigger_ref=cell.id,
            trigger_context={branch, base_branch, repo_id, adhoc: True,
                             experiment_id, experiment_run_id, cell_index})
```

`pipeline_executor.py` is **not edited**. `agent_run.py` is edited only by the integrator's
four-line dispatch registration (section 12.4). The cell-run builder lives in the new
`backend/app/services/experiment_service.py` and imports `build_agent_step_config`,
`adhoc_pipeline_name` and `ADHOC_PREFIX` from `agent_run` read-only - so the hidden-pipeline
filter keeps hiding these, for free.

**Why not call `start_adhoc_agent_run` itself:** it builds exactly one step and an experiment
cell may need two (agent + verify). Reusing its *builders* rather than its *entry point* is
the smaller coupling; the step-config vocabulary stays single-sourced in
`build_agent_step_config`, which is the part that must not fork.

The steps array is the v1 (`Pipeline.steps`) format, the same one `agent_run` writes today.
When 12.8 converts execution to graph-only, this converts with it, in one place.

### 2.2 The matrix, and how model + prompt bind to a cell

```jsonc
"matrix": {
  "models": [
    {"agent": "claude-code", "model": "claude-opus-5", "label": "opus"},
    {"agent": "claude-code", "model": "claude-haiku-4-5", "label": "haiku"}
  ],
  "prompts": [
    {"prompt_template_id": "tpl_9f2c...", "label": "impl-from-story"},
    {"prompt_template_id": null,          "label": "platform default"}
  ],
  "repeat": 3
}
```

- **Model-axis entries are OBJECTS and `agent` is REQUIRED.** No string sugar, no
  model-name-to-CLI inference table. A guessed agent is a silent fallback (R1); a `422`
  naming the entry is not. `agent` must be one of `agent_run.AGENT_BY_RUNNER_TYPE`'s values.
- `model: null` is legal and means "the CLI's own default" - a real control variant.
- `prompt_template_id: null` is legal and means "the platform default prompt"
  (`agent_prompt.DEFAULT_PROMPT_TEMPLATE`) - the other control variant.
- Optional per-entry `"step_config": {...}` overlay, merged into the cell's step config
  (models overlay first, prompts second). This is the escape hatch for knobs the platform
  does not model (thinking budget, temperature) and it is what makes mock-agent experiments
  scriptable. **Reserved keys refuse the launch with 422 rather than being dropped:**
  `agent`, `model`, `prompt_template`, `base_branch`, `branch`, `commit`, `card_id`. An
  overlay that could silently rewrite the axis it is varying is the definition of dark.
- `repeat >= 1`. `cells = len(models) * len(prompts) * repeat`.
- Cell ordering is deterministic and is part of the contract:
  `cell_index = ((model_i * len(prompts)) + prompt_i) * repeat + repeat_i`, and
  `variant_index = cell_index // repeat`.

Binding, per cell, through machinery that already exists end to end:

| Axis | Written into | Reaches the agent via |
|---|---|---|
| model | `step_config["model"]` (`build_agent_step_config(model=...)`) | `_build_local_execution_config` -> `exec_config["agent"]["model"]` -> `/workspace/.control/agent.<id>.json` -> `agent_config.model` -> the CLI flag |
| prompt | `step_config["prompt_template"] = PromptVersion.body` | the BACKEND renders it (`agent_prompt.render_agent_prompt`, one producer since 12.5) and ships the finished text in the agent config |

Nothing new on the wire. The control-layer protocol stays frozen - which is the whole point
of 12.5 having landed the usage channel before this phase.

### 2.3 The verify step (where TestRuns actually come from)

`Experiment.verify = {"image": "...", "command": "...", "timeout": N}` appends one script
step to every cell run:

```python
{"id": "verify", "name": "Verify", "type": "script",
 "config": {"image": verify["image"], "command": verify["command"]},
 "timeout": verify.get("timeout", 900), "on_success": "next", "on_failure": "stop"}
```

The command runs the target repo's suite with the tie-back plugin
(`pytest -p runner_common.pytest_lazyaf ...`); the plugin writes
`$LAZYAF_TEST_RESULTS_PATH` and the control runtime ships it. No new protocol.

`verify: null` is legal and means the only test evidence is whatever the agent itself
shipped. A leaderboard cell with no `TestRun` rows renders `pass_rate: null` with
`reason: "no test evidence"` - **never `0%`** (PLAN: `test_leaderboard_handles_zero_runs`).

Ordering note (stated, not silent): the agent step is `on_failure: stop`, so a cell whose
agent step failed never runs verify. That is deliberate for 12.6.5 - a crashed agent produced
no measurement, which is precisely the `error` classification in 3.2, not a `0%` score.

### 2.4 Branches, and the push-trigger hazard (0.8)

Every cell gets its own branch: `lazyaf/exp/{experiment_id[:8]}/{cell_index:03d}`.

`push_branches` defaults to **False**: cells commit inside their ephemeral workspace and do
not push. Rationale: a 20-cell matrix pushing 20 branches into a repo with a push-triggered
pipeline that declares no `branches:` pattern starts 20 CI runs that the experiment cap does
not cover and did not estimate.

`push_branches: true` is supported (it is how you keep the diffs), and when it is set the
dry-run response carries an explicit warning naming every push-triggered pipeline in the repo
that would match, e.g.
`"push_branches=true: pipeline 'Test Suite' triggers on every branch and will start 20 additional runs not covered by this experiment's cap"`.
Stated, never silent.

### 2.5 The cell -> run link, and the synchronous-completion trap (0.4)

`trigger_ref = ExperimentRun.id` is written **before** `start_pipeline` is called, as part of
the run row itself. Everything downstream resolves the cell from it:

- `on_cell_complete` looks the cell up by `trigger_ref`, so a run that failed image preflight
  *inside* `start_pipeline` still lands its cell correctly.
- test-result stamping resolves by `trigger_type`/`trigger_ref` too (section 3.3), which
  removes the race entirely: a mock step can finish in under 100 ms, before the pump could
  have written `pipeline_run_id`.

`experiment_runs.pipeline_run_id` is written by the pump after `start_pipeline` returns, and
backfilled by `on_cell_complete` if the pump never got there. It is for the UI's "open this
run" link and for nothing load-bearing.

### 2.6 The pump: concurrency, re-entrancy, and the CAS

There is no polling loop. Dispatch is driven by launch and by cell completion.

```python
async def pump(db, experiment_id: str) -> int:
    """Dispatch as many pending cells as concurrency and budget allow.
    Returns the number dispatched. NEVER raises."""
```

1. **Re-entrancy guard.** One `asyncio.Lock` per experiment id, plus a `_repump` flag: a
   caller that finds the lock held sets the flag and returns; the holder loops until the flag
   is clear. This is what stops `pump -> dispatch -> synchronous failure -> on_cell_complete
   -> pump` from recursing 200 frames deep on a preflight failure.
2. **Claim by CAS, never by read-then-write:**
   ```python
   res = await db.execute(
       update(ExperimentRun)
       .where(ExperimentRun.id == cell_id, ExperimentRun.status == "pending")
       .values(status="dispatching", started_at=datetime.utcnow()))
   if res.rowcount != 1:   # someone else took it
       continue
   ```
3. Live-cell count = `COUNT(*) WHERE experiment_id=... AND status IN ('dispatching','running')`,
   read from the DB, never from memory - a backend restart must not lose the count.
4. Budget gate before every single dispatch (section 5.3).
5. On any exception building or starting a cell run: the cell goes `error` with the exception
   text in `cell.error`, and the pump continues with the next cell. One bad cell never kills a
   matrix.
6. When no cell is pending and none is live, the experiment finalizes:
   `aborted` if it was aborted, else `budget_exhausted` if any cell is `skipped_budget`, else
   `complete`. `completed_at` set, `budget_overrun_usd` computed, both frames broadcast.

**Restart durability (R1: nothing dark).** The pump is in-process; a backend restart with
cells still `pending` leaves the experiment stalled. That state is REPORTED, not hidden:
`GET /api/experiments/{id}` returns `"stalled": true` when `status == "running"`, no cell is
live, and pending cells remain. `POST /api/experiments/{id}/resume` re-pumps and returns the
number dispatched. Cells left in `dispatching` with no run after a restart are reset to
`pending` by `resume` (they never started; the run row would exist otherwise).
The integrator MAY additionally wire `resume_stalled_experiments()` into the lifespan
(section 12.3, optional line) - the endpoint is the guaranteed path either way.

### 2.7 Abort

`POST /api/experiments/{id}/abort`: set `status="aborted"`, then CAS every `pending` cell to
`cancelled`. Running cells are **left to finish** (PLAN:
`test_abort_cancels_pending_runs`); their results still land and still count. The last
completion finalizes the experiment. Abort on a terminal experiment is a `409`, not a no-op
that pretends to have done something.

---

## 3. RECORDING WHAT HAPPENED

### 3.1 `on_cell_complete(db, pipeline_run, success)`

Lives in `experiment_service.py`, called from `agent_run.on_run_complete`'s dispatch
(section 12.4). Never raises (the caller already logs-and-swallows; this one is defensive
too, because a raised exception here would abandon the pump and stall the matrix).

```
cell = SELECT ... WHERE id = pipeline_run.trigger_ref
if cell is None or cell.status is terminal:  return      # idempotent
cell.pipeline_run_id = pipeline_run.id                    # backfill
cell.status = classify(...)   # 3.2
cell.completed_at = utcnow()
broadcast experiment_cell_status
await pump(db, cell.experiment_id)
```

### 3.2 Cell outcome classification - from persisted evidence only

| Run outcome | TestRun rows for the run | Cell status | In leaderboard denominators |
|---|---|---|---|
| `passed` | any / none | `passed` | yes |
| `failed` | >= 1 row exists | `failed` | yes |
| `failed` | zero rows | `error` | **no** - counted in `error_rate` and printed |
| `cancelled` | any | `cancelled` | no |

No string-matching on error messages, no heuristics. "The suite was red" and "nothing was
ever measured" are different facts and the schema keeps them different, which is
`phase-specs-and-metrics.md`'s error rule ("an agent that writes bad code is *not* an error -
it is an unsolved trial") implemented at the only point where it is cheap to get right.
`error_rate > 0.10` for a variant sets a leaderboard warning (section 4.5).

### 3.3 Stamping the coordinates onto `TestRun` - server-derived, never from the wire

The ONE edit to `backend/app/services/test_ingestion.py`. `_resolve_run_context` already
selects `PipelineRun.trigger_context` in a single joined query; extend that same query with
`PipelineRun.trigger_type` and `PipelineRun.trigger_ref`, and when
`trigger_type == "experiment"` load the cell (one indexed PK read) and carry its coordinates
on `_RunContext`:

```python
@dataclass
class _RunContext:
    ...existing fields...
    experiment_run_id: str | None = None
    model: str | None = None
    prompt_template_id: str | None = None
    prompt_version: int | None = None
```

...then set those four on both the create and the update branch of `ingest_manifest`.

**The wire carries none of this.** PLAN sketched
`test_ingest_propagates_experiment_context` - the manifest forwarding model/prompt from the
container. This design deviates deliberately: the container is untrusted, the backend already
knows the answer from the row it created, and `usage_ingestion`'s own docstring establishes
the precedent ("`step_run_id, pipeline_run_id` | HERE - never trusted from the wire"). A
step cannot mislabel which variant it was. The renamed contract test is
`test_ingest_stamps_experiment_coordinates_from_the_cell` and its negative twin is
`test_ingest_ignores_wire_supplied_model` (contract #4).

`TestResultsManifest` (schemas/testref.py) is **not** changed. The frozen protocol stays
frozen.

---

## 4. AGGREGATION

All of it lives in `backend/app/services/experiment_metrics.py` as **pure functions over
fetched rows**, unit-tested against fixtures. No metric is computed inline in an endpoint -
that rule is lifted verbatim from `phase-specs-and-metrics.md` Part 1 so 13.4's
`bench_metrics.py` can absorb these without an archaeology pass.

### 4.1 Variant identity

`variant = (agent, model, prompt_template_id, prompt_version)`. Repeats collapse into it;
`variant_index` on the cell row makes the grouping an integer comparison rather than a
four-way tuple match in SQL. `label` comes from the matrix entries (`"opus / impl-from-story"`).

### 4.2 Pass-rate per criterion

```sql
SELECT er.variant_index,
       trf.criterion_id,
       SUM(CASE WHEN tr.status = 'passed'  THEN 1 ELSE 0 END) AS passed,
       SUM(CASE WHEN tr.status = 'failed'  THEN 1 ELSE 0 END) AS failed,
       SUM(CASE WHEN tr.status = 'skipped' THEN 1 ELSE 0 END) AS skipped
FROM test_runs tr
JOIN experiment_runs er ON er.id = tr.experiment_run_id
JOIN test_refs      trf ON trf.id = tr.test_ref_id
WHERE er.experiment_id = :experiment_id
  AND er.status IN ('passed', 'failed')          -- error/cancelled excluded (3.2)
GROUP BY er.variant_index, trf.criterion_id
```

Rules, each with a named test:

- `pass_rate = passed / (passed + failed)`. **Skipped is excluded from the denominator**
  (`test_leaderboard_filters_skipped`).
- Denominator `0` -> `pass_rate: null` plus a `reason` string, **never `0.0`**
  (`test_leaderboard_handles_zero_runs`).
- `criterion_id IS NULL` (a `TestRef` with no criterion link, including auto-created orphans)
  is **not dropped** - it aggregates into a separate `unlinked_tests` block per variant. Tests
  that ran and nobody counted is exactly the kind of quiet hole R1 exists to prevent.
- The variant headline is the **MACRO** average over criteria (equal weight per criterion),
  with the pooled micro rate carried alongside as a footnote field. This is
  `phase-specs-and-metrics.md` M1's macro rule with "criterion" in the seat "case" occupies
  there; a micro headline would let one criterion with 40 tests own the number.

### 4.3 Cost per cell, and the effort axis

```sql
SELECT er.id, er.variant_index, su.cost_usd, su.cost_source, su.wall_clock_ms,
       su.input_tokens, su.output_tokens
FROM experiment_runs er
JOIN step_usages su ON su.pipeline_run_id = er.pipeline_run_id
WHERE er.experiment_id = :experiment_id
```

Rows are fetched and summed **in Python over `Decimal`** - never `SUM()` in SQL. SQLite
returns a float for `SUM(NUMERIC)`, and this codebase's money rule is "Decimal in Python and
in the DB, string on the wire, no floats for dollars, ever" (`models/usage.py` docstring).
The row count is bounded by `cells * steps_per_cell <= 400`.

Per variant:
- `cost_usd_total` - sum of non-NULL `cost_usd`.
- `cost_usd_per_run_median` - **median**, not mean, over the variant's cells. M2's heavy-tail
  argument applies at any n.
- `cost_coverage` = rows with `cost_source != 'unknown'` / all rows. Surfaced on **every**
  cell and variant, per api-surface 4.2: a variant at 0.6 coverage has an unenforced budget
  and must say so.
- `wall_clock_ms_median`, `input_tokens_total`, `output_tokens_total`.

### 4.4 Indexes these queries need

| Table | Index | Status |
|---|---|---|
| `test_runs` | `(experiment_run_id, test_ref_id)` | **new**, section 1.4 |
| `experiment_runs` | `(experiment_id, cell_index)` UNIQUE | new |
| `experiment_runs` | `(experiment_id, status)` | new - the pump's live count and the budget scan |
| `experiment_runs` | `(pipeline_run_id)` | new - the usage join and the run-detail reverse lookup |
| `step_usages` | `(pipeline_run_id, role)` | **already exists** (0.6); leading column serves the join |
| `test_refs` | PK + `(criterion_id)` | already exist |
| `experiments` | `(status, created_at)`, `(target_type, target_id)` | new - list and "experiments on this card" |

### 4.5 The boundary with Milestone 13 (do not cross it)

12.6.5 **reports**. It does not rank. Every leaderboard response carries:

```json
"ranked": false,
"note": "Reported, not ranked. Ranking requires the paired cluster bootstrap and the separability rule (Milestone 13.4, docs/milestone-13/phase-specs-and-metrics.md Part 2). Sort the table if you like; the platform makes no claim that one variant beats another."
```

Hard behaviours borrowed from 13.4 that are cheap enough to honour NOW, so the board never
teaches the wrong habit:

- `n < 3` repeats -> `"insufficient_repeats": true` on the variant, point values only.
- `error_rate > 0.10` -> warning `"{label}: {k}/{n} cells errored ({pct}%) - numbers shown, comparison disabled"`.
- `cost_coverage < 0.9` pooled -> warning naming the unpriced variants.
- No `separable`, no CIs, no "winner", no Holm correction, no KM curve. Those arrive with
  `bench_metrics.py`. Anything this phase emits must remain TRUE after 13.4 lands.

---

## 5. GUARDRAILS

### 5.1 The dry run

`POST /api/experiments` with `"dry_run": true` returns `200` and **creates nothing** (the
shape api-surface 4.1 already specifies). `GET /api/experiments/{id}/estimate` runs the same
pure function against a saved draft.

```json
{
  "cells": 12, "models": 2, "prompts": 2, "repeat": 3, "runs": 12,
  "estimated_cost_usd": "7.44",
  "estimate_basis": "historical-median",
  "per_variant": [
    {"label": "opus / impl-from-story", "agent": "claude-code", "model": "claude-opus-5",
     "prompt_template_id": "tpl_9f2c...", "runs": 3,
     "estimate_usd": "1.86", "basis": "historical-median", "samples": 14}
  ],
  "budget_usd": "5.00",
  "within_budget": false,
  "warnings": [
    "variant 'haiku / platform default': no priced history for model 'claude-haiku-4-5' - its cost is NOT in this estimate",
    "push_branches=true: pipeline 'Test Suite' triggers on every branch and will start 12 additional runs not covered by this experiment's cap"
  ]
}
```

**Estimate basis - history, not a price table.** Per variant: the median `cost_usd` of the
most recent 50 `step_usages` rows with that `model` and `cost_source != 'unknown'`. This is
the owner's 2026-08-29 decision applied consistently ("while the CLIs keep reporting cost, a
second pricing table is a second source of truth that will drift"). With no history the
variant contributes **nothing** to the total and `estimate_basis` degrades to `"partial"`
(some variants priced) or `"no-history"` (none) - the number is then explicitly a LOWER
BOUND and says so. A missing estimate never silently reads as `$0.00`.

### 5.2 Launch validation (all `422` with the offending value named)

| Rule | Message shape |
|---|---|
| `matrix.models` non-empty AND `matrix.prompts` non-empty | `test_create_experiment_validates_matrix` |
| `repeat >= 1` | |
| `cells <= 200` (`EXPERIMENT_MAX_CELLS`) | names the computed count |
| `budget_usd > 0`, required | a cap that can be omitted is not a cap |
| `1 <= max_concurrency <= 8` | |
| every `agent` in the known vocabulary | names the unknown value and the legal set |
| every `prompt_template_id` resolves | `404`-style detail naming the id |
| target resolves; `target_type in {card, user_story}` | `feature` -> 422 naming the phase |
| `user_story` target requires an explicit `repo_id` in `feature.repo_ids` | a story spans repos; guessing one is dark |
| step_config overlay touches no reserved key | names the key (section 2.2) |
| `estimated_cost_usd > budget_usd` while `estimate_basis == "historical-median"` | refuse: raise the cap or shrink the matrix. Under `partial` / `no-history` the launch proceeds (we cannot prove it) and the response echoes `"budget_enforced_at_dispatch": true` |
| already launched | `409` |

### 5.3 The cap, enforced HARD at dispatch

Before **every single** cell dispatch the pump recomputes observed spend from `step_usages`
(section 4.3's query, summed in `Decimal`) and refuses when
`observed_spend >= experiment.budget_usd`. Refused cells go `skipped_budget` (all of them, in
one pass - the matrix does not dribble), the experiment finalizes as `budget_exhausted`, and
the response/frames say so.

Three properties stated plainly, because a cap that is quietly approximate is worse than none:

1. **The cap bounds DISPATCH, not in-flight spend.** Maximum overshoot is bounded by
   `max_concurrency` cells already running when the cap trips. Whatever overshoot occurs is
   written to `Experiment.budget_overrun_usd` and rendered next to the cap - the M13
   `budget_overrun_usd` contract, one phase early.
2. **`cost_source="unknown"` rows count as ZERO against the cap** (api-surface 4.2 -
   "nothing else is defensible"), which is exactly why `cost_coverage` is on every cell and
   variant: coverage 0.4 means the cap is largely unenforced and the UI must show that.
3. **No history does not disable the cap.** The estimate is advisory; enforcement runs off
   observed `StepUsage`, so an unpriceable model is still stopped once real dollars land.

Contract test: `test_cap_stops_dispatch` - synthetic `StepUsage` rows push observed spend past
the cap; assert no further cell leaves `pending`, remaining cells are `skipped_budget`, and
`experiment.status == "budget_exhausted"`.

---

## 6. REST SURFACE

New router `backend/app/routers/experiments.py`. Schemas in
`backend/app/schemas/experiment.py`, which is THE source of truth for these wire shapes (R3);
`frontend/src/lib/api/types.ts` mirrors it and contract #2 pins the mirroring.

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/experiments` | create, or dry-run with `"dry_run": true` | `201` / `200` |
| `GET /api/experiments` | list; `?status=&target_id=&repo_id=` | newest first |
| `GET /api/experiments/{id}` | detail: experiment + cells + progress + spend + `stalled` | |
| `PATCH /api/experiments/{id}` | name/description/budget/concurrency; matrix only while `draft` | `422` on a launched matrix |
| `DELETE /api/experiments/{id}` | draft or terminal only | `422` while running |
| `GET /api/experiments/{id}/estimate` | the dry run for a saved draft | |
| `POST /api/experiments/{id}/launch` | freeze prompt versions, create cells, pump | `202`; `409` if launched |
| `POST /api/experiments/{id}/abort` | cancel pending, let running finish | `409` if terminal |
| `POST /api/experiments/{id}/resume` | re-pump after a restart/stall | returns `{"dispatched": n}` |
| `GET /api/experiments/{id}/results` | per-CELL rows with coordinates, status, cost, tests | the matrix view |
| `GET /api/experiments/{id}/leaderboard` | per-VARIANT aggregation (section 4) | |
| `GET /api/leaderboards/feature/{feature_id}` | cross-experiment, all criteria under a feature; `?experiment_id=` repeatable | PLAN's API table |

`GET /api/leaderboards/feature/{id}` also emits one extra row with `"variant": null`,
labelled `"non-experiment runs"`, covering `TestRun`s with `experiment_run_id IS NULL` for
those criteria - the repo's ordinary baseline, free, and honest about what it is.

WS frames (broadcast directly via `manager.broadcast(...)`, no edit to `websocket.py`):

```python
"experiment_status"      # {id, name, status, cells_total, by_status: {...},
                         #  spend_usd, budget_usd, cost_coverage, stalled}
"experiment_cell_status" # {id, experiment_id, cell_index, variant_index, status,
                         #  pipeline_run_id, label, agent, model,
                         #  prompt_template_id, prompt_version}
```

Emitted on: launch, every cell status transition, and every experiment finalization.

---

## 7. MCP + CLI

`backend/app/mcp/server.py`, thin `httpx` wrappers in the existing `@mcp.tool()` idiom:

- `launch_experiment(name, target_type, target_id, models, prompts, repeat=1, budget_usd, verify=None, dry_run=False, repo_id="")` - PLAN's named tool. With `dry_run=True` it returns the estimate and creates nothing, so Claude Desktop can cost a matrix before spending.
- `get_experiment(experiment_id)` - status + per-cell progress.
- `get_leaderboard(experiment_id)` - the variant table.
- `abort_experiment(experiment_id)`.

CLI (`cli/lazyaf/cli.py`) is **out of scope** for this phase; api-surface 5.2's `bench` group
arrives with M13. Stated so its absence is a decision, not an oversight.

---

## 8. UI (R8)

Route `/experiments` -> `frontend/src/lib/pages/ExperimentsPage.svelte`, plus a leaderboard
panel reachable from a Feature on the existing Specs page.

Surfaces, in one page, no deep modals (the Specs page's established idiom):

1. **List** - name, target, status pill, cells done/total, spend vs cap with a bar,
   `cost_coverage` badge, live from the two WS frames.
2. **Create** - target picker (card / story), model rows (`agent` select + `model` text +
   label), prompt rows (template select incl. "platform default"), repeat, budget, concurrency,
   verify block, `push_branches` toggle. **Launch is gated behind the dry run:** the estimate
   panel (cells, runs, dollars, basis, warnings) must be fetched and shown before the Launch
   button enables. Warnings render verbatim; the estimate never renders `$0.00` for an
   unpriced variant - it renders the basis string.
3. **Detail / matrix** - a grid, rows = model variants, cols = prompt variants, each cell a
   stack of repeat chips coloured by status, linking to the run view.
4. **Leaderboard** - one row per variant: macro pass-rate (or `N/A`), per-criterion
   expansion, cost total + median per run, `cost_coverage`, wall-clock median, error rate.
   The `ranked: false` note renders **verbatim and always**, and `insufficient_repeats`
   /`error_rate` warnings render as badges on the row. Sorting is a client convenience and is
   labelled as such.

`data-testid` vocabulary (contract #3, the Playwright spec depends on it):
`experiments-page`, `nav-experiments`, `experiment-create-btn`, `matrix-model-row`,
`matrix-prompt-row`, `dry-run-btn`, `dry-run-panel`, `dry-run-warning`, `launch-btn`,
`experiment-item`, `experiment-status`, `cell-chip`, `leaderboard-table`,
`leaderboard-row`, `leaderboard-not-ranked-note`, `criterion-rate`.

---

## 9. TESTS

### 9.1 T1 (unit / non-Docker integration)

| File | Pins |
|---|---|
| `tdd/unit/models/test_experiment_models.py` | table names, index names, cascade, status vocabularies; `cell_index`/`variant_index` arithmetic |
| `tdd/unit/services/test_experiment_validation.py` | every 5.2 rule, one test each, each asserting the message names the offending value |
| `tdd/unit/services/test_experiment_matrix_expansion.py` | NxMxR -> cell rows with correct frozen coordinates and deterministic ordering (`test_launch_creates_pipeline_run_per_cell`) |
| `tdd/unit/services/test_prompt_version_freeze.py` | get-or-create by content hash; a template edited between two launches yields version 2; a concurrent insert is absorbed; versions resolve before any dispatch |
| `tdd/unit/services/test_experiment_scheduler.py` | concurrency ceiling; CAS single-winner under two concurrent pumps; re-entrancy guard (a synchronous completion does not recurse); `test_experiment_completes_when_all_runs_terminal`; `test_abort_cancels_pending_runs`; stall detection + `resume` |
| `tdd/unit/services/test_experiment_budget.py` | `test_cap_stops_dispatch`; overrun recorded; `unknown` cost counts as zero and lowers coverage; estimate basis strings; no-history never reads as `$0` |
| `tdd/unit/services/test_experiment_metrics.py` | every 4.2/4.3 rule against fixtures: skipped excluded; zero denominator -> `null`; macro vs micro; unlinked tests bucketed; median not mean; `insufficient_repeats`; `error_rate` warning; `ranked` is always `false` |
| `tdd/integration/api/test_experiments_api.py` | the whole surface incl. `409`/`422` paths and the WS frames (real manager, not an AsyncMock - R6) |
| `tdd/integration/api/test_experiment_stamping.py` | contract #4 both ways: coordinates stamped from the cell; wire-supplied `model` ignored; non-experiment runs still stamp NULL |
| `runner-common/tests/test_mock_test_results.py` | the mock manifest emission (9.3) |
| `frontend/src/lib/stores/experiments.test.ts` | store reducers over the two WS frames |

### 9.2 The exit gate: `tdd/e2e/test_experiment_matrix.py`

2 models x 2 prompts x repeat 1 = 4 cells on mock agents, quick tier (not `@slow`), real
containers via the existing T3 preflight.

Fixture shape: prompt template **A** has a deliberately long body and **B** a short one (so
`input_tokens` differ); model variant **mock-pass** scripts a manifest of
`{c1: passed, c2: passed, c3: skipped}` and **mock-fail** scripts
`{c1: passed, c2: failed, c3: skipped}`, via the per-axis `step_config` overlay carrying
`mock_config` (section 2.2 + 9.3). Three `TestRef`s linked to three criteria, plus a fourth
criterion with no test at all.

Assertions:

1. 4 cells created with the 4 distinct coordinate tuples; all reach terminal; 2 `passed`,
   2 `failed` exactly as scripted.
2. Every cell run carries `trigger_type == "experiment"` and `trigger_ref == cell.id`
   (`test_cell_run_carries_trigger_ref`, replacing PLAN's `pipeline_run_id.experiment_id`).
3. Leaderboard has **4 variant rows**.
4. **Per-variant aggregation is real:** criterion c1 = 1.0 for all four variants; criterion c2
   = 1.0 for the two mock-pass variants and 0.0 for the two mock-fail variants.
5. c3's skips are excluded from every denominator; the fourth, untested criterion reports
   `pass_rate: null` with a reason - not `0.0`.
6. `cost_coverage == 1.0` and `cost_usd_total == "0.000000"` on every variant (the mock
   reports a genuinely known zero at `cost_source="cli-reported"`), proving the usage join
   runs end to end.
7. `GET /api/pipeline-runs/{cell.pipeline_run_id}/usage` shows **different**
   `input_tokens` for the two prompt variants - the prompt actually reached the container.
8. `ranked` is `false` and the note string is present.
9. The persisted step config of each cell's ephemeral pipeline carries the cell's `model`, and
   it equals `experiment_runs.model` (contract #5, the R3 pin across the dispatch boundary).

### 9.3 The one runner-common addition

`MockExecutor` gains an optional `mock_config["test_results"]`:

```jsonc
"test_results": [
  {"lazyaf_test_id": "exp.c1", "status": "passed",  "duration_ms": 4},
  {"lazyaf_test_id": "exp.c2", "status": "failed",  "duration_ms": 7},
  {"lazyaf_test_id": "exp.c3", "status": "skipped"}
]
```

When present AND `LAZYAF_TEST_RESULTS_PATH` is set, the mock writes
`{"version": 1, "results": [...]}` atomically to that path (temp file + `os.replace` in the
destination directory, the same discipline as `pytest_lazyaf`). Absent env var -> no-op.
Nothing here may fail the step: every exception is caught and logged, mirroring
`pytest_lazyaf`'s rule. ~25 lines plus its unit test. This is what lets the exit gate exercise
the whole tie-back -> aggregation path deterministically in the quick tier, without a second
container per cell.

### 9.4 Playwright (R8): `frontend/e2e/experiments.spec.ts`

Against the compose e2e stack with `LAZYAF_TEST_MODE=true`, `resetBackend` in `beforeEach`,
same idiom as `spec-layer.spec.ts`:

1. `/#/experiments` renders empty state; nav item is reachable.
2. Build a 2x2 matrix in the form; **Launch stays disabled until the dry run is fetched**;
   the dry-run panel shows cells/runs/estimate/basis and any warning verbatim.
3. Launch a mock 2x2, watch cells go live over WS, reach terminal.
4. Leaderboard renders 4 rows, shows an `N/A` cell for the untested criterion, and renders
   the `leaderboard-not-ranked-note` verbatim.
5. Abort on a running experiment moves pending cells to cancelled.

### 9.5 Ratchet (R4/R7)

- `tdd/tier_floors.json`: T1 and T3 floors are **re-measured, never guessed** (the file's own
  note is explicit about this). Agent A owns the file; Agent B reports its measured frontend
  counts to Agent A before the last commit (contract #7).
- **No new entries in `tdd/skip_baseline.json`.** Nothing in this phase needs a conditional
  skip; if an implementer thinks it does, that is a design bug - report it instead of
  baselining it.
- The dogfood pipeline is unchanged this phase: the exit-gate e2e runs in T3, which the
  dogfood run already executes, so R7 coverage arrives without editing
  `.lazyaf/pipelines/test-suite.yaml`.

---

## 10. FILE OWNERSHIP - two implementers, disjoint

### Agent A - backend engine (models, migration, service, metrics, API)

**Owns exclusively:**
- `backend/app/models/experiment.py` (new)
- `backend/app/schemas/experiment.py` (new)
- `backend/app/services/experiment_service.py` (new)
- `backend/app/services/experiment_metrics.py` (new)
- `backend/app/routers/experiments.py` (new)
- `backend/alembic/versions/0008_experiments.py` (new)
- `backend/app/models/testref.py` (the two columns + one index of 1.4, nothing else)
- `backend/app/services/test_ingestion.py` (the 3.3 stamp, nothing else)
- `runner-common/runner_common/executors/mock.py` (9.3) + `runner-common/tests/test_mock_test_results.py`
- every test file in 9.1 except the frontend store test, plus `tdd/e2e/test_experiment_matrix.py`
- `tdd/integration/test_migrations.py` (head constant + `EXPECTED_TABLES`) - **shared file,
  see contract #9**
- `tdd/tier_floors.json`

**Must not touch:** `pipeline_executor.py`, `agent_run.py`, `routers/spec.py`,
`models/spec.py`, `schemas/testref.py`, `main.py`, `models/__init__.py`, anything under
`frontend/`, `backend/app/mcp/`.

### Agent B - UI + MCP

**Owns exclusively:**
- `frontend/src/lib/pages/ExperimentsPage.svelte` (new)
- `frontend/src/lib/components/experiments/*.svelte` (new: `ExperimentList`, `MatrixBuilder`,
  `DryRunPanel`, `CellGrid`, `LeaderboardTable`)
- `frontend/src/lib/stores/experiments.ts` + `experiments.test.ts` (new)
- `frontend/src/lib/stores/websocket.ts` (the two new frame types ONLY)
- `frontend/src/lib/api/client.ts` + `types.ts` (the experiment surface ONLY)
- `frontend/e2e/experiments.spec.ts` (new)
- `backend/app/mcp/server.py` (the four tools of section 7 ONLY)

**Must not touch:** anything under `backend/app/{models,services,routers,schemas}`,
`App.svelte` (integrator), any Playwright spec other than its own.

---

## 11. CROSS-AGENT CONTRACTS (pin these first; they are the only shared surfaces)

1. **WS frames.** Names and payload keys exactly as section 6. Agent A broadcasts them via
   `manager.broadcast("experiment_status", ...)` / `"experiment_cell_status"`; Agent B adds
   both to `ServerMessageType`, `HANDLED_MESSAGE_TYPES` and the `handleServerMessage` switch.
   `websocket.test.ts` fails in BOTH directions (0.11), so these land in the same wave or the
   suite is red.
2. **Wire shapes.** `backend/app/schemas/experiment.py` is the source of truth; `types.ts`
   mirrors it field-for-field. Agent B's `client.test.ts` asserts against a fixture captured
   from the real API, not a hand-written object.
3. **`data-testid` vocabulary** exactly as section 8. Agent B owns both sides of it (component
   + spec), so it is a contract with the design rather than between agents - it is listed here
   because the reviewer checks it.
4. **Stamping direction.** Coordinates on `TestRun` come from the cell row, server-side.
   `test_ingest_ignores_wire_supplied_model` is the negative pin. Agent A owns both.
5. **Dispatch pin (R3).** `experiment_runs.model` == the `model` in the persisted step config
   of that cell's ephemeral pipeline. One shared test asserts both sides in one process
   (9.2 assertion 9).
6. **`ranked: false` and its note string** are part of the API contract, asserted by both
   `test_experiment_metrics.py` (backend) and the Playwright spec (verbatim render).
7. **Tier floors.** Agent B reports measured frontend/unit counts to Agent A; Agent A
   re-measures T1/T3 and updates `tdd/tier_floors.json` last, with a note naming what grew.
8. **The mock `test_results` key** (9.3) is Agent A's; Agent B never touches `mock_config`.
9. **`tdd/integration/test_migrations.py`** is edited by Agent A for two things only:
   `ALEMBIC_HEAD_REVISION = "0008"` and the three new table names in `EXPECTED_TABLES`
   (grouped under a `# 0008 (Phase 12.6.5 experiments)` comment). If Lane C also lands a
   migration this wave, the integrator merges these two edits - they are non-overlapping
   lines by construction.

---

## 12. REGISTRATION LINES FOR THE INTEGRATOR

### 12.1 `backend/app/main.py`

```python
# line 16, extend the existing routers import:
from app.routers import git, playground, models, steps, spec, test_results, experiments

# after line 149 (app.include_router(test_results.router)):
app.include_router(experiments.router)
```

### 12.2 `backend/app/models/__init__.py`

```python
# with the other model imports:
from app.models.experiment import (
    Experiment,
    ExperimentRun,
    ExperimentStatus,
    ExperimentRunStatus,
    PromptVersion,
)

# and in __all__:
    "Experiment",
    "ExperimentRun",
    "ExperimentStatus",
    "ExperimentRunStatus",
    "PromptVersion",
```

### 12.3 `backend/app/main.py` lifespan - OPTIONAL

```python
# inside the existing startup lifespan, after init_db():
from app.services.experiment_service import resume_stalled_experiments
await resume_stalled_experiments()   # logs and re-pumps experiments left running by a restart
```

Optional because `POST /api/experiments/{id}/resume` is the guaranteed path and the stalled
state is always REPORTED (section 2.6). Add it if you want restarts to self-heal.

### 12.4 `backend/app/services/agent_run.py` - the one hook (4 lines + 1 import)

```python
# with the other trigger constants (~line 118):
TRIGGER_EXPERIMENT = "experiment"
ADHOC_TRIGGER_TYPES = (TRIGGER_CARD_WORK, TRIGGER_PLAYGROUND, TRIGGER_EXPERIMENT)

# inside on_run_complete's try block, before the playground fallthrough:
        if trigger_type == TRIGGER_CARD_WORK:
            await _complete_card_work(db, pipeline_run, success)
        elif trigger_type == TRIGGER_EXPERIMENT:
            from app.services.experiment_service import on_cell_complete
            await on_cell_complete(db, pipeline_run, success)
        else:
            await _complete_playground(db, pipeline_run, success)
```

The import is local for the same reason `pipeline_executor`'s is: no import-time dependency
between the modules. `on_cell_complete` never raises, so the existing log-and-swallow wrapper
stays a backstop rather than a load-bearing part.

### 12.5 `frontend/src/App.svelte`

```js
// with the other page imports:
  import ExperimentsPage from './lib/pages/ExperimentsPage.svelte';

// in `routes`:
    '/experiments': ExperimentsPage,
```

```svelte
<!-- in the nav, after the Specs item: -->
      <a href="/experiments" use:link class="nav-item" data-testid="nav-experiments" class:active={$location === '/experiments'}>
        <span class="nav-icon">🔬</span>
        <span class="nav-label">Experiments</span>
      </a>
```

---

## 13. RISK REGISTER

| Risk | Mitigation |
|---|---|
| Migration number collision with 12.6.6 | 0.1; only four lines change if the integrator renumbers |
| Fan-out cost (the phase's headline risk) | required cap + dispatch-time enforcement + recorded overrun + `cost_coverage` on every cell (section 5) |
| Push triggers amplifying a matrix (0.8) | `push_branches` defaults false; when true the dry run names every pipeline that would fire (2.4) |
| Synchronous run completion re-entering the pump | `trigger_ref` link written before `start_pipeline` + the re-entrancy guard (0.4, 2.6) |
| Backend restart stalls a matrix | reported as `stalled`, `POST /resume`, optional lifespan sweep (2.6) |
| A template edited mid-experiment splitting a variant | all prompt versions frozen before any dispatch (1.3) |
| Leaderboard read as a ranking it has not earned | `ranked: false` + verbatim note + `insufficient_repeats` + `error_rate` warnings (4.5) |
| 4 containers in the quick tier slowing T3 | repeat=1, concurrency 2, mock agents at `delay_ms: 50`; if T3 wall-clock regresses more than ~90s, move the matrix e2e behind `@slow` and say so in `tier_floors.json` rather than trimming assertions |
| `SUM(NUMERIC)` float drift | dollars summed in Python over `Decimal`, never in SQL (4.3) |

## 14. Seams left open on purpose

- **Retries.** A cell that errors is not re-run. `cell_index` is unique per experiment, so a
  retry lane would add an `attempt` column; nothing here forecloses it.
- **`target_type="feature"`**, cross-repo cells, and multi-step strategy shapes: 13.2's
  orchestrator, not this phase.
- **`role` on the cell.** `StepUsage.role` is on the frozen wire and NULL everywhere; a
  single-agent-step cell has one role. `cost_by_role` becomes computable the moment 13.2
  writes roles, with no schema change here.
- **Criterion-history sparklines** (deferred into this phase by the roadmap) ride
  `GET /api/criteria/{id}/history`, which already returns `model` / `prompt_template_id` and
  which starts returning real values the moment 3.3 lands. The sparkline is a Feature-page
  component; it is listed in section 8's leaderboard panel and is the first thing to cut if
  Agent B runs out of wave.
