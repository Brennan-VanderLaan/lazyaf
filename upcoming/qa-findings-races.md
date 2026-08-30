# QA-3 — Concurrency and races

**Lane:** QA-3 (concurrency, races, duplicate side effects, teardown)
**Target:** isolated QA sandbox at `http://localhost:8790` (compose project `lazyaf-qa`)
**Date:** 2026-08-30
**Method:** genuinely simultaneous requests released from a barrier (threads and asyncio),
correlated with `docker logs lazyaf-qa-backend-qa-1` and `docker volume ls`.
No source, test, config, or compose file was edited.

Regression tests: `tdd/qa/test_qa3_*.py` + `tdd/qa/qa3_support.py`.
Run them with:

```
LAZYAF_QA_BASE_URL=http://localhost:8790 python -m pytest -c tdd/qa/pytest.ini tdd/qa/test_qa3_workspace_race.py tdd/qa/test_qa3_duplicate_starts.py tdd/qa/test_qa3_state_races.py tdd/qa/test_qa3_terminal_run.py tdd/qa/test_qa3_concurrent_readers.py
```

Every test encoding a defect is `@pytest.mark.xfail(strict=True)` so it turns into a
failure the moment the bug is fixed. Four tests are deliberately NOT xfail — they lock in
behaviour that is already correct.

> **Environment caveat.** The QA sandbox is shared with four sibling QA lanes that reset it
> and push malformed data into it continuously. Every number below was re-measured in an
> isolated, self-contained probe after a reset. Two observations that belong to other lanes
> are listed at the very bottom so they are not mistaken for mine.

---

## Ranked findings

| # | Severity | One line |
|---|----------|----------|
| QA3-1 | BLOCKER | Two parallel steps of one run destroy each other's workspace volume; ~1 run in 3 fails spuriously |
| QA3-12 | BLOCKER | 20 simultaneous `GET /api/pipeline-runs` return HTTP 500 with only ~300 step rows in the DB |
| QA3-3 | BLOCKER | Simultaneous `POST /cards/{id}/start` all win: N jobs, N runs, N branches for one card |
| QA3-8 | MAJOR | `start` racing `delete` on one card is a guaranteed HTTP 500 (`StaleDataError`) |
| QA3-9 | MAJOR | Any unhandled 500 poisons the keep-alive connection — the *next* request dies too |
| QA3-10 | MAJOR | A run keeps dispatching steps after it has broadcast a terminal status |
| QA3-4 | MAJOR | `POST /cards/{id}/retry` has the same unguarded check as `start` |
| QA3-6 | MAJOR | `approve` has no state guard: a `todo` card goes straight to `done` |
| QA3-7 | MAJOR | Simultaneous `approve` + `reject` are both accepted; last writer decides |
| QA3-5 | MAJOR | Concurrent duplicate-name `POST /api/prompt-templates` → 500 instead of 409 |
| QA3-2 | MAJOR | Raw Docker client errors (URL, API version, container id) leak into step errors shown in the UI |
| QA3-13 | MAJOR | A single `GET /api/pipeline-runs` takes 0.6–1.6 s on a near-empty DB (`echo=True` on the engine) |
| QA3-11 | MINOR | A finished run reports `current_step=0` while all its steps are listed complete |
| QA3-14 | MINOR | Simultaneous cancels intermittently return 500 (load-dependent; see caveat) |
| QA3-15 | MINOR | `POST /api/test/reset` while a run is live orphans that run's Docker volume |
| QA3-16 | MINOR | Cancelling a run during workspace provisioning leaks the volume |
| QA3-17 | MINOR | Deleting a pipeline/repo mid-run returns 204 and cascades the run row away under a live container |
| QA3-18 | MINOR | Two simultaneous `DELETE /api/repos/{id}` both return 204 |
| QA3-19 | POLISH | The seeded `in_review` card points at a branch that does not exist, so `approve` on it always 400s |

---

## QA3-1 — BLOCKER — Parallel steps of one run destroy each other's workspace

**Repro**

```bash
REPO=$(curl -s -X POST localhost:8790/api/test/seed | python -c 'import sys,json;print(json.load(sys.stdin)["repo"]["id"])')
PIPE=$(curl -s -X POST localhost:8790/api/repos/$REPO/pipelines -H 'content-type: application/json' -d '{
  "name":"race","description":"","steps":[],"triggers":[],
  "steps_graph":{"version":2,"edges":[],"entry_points":["s0","s1"],"steps":{
    "s0":{"id":"s0","name":"S0","type":"script","config":{"command":"echo ok"}},
    "s1":{"id":"s1","name":"S1","type":"script","config":{"command":"echo ok"}}}}}' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
RUN=$(curl -s -X POST localhost:8790/api/pipelines/$PIPE/run -d '{}' -H 'content-type: application/json' \
  | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
sleep 15
curl -s localhost:8790/api/pipeline-runs/$RUN | python -m json.tool
```

Repeat ~5 times.

**What happened** — roughly 1 run in 3 (6/18 measured across three step-timing shapes) comes
back `failed` even though both steps are `echo ok`. The failing step carries:

```
local execution error: 409 Client Error for
http+docker://localhost/v1.54/volumes/lazyaf-ws-42d4b70c-2195-438c-9ba4-42854c7a3aa6:
Conflict ("remove lazyaf-ws-42d4b70c-...: volume is in use -
[12d385f5c4f0cfa8412a04297abb1f56b5d3ce67db80aebca71cb41ae697fe74]")
```

and the backend log carries, for every single failing run id:

```
WARNING app.services.workspace_service - Replacing stale workspace row
        1bb0e03d-9078-415f-b02c-56354bed0cfa (status=creating)
        for run 42d4b70c-2195-438c-9ba4-42854c7a3aa6
```

**What should happen** — a graph whose steps all succeed passes, every time. Two steps of the
same run share one volume by design (`lazyaf-ws-<run_id>`); the second arrival must wait for
the first provision, not delete it.

**Root cause read** — `backend/app/services/workspace_service.py:312-320`. `get_or_create`
re-checks the row under the volume lock and treats anything that is not
`READY`/`IN_USE`/`CLEANING` as replaceable:

```python
# CREATING (stranded), FAILED, or CLEANED: replace the row.
logger.warning("Replacing stale workspace row %s (status=%s) for run %s", ...)
await run_in_threadpool(self._sync_remove_volume, volume_name)
```

`CREATING` is only evidence of a crash when it is *old*. A `CREATING` row a second old means a
sibling step is provisioning right now, and `_sync_remove_volume` uses `force=True`
(`workspace_service.py:210`), so it tears the volume out from under the live population
container. Docker refuses with 409 when the container is still attached — which is the
*lucky* case; when the population container has already exited, the removal succeeds and the
first step's populated workspace is silently discarded and re-created.

Suggested fix: give the `CREATING` branch an age threshold (reuse `stuck_threshold`, the
15-minute value `audit_orphans` already uses), and treat a fresh `CREATING` row as "wait and
re-read" rather than "stranded".

**Test** — `tdd/qa/test_qa3_workspace_race.py::test_parallel_entry_points_do_not_destroy_the_shared_workspace`
(10 trials, so a clean run is ~1.5% likely).

---

## QA3-2 — MAJOR — Raw Docker client errors are shown to the user

The step error above is written verbatim into `StepRun.error` and returned by
`GET /api/pipeline-runs/{id}`, so it is what a viewer reads in the run panel: an internal
socket URL, the Docker API version, and a 64-hex container id. During a demo this is the
worst kind of failure text — long, unwrapped, and meaningless to the audience.

**What should happen** — "Workspace for this run was removed while the step was running"
(or similar), with the Docker detail in the log only.

**Test** — `tdd/qa/test_qa3_workspace_race.py::test_step_errors_never_leak_raw_docker_client_text`.

---

## QA3-3 — BLOCKER — Simultaneous card `start` produces N runs

**Repro** — fire 5 `POST /api/cards/{id}/start` from a barrier (see
`tdd/qa/qa3_support.py::fire_together`). Measured, with all requests released in the same
instant:

| simultaneous starts | accepted (200) | pipeline runs created |
|---|---|---|
| 2 | 2 | 2 |
| 3 | 3 | 3 |
| 5 | 5 | 5 |
| 10 (staggered by network) | 3 | 3 |

**What happened** — every request created its own `Job`, its own hidden ad-hoc `Pipeline`, its
own `PipelineRun`, and its own `lazyaf/<job8>` branch name. The card keeps only the **last**
branch name, so the other branches are orphaned with no row pointing at them.

**What should happen** — exactly one start wins; the rest get the 400 the endpoint already
knows how to return.

**Root cause** — `backend/app/routers/cards.py:291-355`. Read-then-check with no row lock:

```python
result = await db.execute(select(Card).where(Card.id == card_id))
card = result.scalar_one_or_none()
if card.status != "todo":
    raise HTTPException(400, "Card must be in 'todo' status to start")
...                                   # ~6 awaits: repo lookup, agent-file validation
card.status = "in_progress"
await db.commit()
```

The guard is a TOCTOU window six awaits wide. `select(...).with_for_update()`, or a
conditional `UPDATE cards SET status='in_progress' WHERE id=? AND status='todo'` whose
rowcount decides, would close it.

**Test** — `tdd/qa/test_qa3_duplicate_starts.py::test_simultaneous_card_start_creates_exactly_one_run`.

---

## QA3-4 — MAJOR — `retry` has the same hole

`POST /api/cards/{id}/retry` (`backend/app/routers/cards.py:559`) repeats the pattern.
10 simultaneous retries on one card were **all** accepted and left 11 runs against that card.

**Test** — `tdd/qa/test_qa3_duplicate_starts.py::test_simultaneous_card_retry_creates_exactly_one_run`.

---

## QA3-5 — MAJOR — Concurrent duplicate-name create returns 500

**Repro** — 10 simultaneous `POST /api/prompt-templates` with the same `name`.

**What happened** — `{201: 1, 409: 8, 500: 1}`. The pre-check catches eight of them; one loses
the race and the `IntegrityError: UNIQUE constraint failed: prompt_templates.name` escapes as a
bare `500 Internal Server Error` (`text/plain`, body literally `Internal Server Error`).

**What should happen** — 409 for all nine losers. The `IntegrityError` needs catching around
the insert, not just a pre-check.

**Note** — the agent-files endpoint gets this right (20 simultaneous same-name creates →
`{201: 1, 400: 19}`, exactly one row). That correct behaviour is locked in by
`test_simultaneous_agent_file_create_yields_exactly_one_row`.

**Test** — `tdd/qa/test_qa3_duplicate_starts.py::test_simultaneous_prompt_template_create_never_returns_500`.

---

## QA3-6 — MAJOR — `approve` has no state guard

```bash
CARD=$(curl -s -X POST localhost:8790/api/repos/$REPO/cards -H 'content-type: application/json' \
       -d '{"title":"never started","description":"","step_type":"agent"}' \
       | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')
curl -s -X POST localhost:8790/api/cards/$CARD/approve -d '{}' -H 'content-type: application/json'
# -> 200, card.status == "done"
```

A card that was never started, has no branch and no work is moved to `done`. Approving it a
second time also returns 200.

**Root cause** — `backend/app/routers/cards.py:406-465`. Unlike `start`, `approve_card` never
looks at `card.status` at all; the merge is skipped when `card.branch_name` is falsy and the
handler falls straight through to `card.status = "done"`.

**Test** — `tdd/qa/test_qa3_state_races.py::test_approve_rejects_a_card_that_was_never_started`.

---

## QA3-7 — MAJOR — `approve` and `reject` both win

**Repro** — 6 requests from a barrier, alternating approve/reject, on one card.

**What happened** — both transitions are accepted every time; neither refuses the other. The
card is decided by whichever request commits last. In the seeded `in_review` case (a card that
does have a branch name) roughly 1 burst in 8 settles on:

```
status: "done",  branch_name: null
```

i.e. a card the board shows in **Done** that was in fact rejected, whose branch reference has
been cleared, and which never merged anything. `reject` also clears `pr_url`, so the audit
trail is gone.

**Root cause** — `approve_card` (`cards.py:406`) and `reject_card` (`cards.py:477`) both do a
plain read → mutate → commit with no row lock and no expected-state check.

**Test** — `tdd/qa/test_qa3_state_races.py::test_approve_and_reject_cannot_both_win_on_one_card`
(uses a branchless card so the race is deterministic rather than 1-in-8).

---

## QA3-8 — MAJOR — `start` racing `delete` is a guaranteed 500

**Repro** — create a card, then release `POST /cards/{id}/start` and `DELETE /cards/{id}`
in the same instant. Reproduced **8/8** and again **9/10**.

**What happened**

```
start  -> 500 Internal Server Error   (text/plain)
delete -> 204
```

Backend log:

```
sqlalchemy.orm.exc.StaleDataError: UPDATE statement on table 'cards'
expected to update 1 row(s); 0 were matched.
```

`start_card` loaded the card, spent six awaits validating, then committed against a row that
`delete_card` had removed in between.

**What should happen** — the loser gets `404 Card not found`. A row that vanished mid-request
is an ordinary outcome, not a crash.

**Root cause** — `backend/app/routers/cards.py:355` (`await db.commit()` in `start_card`). Same
missing row lock as QA3-3; here the consequence is an unhandled exception rather than a
duplicate.

`PATCH /cards/{id}` racing `DELETE` does **not** 500 (4/4 clean) — its window is much narrower.

**Test** — `tdd/qa/test_qa3_state_races.py::test_start_racing_delete_does_not_500`.

---

## QA3-9 — MAJOR — An unhandled 500 poisons the keep-alive connection

This is an amplifier for every other 500 in this report, and it is the most demo-visible item
here.

**Repro** — on ONE persistent HTTP connection: provoke the QA3-8 race, then immediately issue
`GET /health` on the *same* connection.

**Measured**

| scenario | next request on the same connection |
|---|---|
| after a 500 | **9 of 9 failed** with `RemoteProtocolError: Server disconnected without sending a response` |
| after a 200 (control) | 0 of 10 failed |

The 500 response itself carries no `Connection: close` that the client can act on
(`date`, `server: uvicorn`, `content-length: 21`, `content-type: text/plain`), so the client
re-uses a connection the server has already torn down.

**What should happen** — a handled error response leaves the connection usable. In a browser,
the current behaviour means one server-side crash costs **two** UI requests: the action, and
whatever the app fetched next — which surfaces as a blank panel or a generic "network error"
rather than a message about the thing that actually failed.

**Test** — `tdd/qa/test_qa3_state_races.py::test_a_500_does_not_poison_the_keep_alive_connection`.

---

## QA3-10 — MAJOR — A run keeps executing after it is terminal

**Repro** — a v2 graph with 4+ parallel entry points whose steps carry the pre-12.6
`executor: legacy` key (a definition a migrated repo can still have), so each step fails
*synchronously* inside dispatch. Watch `/ws` while the run starts.

**Observed event stream** (4 entry points, ids trimmed):

```
pipeline_run_status  running
step_run_status      s0 running
step_run_status      s0 failed
card_updated                      <- on_fail trigger action fires
pipeline_run_status  FAILED       <- run is terminal here
step_run_status      s1 running   <- ...and it starts another step
pipeline_run_status  FAILED
step_run_status      s2 running
pipeline_run_status  FAILED
step_run_status      s3 running
pipeline_run_status  FAILED
pipeline_run_status  FAILED       <- 5 terminal broadcasts for one run
```

Backend log confirms `_complete_pipeline` ran once per entry point:
`Pipeline run 5549ae83 completed with status failed` × 4. On a 200-entry-point pipeline
(a sibling lane's, in the shared log) the same line appeared **200 times** for one run.

**What happened** — because `_complete_pipeline` has no already-terminal guard, each pass also
re-ran workspace cleanup and re-evaluated the run's `on_pass`/`on_fail` trigger action. Log
line `Workspace cleaned for run b6ed8c58` appears three times for a single run.

**What should happen** — one terminal broadcast, one cleanup, one trigger action, and no step
dispatched into a finished run.

**Root cause** — two places, both in `backend/app/services/pipeline_executor.py`:

* `:1198-1206` — the entry-point dispatch loop never re-checks the run status between
  iterations:
  ```python
  async with self._run_lock(pipeline_run.id):
      for step_id in entry_points:
          if step_id in steps_dict:
              await self._execute_graph_step(...)
  ```
* `:838 _complete_pipeline` — sets status/`completed_at`, cleans the workspace and runs the
  trigger action unconditionally; it never asks whether the run is already terminal.

**Test** — `tdd/qa/test_qa3_terminal_run.py::test_no_step_starts_after_the_run_broadcasts_a_terminal_status`
(websocket-based; REST polling cannot see this, because the poller's own requests queue behind
the executor's dispatch loop and only land after it has finished).

---

## QA3-11 — MINOR — A finished run reports an incoherent step position

Same cause as QA3-10. A finished 4-step run returns:

```json
{ "status": "failed", "current_step": 0, "steps_completed": 0,
  "steps_total": 4, "completed_step_ids": ["s3","s0","s1","s2"] }
```

A progress readout built on `current_step` / `steps_completed` shows **0 of 4** on a run whose
four steps all ran. (`steps_completed` counting only *passing* steps is by design and is not
the bug; `current_step` staying at 0 is.)

**Test** — `tdd/qa/test_qa3_terminal_run.py::test_a_finished_run_reports_a_coherent_step_position`.

---

## QA3-12 — BLOCKER — The run-list endpoint collapses under a few readers

**Repro** — populate a *modest* history (5 runs × 60 steps ≈ 302 `step_runs` rows; the test
does it in about 10 s), then fire N simultaneous `GET /api/pipeline-runs?limit=100`.

| concurrent readers | result | wall time |
|---|---|---|
| 10 | 10 × 200 | **29.3 s** |
| 20 | 15 × 200, **5 × 500** | 32.3 s |
| 40 | 15 × 200, **25 × 500** | 31.2 s |
| 60 | 16 × 200, **44 × 500** | 31.2 s |

With 6 long runs also in flight the picture is the same. `/health` under the same burst: 40/40
× 200 in 0.6 s.

Backend log, 233 occurrences:

```
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```

The client sees a bare `500 Internal Server Error`, and (per QA3-9) its next request on that
connection dies too.

**Contributing causes**

1. `backend/app/database.py:15`
   ```python
   engine = create_async_engine(settings.database_url, echo=True)
   ```
   `echo=True` is **hardcoded, not env-gated**, and the container's logging config emits each
   statement twice. This is on the request path for every query in the app.
2. `list_all_pipeline_runs` (`backend/app/routers/pipelines.py:369-393`) eager-loads
   `step_runs → executions` for up to 100 runs with no projection, so one request materialises
   the entire step history.
3. No pool tuning: stock async defaults are 5 connections + 10 overflow with a 30 s checkout
   timeout, and a request holds its connection for its whole duration.

**Blast radius** — the pool is process-wide. Once the run-list burst saturates it, unrelated
endpoints 500 as well: `/api/repos` went to **84/100 × 500** immediately after a run-list
burst, having been 100/100 × 200 on a clean database moments before.

**What should happen** — a dashboard open in two browser tabs during a demo must not 500.

**Tests** — `tdd/qa/test_qa3_concurrent_readers.py` (`..._do_not_get_500s`,
`..._not_pathologically_slow`), plus the clean-database control
`test_health_and_repo_list_survive_the_same_burst_on_a_clean_database`.

---

## QA3-13 — MAJOR — A single run-list request takes 0.6–1.6 s

Measured on a near-empty database (7 runs), best of three:

```
$ curl -o /dev/null -w '%{http_code} %{time_total}s\n' 'localhost:8790/api/pipeline-runs?limit=100'
200 1.219511s
200 0.646273s
200 1.588699s
```

`limit=20` is no faster (0.76–1.05 s), so this is per-request overhead rather than row volume —
consistent with `echo=True`. This latency is the multiplier that turns ten readers into pool
exhaustion.

---

## QA3-14 — MINOR (load-dependent) — Concurrent cancels intermittently 500

10 simultaneous `POST /api/pipeline-runs/{id}/cancel` returned `{200: 7, 500: 3}` on one burst
and `{200: 9, 500: 1}` on another, both while the machine was busy with sibling lanes. The
correlated backend error is:

```
app.services.workspace_service.WorkspaceCleanupError: Failed to remove volume
lazyaf-ws-beee77de-...: 409 Conflict ("remove ...: volume is in use - [237d74dd...]")
  File "/app/app/services/pipeline_executor.py", line 958, in _cleanup_workspace
```

`cancel_run` (`pipeline_executor.py:3949-3952`) kills the step container *best effort* and then
immediately removes the run's volume, so the removal races the container's exit.

**Honest caveat** — I could **not** reproduce this on an otherwise idle daemon: 80 further
cancels across 8 bursts were all 200. `_cleanup_workspace` does swallow `WorkspaceCleanupError`,
so I never captured the exception that actually produced the 500. Treat the 500 as observed but
unattributed. The regression test for it is deliberately **non-strict** xfail
(`test_concurrent_cancels_never_return_500`) so it does not cry wolf on a quiet machine; the
state-level control next to it (`test_concurrent_cancels_converge_on_one_cancelled_run`) is a
normal passing test.

---

## QA3-15 — MINOR — `test/reset` during a live run orphans the volume

```bash
RUN=... # start a run with a `sleep 20` step
sleep 3
curl -X POST localhost:8790/api/test/reset
docker volume ls -q | grep lazyaf-ws-
# -> lazyaf-ws-42d51664-e288-4fc2-b700-2c5bcfb6ea68  (the killed run's volume, still there)
```

`reset_state` (`backend/app/routers/test_api.py:161-190`) deletes the repos' **git storage**
explicitly before wiping the tables, but nothing removes the workspace **volumes** — and wiping
the `workspaces` table destroys the rows `audit_orphans` sweeps 1 and 2 would have used. Only
sweep 3 (unmatched volumes older than the 15-minute `stuck_threshold`) can eventually reclaim
them. The dogfood CI lane resets between tests, so this is a steady drip.

Reset itself is otherwise well-behaved: it returned 200 in 0.3 s with a run in flight, `/health`
stayed 200, and the killed run 404s cleanly instead of wedging.

---

## QA3-16 — MINOR — Cancelling during provisioning leaks the volume

Cancel timing vs. leak, `sleep 20` single-step runs, volumes counted 8 s after terminal:

| cancel delay | volume leaked |
|---|---|
| 0.0 s | yes |
| 0.2 s | yes |
| 0.4 s | yes |
| 0.6 s | yes |
| 4.0 s | no |

`cancel_run` calls `_cleanup_workspace`, which is a no-op when no workspace row exists yet
(`workspace_service.py:459-462`); the provisioning task then goes on to create the row and the
volume. `audit_orphans` sweep 2 reclaims it after the 5-minute grace period, so this is a
delayed leak rather than a permanent one — but it is a leak per cancelled-early run.

---

## QA3-17 — MINOR — Deleting a pipeline or repo mid-run

```
DELETE /api/pipelines/{id}   while its run is executing -> 204
GET    /api/pipeline-runs/{run_id}                       -> 404 (row cascaded away)
```

Same for `DELETE /api/repos/{id}`. The step container keeps running with no row to report to,
and the workspace row goes with the cascade, so its volume becomes sweep-3 garbage. Deleting a
resource with a live run should either be refused (409) or cancel the run first.

---

## QA3-18 — MINOR — Two simultaneous repo deletes both return 204

Ten create-then-double-delete pairs produced `{('delete', 204): 20}` — both deletes claim
success. Sequentially the endpoint is correct: `DELETE /api/repos/<random-uuid>` returns 404,
as do `pipelines`, `cards`, `agent-files`, `prompt-templates`, `user-stories`, `features` and
`criteria`. Only the concurrent pair reports a deletion that did not happen.

---

## QA3-19 — POLISH — The seeded review card has a phantom branch

`POST /api/test/seed` creates its `in_review` card with `branch_name: "lazyaf/seed-review"`,
but `_init_seed_git_repo` only creates the default branch. Every `approve` on the seeded card
therefore fails with:

```
400 {"detail":"Merge failed: Branch 'lazyaf/seed-review' not found"}
```

Anyone demoing or testing the approve path off the seed fixture hits a red toast on the first
click. `backend/app/routers/test_api.py:265-274`.

---

## Verified NOT a bug

Probed hard, behaved correctly:

1. **Workspace isolation across runs.** 8 parallel runs on the same repo, each writing and
   re-reading a per-run marker file: 8/8 passed, every marker intact. Cross-run isolation is
   solid — the QA3-1 defect is strictly *within* one run.
2. **Cancel storms converge.** 10 simultaneous cancels of one run → exactly one `cancelled`,
   no wedge, no duplicate terminal state. (Locked in by a passing test.)
3. **Cancelling a terminal run is refused.** `400 {"detail":"Pipeline run cannot be cancelled"}`;
   status and `completed_at` unchanged. A green run cannot be turned red after the fact.
4. **Start-then-cancel in the same instant.** 4/4 trials settled on `cancelled` with the step
   `cancelled`; no wedged runs.
5. **Cancel at a step transition.** 6 trials at staggered offsets across a 3-step pipeline; all
   reached `cancelled`, none wedged.
6. **Push-event dedup holds under concurrency.** 10 simultaneous identical push events →
   `triggered_runs` totalled 1 and exactly 1 run existed. `should_trigger` has no `await`
   inside it, so it is atomic within an event-loop turn.
7. **Distinct pushes are not over-deduped.** 10 simultaneous events with different shas → 10
   runs (the dedup key includes the sha), which is the correct behaviour.
8. **A push arriving while a run for that repo is mid-flight** starts an independent second run
   with its own workspace; no collision, no interference with the in-flight run.
9. **Agent-file name uniqueness under concurrency.** 20 simultaneous same-name creates →
   `{201: 1, 400: 19}`, exactly one row, `by-name` lookup consistent. (Locked in by a passing test.)
10. **Concurrent updates do not 500.** 20 simultaneous `PATCH` to one agent file → 20 × 200;
    20 simultaneous `PATCH` to one card → 20 × 200.
11. **Bulk create.** 100 simultaneous card creates in one repo → 100 × 201, 100 rows, no
    duplicates or lost writes.
12. **Reset during a live run does not wedge the backend.** 200 in 0.3 s, `/health` still 200
    afterwards, the killed run 404s cleanly. (Only the volume leaks — QA3-15.)
13. **Delete of a non-existent id** returns 404 on all eight resource types tested.
14. **`PATCH` racing `DELETE`** on the same card: 4/4 clean (200 + 204), no `StaleDataError`.
15. **Repo names are not unique by design.** Two sequential creates with the same name both
    return 201, so the concurrent result (10 repos, one name) is not a race — not reported.
16. **Manual runs are not deduped**, and should not be: 10 simultaneous
    `POST /api/pipelines/{id}/run` → 10 distinct runs. An explicit user action is not a
    duplicate event.
17. **Completed runs clean up their volume.** A run to completion, and a cancel 4 s in, both
    left zero new `lazyaf-ws-*` volumes.

---

## Observed but belongs to another lane

Noted only so these are not double-counted, and because they contaminated my first flood
measurements:

* A pipeline containing a step with `type: "banana"` makes `GET /api/pipelines` return **500**
  (`ResponseValidationError` at `backend/app/routers/pipelines.py:145`) for *every* caller — one
  bad row takes down the whole list. 97 occurrences in the shared log.
* `GET /api/repos/{id}/lazyaf/pipelines/{name}` returns 500 on malformed YAML
  (`PipelineYaml() argument after ** must be a mapping, not list/NoneType/str`).
