# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

> **Reconciled against the tree 2026-08-30.** Status claims in this file name
> their evidence. History lives in `historical-documents/`; this file is about
> what is next.

---

## What to do next

> Reconciled against the tree on **2026-08-30**. Every status below names its
> evidence — a file, a test, a commit, or a measured number. Where a claim could
> not be checked in the tree, it says so rather than assuming.
>
> Status vocabulary, used consistently in this document:
> **COMPLETE** (landed and evidenced) · **IN PROGRESS** (some parts landed,
> named individually) · **DESIGNED** (a written plan, zero implementation) ·
> **NOT STARTED**.

### 1. Now — finish Phase 12.8: retire the v1 array pipeline format

P1 and P2 landed in commit `b79bb7f`. **P3 through P6 have not started.** The
plan of record, with the strict file-ownership split and the acceptance gate, is
`upcoming/wave10-v1-retirement.md`.

| Step | What it is | State |
|---|---|---|
| P1 | The graph gains terminal actions: `StepActions`, `describe_terminal_action`, `_run_terminal_action` | **COMPLETE** — `b79bb7f`; `STEP_ACTION_PREFIXES` and `_run_terminal_action` both live in `backend/app/services/pipeline_executor.py` |
| P2 | `array_to_graph` becomes the faithful, *refusing* boundary converter | **COMPLETE** — `b79bb7f`; `backend/app/schemas/pipeline.py:597` |
| **P3** | **Every writer emits graphs; every reader stops reading the array; `steps` leaves the wire** | **NOT STARTED — this is the next action.** `PipelineRead.steps`, `PipelineCreate.steps` and `PipelineUpdate.steps` are all still on the wire (`backend/app/schemas/pipeline.py:236,255,268`) |
| P4 | Migration (next free revision id): backfill `steps` -> `steps_graph`, add the `definition_error` column | NOT STARTED. The `definition_error` *schema field* already exists (`schemas/pipeline.py:283`) but there is no column and no backfill revision. (The uncommitted `0012_workspaces_per_worker.py` in the tree is a **different** concurrent wave's migration — 12.8's backfill will need the next free revision id.) |
| P5 | Delete the executor's array fork | NOT STARTED — `is_graph`, `_handle_action` and `parse_steps` all still branch in `pipeline_executor.py` |
| — | **ACCEPTANCE GATE** (`wave10-v1-retirement.md` §5). Nothing below runs until it passes. | — |
| P6 | Migration (the revision after P4's): drop the `steps` column; the tombstone lands | NOT STARTED |

Two things to carry into P3 that the wave doc flags and the tree confirms:

- `PipelineRead.definition_error` must land **with** P4's column, not before it —
  until the column exists the API would read an attribute the ORM does not have.
- YAML export is a P3-adjacent hazard, not a cosmetic one. `export_pipeline_yaml`
  (`backend/app/routers/pipelines.py:576`) writes a graph as a `steps` **dict**
  keyed by step id, while `PipelineYaml.steps` is a **list** — so a graph export
  cannot be re-imported — and it drops `timeout`, `continue_in_context` and, since
  P1, `actions` entirely. Once actions are the only way a graph can express
  `merge:`/`trigger:`, a lossy export silently discards auto-merge. See T18 in the
  ledger.

### 2. Then — Milestone 13: the benchmark & evaluation harness

**NOT STARTED. Zero implementation.** A repo-wide grep for `BenchmarkCase`,
`StrategyTemplate`, `TrialIteration`, `fail_to_pass` and `cost_to_solve` returns
exactly one hit, and it is a comment
(`backend/app/services/agent_run.py:15`). The design is complete and lives in
this document plus `docs/milestone-13/`. Start at **13.1 (corpus & fixtures)**.

**One blocker the design does not mention.** `backend/app/models/workspace.py` at
HEAD declares `pipeline_run_id` with `unique=True`, so a pipeline run owns
exactly one workspace — meaning K parallel agents would share a single checkout.
That directly contradicts 13.2's "a branch and a workspace per worker", which is
the substrate the whole parallel-strategy thesis rests on. A concurrent wave is
fixing it (an uncommitted `backend/alembic/versions/0012_workspaces_per_worker.py`
is in the working tree). **Confirm that has landed and is migrated before
starting 13.2.**

Both of Milestone 13's mandatory in-12.x hooks are in place: `StepUsage` (12.5,
migration `0005_step_usage.py`) and the cost axis in 12.6.5.

### 3. Waiting — Phase 14.5: runner images with inference baked in

**DESIGNED, zero implementation.** No `vllm` or `ollama` appears anywhere under
`images/` or `scripts/`, and no GPU-yield / drain mechanism exists. Wiring doc:
`upcoming/wave9-145-runner-images.md`. It blocks nothing today, but it is what
makes Milestone 13's headline experiment — an expensive planner directing K
cheap local workers — runnable on hardware the owner already owns.

### 4. Standing — the open-item ledger

The [verified open items](#open-items-verified-2026-08-30) below are the
non-phase work: two security-posture problems, four correctness defects, and a
handful of validation gaps. They are carried here so they are not lost between
milestones. Each was re-checked against the tree on 2026-08-30; the ones fixed
that day were removed.

---

## Status at a glance

### Milestones

| Milestone | Status | Evidence |
|---|---|---|
| 1-11 (foundation through playground) | COMPLETE | `historical-documents/phase-01…phase-11` |
| **12 — Runner architecture + spec/eval layer** | **IN PROGRESS** — every phase through 12.7 COMPLETE; **12.8 open** | Detail retired to [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md); 12.8 tracked below |
| **13 — Benchmark & evaluation harness** | **NOT STARTED** | Zero implementation. Grep for `BenchmarkCase` / `StrategyTemplate` / `TrialIteration` / `fail_to_pass` / `cost_to_solve` across `backend/`, `frontend/`, `cli/`, `tdd/` returns one hit, a comment at `backend/app/services/agent_run.py:15`. Design: this document + `docs/milestone-13/` |
| **14 — Self-hosted OpenAI-compatible endpoints** | **COMPLETE** (2026-08-30) | Commit `4b429c6`: 56 files, ~21.5k lines. `ModelEndpoint` + migration `0011_model_endpoints.py`, capability probe, agent harness in `runner-common/runner_common/harness/`, stdlib mock OpenAI server, Endpoints UI. Out of the 12.x sequence |
| **14.5 — Runner images with inference baked in** | **DESIGNED** | Zero implementation: no `vllm`/`ollama` anywhere under `images/` or `scripts/`; no GPU-yield mechanism exists. Doc: `upcoming/wave9-145-runner-images.md` |

### Milestone 12 phases

Every phase below is COMPLETE and committed. Full narrative, deliverables and
exit gates: [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md).

| Phase | Status | Evidence |
|---|---|---|
| 0 — Self-hosting bootstrap | COMPLETE | Push to the internal remote runs the tiered dogfood pipeline; `scripts/ci_gate.py` enforces `tdd/skip_baseline.json` and the per-tier floors in `tdd/tier_floors.json` |
| 12.0 — Unify runner entrypoints | COMPLETE | `images/agent-base/Dockerfile` installs `runner-common` system-wide (line 29) and **asserts the import at build time** (line 34: `python3 -c "import runner_common.agent_wrapper as w; assert callable(w.main)"`). `images/claude` and `images/gemini` inherit from it. The three monolithic entrypoints were deleted in `67a4e1c` |
| 12.1 — LocalExecutor + step state machine | COMPLETE | `StepExecution` with unique `execution_key`, `StepStateMachine`, `recover_orphaned_executions()` at startup |
| 12.2-INT — Workspace persistence + executor wiring | COMPLETE | Run `71d56980` (push-triggered) executed all 6 steps in ephemeral containers on a persistent volume; `verify_executor` confirmed `executor='local'` for every one |
| 12.2.5 — Specification data model | COMPLETE | `backend/app/models/spec.py`, migration `0003_spec_layer.py`, live routes `/api/features`, `/api/user-stories`, `/api/criteria`, `/api/prompt-templates`; three north-star stories seeded |
| 12.3 — Control layer & step images | COMPLETE | `lazyaf-{base,claude,test-runner}:dev` built by `scripts/build_images.py` with content-hash labels; in-container runtime reports to `/api/steps/*`; run #11 passed the gate |
| 12.2.6 — Test result tie-back | COMPLETE | A push-triggered run wrote a TestRun joined to criterion `fb95f11d` (`passed` / commit `2a513dd4` / branch `main` / `us1.pipeline-outcome-gates-branch`); 9 TestRefs linked, 0 orphans. `GET /api/criteria/{id}/history` live (`routers/test_results.py:99`) |
| 12.4 — Script/docker steps fully ephemeral | COMPLETE | Script/docker execution removed from the images **and** from `runner-common`; the interim DooD anchor retired |
| 12.5 — Agent steps via the control layer | COMPLETE | Migration `0005_step_usage.py`; `StepUsage` live on the gate at 1163/7 tokens; `POST /api/steps/{id}/usage` |
| 12.6 — RemoteExecutor + runner agents | COMPLETE | Ported contract suite executes at zero skips; polling stack deleted and policed by `tdd/unit/services/test_no_legacy_code.py` (6 assertions, two mechanisms that cannot silently skip); all seven polling endpoints 404 on a live probe. Migrations `0006`, `0007` |
| 12.6.5 — Experiments & leaderboards | COMPLETE | Migration `0010_experiments.py`; `GET /api/experiments/{id}/leaderboard` and `GET /api/leaderboards/feature/{id}` (`routers/experiments.py:521,549`); finalize is one atomic CAS |
| 12.6.6 — Spec-curated agent context | COMPLETE | `backend/app/routers/spec_context.py`; `GET /api/cards/{id}/spec-context`; `card_id` resolves from step config as well as run context |
| 12.7 — Debug re-run mode | COMPLETE | `backend/app/routers/debug.py`, migration `0009_debug_sessions.py`, `images/debug-sidecar/`, `WS /api/debug/{id}/terminal`, `lazyaf debug` shipped in the wheel |
| **12.8 — Retire the v1 array format** | **IN PROGRESS** | P1+P2 landed (`b79bb7f`); **P3-P6 not started.** Plan: `upcoming/wave10-v1-retirement.md`. See [Phase 12.8](#phase-128--retire-the-v1-array-pipeline-format-in-progress) |
| 12.9 — Kubernetes orchestrator | NOT STARTED (deliberately future) | Scope decision 2026-08-29: K8s stays out of Milestone 12 |

> **Correction, 2026-08-30.** Earlier revisions of this file carried a note
> saying 12.0's COMPLETE mark "remains aspirational — the three runner images
> still ship monolithic entrypoints and do not import `runner-common`". That
> note was stale and is **wrong**: the images import `runner-common` and assert
> it at build time (see the 12.0 row above), and the monolithic entrypoints were
> deleted in `67a4e1c`. The same revisions also described 12.6.5, 12.6.6 and
> 12.7 as in progress after they had landed, and said 12.8 was blocked on a
> decision the owner had already made. All four are corrected here.

### Numbers

| Thing | Value | Source |
|---|---|---|
| T1 (unit + non-Docker integration) | **4724 executed, 0 failed** | Measured on the 12.8 P1-P2 landing (`b79bb7f`), which added ~2,240 lines of test. `tdd/tier_floors.json` still records the previous green measurement — floor 4432, measured 4523 on 2026-08-30 — because the floor is only raised on a deliberate ratchet. **The floor is stale-low by design, not by neglect; raise it on the next green T1.** |
| T2 (Docker integration) | 77 executed, floor 75 | `tdd/tier_floors.json` |
| T3 (e2e quick) | 22 executed, floor 21 | `tdd/tier_floors.json` |
| Alembic head (committed) | **`0011_model_endpoints`** | `git ls-files backend/alembic/versions/` — 0001-0007, 0009, 0010, 0011. There is no `0008`. `0012_workspaces_per_worker.py` exists in the working tree but is **not committed** (a concurrent wave) |
| MCP tools | 45 | `grep -c '@mcp.tool' backend/app/mcp/server.py` |
| Release CI | Publishes **9 images** to GHCR: 3 service (`backend`, `frontend`, `runner-agent`) + 6 step (`base`, `agent-base`, `claude`, `gemini`, `test-runner`, `debug-sidecar`) | `.github/workflows/images.yml`; the step list is read from `scripts/build_images.py`'s `IMAGES` table, not duplicated |
| Release tags | **None. `git tag` is empty.** `release.yml` triggers only on `push: tags: ['v*']` (plus manual dispatch), so the tag path has never fired. `images.yml` also runs on push to `main`. | `.github/workflows/release.yml:51-54`, `images.yml:60-64` |

---

## Open items (verified 2026-08-30)

Findings from the adversarial QA pass (`upcoming/qa-triage.md`, which keeps the
reproductions) plus the security review, carried here so they survive between
milestones. **Every item below was re-checked against the tree on 2026-08-30**
and the file:line evidence is the check. Items fixed that day were removed —
they are listed at the bottom so the shrinkage is visible rather than silent.

These are not a phase. Fold them into whatever phase touches the same file, or
schedule the security block on its own.

### Security posture

**S1 — No authentication on any human-facing router, while compose binds
`0.0.0.0` and mounts the Docker socket.** CONFIRMED.
Not one of `cards.py`, `pipelines.py`, `repos.py`, `spec.py`, `experiments.py`,
`model_endpoints.py` or `jobs.py` declares any dependency other than `get_db` —
there is no auth dependency anywhere on the human-facing surface. Machine
surfaces are the exception and *are* authenticated: `steps.py` and
`ws_runners.py` verify the step / runner JWT.

Meanwhile `docker-compose.yml:5` publishes `"8000:8000"` — no `127.0.0.1`
prefix, so the API listens on every interface — and `docker-compose.yml:14`
mounts `/var/run/docker.sock` into the backend, which is root-equivalent on the
host. The internal git server rides the same unauthenticated port.

The README is honest about the posture ("No authentication, and it holds your
Docker socket… Run it somewhere you trust, bound to localhost") but **no compose
file in the repo actually binds to localhost** — the only `127.0.0.1` in
`docker-compose.yml` is inside a healthcheck (line 144). So the instruction and
the shipped default disagree, and the default is the permissive one. Minimum
fix: make `127.0.0.1:8000:8000` the default binding and require an explicit
opt-out to widen it.

Related and still open: **T21** — the playground `internal/*` endpoints were
reported unauthenticated. *Not re-verified this pass:*
`backend/app/routers/playground.py` and `services/playground_service.py` were
under concurrent edit and deliberately not opened. Re-check before closing.

**S2 — Step containers have no CPU or PID limit, and there is no fan-out cap.**
CONFIRMED. `backend/app/services/execution/local_executor.py:773-774` sets
`mem_limit` when a step asks for one and sets nothing else — no `nano_cpus`, no
`cpu_quota`, no `pids_limit`. And `pipeline_executor.py` has no semaphore or
`max_parallel` of any kind, so a wide graph fans out as far as its edges allow.
One pipeline can starve the host the backend is running on.

### Correctness

**T5 — The run-list serializer ships every step's full logs.** CONFIRMED.
`GET /api/pipeline-runs` and `GET /api/pipelines/{id}/runs`
(`backend/app/routers/pipelines.py:399,420`) both return
`list[PipelineRunRead]`; `PipelineRunRead.step_runs` is `list[StepRunRead]`
and `StepRunRead.logs` is a plain `str` (`schemas/pipeline.py:349`). With
`limit` up to 100 runs, a dashboard poll drags every log line of every step of
every listed run across the wire. The fix is a list-shaped read model without
`logs`; the per-step log endpoint already exists
(`GET /api/pipeline-runs/{run_id}/steps/{step_index}/logs`).

**T9 — Duplicate graph edges dispatch a step twice — and now fire its effects
twice.** CONFIRMED.
`graph_definition_errors` says so in its own docstring: duplicate entry points
and duplicate parallel edges are "NOT checked here (deliberately)"
(`pipeline_executor.py:891-894`). In `_handle_graph_step_complete`, the fan-out
loop appends one entry to `steps_to_execute` per matching edge
(`pipeline_executor.py:4645-4665`), and the already-completed/already-active
guard is evaluated *before* `_reserve_active_steps` runs — so two identical
`A -> B` edges both pass the guard and `_execute_graph_step` is called twice
for B.

Since 12.8 P1 this got sharper. Terminal actions are keyed to node *completion*,
not to the edge (`pipeline_executor.py:4613-4634`), and there is no
already-handled guard at the entry of `_handle_graph_step_complete` — so a node
that completes twice fires its `merge:` / `trigger:` twice. A duplicated edge is
now a double merge or a duplicate spawned card, not just a wasted container.
Cheapest fix: reject duplicate edges in `graph_definition_errors` (422 at the
boundary), and de-duplicate `steps_to_execute` as defence in depth.

**T10 — `POST /api/pipelines/{id}/run` walks the whole graph inside the request
handler.** CONFIRMED. `run_pipeline` still does
`await pipeline_executor.start_pipeline(...)` inline
(`backend/app/routers/pipelines.py:309,378`), and there is no bound anywhere on
graph size — no `MAX_STEPS`, no step-count check in `pipeline_executor.py` or
`schemas/pipeline.py`. This is the one place the codebase departs from standing
rule **R5** (async-first: "HTTP/git-push handlers return a run id immediately").
Measured at 299 s on a 400-step chain in the QA pass.

**T18 — YAML export is lossy, and for graphs not re-importable.** CONFIRMED, and
worse since 12.8 P1. `export_pipeline_yaml`
(`backend/app/routers/pipelines.py:576-620`) emits a graph's `steps` as a **dict
keyed by step id** with `on_success` holding a list on fan-out, while the import
schema `PipelineYaml.steps` is a `list[PipelineStepYaml]` whose `on_success` is a
single `str` (`schemas/lazyaf_yaml.py:35-60,101`). The export also drops
`timeout`, `continue_in_context`, and — the new one — `actions`, which since P1
is the *only* way a graph expresses `merge:` and `trigger:`. Exporting a pipeline
that auto-merges and re-importing it produces a pipeline that does not.

### Validation and robustness

**T13 — `.lazyaf/pipelines/*.yaml` is a second, unvalidated definition door.**
CONFIRMED. `PipelineStepYaml.type` is `str = Field("script", …)`
(`schemas/lazyaf_yaml.py:54`) — a bare string, so `type: banana` is accepted —
and `timeout: int = Field(300, …)` (line 58) has no bounds, so `timeout: -5`
passes. Both should be a `Literal` and a bounded `int` respectively; the platform
API and the YAML door should validate identically.

**T14 — A malformed pipeline YAML vanishes from the listing and 500s on fetch
with the raw Python exception.** CONFIRMED. The list endpoints swallow it with
`except Exception … continue` and a bare `print`
(`routers/lazyaf_files.py:83-86` and `179-182`), so a broken file is invisible;
fetching it directly raises
`HTTPException(500, detail=f"Error parsing pipeline file: {e}")`
(lines 128, 225, 279). Silent for the case that matters, loud and leaky for the
case that does not.

**T20 — The usage manifest accepts impossible accounting.** CONFIRMED.
`UsageManifest` (`backend/app/schemas/usage.py:57-75`) declares
`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`,
`wall_clock_ms`, `container_seconds` and `gpu_fraction` with no `Field`
constraints at all — negative tokens, negative wall-clock and
`gpu_fraction: 99999` are all valid. This one matters more than it looks:
Milestone 13's headline metric is cost-to-solve, computed from exactly these
numbers.

**T22 — Internal exception text leaks into user-facing `detail` strings.**
CONFIRMED. `routers/git.py:71,73,100,104,154,156`,
`routers/lazyaf_files.py:128,225,279,307` and `routers/pipelines.py:367` all
interpolate a caught exception straight into the response body.

**T24 — Assorted polish.** Partly fixed on 2026-08-30 ("1 steps" and the
long-name overflow landed in `a39cb24`). The remainder — 400-vs-409
inconsistency, unbounded `commits?limit`, silent PATCH drops — was not
re-verified this pass.

### Closed on 2026-08-30 — verified fixed, not just claimed

Kept as a short list so a reader can tell the ledger shrank on evidence.

| Was | Fixed by | Verified how |
|---|---|---|
| T1 — every timestamp naive UTC, durations render negative | `db5f9f5` | `UTCDateTime` is the serialization type throughout `schemas/pipeline.py` |
| T2 — card lifecycle had no state guards; `PATCH` could fabricate `done` | `db5f9f5` | `_require_status` + one transition table in `routers/cards.py` |
| T3 — unhandled DB exceptions returned plain-text 500s and killed the connection | `db5f9f5` | Structured JSON handler; connection preserved |
| T4 — structurally broken graphs reported PASSED | `db5f9f5` | `_verify_graph_coverage` now gates every success verdict (`pipeline_executor.py:2055`) |
| T6 — `start`/`retry` were read-check-write races | `db5f9f5` | `_claim_card` is a conditional UPDATE whose rowcount is the decision (`routers/cards.py:266,1019`) |
| T8 — no length bound on any name field | `db5f9f5` | Constrained `Name` / `Body` string types (`schemas/_strings.py:76,89`) applied across the schemas |
| T12 — deleting a pipeline/repo mid-run cascaded the live run away | `db5f9f5` | Named in the commit; guard added in the pipeline/repo delete paths |
| T16 — the seeded review card pointed at a branch nothing created | `db5f9f5`, `5334b09` | Seed is idempotent and creates the branch |
| T17 — pipeline name went raw into `Content-Disposition` | `db5f9f5` | `_content_disposition()` now owns the header (`routers/pipelines.py:642`) |
| T19 — ingested repo said `main`, git HEAD said `master` | `db5f9f5` | `dulwich init_bare` HEAD now matches the row |
| T23 — step/runner JWT secrets defaulted to published constants | `acb7408` | `backend/app/config.py` refuses `RETIRED_PUBLIC_SECRETS` and `_PLACEHOLDER_SECRETS`, raising `MissingSecretError`; the only escape is `LAZYAF_DEV_EPHEMERAL_SECRETS=1` |
| T7 — UI never resynced after a dropped socket | `a39cb24` | Per the commit: `ConnectionStatus` names staleness and reconnects without a reload. *Frontend files were under concurrent edit and not opened; this rests on the commit message, not a direct read.* |
| T15 — `resolve-conflicts` had no state guard | `db5f9f5` | `_require_status(RESOLVE_CONFLICTS_FROM)` plus an atomic claim (`routers/cards.py:900`). The separate "force-merges invented content with no conflict present" half was **not** re-verified — treat that half as open until someone checks |

---

### Completed phases — where the detail went

Every phase from 1 through 12.7 is COMPLETE. The narrative, deliverables, exit
gates and test-first tables live in `historical-documents/`; this file keeps only
the status line and the evidence.

| Phase | Detail |
|---|---|
| 1 — Project Foundation | [`phase-01-project-foundation.md`](historical-documents/phase-01-project-foundation.md) |
| 2 — Repo & Card Management | [`phase-02-repo-and-cards.md`](historical-documents/phase-02-repo-and-cards.md) |
| 3-3.75 — Runner Pool & Git Server | [`phase-03-runner-pool.md`](historical-documents/phase-03-runner-pool.md) |
| 4 — Agent Integration | [`phase-04-agent-integration.md`](historical-documents/phase-04-agent-integration.md) |
| 5 — Review Flow | [`phase-05-review-flow.md`](historical-documents/phase-05-review-flow.md) |
| 6 — Polish (ongoing QoL) | [`phase-06-polish.md`](historical-documents/phase-06-polish.md) |
| 7 — MCP Interface | [`phase-07-mcp-interface.md`](historical-documents/phase-07-mcp-interface.md) |
| 8 — Test Result Capture | [`phase-08-test-capture.md`](historical-documents/phase-08-test-capture.md) |
| 8.5 — CI/CD Foundation | [`phase-08.5-cicd-foundation.md`](historical-documents/phase-08.5-cicd-foundation.md) |
| 9-9.1 — Pipelines | [`phase-09-pipelines.md`](historical-documents/phase-09-pipelines.md) |
| 10 — Events & Triggers | [`phase-10-events-and-triggers.md`](historical-documents/phase-10-events-and-triggers.md) |
| 11 — Agent Playground | [`phase-11-agent-playground.md`](historical-documents/phase-11-agent-playground.md) |
| — Pipeline node graph UI ("graph creep") | [`graph-creep.txt`](historical-documents/graph-creep.txt) |
| **0, 12.0-12.7 — Runner architecture + spec/eval layer** | [**`phase-12-runner-architecture.md`**](historical-documents/phase-12-runner-architecture.md) — architecture reference, attempt-#3 phase plans with exit gates, the January-era per-phase specs, and the superseded January plan for 12.8 |

Implementation-level wave documents (contracts, review findings, verification
transcripts) stay in `upcoming/`: `wave2-123-wiring.md`, `wave4-125-wiring.md`,
`wave5-126-wiring.md`, `wave6-1265-wiring.md`, `wave6-1266-wiring.md`,
`wave6-127-wiring.md`, `wave8-m14-wiring.md`, `wave9-145-runner-images.md`,
`wave10-v1-retirement.md`. QA evidence: `qa-triage.md` and the
`qa-findings-*.md` set. Post-mortem of the abandoned first attempt:
`failure_01-salvage-audit.md`.

---

## Milestone 12 — Attempt #3 Roadmap

> **Where it stands (2026-08-30): every phase through 12.7 is COMPLETE and
> committed; 12.8 is the only one open.** The per-phase plans, deliverables
> and exit gates for the completed phases were retired on 2026-08-30 to
> [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md).
> What stays here is what still governs: the history, the north-star user
> stories, the standing rules, the open phase, and the decision log.

> Scope decision (owner, 2026-08-29): finish EVERYTHING numbered 12.x — the runner
> architecture arc AND the spec/eval layer (12.2.5, 12.2.6, 12.6.5, 12.6.6).
> 12.9 (Kubernetes) stays future. No external CI, ever: LazyAF gates LazyAF,
> starting on the legacy execution path and ratcheting onto the new architecture
> as it lands. This section supersedes the old "Why this order" note (12.2.6 now
> deliberately follows 12.3 — the protocol froze in January; the retrofit is planned,
> not accidental).

### History (why this is attempt #3)

- Attempt #1 (`failure_01`, 2026-01-03/04): 12.0 -> 12.7 in two days. Collapsed.
  Post-mortem + salvage map: `upcoming/failure_01-salvage-audit.md`.
- Attempt #2 (main, 2026-01-09/13): test infra first, 12.0-12.3 built as clean
  libraries — never wired into the live path.
- Attempt #3 (now): wire the dark libraries, finish the arc, with the platform
  gating its own development the whole way.

### North-star user stories (the e2e layer encodes these; 12.2.5 stores them)

US-1 "Commits land, AI workflows run" (self-hosted CI / dogfood)
     Given a repo ingested into LazyAF with a pipeline bound to a push trigger,
     when I push to the internal remote, the pipeline runs my steps (tests,
     builds, agent steps) in isolated containers, live status/logs stream to the
     UI, and the outcome gates the branch. Milestone acceptance bar: LazyAF runs
     LazyAF's own tdd suite this way — with the execution tiers ACTUALLY
     EXECUTING (see R4; a green run that skipped the Docker tier is a failure).

US-2 "Card dev loop"
     Given a card describing a feature, when I start it, an agent implements it
     on a branch; completion triggers the gating pipeline; on pass the card
     reaches review with a diff; approve merges to target. Continuously covered
     from 12.5 on by a zero-cost mock-agent e2e inside the dogfood suite.

US-3 "Compare bench"
     Given a workflow/card and a set of (model x prompt) variants, when I launch
     a comparison, each variant runs in isolation and I get a side-by-side of
     outcomes (pass-rates per criterion, diffs, cost/time). Named artifact:
     tdd/e2e/test_experiment_matrix.py (12.6.5's exit gate).

Standing note: the owner's remote ambition is runpod.io-style nodes hosting
cheap models (hybrid agentic programming). Such pods often run AS containers
with no Docker socket — so the 12.6 runner-agent protocol must NOT assume
Docker; the agent's executor seam stays pluggable (LocalExecutor today, native
or OpenAI-compatible agents later) without protocol changes.

### Standing rules (distilled from the failure_01 post-mortem — non-negotiable)

R1 NOTHING GOES DARK. New execution code is wired into the default dev compose
   path the day it lands, and the e2e suite runs through it. Routing is
   OBSERVABLE: StepRun records which executor ran it (local|legacy|remote), the
   dogfood pipeline asserts the expected executor per phase via the API, and a
   spy test proves locally-routed steps never enter job_queue. A silent
   fallback to legacy is indistinguishable from success without this.
R2 DELETE ONLY AFTER ACCEPTANCE. The legacy path stays callable until the
   replacement passes the ported contract suites end-to-end; removal is its own
   commit with every consumer (frontend, playground) migrated in it.
R3 ONE SOURCE OF TRUTH PER WIRE CONTRACT. Control-layer payloads, WS runner
   protocol, test manifests: shared pydantic schemas or contract tests pinning
   both sides, plus at least one real round-trip test per contract
   (container -> API -> DB row).
R4 NO FAKE GREEN. No `pass # architecture ensures this` tests. Skip budget is
   a committed baseline (`tdd/skip_baseline.json`: per-file skip reasons);
   the CI gate runs pytest with skip reporting and FAILS if (a) any
   environment-dependent skip appears that is not in the baseline, or (b) a
   tier's executed-test count drops below its committed floor. Un-skipping
   requires shrinking the baseline in the same commit (the ratchet — the 137
   dormant 12.6 contract tests must hit zero skips by 12.6's gate).
   xfail(strict=True) where a target is known-missing.
R5 ASYNC-FIRST. Pipeline runs execute as asyncio tasks with their own session
   scope; HTTP/git-push handlers return a run id immediately. No awaiting
   containers inside request handlers. selectinload every relationship touched
   in async services; typed publish API on the WS manager.
R6 REAL SEAMS IN TESTS. At least one integration test per phase uses named
   volumes (not tmp_path bind mounts). Workspace/mount addressing is an
   explicit enum (volume | bind) — never inferred from path shape; unit test
   feeds a Windows path (C:\...) to prove no misclassification. E2e
   real-backend tier runs workers=1 or per-worker namespaces; the test-reset
   endpoint resets in-memory singletons too. Tests covering broadcast paths
   use the real WS manager with a capturing transport — never an AsyncMock.
R7 SELF-HOSTING RATCHET. The dogfood pipeline definition lives in-repo and
   syncs on push (see Phase 0a); it is upgraded to consume each phase as it
   lands and is the standing acceptance test for the whole arc.
R8 UI SHIPS WITH ITS SPEC. Every phase that ships or rebuilds a UI surface
   ships a Playwright spec for it in the same phase, named in the deliverables.
   "UI shows it live" in an exit gate always means a named spec, never vibes.
   Pure store logic gets vitest coverage (first frontend unit layer).

### Standing operational policy

Still governing, carried over from the attempt-#3 sequencing note. (The Track A
/ Track B split it accompanied is now history — see
[`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md)
Part 1.)

- **Alembic migrations land serialized on `main` only**; a parallel wave rebases
  before generating one. Startup runs `alembic upgrade head`; an existing
  unversioned dev DB is stamped at baseline first.
- Committed head is **`0011_model_endpoints`** (there is no `0008`). Two waves
  currently want the next id — 12.8 P4's backfill and the uncommitted
  `0012_workspaces_per_worker.py` — so **check `git ls-files
  backend/alembic/versions/` before you generate**, not just the directory
  listing.

---

### Phase 12.8 — Retire the v1 array pipeline format [IN PROGRESS]

The last open phase of Milestone 12, and the only one with work left in it.
Plan of record — ordered phases, strict file ownership, pinned cross-agent
contracts, and the acceptance gate: **`upcoming/wave10-v1-retirement.md`**.

**The decision** (owner, 2026-08-30): execution goes **graph-only**. The array
survives only as an authoring convenience at two edges — repo YAML and the
pipeline API — both converting through `array_to_graph` at the boundary. See the
decision log entry for the full shape.

**Why it could not be done by subtraction.** `on_success` / `on_failure` in v1
is two vocabularies wearing one coat: `next` / `stop` are *flow* and already
exist as graph edges, but `merge:{branch}` and `trigger:{card}` are *effects*
that lived nowhere except `_handle_action`, which only the array branch reached.
`array_to_graph` silently dropped both, so deleting the array path would have
quietly removed card auto-merge and pipeline chaining. P1 landed the replacement
before anything was removed.

#### State

| Step | Deliverable | Status |
|---|---|---|
| P1 | `StepActions` on `PipelineStepV2`, `describe_terminal_action`, `_run_terminal_action` and its dispatch site; `_trigger_card` / `_merge_branch` / `_resolve_merge_source_branch` gain `step_id`-keyed forms. Plus executed tests for graph `failure` and `always` edges, which had **never** been dispatched by any test in any tier. | **COMPLETE** — `b79bb7f` |
| P2 | `array_to_graph` becomes faithful and *refuses loudly* what it cannot represent — naming the step, the value and the vocabulary. Refuses `trigger:pipeline:`, unknown actions, duplicate ids, empty input, and any conversion producing an unreachable node. Two deliberate carve-outs: a mid-array `stop` is a refusal (v1 could hold a contradiction the graph cannot), and flow on the *terminal* step is neither an edge nor a refusal (the dogfood pipeline's last step is `on_success: next` with nothing after it). | **COMPLETE** — `b79bb7f` |
| **P3** | Every writer emits `steps_graph=`; `PipelineRead.steps` and its `parse_steps` validator are deleted; `pipeline_to_ws_dict` loses the key; `PipelineUpdate._reject_nulls` drops `"steps"`; `serialize_steps` goes; `verify_executor` re-keys on `step_id`; the frontend loses `convertLegacyToGraph`. | **NOT STARTED — next action** |
| P4 | Migration: backfill `Pipeline.steps` -> `steps_graph` for every row without a graph using an **inlined, frozen** converter, and add the `definition_error` column. Pure `UPDATE` + additive `ALTER` — no column dropped, no table rebuilt, fully reversible. | NOT STARTED |
| P5 | Delete the executor fork: `_execute_step`, `_handle_action`, `_trigger_pipeline`, `parse_steps`, `describe_step_action` / `STEP_ACTIONS` / `STEP_ACTION_PREFIXES`, `Pipeline.has_graph_definition()`, the `else:` branch of `start_pipeline`, and the `is_graph` / `steps` parameters threaded through the local-step helpers. **The fork that gets missed**: `_on_step_complete_locked` (the job-callback path) recomputes `graph` itself rather than receiving `is_graph`. | NOT STARTED |
| — | **ACCEPTANCE GATE** — `wave10-v1-retirement.md` §5: named tests exist and pass, all three tiers green, and the dogfood ratchet holds on **real backfilled data**. Nothing below runs until it passes. | — |
| P6 | Migration: `batch_alter_table('pipelines')` + `drop_column('steps')`; `Pipeline.steps` removed from the model; `_adopt_unversioned` taught about retired columns; the no-legacy guards added. | NOT STARTED |

#### The invariant that makes this safe

`Pipeline.steps` stays on the ORM model, with its python-side `default="[]"`,
until the very last phase. The column is `nullable=False` with **no
server_default** (`0001_baseline.py` declares a bare
`sa.Column('steps', sa.Text(), nullable=False)`), so any state where the model
has stopped declaring the field while the column still exists is a backend that
cannot INSERT a pipeline. Keeping model and column together removes that hazard
— and that is what lets the migration split into two revisions, which is what
buys **R2**: acceptance is a green dogfood run on real backfilled data, and it
happens *between* the two revisions.

#### Also in 12.8

- The dogfood pipeline converts to a v2 graph. That conversion is what proves
  the retirement, not a unit test.
- Retire completed phase sections to `historical-documents/`. **Done
  2026-08-30**: the 12.0-12.7 narrative moved to
  [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md),
  taking this file from 3,858 lines to roughly a third of that.
- `runner-common` adopted everywhere. **Already true** — see the 12.0 row in the
  status tables; the open question in the decision log about whether 12.0
  "counts as done" is resolved.
- Audit the January-era 12.8 regression matrix against the dogfood suite and
  backfill the gaps. The matrix itself is in
  [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md)
  Part 4; the legacy-removal half is already covered by
  `tdd/unit/services/test_no_legacy_code.py`.
- Dead-code sweep and docs.

---

### Phase 12.9 — Kubernetes Orchestrator [NOT STARTED, deliberately future]

> Out of scope for Milestone 12 by owner decision (2026-08-29). Nothing below
> exists in the tree: there is no `KubernetesOrchestrator` and no
> `test_k8s_orchestrator_contract.py`. Kept here rather than retired because it
> is future work, not history.

**Goal**: Same code works on Kubernetes

#### Tests First (Define Contracts)

**test_k8s_orchestrator_contract.py** - Write BEFORE implementing K8s
| Test | Defines Contract |
|------|------------------|
| `test_creates_k8s_job_for_step` | Job resource created |
| `test_uses_pvc_for_workspace` | PVC mounted |
| `test_node_selector_from_labels` | Labels -> node selector |
| `test_job_completion_detected` | Job status watched |

- [ ] Write `test_k8s_orchestrator_contract.py` (defines K8s behavior)

#### Implementation (Make Tests Pass)

- [ ] Implement `KubernetesOrchestrator` (make tests pass)
- [ ] PersistentVolumeClaims for workspaces
- [ ] K8s Jobs for step execution
- [ ] Node selectors based on runner labels
- [ ] Integration tests in K8s environment

#### Done Criteria

- [ ] K8s orchestrator tests pass (mocked)
- [ ] Integration tests pass (real K8s)

**Effort**: 2-3 weeks when needed
**Outcome**: Production-ready K8s deployment

---

### Decision log

> The record of what was decided, by whom, and why. Newest blocks last.

#### Milestone 12, attempt #3 (2026-08-29)

- 2026-08-29 Scope: all of 12.x; K8s stays future. (Owner)
- 2026-08-29 No external CI ever; self-host ASAP. (Owner)
- 2026-08-29 12.6 loopback-first; remote hardware manual. (Owner)
- 2026-08-29 12.2.6 re-sequenced AFTER 12.3 (protocol froze in January;
  retrofit is deliberate). (Claude, from audit)
- 2026-08-29 Workspace population: helper-container clone over a named
  compose network into /workspace/repo. (Claude — veto welcome)
- 2026-08-29 Interim DooD: dedicated CI runner with Docker socket for the T2
  tier until 12.4's step-container socket option. (Claude — veto welcome)
- 2026-08-29 Playground migrates off job_queue in 12.5. (Claude)
- 2026-08-29 NativeOrchestrator deferred; runner-agent executor seam stays
  pluggable/Docker-agnostic for runpod-style + OpenAI-compatible agents.
  (Claude, honoring owner's hybrid-model ambition)
- 2026-08-29 12.6.5 guardrails: dry-run estimate + per-experiment cap, not
  confirm-only. (Claude — veto welcome)

Decisions made DURING implementation (all shipped and gate-verified):
- 12.3 reporting path: control-mode steps report through the in-container
  runtime to /api/steps/*, which is the SOLE StepRun.logs writer; stock
  images keep the stdout-stream path. Mode is explicit (image label value
  `1` + LAZYAF_CONTROL), never inferred, and a control step whose runtime
  never reported CANNOT pass (StepExecution stuck PREPARING = loud failure).
- 12.3 config delivery: per-step `/workspace/.control/<step_execution_id>.json`
  via put_archive onto a created-but-unstarted container, consumed-once.
  (Per-step, not per-run: the run-scoped path collided under graph fan-out.)
- Step capabilities: `needs: [docker]` sugar translates to the socket mount
  behind an allowlist — one translation site for 12.4 to change. Interim
  runner-service DooD anchor retired when the tiers moved to step containers.
- Images: `lazyaf-{base,claude,test-runner}:dev`, content-hash labels, built
  by `scripts/build_images.py`; T2 preflights `--check` so stale images fail
  loudly instead of silently testing yesterday's runtime. Backend never
  auto-builds; `step_default_image` deliberately stays a pullable image.
- Environment invariants learned from dogfood runs #5-#11 (each now regression
  -tested): population chowns the repo to the step uid; the entrypoint joins
  the socket group before gosu; the tree hash sorts POSIX-normalized paths;
  Docker client timeout must exceed the longest container wait budget.

- 2026-08-29 Benchmark harness scoped as Milestone 13 (corpus + trials +
  effectiveness board), with two mandatory hooks inside 12.x: StepUsage
  telemetry in 12.5 and the cost-to-solve axis in 12.6.5. (Owner ambition,
  Claude scoping)
- 2026-08-29 Benchmark oracle is layered (fail_to_pass/pass_to_pass tests +
  optional linked UserStory criteria); corpus = ingested repos pinned per case;
  loop = sequential pipeline runs driven by a Trial orchestrator (the graph is a
  DAG — no cycles); cost = CLI-reported + GPU-node model. (Owner)

- 2026-08-29 Milestone 13 targets a PUBLIC write-up + reproducible bundle:
  provenance per trial (image hashes, harness/model version, policy), variance
  over N repeats reported natively, controls (base-state + null-agent), public
  fixtures with contamination noted, and headline metrics = cost-to-solve,
  regression rate, iterations-to-solve. (Owner — "I am a scientist at heart")

- 2026-08-29 Benchmark trials vary ARBITRARY STRATEGY GRAPHS (one-shots,
  adversarial fan-outs, planner->K-workers->integrator, gated combinations), not
  a fixed set of loops; models are bound to graph ROLES per trial, so a single
  strategy may mix a high-end planner with cheap parallel workers. Cost is
  attributed per role; wall-clock and integration-conflict rate join the board
  because parallelism trades money for latency. (Owner)
- 2026-08-29 No v0 case study from existing session data — wait for real
  controlled trials with a one-shot baseline. (Owner)

- 2026-08-29 Parallel strategies are GIT-NATIVE: branch + workspace per worker
  off the case base commit, rejoined through the platform's existing
  merge/rebase/resolve-conflicts machinery. Workers never share a checkout, so
  parallelism is not a hazard to engineer around - it is the substrate LazyAF
  already is. Integration POLICY (sequential-merge / rebase / cherry-pick /
  agent-composed, and on-conflict: fail | resolver-agent | human) is part of the
  strategy under test, and integration conflict/resolution cost are measured
  outcomes. Structured conflicts make conflict resolution itself an
  agent-addressable step - a strategy variant a single-sandbox harness cannot
  express. (Owner: "lazyaf is the bridge")

#### Milestone 14 and 14.5 (2026-08-30)

- **2026-08-30 We own the agent loop.** A minimal tool-calling harness in
  `runner-common`, running inside the existing control-mode container as a new
  entry in the `EXECUTORS` registry beside claude/gemini/mock. Wrapping an
  existing OSS agent CLI was rejected for one specific reason: Milestone 13
  makes *loop shape* the independent variable, and a loop we do not own is one
  we cannot vary. (Owner) — **shipped**, `runner-common/runner_common/harness/`.
- **2026-08-30 Capability is probed and recorded, never assumed.** Tool-calling
  support is inconsistent across self-hosted models, so probe at registration
  and store the result on the endpoint. This turns "does tool support matter?"
  from an assumption into something Milestone 13 can measure. (Owner) —
  **shipped**; verified against this host's ollama, where `llama3.1:8b` reported
  `probe_status: ok`, tools/streaming/usage all true, and a context window of
  `131072` discovered through ollama's `POST /api/show` extension.
- **2026-08-30 Three reachability modes**, because the deployments genuinely
  differ: `runner-local` (home bare metal behind NAT), `direct` (default;
  runpod and any routable endpoint), `proxy` (central auth/logging — opt in per
  endpoint, never the default, because it makes the backend an inference
  bottleneck). (Owner)
- **2026-08-30 Combined runner+inference images, not a local compose profile.**
  One image = one deployable node: `FROM` the upstream inference server with the
  12.6 runner agent layered on. The pod dials OUT over WebSocket, so **zero
  inbound connectivity** — which is what makes the same image work on runpod and
  behind home NAT. We do **not** rebuild the inference servers; forking
  `ollama/ollama` and `vllm/vllm-openai` would mean multi-GB pushes on every
  release plus tracking CUDA/torch compatibility forever. (Owner)
- **2026-08-30 14.5 targets Windows desktops with idle RTX cards.** Docker
  Desktop on the WSL2 backend, running the *same* image as runpod with
  `--gpus all` (NVIDIA's CUDA-on-WSL driver makes this work), so there is
  nothing Windows-specific to maintain. The box takes a dual role — serve models
  **and** execute steps — advertised as labels rather than as two deployment
  shapes. (Owner)
- **2026-08-30 Yield on GPU busy by DRAINING, not disconnecting.** LazyAF must
  not fight the owner for his own GPU: the node samples GPU utilization and
  stops accepting new assignments while letting an in-flight step finish, then
  resumes, with hysteresis so a transient spike cannot flap it. The naive
  version — disconnect when busy — is wrong *precisely because* 14.5 establishes
  that the connection IS the advertisement, so dropping it would orphan running
  work. Expect a small backend availability flag. The UI must show WHY a node is
  not taking work, and a manual force-drain / force-available override is
  required. (Owner)
- **2026-08-30 LazyAF builds its own heavy runner images, not GitHub.** A
  repo-defined pipeline with docker steps builds and pushes
  `lazyaf-runner-ollama` and `lazyaf-runner-vllm` on a LazyAF runner agent. Two
  reasons, and the second is the hard one: a ~45GB vLLM build does not fit a
  standard GitHub runner, and **a self-hosted Actions runner on a PUBLIC repo
  would execute fork PRs on the owner's hardware**. GitHub keeps the wheel, the
  small service images, the existing step images, secret-scan and
  release-please; `pr-build.yml` stays GitHub-hosted for exactly that reason.
  GHCR credentials reach the build step through 12.5 `secret_environment`, so
  they never appear in `docker inspect`. (Owner) — **dogfooding at the top of
  the stack: the platform ships its own artifacts.**

#### Phase 12.8 (2026-08-30)

- **2026-08-30 RESOLVED: retire the v1 array pipeline format.** (Owner: "retire
  the old pipeline format".) The shape is the one 12.8 recommended and the owner
  confirmed: **execution is graph-only**. Concretely:
  - `pipeline_executor` loses its array branch entirely — `is_graph` and every
    two-way fork behind it disappear, leaving ONE path through the executor.
    This is the whole point: the array path is a second execution semantics
    nobody reads, and every graph fix since 12.4 had to be written twice.
  - The array survives ONLY as an authoring convenience at two edges — repo YAML
    (`.lazyaf/pipelines/*.yaml`) and the pipeline API — both converting via
    `array_to_graph` at the boundary. A human writing a five-step pipeline
    should not have to hand-author nodes, edges and positions; a human is not
    the executor.
  - Everything that currently persists `steps=json.dumps([...])` writes a graph
    instead: `trigger_service.upsert_materialized_pipeline`, `agent_run` (both
    card-work sites), `experiment_service`, the test-mode seed.
  - `Pipeline.steps` is backfilled into `steps_graph` by a migration and then
    DROPPED (R3: one source of truth per wire contract; R2: deleted after the
    backfill is accepted). `0007_drop_polling_runner_columns.py` is the
    precedent for the SQLite table rebuild.
  - The dogfood pipeline converts to a v2 graph, which is what proves it.
- **2026-08-30 The retirement is additive-first, in six ordered phases.**
  (Claude, from the wave-10 recon; owner veto welcome.) The capability lands
  before anything is removed, and the column drop is a *separate revision* from
  the backfill so acceptance — a green dogfood run on real backfilled data — can
  happen between the two. A departure from the earlier recommendation of one
  combined revision: that NOT-NULL hazard only bites if the model field is
  removed before the column, and here it is not.
- **2026-08-30 `StepActions` is named `actions.`, deliberately not
  `on_success`.** (Claude.) `export_pipeline_yaml` already writes `on_success` on
  a graph step meaning "the ids of the nodes this edge points at" — and on a
  fan-out that is a LIST, the same shape an action list would be. The two would
  have been indistinguishable by shape.
- **2026-08-30 RESOLVED: 12.0 counts as done.** The earlier open question — does
  12.0 close at 12.5 (runner-common adopted by agent images) or at 12.8 (all
  runners retired)? — is answered by the tree: `images/agent-base` installs
  `runner-common` system-wide and asserts the import at build time, `claude` and
  `gemini` inherit it, and the monolithic entrypoints were deleted in `67a4e1c`.

---

## Milestone 13 — Benchmark & Evaluation Harness

> **STATUS: NOT STARTED — zero implementation, as of 2026-08-30.** A repo-wide
> grep for `BenchmarkCase`, `StrategyTemplate`, `TrialIteration`, `fail_to_pass`
> and `cost_to_solve` across `backend/`, `frontend/`, `cli/` and `tdd/` returns
> exactly one hit, and it is a comment
> (`backend/app/services/agent_run.py:15`). Everything below is **design**. The
> data models described under "Specification Layer Models" for
> `BenchmarkSuite` / `BenchmarkCase` / `StrategyTemplate` / `Trial` /
> `TrialIteration` are likewise designed and unbuilt — no table, no migration,
> no router.
>
> Both mandatory in-12.x hooks did land, so this does not start with a retrofit:
> `StepUsage` (12.5, migration `0005_step_usage.py`) and the cost axis in
> 12.6.5.
>
> **Known blocker, not mentioned in the design below.**
> `backend/app/models/workspace.py` declares `pipeline_run_id` with
> `unique=True`, so a pipeline run owns exactly one workspace and K parallel
> agents would share a single checkout — which contradicts 13.2's "a branch and
> a workspace per worker", the substrate the whole parallel-strategy thesis
> rests on. A concurrent wave is fixing it (an uncommitted
> `backend/alembic/versions/0012_workspaces_per_worker.py` sits in the working
> tree). Confirm it has landed and migrated before starting 13.2.


> **The question this answers:** take a repo at a known state, set the AI loop
> loose, and measure what it actually cost to get to a solution — across models,
> prompts, loop policies, and problem verticals. Not "can an agent do this once"
> but "which loop is *effective*, and at what price".
>
> Scoped 2026-08-29 from the owner's benchmark ambition. Milestone 12 builds the
> execution platform; Milestone 13 turns it into an instrument. Two hooks
> (StepUsage in 12.5, the cost axis in 12.6.5) MUST land inside 12.x or this
> milestone starts with a retrofit.

### What already exists to build on

| Need | Already in place |
|------|------------------|
| Deterministic starting state | `populate_workspace(..., commit_sha)` — workspaces already clone at a pinned commit (12.2-INT) |
| Isolated, reproducible execution | Ephemeral control-mode containers on per-run volumes, byte-reproducible images (12.3) |
| Outcome capture per test | TestRef / TestRun tie-back keyed to criterion + commit (12.2.6) |
| Matrix fan-out | `Experiment.matrix = {models, prompts, repeat}` + executor fan-out (12.6.5) |
| Effort capture | `StepUsage` (12.5 — the new hook) |
| Human-meaningful intent | Feature / UserStory / AcceptanceCriterion (12.2.5) |

### Design decisions (owner, 2026-08-29)

- **Success oracle: BOTH, layered.** Each case ships `fail_to_pass` /
  `pass_to_pass` test ids for objective, cheap scoring (SWE-bench-shaped), AND
  may link a UserStory so criteria give partial credit and human-readable
  grouping. The oracle decides *solved*; the criteria explain *what was solved*.
- **Corpus: ingested repos pinned per case.** Fixture repos live in the internal
  git server; each case pins a `base_commit_sha`. Hermetic, offline-capable,
  versioned with the platform, and reuses ingest + population unchanged.
- **The independent variable is the STRATEGY, not the model** (owner,
  2026-08-29). The thesis under test is "these ways of building software with AI
  work better", so trials vary the *loop shape* and treat models as a resource a
  strategy allocates. Models remain a covariate: repeat the comparison across
  model mixes to show an effect is not one model's artifact.
- **A strategy is an arbitrary graph of activity, expressed as data.** Not an
  enum of blessed loops — a `StrategyTemplate` is a pipeline graph (steps, edges,
  fan-out/join, conditions) + a role->model assignment + a loop policy. The
  platform already executes exactly this shape, so adding a strategy is authoring
  data, not code. The catalog is open-ended; representative members:
  - **one-shot** — a single agent step, the naive baseline every claim is measured against
  - **test-first** — write oracle-shaped tests, then implement against them
  - **adversarial** — implement -> fan out N independent reviewers -> join -> fix
  - **planner/executor fan-out** — a high-end model writes instructions, K small
    models execute them in parallel, an integrator merges (the owner's hypothesis:
    buy latency and cost efficiency by spending a little intelligence up front)
  - **gated** — any of the above, plus a real CI gate that must pass before done
  - and combinations, since these compose as graphs rather than as modes
- **Loop driver: the LazyAF pipeline loop.** Each iteration is a real pipeline
  run of the strategy graph — visible, costed, diffable. *Implementation note:*
  the v2 graph is a DAG (`entry_points` + all-upstream-satisfied traversal), so
  iteration is NOT a cycle in the graph: a **Trial orchestrator drives N
  sequential pipeline runs**, one per iteration, feeding the previous iteration's
  failures forward. Fan-out *within* an iteration is native graph behavior and
  needs no new engine work.
- **Cost sources: CLI-reported + GPU-node model.** See `StepUsage`.
- **Audience: a public write-up backed by a reproducible bundle** (owner,
  2026-08-29). Anyone should be able to download the corpus + results and re-run
  them. This makes provenance (image hashes, harness version, model version,
  policy) a hard requirement rather than nice-to-have, and it puts fixture
  LICENSING in scope — the bundle ships git bundles for cases whose license
  permits redistribution and fetch-instructions + a patch for the rest.
- **Contamination: public repos, noted** (owner). Fixtures come from real public
  repos for realism; every case carries `contamination_risk` and the caveat is
  disclosed in the write-up and the bundle. Deliberate trade: realism now,
  honesty about the threat to validity, and the door stays open to adding
  low-risk cases later (the field already distinguishes them).
- **Variance is a platform feature, not an analysis afterthought** (owner). Every
  reported figure is over N repeats with its distribution; the board flags
  comparisons whose intervals overlap instead of ranking noise. `repeat` is
  already an axis of `Experiment.matrix` — the work is aggregation + honest
  presentation, not new execution machinery.
- **Headline metrics** (owner): (1) **cost-to-solve** — median $ per solved case,
  the one number that answers "what did the solution actually cost"; (2)
  **regression rate** — how often a loop breaks `pass_to_pass` while fixing the
  target, i.e. whether you could trust it in real CI; (3) **iterations-to-solve
  distribution** — whether a loop converges or thrashes, and whether late
  iterations ever earn their keep. Solve-rate-at-fixed-budget stays available as
  a normalization but is not the headline.
- **Added for graph strategies:** because strategies differ in *shape*, the board
  also reports **wall-clock-to-solve** and **speedup** (serial-equivalent /
  wall-clock) — a fan-out that costs 3x but finishes in a third of the time is a
  different trade, not a worse result — plus **integration conflict rate** for
  parallel strategies and **cost-by-role**, which is what actually tests the
  "expensive planner, cheap workers" hypothesis.
- **Fairness across differently-shaped strategies** comes from holding the case,
  base commit, oracle, and BUDGET constant: given the same dollars and the same
  wall-clock ceiling, which strategy solves more? That is why fixed-budget
  solve-rate stays in the board even though cost-to-solve is the headline —
  without a shared cap, an unbounded strategy can always "win" by spending more.

### Controls (a benchmark without them proves nothing)

- **Base-state control**: at `base_commit_sha` every `fail_to_pass` test MUST
  fail and every `pass_to_pass` MUST pass — a case whose oracle is already green
  is broken, and `lazyaf bench validate` refuses it (Phase 13.1).
- **Null-agent control**: a trial variant that changes nothing must score 0%
  solved. If it ever scores above zero, the oracle is measuring something other
  than the fix.
- **Determinism disclosure**: whatever the provider exposes (temperature, seed)
  is recorded per trial, so "we could not pin this" is stated rather than hidden.

### Detailed specifications

The implementable detail lives in `docs/milestone-13/` (PLAN.md keeps the shape
and the decisions; the companion docs keep the 3,000 lines an implementer needs,
the same split `historical-documents/` uses for completed phases):

| Document | What it pins |
|---|---|
| `docs/milestone-13/strategy-catalog.md` | The StrategyTemplate graph contract (reserved `lazyaf_*` keys, the six-pass `expand_strategy_graph`, strict two-way role-binding, K-parameterization) and a 7-entry catalog with REAL v2 graph JSON: one-shot, test-first, adversarial-review, planner-fanout (expanded at K=4), planner-fanout-resolver, one-shot-gated, composed-full. Plus the integration-policy x on-conflict matrix, including the cells that are rejected as incoherent. |
| `docs/milestone-13/api-surface.md` | Every endpoint (`/api/bench/*`), the full `POST /api/steps/{id}/usage` contract for Phase 12.5, how the benchmark layer joins the spec layer without double-counting criterion history in the dogfood corpus, MCP tools, `lazyaf bench` CLI, and the indexes the read-heavy board queries need. |
| `docs/milestone-13/phase-specs-and-metrics.md` | Phase deliverables 13.1-13.5 with contract test files and Definition-of-Done checklists; the metrics defined mathematically; the variance/separability rules; and the `METHOD.md` template that ships in every exported bundle. |

**The one thing from those docs that belongs in the plan itself** — because it
governs how every number is allowed to be stated:

> **Cost-to-solve is censored data.** A trial stopped by the budget cap is not a
> missing observation and not infinite cost; it is the statement *"more than what
> was spent"*. So the board never prints a lone median. It prints three numbers
> together: the **paired median** over the case set every compared variant solved
> (difficulty held constant; `INSUFFICIENT` under 5 shared cases), the
> **amortized cost per solve** over ALL trials including failures (what it
> actually costs a user to get one working change), and the **Kaplan-Meier
> censored p50** — which, when survival never reaches 0.5, renders
> `> $X.XX (only N% solved)` rather than inventing a median. If the paired and
> amortized numbers rank differently, the board says so in words instead of
> picking a winner: that variant is cheap when it works and expensive when it
> does not, and that is the finding.

### Phase 13.1 — Corpus & fixtures
`BenchmarkSuite` / `BenchmarkCase` models + CRUD + a `lazyaf bench` CLI to author
cases from a real repo state. A case-validation command that proves a case is
well-formed: at `base_commit_sha` every `fail_to_pass` test FAILS and every
`pass_to_pass` test PASSES (a case whose oracle is already green is broken).
Seed a starter suite spanning verticals x complexity.
   EXIT GATE: `lazyaf bench validate <suite>` green on the starter suite;
   validation catches a deliberately-miswired case.

### Phase 13.2 — Trial orchestrator & loop policy
`StrategyTemplate` + `Trial` / `TrialIteration` + the orchestrator: bind roles to
models, reset to `base_commit_sha` on a fresh branch, run iteration pipelines of
the strategy graph until solved / max_iterations / budget_usd exhausted,
recording per-iteration cost (by role), diff churn, and oracle progress. Budget
enforcement is hard (a trial cannot outspend its cap) and applies across the
whole fan-out, not per worker. Parallel templates allocate **a branch and a workspace per
worker** off the case's base commit and rejoin through the platform's existing
merge/rebase/resolve-conflicts machinery; the template's `integration.policy`
decides how, and `on_conflict` may hand the structured conflict to a resolver
agent. Conflicts, resolutions and integration cost are recorded as trial
outcomes rather than silently dropped.
   EXIT GATE: a mock-model trial on a starter case solves at a known iteration
   with a complete per-iteration cost curve; a deliberately-unsolvable case
   terminates at budget_exhausted without overspending.

### Phase 13.3 — Strategy experiments & the effectiveness board
Extend `Experiment` so a matrix axis is **strategy_template x model_assignment x
repeat** over a suite of cases. Aggregate per strategy, per vertical, per
complexity: solve-rate at a shared budget, median cost-to-solve, cost-by-role,
wall-clock and speedup, regression rate, integration conflict rate, and the
iterations-to-solve distribution. This is the board that answers the actual
question — *which way of working with AI is effective, and what does it cost* —
rather than which vendor's model scores highest.
   EXIT GATE: a 3-strategy matrix (one-shot / adversarial / planner-fanout) over
   a 3-case suite, repeated, produces a board where the SAME case is comparable
   across strategy shapes on cost, wall-clock and regression rate — and the
   one-shot baseline is present in every comparison as the control.

### Phase 13.4 — Variance, controls & the "real or noise" question
Aggregation over N repeats with distributions (median + spread, not means alone);
the board refuses to rank variants whose intervals overlap and says so. Null-agent
and base-state controls run as first-class trial variants. Per-vertical and
per-complexity breakdowns; split-by-`contamination_risk` views so a skeptic can
ask "does the gap survive on low-risk cases?" and get an answer.
   EXIT GATE: a 3-repeat matrix reports distributions, and a deliberately
   noise-level difference is flagged as not-separable rather than ranked.

### Phase 13.5 — The reproducible bundle (publishability)
`lazyaf bench export <suite> --with-results` produces a portable bundle:
corpus (git bundles where licensing permits, fetch-instructions + patch where it
does not), case metadata incl. oracle ids and contamination tags, all trials with
their full provenance block, and a `METHOD.md` stating what was measured, the
controls, the caveats, and the exact commands to re-run it. `lazyaf bench import`
round-trips it, and re-running a bundle on the same harness version reproduces
the case set exactly (results within variance, which is the honest claim).
   EXIT GATE: export -> import on a clean checkout reconstructs the suite and
   replays a trial; the bundle's stated re-run command works verbatim; a bundle
   whose harness/image hashes differ from the current tree says so loudly instead
   of silently comparing apples to oranges.

### Open questions for Milestone 13

- **How many repeats** buy enough signal at what cost? (N is a dial; the answer is
  empirical — measure spread on the starter suite before fixing a default.)
- **Network access during a trial?** Dependency installs say yes; reproducibility
  and cost control say pin a proxy/cache. Leaning: allow, but record it as
  provenance and offer a cached-only mode for published runs.
- **Partial credit** (`fail_to_pass` 3/5) recorded but not headline — the board
  needs one honest number and "solved" is binary. Revisit if partial progress
  turns out to discriminate between loops that binary solve-rate cannot.
- **Licensing per fixture** now that the bundle is public: the export must decide
  per case what it may redistribute (hence `license` on `BenchmarkCase`).
- **How do K workers avoid duplicating each other's work?** The planner's
  instructions are the coordination mechanism; if they partition badly, fan-out
  degrades into K agents solving the same subproblem. Worth measuring directly
  (overlap in files touched across workers) rather than assuming.
- **Does the integrator need to be a strong model?** Cheap planning + cheap
  execution + expensive integration may be the real shape. `cost_by_role` is
  designed to answer this empirically.
- **Which integration policy wins?** Sequential merge is simplest; rebase keeps
  history linear but re-runs conflicts per worker; agent-composed integration
  could dodge conflicts entirely by having one model read K branches and write
  the union. This is a strategy axis, so the harness answers it rather than the
  architecture assuming it.
- **Is conflict rate a function of the PLANNER's quality?** A good instruction
  split should produce near-disjoint diffs. If conflict rate correlates with
  planner model strength, that is a strong, publishable result about where to
  spend intelligence in a fan-out.
- **Fan-out ceiling:** at what K does added parallelism stop helping (or start
  hurting via conflicts)? A sweep over K on one case is a cheap early experiment
  and probably the first genuinely publishable result this harness can produce.

---

## Milestone 14 — Self-Hosted & OpenAI-Compatible Model Endpoints

> **STATUS: 14.1-14.3 COMPLETE, landed 2026-08-30** in commit `4b429c6` (56
> files, ~21,500 lines). `ModelEndpoint` + migration `0011_model_endpoints.py`;
> the capability probe (`backend/app/services/model_endpoints/probe.py`); the
> agent harness with a tool-calling loop and a text-fence fallback
> (`runner-common/runner_common/harness/`); a stdlib mock OpenAI server so CI
> needs no GPU (`tdd/shared/mock_openai/`); the Endpoints UI, store and
> Playwright spec. Verified against this host's real ollama: `llama3.1:8b`
> probed `ok` with tools, streaming and usage all true and a context window of
> `131072` discovered via ollama's `/api/show`.
>
> **14.4 ("prove it" — a dogfood step against a real self-hosted endpoint, and
> the fan-out hypothesis measured) is NOT COMPLETE**, because its second half
> depends on Milestone 13, which has not started. Test notes and the manual
> verification transcript: `upcoming/m14-testing.md`.
>
> **14.5 is DESIGNED ONLY — zero implementation.** No `vllm` or `ollama`
> appears anywhere under `images/` or `scripts/`, and no GPU-yield or drain
> mechanism exists in the tree. Wiring doc:
> `upcoming/wave9-145-runner-images.md`.
>
> Milestone 14 sits OUTSIDE the 12.x sequence.


> **The ask (owner, 2026-08-30):** run agents against models we host ourselves —
> ollama and vLLM on bare metal at home, or on runpod.io — so "all sorts of AI
> models" can be in the mix. Push-style: LazyAF reaches out when it wants work
> done, never a long-poll loop.
>
> **Sequencing:** independently valuable for the product (cheap local models
> doing routine work), AND a hard prerequisite for Milestone 13's central
> hypothesis — "a high-end model writes instructions, K cheap models execute in
> parallel" is unmeasurable without cheap models. It can be built in parallel
> with 13.1/13.2; 13.3's headline experiment depends on it.

### The thing that is actually hard

The transport is easy and already push: LazyAF is the HTTP client, so calling
`/v1/chat/completions` involves no polling by anyone. **The hard part is that
ollama and vLLM are inference servers, not agents.** Claude Code and the Gemini
CLI ship their own agent loop — read files, edit, run commands, iterate until
done. A raw OpenAI-compatible endpoint gives you completions and nothing else.
So LazyAF has to supply the loop.

### Decisions (owner, 2026-08-30)

- **We own the loop.** A minimal tool-calling harness in `runner-common`
  (read/write/list files, run shell, apply patches, stop conditions) running
  inside the existing control-mode container, as a new entry in the `EXECUTORS`
  registry beside claude/gemini/mock. Wrapping an existing OSS agent CLI was
  rejected for a specific reason: Milestone 13 makes *loop shape* the
  independent variable, and a loop we do not own is one we cannot vary.
- **All three reachability modes**, because the deployments genuinely differ:
  | Mode | For | How |
  |---|---|---|
  | `runner-local` | Home bare metal behind NAT | A 12.6 runner agent runs on the box hosting ollama; the backend pushes the step there over WS and the step container calls `localhost`. Zero inbound connectivity, no tunnel. |
  | `direct` (default) | runpod, any routable endpoint | The step container calls the endpoint URL itself. |
  | `proxy` | Central auth/logging | The backend brokers the call. Convenient, but it puts inference traffic through the backend and makes it a bottleneck — opt in per endpoint, never the default. |
- **Capability is probed and recorded, not assumed.** Tool-calling support is
  inconsistent across self-hosted models (ollama supports it for some; vLLM
  depends on model plus chat template). Probe at registration, store the result
  on the endpoint, and let the strategy decide — which turns "does tool support
  matter?" from an assumption into something Milestone 13 can measure.
- **Cost: tokens always, dollars when known.** Token counts come from the
  OpenAI-compatible `usage` field. An endpoint may carry a $/hour rate (a
  runpod pod rate, or an estimate for home hardware); when set, cost is
  rate x wall-clock occupancy recorded as `cost_source="gpu-node"`. When unset,
  tokens are recorded and cost stays null, and the board must show WHICH trials
  have real cost data rather than silently mixing them.

### What already anticipates this

`StepUsage.provider` already includes `openai-compatible` and `self-hosted`;
`cost_source` already includes `gpu-node`; and 12.6 deliberately kept the
runner-agent's executor seam Docker-agnostic and pluggable for exactly this.
The work is a new executor plus an endpoint registry, not a re-architecture.

### Phases

- **14.1 — Endpoint registry.** A `ModelEndpoint` entity (name, base_url, model,
  auth style + secret reference, reach mode, optional $/hour, probed
  capabilities), CRUD, a health/capability probe, and secret handling that
  reuses the 12.5 `secret_environment` path so a key never reaches
  `docker inspect`. Endpoints with no auth (typical LAN ollama) are first-class.
- **14.2 — The agent harness.** The tool-calling loop in `runner-common`, with a
  no-tools fallback for models that cannot tool-call, bounded iterations and
  budget, and usage reporting through the existing sidecar so self-hosted runs
  land in `StepUsage` like every other step.
- **14.3 — UI category.** A Model Endpoints surface (register, probe, health,
  capabilities, rate) and endpoint selection wherever an agent is chosen: agent
  step config, card creation, playground, and the 12.6.5 experiment matrix — the
  last one is what lets a matrix mix API and self-hosted models in one run.
- **14.4 — Prove it.** A dogfood step running against a real self-hosted
  endpoint, and the Milestone 13 fan-out hypothesis finally runnable:
  expensive planner, K cheap local workers, measured.

### Phase 14.5 — Runner images with inference baked in

> **The ask (owner, 2026-08-30):** "images available as runners with vllm and
> llama baked in so that you mount your data / cache of models and just go."
> Chosen shape: the COMBINED runner+inference image (not a local compose
> profile). Servers: **vLLM** and **ollama**.

One image = one deployable node. `FROM` the upstream inference server, with the
12.6 runner agent layered on top. You give a pod the model cache, a LazyAF
server URL and a runner token; it starts the inference server, dials OUT over
WebSocket, and its steps call the model on `localhost`. **Zero inbound
connectivity** - which is what makes it work on runpod and behind home NAT
alike, and why this is a runner image rather than a model image.

- `lazyaf-runner-ollama` FROM `ollama/ollama` - mount `~/.ollama`; ollama pulls
  models itself, so "mount your data and go" is literally true.
- `lazyaf-runner-vllm` FROM `vllm/vllm-openai` - mount the HF cache; the
  throughput option and the right one on a rented GPU.

**We do NOT rebuild the inference servers.** Both upstreams publish official
images; forking them means multi-GB pushes on every release plus tracking
CUDA/torch/vLLM compatibility forever. We add a thin layer - the runner agent,
an entrypoint that supervises two processes, and the endpoint declaration - and
inherit their release engineering.

**Windows desktops with idle RTX cards (owner, 2026-08-30).** The target is a
Windows box running Docker Desktop on the WSL2 backend, using the SAME image as
runpod with `--gpus all` (NVIDIA's CUDA-on-WSL driver makes this work), so there
is nothing Windows-specific to maintain. Two decisions attach to it:

- **Dual role, selected per endpoint.** The box can serve models AND execute
  steps; the difference is advertised as labels, not as two deployment shapes.
- **Yield on GPU busy.** LazyAF must not fight the owner for his own GPU. This
  is genuinely new mechanism: the node samples GPU utilization and DRAINS -
  stops accepting new assignments while letting an in-flight step finish - then
  resumes, with hysteresis so a transient spike cannot flap it. The naive
  version (disconnect when busy) is wrong precisely because 14.5 establishes
  that the connection IS the advertisement, so disconnecting would orphan
  running work. Expect a small backend availability flag; silently dropping the
  connection is not acceptable. The UI must show WHY a node is not taking work,
  and a manual force-drain / force-available override is required.

Design points the wiring doc must settle:
- **Two processes, one container.** The entrypoint starts the inference server,
  waits for it to be healthy, then starts the runner agent, and propagates
  signals and exit codes so a dead server does not leave a live runner
  advertising a model that is gone.
- **How the pod's endpoint gets registered.** 14.1 already has `runner-local`
  reach mode plus `requires: {has: ["endpoint:<name>"]}` label matching, so the
  minimum is: the operator registers the endpoint once and the pod advertises
  the matching label from env. Whether the pod may ALSO self-register through
  the API is a real decision - it is the difference between "just go" and a
  node being able to write rows in your control plane.
- **LazyAF builds the heavy images itself** (owner, 2026-08-30). A repo-defined
  pipeline with docker steps builds and pushes the two runner images on a LazyAF
  runner agent, not on GitHub compute. This is dogfooding at the top of the
  stack - the platform ships its own artifacts - and it also sidesteps a real
  problem: a ~45GB vLLM build does not fit a standard GitHub runner, and a
  self-hosted Actions runner on a PUBLIC repo would expose the owner's hardware
  to fork PRs. GitHub keeps the wheel, the small service images, the existing
  step images, secret-scan and release-please; `pr-build.yml` stays
  GitHub-hosted for exactly that reason. GHCR credentials reach the build step
  through 12.5 `secret_environment`, so they never appear in `docker inspect`.
- **Image size is a release problem, not a detail.** A CUDA vLLM image is ~10GB.
  These must not ride the normal per-tag image matrix; they need their own
  trigger, their own cadence, and a documented "build it yourself" path.
- **GPU passthrough** differs between runpod (automatic) and local
  `docker run --gpus`; the docs must not assume either.

### Open questions

- Which model families are worth pinning as known-good in the docs, and do we
  ship a capability matrix or let the probe speak for itself?
- Context-window limits vary wildly on local models; does the harness truncate,
  summarize, or refuse when a repo context exceeds them?
- Concurrency: one local GPU serving K parallel fan-out workers will queue.
  Does the endpoint carry a max-concurrency the scheduler respects?

---

# Reference

> Background for a reader who has not seen LazyAF before. None of this is a
> plan; it is what the system is.

## What is LazyAF?

LazyAF is a local-first CI/CD platform that integrates AI agents as first-class citizens. Instead of writing GitHub Actions YAML, you define pipelines with a mix of:

- **Agent steps**: Claude or Gemini implements features, fixes tests, reviews code
- **Script steps**: Traditional shell commands (lint, test, build)
- **Docker steps**: Commands in isolated container images

The core workflow:
1. **Ingest** a repo via CLI (`lazyaf ingest /path/to/repo`)
2. **Create cards** describing features or tasks
3. **Start work** -> Runner clones, executes, pushes to internal git server
4. **Pipeline triggers** -> Tests run, AI fixes failures, auto-merge on success
5. **Land changes** to real remote when ready (`lazyaf land`)

---

## Long-Term Vision: Specification-Driven Development

> Sprint reviews, PRs, and code review evolved to mentor humans. With LLMs in the loop, the leverage shifts from *implementation review* to *specification fidelity* — does the result match the product intent, and can we prove it repeatedly?

LazyAF is moving toward a model where humans **over-specify what the software must do** (features, user stories, acceptance criteria) and LLMs handle implementation. The platform's job is to:

1. **Capture intent** — features, user stories, and acceptance criteria live in a queryable database, not scattered across Jira/Notion/heads.
2. **Tie tests back to intent** — every test in every repo declares which acceptance criterion it covers, and every run reports back. Test history is keyed to `(criterion, commit, model, prompt)`.
3. **Run experiments, not just builds** — re-run the same card across multiple model/prompt combinations and compare pass-rates per criterion. Make model + prompt selection an evidence-driven decision.
4. **Curate context for parallel agents** — the spec DB lets the platform hand each agent the relevant slice of intent (instead of stuffing the whole codebase into a context window).
5. **Enable cross-repo features** — a "feature" can span multiple repos (frontend + backend + infra), and the platform tracks delivery across all of them.

This direction reframes LazyAF as a *platform for software science*: experiments, leaderboards, regression dashboards, and reusable prompt structures — alongside the day-to-day "active project management" view (cards, kanban, pipelines).

The spec layer is being added to Phase 12.x in parallel with the runner architecture refactor (see Phases 12.2.5, 12.2.6, 12.6.5, 12.6.6).

---

## Project Structure

```
lazyaf/
|-- backend/
|   |-- app/
|   |   |-- main.py              # FastAPI app entry point
|   |   |-- config.py            # Settings
|   |   |-- database.py          # SQLAlchemy async setup
|   |   |-- models/              # SQLAlchemy models
|   |   |-- routers/             # API endpoints
|   |   |-- services/            # Business logic
|   |   |-- schemas/             # Pydantic models
|   |   +-- mcp/                 # MCP server for Claude Desktop
|   |-- git_repos/               # Internal bare git repos
|   |-- runner/
|   |   |-- Dockerfile
|   |   +-- entrypoint.py        # Runner execution logic
|   |-- pyproject.toml
|   +-- alembic/                 # DB migrations
|-- cli/                         # LazyAF CLI tool (ingest, land)
|   |-- pyproject.toml
|   +-- lazyaf/cli.py
|-- frontend/
|   |-- src/
|   |   |-- lib/
|   |   |   |-- components/      # Svelte components
|   |   |   |-- stores/          # State management
|   |   |   +-- api/             # API client
|   |   +-- routes/              # Pages
|   |-- package.json
|   +-- vite.config.ts
|-- images/                      # Step images: base, agent-base, claude,
|                                #   gemini, test-runner, debug-sidecar
|-- runner-common/               # Shared runner library + the M14 agent harness
|-- runner-agent/                # Remote runner agent (12.6)
|-- tdd/                         # Test tiers T1/T2/T3 + floors + skip baseline
|-- .lazyaf/pipelines/           # LazyAF's own dogfood CI, synced on push
|-- historical-documents/        # Archived phase documentation
|-- upcoming/                    # Per-wave wiring docs, QA findings, post-mortems
|-- docs/milestone-13/           # Milestone 13 design (not yet implemented)
|-- docker-compose.yml
|-- PLAN.md                      # This file
+-- README.md
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | Svelte + Vite | Reactive UI, fast builds |
| Backend | FastAPI | Async Python API |
| Database | SQLite + SQLAlchemy | Simple persistence (PostgreSQL ready) |
| Queue | In-memory | Job management |
| Containers | Docker SDK for Python | Runner isolation |
| Real-time | WebSockets | Status updates |
| Git | Dulwich | Pure Python git server |
| MCP | FastMCP | Claude Desktop integration |

---

## Core Data Models

> Verified against `backend/app/models/` on 2026-08-30. Models marked
> **DESIGNED** have no table, no migration and no router — they are
> Milestone 13's shape, not the tree's.

### Repo
```python
class Repo:
    id: UUID
    name: str
    remote_url: str | None       # Real remote (GitHub/GitLab)
    default_branch: str          # e.g., "dev" or "main"
    is_ingested: bool
```

### Card
```python
class CardStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"

class Card:
    id: UUID
    repo_id: UUID
    title: str
    description: str
    status: CardStatus
    branch_name: str | None
    step_type: StepType          # agent | script | docker
    step_config: dict            # Type-specific config
```

### Pipeline
```python
class Pipeline:
    id: UUID
    repo_id: UUID
    name: str
    steps: list[PipelineStep]    # Ordered execution
    triggers: list[TriggerConfig]

class PipelineStep:
    name: str
    type: StepType               # agent | script | docker
    config: dict
    on_success: str              # "next" | "stop" | "merge:{branch}"
    on_failure: str              # "next" | "stop" | "trigger:{card_id}"
    continue_in_context: bool    # Preserve workspace
```

---

## Specification Layer Models

> Introduced in Phase 12.2.5 and **live since 2026-08-29** — `Feature`, `UserStory`, `AcceptanceCriterion` and `PromptTemplate` are in
> `backend/app/models/spec.py`, `TestRef`/`TestRun` in `models/testref.py`,
> `Experiment` in `models/experiment.py`, `StepUsage` in `models/usage.py`. These models capture *what the software must do* and let the platform measure whether AI-generated changes still satisfy intent. Hierarchy is intentionally shallow: `Feature -> UserStory -> AcceptanceCriterion`. Tests and runs are orthogonal entities that join back to criteria.

### Feature
A product capability. Cross-repo by design — a single feature can span frontend, backend, infra, etc.

```python
class Feature:
    id: UUID
    name: str
    description: str             # Markdown, free-form product narrative
    repo_ids: list[UUID]         # Repos this feature touches (one or many)
    status: FeatureStatus        # proposed | active | shipped | deprecated
    owner: str | None            # Free-form (email, handle, team name)
    created_at: datetime
    updated_at: datetime
```

### UserStory
A natural-language behavior expectation in the gherkin spirit (less rigid). Belongs to one feature.

```python
class UserStory:
    id: UUID
    feature_id: UUID
    title: str                   # "User can revoke an API key"
    persona: str | None          # "As a security-conscious admin"
    narrative: str               # Free-form: "When X, then Y, so that Z"
    repo_ids: list[UUID]         # Subset of parent feature's repos
    priority: int                # Simple integer, not story points
    status: StoryStatus          # draft | accepted | in_progress | done | blocked
    created_at: datetime
    updated_at: datetime
```

### AcceptanceCriterion
A single, testable expectation. Natural language; one or more `TestRef`s prove it.

```python
class AcceptanceCriterion:
    id: UUID
    user_story_id: UUID
    description: str             # "Revoked keys return 401 within 60s globally"
    is_required: bool            # Story-blocking vs nice-to-have
    created_at: datetime
```

### TestRef
A pointer from a test in the application repo back to one or more acceptance criteria. The application's test suite emits a manifest declaring its `lazyaf_test_id`s; the platform reconciles that manifest against TestRefs to detect drift (orphaned tests, uncovered criteria).

```python
class TestRef:
    id: UUID                     # Stable platform-side ID
    lazyaf_test_id: str          # Stable repo-side identifier (decorator/sidecar)
    repo_id: UUID
    file_path: str               # e.g., "tests/api/test_keys.py"
    test_name: str               # e.g., "test_revoked_key_returns_401"
    framework: str               # "pytest" | "vitest" | "go-test" | "custom"
    criterion_ids: list[UUID]    # Many-to-many with AcceptanceCriterion
    last_seen_commit: str | None # SHA of latest commit where test was observed
    is_orphaned: bool            # True if test_id no longer found in repo
```

### TestRun
The result of executing one TestRef. Joined to commit, and (when run inside an experiment) to model + prompt.

```python
class TestRun:
    id: UUID
    test_ref_id: UUID
    pipeline_run_id: UUID | None # Pipeline that produced this run
    step_execution_id: UUID | None
    commit_sha: str
    repo_id: UUID
    status: TestStatus           # passed | failed | skipped | error
    duration_ms: int
    output: str | None           # Truncated stdout/stderr or pointer to artifact
    model: str | None            # e.g., "claude-opus-4-7" - set inside experiments
    prompt_template_id: UUID | None
    prompt_version: int | None
    experiment_id: UUID | None
    created_at: datetime
```

### Experiment
A user-defined run that evaluates one or more (model, prompt) tuples against a card / story / feature. Produces TestRuns tagged with the matrix coordinates so leaderboards can aggregate.

```python
class Experiment:
    id: UUID
    name: str
    description: str
    target_type: str             # "card" | "user_story" | "feature"
    target_id: UUID
    matrix: dict                 # {"models": [...], "prompts": [...], "repeat": N}
    status: ExperimentStatus     # draft | running | complete | aborted
    created_by: str
    created_at: datetime
    completed_at: datetime | None
```

### PromptTemplate
A versioned, reusable prompt. Leaderboards rank `(template_id, version, model)` by pass-rate per criterion.

```python
class PromptTemplate:
    id: UUID
    name: str
    purpose: str                 # "implement-from-story" | "fix-failing-test" | etc.
    versions: list[PromptVersion]

class PromptVersion:
    id: UUID
    template_id: UUID
    version: int
    body: str                    # The prompt itself, with placeholders
    placeholders: list[str]      # e.g., ["{story_narrative}", "{failing_test_output}"]
    created_at: datetime
    notes: str | None
```

### StepUsage  *(Phase 12.5 — effort telemetry)*
Per-agent-step resource accounting. Without this, `TestRun` records *what happened*
and nothing records *what it cost* — and the Benchmark harness (Milestone 13) has no
effort axis. It rides the control-layer protocol as a fourth channel alongside
status / logs / test-results, so it must land WITH 12.5's agent-step migration
rather than after it (12.2.6 became a retrofit precisely because 12.3 froze first).

```python
class StepUsage:
    id: UUID
    step_execution_id: UUID
    step_run_id: UUID | None
    provider: str                # "anthropic" | "google" | "openai-compatible" | "self-hosted"
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: Decimal | None     # CLI-reported where available, else derived
    cost_source: str             # "cli-reported" | "gpu-node" | "estimated" | "unknown"
    wall_clock_ms: int
    container_seconds: float | None
    created_at: datetime
```

> **Cost sources (owner decision 2026-08-29):** the agent CLIs report their own
> token counts and dollar cost — the control runtime scrapes that at step end
> (`cost_source="cli-reported"`). Self-hosted / runpod-style nodes have no
> per-token bill, so their dollars come from a node-rate x occupancy model
> (`cost_source="gpu-node"`). Both land on one comparable USD axis; no separate
> pricing table is needed while the CLIs keep reporting cost.

### BenchmarkSuite / BenchmarkCase  *(Milestone 13 — DESIGNED, not built)*
A corpus of repos pinned at known states, each with a task and a definition of
"solved". Cases are the fixtures a loop is benchmarked against.

```python
class BenchmarkSuite:
    id: UUID
    name: str                    # "core-v1"
    description: str
    tags: list[str]              # verticals covered

class BenchmarkCase:
    id: UUID
    suite_id: UUID
    slug: str                    # "flask-api.missing-pagination"
    repo_id: UUID                # an INGESTED fixture repo (internal git server)
    base_commit_sha: str         # every trial starts here, byte-identical
    task_statement: str          # what the agent is told to do
    vertical: str                # "web-api" | "data-pipeline" | "frontend" | "cli" | ...
    complexity: str              # "trivial" | "small" | "medium" | "large"
    fail_to_pass: list[str]      # lazyaf_test_ids that MUST flip red -> green
    pass_to_pass: list[str]      # lazyaf_test_ids that must STAY green (regression guard)
    user_story_id: UUID | None   # layered criteria: the human-meaningful "why"
    loop_defaults: dict          # {max_iterations, budget_usd, per_step_timeout}
    contamination_risk: str      # "high" (public repo, likely in training data)
                                 # | "medium" | "low" (self-authored / post-cutoff)
    source_url: str | None       # upstream provenance for public fixtures
    license: str | None          # SPDX id - decides what the public bundle may ship
    test_command: str            # the PINNED oracle invocation, e.g. "pytest -q"
    oracle_file_hashes: dict     # {path: sha256} for every file carrying an oracle
                                 # id - an agent that edits the oracle to pass is
                                 # cheating, and this is how the trial detects it
    quarantined_tests: list[str] # ids ejected by the flake screen, kept on the record
    reference_patch: str | None  # gold patch where upstream has one -> enables the
                                 # "is this case solvable at all" control
    solvable_verified: bool      # the gold-patch control passed
    created_at: datetime
```

### StrategyTemplate  *(Milestone 13 — the independent variable; DESIGNED, not built)*
A strategy is a graph of activity plus how models are assigned to its roles. It
is DATA: authoring a new strategy means writing a template, not changing code.

```python
class StrategyTemplate:
    id: UUID
    slug: str                    # "planner-fanout-8" | "adversarial-3" | "one-shot"
    description: str
    graph: dict                  # a v2 pipeline graph: steps + edges + fan-out/join.
                                 # Step configs carry ROLE placeholders, not models:
                                 #   {"role": "planner"} / {"role": "worker", "fanout": 8}
    roles: list[str]             # ["planner", "worker", "integrator", "reviewer"]
    loop_policy: dict            # {max_iterations, budget_usd, stop_on}
    parallelism: dict            # {max_concurrent_workers, branch_per_worker: bool}
    variables: dict              # {"K": {"type":"int","default":4,"min":1,"max":32}} -
                                 # what makes planner-fanout-4 and -16 the SAME
                                 # template, so a K-sweep is one template not sixteen
    integration: dict            # HOW parallel work rejoins - itself under test:
                                 # {"policy": "sequential-merge" | "rebase-onto-trunk"
                                 #            | "cherry-pick" | "agent-composed",
                                 #  "on_conflict": "fail" | "resolver-agent" | "human"}
    created_at: datetime
```

> **Why roles, not models:** the owner's leading hypothesis is that a high-end
> model writing instructions for a fan-out of small models beats one big model
> doing everything. That strategy is only expressible if a template says "planner"
> and "worker" and the *trial* binds those roles to concrete models — otherwise
> every model mix is a different template and nothing is comparable.

> **Parallelism is git-native — LazyAF is the bridge.** K workers do not fight
> over one checkout: each gets its own workspace cloned at the case's base commit
> on **its own branch**, works freely, and commits. Integration is then a *git
> merge*, not a file-level reconciliation — which is precisely the substrate this
> platform already is:
>
> | Needed for fan-out | Already shipped |
> |---|---|
> | Per-worker isolation | Internal git server (bare repo per project) + workspace-per-clone at a pinned commit |
> | Independent work | Branch-per-unit-of-work, the model cards have used since Phase 2 |
> | Integration | `git_server.merge_branch` / `rebase_branch` |
> | Conflict handling | `POST /api/cards/{id}/resolve-conflicts` returns STRUCTURED conflicts and accepts resolved contents; `ConflictResolver.svelte` is the human path |
> | Review before merge | The existing approve/reject diff flow |
>
> Two consequences. First, fan-out needs far less new machinery than a
> from-scratch harness would: the orchestrator allocates branches and calls
> merges the platform already performs. Second — and more interesting —
> **conflict resolution is itself an agent-addressable task**, because conflicts
> come back as structured data rather than as a wall of `<<<<<<<` markers. A
> strategy can legitimately say "on conflict, spawn a resolver agent", which is a
> strategy variant nobody can benchmark on a single-sandbox harness.
>
> So **integration policy becomes a measured variable, not a fixed detail**:
> sequential merge, rebase-onto-trunk, cherry-pick, agent-composed integration,
> resolver-on-conflict. Integration conflict rate and resolution cost are
> outcomes of the strategy under test — plausibly the dominant cost of aggressive
> parallelism, and exactly the number that decides whether the pattern is worth
> it.

### Trial / TrialIteration  *(Milestone 13 — DESIGNED, not built)*
A Trial is one loop run of one case under one (model, prompt, policy) variant.
TrialIteration is the per-cycle record — the cost *curve*, which is the actual
science: not just "did it solve it" but "was iteration 4 worth paying for".

```python
class Trial:
    id: UUID
    experiment_id: UUID | None   # set when part of a matrix fan-out
    benchmark_case_id: UUID
    strategy_template_id: UUID   # THE independent variable
    model_assignment: dict       # {"planner": "claude-opus-5", "worker": "haiku-4.5",
                                 #  "integrator": "claude-sonnet-5"} - a strategy may
                                 # use several models in different roles, so cost is
                                 # attributed PER ROLE from StepUsage, never per trial
    prompt_template_id: UUID | None
    prompt_version: int | None
    loop_policy: dict            # {max_iterations, budget_usd, stop_on}
    status: str                  # running | solved | failed | budget_exhausted | error
    template_variables: dict     # {"K": 16} - a trial that does not record the K it
                                 # ran at cannot be reproduced
    target_met: bool             # all fail_to_pass green at the final commit
    clean: bool                  # zero pass_to_pass broken at the final commit
                                 # solved == target_met AND clean. Storing the halves
                                 # separately is the only way to compute a regression
                                 # rate that is not definitionally zero.
    solved_at_iteration: int | None   # None = never solved (a CENSORED observation,
                                 # not a missing one - see the metrics spec)
    budget_overrun_usd: Decimal  # spend already in flight when the cap hit; recorded
                                 # rather than hidden
    queued_ms: int               # excluded from wall_clock, never silently folded in
    machine_profile: str         # "local-16c-64g" | "runpod-a100" | ... - a speedup
                                 # number is meaningless without the host it ran on
    host_concurrency_limit: int  # what the fan-out was actually ALLOWED to run, which
                                 # is not always the K it asked for
    error_class: str | None      # "infra" | "provider" | "oracle_tampered" |
                                 # "base_state_invalid" - trials that failed for
                                 # reasons that are not the strategy's fault must be
                                 # excludable from denominators, and visibly so
    iterations_used: int
    total_cost_usd: Decimal
    cost_by_role: dict           # {"planner": 0.42, "worker": 1.10, ...}
    total_input_tokens: int
    total_output_tokens: int
    wall_clock_ms: int           # co-headline: parallelism buys latency with money,
                                 # so a cost-only board would rank fan-out as worse
                                 # while hiding the entire point of it
    serial_equivalent_ms: int | None  # summed step time; wall_clock/serial = speedup
    integration_conflicts: int   # merges that did not apply cleanly
    conflicts_resolved: int      # of those, how many a resolver agent/human fixed
    integration_cost_usd: Decimal  # what rejoining the work cost - the tax on
                                 # parallelism, and the number that decides whether
                                 # fan-out actually pays
    base_commit_sha: str
    final_commit_sha: str | None
    branch: str
    # --- provenance: what makes this number falsifiable by someone else ---
    harness_version: str         # git describe of LazyAF at trial time
    image_hashes: dict           # {"lazyaf-base": "1f9bff1a6d1e", ...} - already
                                 # stamped as content-hash labels by build_images.py
    model_version: str | None    # the provider's exact version, not just the family
    determinism: dict            # {temperature, seed, top_p} where exposed
    suite_version: str           # corpus revision the case came from
    created_at: datetime
    completed_at: datetime | None

class TrialIteration:
    id: UUID
    trial_id: UUID
    iteration_index: int
    pipeline_run_id: UUID        # each iteration IS a visible pipeline run
    commit_sha: str | None       # what the agent produced this cycle
    lines_added: int
    lines_removed: int
    files_touched: int
    fail_to_pass_passed: int
    fail_to_pass_total: int
    pass_to_pass_broken: int     # regressions this iteration introduced
    criteria_verified: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    duration_ms: int
    created_at: datetime
```

### Card ↔ Spec Links  *(partly built)*

The existing `Card` model gains optional links into the spec layer. Cards are still the active unit of work; the spec layer is the meta layer of *why*.

```python
class Card:
    # ... existing fields ...
    feature_id: UUID | None          # If this card delivers part of a feature
    user_story_id: UUID | None       # If this card delivers a story
```

`feature_id` and `user_story_id` are live on `backend/app/models/card.py:54-55`.
The promote-to-feature *workflow* is referenced in `routers/spec.py:164` but the
`promotes_to_feature` flag described in earlier drafts of this plan does not exist
on the model — it was never built, and has been dropped from the sketch above.

A card with neither link is fine — it's a pure work item (e.g., a bug fix, a chore). When work outgrows a card, the user can promote it to a `Feature` and the card becomes the first child story.

---

## API Summary

Route names below were read off `backend/app/routers/` on 2026-08-30. This is a
map, not the contract — FastAPI's own `/docs` is authoritative.

**Core**

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/repos`, `POST /api/repos/ingest` | Repo management and ingest |
| `GET/POST /api/repos/{id}/cards` | Card CRUD |
| `POST /api/cards/{id}/start` · `/retry` · `/approve` · `/reject` | Card lifecycle (state-guarded, atomic claim) |
| `POST /api/cards/{id}/rebase` · `/resolve-conflicts` · `/resolve-rebase-conflicts` | Merge and rebase machinery |
| `GET /api/cards/{id}/diff` | Review diff |
| `GET/POST /api/pipelines`, `POST /api/pipelines/{id}/run` | Pipeline CRUD and ad-hoc run |
| `GET /api/pipeline-runs`, `GET /api/pipeline-runs/{id}` | Run status |
| `GET /api/pipeline-runs/{id}/steps/{index}/logs` | Per-step logs (use this, not the run list — see ledger T5) |
| `GET /api/pipeline-runs/{id}/usage` | Usage rollup for a run (12.5) |
| `GET /api/pipelines/{id}/export/yaml` | YAML export (lossy for graphs — see ledger T18) |
| `GET/POST /api/repos/{id}/lazyaf/pipelines` · `/agents` | The repo-YAML definition door |
| `/git/{id}.git/*` | Internal git server (`git-receive-pack` is the push trigger) |
| `/ws` | WebSocket for real-time UI updates |

**Machine surfaces (authenticated — the only ones that are)**

| Endpoint | Purpose |
|----------|---------|
| `POST /api/steps/{id}/status` · `/logs` · `/heartbeat` | Control-layer reporting from inside a step container (12.3), authenticated by a per-step JWT |
| `POST /api/steps/{id}/test-results` | Test-manifest delivery (12.2.6) — this is the ingest path; there is **no** `/api/test-results/ingest` |
| `POST /api/steps/{id}/usage` | Effort telemetry -> `StepUsage` (12.5) |
| `WS /ws/runner` | Runner-agent protocol (12.6), authenticated by a runner JWT |

**Specification and evaluation layer — LIVE (12.2.5, 12.2.6, 12.6.5, 12.6.6)**

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/features` (+ `PATCH`/`DELETE`) | Feature CRUD (cross-repo) |
| `GET/POST /api/features/{id}/stories`, `/api/user-stories` | User story CRUD |
| `GET/POST /api/criteria` | Acceptance criterion CRUD |
| `GET /api/test-refs`, `POST /api/test-refs/reconcile` | Test reference registry and reconciliation |
| `GET /api/criteria/{id}/history` | Pass/fail history per (model, prompt) |
| `GET/POST /api/experiments`, `POST /api/experiments/{id}/abort` · `/resume` | Experiment CRUD and launch |
| `GET /api/experiments/{id}/estimate` | Dry-run cost / run-count estimate before launch |
| `GET /api/experiments/{id}/leaderboard`, `GET /api/leaderboards/feature/{id}` | Aggregated pass-rate per (prompt, model) |
| `GET/POST /api/prompt-templates` | Prompt template + version CRUD |
| `GET /api/cards/{id}/spec-context` | The curated per-card context bundle (12.6.6) |

**Model endpoints (Milestone 14) and debug (12.7)**

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/model-endpoints`, `POST /api/model-endpoints/{ref}/probe` | Self-hosted OpenAI-compatible endpoint registry and capability probe |
| `GET /api/model-endpoints/{ref}/usage` | Per-endpoint usage rollup |
| `GET/POST /api/debug`, `POST /api/debug/{id}/resume` · `/abort` · `/extend` | Debug re-run sessions |
| `WS /api/debug/{id}/terminal` | Terminal attach to a paused step |

**Not yet built:** everything under `/api/bench/*` (Milestone 13). Specified in
`docs/milestone-13/api-surface.md`; no router exists.

**MCP:** 45 tools for Claude Desktop orchestration
(`grep -c '@mcp.tool' backend/app/mcp/server.py`).

---

## Agent Guidelines for This Repo

When working on LazyAF, agents should:

1. **Understand the architecture**: Backend (FastAPI) + Frontend (Svelte) + Runners (Docker)
2. **Check existing patterns**: Look at similar routers/services before creating new ones
3. **Run tests after changes**: `pytest` for backend, `npm test` for frontend
4. **Use the internal git server**: Changes go to internal server, not GitHub
5. **Follow the step type model**: All work is agent/script/docker steps
6. **Reference historical docs**: See `historical-documents/` for completed phase details
