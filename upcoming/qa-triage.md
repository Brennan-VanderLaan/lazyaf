# QA triage — adversarial pass, 2026-08-30

**Inputs:** six lane reports (`upcoming/qa-findings-{api-fuzz,state-machine,races,resource-graph,ui-workflow,demo-polish}.md`),
~118 findings, ~5,400 lines of authored regression tests under `tdd/qa/` and `frontend/e2e/qa/`.

**Output:** 24 deduped findings, each verified by me against the QA stack at `http://localhost:8790`
(reset via `POST /api/test/reset`) or against source where execution was impossible.

**Verification effort:** every finding below was re-run by me from scratch. I also executed the
lanes' own suites: `test_api_fuzz_findings.py` + QA-5 + QA-6 (55 passed / 47 xfailed, zero misfires),
`test_qa2_state_machine.py` (14 passed / 16 xfailed), the QA-4 graph suites (18 passed / 24 xfailed /
1 failed), and the QA-3 suites (1 passed / 10 xfailed / 4 failed).

**Verdict tally:** 20 CONFIRMED · 3 PLAUSIBLE · 3 REFUTED (plus 1 finding the reports got backwards —
see T3b, which QA-3 filed under "verified NOT a bug").

Host clock during verification: `08:06:32 EDT (UTC-04:00)`. Backend emitted `2026-08-30T12:06:32.695487`.
That one line is finding T1.

---

## Ranked findings

Ranked by real user impact with demo-visibility weighted explicitly: a defect a viewer would *see*
outranks an equally severe API edge case.

| # | Severity | Demo-visible | Area | One line |
|---|----------|--------------|------|----------|
| **T1** | BLOCKER | **Yes — constantly** | serialization | Every timestamp is naive UTC, so live durations render `-14399s` and "Started" reads hours in the future |
| **T2** | BLOCKER | **Yes — the board** | card lifecycle | `approve`/`reject`/`PATCH status` have no state guard: a card that never ran reaches **Done** in one click |
| **T3** | BLOCKER | **Yes — twice per crash** | error handling | Every unhandled DB exception is a bare plain-text 500 *and* kills the keep-alive connection, so the next UI request dies too |
| **T4** | BLOCKER | **Yes — green tick** | graph execution | A cycle, an unreachable step, or a one-character `on_success` typo all report **PASSED** having run one third of the pipeline |
| **T5** | BLOCKER | **Yes — dashboard** | run listing | `GET /api/pipeline-runs` serializes every step's full logs; ~300 step rows → 2 s responses and 500s at 40 readers |
| **T6** | MAJOR | **Yes — double-click** | card lifecycle | `start`/`retry` are read-check-write races: one double-click starts N agent runs, N jobs, N branches |
| **T7** | MAJOR | **Yes — silently stale** | frontend | The UI never resyncs after a dropped socket, never says the backend is gone, and pins "Unknown error" forever |
| **T8** | MAJOR | **Yes — Run unclickable** | validation | No name/title field anywhere has a length bound; a long pipeline name pushes the **Run** button off-screen |
| **T9** | MAJOR | **Yes — live spinner** | graph execution | Duplicate entry points / duplicate edges dispatch a step N times, and the run is stamped `passed` while a step still runs |
| **T10** | MAJOR | Partly | graph execution | `POST /run` blocks the request handler walking the whole graph — measured **299 s** on a 400-step chain |
| **T11** | MAJOR | No | container safety | Step containers run with `Memory=0 NanoCpus=0 PidsLimit=unset` and there is no fan-out cap |
| **T12** | MAJOR | Partly | lifecycle | Deleting a pipeline/repo mid-run cascades the live run away: instant 404, uncancellable, leaked volume + container |
| **T13** | MAJOR | No | YAML surface | `.lazyaf/pipelines/*.yaml` is a second, unvalidated definition door: `type: banana` and `timeout: -5` both accepted |
| **T14** | MAJOR | Partly | YAML surface | A malformed YAML file vanishes from the listing and 500s on fetch with the raw Python exception |
| **T15** | MAJOR | No | data integrity | `resolve-conflicts` force-merges caller-invented file contents with no conflict present and no state guard |
| **T16** | MAJOR | **Yes — first click** | seed fixture | The seeded `in_review` card points at a branch that was never created, so the demo's first Approve is a red toast |
| **T17** | MAJOR | **Yes — Export** | export | Pipeline name goes raw into `Content-Disposition`: 500 on non-Latin-1, dropped connection on CR/LF |
| **T18** | MAJOR | No | export | YAML export is lossy and, for v2 graphs, emits a document LazyAF cannot re-import |
| **T19** | MAJOR | No | git | An ingested repo reports `default_branch: main` while its git HEAD is `master` |
| **T20** | MAJOR | No | usage/cost | The usage manifest accepts negative tokens, negative wall-clock and `gpu_fraction: 99999` |
| **T21** | MINOR | No | auth | Playground `internal/*` endpoints are unauthenticated and answer `{"ok":true}` for sessions that do not exist |
| **T22** | MINOR | No | error surface | Internal exception text and internal module paths leak into user-facing `detail` strings |
| **T23** | MINOR | No | config | Step/runner JWT secrets default to constants published in this repo, with no startup warning |
| **T24** | POLISH | Some | assorted | "1 steps"; wrong branch name in QUICKSTART; 400-vs-409 inconsistency; unbounded `commits?limit`; silent PATCH drops |

**Not carried forward (refuted):** QA3-1 (workspace race), QA3-2 (raw Docker text), QA3-13
(run-list latency on a near-empty DB). See "Refuted findings" below. QA-API-16 (`database is locked`)
was honestly self-reported as unreproducible and I could not reproduce it either; it is folded into T3.

---

# Findings

---

## T1 — BLOCKER — Every timestamp is naive UTC

**Merged from:** QA5-1, QA6-1, QA6-2, QA6-3 (four "findings", one line of code)

**Verdict: CONFIRMED.**

**Reproduction**

```
$ date '+%Y-%m-%dT%H:%M:%S %Z (UTC%z)'
2026-08-30T08:06:32 EDT (UTC-0400)

$ curl -s -XPOST localhost:8790/api/repos -H 'content-type: application/json' -d '{"name":"tz"}' | jq -r .created_at
2026-08-30T12:06:32.695487          # no Z, no offset
```

Per ECMA-262 a date-*time* string with no designator is parsed as **local** time, so a browser at
UTC−4 reads that as 12:06 local — four hours in the future. `Date.now() - startTime` is therefore
`-14400s` for a row created this instant.

Confirmed at every consumer:

- `frontend/src/lib/pages/PipelinesPage.svelte:124` — `if (seconds < 60) return \`${seconds}s\`` is
  **true for negative values**, so the guard prints the raw negative instead of clamping. Renders `-14399s`.
- `PipelinesPage.svelte:119` `formatDate` → the "Started" column shows a time that has not happened yet.
- `RunnerPanel.svelte:81` `connectionAge` has `Math.max(0, …)`, which converts the wrong negative
  into a wrong constant: a runner that enrolled an hour ago reads **`ws 0s`** forever.
- Same math copy-pasted in `PipelineRunViewer.svelte:123` and `JobStatus.svelte:94`.

**Fix altitude: one shared serialization change, not four formatters.** Every model uses
`mapped_column(DateTime, default=datetime.utcnow)`. Emit tz-aware UTC — `datetime.now(timezone.utc)`,
or a pydantic field serializer that appends `Z` — and all four symptoms resolve without touching the
frontend. Then, separately, collapse the four `formatDuration` copies into one helper and clamp
defensively (`if (!Number.isFinite(s) || s < 0) return '—'`); and **remove the `Math.max(0, …)` in
`connectionAge`**, which will otherwise keep the runner panel silently broken after the real fix lands.

Fixing only the formatters looks like it worked and leaves `formatDate` and `connectionAge` wrong.

---

## T2 — BLOCKER — The card lifecycle has no state guards at all

**Merged from:** QA-API-01, QA-API-17, QA2-01, QA2-01b, QA2-02, QA2-04, QA3-6, QA3-7, QA5-4
(nine findings across five lanes — one missing gate)

**Verdict: CONFIRMED**, every sub-case.

**Reproduction**

```bash
R=$(curl -s -XPOST localhost:8790/api/repos/ingest -H 'content-type: application/json' -d '{"name":"demo"}' | jq -r .id)
C=$(curl -s -XPOST localhost:8790/api/repos/$R/cards -H 'content-type: application/json' -d '{"title":"never started"}' | jq -r .id)
curl -s -XPOST localhost:8790/api/cards/$C/approve -H 'content-type: application/json' -d '{}'
```

→ `200`, `{"status":"done","merge_result":null}`, `branch_name: null`. Repeated approves keep returning 200.

Measured, all on this stack today:

| gesture | result |
|---|---|
| `approve` on a `todo` card | 200 → `done`, nothing merged |
| `reject` on a `todo` card | 200 → stays `todo`, clears `branch_name`/`pr_url` from any state |
| `PATCH {"status":"done"}` on any card | 200 (this is the board's drag-to-Done path) |
| 3 × `approve` on one card | **3 pipeline runs** — `card_complete` triggers re-fire every time |
| 6 concurrent alternating approve/reject | all 200; final state decided by whoever commits last |

**Root cause** — `backend/app/routers/cards.py`. `approve_card` never reads `card.status`; the merge
block is `if card.branch_name and repo.is_ingested:` and `card.status = "done"` runs **outside** it,
under a comment that says "only if merge succeeded". `reject_card` has no check either.
`trigger_service.on_card_status_change` is called unconditionally, even when `old_status == "done"`.
`Board.svelte handleDrop` special-cases only `todo → in_progress`; every other drag is a raw PATCH.

The same file already knows how to do this: `retry_card:567` returns
`400 "Can only retry cards in 'failed' or 'in_review' status, current: {status}"`.

**Fix altitude: a whole class, not five endpoints.** There is no shared "is this transition legal"
primitive — `start` and `retry` each hand-roll a guard and the other four paths have none. Define the
card state machine once (allowed transitions + a single `assert_transition(card, target)` helper),
route `approve`/`reject`/`resolve-conflicts`/`update_card` through it, and make `on_card_status_change`
a no-op when `old == new`. Patching `approve_card` alone leaves the drag-to-Done path, the reject
path, and the trigger re-fire wide open — QA-5 found this bug through the board, QA-1 through the API,
and QA-2 through a running card; they are the same hole.

**Demo note:** this is the single finding that puts a *wrong fact* on the screen the audience is
looking at. Work that never happened shows as Done, indistinguishable from work that did.

---

## T3 — BLOCKER — Unhandled DB exceptions are bare 500s that also kill the connection

**Merged from:** QA-API-02, QA-API-03, QA-API-04, QA-API-05, QA-API-16, QA2-05, QA3-5, QA3-8, QA3-9

**Verdict: CONFIRMED.** I hit this accidentally on my *first* probe: a `PATCH {"title": null}` 500'd,
and my very next request on that session raised `RemoteDisconnected`.

**Reproduction (the amplifier — QA3-9)**

```python
s = requests.Session()
s.patch(f"{B}/api/cards/{C}", json={"title": None})   # -> 500, text/plain "Internal Server Error"
s.get(f"{B}/health")                                   # -> ConnectionError, 6/6 trials
```

Control: 200-then-next failed 0/6. The 500 carries no `Connection: close`, so the client re-uses a
socket the server already tore down. **One server-side crash costs the UI two requests** — the action,
and whatever the app fetched next — which surfaces as a blank panel or a generic network error rather
than a message about the thing that actually failed.

**The 500 sources, all verified today:**

| trigger | exception in the log | count observed |
|---|---|---|
| `PATCH {"<required field>": null}` — cards (title/status/description/runner_type), features, prompt-templates, agent-files | `IntegrityError: NOT NULL constraint failed` | 9+ |
| 20 concurrent `POST /api/prompt-templates` with one name | `IntegrityError: UNIQUE constraint failed: prompt_templates.name` | **4–11 per 20-request trial**, 3/3 trials |
| 10 concurrent `POST /api/agent-files` with one name | `IntegrityError: UNIQUE constraint failed: agent_files.name` | **6 of 10** — see T3b |
| `start` racing `delete` on one card | `StaleDataError: expected to update 1 row(s); 0 were matched` | 3 of 6 trials |
| `priority: 9223372036854775808` (2^63) | `OverflowError: Python int too large` | deterministic |
| JSON `NaN` / `Infinity` / `-Infinity` literal, **any endpoint** | `ValueError: Out of range float values are not JSON compliant` | deterministic |
| 40 concurrent `GET /api/pipeline-runs` (see T5) | `TimeoutError: QueuePool limit of size 5 overflow 10 reached` | 10 of 40 |

Every one returns the literal string `Internal Server Error` with `content-type: text/plain` — not
JSON — so a frontend doing `res.json()` on the error path throws a *second* time (this is the direct
cause of the `alert("Unknown error")` in T7).

### T3b — a correction to the reports

QA-3 filed "agent-file name uniqueness under concurrency" under **verified NOT a bug** ("20
simultaneous same-name creates → `{201: 1, 400: 19}`") and locked it in as a passing guard test.
**That test fails today:** `{500: 6, 400: 3, 201: 1}`. QA-1, from the other direction, wrote that
`agent_files` "has no unique constraint" — it does: `backend/app/models/agent_file.py:14` is
`unique=True`. Both lanes got the same fact wrong in opposite directions. The duplicate-name 500 is
not one endpoint; it is the shared check-then-insert shape, present on **at least** prompt-templates
and agent-files.

**Fix altitude: three app-level exception handlers, not N endpoint patches.**

1. `@app.exception_handler(IntegrityError)` → 409 (unique) / 422 (not-null). Fixes the null-PATCH
   class *and* both duplicate-name races *and* the un-caught `PendingRollbackError` /
   `InvalidRequestError` cascade I saw 26× in the log.
2. `@app.exception_handler(RequestValidationError)` that scrubs non-finite floats from `exc.errors()`
   before rendering. Fixes `NaN`/`Infinity` **across the entire service in one place**.
3. A catch-all that returns a JSON 500 rather than letting the exception escape to uvicorn — which is
   what tears down the connection. This is the single highest-leverage line in this whole triage:
   it halves the blast radius of every other 500 in the product.

Then, separately and at a lower altitude: bound `int` fields that reach the DB (`Field(le=2**63-1)`),
and add `connect_args={"timeout": 30}` + WAL to the SQLite engine.

---

## T4 — BLOCKER — Structurally broken pipelines report PASSED

**Merged from:** QA4-02, QA4-03, QA4-04, QA4-08

**Verdict: CONFIRMED**, all four, on a repo with real commits.

**Reproduction** — three shapes, all accepted at 201, all green:

```
cycle       a→b→c→b, entry [a]      → status=passed  steps_completed=1/3  completed_ids=['a']
unreachable {a, orphan}, no edges   → status=passed  steps_completed=1/2  completed_ids=['a']
typo        on_success: "nextt"     → status=passed  steps_completed=1/3  (steps 2 and 3 never created)
```

Plus the fourth door: a step-less `.lazyaf/pipelines/nosteps.yaml` run via
`POST /api/repos/{id}/lazyaf/pipelines/nosteps/run` returns
`{"status":"passed","message":"Started pipeline run for 'No Steps'"}` → run `passed 0/0`, while the
platform endpoint correctly answers `400 "Pipeline has no steps defined"` for the same thing.

**Root cause** — two gaps that compose. `validate_graph_integrity` checks edge endpoints and entry
points but never acyclicity or reachability. Then `pipeline_executor.py:3440`:

```python
elif not steps_to_execute:
    all_passed = await self._check_all_steps_passed(db, pipeline_run)
    await self._complete_pipeline(db, pipeline_run, success=all_passed)
```

`_check_all_steps_passed` only inspects StepRuns that were **created**, so steps that were never
created cannot count against the verdict. And `_handle_action` logs `"Unknown action '{action}',
treating as 'stop'"` then calls `_complete_pipeline(success=step_success)` — the *step* passed, so
the *run* passes.

**Fix altitude: one invariant, not four bugs.** `_complete_pipeline(success=True)` must require that
every step in the graph either ran or was skipped by a *taken* edge condition; anything else is
`failed` (or a new `incomplete`) with a reason naming the unreached steps. Add acyclicity +
reachability to `validate_graph_integrity`, close the `on_success`/`on_failure` vocabulary at the
schema (the way `trigger_type` already is — that enum is the model to copy), and route
`run_repo_pipeline` through the same no-steps gate the platform endpoint uses.

**Demo note:** for a CI product a false green is the worst possible defect class. Ranked above the
races because the pipeline finishes *fast* and *green* — nothing on screen suggests anything is wrong.

---

## T5 — BLOCKER — The run list collapses under a handful of readers

**Merged from:** QA3-12, QA4-18, QA6 "needs confirmation", QA2 environment note
**Also merged and REFUTED:** QA3-13

**Verdict: CONFIRMED — with a corrected precondition.**

QA-3 attributed this to `echo=True` and per-request overhead. That diagnosis is wrong, and their own
strict test now says so. The real driver is **step-row volume × unbounded log serialization.**

Measured by me, on an idle daemon:

| database state | `GET /api/pipeline-runs?limit=100` | 40 concurrent readers |
|---|---|---|
| 41 runs, small step counts | **15 ms**, 47 KB | 40 × 200 in 1.7 s |
| + one 400-step run (700 step rows total) | **2.24 s**, 509 KB | **30 × 200, 10 × 500** in 57 s |

At the higher volume, `limit=5` still takes **2.05 s and returns 464 KB**, because one run holds 400
step rows and the listing serializes every step's full `logs`. Backend log: `TimeoutError: QueuePool
limit of size 5 overflow 10 reached` ×10 — exactly matching the ten 500s.

`PipelinesPage` polls this endpoint every 3 s whenever any run looks active, and per T7 that can be
forever. Two browser tabs during a demo is a plausible trigger.

**Root cause** — `backend/app/routers/pipelines.py:373`:
`selectinload(PipelineRun.step_runs).selectinload(StepRun.executions)` with no projection, and
`StepRunRead.logs` serialized in full for every step of every listed run. The websocket's
`step_run_to_ws_dict` already omits `logs`; the REST listing does not.

**Fix altitude: the listing's shape, plus pool headroom — not the engine's echo flag.**
Drop `logs` from the list serializer (leave full logs to `/api/pipeline-runs/{id}/steps/{i}/logs`),
and either drop the `executions` eager-load or project only what the list view renders. Then raise
`pool_size`/`max_overflow` for headroom. Turning off `echo=True` is worth doing for log legibility
(T24) but will **not** fix this — I measured 15 ms per request with echo on.

**Blast radius, corrected:** QA-3 reported that a run-list burst takes unrelated endpoints down with
it (`/api/repos` at 84/100 × 500). That did **not** reproduce: immediately after a burst that
produced 10 × 500, `/api/repos` returned 40/40 × 200 in 0.3 s and `/health` 40/40 in 0.1 s.

---

## T6 — MAJOR — `start` and `retry` are read-check-write races

**Merged from:** QA2-03, QA3-3, QA3-4

**Verdict: CONFIRMED.**

**Reproduction** — N threads released from one barrier onto one card:

| simultaneous `POST /cards/{id}/start` | accepted | pipeline runs created |
|---|---|---|
| 2 | 2 × 200 | 2 |
| 3 | 3 × 200 | 3 |
| 5 | 5 × 200 | 5 |

5 simultaneous `retry` on a `failed` card: 5 × 200. Each start creates its own `Job`, its own hidden
ad-hoc `Pipeline`, its own `PipelineRun`, its own agent container and its own `lazyaf/<job8>` branch.
The card keeps only the last `job_id`; the rest are orphans nothing points at, and the card's final
status is whichever run commits last.

**Root cause** — `backend/app/routers/cards.py:296-355`. The `card.status != "todo"` check and the
`card.status = "in_progress"` write are separated by ~6 awaits (repo lookup, agent-file validation).
That is a TOCTOU window six awaits wide with no row lock.

**Fix altitude: one atomicity pattern, applied to the same five handlers as T2.** SQLite has no
`SELECT … FOR UPDATE`, so the portable form is a conditional update whose rowcount is the decision:

```sql
UPDATE cards SET status='in_progress' WHERE id=:id AND status='todo'
```

`rowcount == 0` → the 400 the endpoint already knows how to return. This is the same fix shape as T3's
IntegrityError handling and belongs in the same change: *make the check and the write one statement.*

**Demo note:** a double-click is the single most likely thing a human does at a demo, and it currently
launches two containers.

---

## T7 — MAJOR — The frontend has no concept of "the server is unreachable"

**Merged from:** QA5-2, QA5-3, QA5-5, QA5-6, QA6-5

**Verdict: CONFIRMED at source** (I did not re-drive a browser; every claim is a grep I reproduced).

Five reported symptoms, one missing layer:

| symptom | source |
|---|---|
| Board never resyncs after a dropped socket; changes made during the outage are lost forever | `stores/websocket.ts:183` — `ws.onopen` sets `status` and clears the timer, and does nothing else |
| A dead backend is completely invisible — no banner, no badge | `websocketStore` has a 4-state `status` store and **zero consumers**: the only two references in the whole app are `App.svelte:26/30` (connect/disconnect) |
| "Unknown error" pinned in the sidebar forever after one blip | `stores/runners.ts` clears `error` only inside `load()`, and `load()` has exactly one call site — `RunnerPanel.svelte:50`, inside `onMount` |
| Every failure is a native `alert("Unknown error")` | `api/client.ts:15` — `await response.json().catch(() => ({ detail: 'Unknown error' }))`; the status code is discarded before it can be used, and a 502 with an HTML body (or a plain-text 500 from T3) always lands here |
| Deleted runs never leave the Runs tab; spinner never stops | `stores/pipelines.ts:112` `loadRecent` merges with `map.set` and never evicts; `removeRun` (line 243) and `clear` have **no component callers** |

`stores/runners.ts:62` documents the contract that was never implemented — it says `load()` is called
"on mount **and after a socket reconnect**". Only the mount half exists.

**Fix altitude: one connection-state layer, not five patches.**

1. On `ws.onopen`, refetch the snapshot (repos, cards for the selected repo, active runs, runners).
   Deltas missed during a gap can only be recovered by a snapshot — this is the fix for staleness
   *and* the reason the runner store's own docstring exists.
2. Subscribe one component to `websocketStore.status` and render a persistent "reconnecting…"
   indicator. The state is already computed; nothing reads it.
3. In `client.ts`, keep the status code: `throw new ApiError(response.status, detail)` and handle
   `detail` being an array (FastAPI's 422 shape, which today renders as `alert("[object Object]")`).
4. Make `loadRecent` reconcile — evict ids the server no longer returns within the requested window.

Item 3 is what turns T3's plain-text 500s into a message a human can act on, so it is worth doing even
before the backend handlers land.

---

## T8 — MAJOR — Nothing validates the length or shape of any name or title

**Merged from:** QA6-4, QA6-6, QA-API-13, QA4-19, QA5-7, QA5-8, QA6-9

**Verdict: CONFIRMED.**

```
POST /api/repos {"name": ""}          -> 201
POST /api/repos {"name": "   "}       -> 201
POST /api/repos {"name": "Q"*5000}    -> 201
POST /api/repos/{id}/pipelines {"name": ""}        -> 201
POST /api/repos/{id}/pipelines {"name": "Q"*5000}  -> 201
POST /api/repos/{id}/cards {"title": ""}           -> 201
```

Also stored verbatim and round-tripped: NUL bytes, control characters, a 1 MB name, RTL overrides,
zero-width characters. QA-1 chased the traversal/injection payloads to ground and found **no** path
to a filesystem or a shell — repo storage is keyed by UUID, agent-file names go into a JSON value,
git work goes through in-process dulwich. I agree with that assessment; the residue is display and
storage, not RCE.

The display residue is where this earns MAJOR:

- QA-6 measured a 5000-char pipeline name rendering an `<h3>` at **66,516 px** inside a 436 px card.
  `.card-header` is `display:flex; justify-content:space-between`, so `.card-actions` — **Edit** and
  **Run** — is pushed to x≈66,516, and `.pipelines-page { overflow: hidden }` clips it away rather
  than letting you scroll to it. *The pipeline cannot be run from the UI at all.*
- `Card.svelte` `.card-title` sets `word-wrap: break-word` but is a flex item with the default
  `min-width: auto`, so it is sized to max-content (6,445 px measured) and hard-clipped by
  `.card { overflow: hidden }` — no ellipsis, no tooltip, ~97% unreachable. Confirmed at source.
- An empty repo name renders an unlabelled but still-clickable sidebar row.

**Fix altitude: a shared constrained type, not per-field patches.** There is no base model for names
and titles anywhere, which is exactly why a dozen endpoints share the defect. Define one
`NonEmptyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]`
and use it for every name/title on every create and update schema. Independently — because a bounded
name is not a licence to skip CSS — give `.card-header h3` and `.card-title` `min-width: 0` plus
`overflow:hidden; text-overflow:ellipsis`, so the layout survives content the server did not author.

---

## T9 — MAJOR — Duplicate dispatch, and a run that is "done" while steps still run

**Merged from:** QA4-06, QA4-07, QA3-10, QA3-11, QA4-17

**Verdict: CONFIRMED.**

**Reproduction (a)** — `entry_points: ["a","a","a"]` on a one-step graph, accepted at 201:

```
run status=passed  steps_completed=1/1
  step_runs: [('a','passed',0), ('a','running',0), ('a','running',0)]
```

Three StepRuns and three containers for one step, and the run is stamped **passed** while two of them
are still `running` — against a workspace `_complete_pipeline` has already cleaned up.

**Reproduction (b)** — two edges `a→b`, one `condition: success` and one `condition: always` (a
perfectly reasonable thing to draw, and what you get by dragging the same connection twice):

```
run status=passed  steps_completed=2/2
  step_runs: [('a','passed',0), ('b','running',1), ('b','passed',1)]
```

**Reproduction (c)** — incoherent counters on every finished run I observed:

```
status=failed  steps_completed=0/2  completed_step_ids=['b','a']  current_step=0
```

A progress bar driven by `steps_completed` and a graph driven by `completed_step_ids` disagree on
screen, and `current_step` never leaves 0.

**Root cause** — `entry_points` is looped as a raw list (`pipeline_executor.py:1200`);
`steps_to_execute` is a plain list appended without de-duplication (`:3408`) and its "already active?"
guard reads a snapshot captured **before** any dispatch; and `_complete_pipeline` (`:838`) has **no
already-terminal check** — it re-sets the status, re-runs `_cleanup_workspace`, re-executes the
`on_pass`/`on_fail` trigger action and re-broadcasts, once per entry point. The state machine *is*
guarded, but it is `pop`ped on the first call, so every subsequent call falls straight through.

**Fix altitude: three small changes at one seam.** De-duplicate `entry_points` and `steps_to_execute`
(make them sets), re-read `active_step_ids` immediately before each dispatch rather than from a
pre-loop snapshot, and give `_complete_pipeline` a `if pipeline_run.status in TERMINAL: return` guard
at the top. That last line alone fixes the multiple cleanups, the duplicated trigger actions, the
repeated terminal broadcasts and the frozen `current_step`.

---

## T10 — MAJOR — `POST /run` blocks the request handler walking the whole graph

**Merged from:** QA4-05 (confirmed), QA4-01 (see verdict)

**Verdict: QA4-05 CONFIRMED, and considerably worse than reported. QA4-01 PLAUSIBLE.**

Measured, chains of steps that fail synchronously (the realistic `executor: legacy` stale-config case,
whose own error text says the value was removed in 12.6):

| chain length | `POST /run` latency | outcome |
|---|---|---|
| 100 | 3.7 s | 200 |
| 200 | **32.4 s** | 200 |
| 400 | **299.0 s** | 200 |

`start_pipeline`'s docstring says it "returns as soon as the run row exists and the entry steps are
dispatched". It does not. Any reverse proxy or browser in front of this times out and the user sees a
failed request for a run that is actually proceeding.

**On QA4-01 (RecursionError → 500 → run wedged at `running` forever):** I could **not** reproduce the
crash. At n=100/200/400 every run returned 200 and reached a proper terminal state with
`completed_at` set — no `RecursionError` in the log, no wedged run. But the mutual recursion the
reporter identified is unambiguously present in source (`_handle_graph_step_complete:3415` → `for
step_id in steps_to_execute: await self._execute_graph_step(...)`, and `_execute_graph_step:1588` →
`await self._handle_graph_step_complete(...)` on the route-error path), and they noted the threshold
depends on how deep the ASGI stack already is. I am carrying it as **PLAUSIBLE**, not CONFIRMED, and
not ranking it on its own — because the fix is identical either way.

**Fix altitude: make the traversal iterative.** A work-queue (or handing each newly-ready step to
`asyncio.create_task`) removes the relationship between chain length and both stack depth and request
latency, fixing QA4-05 and QA4-01 in one change.

---

## T11 — MAJOR — Step containers have no resource limits and there is no fan-out cap

**Merged from:** QA4-21, QA4-12

**Verdict: CONFIRMED** (limits measured; fan-out cap confirmed absent at source).

`docker inspect` of a live step container the QA stack started, this session:

```
Memory=0  NanoCpus=0  CpuShares=0  PidsLimit=<no value>  Privileged=false  ReadonlyRootfs=false
```

`Memory=0` and `NanoCpus=0` are Docker's "unlimited". The only resource key in
`backend/app/services/execution/local_executor.py` is line 774, `run_kwargs["mem_limit"] =
memory_limit`, guarded by `if memory_limit:` — i.e. a cap exists only when the *step author* asks for
one. `grep -E 'Semaphore|max_parallel|max_concurrent'` over `pipeline_executor.py` and
`local_executor.py` returns nothing: there is no fan-out cap either.

This platform's premise is executing commands an AI wrote. Combined with T22's unbounded `timeout`, a
single accepted pipeline definition can put N unbounded containers on the host for an unbounded time —
and in the shipped compose files that host also runs the LazyAF backend.

**Fix altitude: a platform default, not a per-step option.** Settings-driven defaults for `mem_limit`,
`nano_cpus`/`cpu_shares` and `pids_limit` applied to *every* step container, with the existing
per-step `memory_limit` allowed to **lower** them rather than being the only source; plus a
configurable max-parallel-steps semaphore. These two are the difference between "a bad pipeline
definition fails" and "a bad pipeline definition takes the machine down".

**Verified correct alongside:** containers are not `Privileged`, and they carry
`lazyaf.pipeline_run_id` / `lazyaf.execution_key` labels, so orphans are findable and reapable. I used
those labels to find the container I inspected.

---

## T12 — MAJOR — Deleting a pipeline or repo mid-run cascades the live run away

**Merged from:** QA2-07, QA2-08, QA3-17, QA3-18

**Verdict: CONFIRMED.**

**Reproduction**

```
run a 30s step; 6s in:
DELETE /api/pipelines/{id}            -> 204
GET    /api/pipeline-runs/{run_id}    -> 404   (immediately)
POST   /api/pipeline-runs/{run_id}/cancel -> 404
```

Measured after the step's natural end: the run's workspace volume `lazyaf-ws-55a27810-…` was still
present, and a `python:3.12` container sat `Exited (0)` un-removed — the executor's cleanup is driven
off rows that no longer exist. The step task keeps running and writing to deleted rows.

`Pipeline.runs` is `cascade="all, delete-orphan"`, so the `PipelineRun`, its `StepRun`s and its
`StepExecution`s all go while the step is executing. Same for `DELETE /api/repos/{id}`; QA-2
additionally observed the `Job` row surviving as a permanent orphan at `running`, because `Job.card_id`
is a plain FK with no cascade and `_complete_card_work` bails on `card is None`.

Related, same missing check: two simultaneous `DELETE /api/repos/{id}` both return **204** (6 of 8
deletes in my trials claimed success for a deletion that happened once).

**Fix altitude: one in-flight guard on the delete handlers.** Refuse with 409 while any run of the
pipeline (or any card/pipeline of the repo) is `pending`/`running`, or cancel every live run first and
then delete. The vocabulary already exists — `POST /api/pipeline-runs/{id}/cancel` refuses a finished
run cleanly, and `jobs.py::_cancel_adhoc_run_for_job` gets the cancel-then-write ordering right. This
is a two-line precondition on two handlers, not an architecture change.

---

## T13 — MAJOR — `.lazyaf/pipelines/*.yaml` is a second, unvalidated definition door

**Merged from:** QA4-11, QA4-13 (YAML half), QA4-08 (execution half, ranked in T4)

**Verdict: CONFIRMED.** I pushed a corpus to a seeded repo over the real git remote.

```
GET .../lazyaf/pipelines/banana      -> 200  {"type":"banana", ...}
GET .../lazyaf/pipelines/negtimeout  -> 200  {"timeout":-5, ...}
```

The graph API rejects the identical `type: banana` at 422 with `Input should be 'agent', 'script' or
'docker'`. The YAML path accepts it, materializes it, and only tells you after it has created a run,
a workspace and a StepRun. Same asymmetry for `on_success`/`on_failure` free text (T4), `timeout`
bounds (T22), and trigger `type`.

**Root cause** — `backend/app/schemas/lazyaf_yaml.py:53` types `PipelineStepYaml.type` as
`str = Field("script", …)` where the graph path uses the `StepType` enum.

**Fix altitude: make the two doors share one schema.** This is not "add an enum to one field" — it is
that `PipelineStepYaml` and `PipelineStepV2` are two independent definitions of the same concept and
only one is validated. Either derive the YAML model from the graph model, or at minimum give every
overlapping field the identical constrained type, and add a test that asserts the two paths agree on
a shared table of accept/reject cases.

---

## T14 — MAJOR — A malformed pipeline YAML vanishes silently and 500s on fetch

**Merged from:** QA4-09, QA4-16

**Verdict: CONFIRMED.** Seven files pushed; three listed.

```
GET  .../lazyaf/pipelines           -> 200, 3 entries; alist/nullish/scalar/empty all ABSENT
GET  .../lazyaf/pipelines/alist     -> 500 {"detail":"Error parsing pipeline file:
       app.schemas.lazyaf_yaml.PipelineYaml() argument after ** must be a mapping, not list"}
GET  .../lazyaf/pipelines/nullish   -> 500 (… not NoneType)
GET  .../lazyaf/pipelines/scalar    -> 500 (… not str)
GET  .../lazyaf/pipelines/empty     -> 404 "Pipeline not found"   (the file exists; 0 bytes)
```

Three defects in one: `list_repo_pipelines` swallows the failure with a bare `print(); continue`
(`routers/lazyaf_files.py:181` — not even the logger), so the user's pipeline is simply *gone* with no
diagnostic anywhere they can see; the get-one endpoint answers **500** for what is unambiguously a
client-side content error; and the raw Python exception including the internal module path
`app.schemas.lazyaf_yaml.PipelineYaml` is pasted into `detail` and lands in a UI toast.

**Fix altitude: one error-shape decision applied to the file-reading endpoints.** Return the file in
the listing with a parse-error marker (or a companion `errors` array), answer 400/422 naming the file
and the line, and log the exception server-side only. `if content:` at `:214` also needs to
distinguish "empty file" from "no file". Agents have the identical bug at `:128`, so fix them together.

---

## T15 — MAJOR — `resolve-conflicts` force-merges invented content with no conflict present

**From:** QA2-06

**Verdict: CONFIRMED.** I could not reproduce it by hand (it needs a card with a real agent-produced
branch, and my hand-built cards had none), but **QA-2's own test reproduced it end-to-end in my run**:
`test_resolve_conflicts_refuses_when_there_is_no_conflict` XFAILed, meaning the endpoint returned
non-400 for a clean, already-`done` card with caller-invented file contents including
`never_existed.txt`. The source confirms why.

**Root cause** — `backend/app/routers/cards.py:499-543`. The handler checks only that the card exists,
the repo is ingested, and `resolutions` is non-empty, then calls `git_repo_manager.resolve_and_merge`
directly. It never verifies that a conflicting merge actually exists, never checks that each resolved
`path` is one of the reported conflicting paths, never checks the card's status, and never checks
`card.branch_name is not None` (a rejected card has `branch_name = None`, which is then handed to git
as the source branch).

Secondary consequence, and the reason this is MAJOR rather than MINOR: it is a **lost-update vector**.
A conflict dialog left open in a browser tab can be submitted much later and will overwrite whatever
the default branch has become in the meantime, with no staleness check.

**Fix altitude: this endpoint's contract, plus the T2 state guard.** Re-run `merge_branch`, require a
`conflicts` result, and reject any resolution path not in it — that is this handler's own bug. The
missing status check is the shared T2 gate and should come from there, not be hand-rolled again here.

---

## T16 — MAJOR (demo) — The seeded review card points at a branch that does not exist

**Merged from:** QA3-19, QA2-09

**Verdict: CONFIRMED.**

```
POST /api/test/seed   -> in_review card with branch_name "lazyaf/seed-review"
POST /api/cards/{id}/approve
  -> 400 {"detail":"Merge failed: Branch 'lazyaf/seed-review' not found"}
POST /api/cards/{id}/resolve-conflicts
  -> 400 {"detail":"Merge failed: Branch not found"}
```

`_init_seed_git_repo` only creates the default branch (`routers/test_api.py:265-274`). Anyone demoing
or testing the approve path off the seed fixture hits a red toast on the first click.

QA-2 found the same shape organically: a card whose agent made no file changes also lands `in_review`
with a `branch_name` that was never pushed, and every subsequent action then fails with a git-level
string exposing the internal branch name.

**Fix altitude: one line in the seed fixture, plus a message fix in the general case.** Create the
branch in `_init_seed_git_repo`. Separately, a run that produced no commit should not present the card
for review at all, or should say "the agent made no changes" rather than surfacing a git error.

I am ranking this above several technically-worse findings **purely on demo visibility**: it is the
first thing a person clicks in a freshly seeded stack.

---

## T17 — MAJOR — Pipeline name goes raw into `Content-Disposition`

**Merged from:** QA-API-06, QA-API-20

**Verdict: CONFIRMED.**

| pipeline name | `GET /api/pipelines/{id}/export/yaml` |
|---|---|
| `Deploiement` | 200, `attachment; filename=Deploiement.yaml` |
| `中文-pipeline` | **500** |
| `ship-it-🚀` | **500** |
| `evil\r\nX-Injected: yes` | **connection aborted, no response at all** |
| `x; filename=other.sh` | 200, `attachment; filename=x;_filename=other.sh.yaml` |

`backend/app/routers/pipelines.py:569` interpolates the name into the header. Starlette latin-1
encodes header values, so anything above U+00FF raises `UnicodeEncodeError` → 500. CR/LF/NUL get
further and are rejected by h11 at the wire level, so uvicorn drops the connection with no status
line — the worst failure mode, because the client sees a network error rather than an HTTP error.
The filename is also unquoted, so `;` and `"` land in the header structure.

**Fix altitude: one line.** RFC 6266: a sanitised ASCII `filename="…"` fallback plus
`filename*=UTF-8''<percent-encoded>`. Name a pipeline "Déploiement 🚀" — exactly the sort of name a
demo audience types — and today the Export button produces a 500 toast.

---

## T18 — MAJOR — YAML export is lossy and, for graphs, not re-importable

**Merged from:** QA-API-07, QA4-10a/b/c

**Verdict: CONFIRMED.** Both halves, verified by round-trip.

**Legacy export drops everything but name/type/config.** Stored:

```json
[{"name":"build","type":"script","config":{"command":"make"},"on_success":"deploy",
  "on_failure":"rollback","timeout":7200,"continue_in_context":true}, …]
```

Exported: `name`, `type`, `config` only — `timeout`, `on_success`, `on_failure`,
`continue_in_context` and the **entire `triggers` block** are gone, at HTTP 200 with no warning.

**Graph export emits a shape the importer cannot read.** `steps` comes out as a **mapping** while
`PipelineYaml.steps` is `list[PipelineStepYaml]`; `on_success` carries edge *targets* (a bare id, or a
list of them on a fan-out) which are not in the action vocabulary at all and fall into T4's
"unknown action → stop, report the step's verdict" path; `timeout` is dropped; and `entry_points` /
`version` are emitted but have no corresponding fields, so they are discarded on the way back in.

Committing LazyAF's own export to `.lazyaf/pipelines/` makes the pipeline silently vanish from the
listing (T14) — an operator who exports, commits and re-imports gets a *different* pipeline.

**Fix altitude: pick one direction and make it total.** Either make the exporter emit the importer's
schema (and add a round-trip property test asserting `import(export(p)) == p`), or stop offering
export for v2 graph pipelines until it does. Patching individual dropped fields will not help while
the container shape is wrong.

QA-4's separate note that a 393-byte YAML alias bomb inside a `dict[str, Any]` step config produced a
1.9 MB response is credible and mechanically sound; I did not re-measure it and am folding it into
this finding's fix as "cap the parsed size on the YAML path".

---

## T19 — MAJOR — Ingested repo says `main`, its git HEAD says `master`

**From:** QA-API-08

**Verdict: CONFIRMED.**

```
POST /api/repos/ingest {"name":"hd","default_branch":"main"}
GET  /api/repos/{id}          -> "default_branch": "main"
GET  /api/repos/{id}/branches -> "default_branch": "master"
GET  /git/{id}.git/HEAD       -> "ref: refs/heads/master"
```

`backend/app/services/git_server.py:59` calls `DulwichRepo.init_bare()`, which writes
`ref: refs/heads/master` and is never told otherwise, while the `Repo` row keeps the requested value.
Two sources of truth, silently disagreeing: the UI shows `main`, a `git clone` of the clone-url lands
on `master`, and `/branches` and `/commits` resolve the default differently from the repo detail panel.

**Fix altitude: one line.** Set HEAD to the repo's `default_branch` immediately after `init_bare`.

---

## T20 — MAJOR — The usage manifest accepts impossible accounting

**From:** QA-API-10

**Verdict: CONFIRMED** (QA-1's method is sound: the endpoint validates the body before checking auth,
so a `401` proves the body passed schema validation. My first probe returned 422 because I omitted
required fields; with a complete manifest, every one of these reaches 401).

| field | value | result |
|---|---|---|
| `input_tokens` | `-5` | 401 — accepted by the schema |
| `output_tokens` | `-1` | 401 |
| `wall_clock_ms` | `-99999` | 401 |
| `container_seconds` | `-1.0` | 401 |
| `gpu_fraction` | `99999.0` | 401 (`1.0` is documented as "exclusive") |
| `cost_usd` | `-1000000` | 401 |
| `cost_usd` | `"NaN"` | **422 `finite_number`** — correctly rejected |

`backend/app/schemas/usage.py:56-73`: every numeric field is a bare `int | None` / `float | None` with
no `Field(ge=…)`. `RunUsageRollup.build` sums whatever it is given, so one misbehaving runner shows a
negative duration or a negative spend on the usage dashboard.

**Fix altitude: field constraints on one model.** `ge=0` on the counters and durations,
`0 < gpu_fraction <= 1`, `ge=0` on cost. Credit where due: `cost_usd` *is* already guarded against
non-finite Decimals — the right instinct applied to exactly one field. Extend it to the rest.

---

## T21 — MINOR — Playground `internal/*` endpoints are unauthenticated

**From:** QA-API-11

**Verdict: CONFIRMED.**

```
POST /api/playground/no-such-session/internal/status {"status":"totally-made-up"} -> 200 {"ok":true}
POST /api/playground/no-such-session/internal/log    {"lines":["injected"]}       -> 200 {"ok":true}
GET  /api/playground/no-such-session/status                                        -> 404  (read side is right)
```

No `Authorization` header is taken by any of the four `internal/*` handlers
(`routers/playground.py:226-257`), and the status string is unvalidated.

**Fix altitude: apply the treatment `/api/steps/*` already got.** `routers/steps.py` documents
"zombie-token hardening (12.3 adversarial review)", requires an `Authorization` header on all five
endpoints and answers 409 on terminal executions. The playground path is the same class of
container→backend callback and never got the same pass. This is a consistency fix, not a new design.

---

## T22 — MINOR — `timeout` has no bounds and three readers disagree about `0`

**From:** QA4-13

**Verdict: CONFIRMED.**

`timeout` values `-1`, `0` and `999999999` (~31 years) are all accepted at 201 by both definition
paths. Running one:

```
run status=failed
  step failed: "step timed out after -1s"
```

LazyAF created a container, started it, then immediately killed it, and told the user about a negative
duration. QA-4 further documents that `0` is read as `0` at `pipeline_executor.py:1635` and `:3003`
but coerced to the default at `:1799` — the site that actually runs the step.

**Fix altitude: one constrained field, three declarations, one reader.**
`timeout: int = Field(300, ge=1, le=<policy max>)` on `PipelineStepV2`, `PipelineStepConfig` and
`PipelineStepYaml` (this is the same two-doors problem as T13), and collapse the three read sites into
one helper.

---

## T23 — MINOR — Step/runner JWT secrets default to published constants

**From:** QA-API-18

**Verdict: CONFIRMED at source.**

`backend/app/services/control_layer/auth.py:14` —
`_SECRET_KEY = "lazyaf-step-auth-secret-key-change-in-production"`, with the same shape for
`runner_auth_secret` at `config.py:77`. Both **are** overridable
(`LAZYAF_STEP_AUTH_SECRET` / `LAZYAF_RUNNER_AUTH_SECRET`, wired at `main.py:43-46`), which is why this
is MINOR and not MAJOR. The gap is that nothing warns when the default is still in use: a self-hosted
deployment that never sets the variable accepts step tokens minted from a constant in the public
source tree, letting anyone who can reach the backend write logs, status, usage and test results into
any step.

**Fix altitude: one startup check.** Log a WARNING (or refuse to start outside test mode) when the
configured secret still equals the module default — the same treatment `WEB_CONCURRENCY > 1` already
gets a few lines below in `main.py`.

---

## T24 — POLISH — Assorted, verified

All confirmed; grouped because none justifies its own section.

| item | evidence | fix altitude |
|---|---|---|
| `echo=True` hardcoded on the engine (`database.py:15`) | Confirmed. Backend logs are almost entirely SQL; finding a traceback required filtering thousands of lines. **Not** the cause of T5 — I measured 15 ms/request with echo on. | Env-gate it. Worth doing for incident legibility, not for throughput. |
| "1 steps" | `{pipeline.steps.length} steps`, unconditional in both branches of `PipelinesPage.svelte` (lines 207, 263) | One-line pluralize |
| QUICKSTART documents a branch that cannot exist | `QUICKSTART.md:180` says `git merge lazyaf/card-123-feature-name`; real branches are `lazyaf/{job_id[:8]}`, and since `lazyaf` is also the remote name the real command is `git merge lazyaf/lazyaf/3f9a2b1c` | Doc fix; call out the remote/prefix collision, it reads like a typo |
| `runner-agent` starts by default | `docker-compose.yml:54` has no `profiles:` key, unlike `runner-agent-e2e` at `:153` | Doc or compose fix |
| Duplicate-name conflicts use two status codes | agent-files → **400**, prompt-templates → **409**, same class of conflict | Pick 409; the T3 IntegrityError handler is the natural place |
| `commits?limit` unbounded | `-1`, `0`, `1000000000000` all 200; every sibling bounds it (`/api/pipeline-runs` is `ge=1, le=100`) | One `Query(ge=1, le=…)` |
| `PATCH /api/user-stories/{id}` silently ignores `feature_id` | 200, `feature_id` unchanged. Pydantic's default `extra='ignore'` drops it | Either implement the move or 422. Applies to *every* PATCH on the service |
| `PATCH` leaves `steps` and `steps_graph` both populated and disagreeing | Confirmed: `steps` still `[{"name":"LEGACY"…}]`, `steps_graph` holds what actually runs. The YAML materializer already nulls the other side (`trigger_service.py:95`); `update_pipeline` does not | Mirror what the materializer already does |
| `PipelineStepV2.id` is decorative | `{"KEY": {"id": "DECLARED", …}}` accepted at 201; the executor keys off the dict key and the declared id is never read | Enforce `key == value.id` in `validate_graph_integrity`, or drop the field |
| RTL override in a title reverses displayed text | QA-5 measured stored ≠ displayed | `unicode-bidi: isolate`, or strip U+202A–U+202E / U+2066–U+2069 |
| Card modal creates no history entry, so Back leaves the app | QA-5 | Push a history entry on modal open |

---

# Patterns

The patterns are worth more than the instances. Five of them account for 21 of the 24 findings.

### P1 — There is no shared "is this transition legal" primitive

`start` and `retry` each hand-roll a status guard; `approve`, `reject`, `resolve-conflicts` and
`PATCH status` have **none**. That single absence produced nine separate reported findings across five
lanes (T2, T15, and the state half of T6), which the lanes each described as a different bug because
each reached it through a different door — the API, the board's drag handler, a running card, a race.
`retry_card:567` proves the pattern is understood in the same file. **Fix the primitive, not the six
handlers.**

### P2 — Every bare 500 in this entire QA pass is one un-caught SQLAlchemy exception

`IntegrityError` (NOT NULL and UNIQUE), `StaleDataError`, `OverflowError`, `TimeoutError` (QueuePool).
Not one of them has a handler; all of them escape to uvicorn as the literal text `Internal Server
Error`. And because they escape *unhandled*, uvicorn tears down the connection — so **every 500 in
this report costs the UI two requests**, verified 6/6 against a 0/6 control. Three
`@app.exception_handler` registrations fix eight findings and halve the blast radius of anything left.
This is the highest-leverage change in the triage.

### P3 — Read-then-write with no atomicity, everywhere state matters

`start`, `retry`, `approve`, `reject`, prompt-template create, agent-file create, repo delete, pipeline
delete. Identical TOCTOU shape each time: a check, several awaits, then a write against a row that may
have moved. One pattern — a conditional `UPDATE … WHERE id=? AND <expected state>` whose rowcount is
the decision, plus an `IntegrityError` catch around inserts — closes all of them. Note that P2 and P3
are the *same* fix from two angles: make the check and the write one statement, and handle the loser.

### P4 — "No more steps I can reach" is treated as success

`_complete_pipeline(success=…)` derives its verdict from StepRuns that were *created*, so a step that
was never created cannot fail a run. A cycle, an unreachable node, an `on_success` typo and a step-less
YAML file are four descriptions of that one gap (T4). For a CI product this is the most dangerous
pattern in the report, because it fails *green* and *fast* — nothing on screen suggests a problem.

### P5 — Two definition doors, one of them unvalidated

The graph API validates step `type`, edge `condition`, `entry_points` and node `id`; the
`.lazyaf/pipelines/*.yaml` path validates none of them, and additionally swallows its own parse
failures with a bare `print()`. T13, T14, T22 and T4's step-less case are all this asymmetry. The
schemas are two independent definitions of one concept — that is the thing to fix, not the individual
fields.

### Two smaller ones

**P6 — one serialization decision, four rendered symptoms.** Naive `datetime.utcnow()` in every model
produces the negative durations, the future timestamps, the frozen `ws 0s` and the `NaNm NaNs`. Four
"findings", one line. And `formatDuration` is copy-pasted three times with a fourth near-variant, so
the *frontend* fix has to be made four times while the *backend* fix has to be made once.

**P7 — validation exists as a per-field afterthought, not a shared type.** No name or title field
anywhere carries `min_length`, `max_length` or a strip. That one absence produces the ghost sidebar
row, the 66,516 px heading that pushes **Run** off-screen, the clipped card title and the 1 MB name.

### What held up

This deserves saying plainly, because it is signal too. Across six adversarial lanes:

- **No SQL injection.** Every payload reached the database as a bound parameter; the routers use
  SQLAlchemy `select()` throughout with no string-built SQL.
- **No shell injection.** Git work goes through in-process dulwich, so `; rm -rf /`, `$(id)`, `--all`
  and `--upload-pack=` are treated as ref strings and cleanly rejected.
- **No XSS.** Svelte's default escaping holds; QA-5 instrumented `window.alert` rather than merely
  observing, and nothing fired.
- **No path traversal.** Repo storage is keyed by UUID; URL-encoded traversal does not escape the route.
- **Cancellation is genuinely correct.** Cancel storms converge on one `cancelled`, cancelling a
  terminal run is refused, the stale-completion guard in `agent_run._complete_card_work` holds against
  a straggler step task, and no run wedged. This is the one lifecycle path that was clearly designed
  rather than accreted — and it is the shape the paths in P1 should copy.
- **Path parameters, content types, body handling, deep nesting and large payloads** were all probed
  hard and answered with precise 4xx.
- Containers are not privileged and carry findable labels.

The platform's *data layer* held. What did not hold is its **error handling, its state machine, and
its completion invariant** — three seams, not a hundred bugs.

---

# Regression test grading

I ran every pytest suite the lanes authored, against a reset stack.

## Sound — merge these

| Suite | Result | Note |
|---|---|---|
| `tdd/qa/test_api_fuzz_findings.py` (83) | 55 passed / 47 xfailed combined, **0 misfires** | The best of the set. Assertions state correct behaviour, xfail reasons cite file:line, and the 46 non-xfail guards lock genuinely-correct behaviour (bound query params, cascade deletes, 404 shapes, `retry`'s state guard). Every xfail matched what I reproduced independently. |
| `tdd/qa/test_qa2_state_machine.py` (30) | 14 passed / 16 xfailed, clean | Drives *real* card runs end to end rather than asserting on source. It is what upgraded T15 from "plausible" to CONFIRMED for me. |
| `tdd/qa/test_qa5_timestamps.py`, `test_qa5_card_state_machine.py`, `test_demo_polish_api.py` | clean | Small, precise, correctly scoped. QA-5 deliberately defined local fixtures rather than depending on the shared `conftest.py` another lane was rewriting — good judgement. |
| `test_graph_definition_qa4.py`, `test_pipeline_export_qa4.py`, `test_step_resource_limits_qa4.py` | clean xfails | The resource-limit tests read `docker inspect` directly, which is the right altitude for that claim. |
| `frontend/e2e/qa/qa5-ui-workflow.spec.ts` | not run (needs a vite server) | Self-skips unless `QA5_UI_URL` is set, so it cannot disturb the existing e2e lane. Assertions read correctly. |

## Testing the wrong thing — fix or delete before merging

**1. `tdd/qa/test_qa3_workspace_race.py` — both tests XPASS(strict); the suite is red.**
Both `test_parallel_entry_points_do_not_destroy_the_shared_workspace` and
`test_step_errors_never_leak_raw_docker_client_text` **passed**, which under `strict=True` is a
failure. I independently ran the same shape 10 times: 10/10 clean. The finding needs daemon contention
the test does not create, so as written these will fail on any idle machine — the opposite of a useful
regression test. Either make the test create the contention (start a sibling volume operation
deliberately) or drop the strict marker and the finding.

**2. `test_qa3_duplicate_starts.py::test_simultaneous_agent_file_create_yields_exactly_one_row` —
authored as a guard, encodes a false belief, and fails.**
It asserts `{201: 1, 400: N}` and locks that in as correct behaviour. Actual: `{500: 6, 400: 3, 201: 1}`.
This is not a broken test so much as a **finding filed in the wrong column** — QA-3 listed it under
"verified NOT a bug" and QA-1 independently mis-stated the constraint. Convert it to a strict xfail
under T3.

**3. `test_graph_execution_qa4.py::test_cycle_reports_pass_having_run_one_step` — missing its marker.**
QA-4's report cites this as the test for QA4-03 and states that every finding-encoding test carries
`xfail(strict=True)`. This one does not. It asserts the correct behaviour
(`not (passed and steps_completed < steps_total)`) with no marker, so it **fails today** and leaves the
QA-4 suite red by default — which destroys the "xfails turn into a loud XPASS when fixed" contract the
whole suite depends on. One-line fix; must be fixed or the suite is unusable as a gate.

**4. `test_qa3_concurrent_readers.py::test_a_single_run_list_request_is_not_pathologically_slow` —
XPASS(strict); its premise is wrong.**
It encodes QA3-13 ("0.6–1.6 s on a nearly-empty database … `echo=True` is a large part of it"). It
passes. My measurement agrees: 15 ms on a 41-run database. Delete this test and the finding.
Its sibling `test_concurrent_readers_of_the_run_list_do_not_get_500s` **correctly xfailed** — it
builds its own ~300-step-row volume first, which is the actual precondition. Keep that one, and
rewrite its xfail reason, which currently blames `echo=True`.

**5. `frontend/e2e/qa/demo-polish.spec.ts` — sound specs, unsound placement.**
The assertions are good and the `page.route` fixtures are byte-accurate to the backend's wire format
(a defensible choice for pure rendering bugs on a shared stack). But unlike QA-5's spec it has **no
env guard**, and `playwright.config.ts` sets `testDir: './e2e'` — so all 12 specs, 9 of them
`test.fail`, get collected into the project's default `npm run test:e2e` run against whatever
`FRONTEND_URL` is. Add the same opt-in skip QA-5 used. Its header comment also says "specs marked
`fixme`" while the specs use `test.fail`.

**Correctly non-strict, no action:** `test_qa3_state_races.py::test_concurrent_cancels_never_return_500`
is deliberately `strict=False` because the reporter could not reproduce it on an idle daemon. That was
the right call and it earned its keep — it xfailed in my run, so QA3-14 does still occur intermittently.

---

# Refuted findings

Stated plainly so nobody spends time on them.

**QA3-1 (BLOCKER, "parallel steps destroy each other's workspace; ~1 run in 3") — PLAUSIBLE, not
reproducible.** 20 trials on an idle daemon (10 mine, 10 from the lane's own test) passed 20/20, and
the lane's own strict test now XPASSes. What the reporter missed is that their measurement was taken
while four sibling QA lanes were hammering the same Docker daemon; their own environment caveat says
so, and the 6/18 failure rate is consistent with contention rather than a deterministic defect. The
code they identified is real — `workspace_service.py:312` treats a `CREATING` row as replaceable with
no age check and force-removes the volume — so the fix they propose (give the `CREATING` branch an age
threshold, reuse the 15-minute `stuck_threshold` that `audit_orphans` already uses) is worth making as
hardening. It is not a blocker.

**QA3-2 (MAJOR, "raw Docker client errors shown to the user") — REFUTED as a live defect.** It is
entirely contingent on QA3-1 firing, which it does not. No step error I produced contained Docker
client text. Sanitising executor errors is still good practice, but there is nothing to reproduce.

**QA3-13 (MAJOR, "a single run-list request takes 0.6–1.6 s on a near-empty DB") — REFUTED.**
Measured 14–31 ms across 5 trials on a 41-run database, and `limit=20` was no different. The reporter
attributed this to `echo=True` on the engine; that attribution is wrong, and acting on it would have
produced a fix that changed nothing. The real driver is step-row volume plus full log serialization
(T5), which their sibling test measures correctly.

**"A step with `type: banana` makes `GET /api/pipelines` return 500 for every caller" (QA-3, "observed
but belongs to another lane") — REFUTED for the API path.** `type: banana` is rejected at 422 on
create, and both `GET /api/pipelines` and `GET /api/repos/{id}/pipelines` returned 200 with such rows
in the database. The 500s QA-3 saw in the shared log almost certainly came from a *YAML-materialized*
row, since that path does not validate `type` (T13) — which makes it an argument for T13, not a
separate finding.

**QA-API-16 (`database is locked` → bare 500) — NOT REPRODUCED.** The reporter was explicit that they
could not reproduce it on demand and reported it on log evidence alone, which was the honest call. I
could not reproduce it either. The configuration gap they identified is real (no `connect_args
{"timeout": …}`, no WAL) and is folded into T3's fix list; it does not need its own entry.

**QA-6's note that the concurrent prompt-template 500 "was fixed mid-session by the parallel
implementation wave" — REFUTED.** It is not fixed. Three trials of 20 concurrent creates today:
`{409:15, 201:1, 500:4}`, `{500:10, 201:1, 409:9}`, `{500:11, 409:8, 201:1}`. QA-6 pinned a guard-rail
test on the *sequential* path, which was always correct; the race was never the same thing.

---

# Suggested fix order

Ordered by (leverage × demo-visibility) ÷ effort, not by severity.

1. **T1** — emit tz-aware UTC. One backend change; repairs four rendered symptoms. Highest
   visibility-per-line in the entire report.
2. **T3, item 3** — an app-level exception handler that returns JSON instead of letting exceptions
   escape. One registration; halves the blast radius of every remaining 500.
3. **T2 + T6** — the card state machine and the conditional-UPDATE pattern, done together. They are one
   seam and nine reported findings.
4. **T3, items 1–2** — the `IntegrityError` and `RequestValidationError` handlers. Closes the
   duplicate-name races and the `NaN` trap service-wide.
5. **T4 + T9** — the completion invariant and the terminal-run guard. `if pipeline_run.status in
   TERMINAL: return` at the top of `_complete_pipeline` is one line and fixes four symptoms.
6. **T5** — drop `logs` from the run-list serializer. One line, and it is what a demo dashboard polls.
7. **T16, T17, T19, T24** — the one-line demo repairs: seed the branch, RFC-6266 the filename, set git
   HEAD, pluralize "1 step".
8. **T7** — the frontend connection-state layer. Larger, but it is what makes every backend fix above
   actually legible to a user.
9. **T11** — container resource defaults and a fan-out cap. Not demo-visible, but it is the difference
   between "a bad pipeline definition fails" and "a bad pipeline definition takes the host down", and
   this platform's premise is running commands an AI wrote.
10. Everything else.
