# What is next

**Status:** recommendation. No code changed, nothing committed, nothing pushed.
**Owns:** this file.
**Scope:** the tree at `ac777bc` plus the uncommitted working tree, judged
against the question "what should the next session do."
**Method:** four survey lanes read the code, then every bug and every "not
started" was put through an adversarial verifier told to refute it. Two findings
died and are recorded in §6 so nobody re-files them. Everything below is cited
to a file and a line I read, not to a document that claimed it.

---

## 1. Where this project actually is

**Phase 12.8 is complete.** `pipelines.steps` is gone — dropped by
`backend/alembic/versions/0015_drop_pipeline_steps.py`, with the tombstone at
`backend/app/models/pipeline.py:43-52`. `steps_graph` is the only definition a
pipeline has. The v1 array survives deliberately, as the authoring dialect at
two edges (`.lazyaf/pipelines/*.yaml` and `PipelineCreate.steps`), converted at
the boundary by `array_to_graph` — that is a decision (PLAN.md:1373), not
unfinished work.

**Alembic head is `0015`.** The chain is 0001-0007, 0009-0015; there is no 0008.
`tdd/integration/test_migrations.py:42` pins it. The next free id is **0016** and
no wave currently wants it.

**Rung 0 security landed** in `ac777bc`: loopback bind defaults, the docker
socket out of the default step bind allowlist, and the forged-push endpoint
gated on the runner secret. Only item 0.4 (pin `LAZYAF_VERSION`) remains, and it
is blocked on cutting a first release — `git tag` is still empty.

**T1 is green at 5493 executed.** T2 was last measured 2026-08-31 at 83. T3 is
green but carries a real intermittent failure (§4, D1).

### What is fiction

PLAN.md at `ac777bc` is three commits behind and points at finished work as "the
next action." **A concurrent session has already fixed most of that in the
working tree** — `git status` shows `M PLAN.md` (+258/-97). The top sections,
the Milestone 12 table, the 12.8 row and the Alembic head row (PLAN.md:244) all
read correctly now. What is still stale, in the working tree, is the **ledger**:

| Line | Says | Reality |
|---|---|---|
| `PLAN.md:337` | S1-bind **OPEN** | Closed by `ac777bc`. `.env.example:75-76` ship `127.0.0.1` defaults |
| `PLAN.md:518` | T18 YAML export **CONFIRMED** broken | Closed. `routers/pipelines.py:859` emits the array dialect and 409s on an inexpressible graph |
| `PLAN.md:713` | L3-1 stale-junit trap **OPEN** | Closed by `c5b6668`, both halves, except the per-invocation junit path |
| `PLAN.md:1095-1098` | Committed head is `0012`; 12.8 P4 takes `0013` | Head is `0015`; both ids are taken. **This is the paragraph a migration author consults** |
| `PLAN.md:2400` | YAML export "lossy — see ledger T18" | No longer lossy |

Outside PLAN.md, four more documents assert things that are no longer true:

- `README.md:449-452` — "**IN PROGRESS.** Retiring the v1 array format … the
  rest has not [landed]." All of it landed. The tail ("repo YAML still takes the
  ordered-list form, **and will until that work completes**") is *also* wrong —
  repo YAML keeps the array permanently, by design (`schemas/lazyaf_yaml.py:120-121`).
- `QUICKSTART.md:210-213` — tells the reader `docker-compose.yml` hardcodes
  `"8000:8000"` and needs a manual edit. It does not (`docker-compose.yml:7`,
  `:37`, `:138` are all loopback), and the edit it recommends would undo Rung 0.
  The same file says the opposite at `:160-163` and `:190`.
- `upcoming/wave10-v1-retirement.md` — fully implemented, still in imperative
  voice, still hardcodes `0012`/`0013` in 14 places. Belongs in
  `historical-documents/`.
- `upcoming/README-maintainer-notes.md` — cites 41 MCP tools (real: 45) and
  ~1,700 tests (real: 5493).

### The thing that is not a document problem

**The dogfood ratchet has been dark for 11 days.** The internal git server's
`main` is at `ee9fb0c` (2026-08-30 12:25); `git rev-list --count ee9fb0c..HEAD`
is **29**. The newest push-triggered run on the owner's stack is
2026-08-30T16:26:09, `failed`, 1 of 7 steps, dying at tier1 on a
`ModuleNotFoundError` for a module that landed 3.5 hours later. The last run to
go *green* was 2026-08-30T06:06:41 with **6** steps; the YAML now has **11**.
Five of those eleven steps — `secret-scan`, `seed-endpoints`, `harness-probe`,
`harness-probe-notools`, and the full remote lane — have never executed
anywhere, ever.

`README.md:534-544` and `CONTRIBUTING.md:150-156` both say, in the present tense,
that LazyAF gates its own commits and that GitHub deliberately does not. Right
now nothing decides correctness. Six migrations, a column drop and a security
rung shipped ungated. That is the same failure `c5b6668` fixed one level down —
a gate that cannot tell a passing suite from a suite that never ran — reappearing
at the level above it.

---

## 2. The recommendation

### First: re-arm the dogfood ratchet

Not because it is exciting. Because every other number in this repo is
downstream of it, and because R7 calls it "the standing acceptance test for the
whole arc." The irreversible half of 12.8 — `DROP COLUMN pipelines.steps` —
shipped without the run that `wave10-v1-retirement.md:539` §5.3 said was its
acceptance. Under R2 that is a debt, not a completed phase.

It is also the one item where the survey found a **live regression introduced by
the last commit**, and it is one line:

> **`LAZYAF_STEP_BIND_ALLOWLIST` reaches no container.** `.env.example:60` tells
> the operator to uncomment it. `grep -rn STEP_BIND_ALLOWLIST docker-compose*.yml`
> returns nothing, and `grep -n env_file docker-compose*.yml` returns nothing at
> all. `docker-compose.yml:18-29` gives the backend an explicit `environment:`
> list that names six variables and not this one. `backend/app/config.py:417`
> reads it from the process environment only — and `Settings` is a plain
> `BaseModel` (`config.py:323`), so its `class Config: env_file = ".env"` at
> `:393-394` is dead code that has never done anything. Putting the value in
> `.env` is inert in *every* deployment shape, native uvicorn included.

`ac777bc`'s own message says it "deliberately breaks LazyAF's own dogfood
pipeline until the variable is set, and that is the point." There is currently no
supported way to set it. Both `needs: [docker]` steps
(`.lazyaf/pipelines/test-suite.yaml:133`, `:146` — tier2 and tier3) will fail at
dispatch with `bind mount source ... is not permitted from pipeline step config`
(`backend/app/services/execution/local_executor.py:227-237`), naming the variable
the operator has already set.

It has not bitten yet only because `lazyaf-backend-1` has been up 9 days and is
running pre-`ac777bc` code. It fires on the next `docker compose up`.

**The sequence:**

1. Add `LAZYAF_STEP_BIND_ALLOWLIST: ${LAZYAF_STEP_BIND_ALLOWLIST:-}` to the
   backend `environment:` block in `docker-compose.yml` and
   `docker-compose.release.yml`. Do the same for `LAZYAF_BIND_ALLOWLIST` on the
   runner-agent services — `runner-agent/lazyaf_runner/config.py:272` has the
   identical unwired hole and will bite the moment a remote step declares
   `needs: [docker]`. Delete the dead `env_file` stanza at `config.py:393-394`
   while you are there (R1: it advertises a mechanism that does not exist).
2. Make the T3 failure self-describing before you gate on it (§4, D1). Ten
   minutes, and it converts an unfalsifiable rumour into a fact.
3. `python scripts/build_images.py` — the `:dev` step images are older than the
   backend container and four steps in the YAML have never run.
4. Rebuild the stack, set the variable, `git push lazyaf main`, and fix forward.

**Do not estimate this as a clean push.** 29 commits of drift, six migrations,
and five steps that have never executed. Budget a debugging session, not a green
run. That is the honest shape of it and it is exactly why it should not wait
another 29 commits.

One thing that will *not* break it: the Rung 0 push-event secret.
`backend/app/routers/git.py:173` `git_receive_pack` calls `on_push` in-process at
`:222` with no auth gate; only the forged-push HTTP endpoint at `:274` requires
the secret. `git push lazyaf main` still fires triggers.

### Second: the graph executor's three correctness bugs

Once the gate is back on, this is the block to spend a session on, and it comes
before the harness for a reason I will defend: **the harness measures pipeline
graphs, and the graph engine is currently nondeterministic on the single most
important thing a CI product does.**

`_all_upstream_satisfied` (`pipeline_executor.py:4722`) promises in its own
docstring at `:4731-4733` that a step runs when its upstream edges are complete
"AND the edge conditions match (success edge requires success, etc)." The code
never checks the condition — it only checks `from_step not in completed_ids`.
Reproduced at HEAD on `test --success--> deploy`, `build --success--> deploy`:

- Order A (test fails first, build passes second) → dispatches `deploy`. A merge
  node runs on a red build.
- Order B (build passes first, test fails second) → dispatches nothing, stamps
  the run `failed`, and blames the engine: *"edge 'e2' from finished step 'build'
  selected it, but it never ran (and nothing dispatched it)."*

Same pipeline, same outcomes, opposite results, decided by container finish
order. And the branch-then-rejoin shape (`a --success--> ok`,
`a --failure--> recover`, both `--always--> report`) can never go green in *any*
order, because the untaken branch never enters `completed_ids`. Since 12.8
retired the array path, edges are the only way to express failure routing.

The fix is smaller than it looks: `unreached_graph_steps`
(`pipeline_executor.py:984-995`) already computes the correct "did this edge's
condition actually fire" predicate. Extract it and pass `_graph_step_outcomes`
(`:4770`) into the gate. The work is the test matrix, not the change.

Ship it alongside the two that share its blast radius:

- **Duplicate dispatch** (`:4603-4623` fan-out loop, `:2048` entry points): two
  edges between the same pair append the target twice, producing two containers,
  two StepRuns, and a terminal action fired twice. The shipped graph editor
  blocks the literal duplicate (`ConnectPanel.svelte:93`) but *not* the
  `always` + `success` overlap, which reproduces. The API and CLI take anything.
- **Definition pinning** (`models/pipeline.py:86-108` — `PipelineRun` carries no
  graph snapshot): the executor re-reads the live `Pipeline` row at every step
  boundary (`pipeline_executor.py:4377-4381`, `:4458`) *and* inside the spawned
  step task before the container starts (`:3049-3128`). Editing a pipeline while
  a run is in flight retargets that run — including firing a `merge:` that did
  not exist when it started. `update_pipeline` (`routers/pipelines.py:312-345`)
  has no in-flight guard. A snapshot on the run fixes both halves; a fix that
  only snapshots at completion leaves the dispatch path open.

All three are the difference between "LazyAF runs your graph" and "LazyAF
usually runs your graph." They are also the substrate the harness's independent
variable sits on: an arm whose graph has a fan-in is measured by a coin flip.

### Third: experiment harness P0

**Yes, it is the right next big thing — after the two above, and it does not need
a security milestone in front of it.**

The case for it is strong and the survey confirms the design's own audit. Its
execution substrate is real: `put_archive` exists
(`local_executor.py:896`) so a `checks/` tree reaches a grade container without a
bind mount, which matters now that Rung 0 emptied the allowlist. Migration head
is `0015` so its "0016 onward" is correct. It is additive — new tables, new
service package — and deliberately leaves the shipped 12.6.5 experiment stack
and its five Playwright specs alone. It runs the owner's code on the owner's box
and adds no network surface, so it is gated on **no** security work.

Two caveats, both cheap:

- **The classify_cell R4 hole is already fixed, uncommitted.**
  `backend/app/services/experiment_service.py` is modified in the working tree:
  the evidence check now runs before the outcome is consulted, a zero-test exit-0
  run returns `error` rather than `passed`, and a shared `UNMEASURED_CELL_ERROR`
  is wired into the `/resume` orphan sweep (which previously left `cell.error`
  blank — a separate R1 hole). Review and commit it. **Do not scope it as harness
  work.** One flaw to fix before committing: the new docstring justifies itself
  by claiming the bug "reported a pass rate of 1.0: a fabricated 100%." It never
  did — the buggy path returns `None` with `NO_TEST_EVIDENCE`. The real harm was
  that it disarmed `MAX_ERROR_RATE` and `MIN_REPEATS`. A correct fix with a
  fabricated rationale is the same defect class it is fixing.
- **Pin the container resource spec in the provenance block before P5.** Rung 1.1
  proposes `nano_cpus=2.0` on a 24-CPU host — a 12× cut that rebases wall-clock
  *and* `agent_seconds`, the harness's designated parallelism-robust headline
  (`experiment-harness.md:700`). Today steps are unbounded. §7.2's pinned list
  and §7.3's NOT-PINNED list both omit container limits, and §6.2's refusal table
  has no mixed-platform row. This is a **doc edit, today, unblocked** — add the
  resolved spec + its hash to §7.2 and a `BLOCKED | mixed container spec` row to
  §6.2. It is not a reason to build Rung 1.1 first: P0 is hello-world × N=1,
  where a CPU cap is invisible. The corpus that would be rebased arrives at P5.

### The decision you have to make before 13.1, and it is not on this list

There are **six** documents totalling ~6,000 lines describing this unbuilt
subsystem: PLAN.md:1350-1400, four files under `docs/milestone-13/`, and
`upcoming/experiment-harness.md`. PLAN.md already routes a 13.1 reader to
`leaderboards-and-corpus.md` in four places and marks it as superseding; the
harness doc reconciles with that one row by row (15 citations) and adopts
`strategy-catalog.md`'s graph dialect unchanged. The genuinely unreconciled
document is **`phase-specs-and-metrics.md`** — 998 lines of 13.1-13.5 phase
deliverables, contract-test filenames and DoD checklists written against the
`StrategyTemplate`/`Trial` vocabulary that `experiment-harness.md` §0.4 replaces
with `ARM`. The harness doc never cites it. Zero hits.

The choice is not "which design." It is the scope call the harness names itself
at §11 tension 3: **defer M13's SWE-bench machinery** (`bench case derive`,
`lazyaf-oracle`, `LAZYAF_TEST_ID_MODE=nodeid`, the nine authored cases) behind
the first real measurement. That is a large reduction and the doc says out loud
it "should be a conscious owner decision, not a silent one." Mechanically the
fix is three lines: a status pointer at PLAN.md:1350, a row in the doc index at
PLAN.md:1462-1471, and a note on whether `phase-specs-and-metrics.md`'s phase
plan survives the rename.

The working-tree PLAN.md at `:97-108` already surfaces this conflict. Good. It
is still unmade.

---

## 3. Do this first regardless

Everything here is under a session, and every item removes either a live hazard
or a false belief. The two marked **LOUD** are the cheap-and-unblocks-a-lot
answers.

1. **Commit the working tree.** Five modified files and one untracked doc are
   sitting there: `PLAN.md` (the de-staling), `experiment_service.py` +three test
   files (the classify_cell R4 fix), `scripts/preflight.py`, and
   `upcoming/shipping-happy-path.md`. The preflight change is a real regression
   fix — `resolve_port` did a bare `int(raw)` on `LAZYAF_BACKEND_PORT`, so after
   Rung 0 made `127.0.0.1:8000` the shipped default, the documented third install
   step failed a *correct* config and advised the user to "set it to a port
   number," whose only effect would be republishing an unauthenticated API on
   `0.0.0.0`. A preflight that fails a good config and talks the user into a
   worse one is the sharpest R1 violation found this week. Review and land it.
2. **LOUD — one line in `docker-compose.yml` re-arms tier2 and tier3.** See §2.
   Nothing else in this report has that ratio.
3. **LOUD — rename the `base_url` fixture at
   `tdd/qa/test_api_fuzz_findings.py:86`.** It shadows the session-scoped fixture
   `pytest-base-url` registers (pulled in transitively by `pytest-playwright`,
   `backend/pyproject.toml:38`), so every test in that module errors in setup.
   82 tests come back — 63 as visible ERRORs plus **19 that pytest has been
   laundering into XFAIL**, because an xfail-marked test whose *setup* explodes
   reports XFAIL, not ERROR. That is a third of the QA corpus reporting "known
   broken" without making a single HTTP request. One identifier.
4. **Add `"../runner-agent/tests"` to T1's selection in `scripts/run_tier.py`.**
   189 tests, 3.58s, no Docker, zero blockers; its conftest inserts its own
   `sys.path` and the backend env already has every dependency. I confirmed the
   combined selection passes. This is PLAN.md's own L3-3 recommendation, already
   written at `:781-783`. (Do **not** justify it as "gating Rung 0" — `ac777bc`
   touches no file under `runner-agent/`, and its tests landed inside T1's
   existing selection.)
5. **Raise the T1 floor.** `tdd/tier_floors.json` says `floor: 4432` against a
   suite that executes 5493 — a 19.3% margin. The floor's only job is catching
   whole files that silently stop collecting; at that margin a thousand-test
   subtree can vanish and the gate prints OK. Raise to **~5383** and set
   `measured: 5493`. The file documents the rule ("measured minus ~2% slack") and
   nobody has applied it for three waves. T2 is due a raise to ~81, which costs a
   ~7-minute Docker re-measure.
6. **Fix the number PLAN.md quotes as its own T1 total.** `PLAN.md:211` at HEAD
   said 4836 — and `PLAN.md:719` reproduces that exact string from the
   invocation where *zero tests ran*. An R4 fake green sitting in the Numbers
   table, cited as evidence elsewhere in the same file. The working tree has
   already corrected the row; make sure the correction lands.
7. **Two stale-doc edits that point users at harm:** `QUICKSTART.md:210-213`
   (tells them to undo Rung 0) and `README.md:449-452` (says 12.8 is in
   progress). Both are trivial and both are on the path an outsider reads.
8. **Recreate your own containers.** `docker ps` shows `lazyaf-backend-1` at
   `0.0.0.0:8000`, `lazyaf-frontend-1` at `0.0.0.0:5173`, `lazyaf-mock-endpoint-1`
   at `0.0.0.0:8099` — all created before `ac777bc` changed the compose defaults.
   **The Rung 0 loopback fix is not in effect on your running stack.** Nothing in
   that commit's docs says "recreate your containers." Separately, eight
   `lazyaf-runner-claude-*` / `lazyaf-runner-gemini-*` containers have been up 11
   days for services that `67a4e1c` deleted — the R2 tail of a removed
   architecture.

---

## 4. The bug list

Ranked by (would a user hit it × how bad). File:line, and what the fix is.

| # | Bug | Where | Fix |
|---|---|---|---|
| 1 | **Fan-in gate ignores edge conditions.** A join fires on a red branch or wedges forever, decided by container finish order. Branch-then-rejoin can never go green | `pipeline_executor.py:4722-4750`; docstring promises it at `:4731` | Extract the predicate already in `unreached_graph_steps` `:984-995`, pass `_graph_step_outcomes` `:4770` into the gate. Change is small; the order-swapped test matrix is the work |
| 2 | **`LAZYAF_STEP_BIND_ALLOWLIST` reaches no container** — every `needs: [docker]` step fails at dispatch on next restart, with an error naming a variable that cannot be set | `docker-compose.yml:18-29`, `config.py:393-394,417`, `local_executor.py:227-237` | One `environment:` line per compose file, ×2 services. Delete the dead `env_file` |
| 3 | **Editing a pipeline mid-run retargets the run**, including firing a `merge:` that did not exist when it started — and changing the image a dispatched step runs | `models/pipeline.py:86-108` (no snapshot), `pipeline_executor.py:4377-4381`, `:3049-3128`, `routers/pipelines.py:312-345` | Snapshot `steps_graph` onto `PipelineRun`. Must cover the dispatch path, not just completion |
| 4 | **A backend restart strands every in-flight run as `running` forever.** Card stuck `in_progress` (only `/reject` exits); repo and pipeline become undeletable (`IN_FLIGHT_RUN_STATUSES` 409s at `pipelines.py:370`, `repos.py:213`); experiment `/resume` skips the cell and `is_stalled` returns False | `main.py:79-127` is the whole sweep; `recovery.py:74-110` touches only `StepExecution`. `recovery.py:21-27` and `runner_dispatcher.py:31-36,660-664` all assert a "run-level orphan sweep" that does not exist | A startup reconcile. Cheapest honest first step: delete the three false guarantees |
| 5 | **The run-list endpoint inlines every step's full logs.** Measured live: `?limit=15` → 1,495,934 bytes, 97.4% log text, largest single log 285 KB. Fetched every 3s while any run is active, and on every WS reconnect from any page | `schemas/pipeline.py:433` (`logs` on `StepRunRead`), embedded at `:500`; both `routers/pipelines.py:503` and `:524` | A list-shaped response model without `logs`. The payload is **dead** — nothing in the frontend or the tests reads it off a list response |
| 6 | **Duplicate edges / entry points dispatch a step twice** — two containers, two StepRuns, `trigger:` fires twice (two fix cards; `_spawn_fix_card:5136` has no idempotency key), progress bar over 100% | `pipeline_executor.py:4603-4623`, `:2048`; validation deliberately skips it per `:842-844` | Dedupe at definition time plus both dispatch sites. The UI blocks the literal dupe but not `always`+`success` between the same pair |
| 7 | **A malformed `.lazyaf/pipelines/*.yaml` vanishes from the listing and 500s with raw Python in a toast** — the most likely thing a new user gets wrong | `routers/lazyaf_files.py:181` (bare `print()`, file silently dropped), `:128,225,279,332` (raw `{e}` in `detail`). Empty file 404s as "not found" via `if content:` at `:214` | One router. ~12 sites across `lazyaf_files.py`, `git.py:95,126,250`, `repos.py:136,322` |
| 8 | **Redaction has no pattern for GitLab / HuggingFace / Stripe / npm tokens** | `services/redaction/patterns.py:108-152` | **Lower than it reads.** Pass 1 harvests any env var matching `SECRET_ENV_NAME` (`patterns.py:315-318`) by exact value, which is strictly stronger than a regex, and the bundle README says in three places what redaction does not cover. The higher-leverage half is widening `SECRET_ENV_NAME` — `GITHUB_PAT`, `GH_PAT`, `STRIPE_KEY`, `SLACK_WEBHOOK_URL`, `SENTRY_DSN` and `DATABASE_URL` all miss it |
| 9 | **Two stacks on one docker daemon GC each other's workspace volumes** | `workspace_service.py:83` label has no instance component; sweep at `:252` | **Narrower than the ledger says**: Docker 409s on a volume any container references, and that propagates to the sweep's `except`. The exposure is volumes with no container attached — between steps, a paused debug session, a remote step in flight |
| 10 | **Runner workspace volumes leak, and the rate is increasing.** Daemon census today: 69 `lazyaf-ws-*`, of which 48 carry `lazyaf.runner-workspace=true`, vs 36 on 2026-08-31 | `runner-agent/lazyaf_runner/workspace.py:155` — the provisioned map is in-memory; the labels at `:62-63` are write-only | Reconcile from labels at startup |
| 11 | **`gpu_fraction` is unbounded and reaches the cost multiplier.** `LAZYAF_GPU_FRACTION=100` meaning "100 percent" is a silent 100× bill. The *rate* is guarded in two places; the fraction is not. `cost_usd` also accepts negatives, so one row makes a run's total go **down** | `schemas/usage.py:58-76` (no constraint anywhere), `run.py:390-397`, `usage_pricing.py:57-62`, `usage_ingestion.py:74-87` | `ge=0` on six fields is free. `gpu_fraction` needs a **decision**, not a constraint: under never-fail-a-step a 422 discards the whole accounting row, so clamp-and-log in `usage_ingestion` is probably the R1 answer |
| 12 | **Playground `internal/*` routes are unauthenticated and answer `ok:true` for sessions that do not exist** (all four) | `routers/playground.py:316,323,336,343` | These are **dead** — marked LEGACY-ONLY at `:292-296`, zero in-tree callers, on the 12.6 deletion list. R2 probably says delete, not authenticate. `routers/jobs.py:159` has the same defect and is not dead |

**D1 — the T3 flake.** Listed separately because it is the one the surveys got
wrong in opposite directions and it will waste a session if taken at face value.
`tdd/e2e/test_us2_card_loop.py` fails intermittently on `KeyError: 0` at `:356`.
It is **not** a red tier — two independent full-suite runs after the failing one
passed 22/22 — and it is **not** a product bug. The captured traceback is
`InvalidRequestError: This session is in 'prepared' state`, from
`tdd/conftest.py:344-357` handing every request one shared `AsyncSession` while
the test's own container double issues an API call from a background task. In
production `database.py:76-78` gives each request its own session. Two fixes, in
order: **(a)** assert `response.status_code == 200` before `.json()` at
`tdd/e2e/test_us2_card_loop.py:342-343` and `:355-356` — neither call checks, so
a 500 surfaces as a KeyError naming nothing, which is R1 *inside the test* and is
why three separate agents reported three different root causes; **(b)** give the
ASGI client a per-request session. Do (a) before re-arming the ratchet: a gate
that stops pushes for a harness bug it cannot describe trains you to ignore it.

---

## 5. The PLAN.md edit list

Line numbers are against the **working tree**, which already carries a large
de-staling from a concurrent session. Verify `git status` before starting; if
that work has been committed, these are what remain.

- **`:1095-1098`** — "Committed head is **`0012_workspaces_per_worker`** … 12.8
  P4 takes **`0013`**, P6 takes **`0014`**." Head is `0015`; both ids are taken
  (`0013` by endpoint modalities in `70f9d6c`, `0014`/`0015` by 12.8). Next free
  is `0016`. **This is the standing-policy paragraph an implementer reads before
  generating a revision** — the highest-value single edit left in the file. Keep
  the adjacent instruction at `:1099` ("check `git ls-files
  backend/alembic/versions/` before you generate"); obeying it yields the right
  answer even when the paragraph is wrong, which is why nothing has forked.
- **`:337` S1-bind** — CLOSED by `ac777bc`; move to the closed table. **Keep one
  sentence**: `docker-compose.dev.yml:5` really does still read `"8000:8000"`
  with `--host 0.0.0.0` at `:19`, and `docker-compose.qa.yml:19` is the same.
  Both are off the fresh-install path (no doc references them; the only in-tree
  consumer is an image-rot walk at `test_no_legacy_code.py:186-189`), so this is
  a footnote, not an open item.
- **`:518-556` T18** — CLOSED. Evidence: `routers/pipelines.py:859`
  `export_pipeline_yaml`, `:876-878` writes `triggers`, `:881`
  `graph_to_yaml_steps` (defined `:746`) returns a list, `:679`
  `GraphNotExportable` drives the 409 at `:883-893`, `:894-907` refuses a
  graphless pipeline rather than emitting `steps: []`. Also strike the T18
  annotation at **`:2400`**, and add a row to the closed table where T17 and T19
  already sit. **One code file goes with this edit**:
  `tdd/qa/test_api_fuzz_findings.py:361-393` is a `strict=True` xfail still
  asserting the old behaviour, with line refs (`pipelines.py:499-503`) that point
  nowhere. It has not screamed because `tdd/qa` is in no tier. It needs
  rewriting, not unmarking — it posts `on_success: "deploy"` as a bare step id,
  which `array_to_graph` does not accept.
- **`:713-736` L3-1** — closed by `c5b6668`: `run_tier.py:168`
  `junit_path.unlink(missing_ok=True)` and `ci_gate.py:109-126`'s
  `--max-age-seconds` refusal. Mark it **"closed except the per-invocation junit
  path"** rather than striking it — the entry's own third sub-item
  (`:1094`-adjacent, "derive the junitxml path per-invocation so concurrent runs
  cannot collide") never shipped; `run_tier.py:145` still uses fixed literals, so
  two concurrent runs of the same tier share one file, and half (a)'s unlink
  makes that window slightly worse. Add a small open R4 item while you are there:
  the new freshness guard has **zero test coverage** — none of
  `test_ci_gate.py`'s 19 tests exercises `max_age`, every fixture writes its
  junit fresh, and inverting the comparison at `ci_gate.py:115` leaves T1 green.
- **`:236-250` S1 core** — still open and substantively right, but the count is
  wrong: seven HTTP routes carry a credential check now, not six
  (`steps.py:187,260,293,336,375` + `model_endpoints.py:527` +
  `git.py:274`), plus two websockets.
- **The document index (`:910-916` at HEAD)** — it enumerates wave docs, QA
  evidence and post-mortems, and by its own wording has no category for a
  forward-looking design proposal. Six files are unreachable by navigation:
  `security-posture.md`, `experiment-harness.md`, `shadow-ci.md`,
  `graph-test-catalogue.md`, `sprawl.md`, `README-maintainer-notes.md` — and now
  `shipping-happy-path.md` and this file. Add a section for **unimplemented
  proposals and their gates**, because two of them stopped being blocked at
  `0adfad0` and nothing records it: `shadow-ci.md:3` ("Lands after 12.8") and
  `graph-test-catalogue.md:3-5` ("written to be executed by an implementation
  wave *after* Phase 12.8 lands"). 875 and 468 lines that went actionable and
  nobody noticed.
- **`:1164` "Also in 12.8: the dogfood pipeline converts to a v2 graph … NOT
  DONE"** — strike **only this evidence paragraph**, which is dated 2026-08-31
  and operationalized the clause as rewriting the YAML file. The identical
  sentence in the 2026-08-30 decision log is the *acceptance criterion* and
  should stand, read correctly: the materialized pipeline **row** becomes a graph
  via the boundary converter, proven by a real push-triggered run. That is §2's
  first recommendation. **Do not rewrite `.lazyaf/pipelines/test-suite.yaml` into
  graph form** — no schema field parses it, and it would produce a silently-empty
  pipeline and four red T1 tests.
- **API Summary** — add the diagnostics surface from `be5e943`:
  `GET /api/diagnostics/system`, `/logs`, `POST /bundle`, `GET /bundle/{id}`,
  `/bundle/{id}/download` (`routers/diagnostics.py:183,225,312,340,356`). ~10.4k
  lines of user-facing surface with no row in any table. Skip the Project
  Structure tree — it lists no individual service or router, so adding
  `redaction/` there would be inconsistent with the section's altitude.
- **Retire `upcoming/wave10-v1-retirement.md`** to `historical-documents/`. It is
  a `git mv` plus six reference updates — including a code docstring at
  `backend/alembic/versions/0013_endpoint_modalities.py:44`. Low priority, not a
  hazard: its own §4.7 at `:449` says "do not hardcode `down_revision` from this
  plan," and `test_migrations_pipeline_retirement.py:180` fails T1 loudly if the
  chain ever forks.

---

## 6. What not to do next

**Do not start Rung 1.2 (human auth).** Not because the API being open is fine —
because the design as written is wrong in a way that costs a session to discover.
`security-posture.md` §1.4's three-plane table concludes "human auth can be added
without a flag day," and §3.2 1.2 says git smart-HTTP "uses git's native HTTP
Basic against the same secret." Git is filed as a human plane and it carries
**uncredentialed machine traffic**: `workspace/population.py:182` builds a bare
clone URL from `config.py:409`, `_build_clone_script:147-152` emits a
credential-free `git clone` into the helper container,
`pipeline_executor.py:3828` puts the same URL in every remote step's exec
context, and `runner_common/git_helpers.py:129-136` pushes to it from inside the
agent container. Land §3.2 1.2 as written and every clone and every agent push
401s. Both obvious repairs are worse: injecting the secret hands the master human
credential to the code the platform runs (and into a shell script inside a user
container, and into `.git/config`, and into two log streams); exempting `/git`
leaves anonymous push able to rewrite a repo's materialized pipelines behind
nothing but a UUID. **The honest prerequisite is a decision about a fourth
credential for the git plane.** That is a design session, and it is not what to
do this week.

And the underlying urgency is low *for this operator*. Rung 0 made the API
loopback-only on one box used by one person. Auth becomes urgent at exactly three
moments, none of which is now: a second machine needs to reach the backend
(14.5), shadow CI Phase 2 runs a stranger's code, or you want to show this to
someone over a network. Note also that three more surfaces need exempting that
the doc does not name — `debug.py:80-86` already signs its join token with
`step_auth_secret` and `cli/lazyaf/debug_cmd.py:500` sends it on the same
`Authorization` header; `main.py:449`'s bare `@app.websocket("/ws")` is not on a
router at all and cannot carry a header from a browser; and the CLI has no config
storage (`cli/lazyaf/cli.py:60-65` is one env var and a flag), so `$LAZYAF_TOKEN`
mirroring `$LAZYAF_SERVER` is the conforming cheap answer.

**Do not build Phase 14.5.** PLAN.md's own line is right: "It blocks nothing
today." 2,465 lines of design, GPU images, a supervisor, a new orchestrator
answer for pods with no docker socket. It is correctly last, and it is the
strongest argument for doing security work *eventually* rather than now — it is
the milestone that makes hardening non-optional. Two corrections for whenever it
comes up: the "build pipeline blocker" is not a blocker (the design already
routes those images out of `scripts/build_images.py::IMAGES` and says so at
`:1196`), and Rung 1.2 is **not** a 14.5 prerequisite —
`security-posture.md:972` says so in bold. Only Rung 1.1 is.

**Do not re-implement anything in 12.8.** PLAN.md at `ac777bc` says P3 is "NOT
STARTED — this is the next action." It shipped two commits ago against a schema
that no longer has the field. The working tree already fixes this; make sure it
lands before any agent reads the committed version.

**Do not build a scheduler.** A survey lane filed `schedule` as a dark contract —
accepted by the validator, read by no code, fires never. Refuted. `schedule` is a
run *label* on `PipelineRun.trigger_type`, documented as exactly that at
`docs/examples/pipelines/nightly-expensive.yaml:7-17` ("It is a label, not a
scheduler"), it starts the run immediately, and it has a passing test
(`test_pipeline_execution_api.py:531-542`). The residue is real but different and
small: `TriggerConfig.type` is a bare unannotated `str` (`schemas/pipeline.py:186`)
with no validator, so `type: banana` is accepted and silently ignored at
`trigger_service.py:196,472`. That is a ~10-line closed-vocabulary validator
aligning the REST door with the MCP door, which already refuses
(`mcp/server.py:494`).

**Do not chase "the 500 in `GET /api/pipelines/{id}/runs`" as a product bug.**
See §4 D1. There is no confirmed production 500; the mechanism is a shared
session in the test fixture.

**Do not tier `tdd/qa` yet.** It is not tier-hostable as written —
`tdd/qa/pytest.ini` is a standalone config written specifically to escape
`tdd/conftest.py`, and `tdd/qa/conftest.py:17-40` speaks HTTP to a live stack.
Worse, it is **not trustworthy as a backlog number yet**: at least one finding is
dead (`test_graph_definition_qa4.py:187-211` fails with `KeyError: 'steps'`
because the field it reads was dropped by `0015` — the bug it encodes is now
structurally impossible), one is a refuted finding still asserted strictly
(`test_qa3_concurrent_readers.py:93`, which `qa-triage.md:1036` itself lists under
"Refuted findings" and at `:998` already prescribes deleting), and two are
deliberately load-dependent `strict=False` probes. Sequence: rename `base_url`,
re-run for a true count, sweep the corpus for post-12.8 staleness, *then* decide
the lane. Any percentage quoted before that is unmeasured for a third of the
corpus and inflated by dead tests — an R4 problem pointed the other way.

**Do not fold `runner-agent/tests` into T1 believing it closes a Rung 0 gap.**
`ac777bc` touches no file under `runner-agent/`. Its tests landed inside T1's
existing selection and are already gated. Fold it in because it is 189 free
tests, which is reason enough.

---

## 7. The dependency graph

Hard edges — A must precede B, with a reason that is not taste:

```
B6 (compose env line)  ──▶  dogfood ratchet re-armed  ──▶  every R7 claim in README/CONTRIBUTING
D1(a) (status_code assert) ─┘                          └─▶  R2 acceptance for the 0015 column drop

graph correctness (fan-in, dup dispatch, definition pinning)
        └──▶ any benchmark number measured on a graph with a fan-in
        └──▶ shadow CI Phase 2 (it runs graphs over foreign commits)

C4 (a credential for the git plane) ──▶ Rung 1.2 human auth ──▶ shadow CI upstream-PR path
                                                             └─▶ webhooks instead of polling

Rung 1.1 (container hardening spec) ──▶ Phase 14.5 (it multiplies spawn hosts)
                                     └─▶ shadow CI Phase 2 (foreign conftest.py in a container)
                                     └─▶ harness P5 corpus (caps rebase wall-clock and agent_seconds)

base_url rename ──▶ a true QA backlog number ──▶ any decision about tiering tdd/qa
```

Soft edges worth respecting:

- **`ingest-remote` is shared** between shadow CI Phase 1 and harness P5.
  `shadow-ci.md:353` already specifies the shape (commit_sha optional, refspec a
  parameter). But the phasing removes the urgency the surveys gave it: the
  harness **explicitly excludes** it from P0 (`experiment-harness.md:1035`) and
  its first slice is a `kind: empty` greenfield problem that "needs no ingest at
  all." Whichever milestone gets there first pays once. They do not need to be
  adjacent.
- **One column's worth of decision is due now, though**: shadow CI rejects a
  `kind` enum (`shadow-ci.md:303`, because it makes "a bench case pinned inside a
  repo I also shadow" unrepresentable) and proposes nullable-url-plus-flags; the
  harness assumes `Repo.mode = 'bench'` (`experiment-harness.md:576`) and never
  cites §8.1. An unnoticed collision, not two considered positions. Cheap to
  settle, annoying to migrate later.

**Non-edges, stated because the security doc implies more gating than survives
scrutiny.** The experiment harness needs **no** security work to start. Shadow CI
Phase 1 (mirror only, nothing runs, nothing spends) needs none either — and it
has been actionable since the day it was written; `shadow-ci.md:456` says "Phase
1 can start immediately, every file it needs is uncontested," and its nine
BLOCKED rows gate Phases 2-4. What `0adfad0` actually unblocked there is Phase 2.

`security-posture.md` §6.4's nine-item gate on shadow CI is a gate on **Phase 2
and later**, and it both over- and under-counts. Over: §4.1's structural refusal
to run mirrored YAML means the operator always chooses the image and the step
vocabulary, which collapses the image allowlist (2.2) and the per-repo trust tier
(3.4.1). Under: a survey lane then argued the gate reduces to container
hardening alone, and that is wrong in the expensive direction. A stranger's
`conftest.py` in a step container is not ACE in a sandbox — it is ACE **inside
the unauthenticated admin API**, holding its address in an env var. Every step
joins `lazyaf-network` (`local_executor.py:789`), the same bridge as the backend,
and is handed `LAZYAF_BACKEND_URL=http://backend:8000` (`:698-700`).
`security-posture.md:627-645` carries the live probe: from that network,
`GET /api/repos` → 200, `GET /api/debug` → 200, `git push` → exit 0. `cap_drop`
and `pids_limit` touch none of that. The realistic Phase 2 gate is: container
hardening **plus the step network split (2.1) plus the shared-volume secret path
(1.6)** — a claude-code step's `ANTHROPIC_API_KEY` lands at
`/workspace/.control/agent.<id>.json` on the shared volume
(`control_layer/workspace.py:8-21,187`), which a stranger's tests can read —
plus log redaction and a dispatch bound. Roughly six, not nine and not one.

**The one gap nobody owns**, because three documents each work around it at their
own layer: there is no global bound on concurrent step dispatch.
`pipeline_executor.py:2424-2436` is a bare `_spawn_task` per step. Four crude
bounds exist or are proposed at four layers — the per-endpoint admission gate
(`:1503`, skipped for `reach=runner-local`), `Experiment.max_concurrency`
(`experiment_service.py:791`, cells only), the harness's proposed
`LAZYAF_TRIAL_SLOTS` (which calls itself "a static-width guess wearing a cap's
clothing"), and shadow CI's `max_concurrent_shadow_runs`. That is the R3 failure.
One dispatch-level semaphore with a setting is small work and is the honest place
for it — and for the harness it is not decoration, because a wall-clock number
measured on an unbounded box with N other trials contending is precisely the
plausible-looking number R1 exists to forbid.

---

## 8. Evidence quality

Strong (executed or read at HEAD): the fan-in reproduction, the duplicate-dispatch
reproduction, the mid-run retarget reproduction, the compose/env-var gap, the
1.5 MB run-list measurement, the volume census, T1 at 5493, the migration chain,
the `base_url` shadow and its 19 laundered xfails, the 29-commit ratchet gap.

Thin, and flagged as such: **B7's restart-strands-runs** is established
structurally — the startup sweep is five calls and none touches `PipelineRun` or
`Card` — not by restarting anything, because the owner's stack is live. The claim
rests on code plus `recovery.py:24` asserting a sweep that does not exist.
**D1's root cause** is a single captured traceback, not a characterized race;
the status-code assertion is recommended precisely so the next occurrence names
itself. **QA findings T6, T7, T12, T15** were not verified to the standard of the
rest. The **duplicate-dispatch progress-bar overshoot** is derived from
`:4308,:4429` being unconditional, not measured — verifying it needs Docker/T2.

Two probes ran against the QA sandbox on :8790 and are worth discounting: that
process reports `stale: true` with 20 stale files including `routers/pipelines.py`
and `config.py`, so it is running pre-`ac777bc` code. (That the staleness probe
works at all is a point in `be5e943`'s favour.) Everything read from the owner's
stack on :8000 was a GET.
