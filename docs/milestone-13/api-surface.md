### Milestone 13 — API Surface & Data-Model Detail

**Goal**: pin the complete REST / MCP / CLI surface for the benchmark harness, plus
the data-model detail the "Specification Layer Models" sketches leave open, so
13.1-13.5 are implementable without re-litigating shapes. Every write path names
its auth, every read path says whether it is read-heavy and what index carries it,
and every joint with the existing spec layer (12.2.5 / 12.2.6 / 12.5 / 12.6.5)
is stated rather than assumed.

> **Why now:** three of these contracts cannot wait for Milestone 13.
> `POST /api/steps/{id}/usage` (section 2) MUST ship inside **12.5** with the
> agent-step migration, because the control-layer protocol freezes there and
> 12.2.6 already taught us what a retrofit costs. `StepUsage.role` (section 2.6)
> must exist on the same migration or `cost_by_role` -- the number that tests the
> whole "expensive planner, cheap workers" hypothesis -- is unrecoverable after
> the fact. And `TestRun.origin` (section 3) must land before the first dogfood
> trial runs, or synthetic trial results silently flip real acceptance criteria.

---

#### 0. Conventions

| Convention | Rule |
|---|---|
| Router | `backend/app/routers/benchmark.py`, `APIRouter(prefix="/api/bench", tags=["benchmark"])`. Usage ingestion stays in `routers/steps.py` (it shares the step auth machinery, exactly as `/test-results` does). |
| IDs | String UUIDs, as everywhere else in the tree. |
| Pagination | `limit` (default 100, `ge=1, le=1000`) + `offset` (`ge=0`) on every list; ordering is always explicit and deterministic (a natural key, id as tiebreak). |
| 404 vs empty | An unknown parent id is a 404, never an empty list (spec.py / test_results.py idiom: "no rows" and "no such thing" are different facts). |
| Vocabulary errors | An out-of-vocabulary enum value is a 400 whose detail NAMES the valid set (`list_test_refs` idiom). |
| Money | `Decimal` in the DB (`NUMERIC(12,6)`), **string** on the wire. No floats for dollars, ever. |
| Time | `duration_ms` / `wall_clock_ms` are ints; timestamps are ISO-8601 UTC. |
| Read-heavy | Endpoints tagged **[read-heavy]** are called by the board / UI in a loop and MUST be served by an index listed in section 6. |
| Mutation of measured data | Anything a `Trial` has already referenced is frozen (409 + "fork it"), see 1.3. Provenance is worthless if the referent can change under it. |

---

#### 1. Corpus, strategies, trials (Phases 13.1-13.2)

##### 1.1 Benchmark suites

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/bench/suites` | Create a suite | 409 on duplicate `name` |
| `GET /api/bench/suites` | List suites | **[read-heavy]** `?tag=`, `?limit/offset` |
| `GET /api/bench/suites/{suite_id}` | One suite + case count + version + content hash | |
| `PATCH /api/bench/suites/{suite_id}` | Rename / retag / edit description | metadata only; case edits go through 1.2 |
| `DELETE /api/bench/suites/{suite_id}` | Delete | 409 if any `Trial` references a case in it |
| `POST /api/bench/suites/{suite_id}/validate` | Validate every case (13.1 exit gate) | async; returns a validation-run id |
| `GET /api/bench/suites/{suite_id}/validation` | Latest suite-wide validation summary | **[read-heavy]** |

```json
// POST /api/bench/suites
{
  "name": "core-v1",
  "description": "Starter corpus: 3 verticals x 3 complexities",
  "tags": ["web-api", "data-pipeline", "cli"]
}
// 201
{
  "id": "sui_9f2c...",
  "name": "core-v1",
  "description": "Starter corpus: 3 verticals x 3 complexities",
  "tags": ["web-api", "data-pipeline", "cli"],
  "version": 1,
  "content_hash": "sha256:0000...",
  "case_count": 0,
  "created_at": "2026-08-29T14:02:11Z",
  "updated_at": "2026-08-29T14:02:11Z"
}
```

**Data-model detail the sketch omits — suite versioning.** `Trial.suite_version` is
in the model sketch with nothing that produces it. Definition:

```python
class BenchmarkSuite:
    # ... as sketched ...
    version: int            # monotonic; bumped by ANY case insert/update/delete
    content_hash: str       # "sha256:<hex>" over canonical JSON of the suite's
                            # case rows, sorted by slug, with volatile fields
                            # (id, created_at, updated_at) excluded
```

`content_hash` is what the bundle (13.5) compares, because `version` is a local
counter and two machines will disagree about it. `version` is what a human reads
in the board footer. Both are stamped on every `Trial`; the board refuses to pool
trials whose `suite_content_hash` differs and says so (section 4.3).

##### 1.2 Benchmark cases

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/bench/suites/{suite_id}/cases` | Author a case | 400 on unknown `repo_id`; 422 on bad enum |
| `GET /api/bench/suites/{suite_id}/cases` | List cases in suite | **[read-heavy]** filters: `vertical`, `complexity`, `contamination_risk` |
| `GET /api/bench/cases/{case_id}` | One case, with resolved oracle refs | **[read-heavy]** |
| `PATCH /api/bench/cases/{case_id}` | Edit | 409 once a `Trial` references it (fork the suite instead) |
| `DELETE /api/bench/cases/{case_id}` | Delete | 409 if referenced by a `Trial` |
| `POST /api/bench/cases/{case_id}/validate` | Base-state control (13.1) | async; returns validation id |
| `GET /api/bench/cases/{case_id}/validation` | Latest validation record | **[read-heavy]** |

```json
// POST /api/bench/suites/{suite_id}/cases
{
  "slug": "flask-api.missing-pagination",
  "repo_id": "repo_31ab...",
  "base_commit_sha": "c0ffee1234567890abcdef1234567890abcdef12",
  "task_statement": "GET /items returns every row. Add limit/offset pagination.",
  "vertical": "web-api",
  "complexity": "small",
  "fail_to_pass": ["items.list.paginates", "items.list.rejects_negative_limit"],
  "pass_to_pass": ["items.list.returns_json", "auth.requires_token"],
  "user_story_id": "sty_77de...",
  "loop_defaults": {"max_iterations": 6, "budget_usd": "5.00", "per_step_timeout": 900},
  "contamination_risk": "high",
  "source_url": "https://github.com/example/flask-api",
  "license": "BSD-3-Clause"
}
```

```json
// 201 — oracle ids are RESOLVED against the TestRef registry at write time
{
  "id": "cas_44b1...",
  "suite_id": "sui_9f2c...",
  "slug": "flask-api.missing-pagination",
  "...": "...",
  "oracle": {
    "fail_to_pass": [
      {"lazyaf_test_id": "items.list.paginates",
       "test_ref_id": "trf_01...", "criterion_id": "crt_9a...", "known": true},
      {"lazyaf_test_id": "items.list.rejects_negative_limit",
       "test_ref_id": null, "criterion_id": null, "known": false}
    ],
    "pass_to_pass": ["..."]
  },
  "validation_status": "unvalidated",
  "created_at": "2026-08-29T14:07:02Z"
}
```

An oracle id with `known: false` is **not** an error at author time -- the test may
not exist yet at `base_commit_sha`, which for `fail_to_pass` is the normal case
(the fix commit adds it). It IS an error at validation time if the id never
appears in a run (see 1.2.1). Resolution follows pinned contract #1: a TestRef is
identified by the PAIR `(repo_id, lazyaf_test_id)`, so a case resolves only
against its own fixture repo.

##### 1.2.1 Case validation (the base-state control)

`POST /api/bench/cases/{case_id}/validate` populates the fixture workspace at
`base_commit_sha`, runs the repo's test suite once via a normal pipeline run
(so the manifest ingestion path is the SAME one trials use -- one source of
truth, R3), and asserts the control.

```json
// POST /api/bench/cases/{case_id}/validate
{"force": false}          // force=true re-validates a case already green
// 202
{"validation_id": "val_a1...", "case_id": "cas_44b1...", "status": "running",
 "pipeline_run_id": "run_88..."}
```

```json
// GET /api/bench/cases/{case_id}/validation
{
  "validation_id": "val_a1...",
  "case_id": "cas_44b1...",
  "status": "invalid",
  "validated_at": "2026-08-29T14:19:40Z",
  "base_commit_sha": "c0ffee12...",
  "pipeline_run_id": "run_88...",
  "fail_to_pass": [
    {"lazyaf_test_id": "items.list.paginates", "observed": "failed", "ok": true},
    {"lazyaf_test_id": "items.list.rejects_negative_limit", "observed": "passed", "ok": false}
  ],
  "pass_to_pass": [
    {"lazyaf_test_id": "items.list.returns_json", "observed": "passed", "ok": true},
    {"lazyaf_test_id": "auth.requires_token", "observed": "missing", "ok": false}
  ],
  "problems": [
    "fail_to_pass 'items.list.rejects_negative_limit' already PASSES at base commit -- the oracle is already green",
    "pass_to_pass 'auth.requires_token' did not run at base commit (id not in manifest)"
  ],
  "image_hashes": {"lazyaf-base": "1f9bff1a6d1e", "lazyaf-python": "77c1a2b0de41"},
  "harness_version": "v0.13.0-4-g69f3ef0"
}
```

Vocabulary: `status` is `pending | running | valid | invalid | error`.
`observed` is `passed | failed | skipped | error | missing`. A `missing`
`pass_to_pass` id is INVALID, not a warning: a regression guard that never runs
guards nothing. `skipped` is invalid for both lists (12.2.6 open question 2 says
skipped is informational for criteria; for an ORACLE it is a hole).

**Data-model detail — the validation record.** New table, not implied by the
sketch:

```python
class BenchmarkCaseValidation:
    id: UUID
    benchmark_case_id: UUID
    pipeline_run_id: UUID | None
    status: str                 # pending|running|valid|invalid|error
    observed: dict              # {lazyaf_test_id -> "passed"|"failed"|...}
    problems: list[str]
    base_commit_sha: str
    harness_version: str
    image_hashes: dict
    created_at: datetime
    completed_at: datetime | None
```

`BenchmarkCase.validation_status` is a denormalized mirror of the latest row's
`status` so list views do not N+1. The orchestrator (13.2) REFUSES to launch a
trial on a case whose `validation_status != "valid"` -- 409, detail naming the
problems. This is the base-state control being enforced by construction rather
than by discipline.

##### 1.3 Strategy templates (the independent variable)

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/bench/strategies` | Create a template | body validated as a v2 graph, see below |
| `GET /api/bench/strategies` | List | **[read-heavy]** `?slug=`, `?include_forks=` |
| `GET /api/bench/strategies/{id}` | One template + `content_hash` + `referenced_by_trials` | |
| `PATCH /api/bench/strategies/{id}` | Edit | **409 once referenced by any Trial** |
| `POST /api/bench/strategies/{id}/fork` | Copy to a new slug/version | the sanctioned way to edit a measured template |
| `DELETE /api/bench/strategies/{id}` | Delete | 409 if referenced |
| `POST /api/bench/strategies/validate` | Dry-run graph validation, no row written | used by the CLI before import |

```json
// POST /api/bench/strategies
{
  "slug": "planner-fanout-8",
  "description": "Opus plans, 8 Haiku workers execute on their own branches, Sonnet integrates.",
  "graph": {
    "version": 2,
    "entry_points": ["plan"],
    "steps": [
      {"id": "plan",      "type": "agent",  "role": "planner",
       "prompt_purpose": "split-into-instructions"},
      {"id": "work",      "type": "agent",  "role": "worker", "fanout": 8,
       "fanout_source": "plan.instructions", "needs": ["plan"],
       "workspace": "branch-per-worker"},
      {"id": "integrate", "type": "agent",  "role": "integrator",
       "needs": ["work"], "join": "all"},
      {"id": "oracle",    "type": "script", "needs": ["integrate"],
       "command": "pytest -p lazyaf --lazyaf-results $LAZYAF_TEST_RESULTS_PATH"}
    ]
  },
  "roles": ["planner", "worker", "integrator"],
  "loop_policy": {"max_iterations": 6, "budget_usd": "5.00",
                  "stop_on": ["solved", "budget_exhausted", "no_diff"]},
  "parallelism": {"max_concurrent_workers": 4, "branch_per_worker": true},
  "integration": {"policy": "sequential-merge", "on_conflict": "resolver-agent",
                  "resolver_role": "integrator"}
}
```

Validation rules the endpoint enforces (each has a named unit test, section 7):

| Rule | Failure mode it prevents |
|---|---|
| Graph is a DAG; every `needs` target exists; `entry_points` are non-empty and have no `needs` | iteration is driven by the orchestrator, NOT by a cycle -- a cyclic graph would deadlock the v2 traversal |
| Every step `role` appears in `roles`, and every declared role is used by >= 1 step | a role nobody binds is a silent unpriced model |
| `fanout > 1` requires `parallelism.branch_per_worker == true` | K workers sharing one checkout is the bug the git-native design exists to avoid |
| A step with `fanout` must have exactly one downstream `join` step | orphaned worker branches are never integrated and their cost is unattributed |
| `integration.policy` in `{sequential-merge, rebase-onto-trunk, cherry-pick, agent-composed}` | integration policy is a measured axis; typos must not silently become defaults |
| `on_conflict` in `{fail, resolver-agent, human}`; `resolver-agent` requires a `resolver_role` in `roles` | a resolver with no model binding cannot run |
| `on_conflict == "human"` is rejected for templates used inside an Experiment matrix | an unattended matrix must never block on a human |
| Exactly one terminal oracle step | scoring must read one manifest, not race two |
| `loop_policy.budget_usd` present and > 0 | fairness across shapes requires a cap (an unbounded strategy always "wins") |

**Data-model detail — template identity and freezing.**

```python
class StrategyTemplate:
    # ... as sketched ...
    version: int                # 1 for a new slug; fork bumps
    forked_from_id: UUID | None
    content_hash: str           # sha256 over canonical(graph, roles, loop_policy,
                                # parallelism, integration) -- the provenance key
    is_frozen: bool             # set True on first Trial reference; PATCH -> 409
```

`Trial` gains `strategy_content_hash: str` alongside `strategy_template_id`, for
the same reason the suite has one: the bundle must be able to prove that two
trials on different machines ran the same graph.

##### 1.4 Trials

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/bench/trials` | Launch one trial (or dry-run it) | 202; async per R5 |
| `GET /api/bench/trials` | List / filter | **[read-heavy]** the board's raw feed |
| `GET /api/bench/trials/{id}` | Full trial incl. provenance + cost_by_role | **[read-heavy]** |
| `POST /api/bench/trials/{id}/cancel` | Cancel | idempotent; terminal -> 409 |
| `GET /api/bench/trials/{id}/iterations` | Iteration series (the cost curve) | **[read-heavy]** |
| `GET /api/bench/trials/{id}/iterations/{index}` | One iteration + its pipeline run id | |
| `GET /api/bench/trials/{id}/workers` | Per-worker branches, diffs, merge outcome | parallel strategies only |
| `GET /api/bench/trials/{id}/usage` | StepUsage rollup, grouped by role | **[read-heavy]** |
| `GET /api/bench/trials/{id}/provenance` | The falsifiability block on its own | what the bundle embeds |

```json
// POST /api/bench/trials
{
  "benchmark_case_id": "cas_44b1...",
  "strategy_template_id": "str_7a20...",
  "model_assignment": {"planner": "claude-opus-5",
                       "worker": "claude-haiku-4-5",
                       "integrator": "claude-sonnet-5"},
  "prompt_template_id": "pmt_5c...",
  "prompt_version": 3,
  "loop_policy_override": {"max_iterations": 4, "budget_usd": "2.50"},
  "variant": "normal",
  "determinism": {"temperature": 0.0, "seed": 42, "top_p": null},
  "experiment_id": null,
  "dry_run": false
}
```

```json
// 202
{
  "trial_id": "trl_c8e1...",
  "status": "running",
  "branch": "bench/trl_c8e1/base",
  "base_commit_sha": "c0ffee12...",
  "effective_loop_policy": {"max_iterations": 4, "budget_usd": "2.50",
                            "stop_on": ["solved", "budget_exhausted", "no_diff"]},
  "estimated_max_cost_usd": "2.50",
  "provenance": {
    "harness_version": "v0.13.0-4-g69f3ef0",
    "image_hashes": {"lazyaf-base": "1f9bff1a6d1e", "lazyaf-claude": "b3d9e0f11a72"},
    "suite_version": 7,
    "suite_content_hash": "sha256:9ab3...",
    "strategy_content_hash": "sha256:1de4...",
    "model_version": null,
    "determinism": {"temperature": 0.0, "seed": 42, "top_p": null},
    "network_mode": "proxy-cached"
  }
}
```

`dry_run: true` returns the same body with `"status": "dry_run"`, no rows written,
and `estimated_max_cost_usd` computed from the policy cap x fan-out width. This is
how the CLI answers "what is this matrix going to cost me" before spending it.

**`variant` — the controls, as first-class data (13.4).** New field on `Trial`:

| `variant` | Meaning | Expected result |
|---|---|---|
| `normal` | A real strategy run | the measurement |
| `null-agent` | Runs the graph with a no-op agent that produces no diff | MUST be 0% solved |
| `base-state` | Runs only the oracle step at `base_commit_sha` | reproduces the case validation |

The board (4.3) reports these separately and marks the whole board
`trustworthy: false` if `null-agent` ever solved anything.

**Launch-time refusals** (409, detail naming the reason): case not `valid`;
strategy graph invalid; a role in the template with no entry in
`model_assignment`; `budget_usd` absent after override merge; `on_conflict:
human` inside an experiment.

##### 1.5 Trial iterations and workers

```json
// GET /api/bench/trials/{id}/iterations
{
  "trial_id": "trl_c8e1...",
  "iterations": [
    {
      "iteration_index": 0,
      "pipeline_run_id": "run_a1...",
      "commit_sha": "aa11bb22...",
      "lines_added": 84, "lines_removed": 12, "files_touched": 3,
      "fail_to_pass_passed": 1, "fail_to_pass_total": 2,
      "pass_to_pass_broken": 0,
      "criteria_verified": 0,
      "cost_usd": "0.7412", "input_tokens": 41022, "output_tokens": 5108,
      "duration_ms": 194301,
      "integration": {"conflicts": 2, "resolved": 2, "cost_usd": "0.1904",
                      "policy": "sequential-merge"}
    }
  ],
  "cumulative_cost_usd": "0.7412",
  "budget_usd": "2.50",
  "budget_remaining_usd": "1.7588"
}
```

**Data-model detail — per-worker records.** `Trial.integration_conflicts` /
`conflicts_resolved` / `integration_cost_usd` are aggregates with nothing under
them. The rows that produce them:

```python
class TrialWorker:
    id: UUID
    trial_iteration_id: UUID
    worker_index: int             # 0..K-1
    role: str                     # usually "worker"
    branch: str                   # "bench/trl_c8e1/i0/w3"
    workspace_id: UUID | None
    instruction: str | None       # the planner's slice for this worker
    head_commit_sha: str | None
    files_touched: list[str]      # answers the "did K workers duplicate work?"
                                  # open question -- overlap is a set intersection
    lines_added: int
    lines_removed: int
    cost_usd: Decimal
    status: str                   # running|done|failed|timeout
    merge_status: str             # clean|conflicted|resolved|abandoned
    conflict_paths: list[str]
    resolution_source: str | None # "resolver-agent"|"human"|None
    resolution_cost_usd: Decimal
```

`GET /api/bench/trials/{id}/workers?iteration=0` returns these. `files_touched`
is the direct measurement the roadmap's "how do K workers avoid duplicating each
other's work?" open question asks for -- worker overlap is
`len(intersection) / len(union)` over that column, computed by the board, not
assumed.

`TrialIteration` gains `worker_count: int` and `integration_policy: str` (the
policy actually used -- a template may fall back, and the board groups by it).

---

#### 2. The StepUsage ingestion contract (Phase 12.5, in full)

> **Why now:** this is the fourth channel of the control-layer protocol, alongside
> status / logs / test-results. It ships WITH 12.5's agent-step migration or it is
> a retrofit against a frozen protocol -- the exact mistake 12.2.6 documents.

##### 2.1 Endpoint

`POST /api/steps/{step_id}/usage` -- lives in `backend/app/routers/steps.py`, not
in the benchmark router, because it shares that module's auth and terminal-write
machinery verbatim.

| Aspect | Contract |
|---|---|
| Auth | `Authorization: Bearer <step token>` -- the SAME token as `/logs`, `/status`, `/heartbeat`, `/test-results`, validated by `verify_step_auth`. Missing header or non-Bearer format: 401. Token not matching this `step_id`: 401. |
| Terminal rejection | `_reject_terminal_writes(execution)` -- a StepExecution in `completed / failed / cancelled / timeout` answers **409**, same zombie-token hardening as every other write endpoint. |
| Idempotency | Keyed on `step_execution_id`. A re-POST UPDATES the existing `StepUsage` row (a retrying runtime must not double-bill). |
| Ordering vs `/status` | The runtime POSTs usage BEFORE its terminal `/status`. A usage POST arriving after terminal is a 409 and is dropped -- see 2.4 for why that is acceptable. |
| Derived server-side | `step_run_id`, `pipeline_run_id`, `trial_iteration_id`, `role` fallback, and `cost_usd` when `cost_source == "gpu-node"`. |

```python
@router.post("/{step_id}/usage", response_model=UsageIngestResponse)
async def ingest_step_usage(
    step_id: str,
    request: UsageManifest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> UsageIngestResponse:
    """
    Ingest one agent step's resource accounting (Phase 12.5).

    Called by the control runtime after the agent CLI exits, from
    LAZYAF_USAGE_PATH. Same Bearer step token as /logs; terminal
    StepExecutions answer 409. Idempotent per step_execution: a re-POST
    updates rather than duplicates. When cost_usd is absent and the step
    ran on a registered GPU node, the SERVER prices it (node rate x
    occupancy) and stamps cost_source="gpu-node".
    """
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)
    usage = await ingest_usage(db, execution, request)
    return UsageIngestResponse(
        usage_id=usage.id,
        cost_usd=str(usage.cost_usd) if usage.cost_usd is not None else None,
        cost_source=usage.cost_source,
    )
```

##### 2.2 Request schema (one source of truth, R3)

`backend/app/schemas/usage.py` -- written by the control runtime, validated here,
and imported by the runtime's own tests. Version-pinned with `Literal` exactly as
`TestResultsManifest` is, so an unknown version is a 422 and never a silent
partial parse.

```python
class UsageManifest(BaseModel):
    version: Literal[1]
    provider: Literal["anthropic", "google", "openai-compatible", "self-hosted"]
    model: str | None = None
    model_version: str | None = None      # provider's exact version string
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: Decimal | None = None       # CLI-reported dollars, if any
    cost_source: Literal["cli-reported", "gpu-node", "estimated", "unknown"]
    wall_clock_ms: int
    container_seconds: float | None = None
    gpu_node_id: str | None = None        # set on self-hosted nodes
    gpu_fraction: float | None = None     # 1.0 = exclusive
    determinism: dict = {}                # {temperature, seed, top_p} as exposed
    role: str | None = None               # M13: which strategy role this step was
    raw: dict | None = None               # the CLI's own usage blob, verbatim
```

```json
// POST /api/steps/stp_31fa.../usage
{
  "version": 1,
  "provider": "anthropic",
  "model": "claude-haiku-4-5",
  "model_version": "claude-haiku-4-5-20260210",
  "input_tokens": 18422,
  "output_tokens": 3110,
  "cache_read_tokens": 240110,
  "cache_write_tokens": 12004,
  "cost_usd": "0.1841",
  "cost_source": "cli-reported",
  "wall_clock_ms": 184220,
  "container_seconds": 191.4,
  "determinism": {"temperature": 0.0, "seed": null, "top_p": null},
  "role": "worker",
  "raw": {"total_cost_usd": 0.1841, "usage": {"input_tokens": 18422}}
}
// 200
{"usage_id": "usg_5b2c...", "cost_usd": "0.1841", "cost_source": "cli-reported"}
```

`raw` is capped at 8 KiB server-side and truncated with a marker rather than
rejected -- it exists so a disputed number can be re-derived later, not as a
second source of truth.

##### 2.3 How the control runtime obtains the numbers

The agent CLIs already report their own usage; the runtime scrapes it. Mechanism
mirrors the test-results channel: the agent wrapper writes a manifest to a known
path, and `run.py` ships it after the command exits.

| Env var | Value |
|---|---|
| `LAZYAF_USAGE_PATH` | `/workspace/.control/usage.json` |
| `LAZYAF_ROLE` | the strategy role this step is bound to (M13; empty outside trials) |
| `LAZYAF_GPU_NODE_ID` | set by the executor when the step lands on a self-hosted node |

| Agent | Scrape |
|---|---|
| Claude CLI | invoke with `--output-format json`; the final JSON object carries `total_cost_usd` and a `usage` block. Wrapper copies those into the manifest, `cost_source="cli-reported"`. |
| Gemini CLI | parse its usage summary block; token counts present, dollars often absent -> tokens recorded, `cost_usd=null`, `cost_source="unknown"` unless the node model applies. |
| Self-hosted / vLLM-style | no bill exists -> tokens from the OpenAI-compatible `usage` field, `cost_usd=null`, `container_seconds` measured by the runtime, `cost_source="gpu-node"`. |
| Script / docker steps | no agent, no manifest. The runtime still POSTs `{provider: "self-hosted", cost_source: "unknown", wall_clock_ms, container_seconds}` so wall-clock and container time are complete across the whole graph. |

##### 2.4 When the CLI reports nothing (the never-fail-the-step rule)

**A missing or unparseable usage report NEVER fails a step.** The step's exit code
is ground truth about the work; accounting is telemetry about the work, and
telemetry must not be able to fail work.

```
manifest missing / unparseable / version unknown
  -> WARN in step logs (visible, not silent)
  -> POST {version: 1, provider: <from step config>, cost_source: "unknown",
           cost_usd: null, tokens: null, wall_clock_ms, container_seconds}
  -> step exit code unchanged

POST itself fails (network, 5xx)
  -> retry twice with backoff (same retry helper as /logs)
  -> then give up, WARN, continue
  -> step exit code unchanged

POST returns 409 (step already terminal)
  -> drop, WARN, continue
```

A `StepUsage` row with `cost_source="unknown"` is not missing data -- it is the
recorded fact that the provider told us nothing. The board counts those rows and
reports `cost_coverage` per cell (section 4.3); a cell whose coverage is below
100% carries a warning rather than a quietly-too-cheap median. That is the whole
difference between "we could not price this" and "this was free".

##### 2.5 The gpu-node cost model (self-hosted)

Self-hosted nodes have no per-token bill, so dollars come from node rate x
occupancy. **The server prices it, not the runtime**, so the rate table lives in
one place and history can be re-priced when a rate is corrected.

```python
# backend/app/services/usage_pricing.py
def gpu_node_cost_usd(node_rate_usd_hour: Decimal,
                      container_seconds: float,
                      gpu_fraction: float = 1.0) -> Decimal:
    """Occupancy pricing: you rent the node, not the tokens.

    cost = rate_per_hour * (container_seconds / 3600) * gpu_fraction

    container_seconds is WALL time the container held the node, including
    model load and idle-in-step time, because that is what the node bills
    for. gpu_fraction < 1.0 only when the node is deliberately shared
    (MIG slice, multi-tenant vLLM); default 1.0 = exclusive.
    """
    return (node_rate_usd_hour
            * Decimal(str(container_seconds)) / Decimal(3600)
            * Decimal(str(gpu_fraction))).quantize(Decimal("0.000001"))
```

Rates are configuration, addressed by node id:

```json
// backend config: LAZYAF_GPU_NODE_RATES (json) or gpu_node_rates table
{
  "runpod-a100-80g": {"rate_usd_hour": "1.89", "currency": "USD",
                      "note": "on-demand list price 2026-08"},
  "local-4090":      {"rate_usd_hour": "0.00",
                      "note": "owned hardware; electricity not modelled"}
}
```

Server-side precedence when writing `StepUsage.cost_usd`:

1. `cost_usd` present in the manifest -> use it, `cost_source="cli-reported"`.
2. else `gpu_node_id` known AND a rate is configured -> compute per above,
   `cost_source="gpu-node"`.
3. else `cost_source="unknown"`, `cost_usd=null`.

There is deliberately no token-price table. Owner decision (2026-08-29): while the
CLIs report cost, a second pricing table is a second source of truth that will
drift. `cost_source="estimated"` stays in the vocabulary for a future price-table
backfill and is written by nothing today.

A `rate_usd_hour` of `"0.00"` on owned hardware is honest, not a bug: the write-up
states that self-hosted trials are priced at marginal cash cost and that the
comparison to API pricing is therefore favourable to self-hosting. Disclosed,
not hidden.

##### 2.6 StepUsage model additions

The sketch is missing exactly the fields the board needs:

```python
class StepUsage:
    # ... as sketched ...
    role: str | None            # M13: strategy role -- WITHOUT this, cost_by_role
                                # is unrecoverable and the planner/worker
                                # hypothesis is untestable
    model_version: str | None   # provenance: the exact version, not the family
    gpu_node_id: str | None
    determinism: dict           # as the provider exposed it
    raw: dict | None            # the CLI's own blob, <= 8 KiB
```

`role` resolution order at ingestion: manifest `role` -> `step_config.role` ->
`StepRun.step_config["experiment_context"]["role"]` -> `None`. A `StepUsage` with
`role=None` inside a trial is aggregated under the bucket `"unattributed"` and
counted in a board warning; it is never silently dropped from the trial total.

##### 2.7 Usage read endpoints

| Method + path | Purpose | Notes |
|---|---|---|
| `GET /api/steps/{step_id}/usage` | One step's usage | operator/UI |
| `GET /api/pipeline-runs/{run_id}/usage` | Rollup for a run, grouped by role | **[read-heavy]** |
| `GET /api/bench/trials/{id}/usage` | Rollup for a trial, by role and iteration | **[read-heavy]** |

```json
// GET /api/bench/trials/trl_c8e1.../usage
{
  "trial_id": "trl_c8e1...",
  "total_cost_usd": "2.1104",
  "cost_coverage": 0.94,
  "by_role": {
    "planner":    {"cost_usd": "0.4210", "input_tokens": 22110, "output_tokens": 4021, "steps": 4},
    "worker":     {"cost_usd": "1.4988", "input_tokens": 188410, "output_tokens": 30112, "steps": 32},
    "integrator": {"cost_usd": "0.1906", "input_tokens": 40220, "output_tokens": 2110, "steps": 4},
    "unattributed": {"cost_usd": "0.0000", "steps": 2}
  },
  "by_iteration": [{"iteration_index": 0, "cost_usd": "0.7412"}],
  "by_source": {"cli-reported": 36, "gpu-node": 0, "unknown": 2}
}
```

---

#### 3. How the benchmark layer joins the spec layer

The harness adds **no new result store**. Oracle outcomes are `TestRun` rows,
ingested through the exact path 12.2.6 already ships.

##### 3.1 The join, step by step

```
BenchmarkCase.fail_to_pass / pass_to_pass  (list[lazyaf_test_id])
   -> resolved against TestRef by the PAIR (repo_id, lazyaf_test_id)   [contract #1]
   -> TestRef.criterion_id                                             [12.2.5 link]

Trial iteration N
   -> a real pipeline run of the strategy graph
   -> terminal oracle step runs the repo's suite
   -> pytest-lazyaf writes LAZYAF_TEST_RESULTS_PATH
   -> POST /api/steps/{id}/test-results                                [12.2.6, unchanged]
   -> TestRun rows: commit_sha, branch, repo_id, step_run_id,
                    pipeline_run_id, model, prompt_*                   [already derived]
   -> NEW at ingestion: origin, trial_id, trial_iteration_id           [section 3.2]

Scoring (writes TrialIteration)      Criterion history (reads the same rows)
   SELECT TestRun                       GET /api/criteria/{id}/history
   WHERE trial_iteration_id = :it       joins TestRun -> TestRef on criterion_id
   JOIN TestRef ON test_ref_id          filtered by origin (section 3.3)
   WHERE lazyaf_test_id IN (case oracle)
```

One ingestion, two consumers. The trial's `fail_to_pass_passed`,
`pass_to_pass_broken` and `criteria_verified` are computed from those rows -- the
oracle result IS the criterion evidence, which is the whole point of the layered
oracle decision ("the oracle decides *solved*; the criteria explain *what* was
solved").

Scoring rules, stated so nobody re-derives them:

| Quantity | Definition |
|---|---|
| `fail_to_pass_passed` | count of the case's `fail_to_pass` ids whose newest TestRun in THIS iteration is `passed` |
| `pass_to_pass_broken` | count of `pass_to_pass` ids whose newest TestRun in this iteration is `failed` or `error`, **or `missing`** -- a guard test that stopped running counts as broken (an agent that deletes the test must not score as clean) |
| solved | `fail_to_pass_passed == fail_to_pass_total AND pass_to_pass_broken == 0` |
| `criteria_verified` | distinct `criterion_id`s for which EVERY linked active TestRef is green this iteration (partial credit, recorded, not headline) |
| regression rate (board) | trials with any `pass_to_pass_broken > 0` at their FINAL iteration / trials |

##### 3.2 The three columns that make it work

```python
class TestRun:
    # ... existing ...
    origin: str                    # "pipeline" | "trial" | "validation"
    trial_id: UUID | None          # FK, indexed
    trial_iteration_id: UUID | None # FK, indexed
```

Derived at ingestion by the same walk `ingest_manifest` already performs
(`StepExecution -> StepRun -> PipelineRun`): if the PipelineRun carries a
`trial_iteration_id`, `origin="trial"` and both FKs are stamped; if it carries a
`benchmark_case_validation_id`, `origin="validation"`; otherwise `origin="pipeline"`
(the default, and what every existing row backfills to).

`PipelineRun` therefore gains `trial_iteration_id: UUID | None` and
`benchmark_case_validation_id: UUID | None` -- the same nullable-FK pattern
`experiment_id` already uses.

##### 3.3 Avoiding double-counting in the dogfood corpus

The dogfood corpus makes LazyAF's own repo a fixture. A single 5-strategy x 3-repeat
matrix over 6 iterations executes the suite ~90 times, all against real TestRefs
carrying real `criterion_id`s. Without a guard, two things break:

1. **The story-done gate is unblocked by fiction.** `_story_done_blocked_by_required_criteria`
   decides on the criterion's newest run on the repo's default branch, and treats
   runs with **no branch recorded** as relevant. A trial's oracle run can plausibly
   produce exactly that shape -- and one synthetic green would mark a real story done.
2. **Criterion history becomes noise.** 90 synthetic runs bury the CI series a
   human reads to answer "is this criterion actually healthy?".

Three layers, cheapest first:

| Layer | Rule |
|---|---|
| Branch | Trials always work on `bench/{trial_id}/...`, never a default branch. The existing "relevant branch" rule already excludes them. |
| **Origin (the load-bearing one)** | `_story_done_blocked_by_required_criteria` and `GET /api/criteria/{id}/history` filter `origin == "pipeline"` by DEFAULT. Branch alone is not enough: a fixture repo's default branch can legitimately be a trial target, and 12.2.6's own fallback ("if no relevant run, the newest run overall decides") would otherwise reach straight into trial rows. |
| Explicit opt-in | `GET /api/criteria/{id}/history?origin=trial&trial_id=...` returns the synthetic series when that is what you want -- the science view, never the health view. |

Symmetrically, **the board never reads criterion history**. Aggregation selects on
`trial_iteration_id`, so a history filter can never change a published number, and
a scoring change can never move a done-gate. The two consumers share rows and
share nothing else.

`GET /api/criteria/{id}/history` gains `?origin=` (default `pipeline`; `all`
permitted) and `?trial_id=`. Both are additive: existing callers see exactly
today's rows, which is the correct backfill semantics for `origin` defaulting to
`"pipeline"`.

##### 3.4 Linking a case to a story

`BenchmarkCase.user_story_id` is validated at write time (400 on unknown story).
`GET /api/bench/cases/{id}?expand=story` returns the story, its criteria, and
which of the case's oracle ids map onto which criterion -- the human-readable
"what was solved" the layered-oracle decision promises. A case whose oracle ids
resolve to zero criteria is legal (a pure SWE-bench-shaped case) and reported as
`"criteria_coverage": 0.0` rather than refused.

---

#### 4. Experiments extended to strategy matrices, and the board (Phase 13.3-13.4)

##### 4.1 The matrix

`Experiment` (12.6.5) gains `target_type: "benchmark_suite"` and a matrix whose
axes are the M13 independent variables. `matrix` stays a `dict`, so no migration
beyond the target type.

```json
// POST /api/experiments
{
  "name": "fanout-vs-oneshot-K8",
  "description": "Does an expensive planner + 8 cheap workers beat one big model?",
  "target_type": "benchmark_suite",
  "target_id": "sui_9f2c...",
  "matrix": {
    "strategies": ["one-shot", "adversarial-3", "planner-fanout-8"],
    "model_assignments": [
      {"name": "opus-plans-haiku-works",
       "roles": {"planner": "claude-opus-5", "worker": "claude-haiku-4-5",
                 "integrator": "claude-sonnet-5", "reviewer": "claude-haiku-4-5",
                 "agent": "claude-opus-5"}},
      {"name": "all-sonnet",
       "roles": {"planner": "claude-sonnet-5", "worker": "claude-sonnet-5",
                 "integrator": "claude-sonnet-5", "reviewer": "claude-sonnet-5",
                 "agent": "claude-sonnet-5"}}
    ],
    "repeat": 5,
    "cases": null,
    "shared_budget_usd": "5.00",
    "wall_clock_ceiling_ms": 3600000,
    "controls": ["null-agent", "base-state"],
    "determinism": {"temperature": 0.0, "seed": null}
  },
  "dry_run": true
}
```

```json
// 200 with dry_run: true -- cost before commitment
{
  "cells": 3, "model_assignments": 2, "cases": 9, "repeat": 5,
  "trials": 270,
  "control_trials": 18,
  "estimated_max_cost_usd": "1440.00",
  "warnings": [
    "strategy 'one-shot' declares roles ['agent']; assignment 'opus-plans-haiku-works' also binds planner/worker/integrator (ignored for this strategy)",
    "2 of 9 cases are contamination_risk=high"
  ]
}
```

Validation: every strategy slug resolves and is frozen; every role each strategy
declares is bound by every model assignment (400 naming the gap -- an unbound role
is the failure mode that silently substitutes a default model and voids the
comparison); `shared_budget_usd` present (fairness across shapes requires a shared
cap); `repeat >= 1`. Controls named in `matrix.controls` are expanded into
`variant` trials, one per case, and are **not** optional at launch time for an
experiment intended for publication -- launching without them sets
`experiment.publishable = false`.

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/experiments` | Create / dry-run a matrix | existing route, new target_type |
| `POST /api/experiments/{id}/launch` | Fan out to trials | 202; existing route |
| `POST /api/experiments/{id}/abort` | Cancel pending, let running finish | existing semantics |
| `GET /api/experiments/{id}/trials` | Trial list with matrix coordinates | **[read-heavy]** |
| `GET /api/experiments/{id}/progress` | trials by status, spend vs cap, ETA | **[read-heavy]**, WS-mirrored |

##### 4.2 Budget enforcement across the fan-out

Stated here because it is an API-visible contract, not only an orchestrator detail:
budget is enforced **per trial across its whole fan-out**, not per worker. The
orchestrator checks cumulative `StepUsage` before scheduling each iteration and
before each fan-out wave; a wave that cannot fit inside the remaining budget is not
started. `GET /api/bench/trials/{id}` reports
`status: "budget_exhausted"` with `budget_usd` and `total_cost_usd`, and
`total_cost_usd <= budget_usd` is a contract test, not an aspiration.

Steps with `cost_source="unknown"` count as **zero** against the budget (nothing
else is defensible), which is precisely why `cost_coverage` is surfaced on every
board cell -- a cell at 60% coverage has an unenforced budget and must say so.

##### 4.3 The effectiveness board

`GET /api/bench/board` -- one endpoint, composable grouping. **[read-heavy]**, and
the reason section 6 exists.

| Query param | Meaning |
|---|---|
| `experiment_id` | scope (repeatable; pooling across experiments requires matching suite + harness hashes) |
| `suite_id` | scope by corpus instead |
| `group_by` | comma list: `strategy`, `model_assignment`, `role`, `case`, `vertical`, `complexity`, `contamination_risk`, `integration_policy`, `k` (fan-out width) |
| `budget_usd` | evaluate solve-rate at this shared cap (replays iterations while cumulative cost <= cap) |
| `contamination_risk` | filter, e.g. `low` -- the skeptic's view |
| `include_controls` | default true |
| `baseline` | strategy slug the comparisons are made against; default `one-shot` |
| `ci` | `bootstrap` (default) or `none`; `ci_level` default 0.95 |

```json
// GET /api/bench/board?experiment_id=exp_12&group_by=strategy&budget_usd=5.00
{
  "scope": {"experiment_ids": ["exp_12"], "suite_id": "sui_9f2c...",
            "suite_content_hash": "sha256:9ab3...", "harness_version": "v0.13.0-4-g69f3ef0",
            "budget_usd": "5.00", "baseline": "one-shot"},
  "trustworthy": true,
  "warnings": [],
  "controls": {
    "null_agent": {"trials": 9, "solved": 0, "solve_rate": 0.0, "ok": true},
    "base_state": {"cases": 9, "valid": 9, "ok": true}
  },
  "cells": [
    {
      "group": {"strategy": "planner-fanout-8"},
      "n": 90, "solved": 61, "solve_rate": 0.678,
      "solve_rate_ci": [0.575, 0.771],
      "cost_to_solve_usd": {"median": "1.94", "p25": "1.11", "p75": "3.02",
                            "min": "0.44", "max": "4.98"},
      "wall_clock_ms": {"median": 402110, "p25": 288400, "p75": 601200},
      "speedup": {"median": 3.4, "p25": 2.6, "p75": 4.1},
      "iterations_to_solve": {"histogram": {"1": 18, "2": 24, "3": 12, "4": 7},
                              "median": 2, "p90": 4},
      "regression_rate": 0.089,
      "integration": {"conflict_rate": 0.31, "resolved_rate": 0.94,
                      "cost_usd_median": "0.19", "cost_share": 0.098,
                      "worker_overlap_median": 0.12},
      "cost_by_role": {"planner": "0.41", "worker": "1.32", "integrator": "0.21"},
      "cost_coverage": 0.97
    }
  ],
  "comparisons": [
    {"a": "planner-fanout-8", "b": "one-shot", "metric": "cost_to_solve_usd",
     "delta_median": "-0.61", "separable": true,
     "method": "bootstrap 95% CI on the median difference, 10000 resamples",
     "note": "fan-out solves cheaper AND 3.4x faster"},
    {"a": "adversarial-3", "b": "one-shot", "metric": "cost_to_solve_usd",
     "delta_median": "-0.04", "separable": false,
     "reason": "95% CI [-0.31, +0.24] contains 0 -- NOT RANKED"}
  ]
}
```

Hard behaviours (13.4):

- `separable: false` cells are **never ranked**; the UI renders them tied, and the
  `reason` string is displayed verbatim. The board's job is to refuse to sell noise.
- `trustworthy: false` (with the reason in `warnings`) whenever: the null-agent
  control solved anything; any case in scope is not `valid`; scoped trials disagree
  on `suite_content_hash`, `harness_version`, or `image_hashes`; or pooled
  `cost_coverage < 0.9`.
- Any cell with `n < 3` reports point values and `"insufficient_repeats": true`
  instead of intervals.

Companion read endpoints, each **[read-heavy]**:

| Method + path | Purpose |
|---|---|
| `GET /api/bench/board/cases` | The same-case comparability matrix (rows = cases, cols = strategies) -- the 13.3 exit gate |
| `GET /api/bench/board/roles` | Cost-by-role split, the planner/worker hypothesis view |
| `GET /api/bench/board/curve` | Mean cost + solve-progress per iteration index: does iteration 4 earn its keep? |
| `GET /api/bench/board/k-sweep` | Solve-rate / cost / conflict-rate vs fan-out width K -- the roadmap's "first genuinely publishable result" |

##### 4.4 Export / import bundles (Phase 13.5)

| Method + path | Purpose | Notes |
|---|---|---|
| `POST /api/bench/suites/{id}/export` | Build a bundle | 202, async (git bundles are slow) |
| `GET /api/bench/exports/{export_id}` | Build status + manifest | |
| `GET /api/bench/exports/{export_id}/download` | The `.tar.zst` | streamed |
| `POST /api/bench/import` | Import a bundle (multipart) | idempotent by `content_hash` |
| `POST /api/bench/import/preflight` | Report what WOULD change, write nothing | |

```json
// POST /api/bench/suites/sui_9f2c.../export
{"with_results": true, "experiment_ids": ["exp_12"],
 "redistribute": "license-permitting", "include_logs": false}
// 202
{"export_id": "exp_bundle_a4...", "status": "building"}
```

Bundle layout and `manifest.json`:

```
core-v1-bundle/
  manifest.json
  METHOD.md                  # what was measured, controls, caveats, re-run command
  cases/<slug>/case.json
  cases/<slug>/repo.bundle          # licence permits redistribution
  cases/<slug>/FETCH.md + base.patch  # licence does not
  trials/<trial_id>.json            # trial + iterations + workers + provenance
  board/board.json                  # the rendered board at export time
```

```json
// manifest.json
{
  "bundle_schema_version": 1,
  "suite": {"name": "core-v1", "version": 7, "content_hash": "sha256:9ab3..."},
  "harness_version": "v0.13.0-4-g69f3ef0",
  "image_hashes": {"lazyaf-base": "1f9bff1a6d1e", "lazyaf-claude": "b3d9e0f11a72"},
  "exported_at": "2026-08-29T18:00:00Z",
  "cases": [{"slug": "flask-api.missing-pagination", "license": "BSD-3-Clause",
             "redistributed": true, "contamination_risk": "high",
             "source_url": "https://github.com/example/flask-api"}],
  "trials": 270, "controls_included": true,
  "rerun_command": "lazyaf bench import core-v1-bundle.tar.zst && lazyaf bench run --suite core-v1 --experiment fanout-vs-oneshot-K8"
}
```

Import behaviour: `POST /api/bench/import` reconstructs suite + cases + strategy
templates + trials read-only. If the importing tree's `harness_version` or
`image_hashes` differ from the manifest, the import SUCCEEDS but every imported
trial is flagged `provenance_mismatch: true`, and any board scoped over them
reports `trustworthy: false` with the specific mismatched hashes. Loud, not silent
-- the 13.5 exit gate.

---

#### 5. MCP tools and CLI

##### 5.1 MCP tools (`backend/app/mcp/server.py`, `@mcp.tool()`, thin httpx wrappers)

```python
@mcp.tool()
def bench_list_suites(tag: str = "") -> dict: ...

@mcp.tool()
def bench_list_cases(suite_id: str, vertical: str = "", complexity: str = "",
                     contamination_risk: str = "") -> dict: ...

@mcp.tool()
def bench_get_case(case_id: str) -> dict: ...

@mcp.tool()
def bench_validate_case(case_id: str, force: bool = False) -> dict:
    """Run the base-state control. Returns the validation record."""

@mcp.tool()
def bench_list_strategies(include_forks: bool = False) -> dict: ...

@mcp.tool()
def bench_create_strategy(slug: str, description: str, graph_json: str,
                          roles: list[str], loop_policy_json: str,
                          parallelism_json: str = "{}",
                          integration_json: str = "{}") -> dict:
    """Author a strategy as DATA. Validates the graph before writing."""

@mcp.tool()
def bench_launch_trial(benchmark_case_id: str, strategy_template_id: str,
                       model_assignment_json: str, variant: str = "normal",
                       max_iterations: int = 0, budget_usd: str = "",
                       dry_run: bool = False) -> dict: ...

@mcp.tool()
def bench_get_trial(trial_id: str, include_iterations: bool = True) -> dict: ...

@mcp.tool()
def bench_cancel_trial(trial_id: str) -> dict: ...

@mcp.tool()
def bench_launch_experiment(name: str, suite_id: str, matrix_json: str,
                            dry_run: bool = True) -> dict:
    """Strategy matrix over a suite. dry_run defaults TRUE -- a 270-trial
    fan-out is not something an agent should start by accident."""

@mcp.tool()
def bench_board(experiment_id: str = "", suite_id: str = "",
                group_by: str = "strategy", budget_usd: str = "",
                contamination_risk: str = "") -> dict: ...

@mcp.tool()
def bench_export(suite_id: str, with_results: bool = True) -> dict: ...
```

`bench_launch_experiment` defaulting to `dry_run=True` is deliberate: every other
launch tool in this file is cheap, and this one can spend four figures.

##### 5.2 CLI (`cli/lazyaf/cli.py`, click, `@cli.group()` per the `tests` group)

```
lazyaf bench suite create <name> [--description ...] [--tag t]...
lazyaf bench suite list
lazyaf bench suite show <suite>                     # name or id
lazyaf bench suite delete <suite>

lazyaf bench case add <suite> --repo <repo_id> --slug <slug>
        --base-commit <sha> --task <text|@file>
        --vertical <v> --complexity <c>
        --fail-to-pass <id>... --pass-to-pass <id>...
        [--story <story_id>] [--contamination-risk high|medium|low]
        [--license <spdx>] [--source-url <url>]
        [--max-iterations N] [--budget-usd X]
lazyaf bench case add-from-fix <suite> --repo <repo_id> --fix-commit <sha>
        # derives base_commit_sha=<fix>^ and infers fail_to_pass from the
        # tests that flip red->green across the fix commit. Prints the
        # inferred oracle and REQUIRES confirmation -- inference authoring a
        # measurement silently is exactly the miswired case 13.1 must catch.
lazyaf bench case list <suite> [--vertical v] [--contamination-risk low]
lazyaf bench case show <case>
lazyaf bench case validate <case> [--force]

lazyaf bench validate <suite> [--fail-fast]         # 13.1 EXIT GATE
        # exit 0 iff every case is valid; prints a per-case table and the
        # problems verbatim. Non-zero exit is what CI hangs off.

lazyaf bench strategy list
lazyaf bench strategy show <slug>
lazyaf bench strategy import <file.yaml|file.json>  # validate then POST
lazyaf bench strategy validate <file>               # no write, exit code only
lazyaf bench strategy fork <slug> --to <new-slug>

lazyaf bench trial run <case> --strategy <slug>
        --model planner=claude-opus-5 --model worker=claude-haiku-4-5
        [--budget-usd 2.50] [--max-iterations 4]
        [--variant normal|null-agent|base-state] [--seed 42]
        [--dry-run] [--watch]
lazyaf bench trial list [--case c] [--strategy s] [--experiment e] [--status s]
lazyaf bench trial show <trial> [--iterations] [--workers] [--usage]
lazyaf bench trial cancel <trial>

lazyaf bench experiment run <name> --suite <suite>
        --strategy one-shot --strategy planner-fanout-8
        --assignment @assignments.json --repeat 5
        --shared-budget-usd 5.00 --controls null-agent,base-state
        [--dry-run]                                 # dry-run prints trials + max spend
lazyaf bench experiment status <experiment>

lazyaf bench board [--experiment e] [--suite s] [--group-by strategy,vertical]
        [--budget-usd 5.00] [--contamination-risk low] [--json]
        # human output: a rich table; ties printed as "= (not separable)"
        # rather than ordered, and controls printed above the table.

lazyaf bench export <suite> [--with-results] [--experiment e]... --out <file>
lazyaf bench import <file> [--preflight]
```

Two CLI behaviours are contracts, not polish: `lazyaf bench validate` exits
non-zero on any invalid case (that is the 13.1 gate), and
`lazyaf bench experiment run` without `--dry-run` prints the trial count and
maximum spend and requires an interactive confirm (or `--yes`) before launching.

---

#### 6. Indexes for the read-heavy paths

The board is the only genuinely hot query in the system: it scans every trial and
iteration in scope and joins usage. These indexes land in the M13 migration.

| Table | Index | Serves |
|---|---|---|
| `benchmark_cases` | `(suite_id, slug)` UNIQUE | case lookup by human name; suite listing |
| `benchmark_cases` | `(suite_id, vertical, complexity)` | board `group_by` and case filters |
| `benchmark_cases` | `(contamination_risk)` | the skeptic's split |
| `benchmark_case_validations` | `(benchmark_case_id, created_at DESC)` | latest-validation lookup |
| `strategy_templates` | `(slug, version)` UNIQUE | strategy resolution by slug |
| `trials` | `(experiment_id, benchmark_case_id, strategy_template_id)` | the board's primary scan |
| `trials` | `(strategy_template_id, status)` | per-strategy aggregation |
| `trials` | `(benchmark_case_id, created_at DESC)` | per-case history |
| `trials` | `(variant)` partial WHERE `variant <> 'normal'` | control lookups (tiny, hot) |
| `trial_iterations` | `(trial_id, iteration_index)` UNIQUE | the cost curve; iteration fetch |
| `trial_workers` | `(trial_iteration_id, worker_index)` UNIQUE | worker fan-out reads |
| `trial_workers` | `(trial_iteration_id, merge_status)` | conflict-rate aggregation |
| `step_usage` | `(step_execution_id)` UNIQUE | idempotent ingestion |
| `step_usage` | `(trial_id, role)` | `cost_by_role` -- the hypothesis query |
| `step_usage` | `(pipeline_run_id)` | per-run rollup |
| `test_runs` | `(trial_iteration_id, test_ref_id)` | oracle scoring |
| `test_runs` | `(origin, created_at DESC)` | criterion history default filter |
| `test_runs` | existing `(test_ref_id, created_at DESC)` | unchanged, still serves history |

Board aggregations that cannot be served by an index in one pass (percentiles,
bootstrap CIs) are computed in the service layer over an indexed scan, and the
board response is cached per
`(scope, group_by, budget_usd, filters, max(trial.completed_at))` -- the cache key
includes the newest completion so a finishing trial invalidates it for free.

---

#### Tests

**Unit — schemas, validation, pricing, aggregation**

| File | Pins |
|---|---|
| `tdd/unit/benchmark/test_case_schema.py` | Case create rejects unknown vertical/complexity/contamination_risk; empty `fail_to_pass` is a 422; oracle ids resolve by `(repo_id, lazyaf_test_id)` and never cross repos |
| `tdd/unit/benchmark/test_suite_versioning.py` | Any case mutation bumps `version` and changes `content_hash`; hash is stable across row order and ignores `created_at`/`id` |
| `tdd/unit/benchmark/test_strategy_graph_validation.py` | Every rule in the 1.3 table, one test each: cycle rejected, unknown `needs` rejected, `fanout` without `branch_per_worker` rejected, fan-out without a join rejected, unbound role rejected, bad integration policy names the vocabulary, missing budget rejected, `on_conflict: human` rejected inside a matrix |
| `tdd/unit/benchmark/test_strategy_freeze.py` | PATCH on a trial-referenced template is 409; `fork` produces a new id, `version+1`, `forked_from_id` set, and a different `content_hash` |
| `tdd/unit/benchmark/test_usage_manifest_schema.py` | `version: 1` pinned by Literal (unknown version -> 422); Decimal cost survives round-trip as a string; unknown keys ignored; `raw` over 8 KiB truncated not rejected |
| `tdd/unit/benchmark/test_gpu_node_cost_model.py` | `gpu_node_cost_usd` formula and 6-dp quantization; `gpu_fraction` scaling; a zero rate yields exactly `0.000000`, never null |
| `tdd/unit/benchmark/test_usage_cost_precedence.py` | cli-reported beats gpu-node beats unknown; a manifest with `cost_usd` present is never re-priced |
| `tdd/unit/benchmark/test_trial_scoring.py` | `solved` requires all `fail_to_pass` green AND zero `pass_to_pass_broken`; a `pass_to_pass` id MISSING from the manifest counts as broken; `criteria_verified` needs every linked ref green |
| `tdd/unit/benchmark/test_board_aggregation.py` | Median/quartile cost-to-solve over trials; regression rate uses the FINAL iteration; `cost_by_role` sums `StepUsage` not trial totals; `role=None` lands in `unattributed` and is never dropped; cells with `n < 3` report `insufficient_repeats` |
| `tdd/unit/benchmark/test_board_separability.py` | Overlapping bootstrap intervals produce `separable: false` with a reason string and are NOT ranked; a clean separation is ranked |
| `tdd/unit/benchmark/test_board_trustworthiness.py` | A null-agent trial that solved anything flips `trustworthy: false`; mixed `suite_content_hash` in scope flips it; `cost_coverage < 0.9` flips it |
| `tdd/unit/benchmark/test_worker_overlap.py` | `files_touched` intersection/union over K workers is the reported overlap; disjoint workers score 0.0 |

**Integration — API surfaces**

| File | Pins |
|---|---|
| `tdd/integration/api/test_bench_suite_api.py` | Suite/case CRUD; unknown suite is 404 not empty; duplicate slug in a suite is 409; delete blocked by a referencing trial |
| `tdd/integration/api/test_bench_case_validation.py` | A case whose `fail_to_pass` already passes at base validates as `invalid` with a named problem; a missing `pass_to_pass` id is invalid; a well-formed case validates `valid`; launching a trial on an unvalidated case is 409 |
| `tdd/integration/api/test_bench_strategy_api.py` | Strategy CRUD + `POST /validate` dry-run writes nothing; fork flow |
| `tdd/integration/api/test_bench_trial_api.py` | Launch returns 202 with provenance populated; `dry_run` writes no rows; cancel is idempotent and 409s on terminal; iterations endpoint returns the cost curve in index order |
| `tdd/integration/api/test_bench_trial_budget.py` | A trial never exceeds `budget_usd` across a fan-out; termination status is `budget_exhausted`; a wave that cannot fit is not started |
| `tdd/integration/api/test_bench_experiment_matrix.py` | `strategies x model_assignments x cases x repeat` expands to the exact trial count; controls expand to one trial per case per control; an unbound role is a 400 naming the role; dry-run reports cells + max spend without writing |
| `tdd/integration/api/test_bench_board_query.py` | `group_by` combinations; `budget_usd` re-scoring changes solve-rate; `contamination_risk=low` filter; the same case is comparable across strategy shapes (13.3 gate); baseline is always present |
| `tdd/integration/api/test_step_usage_ingestion.py` | Bearer step token accepted; missing/mangled header 401; token for another step 401; terminal StepExecution 409; re-POST updates not duplicates; gpu-node pricing applied server-side when `cost_usd` absent; `role` resolution order |
| `tdd/integration/api/test_trial_criterion_isolation.py` | **The double-counting guard.** A trial's oracle TestRuns get `origin="trial"` + both trial FKs; `GET /api/criteria/{id}/history` excludes them by default and includes them under `?origin=trial`; a required criterion whose ONLY green run came from a trial still BLOCKS story done; the board's numbers are unchanged by any history filter |
| `tdd/integration/api/test_bundle_export_import.py` | Export -> import on a clean DB reconstructs suite + cases + strategies + trials; a licence-restricted case ships FETCH.md + patch instead of a bundle; importing under a different `harness_version` flags `provenance_mismatch` and the board goes `trustworthy: false` |

**Control-runtime and services**

| File | Pins |
|---|---|
| `tdd/unit/control_runtime/test_usage_pickup.py` | Runtime reads `LAZYAF_USAGE_PATH` and POSTs it; missing file -> POST with `cost_source="unknown"` and the step exit code UNCHANGED; unparseable file -> same, plus a warning line in logs; POST 5xx retried twice then abandoned without failing the step; 409 dropped quietly |
| `tdd/integration/services/test_trial_orchestrator_loop.py` | N sequential pipeline runs, previous failures fed forward; stops on `solved`; stops on `no_diff`; each iteration is a real, fetchable `PipelineRun` |
| `tdd/integration/services/test_trial_integration_policies.py` | `sequential-merge` vs `rebase-onto-trunk` vs `agent-composed`; a conflicting pair records `merge_status="conflicted"`, `on_conflict: resolver-agent` records `resolved` + a non-zero `resolution_cost_usd`; `on_conflict: fail` terminates the iteration cleanly |
| `tdd/integration/services/test_trial_branch_allocation.py` | Real seam (R6): K workers get K distinct branches and K distinct workspaces off `base_commit_sha`; no worker sees another's commits before integration |

**E2E, CLI, MCP**

| File | Pins |
|---|---|
| `tdd/e2e/test_bench_trial_e2e.py` | Mock-model trial on a starter case solves at a known iteration with a complete per-iteration cost curve (13.2 gate); a deliberately-unsolvable case ends `budget_exhausted` without overspending |
| `tdd/e2e/test_bench_null_agent_control.py` | The null-agent variant scores exactly 0% on every case in the starter suite |
| `tdd/unit/cli/test_bench_cli.py` | `lazyaf bench validate` exits non-zero on a miswired case and zero on a clean suite; `experiment run` without `--dry-run`/`--yes` refuses to launch; `board --json` output matches the API body |
| `tdd/integration/api/test_bench_mcp_tools.py` | Every `bench_*` tool round-trips against a live backend; `bench_launch_experiment` defaults to dry-run |

---

#### Definition of Done

- [ ] `POST /api/steps/{id}/usage` ships **inside Phase 12.5** with the agent-step migration: step-token auth, terminal-409, idempotent per step execution, `UsageManifest` as the single shared schema imported by both the runtime and the backend (R3).
- [ ] `StepUsage` carries `role`, `model_version`, `gpu_node_id`, `determinism`, `raw`; `role` resolution order is tested and `None` lands in `unattributed`, never dropped.
- [ ] A missing, unparseable, or un-POSTable usage report never changes a step's exit code; `cost_source="unknown"` rows are counted and surfaced as `cost_coverage`.
- [ ] gpu-node pricing is computed server-side from a configured node rate; the formula, quantization and `gpu_fraction` scaling have unit tests; no token price table exists.
- [ ] `TestRun.origin` + `trial_id` + `trial_iteration_id` land before the first dogfood trial; existing rows backfill to `origin="pipeline"`; the story-done gate and criterion history filter `origin="pipeline"` by default.
- [ ] A required criterion whose only green run came from a trial still blocks story done (contract test green).
- [ ] Board aggregation selects on `trial_iteration_id` only -- proven by a test that changes the history filter and asserts the board is byte-identical.
- [ ] Suite/case/strategy/trial/iteration/worker CRUD + validation endpoints implemented in `routers/benchmark.py` following the spec.py / test_results.py idiom (404 on unknown parent, 400 naming the vocabulary, deterministic ordering, limit/offset).
- [ ] `BenchmarkSuite.version` + `content_hash` and `StrategyTemplate.content_hash` + `is_frozen` exist; a trial-referenced template answers 409 to PATCH and offers `fork`.
- [ ] Trial launch refuses an unvalidated case, an invalid graph, an unbound role, or an absent budget -- each with a 409/400 naming the reason.
- [ ] `variant` (`normal | null-agent | base-state`) is a first-class trial field; controls expand automatically in an experiment matrix; an experiment launched without controls is marked `publishable: false`.
- [ ] `GET /api/bench/board` implements every `group_by`, the shared-budget re-scoring, the contamination split, bootstrap intervals, `separable: false` (never ranked, reason shown), and `trustworthy: false` on any control/provenance/coverage failure.
- [ ] `/api/bench/board/cases`, `/roles`, `/curve`, `/k-sweep` ship with the board.
- [ ] Every index in section 6 is in the M13 migration; the board's primary query is proven index-served (EXPLAIN assertion or a scan-count test).
- [ ] Export produces the documented bundle layout + `manifest.json` + `METHOD.md`; import round-trips it; a harness/image hash mismatch flags `provenance_mismatch` and downgrades the board loudly.
- [ ] All `bench_*` MCP tools registered; `bench_launch_experiment` defaults to `dry_run=True`.
- [ ] `lazyaf bench` group complete; `bench validate` exit code is the 13.1 CI gate; `experiment run` requires confirmation for real spend.
- [ ] Every test file named in the Tests section exists and is green, with zero new entries in `tdd/skip_baseline.json` (R4).
- [ ] The dogfood pipeline runs `lazyaf bench validate` on the starter suite on every push (R7), and the benchmark UI surfaces ship with Playwright specs named in their phase (R8).
