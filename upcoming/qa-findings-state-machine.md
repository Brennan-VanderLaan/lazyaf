# QA-2 findings: illegal state transitions and lifecycle abuse

**Lane:** QA-2 (card / job / pipeline-run / step-run state machines)
**Target:** isolated QA stack `http://localhost:8790` (compose project `lazyaf-qa`)
**Date:** 2026-08-30
**Regression tests:** `tdd/qa/test_qa2_state_machine.py` (+ helpers in `tdd/qa/qa2_support.py`)

```
python -m pytest -c tdd/qa/pytest.ini tdd/qa/test_qa2_state_machine.py
LAZYAF_QA_BASE_URL=http://localhost:8790  # default
```

Tests that encode a defect are `@pytest.mark.xfail(strict=True)` — they will XPASS
loudly the moment the bug is fixed. Tests without the marker are regression locks
on behaviour that is already correct.

## Environment note (affects reproduction, not the findings)

The QA stack was shared with several other concurrent QA lanes for the whole
session. Consequences worth knowing when re-running:

- **Never call `POST /api/test/reset`** here — it wipes every lane's fixtures.
  My tests allocate a private repo per test via `POST /api/test/seed` (additive)
  and never assert on the size of a global collection.
- Under that concurrent load, `POST /api/test/seed` intermittently returned
  `500 {"detail": "Seeding failed: (sqlite3.OperationalError) database is locked …"}`
  and `500 … QueuePool limit of size 5 overflow 10 reached …`. Raw SQLAlchemy
  driver text plus a `sqlalche.me` docs URL is being surfaced verbatim in an API
  response body. Probably QA-1/QA-5 territory (load / error-surface), noted here
  only because `qa2_support.api_retry` exists to work around it.

---

## Summary table

| ID | Severity | One line |
|----|----------|----------|
| QA2-01 | **BLOCKER** | `approve` has no status guard: a card that never ran is marked `done` with 200 OK |
| QA2-04 | **BLOCKER** | Dragging a running card to **Done** on the board marks it done, merges nothing, and strands its Job at `running` forever |
| QA2-02 | **MAJOR** | `reject` has no status guard: rejecting a running card returns it to `todo`, abandons the live run, strands the Job at `running`, and lets a second agent start |
| QA2-03 | **MAJOR** | `start` and `retry` are read-check-write races: one double-click starts two agent runs, two Jobs, two branches |
| QA2-06 | **MAJOR** | `resolve-conflicts` force-merges arbitrary file contents into the default branch with no conflict present and no status guard |
| QA2-07 | **MAJOR** | Deleting a pipeline mid-run cascade-deletes the live run (404s instantly, becomes uncancellable) and leaks its step container |
| QA2-08 | **MAJOR** | Deleting a repo mid-run cascade-deletes the card but leaves an orphan Job permanently `running`, and cancels nothing |
| QA2-01b | **MAJOR** | Re-approving an already-`done` card re-fires its `card_complete` trigger every time — 3 clicks, 3 verification runs |
| QA2-05 | **MINOR** | `PATCH /api/cards/{id}` with an explicit `null` on any NOT NULL column → unhandled `IntegrityError` → 500 `Internal Server Error` (plain text) |
| QA2-09 | **POLISH** | A card can sit in `in_review` pointing at a branch that was never pushed; every action on it then 400s with a git-level message |

---

## QA2-01 — BLOCKER — `approve` has no status guard whatsoever

**Reproduce**

```bash
BASE=http://localhost:8790
REPO=$(curl -s -X POST $BASE/api/test/seed | python -c "import sys,json;print(json.load(sys.stdin)['repo']['id'])")
CARD=$(curl -s -X POST $BASE/api/repos/$REPO/cards -H 'content-type: application/json' \
  -d '{"title":"never ran","description":"","step_type":"agent"}' \
  | python -c "import sys,json;print(json.load(sys.stdin)['id'])")

# the card is in todo, has no branch, no job, no diff
curl -s -X POST $BASE/api/cards/$CARD/approve -H 'content-type: application/json' -d '{}'
curl -s $BASE/api/cards/$CARD
```

**What happened**

`200 OK`, `merge_result: null`, `status: "done"`. Nothing was merged because there
was nothing to merge, and nothing said so. The `card_complete` triggers fired
(see QA2-01b), so a downstream verification pipeline was started with
`trigger_context.branch = null` — it "verified" the default branch.

The same call succeeds on a card in `in_progress`, `failed` or already `done`.

**What should happen**

`400` unless the card is in `in_review` (the only status where approval is
meaningful). At minimum, approving a card with `branch_name = null` must be
refused rather than silently marking work complete.

**Root cause**

`backend/app/routers/cards.py` → `approve_card`. Compare with its neighbours:
`start_card` checks `card.status != "todo"`, `retry_card` checks
`card.status not in ("failed", "in_review")`. `approve_card` checks nothing —
it goes straight from the 404 lookup to `if card.branch_name and repo.is_ingested:`,
and when that is false it falls through to `card.status = "done"` unconditionally.

**Demo impact:** clicking Approve on any card in any column says "done". A
never-started card can be marched to Done in one click with no work behind it.

---

## QA2-01b — MAJOR — repeated approve re-fires `card_complete` triggers

**Reproduce** — create a pipeline on the repo with
`triggers: [{"type":"card_complete","enabled":true,"config":{"status":"done"}}]`,
then `POST /api/cards/{id}/approve` three times.

**What happened** — `GET /api/pipelines/{id}/runs` grew 0 → 1 → 2 → 3. Two
*concurrent* approvals (a double-click) produced 2 runs. Each spurious run is a
real container.

**What should happen** — reaching `done` should fire the trigger once. A
transition that does not actually change status should not be a transition.

**Root cause** — `approve_card` computes `old_status = card.status` and then calls
`trigger_service.on_card_status_change(db, card, old_status, "done")`
unconditionally, even when `old_status == "done"`.
`backend/app/services/trigger_service.py:114 on_card_status_change` has no
`old_status != new_status` check and no per-(card, status) dedup.

---

## QA2-02 — MAJOR — `reject` has no status guard and abandons a live run

**Reproduce**

```bash
# card with a 30s mock agent
CARD=... ; curl -s -X POST $BASE/api/cards/$CARD/start
sleep 6
curl -s $BASE/api/cards/$CARD            # in_progress
curl -s -X POST $BASE/api/cards/$CARD/reject
# then watch for a minute:
curl -s $BASE/api/cards/$CARD ; curl -s $BASE/api/jobs/<job_id>
```

**What happened**

- `200 OK`; card → `todo`, `branch_name` → `null`, `pr_url` → `null`.
- The agent run was **not cancelled**. The container ran to completion.
- Observed for 50s past the end of the run: `GET /api/jobs/{id}` →
  `{"status": "running", "completed_at": null, "logs": ""}`. **Permanently.**
- The agent pushed `lazyaf/<job_id[:8]>`; the card no longer references it →
  orphan branch nobody can find or clean up.
- Because the card is back in `todo`, `POST /start` is accepted again. Verified:
  two Jobs live simultaneously (`047fa671` and `969e26de`), two containers, two
  branches `lazyaf/047fa671` + `lazyaf/969e26de`, and the card's final status
  came from whichever run finished last.

**What should happen** — `reject` should be refused from `in_progress` (or should
cancel the run the way `POST /api/jobs/{id}/cancel` correctly does), and must
land the Job.

**Root cause** — two halves:

1. `backend/app/routers/cards.py` → `reject_card`: no status check, no run cancel.
   It just writes `card.status = "todo"; card.branch_name = None`.
2. `backend/app/services/agent_run.py` → `_complete_card_work`, the
   STALE-COMPLETION GUARD (`if card.status != "in_progress" or not job_is_live: …
   return`). The guard is right to refuse to *land the card*, but it returns
   **before touching the Job row**, so a Job whose run has finished is left at
   `running` with `completed_at = NULL` forever. `POST /api/jobs/{id}/cancel`
   gets this right — it cancels the run first, then writes the Job — which is the
   shape `reject` (and QA2-04) needs.

**Demo impact** — `frontend/src/lib/components/JobStatus.svelte` polls
`/api/jobs/{id}/logs` every 3 s while `job.status` is `running` or `queued`. A
permanently-`running` Job is an infinite spinner over an empty log pane plus an
infinite polling loop in the browser tab.

---

## QA2-03 — MAJOR — `start` and `retry` are read-check-write races

**Reproduce** — two threads, one card:

```python
concurrent(lambda: api("POST", f"/api/cards/{card_id}/start"), n=2)
```

**What happened** — both returned `200` with **different** `job_id`s
(`84d5d95f…`, `38d03e2f…`). Two full ad-hoc `PipelineRun`s (`trigger_type=card_work`)
executed, two agent containers ran against the same repo, two branches were pushed.
`card.job_id` kept only one; the other Job and branch are orphans. The card's final
status is whichever run committed last — nondeterministic.

Identical result for `POST /api/cards/{id}/retry` on an `in_review` card.

**What should happen** — one `200`, one `409`/`400`. This is a double-click, the
single most likely thing a human does at a demo.

**Root cause** — `backend/app/routers/cards.py` → `start_card`:

```python
if card.status != "todo":
    raise HTTPException(400, "Card must be in 'todo' status to start")
...           # no row lock, no SELECT ... FOR UPDATE, no conditional UPDATE
card.status = "in_progress"
```

and `retry_card` repeats the pattern. Fix shape: a conditional
`UPDATE cards SET status='in_progress' WHERE id=:id AND status='todo'` and treat
`rowcount == 0` as the refusal, so the check and the write are one statement.
(Note that SQLite is the backing store here, so a `SELECT … FOR UPDATE` is not
available — the conditional UPDATE is the portable form.)

---

## QA2-04 — BLOCKER — dragging a running card to **Done** on the board

This is QA2-02's twin reached through the UI's primary gesture, and it is worse
because nothing is merged.

**Reproduce (clicks)** — start a card, and while it is in the *In Progress*
column drag it to *Done*.

**Reproduce (API)** — `PATCH /api/cards/{id} {"status": "done"}` on an
`in_progress` card.

**What happened**

- `200 OK`, card status `done`.
- **The branch was never merged.** `GET /api/repos/{repo}/branches` still shows
  `lazyaf/<jobid>` alongside `main`, and `GET /api/repos/{repo}/diff?base=main&head=lazyaf/<jobid>`
  still reports the agent's file as added. The board says Done; `main` has nothing.
- The Job stayed `running` / `completed_at: null` indefinitely (same guard as QA2-02).
- The agent container kept running.

**What should happen** — either the board refuses the drop into Done for a card
that has not been approved, or the drop routes through `approve` (the way
`todo → in_progress` already routes through `start`). A card must never read
`done` while its branch is unmerged and its job is running.

**Root cause** — `frontend/src/lib/components/Board.svelte` → `handleDrop`:

```js
if (card.status === 'todo' && status === 'in_progress') {
  await cardsStore.start(cardId);          // special-cased
} else {
  await cardsStore.update(cardId, { status });   // every other drag: raw PATCH
}
```

and `backend/app/routers/cards.py` → `update_card`, which writes `status`
straight through with no transition validation. `frontend/src/lib/components/Column.svelte`
accepts a drop into any column from any column, with no disabled state.

---

## QA2-05 — MINOR — `PATCH` with an explicit `null` on a NOT NULL column → 500

**Reproduce**

```bash
curl -s -i -X PATCH $BASE/api/cards/$CARD -H 'content-type: application/json' -d '{"status":null}'
```

**What happened** — `500` with a **plain-text** `Internal Server Error` body (not
JSON, so a client that does `await res.json()` throws a parse error on top of the
500). Backend log:

```
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) NOT NULL constraint failed: cards.title
[SQL: UPDATE cards SET title=?, updated_at=? WHERE cards.id = ?]
[parameters: (None, '2026-08-30 10:59:15.278489', '56eb81fb-…')]
```

Confirmed for `status`, `runner_type`, `step_type`, `title`, `description`.
The row is left unchanged, so it is not corrupting — it is a crash and a leaked
internal error.

**What should happen** — `422`, the same as `{"status": "banana"}` already gets.

**Root cause** — `backend/app/routers/cards.py` → `update_card`:

```python
update_data = update.model_dump(exclude_unset=True)
for key, value in update_data.items():
    if key == "status" and value is not None:
        value = value.value        # None falls through untouched
    ...
    setattr(card, key, value)
```

`CardUpdate` (`backend/app/schemas/card.py:22`) types every field as `X | None = None`
so that *absent* means "leave alone" — but that also makes an *explicit* `null` a
valid input meaning "set to NULL". Only `feature_id` / `user_story_id` actually
want the unlink semantics; the rest are NOT NULL columns. Fix: drop `None` values
for the non-nullable fields before the loop, or split the unlink-capable fields
into their own sentinel type.

---

## QA2-06 — MAJOR — `resolve-conflicts` merges when there is no conflict

**Reproduce**

```bash
# card ran, produced safe.txt, was approved and is already 'done'
curl -s -X POST $BASE/api/cards/$CARD/resolve-conflicts -H 'content-type: application/json' -d '{
  "resolutions":[
    {"path":"safe.txt","content":"INJECTED, no conflict existed"},
    {"path":"BRAND_NEW_FILE.txt","content":"this file was never in any branch"}]}'
```

**What happened** — `200 OK`:

```json
{"success": true, "merge_type": "merge",
 "message": "Merged lazyaf/a28d8e3d into main with conflict resolution"}
```

A commit `Merge branch 'lazyaf/a28d8e3d' into main (conflicts resolved)` landed on
`main` containing content the caller invented, including a file that existed in no
branch. No conflict existed. The card was already `done`. The card is then set to
`done` again.

**What should happen** — the endpoint's contract is "here are the resolutions for
the conflicts you showed me". It should verify a conflicting merge actually exists
(re-run `merge_branch` and require a `conflicts` result), that every resolved
`path` is one of the reported conflicting paths, and that the card is in a state
where merging is legal.

**Root cause** — `backend/app/routers/cards.py` → `resolve_conflicts`. It checks
only that the card exists, the repo is ingested, and `resolutions` is non-empty,
then calls `git_repo_manager.resolve_and_merge(...)` directly. It also never
checks `card.branch_name is not None` (a rejected card has `branch_name = None`,
which is then passed to git as the source branch).

Secondary consequence: this is a **lost-update** vector. A conflict dialog left
open in a browser tab can be submitted much later and will overwrite whatever
`main` has become in the meantime, with no staleness check.

---

## QA2-07 — MAJOR — deleting a pipeline mid-run destroys the run and leaks its container

**Reproduce**

```bash
PIPE=<pipeline with step: sh -c 'echo start; sleep 30; echo end'>
RUN=$(curl -s -X POST $BASE/api/pipelines/$PIPE/run -d '{}' -H 'content-type: application/json' | jq -r .id)
sleep 4
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE $BASE/api/pipelines/$PIPE   # 204
curl -s $BASE/api/pipeline-runs/$RUN                                            # 404 immediately
curl -s -X POST $BASE/api/pipeline-runs/$RUN/cancel                             # 404
```

**What happened**

- `204 No Content`. The `PipelineRun`, its `StepRun`s and `StepExecution`s were
  cascade-deleted **while the step was executing** (`Pipeline.runs` is
  `cascade="all, delete-orphan"`, `backend/app/models/pipeline.py`).
- The run 404s instantly; `/cancel` can no longer reach it; the UI's run detail
  page dies mid-view.
- **Container leak, measured against a control:** a run allowed to finish
  normally left 0 extra containers. The same pipeline deleted mid-run left one
  `python:3.12` container in `Exited (0)` state that was never removed. The
  executor's cleanup (`local_executor.py` ≈ line 536, `container.remove(force=True)`)
  is driven off rows that no longer exist.
- The step task keeps running and writing to deleted rows.

**What should happen** — `409` while the pipeline has a run in
`pending`/`running`, or delete-with-cancel (cancel every live run first, then
delete). `POST /api/pipeline-runs/{id}/cancel` already refuses a finished run
cleanly, so the vocabulary exists.

**Root cause** — `backend/app/routers/pipelines.py:245 delete_pipeline` — lookup,
`db.delete(pipeline)`, commit. No in-flight check.

---

## QA2-08 — MAJOR — deleting a repo mid-run strands an orphan Job at `running`

**Reproduce** — start a card with a 25 s mock agent, wait 6 s, `DELETE /api/repos/{repo}`.

**What happened**

- `204 No Content`. Git storage deleted, `Repo` row deleted, `Card` cascade-deleted
  (`GET /api/cards/{id}` → 404).
- The **Job row survives** (`GET /api/jobs/{id}` → 200) with `status: "running"`,
  `completed_at: null`, pointing at a `card_id` that no longer exists. Still
  `running` 26 s after the run should have ended, and it can never be landed —
  `_complete_card_work` bails on `card is None`.
- Nothing cancelled the run. The agent kept executing against a repo whose git
  storage had just been deleted.

**What should happen** — `409` while any card of the repo has a live job / any
pipeline of the repo has a live run, or cancel them all first. Jobs of deleted
cards should not survive as permanently-`running` orphans.

**Root cause** — `backend/app/routers/repos.py:155 delete_repo`: lookup,
`git_repo_manager.delete_repo(repo_id)`, `db.delete(repo)`, commit. No in-flight
check, and `Job` has no FK cascade from `Card` (`backend/app/models/job.py`:
`card_id` is a plain FK column, and jobs are not on any relationship with a
cascade).

---

## QA2-09 — POLISH — a card can reach `in_review` with a branch that does not exist

**Reproduce** — run an agent card whose agent makes no file changes (the mock with
no `file_operations`; realistically, an agent that decides there is nothing to do).

**What happened** — the card lands `in_review` with
`branch_name: "lazyaf/85547220"`, but the branch was never pushed —
`GET /api/repos/{id}/branches` shows only `main`. Every subsequent action then
fails with a git-level string:

- Approve → `400 {"detail":"Merge failed: Branch 'lazyaf/85547220' not found"}`
- Diff → `400 {"detail":"Branch not found"}` (the review pane cannot render)
- Rebase → `400 {"detail":"Rebase failed: Branch 'lazyaf/85547220' not found"}`

**What should happen** — a run that produced no commit should not present the card
for review as if there were something to look at, or the message should say "the
agent made no changes" rather than exposing an internal branch name and a git error.

*(The same three messages appear, and are correct and actionable, when a branch
that once existed is deleted — see the "verified not a bug" list.)*

---

# Verified NOT a bug

These were probed hostilely and behaved correctly. Locked down as non-xfail tests
in `tdd/qa/test_qa2_state_machine.py`.

1. **`POST /api/cards/{id}/start` from every non-`todo` status** — `400
   "Card must be in 'todo' status to start"` from `in_progress`, `in_review`,
   `done` and `failed`. No state mutated.
2. **`POST /api/cards/{id}/retry` from every non-retryable status** — `400
   "Can only retry cards in 'failed' or 'in_review' status, current: <status>"`
   from `todo`, `in_progress` and `done`. Message names the current status.
3. **`POST /api/jobs/{id}/cancel` on a running card job** — cancels the ad-hoc run
   first, then lands `Job(status=failed, error="Cancelled by user", completed_at=…)`
   and the card at `failed` (the one status `/retry` accepts). Verified the
   straggler step task does **not** later walk the card into `in_review` — the
   stale-completion guard holds. This is the correct shape the other lifecycle
   paths are missing.
4. **Cancelling the same job twice** — second call `400 "Job cannot be cancelled"`.
5. **Cancelling a finished pipeline run, twice** — `400 "Pipeline run cannot be
   cancelled"` both times; run status unchanged.
6. **Three concurrent cancels of a live pipeline run** — all `200`, run ends
   `cancelled` exactly once, every `StepRun` `cancelled` with a non-null
   `completed_at`. (Two of the three arguably ought to 400, but the resulting
   state is consistent, so this is not a defect.)
7. **Cancelling a nonexistent / malformed run id** — `404 "Pipeline run not found"`,
   no 500.
8. **Every card operation once the branch is deleted** — approve / diff / rebase /
   resolve-conflicts all `400` with a message naming the missing branch, and the
   card is left untouched at `in_review`. No 500, no corruption.
9. **Running a pipeline whose repo has zero commits / is not ingested** — `400
   "Repo must be ingested before running pipelines"`.
10. **Approving a card twice when the branch really was merged** — the second
    merge is a git no-op (`merge_branch` returns "Already up to date" when
    `source_sha == target_sha`, `backend/app/services/git_server.py:647`). The
    git side is idempotent; only the trigger side is not (QA2-01b).
11. **Rebasing a card with no changes** — `200`, rebase reports up-to-date, no
    branch churn, card status untouched.
12. **`resolve-conflicts` with an empty `resolutions` list** — `400 "No conflict
    resolutions provided"`.
13. **Illegal `status` values on PATCH** — `"banana"`, `"DONE"`, `"in-progress"`,
    `7` all `422` with the enum listed in the message. Card unchanged.
14. **Spoofing `job_id` / `branch_name` through PATCH** — silently ignored;
    they are not on `CardUpdate`, so a caller cannot repoint a card at another
    card's job.
15. **`trigger_type` spoofing on `POST /api/pipelines/{id}/run`** — `card_work`
    and `playground` are rejected before any lookup or write
    (`backend/app/routers/pipelines.py:283`), so a caller cannot drive an
    arbitrary card to `in_review` by starting a pipeline.
16. **Script/docker card start and retry** — `400` with an explicit explanation
    naming the removed execution path and the supported alternative; the card is
    never left stuck in `in_progress`.

## Things I tried and could NOT break

- I could not get a card and its **pipeline run** to disagree through the
  cancel path — `jobs.py::_cancel_adhoc_run_for_job` plus the stale-completion
  guard in `agent_run._complete_card_work` genuinely hold, including against a
  straggler step task committing after the cancel.
- I could not corrupt a `PipelineRun`'s own status by racing cancels, nor get a
  `StepRun` to finish with a null `completed_at`.
- I could not get a 500 out of any card lifecycle endpoint by feeding it garbage
  ids, wrong types, or malformed bodies — only the explicit-`null` PATCH (QA2-05)
  crashes.
- Card↔Job disagreement is always in the *same direction*: the card advances and
  the Job is left behind at `running`. I found no case where the Job advanced past
  the card.
