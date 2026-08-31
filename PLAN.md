# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

> **Reconciled against the tree 2026-08-31.** Status claims in this file name
> their evidence. History lives in `historical-documents/`; this file is about
> what is next.
>
> The 2026-08-30 ledger below was re-verified against HEAD (`744376b`) on
> 2026-08-31 by four independent lanes, after three waves had landed on top of
> it (`4f529e1`, `b54dd19`, `08e356d`, `744376b`). Items that closed are in the
> closed table; items whose evidence had drifted carry corrected file:line;
> items nobody could execute say so instead of being asserted.
>
> **Tree caveat, 2026-08-31.** HEAD is clean, but a concurrent wave is editing
> `backend/app/schemas/pipeline.py`, `backend/app/services/agent_run.py`,
> `backend/app/services/experiment_service.py` and `tdd/shared/factories/`.
> Every line number in this file is **HEAD-relative**; re-check those four
> before trusting a line number in them.

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
| P4 | Migration **`0013`**: backfill `steps` -> `steps_graph`, add the `definition_error` column | NOT STARTED. The `definition_error` *schema field* already exists (`schemas/pipeline.py:283`) but there is no column and no backfill revision. **Corrected 2026-08-31:** `0012_workspaces_per_worker.py` is **committed** (`08e356d`), so the committed head is `0012` and the next free id is **`0013`**, not "the next free id, pending". |
| P5 | Delete the executor's array fork | NOT STARTED — every named identifier still branches at HEAD: `parse_steps` (`pipeline_executor.py:711`), `STEP_ACTIONS`/`STEP_ACTION_PREFIXES` (`:793-800`), `describe_step_action` (`:808`), `is_graph` threaded at `:694,704,3034,3171,4139,4306,4393`, `_handle_action` dispatch at `:2575,3112,4406,4546`, `Pipeline.has_graph_definition` (`models/pipeline.py:53`) |
| — | **ACCEPTANCE GATE** (`wave10-v1-retirement.md` §5). Nothing below runs until it passes. | — |
| P6 | Migration **`0014`** (the revision after P4's): drop the `steps` column; the tombstone lands | NOT STARTED |

Three things to carry into P3 that the tree confirms:

- ~~`PipelineRead.definition_error` must land **with** P4's column, not before
  it.~~ **WRONG AND OBSOLETE — corrected 2026-08-31.** The field already landed
  at P1 (`b79bb7f`), deliberately ahead of the column, and
  `schemas/pipeline.py:279-283` documents exactly why that is safe:
  `from_attributes` falls back to the default for an attribute the ORM does not
  declare. Two tests pin it
  (`tdd/unit/schemas/test_pipeline_schemas.py::TestPipelineReadDefinitionError`
  — 6 passed). **The constraint that actually survives** is about the ORM
  *model*, not the schema: `backend/app/models/pipeline.py` must not gain a
  `definition_error` attribute before the `ALTER`, because SQLAlchemy emits the
  model's full column list in every `SELECT`. That is the version stated
  correctly in `upcoming/wave10-v1-retirement.md:723`.
- **The revision numbers in `upcoming/wave10-v1-retirement.md` are a live
  trap.** Fourteen lines there hardcode `0012`/`0013`, including the filenames
  it tells B3 to create (`:283`, `:454`, `:463`) and the acceptance-gate test
  names (`:522-523`). `0012` is now taken. §4.7 (`:449`) does hedge — "if M14
  has taken `0012`, take `0013`/`0014`" — and its answer is now literally
  correct, but it names the wrong wave (M13-1 took it, not M14) and `:445` still
  asserts "head `0011`". An agent reading §3.5's ownership table before §4.7
  would author `0012_pipeline_steps_to_graph.py` and fork the chain into two
  heads.
- YAML export is a P3-adjacent hazard, not a cosmetic one — and it is **worse
  than the 08-30 ledger said**. See T18 below: on top of the steps-as-dict,
  `timeout`, `continue_in_context` and `actions` losses, `triggers` is never
  written at all, and the *legacy* branch re-imports **cleanly** while silently
  resetting every step's `on_success`/`on_failure` to `next`/`stop`.

### 2. Then — Milestone 13: the benchmark & evaluation harness

**NOT STARTED. Zero implementation.** A repo-wide grep for `BenchmarkCase`,
`StrategyTemplate`, `TrialIteration`, `fail_to_pass` and `cost_to_solve` returns
exactly one hit, and it is a comment
(`backend/app/services/agent_run.py:15`). The design is complete and lives in
this document plus `docs/milestone-13/`. Start at **13.1 (corpus & fixtures)**.

**BLOCKER 1 — the workspace constraint — is CLOSED (2026-08-31, `08e356d`).**
The old note here told the reader to confirm it; the answer is **yes**.
`backend/app/models/workspace.py:79` maps `pipeline_run_id` with **no**
`unique=True`; the uniqueness moved to `:65-67`
`Index("uq_workspaces_run_worker", "pipeline_run_id", "worker_key", unique=True)`
with `worker_key` NOT NULL + `"default"` sentinel (`:81-88`). Migration
`0012_workspaces_per_worker.py` (revision `0012`, down `0011`) is committed, and
`tdd/integration/test_migrations.py` pins both the index swap and volume-name
stability. Two lanes of one run are insertable, a duplicate lane is rejected, and
`tdd/integration/services/test_workspace_lifecycle.py:261` writes into and reads
back four independent volumes. **Isolation was checked and is intact**: volume
names still carry the full run id (`models/workspace.py:93-94`), so two runs
never share a volume — "per worker" means per lane *within* one run.
13.2's substrate exists.

**BLOCKER 2 (new, and it is now the hard gate on 13.1) — the oracle only sees
`@pytest.mark.lazyaf_test_id`, which no upstream repo carries.**
`runner-common/runner_common/pytest_lazyaf.py:161-163` returns early for any
unannotated test, and `:210-211` writes no manifest at all when nothing was
recorded — so an unmodified third-party repo POSTs nothing and every imported
case fails validation. Making `psf/requests` scorable therefore means forking and
editing its test files, which makes `base_commit_sha` a commit that exists only
in LazyAF's git server and breaks the `fetch/` reproduction path.
**This is diagnosed and specified, not open-ended**:
`docs/milestone-13/leaderboards-and-corpus.md:287-322` quotes those same three
lines and prescribes `LAZYAF_TEST_ID_MODE=nodeid` (marker wins, then an optional
`LAZYAF_TEST_ID_MAP` overlay, then the pytest nodeid). Zero implementation — a
grep for `LAZYAF_TEST_ID_MODE` across `runner-common/`, `backend/`, `cli/` and
`tdd/` hits only that document. Small change, hard gate.

**BLOCKER 3 (new) — 13.1's exit gate is unsatisfiable for its own normal case.**
`docs/milestone-13/leaderboards-and-corpus.md:261-285`: `api-surface.md:141-143`
says a `fail_to_pass` test "may not exist yet at `base_commit_sha`, which for
`fail_to_pass` is the normal case", while the validator (and Phase 13.1 below)
demands every `fail_to_pass` id **FAIL** at base. A missing test is not a failing
test. And if the agent writes the `fail_to_pass` test, the measured party writes
the measurement. Decide the rule — present-and-red at base, or `missing` as a
third state — **before** cases are authored; it is cheap now and expensive later.

**Read `docs/milestone-13/leaderboards-and-corpus.md` before starting 13.1.** It
landed in `4f529e1` as an adversarial review of this document's own M13 design
and it **contradicts the design in at least two places** (repo pinning by name,
and the oracle id mode). The M13 body below still carries the reviewed design and
does not mark itself superseded.

Both of Milestone 13's mandatory in-12.x hooks are in place: `StepUsage` (12.5,
migration `0005_step_usage.py`) and the cost axis in 12.6.5.

### 3. Waiting — Phase 14.5: runner images with inference baked in

**DESIGNED, zero implementation.** Evidence corrected 2026-08-31: the old
"no `vllm` or `ollama` anywhere under `images/` or `scripts/`" line was **false
when written** — `scripts/seed_dogfood_endpoints.py:68,85` set
`"server_kind": "vllm"` (M14's endpoint layer, `4b429c6`). The status is still
right; the honest evidence is the *absent 14.5 identifiers*: no
`images/node-layer/`, `images/runner-ollama/` or `images/runner-vllm/`
(`images/` holds only agent-base, base, claude, debug-sidecar, gemini,
test-runner); no `scripts/build_inference_images.py`; no `refuses_without_gpu`;
no `gpu.py` `detect()`/`verdict()`; and no GPU-yield/drain mechanism (the only
`drain` hits are M12.6's runner registry). Wiring doc:
`upcoming/wave9-145-runner-images.md`. It blocks nothing today, but it is what
makes Milestone 13's headline experiment — an expensive planner directing K
cheap local workers — runnable on hardware the owner already owns.

### 4. Standing — the open-item ledger

The [verified open items](#open-items-verified-2026-08-31) below are the
non-phase work: the security posture (S1 and its five sub-items, S2), seven
correctness defects, a handful of validation gaps, and — **new on 2026-08-31** —
five defects in the **test gates themselves**, which had never been surveyed.
They are carried here so they are not lost between milestones. Each was
re-checked against the tree on 2026-08-31; the ones fixed are in the closed
table so the shrinkage is visible.

---

## Status at a glance

### Milestones

| Milestone | Status | Evidence |
|---|---|---|
| 1-11 (foundation through playground) | COMPLETE | `historical-documents/phase-01…phase-11` |
| **12 — Runner architecture + spec/eval layer** | **IN PROGRESS** — every phase through 12.7 COMPLETE; **12.8 open** | Detail retired to [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md); 12.8 tracked below |
| **13 — Benchmark & evaluation harness** | **NOT STARTED** | Zero implementation. Grep for `BenchmarkCase` / `StrategyTemplate` / `TrialIteration` / `fail_to_pass` / `cost_to_solve` across `backend/`, `frontend/`, `cli/`, `tdd/` returns one hit, a comment at `backend/app/services/agent_run.py:15`. Design: this document + `docs/milestone-13/` |
| **14 — Self-hosted OpenAI-compatible endpoints** | **COMPLETE** (2026-08-30) | Commit `4b429c6`: 56 files, ~21.5k lines. `ModelEndpoint` + migration `0011_model_endpoints.py`, capability probe, agent harness in `runner-common/runner_common/harness/`, stdlib mock OpenAI server, Endpoints UI. Out of the 12.x sequence |
| **14.5 — Runner images with inference baked in** | **DESIGNED** | Zero implementation. Evidence corrected 2026-08-31 — the old "no `vllm`/`ollama` anywhere" line was false (M14's endpoint layer uses both words). The absent 14.5 identifiers are the evidence: no `images/node-layer/`, `images/runner-ollama/`, `images/runner-vllm/`; no `scripts/build_inference_images.py`; no `refuses_without_gpu`; no `gpu.py` `detect()`/`verdict()`; no GPU-yield mechanism. Doc: `upcoming/wave9-145-runner-images.md` |

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
| T1 (unit + non-Docker integration) | **4836 passed / 0 failed / 0 errors, 1 baselined skip, 0 xfailed** (executed = 4836) | Measured 2026-08-31 against the current tree; the count is re-derivable by tallying `junit-t1.xml` with `ci_gate`'s own rules. Floor in `tdd/tier_floors.json` is **4432** (measured 4523, stamped 2026-08-30) — three waves behind. **RATCHET DUE: raise the floor to ~4739 (4836 minus ~2% slack) and set `measured` to 4836.** The floor was stale-low *by design* at 08-30; as of 08-31 it is stale-low by **neglect**, and the file's own standing instruction ("raise on the next green T1: measured minus ~2%") is now due twice over. |
| T2 (Docker integration) | **83 passed / 0 failed / 0 errors, 1 baselined skip** (executed = 83) | Measured 2026-08-31, same method, against `junit-t2.xml`. Floor 75 / measured 77 in `tdd/tier_floors.json`. **RATCHET DUE: raise to ~81.** |
| T3 (e2e quick) | floor 21, **measured 22 on 2026-08-30 — NOT re-measured on 2026-08-31** | `tdd/tier_floors.json`. Stated rather than assumed: `junit-t3.xml` **does not exist on this host**, so "both tiers green" covers T1 and T2 only and T3's 22 tests are unmeasured here. |
| Alembic head (committed) | **`0012_workspaces_per_worker`** | Corrected 2026-08-31. `git ls-files backend/alembic/versions/` — 0001-0007, 0009, 0010, 0011, **0012**. There is no `0008`. `0012` (revision `0012`, down `0011`) landed in `08e356d`; `tdd/integration/test_migrations.py:42` pins `ALEMBIC_HEAD_REVISION = "0012"`. The next free id is **`0013`**, and only one wave wants it (12.8 P4). |
| MCP tools | 45 | `grep -c '@mcp.tool' backend/app/mcp/server.py` |
| Release CI | Publishes **9 images** to GHCR: 3 service (`backend`, `frontend`, `runner-agent`) + 6 step (`base`, `agent-base`, `claude`, `gemini`, `test-runner`, `debug-sidecar`) | `.github/workflows/images.yml`; the step list is read from `scripts/build_images.py`'s `IMAGES` table, not duplicated |
| Release tags | **None. `git tag` is empty.** `release.yml` triggers only on `push: tags: ['v*']` (plus manual dispatch), so the tag path has never fired. `images.yml` also runs on push to `main`. | `.github/workflows/release.yml:51-54`, `images.yml:60-64` |

---

## Open items (verified 2026-08-31)

Findings from the adversarial QA pass (`upcoming/qa-triage.md`, which keeps the
reproductions) plus the security review, carried here so they survive between
milestones. **Every item below was re-checked against the tree on 2026-08-31**
and the file:line evidence is the check. Items fixed are in the closed table at
the bottom so the shrinkage is visible rather than silent.

These are not a phase. Fold them into whatever phase touches the same file, or
schedule the security block on its own.

### Security posture

**S1 — No authentication on any human-facing router, while compose binds
`0.0.0.0` and mounts the Docker socket.** CONFIRMED, re-verified 2026-08-31,
statically and live.

A decorator-walk over all 20 files in `backend/app/routers/` finds only three
non-`get_db` `Depends(` in the whole tree, and none of them is auth
(`debug.py:353` session factory, `ws_runners.py:644` session factory,
`test_api.py:134` `require_test_mode`). Only two middlewares are registered
(`main.py:247` `UnhandledErrorBoundary`, `:249` `CORSMiddleware`); neither
authenticates.

**Corrected count of authenticated surfaces — there are FOUR, not two.** The
08-30 ledger said "`steps.py` and `ws_runners.py`". Also authenticated:
`model_endpoints.py:515` `POST /{reference}/probe-result` (step JWT) and the
debug terminal socket (`debug.py:361-370`, join-token). In HTTP terms exactly
six routes carry a credential check: `steps.py:187,260,293,336,375`
(`verify_step_auth`, defined at `steps.py:121`) plus that probe-result route;
plus two websockets. Everything else — cards, pipelines, repos, spec,
spec_context, experiments, jobs, agent_files, lazyaf_files, models, playground,
runners, test_results, git, and every HTTP route in `debug.py` — is open.
LIVE on the isolated QA sandbox (`:8790`): `POST /api/repos` with zero headers
returned **HTTP 201** and created a repo (then `DELETE` → 204);
`GET /git/{id}.git/info/refs?service=git-upload-pack` **and**
`?service=git-receive-pack` both returned 200 with a full ref advertisement — so
the git server serves clone **and** push to anyone.

**Two specifics the 08-30 ledger did not name:**

- `POST /git/{repo_id}.git/_internal/push-event` (`routers/git.py:180-199`) is
  unauthenticated and calls `trigger_service.on_push`. An anonymous caller can
  **forge a push event and start pipeline runs** — i.e. spawn containers on the
  host daemon — without pushing anything.
- **CORS is one of the few things NOT wide open, and this is written down so
  nobody "fixes" it.** `config.py:313` defaults `cors_origins` to
  `["http://localhost:5173"]` with `allow_credentials=True` (`main.py:249-255`) —
  not a wildcard. It blocks a drive-by from a victim's browser and does nothing
  about `curl`, which is how every probe above reached the API. **Do not credit
  CORS with mitigating S1.**

**S1-bind — every compose file still publishes on `0.0.0.0` by default, and
`.env.example` ships a portless value that keeps it that way.** OPEN.
`docker compose -f docker-compose.yml config` shows three published ports with
**no `host_ip` line at all**: 8000 (`docker-compose.yml:5`), 5173 (`:35`), 8099
(`:135`). `docker-compose.dev.yml:5` is the same hardcoded `"8000:8000"` with
`uvicorn --host 0.0.0.0`. `docker-compose.release.yml:53` defaults to
`"${LAZYAF_BACKEND_PORT:-8000}:8000"` — and `.env.example:49` ships
`LAZYAF_BACKEND_PORT=8000` with no host IP, so a user who copies the template
(which is what QUICKSTART tells them to do) gets `0.0.0.0` even on the release
stack that supports loopback. Two edits, different costs: `.env.example:49-50`
→ `127.0.0.1:8000` / `127.0.0.1:5173` fixes the release stack for every new user
at zero risk; `docker-compose.yml:5` → `"127.0.0.1:8000:8000"` fixes the dev
stack but breaks anyone reaching it from another machine on purpose.

**S1-e2e — `backend-e2e` publishes a fourth `0.0.0.0` port that carries an
unauthenticated database wipe.** OPEN, and **not in the 08-30 ledger**, which
counted only the two main compose files. `docker-compose.yml:151-186` defines
`backend-e2e` with `ports: - "8765:8000"` (no host IP), the host docker socket,
and `profiles: [e2e]`. `frontend/e2e/compose.test-mode.yml:8-11` overlays
`LAZYAF_TEST_MODE=true` onto exactly that service, which mounts
`POST /api/test/reset` and `/api/test/seed` behind `require_test_mode`
(`test_api.py:123-135`) — a **config gate returning 403 when off, not
authentication**. While the e2e profile is up, port 8765 offers an anonymous
"clear every table" button on every interface. The scoping is done right (a
separate overlay file, one service, profile-gated); what is left is the binding:
`127.0.0.1:8765`.

**S1-debug — `POST /api/debug/{id}/join-token` mints a terminal JWT with no auth
of its own.** OPEN. `debug.py:252-273` takes `(session_id, db)` and nothing else;
it calls `mint_join_token` (`:88-103`), which signs
`{debug_session_id, iat, exp}` with `settings.step_auth_secret`. Session ids are
enumerable from the equally-open `GET /api/debug` (`:233`). LIVE:
`POST /api/debug/does-not-exist/join-token` with zero headers returned **404
from the handler**, not 401 from a gate — which is the proof there is no gate.
The token itself is well built (bounded by `min(now+TTL, session.expires_at)`,
409 after the session ends, re-checked at the socket); the only missing piece is
a caller credential, and that **cannot be fixed independently of S1** because
there is no notion of a user to bind it to.

**S1-docker-sock — the socket is mounted into the backend and a step can still
take it with `needs: [docker]`.** OPEN, and deliberately so. Mounts:
`docker-compose.yml:14`, `docker-compose.release.yml:61`, `backend-e2e:176`, and
the runner services. Step path: `pipeline_executor.py:3268-3287` turns
`needs: [docker]` into a bind mount of `DOCKER_SOCKET_SOURCE`;
`local_executor.py:200-225` gates bind sources against `bind_mount_allowlist()`,
whose default (`:134-145`) is `(DOCKER_SOCKET_SOURCE,)` — the socket is the one
thing on the allowlist. Covered by
`tdd/unit/services/test_pipeline_local_dispatch.py:890` and
`tdd/unit/services/execution/test_local_executor_hardening.py:239-264`. **Nothing
to fix in isolation** — this is the DooD tradeoff and `README.md:216-219` and
`docker-compose.release.yml:57-60` both say so. It stays on the ledger as the
reason S1 is a *takeover* rather than a data leak. **One-line drift worth
fixing**: `local_executor.py:137-142` says the allowlist is "settings-driven via
`step_bind_mount_allowlist`" and reads it with a defensive `getattr` — that
parallel change never landed. A grep for `step_bind_mount_allowlist` across
`backend/` and `tdd/` returns only those two lines. The allowlist is hardcoded
and the comment says otherwise.

**T21 — the playground `internal/*` endpoints are unauthenticated.** The 08-30
ledger deferred this ("not re-verified — under concurrent edit"). **Re-verified
2026-08-31, and it is worse than reported.** `playground.py:277-309` declares
four routes on `session_router` (prefix `/api/playground`, mounted at
`main.py:408`): `POST /{session_id}/internal/status`, `/internal/result`,
`/internal/log`, `/internal/runner`. Every signature is `(session_id, data)` —
no `Header`, no `Depends`, no step JWT, no runner secret. LIVE:
`POST /api/playground/bogus-session/internal/log` with a body of log lines
returned **HTTP 200 `{"ok":true}` for a session id that does not exist**, so
there is no existence check either (`playground_service.append_logs` at
`services/playground_service.py:445-448` iterates and swallows). Anyone who can
reach the port can forge status, results, diffs and log lines into any playground
session. **This one does not need S1 to fix**: these are machine callbacks and
should take the same Bearer step JWT `/api/steps/*` already uses
(`steps.py:121-141` is the pattern), plus a 404 for an unknown session.

**S2 — Step containers have no CPU or PID limit.** CONFIRMED, and re-confirmed
**live** on 2026-08-31 rather than only read.
`backend/app/services/execution/local_executor.py:755-774` builds `run_kwargs`
and sets `mem_limit` only `if memory_limit:` (`:773-774`). A repo-wide grep for
`nano_cpus|cpu_quota|pids_limit|cpu_shares|cpuset` across `backend/app` returns
**exactly one hit** — that `mem_limit` line. Against the QA sandbox:
`LAZYAF_QA_BASE_URL=http://127.0.0.1:8790 python -m pytest -c tdd/qa/pytest.ini tdd/qa/test_step_resource_limits_qa4.py`
→ **2 passed, 2 xfailed** in 10.4 s. Both xfails are `strict=True`, so they are
current confirmations rather than stale annotations:
`test_step_container_has_a_memory_limit` (observed `HostConfig.Memory=0`) and
`test_step_container_has_a_cpu_limit` (`NanoCpus=0`, `CpuShares=0`). The two that
PASS are worth keeping green: the container is not privileged, and it carries the
`lazyaf.pipeline_run_id` label so orphans are findable. Fix: platform defaults for
`mem_limit`, `nano_cpus` and `pids_limit` at `local_executor.py:755-774`, sourced
from `config.py` so an operator can raise them. **Note PID limits have no test at
all** — QA-4 covers memory and CPU only, so a fork bomb in a script step is
neither bounded nor detected.

**S2-fanout — no fan-out cap for script steps.** OPEN, but the 08-30 ledger's
sentence was **wrong** and is corrected here: it said "`pipeline_executor.py` has
no semaphore or `max_parallel` of any kind". There **is** one —
`_admit_to_endpoint` (`pipeline_executor.py:1554`) holds one of a model
endpoint's concurrency slots before the container starts. Its only call site is
`:2857`, guarded by `if endpoint is not None`, so it covers agent/endpoint-bound
steps and nothing else; a plain `script` step passes no gate. (The other
`Semaphore`, `model_endpoints.py:730-740`, is per-endpoint proxying.) The
conclusion survives, the sentence did not. Standing finding:
`tdd/qa/test_graph_execution_qa4.py:284-325 test_fanout_is_capped`,
`xfail(strict=True)`, QA4-12 — "a 20-way fan-out put all 20 into
`active_step_ids` at once". **NOT EXECUTED this pass** (it is `@pytest.mark.heavy`
and would start 20 containers), so it is carried on its marker, not on a run.
Fix: a run-level or platform-level cap on concurrently active graph steps; the
endpoint gate is the shape to copy.

### Correctness

**T5 — The run-list serializer ships every step's full logs.** CONFIRMED, and
now **measured** against the live dev backend (`:8000`) on 2026-08-31 rather than
only read. `GET /api/pipeline-runs?limit=100` → **5,834,441 bytes**, of which
**5,641,837 (96.7%) is the `logs` field**, across 87 runs / 237 step_runs;
largest single step log 285,077 bytes. `GET /api/pipelines/{id}/runs?limit=100` →
5,767,973 bytes (same defect, second endpoint). At the limit the **frontend
actually uses** (`pipelines.ts:181`, `loadForPipeline` default `limit=10`) it is
still **1,665,472 bytes per pipeline-detail open**. Mechanism unchanged:
`backend/app/routers/pipelines.py:399,420` both declare
`response_model=list[PipelineRunRead]`; `PipelineRunRead.step_runs` is
`list[StepRunRead]` and `StepRunRead.logs` is a plain `str`
(`schemas/pipeline.py:349`); no `response_model_exclude` anywhere. Fix: a
list-shaped read model without `logs` (`PipelineRunListRead` /
`StepRunListRead`) on those two routes. The per-step log endpoint already exists
(`routers/pipelines.py:498`), so nothing is lost. Drops the default dashboard
payload ~30x.

**T9 — Duplicate graph edges dispatch a step twice — and now fire its effects
twice.** CONFIRMED.
Both halves re-confirmed 2026-08-31; **line numbers corrected** (the 08-30
ledger cited `:4645-4665` and `:4613-4634`; the code is at `:4662-4679` and
`:4630-4655`).

*Half 1 — validation permits it, deliberately.* `graph_definition_errors` says so
in its own docstring: duplicate entry points and duplicate parallel edges are
"NOT checked here (deliberately)" (`pipeline_executor.py:892-895`), and the edge
loop at `:925-946` does `successors.setdefault(source, []).append(target)` with
no de-dup. Executed: `graph_definition_errors({'entry_points':['a','a'], two
identical a->b edges})` returns `[]`, and `get_downstream_edges(g,'a','success')`
returns 2. The 422 boundary is `schemas/pipeline.py:740-742` calling the same
permissive function, so the API accepts it too.

*Half 2 — dispatch and effects both double.* The fan-out loop at
`pipeline_executor.py:4662-4679` appends one entry to `steps_to_execute` per
matching edge with no de-dup, and its guard (`:4670`) reads sets loaded at
`:4595-4596`, **before** `_reserve_active_steps` runs at `:4693` — so both copies
pass. `_reserve_active_steps` (`:4788-4803`) dedupes `active_step_ids` but **not**
`steps_to_execute`, so `_execute_graph_step` is still called twice. Terminal
actions are keyed to node *completion* (`:4630-4655`) and
`_handle_graph_step_complete` has **no already-handled guard** at its entry
(`:4589-4601` does an unconditional `completed_ids.add(...)`) — so a node that
completes twice fires its `merge:` / `trigger:` twice. `_spawn_fix_card` has no
idempotency key (two cards); `_merge_step_branch` (`:5660-5729`) is **not** a
no-op on re-entry — it re-resolves a source branch and re-runs the
`.lazyaf-context` delete-and-commit against the target. The entry-point half is
the same defect at `:2080` (`for step_id in entry_points:` over the raw list).
`:4810` even admits it in a docstring: "the duplicate dispatch of QA4-06".

Four `xfail(strict=True)` tests already name this as QA4-06 and all four still
xfail today: `tdd/qa/test_graph_definition_qa4.py::test_duplicate_entry_points_are_rejected`,
`::test_parallel_duplicate_edges_are_rejected`,
`tdd/qa/test_graph_execution_qa4.py::test_duplicate_entry_points_dispatch_the_step_once`,
`::test_duplicate_edges_dispatch_the_target_once`. Fix: reject duplicate
`entry_points` and duplicate `(from,to,condition)` triples in
`graph_definition_errors` (422 at the boundary); de-duplicate `steps_to_execute`
at `:4679` as defence in depth; add an already-completed guard at the top of
`_handle_graph_step_complete` so terminal actions cannot fire twice. Then flip the
four xfails — **and update their reason strings, which cite the stale
`pipeline_executor.py:3405-3420`.**

**T10 — `POST /api/pipelines/{id}/run` walks the whole graph inside the request
handler.** CONFIRMED, unchanged. `run_pipeline` is at
`backend/app/routers/pipelines.py:310` (the 08-30 ledger's `:309` is the
decorator) and still does `await pipeline_executor.start_pipeline(...)` inline at
`:378`. A grep for `MAX_STEPS|max_steps|MAX_GRAPH` across `backend/app` returns
**nothing** — no bound on graph size anywhere. This is the one place the codebase
departs from standing rule **R5** (async-first: "HTTP/git-push handlers return a
run id immediately"). Measured at 299 s on a 400-step chain in the QA pass. Fix:
create the run row, return the run id, dispatch the graph on a background task;
add a `MAX_STEPS` bound at the schema boundary so a 400-step chain is a 422.

**T18 — YAML export is lossy, and for graphs not re-importable.** CONFIRMED **by
a live round trip** on the QA sandbox on 2026-08-31, and **worse in two ways the
08-30 ledger does not mention.** A graph pipeline was created carrying
`timeout=900/120`, `continue_in_context=true` and
`actions={success:[merge:main], always:[trigger:card-abc]}`;
`GET /api/pipelines/{id}/export/yaml` returned 200 with exactly `name`,
`description`, `version`, `entry_points`, and a `steps:` **mapping** whose
entries carry only `name`/`type`/`config`/`on_success`. Feeding it back through
`PipelineYaml(**yaml.safe_load(...))` fails: *"steps / Input should be a valid
list"*. What a round trip loses **today**, exactly:

1. it does not re-import at all — `export_pipeline_yaml`
   (`routers/pipelines.py:564`, graph branch `:586-621`) writes `steps` as a dict
   keyed by step id while `schemas/lazyaf_yaml.py:99-101` declares
   `steps: list[PipelineStepYaml]`;
2. `timeout` — dropped, silently reverts to the schema default 300
   (`lazyaf_yaml.py:58`);
3. `continue_in_context` — dropped, reverts to `False` (`:59`);
4. `actions` — dropped entirely, which since P1 is the **only** way a graph
   expresses `merge:`/`trigger:`, so an auto-merging pipeline round-trips into
   one that does not;
5. **NEW — `triggers` is never written into `export_data` on either branch**
   (`:572-576` sets only name/description/version). Every trigger binding is
   silently dropped, so an exported pipeline never fires again;
6. **NEW, and the worst of the set — the LEGACY/v1 branch (`:623-634`) emits only
   `name`/`type`/`config` per step and DOES produce a valid list, so it
   re-imports CLEANLY while silently resetting every step's
   `on_success`/`on_failure` to the defaults `next`/`stop`.** A v1 step that said
   `on_success: merge:main` round-trips into one that merely falls through — a
   silent behaviour change, where the graph case at least fails loudly.

Fix: emit exactly what `PipelineYaml` accepts — `steps` as a **list**, each
carrying `id`/`name`/`type`/`config`/`on_success`/`on_failure`/`timeout`/
`continue_in_context`/`actions`, plus top-level `triggers`. `PipelineStepYaml`
has no `actions` field yet, so the import schema needs it too. Gate it with an
export → import → compare round-trip test covering **both** branches.

**T1 (RE-OPENED as PARTIAL, 2026-08-31) — naive UTC timestamps are fixed on the
REST/pydantic path and still open on every hand-built wire payload.** The 08-30
closed table listed T1 as fully fixed. It is not. **Proof the two transports
disagree**: the REST list ships `2026-08-30T21:01:46.573700+00:00` (offset
present — `UTCDateTime` doing its job), while `datetime.utcnow().isoformat()` —
what the WebSocket dicts call — produces `2026-08-31T10:59:10.667047` with **no
offset**, which a browser parses as LOCAL time. Same row, two transports, two
answers.

Full census of `.isoformat()` in `backend/app`: 39 matching lines = 4 prose /
docstring mentions + 1 correct helper (`schemas/_datetime.py:80`,
`to_utc(value).isoformat()`) + **34 real bare call sites**. Of those 34:

- **LIVE WIRE, 23 sites** — `routers/pipelines.py:106,107` (`pipeline_to_ws_dict`,
  sent at `:230,268`); `services/pipeline_executor.py:1087,1088,1089`
  (`pipeline_run_to_ws_dict`) and `:1105,1106` (`step_run_to_ws_dict`), broadcast
  from 17 call sites; `pipeline_executor.py:5506,5508` (hand-built job dict);
  `services/playground_service.py:255,437,461,475,488,495,523,604` (8 SSE
  payloads); `routers/steps.py:233,234,313` (machine-facing, but a runner
  computing a timeout off a naive string has the same bug);
  `services/model_endpoints/health.py:96`; `services/model_endpoints/probe.py:741`;
  `services/execution/debug_session_service.py:1145`.
- **DEAD wire code, 5 sites** — `routers/pipelines.py:133,134,135,150,151`. See
  T27; they need **deleting**, not fixing.
- **INTERNAL, not wire, 6 sites** — `services/workspace_service.py:218` (a Docker
  volume label), `services/execution/runner_state.py:262,263,271` and
  `debug_state.py:238,243` (persisted into `DebugSession.state_history`, which
  reaches no client). **Leave these alone.**

The fix already exists and was applied unevenly: `utc_isoformat` is imported and
used in `routers/cards.py`, `routers/jobs.py`, `routers/repos.py`,
`services/agent_run.py` and `services/execution/runner_registry.py` — and in
**none** of the 23 live-wire sites. Sharpest instance:
`pipeline_executor.py:5506-5508` hand-builds a job status frame with bare
`isoformat`, while `routers/jobs.py:130-131` builds the same frame with
`utc_isoformat`. **The pipeline WebSocket path is the busiest live surface in the
product and is the one the sweep missed.** 23 mechanical swaps plus seven
imports.

**T25 — the workspace orphan sweep is not scoped per backend instance; two
backends on one Docker daemon GC each other.** OPEN, **new**, and this is the
**live configuration on this machine right now**, not a hypothesis. `docker ps`
shows `lazyaf-backend-1` (`:8000`) and `lazyaf-qa-backend-qa-1` (`:8790`); both
mount the Docker socket and both use **disjoint databases**. Both run the sweep
(`main.py:48,130`; `workspace_service.py:940-966`, every 300 s with the first
sweep immediate on startup). Sweep 3 (`workspace_service.py:822-859`) builds
`live_volumes` from `select(Workspace.volume_name)` against **this** backend's
database, then lists volumes with
`filters={"label": "lazyaf.workspace=true"}` (`:252`) — **a filter with no
instance component** — and removes anything not in its own live set older than
`stuck_threshold` (default 15 min). Creation (`:214-219`) stamps only
`lazyaf.workspace=true` and `lazyaf.created_at`; **no owner id**. So backend A's
live workspace volume is, to backend B, an unmatched old volume — and B deletes
it out from under a running step. The docstring at `:743-753` reasons carefully
about lanes and about the row-before-volume ordering invariant, but that
invariant is **per-database**, and the sweep's premise ("a volume with no row is
garbage") is false the moment a second database exists on the same daemon. Fix:
stamp an instance-id label at `:214-219` and add it to the sweep's list filter at
`:252`. Cheap and fully local to `workspace_service.py`. **Operationally: the QA
sandbox is not a safe bystander — it can delete the owner's live workspace
volumes after 15 minutes, and vice versa. Know this before the next long dogfood
run.**

**T26 — runner-provisioned workspace volumes are unreapable by anything after a
runner restart: 54 of 55 on this daemon.** OPEN, **new**, and the mechanism is
**not** what a prior pass concluded. Current daemon: **55 volumes named
`lazyaf-ws-*`; exactly 1 carries `lazyaf.workspace=true`.** The other 54 are not
simply unlabeled — **36 carry a different label namespace**
(`lazyaf.runner-workspace` / `lazyaf.retain_key` / `lazyaf.created_at`) and 18
have no labels at all. There are **two creators sharing one name prefix with two
label vocabularies**: the backend (`workspace_service.py:83`,
`lazyaf.workspace`) and the runner agent
(`runner-agent/lazyaf_runner/workspace.py:62-64`). The backend sweep filters on
`lazyaf.workspace=true`, so **54 of 55 are structurally invisible to it**. The
runner side is worse: `cleanup(retain_key)` (`workspace.py:223-234`),
`reap_idle(...)` (`:236-256`) and `cleanup_all()` (`:259-265`) **all iterate
`self._provisioned`, a plain in-memory dict initialised empty at `:155` with no
persistence**, and `VOLUME_LABEL`/`RETAIN_KEY_LABEL` are **write-only** — set at
`:107-108` and never used in any `volumes.list(filters=...)`. The instant a
runner-agent process restarts, every volume it created becomes permanently
unreachable by **both** reapers. 36 leaked volumes is that mechanism's receipt.
Disk today is small (10 at 0 B, 19 at ~27.7 kB), so this is a leak, not yet an
outage — but it is unbounded and there are 9 runner containers on this host. Fix:
make the runner reaper **label-driven instead of memory-driven** (list
`lazyaf.runner-workspace=true`, read `retain_key`/`created_at` off the volume);
**separately decide, and write down**, whether the backend sweep should also reap
the runner namespace — the two namespaces sharing the `lazyaf-ws-` prefix is
exactly the ambiguity that produced this. The 18 unlabeled ones need a one-time
manual `docker volume rm`; nothing in the tree can ever reap them.

**T27 — `pipeline_run_to_ws_dict` and `step_run_to_ws_dict` are defined twice,
and the `routers/pipelines.py` copies are dead code that has already drifted.**
OPEN, **new**. Both functions exist at `routers/pipelines.py:121,139` **and**
`services/pipeline_executor.py:1074,1093`. A grep across `backend/app` and `tdd`
finds **zero callers** of the router pair: all 17
`send_pipeline_run_status`/`send_step_run_status` call sites are in
`pipeline_executor.py` and use the executor's copy, and the only test imports are
from `pipeline_executor` (`tdd/unit/services/test_pipeline_executor.py:30-31`).
Only `pipeline_to_ws_dict` (`routers/pipelines.py:96`) is live (`:230,268`). The
dead copies have **already drifted**: the router version emits `trigger_context`
and `logs` and **omits** `active_step_ids`, `completed_step_ids`, `step_id` and
`executor`. `step_id` is the only field that says which graph node a frame refers
to, so the dead copy would silently break every graph UI if anyone wired it up.
Fix: delete `routers/pipelines.py:121-155`. That removes 5 of T1's 34 naive
timestamp sites for free and closes the drift trap.

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
long-name overflow landed in `a39cb24`). **The unbounded `commits?limit`
sub-item is CLOSED** (2026-08-31): `backend/app/routers/repos.py:373` now reads
`get_commit_log(repo_id, branch, max_count=min(limit, 100))`, and
`GET /api/repos/{id}/commits?limit=999999` returns normally on the sandbox. The
neighbouring list endpoints were already bounded declaratively
(`pipelines.py:402,424`, `Query(20, ge=1, le=100)`). **One cosmetic remnant**:
`repos.py:361` declares `limit: int = 20` with no `Query` bounds, so `?limit=-5`
reaches `min(-5, 100) = -5` and returns HTTP 200 with an empty list instead of
422 (verified live). Making it `Query(20, ge=1, le=100)` matches `pipelines.py`
and deletes the `min()` too. **Still not re-verified**: the 400-vs-409
inconsistency and the silent PATCH drops.

### The test gates themselves (new section, 2026-08-31)

The 08-30 ledger surveyed product defects only; nobody had audited the ratchet.
These five are what that audit found. They are **not** a rewrite of
`ci_gate.py` — see the closed table for the parts of the gate that were verified
sound.

**L3-1 — the stale-junit trap: a tier can report OK having run zero tests.**
OPEN. `scripts/run_tier.py:142-183` never unlinks `REPO_ROOT/junit-t{N}.xml`
before launching pytest, and `scripts/ci_gate.py:49-70` accepts any file that
satisfies `path.is_file()`; `main()` catches only `FileNotFoundError` and
`ET.ParseError`. There is no mtime, no freshness, no invocation-time check
anywhere in either file. **REPRODUCED**: `python scripts/run_tier.py T1 -- --help`
printed `CI GATE [T1]: OK - executed=4836 (floor=4432) passed=4836 failed=0` and
exited 0 while **zero tests ran** — `junit-t1.xml`'s md5 and mtime were unchanged
across the whole invocation. `-- --markers` does the same. **The sharp boundary,
which reading alone would not reveal**: `-- --collect-only -q` DOES write a junit
and the gate correctly **FAILED** (`executed count 0 below committed floor
4432`). So the floor defends against "pytest ran and executed nothing" and is
**completely blind to "pytest never wrote a report at all"** — which is why the
floor felt like protection and was not. Third vector, same class: the artifact is
a fixed name at repo root (`run_tier.py:145`), so two concurrent tier runs share
one file. Fix, two halves, both wanted: **(a)** `junit_path.unlink(missing_ok=True)`
in `run_tier()` immediately before the `pytest_cmd` block — that converts every
non-writing pytest exit into `ci_gate`'s existing `FileNotFoundError` path;
**(b)** a `--not-before EPOCH` argument on `ci_gate.py` that refuses a junit older
than the invocation, with `run_tier.py` passing `time.time()` — **(b) is the half
that protects the documented standalone invocation in `ci_gate.py`'s own
docstring, which is the invocation the survey agents used**; (a) does not help
them. Also derive the junitxml path per-invocation so concurrent runs cannot
collide.

**L3-2 — `xfail` is ungated, and `tdd/README.md` recommends it as the documented
way around the skip baseline.** OPEN. `ci_gate.py:63-67` routes any
`<skipped type="pytest.xfail">` into `counts['xfailed']` and never into `skips`;
`:106` prints the number and **nothing in `main()` ever compares it to
anything**. `tdd/skip_baseline.json` holds three entries, all skips. **Worse than
ungated — it is pinned and recommended**: `tdd/unit/scripts/test_ci_gate.py:134`
`test_xfail_is_not_gated_as_skip` asserts `returncode == 0` with an xfailed case
present (the hole is a passing test), and `tdd/README.md:271-272`, in the section
headed *The ratchet rules*, says "Prefer `xfail(strict=True)` when the target is
known-missing — xfails are not gated." **Hole size, measured** against the real
`junit-t1.xml`: an xfail subtracts from `executed`, so the floor's ~2% slack is
the only backstop — flipping **404** passing testcases to xfailed still yields
`OK - executed=4432 (floor=4432) ... xfailed=404`, exit 0; at 405 it fails and
even then misdiagnoses ("Tests are silently not running"). Headroom: 404 in T1,
8 in T2, 1 in T3. **The good-news half**: xfailed is **0** in both gated
artifacts, and all 44 `@pytest.mark.xfail` markers in the repo are in `tdd/qa`,
which no tier runs — the hatch is wide open and currently unused. Fix, cheapest
honest version: fold `xfailed` into `executed` at `ci_gate.py:101` so an xfail
cannot buy floor headroom. Fuller version: a `tdd/xfail_baseline.json` keyed by
reason prefix, checked beside the skip loop. **Either way, three things change in
one commit**: the gate, `test_ci_gate.py:134` (inverted, not deleted), and
`tdd/README.md:271-272` — leaving the rulebook saying "xfails are not gated"
after gating them is how the next drift starts.

**L3-3 — 870 tests run in no tier; the R4 "stated exclusion" list names 21 of
them.** OPEN. `scripts/run_tier.py:73-139` selects `tdd/unit`, `tdd/demos`,
`tdd/integration` (minus `services`), `runner-common/tests` for T1;
`tdd/integration/services` for T2; `tdd/e2e -m 'not slow'` for T3. Outside all
three, **measured not estimated**: `tdd/qa` **208** collected (own
`tdd/qa/pytest.ini`; referenced only by `upcoming/qa-findings-*.md` prose);
frontend vitest **284** in 15 files, **all green** (`vitest run` → 284 passed in
1.5 s; only caller is `npm run test:unit`, which nothing invokes);
`frontend/e2e` Playwright **189** in 17 files (only callers are the human
`scripts/test.ps1:155` / `test.sh:91` e2e lanes); `runner-agent/tests` **189**,
**all green** in 2.4 s (in no tier, no pipeline, no workflow — and it needs the
`--extra dev` incantation or pytest is not even importable);
`.github/scripts` **940 LOC across 6 modules with ZERO tests anywhere in the
repo**. Against that, the stated-exclusion text names only the 21 `@slow` e2e
tests (`tdd/README.md:235`), and `run_tier.py:15-28` adds a one-line aside that
the frontend suites "run by their own lanes" — **they do not; nothing runs
them**, and `runner-agent/tests` is not named at all. Two separable pieces of
work: **TRIVIAL, do now** — correct the exclusion text in `tdd/README.md:235-240`
and `run_tier.py:15-28` to name all five lanes with their counts, because R4 says
stated-not-silent and it is currently silent about 870 tests. **MEDIUM, the real
fix** — `runner-agent/tests` (189, green, pure-Python, 2.4 s) is the obvious first
candidate for T1: same shape as `runner-common/tests`, which 12.7 folded in for
exactly this reason. Frontend vitest (284, green, 1.5 s) wants a tier of its own
or a node step in `test-suite.yaml`. `tdd/qa` cannot be tiered as-is (L3-4);
Playwright needs the compose e2e stack, same blocker as the `@slow` tests.

**L3-4 — the QA lane is not merely ungated, it is RED at HEAD, and one 82-test
file cannot run in the repo's standard environment.** OPEN.
*Red test #1, a close nobody harvested*:
`tdd/qa/test_qa3_concurrent_readers.py::test_a_single_run_list_request_is_not_pathologically_slow`
**FAILS at HEAD with `[XPASS(strict)]`** — QA3-13 ("a single
`GET /api/pipeline-runs` takes 0.6-1.6 s on a nearly-empty database") is
**FIXED**, and the strict marker at `:93` is now the lie. Because no gate runs
this lane, it has been red-because-fixed for an unknown period.
*Broken file*: `cd backend && uv run pytest -c ../tdd/qa/pytest.ini
../tdd/qa/test_api_fuzz_findings.py` → **19 xfailed, 63 errors, zero passed**,
every error `ScopeMismatch: ... session scoped request object` from
`pytest_base_url`. Cause: `backend/pyproject.toml:38` declares
`pytest-playwright`, which pulls in `pytest-base-url`, whose session-scoped
fixture collides with the module's own function-scoped `base_url` at
`tdd/qa/test_api_fuzz_findings.py:86`. Under **bare system python** — the
invocation `upcoming/qa-findings-api-fuzz.md:13` documents — the same file is
**63 passed, 19 xfailed**. A suite whose result depends on which interpreter you
happen to use is not evidence of anything. Fix, in order: (1) harvest QA3-13 —
remove the strict xfail at `:93`, leaving a plain regression guard, the same move
`5334b09` made four times; (2) rename the `base_url` fixture (or add
`-p no:base_url` to `tdd/qa/pytest.ini`); (3) then it is a candidate for L3-3's
tiering work. **Until it is green under one documented invocation it cannot be
gated at all** — and it holds the sharpest security evidence in the repo (QA4-12
fan-out, QA4-21 resource limits) while being unrunnable by a newcomer: `grep` for
"qa" returns **zero hits** in `tdd/README.md`, `README.md`, `QUICKSTART.md`,
this file and `docs/`, and no compose file, script or doc brings up the stack on
`:8790` that `tdd/qa/qa4_support.py:29` targets by default.

**L3-5 — the five workspace-service doubles are ALREADY drifted from the real
service, one commit after `08e356d` unified them.** OPEN. The five:
`tdd/conftest.py:154` `_T1StubWorkspaceService`, plus `FakeWorkspaceService` in
`tdd/unit/execution/test_debug_gate.py:60`,
`tdd/unit/execution/test_debug_session_service.py:51`,
`tdd/unit/services/test_control_mode_dispatch.py:88`,
`tdd/unit/services/test_pipeline_local_dispatch.py:87`. AST-diffed against
`backend/app/services/workspace_service.py`; **two live drifts, both in all
five**: (1) the real `cleanup(self, db, pipeline_run_id, *, worker_key=None)`
(`:552-557`) versus five doubles declaring `cleanup(self, db, pipeline_run_id)`
with **no `worker_key` at all** — and the real docstring says `worker_key=None`
is "what every caller passes today", i.e. the per-lane cleanup caller is the next
thing to land, and when it does **all five break at once: the identical failure
`08e356d` just paid for, re-armed**; (2) the real
`get_or_create(..., commit_sha=None, *, worker_key=None)` (`:323-332`) makes
`worker_key` **keyword-only**, while all five doubles declare it as an ordinary
positional parameter — so `get_or_create(db, run, repo, branch, sha, "w1")`
passes against every stub and raises `TypeError` against the real service. Nothing
is red because the only production callers are
`pipeline_executor.py:2807` (keyword) and `:1788` (no `worker_key`). Fix — the
guard, concretely: `tdd/unit/services/test_workspace_double_parity.py` (under
`tdd/unit`, so `run_tier.py:78` gates it on every push; **not** in
`runner-agent/tests`, which runs in no tier and would put the instrument in the
same blind spot as the thing it watches). `ast.parse` `workspace_service.py`,
extract the parameter shape of the four seam methods
(`get_or_create`/`acquire`/`release`/`cleanup`), then `ast.parse` each double from
a **committed** `(path, classname)` list, assert every entry still resolves (a
deleted double is a failure, not a silent shrink), and assert each double accepts
every call the real signature accepts. Parametrize over `(file, class, method)` so
a failure names which double drifted. AST rather than `inspect.signature` for the
reason `runner-agent/tests/test_control_archive_parity.py:17-22` gives — importing
a test module to introspect its stub gives the check a way to skip itself, and
AST parsing cannot skip. **Run it once as written and it is RED; that is the
acceptance test for the guard.** Cheaper alternative worth naming: hoist ONE
double into `tdd/shared/` and have all five import it — that removes the drift
surface instead of instrumenting five copies of it, but it does not catch the
shared double drifting from the real service, so the parity test is wanted either
way.

### Written-record drift (the failure this survey exists to catch)

- **The dogfood pipeline stored in the platform is FOUR steps behind the YAML
  file.** `.lazyaf/pipelines/test-suite.yaml` has **11** steps at HEAD; the dev
  backend's stored `[repo] Test Suite` (last updated 2026-08-30T11:36:15Z) has
  **7**, missing `secret-scan`, `seed-endpoints`, `harness-probe` and
  `harness-probe-notools`; the two most recent runs both start at step 0 =
  "Sync Dependencies". `trigger_service.on_push` syncs repo-defined definitions
  from the pushed commit (`routers/git.py:186-196`), so **the next push to `main`
  re-ingests all 11**. Standing caveat worth keeping: *"what the dogfood pipeline
  runs"* and *"what `test-suite.yaml` says"* are two different facts.
- **`tdd/unit/schemas/test_graph_pipeline_schemas.py:1178-1204` is a stale
  mirror.** Its docstring says it encodes "The shape of
  `.lazyaf/pipelines/test-suite.yaml`: ten steps" and it asserts a hardcoded
  10-id list with 9 edges. The real file has 11. It **passes** (the list is
  synthetic), so nothing goes red — but it claims to track a file it does not
  read, and the real-file test its docstring defers to ("B1's (§5.1)") **does not
  exist anywhere in `tdd/`**. Deleting the leak gate from the YAML would go
  unnoticed by every tier.
- **The dogfood pipeline's own leak gate is untested code.** `.github/scripts`
  holds 940 LOC across six modules and **zero tests**; `scan_repo_secrets.py`
  became the FIRST step of every push in `b54dd19`. The pipeline's own comment
  says "a leak gate that could not look is not a leak gate that found nothing" —
  and nothing verifies the scanner can look. (It *was* proved by hand on
  2026-08-31 against a scratch repo: a planted `sk-ant-` key, a Google `AIza`
  key, a GitHub `ghp_`, an `AKIA`, a stripped `.gitignore` and a force-added
  `.env` each produced the right non-zero exit; a non-git directory exits 2; the
  real repo at HEAD scans 688 of 688 tracked files and exits 0. That is a manual
  proof, not coverage.)
- **`docs/examples/validate.py` is wired to NOTHING.** `b54dd19` shipped
  `docs/examples/` (catalog, mechanisms, pipelines/, validate.py) and a grep for
  `docs/examples` across `scripts/`, `tdd/`, `.lazyaf/` and `.github/workflows/`
  returns **zero hits**. A broken example rots invisibly, which is the failure
  mode `docs/examples` exists to prevent.
- **`tdd/tier_floors.json` is stale in the same direction as this file** — see
  the Numbers table. Both ratchets are due.
- **The tree's best measurement currently lives only in a commit log**:
  `08e356d`'s message records "T1 4836 passed / 0 failed", a number that appeared
  in neither this file nor `tier_floors.json` until now.

### What was NOT verified on 2026-08-31 (stated, not quietly asserted)

Carried on a marker, a commit message or a prior pass — **not** on a run or a
read this pass. Do not treat any of these as evidenced.

- **T3.** No `junit-t3.xml` on this host; the tier was not run. "Both tiers
  green" means T1 and T2.
- **QA4-12, the fan-out cap** (`tdd/qa/test_graph_execution_qa4.py:284-325`).
  `@pytest.mark.heavy` — it would start 20 containers. Carried on its strict
  marker.
- **`tdd/qa/test_qa3_workspace_race.py`** and the 6 `heavy`/`containers` cases
  deselected from `test_graph_execution_qa4.py` — too slow to finish in budget.
- **T24's remaining halves**: the 400-vs-409 inconsistency and the silent PATCH
  drops.
- **T15's second half** — "force-merges invented content with no conflict
  present" — still not re-verified since 08-30. Treat it as open.
- **T7** (UI resync after a dropped socket) still rests on `a39cb24`'s commit
  message, not a read.
- The **secret-scan step has never actually run in a real pipeline**: it entered
  `.lazyaf/pipelines/test-suite.yaml` in `b54dd19` at 2026-08-30 23:05, after the
  last push, and the platform's stored definition predates it. The script was
  proved by hand against a scratch repo; the *step* is unexercised until the next
  push.

### Closed — verified fixed, not just claimed

Kept as a list so a reader can tell the ledger shrank on evidence. **Closed on
2026-08-31** first, then the 08-30 set.

| Was | Fixed by | Verified how |
|---|---|---|
| **M13 BLOCKER — `Workspace.pipeline_run_id` was `unique=True`, so K parallel agents would share one checkout** | `08e356d` | `models/workspace.py:79` has no `unique=True`; the composite `Index("uq_workspaces_run_worker", "pipeline_run_id", "worker_key", unique=True)` is at `:65-67`; migration `0012_workspaces_per_worker.py` committed (down `0011`), `worker_key` NOT NULL with a `"default"` sentinel (deliberately not nullable — both SQLite and Postgres treat NULLs as distinct in a unique index). `tdd/integration/test_migrations.py` pins the index swap **and** volume-name stability; `tdd/unit/services/test_workspace_service.py` 66 passed; `tdd/integration/services/test_workspace_lifecycle.py:261` writes into and reads back four independent volumes |
| **The M13-1 change did not weaken run isolation** (checked because "per worker" could have meant volumes shared across runs) | — | Volume names still carry the full run id (`models/workspace.py:93-94`): `lazyaf-ws-{run_id}` for the default lane, `-{slug}` otherwise. A lane is a checkout **within** one run; two runs never share a volume, and `HOME=/workspace/home` does not bleed between runs |
| **The `08e356d` `populate_existing` race fix** — `expire_on_commit=False` served a stale cached row to `_get_lane` and a rival caller deleted a populated volume | `08e356d` | Present and documented with the failure mode written out at `workspace_service.py:266-300` |
| **The README/compose contradiction** — "the instruction and the shipped default disagree" | `b54dd19` | `README.md:198-245` gained *"Before you expose it: what this actually opens"*, and it is accurate on every point checked, including that the release stack interpolates the port variable while `docker-compose.yml` hardcodes its mapping. **VERIFIED BY RUNNING IT**: `LAZYAF_BACKEND_PORT=127.0.0.1:8000 LAZYAF_FRONTEND_PORT=127.0.0.1:5173 docker compose -f docker-compose.release.yml config` emits `host_ip: 127.0.0.1` on **both** published ports. The README even tells the reader to verify with `docker compose config` rather than trust the paragraph. *(The binding half is still open — see S1-bind.)* |
| **T24 — unbounded `commits?limit`** | — | `routers/repos.py:373` `max_count=min(limit, 100)`; `?limit=999999` returns normally on the sandbox. *(Missing lower bound remains — see T24.)* |
| **QA3-13 — a single `GET /api/pipeline-runs` took 0.6-1.6 s on a nearly-empty database** | — | The strict xfail at `tdd/qa/test_qa3_concurrent_readers.py:93` now **XPASSes**, which is the proof it is fixed. The marker must be removed — see L3-4. **A ledger that only grows is as useless as one that lies: this one shrank and nobody noticed for lack of a gate.** |
| **T23 — step/runner JWT secrets defaulted to published constants** (re-confirmed 2026-08-31) | `acb7408` | `graph_definition_errors` could not even be imported without a real secret: `app/config.py:245` raised `MissingSecretError` with the full remediation text, and only `LAZYAF_DEV_EPHEMERAL_SECRETS=1` got past it, printing the intended warning. The refusal works exactly as claimed |
| **The four `5334b09` xfail flips stayed flipped** (re-counted 2026-08-31) | `5334b09` | `tdd/qa/test_demo_polish_api.py` → 11 passed, zero xfail (the three removed markers were the pipeline-name length bound and the two repo-name emptiness cases); `test_graph_definition_qa4.py::test_empty_pipeline_name_is_rejected` → 1 passed; whole file 10 passed / 10 xfailed, **zero xpass**. Everything else executable still xfails honestly: `test_yaml_pipelines_qa4` + `test_qa5_card_state_machine` + `test_qa5_timestamps` + `test_pipeline_export_qa4` → 11 passed / 17 xfailed, zero xpass. Marker census: 44 in `tdd/qa`, 42 strict, 2 non-strict — and **both non-strict ones are the honest use**: `test_graph_execution_qa4.py:83` (QA4-05) and `test_qa3_state_races.py:225` (QA3-14) assert absolute wall-clock bounds and both XPASSED on this idle host, exactly as their own reason text predicts ("a strict marker therefore lies in BOTH directions depending on load") |
| **The parts of the CI gate that DO work** — stated so the L3-1/L3-2 fixes do not get over-scoped into a rewrite | — | The gate refuses red input however it is invoked (`ci_gate.py:116-123`, with the comment explaining it was added because a direct invocation once read "GATE OK" beside `failed=3`); the floor catches a run that executed nothing but DID write a report (demonstrated live: `-- --collect-only` → `FAIL - executed count 0 below committed floor 4432`); the floors match the artifacts and both baselined skips match a `reason_prefix` in `tdd/skip_baseline.json`; `run_tier.py:176-179` returns red before ever calling the gate, so a genuinely failing pytest is never laundered. **The three gate defects above are narrow additions to sound core logic** |
| ~~T1 — every timestamp naive UTC~~ **RE-OPENED as PARTIAL 2026-08-31** | `db5f9f5` (REST path only) | Closed on the pydantic/REST path — measured, REST ships `+00:00`. **Open on 23 live hand-built wire payloads**, including every pipeline-run and step-run WebSocket frame. See T1 in Correctness. *A "verified closed" entry that overstated is the exact failure this survey exists to catch, so this one matters more than its effort suggests.* |
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
- Committed head is **`0012_workspaces_per_worker`** (there is no `0008`) —
  corrected 2026-08-31; it landed in `08e356d` and
  `tdd/integration/test_migrations.py:42` pins it. **One** wave now wants the
  next id: 12.8 P4 takes **`0013`**, P6 takes **`0014`**. Keep doing what caught
  this: **check `git ls-files backend/alembic/versions/` before you generate**,
  not just the directory listing — and note that
  `upcoming/wave10-v1-retirement.md` still hardcodes `0012`/`0013` in 14 places
  (see the P3 carry-forwards above).

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
  the retirement, not a unit test. **NOT DONE** (checked 2026-08-31 because
  `b54dd19` edited this file): `.lazyaf/pipelines/test-suite.yaml:31` still
  declares `steps:` as a YAML **list** (`- id: "secret-scan"` at `:62`), with no
  `version: 2` and no `entry_points:`. `b54dd19` added the secret-scan step at
  the front and kept the v1 format — so the file this must convert is now **one
  step longer (11)**.
- Retire completed phase sections to `historical-documents/`. **Done
  2026-08-30**: the 12.0-12.7 narrative moved to
  [`historical-documents/phase-12-runner-architecture.md`](historical-documents/phase-12-runner-architecture.md),
  taking this file from 3,858 lines to **about 45%** of that (~1,744 lines before
  this reconcile). *(Earlier revisions said "roughly a third"; the arithmetic was
  wrong.)*
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
> **The workspace blocker is CLOSED** (`08e356d`, verified 2026-08-31 — see the
> closed table). 13.2's "a branch and a workspace per worker" substrate exists:
> the composite `(pipeline_run_id, worker_key)` unique index replaced the
> per-run one, migration `0012` is committed, and run-to-run isolation was
> checked and is intact.
>
> **Two blockers stand in its place, and the first is a hard gate on 13.1:**
> (1) the oracle only records `@pytest.mark.lazyaf_test_id`, which no upstream
> repo carries (`runner_common/pytest_lazyaf.py:161-163`, `:210-211`) — fix
> specified as `LAZYAF_TEST_ID_MODE=nodeid` in
> `docs/milestone-13/leaderboards-and-corpus.md:287-322`, zero implementation;
> (2) 13.1's exit gate demands every `fail_to_pass` test FAIL at
> `base_commit_sha`, while the design's own normal case is that the test does
> not exist there yet (`leaderboards-and-corpus.md:261-285`). Both are stated in
> full in [What to do next §2](#2-then--milestone-13-the-benchmark--evaluation-harness).
>
> **`docs/milestone-13/leaderboards-and-corpus.md` (Amendment A, `4f529e1`) is an
> adversarial review of the design below and supersedes it where they
> disagree** — repo pinning by name and the oracle id mode at minimum. The body
> below has not been rewritten to match.


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
| `docs/milestone-13/leaderboards-and-corpus.md` **(Amendment A, `4f529e1` — added to this table 2026-08-31; it had been missing)** | An **adversarial review of the three documents above and of this plan's M13 body**, which it contradicts in at least two places (repo pinning by name; the oracle id mode). It holds both live M13 blockers: `LAZYAF_TEST_ID_MODE=nodeid` (§2.5, `:287-322`) and the `fail_to_pass`-missing-at-base contradiction (`:261-285`), plus the corpus on-disk format, the `lazyaf-oracle` binary that was specified nowhere, and the bundle-hash bug that must be fixed before the first published bundle. **Read it before starting 13.1.** |

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
   **CONTESTED, 2026-08-31 — reconcile before writing the validator.** The
   sentence above is the unqualified version, and
   `docs/milestone-13/leaderboards-and-corpus.md:261-285` shows it is
   unsatisfiable for the design's own declared normal case: a `fail_to_pass`
   test "may not exist yet at `base_commit_sha`", and a missing test observes
   `missing`, not `failed`. Either the test must be present-and-red at base (and
   the corpus author supplies it, not the agent), or `missing` becomes a third
   state with its own rule. Cheap to decide now, expensive after cases are
   authored. **And nothing here works at all until the oracle can see an
   unannotated repo** (`LAZYAF_TEST_ID_MODE=nodeid`).
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
