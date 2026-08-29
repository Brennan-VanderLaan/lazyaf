# Salvage Map

Verdict key: **PORT** = lift nearly as-is, **ADAPT** = keep the design/shape, rebuild on main's primitives, **REFERENCE** = read it when the phase starts, port nothing, **DISCARD** = superseded or broken.

## 12.2-integration (workspace persistence & wiring main's dark workspace libs)

**Bottom line:** failure_01 is the only place a DB-row↔Docker-volume lifecycle was ever implemented; take the schema and service *shape*, rebuild the service on main's tested lock manager and state machine, and note that nobody has ever written the repo-clone-into-volume step — that is net-new work for attempt #3.

| Component | Files (failure_01) | Verdict | Reason |
|---|---|---|---|
| Workspace DB model | `backend/app/models/workspace.py`, `models/pipeline.py` (workspace rel) | **ADAPT** | Schema slots directly onto main's `WorkspaceStatus` vocabulary; fix id truncation (`run_id[:8]` → full run id, matching main's `generate_volume_name`), drop or wire the never-written `state_history` column. |
| WorkspaceService | `backend/app/services/workspace_service.py` | **ADAPT** | Right lifecycle shape (idempotent get-or-create, cleanup on completion, orphan sweep) but riddled with defects: lock leak in acquire/release, `MissingGreenlet` lazy-load in orphan sweep, mid-op commits of caller's session, stranded CREATING rows. Rebuild on main's `workspace/locking.py` + `state_machine.py`. |
| Workspace hooks in pipeline_executor | `start_pipeline` workspace block, `_cleanup_workspace` | **REFERENCE** | Hook *placement* (create at run start, cleanup in `_complete_pipeline` before trigger actions) is correct; implementation creates empty volumes and leaks one volume per legacy run. |
| Workspace integration tests | `tdd/integration/execution/test_workspace_integration.py` | **ADAPT** | The missing real-Docker integration tier for main's 12.2 wiring; rework calls to main's API, keep the scenario list. |
| workspace_state.py, workspace_locking.py | `services/execution/workspace_state.py`, `workspace_locking.py` | **DISCARD** | Near-duplicates of main's tested versions; main's event-driven locking is strictly better than the 50ms poll loop. Only lift the ~40-line to_dict/from_dict snippet if state_history gets wired. |
| trigger_dedup.py, step_execution.py model, pipeline_state.py | `services/execution/`, `models/step_execution.py` | **DISCARD / REFERENCE** | All superseded by main's tested equivalents; pipeline_state.py's extras (on_failure='next', duration, serialization) are REFERENCE reading when main wires its own machine into the executor. |

**Ordering:** model → adapted service → executor hooks → integration tests. Decide the **clone strategy** (backend-side clone vs init container) before writing the service — it's the piece no branch ever built.

## 12.3-images (step images & in-container control layer)

**Bottom line:** the `images/` tree is the best code on the branch and directly supersedes *both* of main's half-baked control-layer variants (the string-generator and the broken `backend/docker/` copies) — port it in front of main's already-working `/api/steps/*` router, with three known contract fixes.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| Control runtime core | `images/base/control/{run,executor,heartbeat,config}.py` | **PORT** | Modular, readable, backed by 18 real unit tests; status/heartbeat payloads match main's endpoints verbatim. Fix while porting: enforce `timeout_seconds` (loaded, never enforced), rename `token`→`auth_token` and `working_dir`→`working_directory` to match main's producer, fix log flush on quiet processes. |
| Backend client | `images/base/control/backend_client.py` | **ADAPT** | Auth/URLs match main exactly, but the logs payload is contract-incompatible (plain strings vs main's `LogLine` objects — every batch 422s and is silently dropped). ~5-line fix plus `extend_seconds` on heartbeat plus capping the synchronous retry budget on the log path. |
| Base image | `images/base/Dockerfile`, `requirements.txt` | **PORT** | Bakes deps at build time (vs main's runtime `pip install httpx` hack). Carry over main's XDG/PIP cache env block; fix non-root-user vs root-owned-volume (chown at entrypoint). |
| Claude image | `images/claude/Dockerfile` | **PORT** | Correctly self-contained, unlike main's `backend/docker/claude` whose COPY context is broken as documented. |
| Test-runner image | `images/test-runner/Dockerfile` | **PORT** | Useful standalone CI image with no equivalent on main. One real bug: unquoted `pytest>=7.0` is parsed as a shell redirect — quote it. |
| Gemini image | `images/gemini/Dockerfile` | **DISCARD** | Installs an SDK for a `gemini_agent` module that exists nowhere; derive a future gemini image from runner-common's tested executor instead. |
| Debug-sidecar image | `images/debug-sidecar/Dockerfile` | **REFERENCE** (park) | Two analysts split PORT/REFERENCE — resolution: the file is trivially copyable, but its only consumer is 12.7; park it and copy when 12.7 starts. Not a 12.3 deliverable. |

**Ordering:** base image + control core + backend client together (they're one contract), then claude/test-runner images. **Add a build story** — no compose service, script, or CI ever built these tags on failure_01; everything assumed `:latest` pre-existed.

## 12.4 (execution routing / local dispatch)

**Bottom line:** the dispatch seam in `pipeline_executor.py` is the one thing failure_01 genuinely maps that main still lacks — keep it as the map of where to cut, but the execution model (synchronous, inside the HTTP request) must be inverted to `asyncio.create_task` per run.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| pipeline_executor local-dispatch seam | `backend/app/services/pipeline_executor.py` (+438 vs merge-base) | **REFERENCE** | Right seam (route → local vs legacy split, legacy preserved verbatim), fatally wrong model: entire pipeline awaited inside git-push/HTTP handlers, logs buffered until step end, attempt hardcoded to 1, REMOTE decisions silently fall through to the job queue. |
| ExecutionRouter | `services/execution/router.py` | **ADAPT** | Clean, stateless, 36 real tests — but main has its own richer router stub; merge the `requires:` parsing (arch/has/runner_id) and arch normalization into main's `decide()` shape rather than porting wholesale. |
| config_builder | `services/execution/config_builder.py` | **ADAPT** | Sound factoring (step_config → ExecutionConfig, image defaults, env merging, API-key injection); every constant is hardcoded and `host.docker.internal` is wrong on Linux/compose — make it settings-driven against main's control layer. |
| Routing/migration-compat contract tests | `tdd/unit/execution/test_step_routing_contract.py`, `test_migration_compatibility.py` | **ADAPT** | The back-compat matrix (old YAML, no image key, multiline commands, all three step types) is exactly the regression net for rewiring; merge assertions into main's router test file. |
| LocalExecutor + idempotency + step_state + step_token + steps router | `services/execution/local_executor.py`, `idempotency.py`, `step_state.py`, `step_token.py`, `routers/steps.py` | **DISCARD** | Main rewrote all of these better (DB-backed, real-Docker-tested, working auth). Keep only the pitfall list: the `.control`-dir-written-to-CWD bug, volume-detection-by-leading-slash, and the log-streaming thread pattern. |

**Ordering:** depends on 12.2 (populated workspaces) and 12.3 (real images) landing first — routing steps into empty volumes with missing images is precisely how attempt #1 died.

## 12.5 (agent steps via control layer)

**Bottom line:** take the config-file *contract*, rebuild the wrapper as a thin shim over main's runner-common (100 tests) instead of porting the 520-line untested monolith.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| agent_wrapper.py | `images/base/control/agent_wrapper.py` | **ADAPT** | Right shape (in-container wrapper behind the control layer reading the same config file); body duplicates runner-common badly — dead gemini path, token-bearing git URL leaked into logs, silent deletion of diverged remote branches, path contradictions. Rebuild as a shim over runner-common baked into the image. |
| Agent-step contract test | `tdd/unit/execution/test_agent_step_contract.py` | **ADAPT** | The enumerated config-file contract (title/description/model/agent_file_ids/previous_step_logs/repo_url/branch) is the 12.5 spec; re-target assertions onto main's control-layer protocol. |
| test_polling_removal / test_card_local_execution | `tdd/unit/execution/` | **DISCARD** (idea is REFERENCE) | Test theater (`pass # Architecture ensures this`), and self-skipping since aa72cd2 deleted its import target. Write the *idea* fresh: a spy asserting pipeline_executor never enqueues locally-executed steps to job_queue. |

## 12.6 (remote runners)

**Bottom line:** the highest-value salvage on the whole branch is the 12.6 *contract test suite*; the implementations are 40%-done sketches whose skeletons (state machine, ACK-future pattern, WS endpoint flow) are worth keeping.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| 12.6 contract tests | `tdd/unit/execution/test_runner_state_machine.py`, `test_websocket_protocol.py`, `test_job_recovery.py` (1,770 loc, 137 tests) | **PORT** | A finished 12.6 spec in test form (lifecycle table, wire messages, timeouts, recovery scenarios); skipif harness means they drop onto main dormant and become the acceptance suite. Mechanical import-path fixes only. |
| RunnerStateMachine | `services/execution/runner_state.py` | **PORT** | Matches main's state-machine idiom, 660-line genuinely thorough test file. Fix: add IDLE→DEAD (silently-dead idle runner) and DEAD→DISCONNECTED (the disconnect-after-death crash path). |
| runner_protocol.py | `services/execution/runner_protocol.py` | **ADAPT** | Clean message set and timeouts as the starting shape; add auth (reuse main's step-token pattern), protocol version, cancel message, and a real `execute_step.config` schema; fold in the ad-hoc `ping`. |
| RemoteExecutor | `services/execution/remote_executor.py` | **ADAPT** | Keep the connection-registry + ACK-future + death-monitor skeleton; rebuild everything else — step results never persisted, heartbeat-death strands the step, DB status never leaves `idle` (double-assign race), requeued PENDING steps have no dispatcher. |
| /ws/runner endpoint | `routers/ws_runners.py` | **ADAPT** | Only wired-and-functioning 12.6 piece; rewrite with per-message DB sessions (not one session per multi-hour connection) and token auth. |
| Runner model + migration | `models/runner.py`, `alembic/versions/a1b2c3d4e5f6_*` | **ADAPT** | Column design (name/type/labels/websocket_id + `matches_requirements`) is exactly what PLAN 12.6 needs; re-author the migration against main's chain, update the never-touched schemas. |
| RemoteExecutor contract tests | `tdd/unit/execution/test_remote_executor_contract.py` | **ADAPT** | Right scenario list, wrong coupling (pokes private dicts, patches privates); rewrite against the public API. |
| runner-agent package | `runner-agent/` | **REFERENCE** | Connect/register/heartbeat/reconnect loop and env/CLI config surface worth imitating; zero tests, no auth, inline execution blocks the receive loop. |
| docker_orch.py | `runner-agent/lazyaf_runner/docker_orch.py` | **DISCARD** | Provably broken (`list(coroutine)` TypeError in log reader), no workspace mounting, hardcoded host networking. A future agent embeds LocalExecutor/runner-common. |
| job_recovery.py | `services/execution/job_recovery.py` | **REFERENCE** | Sound principles (DB as truth, idempotent requeue, reconnect protocol), 100% unwired, unhandled requeue-vs-late-completion race, and "requeued" = stranded without a dispatcher. |
| WS runner UI + store | `frontend/src/lib/stores/runners.ts`, `RunnerPanel.svelte` | **REFERENCE** | Push-driven Map store pattern is right; it deleted the working HTTP path and has no initial-state snapshot on connect (empty panel on page reload). Rebuild with a snapshot fetch + WS deltas when 12.6 lands. |

**Ordering:** contract tests + state machine first (they define the spec), protocol, then model/migration, then executor+endpoint together, agent last. **Do not delete the polling stack until the push path passes the ported contract suite end-to-end.**

## 12.7 (debug re-run)

**Bottom line:** a facade — schemas/model/UI/CLI all exist while the three load-bearing pieces (starting the debug run, the breakpoint broadcast, terminal I/O bridging) are respectively missing, arity-broken, and a TODO stub; shelf the clean leaf artifacts and rebuild the rest from spec after 12.2–12.5.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| Debug state machine + tests | `services/execution/debug_state.py`, `test_debug_session_state_machine.py` | **PORT** (shelf) | Clean, dependency-free, mock-free tests with real density (33 tests/83 asserts); cherry-pickable when 12.7 starts. |
| Debug schemas | `backend/app/schemas/debug.py` | **PORT** (shelf) | Clean API contract; revisit the token-in-GET-response oracle. |
| Debug-sidecar image | `images/debug-sidecar/Dockerfile` | **PORT** (shelf) | Trivial tools image; copy when 12.7 starts. |
| DebugSession model + migration | `models/debug_session.py` | **ADAPT** | Sensible schema; dedupe the status enum with debug_state.py, renumber migration. |
| DebugSessionService | `services/execution/debug_session_service.py` | **ADAPT** | Keep the lifecycle skeleton/API surface; fix the fatal gaps — `create_debug_rerun` never starts the run, `resume` ends the session (killing multi-breakpoint), timeout monitor never started, in-memory/DB dual truth. |
| CLI `lazyaf debug` | `cli/lazyaf/cli.py` | **ADAPT** | Async duplex loop + @command UX is a usable start; needs raw-TTY mode and a server that actually speaks its protocol. |
| Debug router, terminal service, executor breakpoint hooks | `routers/debug.py`, `debug_terminal.py`, executor hunks | **REFERENCE / DISCARD** | Router's WS command loop is dead code inside `except ImportError`; terminal bridging is a TODO stub with zero callers; breakpoint hook has a guaranteed `broadcast()` TypeError and hooks the executor loop main is replacing. Only the sidecar-vs-shell split and "breakpoint = pre-step gate" survive as design. |
| Frontend debug UI | `DebugPanel.svelte`, `DebugRerunModal.svelte`, `stores/debug.ts` | **REFERENCE** | Two analysts split ADAPT/REFERENCE — resolution: REFERENCE today (nothing on main serves its endpoints), and mine `DebugRerunModal` (the most finished piece) plus the UX design (breakpoint checkboxes, countdown, join command) at 12.7 time. |
| Debug e2e skeletons + fixtures | `tdd/frontend/e2e/stories/06-debug-rerun/` | **REFERENCE** | All 7 files are `test.skip` shells, and fixture status vocabulary doesn't match the backend enum from the same commit; keep the titles as a UX checklist. |

## Cross-cutting

**Bottom line:** the migration scaffold, the ops post-mortem doc, and the test-selector sweep are cheap, phase-independent wins; the e2e harness is reusable once its reset race is fixed.

| Component | Files | Verdict | Reason |
|---|---|---|---|
| upcoming/sprawl.md | `upcoming/sprawl.md` (521 loc) | **PORT** | The author's real-time post-mortem; almost entirely still applicable to main (create_all, in-memory job_queue, recovery gaps). Copy verbatim as attempt-#3 design input. |
| Alembic scaffold | `backend/alembic.ini`, `alembic/env.py`, `script.py.mako` | **PORT** | Main hand-rolls schema evolution with `PRAGMA`+`ALTER`+bare-except hacks (4 already in `database.py`). Port conditions: regenerate a clean baseline against main's models, and actually run `alembic upgrade head` at startup — failure_01 never invoked it. |
| Alembic version files | `alembic/versions/*` (all 4) | **DISCARD** | Drifted from main's schema (missing steps_graph, active_step_ids, execution columns); regenerating is cheaper and safer. |
| data-testid sweep | ~12 Svelte components (commit 6e0c043) | **PORT** | Mechanical, zero-behavior, applies nearly cleanly (main hasn't touched most files since merge-base d41bb1e); prerequisite for any Playwright work. Skip PipelineEditor hunks (superseded by main's graph editor). |
| vite.config.ts proxy env var | `frontend/vite.config.ts` | **PORT** | 10-line QoL, defaults preserve current behavior. |
| Test-mode API | `backend/app/routers/test_api.py` + config/main hunks | **ADAPT** | Right pattern (env-gated reset/seed); rebuild against main's models, add an in-memory-state reset hook, and either wire or drop the mock-AI flag (it had zero callers). |
| Playwright harness + docker test stack | `tdd/frontend/playwright.config.ts`, helpers, `docker-compose.test.yml`, `scripts/test.*` | **ADAPT** | Solid skeleton with one disqualifying race (fullyParallel + per-test global DB reset) and one gap (backend webServer commented out). Serialize the real tier or namespace per worker. |
| Card-lifecycle real e2e specs | `tdd/frontend/e2e/stories/02-card-lifecycle/` | **ADAPT** | The only skeletons ever converted to real assertions; target flows main still has; never verified green — treat as drafts. |
| E2E story corpus | `tdd/frontend/e2e/**` (~40 files) | **REFERENCE** | 556 of 676 tests are empty `test.skip` shells; mine the describe/test *titles* — the critical-failures and realtime-sync taxonomy is a genuinely good UX checklist. |
| MockWebSocket + WS fixtures | `tdd/frontend/fixtures/` | **ADAPT** | MockWebSocket is near-PORT quality; regenerate message fixtures from whatever protocol main actually ships. |
| tdd/shared chaos/mock library | `tdd/shared/` (~1,900 loc) | **ADAPT** (lazily) | Decent utilities imported by *zero* tests on the branch; port individual pieces only when a consuming test needs them. |

---

# Post-Mortem: why attempt #1 collapsed

**Process causes:**

1. **Velocity without verification.** Git log: phases 12.2, 12.3, 12.4, 12.5, 12.6 landed at 17:02, 17:52, 18:24, 19:04, 21:53 on Jan 3 — 32–50 minutes per 1.4k–5.3k-line phase. PLAN.md marked each COMPLETE with claimed pass counts while load-bearing code carried TODOs: `debug_terminal.py:196` "TODO: Implement full terminal I/O bridging" (the entire point of 12.7), `trigger_dedup.py:245-265` "TODO: Implement proper async database query" (dedup never persisted).

2. **Fake-green test architecture.** Three mechanisms let the suite stay green over dead code: (a) the try-import + `pytest.mark.skipif` harness silently *skipped* whole files when their targets were deleted — `test_polling_removal.py` imported `runner_pool`, deleted by aa72cd2 ~3.5h after the test was written, self-skipping from then on; (b) outright theater — `pass # Architecture ensures this` bodies, 5 bare-pass tests in `test_card_local_execution.py`, ~1,400 lines of chaos/mock helpers imported by zero tests; (c) mock-away-the-broken-seam — breakpoint tests AsyncMock the WS manager, hiding a guaranteed `broadcast()` arity TypeError; 136 workspace unit tests exercised classes the production path never called.

3. **Delete-before-replace.** Commit aa72cd2 ("12.6") deleted the *working* polling stack — `runner_pool.py` (−227), `routers/runners.py` (−408), and 992 lines of their passing tests — in the same commit that introduced an unproven push replacement that nothing ever dispatched to. From that commit forward, agent steps enqueued into a queue nothing dequeued: the system could no longer execute agent steps at all, 12.7 was then stacked on top ("12.7 w/ broken ui", 580f259), and the branch died two working sessions later.

**Technical causes:**

4. **The feature flag hid five phases of dark code.** `LAZYAF_USE_LOCAL_EXECUTOR` defaulted to `'0'` and appears in no compose file — every green run through 12.6 exercised the *legacy* path. The moment the flag mattered, four independent production-only failures would fire at once: empty workspace volumes (no code anywhere clones the repo — "clone" appears only in docstrings), `step_config.json` + auth token written to the backend's CWD instead of the volume (`Path(volume_name)/".control"` in `local_executor.py:132-201`), whole pipelines executing synchronously inside git-push HTTP handlers, and logs/telemetry keyed by execution keys the UI can't correlate.

5. **Integration tests that dodged the integration.** `test_local_executor_docker.py` used tmp_path *bind mounts* exclusively, so the volume-name mount path, the .control-directory write, and `host.docker.internal` — the exact three seams that were broken — were never exercised. Frontend fixtures were speced with a different debug-status vocabulary than the backend enum *in the same commit* (parallel generation, never reconciled).

6. **State drift, self-diagnosed but not fixed.** `upcoming/sprawl.md` (written during the collapse) names it: DB lost on compose down, `create_all()` silently ignoring schema changes (alembic was installed but never invoked — its "migrations were dead weight from day one"), in-memory state (job_queue, runner connections, token store) diverging from DB truth. The author wrote a 521-line design doc for the fixes instead of making them, then the final commit (9ed25e4, "fixed test db reset", 13:32 Jan 4) raised Playwright CI workers from 1 to 50% against a shared-DB per-test global reset — worsening the flake one hour before abandonment (tip 6e0c043, 14:34).

---

# Landmines for attempt #3

1. **The flag-gated dark path.** Then: new execution path behind an env flag set nowhere; all e2e green on the legacy path while 5 phases of untested code accumulated. Now: when wiring main's dark libraries (12.2/12.3), flip the wiring on in the default dev compose *immediately* and make the e2e suite run through it — if the new path can't carry the existing user stories on day one, stop and fix rather than stacking the next phase.

2. **Named-volume vs host-path confusion at every seam.** Then: `Path(volume_name)/".control"` wrote configs to the backend's CWD; volume detection by leading `/` misclassifies Windows paths; `host.docker.internal` hardcoded (wrong on Linux and in compose). Now: make workspace addressing an explicit type (volume vs bind), deliver control files *into* the volume via a helper container or docker cp, make backend_url settings-driven, and write at least one integration test that uses a **named volume**, not tmp_path bind mounts.

3. **Nobody has ever cloned the repo into the workspace.** Then: 12.2's "persistent workspace with git checkout" never existed — volumes were created empty and only agent steps cloned (from inside the container); script/docker steps ran against nothing, producing pipelines that "ran" but did nothing. Now: decide and implement the population strategy (backend-side clone vs init container) as the *first* 12.2 deliverable, with an e2e that asserts a script step can `ls` real repo files.

4. **Synchronous pipelines inside request handlers.** Then: `start_pipeline` awaited directly from POST /run and git-push handlers, with `_handle_action('next')` recursing — a push blocked until the last container exited, holding one AsyncSession throughout. Now: `asyncio.create_task` per run with its own session scope from the start; the request returns a run id, everything else streams over WS.

5. **Delete the old path only after the new one passes acceptance.** Then: aa72cd2 removed polling runners + their 992 test lines before push dispatch existed; steps enqueued into an orphaned queue forever. Now: keep the legacy job-queue path callable until the ported 12.6 contract suite passes against the real push path end-to-end; delete in its own commit with the frontend migrated in the same commit.

6. **skipif-import test harness = silent decay.** Then: renamed/deleted modules turned whole test files into silent skips on the branch itself. Now: if the TDD-scaffold pattern is kept, add a CI gate that fails when the *count of skipped-for-import files increases*, or use `xfail(strict=True)` markers that scream when targets vanish.

7. **AsyncSession lazy-loads and event signatures are production-only bombs.** Then: two `MissingGreenlet` lazy-load bugs (orphan sweep, `_cleanup_workspace` — silently leaking a volume per run) and a `broadcast()` arity TypeError, all invisible because tests mocked the session/manager. Now: `selectinload` every relationship touched in async services, and give the WS manager a typed publish API so arity mistakes are import-time errors; never mock the manager in tests that exercise broadcast paths.

8. **Shared-backend e2e with global reset + parallel workers.** Then: `fullyParallel: true` with per-test `POST /api/test/reset` against one DB — workers wiped each other mid-test; and reset cleared the DB but left in-memory state (job_queue, runner connections, WS manager) pointing at deleted rows. Now: real-backend tier runs `workers: 1` (or per-worker namespacing), and the reset endpoint must also reset in-memory singletons via an explicit hook.

9. **Contract fidelity between producer and consumer.** Then: control-layer client sent log lines as plain strings against a `LogLine`-object schema (every batch 422'd, silently dropped — `send_logs` return ignored), `token` vs `auth_token` key mismatch, frontend/backend debug status vocabularies diverged in one commit. Now: one source of truth per wire contract (pydantic schemas or the ported protocol tests), with a round-trip test that a real container's log batch lands in a real StepRun row.

---

# Quick wins

In order:

1. **`upcoming/sprawl.md` → main.** `git show failure_01:upcoming/sprawl.md > upcoming/sprawl.md`. Zero risk, ~15 minutes, and it's the design input for half of attempt #3's recovery/persistence decisions.

2. **data-testid sweep + vite proxy config.** Cherry-pick the instrumentation hunks from 6e0c043 across ~12 Svelte components (skip PipelineEditor; hand-apply equivalents in main's graph editor) plus the 10-line `vite.config.ts` change. Mechanical, zero behavior change, applies nearly cleanly since main hasn't touched most of these files since d41bb1e. ~2-3 hours, unblocks all future Playwright work.

3. **12.6 contract test suite + RunnerStateMachine.** Port `test_runner_state_machine.py`, `test_websocket_protocol.py`, `test_job_recovery.py` (skipif keeps them dormant on main) and `runner_state.py` with the two missing transitions (IDLE→DEAD, DEAD→DISCONNECTED). This is a finished 12.6 spec for free. ~half a day.

4. **Alembic scaffold with a regenerated baseline.** Port `alembic.ini`/`env.py`/`script.py.mako`, discard all four version files, autogenerate a clean baseline against main's models, and invoke `upgrade head` at startup — replacing the four `PRAGMA`/`except: pass` hacks in `database.py`. ~half a day; do it before 12.2 adds the workspaces table so that table is born as a real migration.

5. **`images/` tree in front of main's steps router.** Port base image + control runtime + backend client with the three contract fixes (LogLine wrapping, `auth_token`/`working_directory` renames, timeout enforcement), plus claude and test-runner images (quote the pytest pin), and add a build script/compose service. ~1-2 days including a real named-volume integration test; retires both of main's half-baked control-layer variants and is the 12.3 wiring prerequisite.