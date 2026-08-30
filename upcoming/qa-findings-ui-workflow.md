# QA-5 — UI workflow abuse (the human path)

**Lane:** QA-5 · **Date:** 2026-08-30 · **Target:** isolated QA stack `http://localhost:8790`
**Frontend under test:** vite dev servers on `:5191` / `:5192` with `VITE_BACKEND_URL` pointed at the
QA backend. The dev stack (`:8000`), its frontend (`:5173`) and the e2e lane (`:8765`) were never
touched.

## How this lane was run

The frontend is not part of `docker-compose.qa.yml`, so I started my own vite dev servers against the
QA backend and drove them with a real browser:

```bash
cd frontend
VITE_BACKEND_URL=http://localhost:8790 npx vite --port 5191 --strictPort   # direct
VITE_BACKEND_URL=http://localhost:8796 npx vite --port 5192 --strictPort   # via a killable forwarder
```

The app loaded and every flow below was exercised in a live browser, not simulated.

To test "the backend dies while the UI is open" **without disrupting the peer QA lanes sharing this
sandbox**, I did *not* stop `backend-qa`. Instead I put a trivial TCP forwarder (`8796 -> 8790`)
between vite and the backend and killed/restarted that process. From the browser's point of view this
is identical to a dead backend — API calls fail and the WebSocket drops — but no other agent's stack
was interrupted.

### Environment caveats (read before triaging)

Two things about this run are environmental, not product defects, and are excluded from the findings:

1. **The QA stack is shared.** Peer QA lanes call `POST /api/test/reset` continuously; my repos and
   cards were wiped roughly every 3–5 minutes mid-flow. Where a probe needed persisted data I
   re-seeded and re-ran. Nothing below depends on data surviving a peer reset.
2. **`docker-compose.qa.yml` bind-mounts `./backend/app`**, and a 5-agent implementation wave is
   editing that tree live, so uvicorn `--reload` restarts constantly. I saw transient `500`s and
   empty response bodies from `/api/runners` during those reloads. **I am not reporting those as
   backend bugs.** They are, however, the *trigger* for finding 5 — and a backend restart is a
   completely normal production event, so the frontend's reaction to one is fair game.

---

## Findings

### 1. BLOCKER — Every timestamp renders hours off, and every live duration renders NEGATIVE

**Severity:** BLOCKER — this is on screen during any demo, next to anything that is running.

**Reproduction**

```bash
date '+host local now: %Y-%m-%d %I:%M:%S %p %Z'
curl -s -X POST http://localhost:8790/api/agent-files \
  -H 'Content-Type: application/json' -d '{"name":"tz-probe","content":"# x"}'
```

Observed on a US-Eastern (UTC−4) machine:

```
host local now: 2026-08-30 06:51:01 AM EDT
{"name":"tz-probe", ..., "created_at":"2026-08-30T10:49:54.189646"}
```

Then open that agent file in the sidebar's Agents panel. The modal renders:

```
Created:  8/30/2026, 10:51:13 AM      <-- while the browser's own clock read 6:51:23 AM
Updated:  8/30/2026, 10:51:13 AM
```

The same value fed through the UI's own duration formatter, measured live in the page:

```json
{
  "backendTimestampAsSent":    "2026-08-30T10:49:27.360473",
  "parsedByBrowser":           "Sun Aug 30 2026 10:49:27 GMT-0400 (Eastern Daylight Time)",
  "browserNow":                "Sun Aug 30 2026 06:49:31 GMT-0400 (Eastern Daylight Time)",
  "RENDERED_DURATION_running": "-14396s",
  "RENDERED_DURATION_job":     "-14396s",
  "unparseable_NaN_case":      "NaNm NaNs"
}
```

**What happened.** A row created one second ago is displayed as created four hours in the future, and
a job or pipeline step that just started shows a duration of **`-14396s`**. Any unparseable timestamp
renders **`NaNm NaNs`**.

**What should happen.** A row created a second ago reads as "a few seconds ago" / `0s`. Durations are
never negative and never `NaN`.

**Root cause.** Two halves:

- The backend stores and serialises **naive UTC**: `default=datetime.utcnow` throughout
  (`backend/app/models/card.py:56-57`, `repo.py:22`, `job.py:30`, `pipeline.py:47-48`,
  `agent_file.py:17-18`, `spec.py:44-45`, `runner.py:86-89`). FastAPI emits it with **no `Z` and no
  offset**: `"2026-08-30T10:49:27.360473"`.
- Per the ECMAScript spec, a date-*time* string with no designator is **local time**, so every
  consumer is wrong by the client's whole UTC offset:
  `frontend/src/lib/pages/PipelinesPage.svelte:119-133`,
  `frontend/src/lib/components/PipelineRunViewer.svelte:123-131`,
  `frontend/src/lib/components/JobStatus.svelte:94-103`,
  `frontend/src/lib/components/AgentFileModal.svelte:142,146`.

The formatter's guard makes it worse rather than better — `if (seconds < 60) return ...` is *true* for
negative values, so the user sees the raw negative second count instead of a clamped `0s`.

**Suggested fix.** Emit aware UTC (`datetime.now(timezone.utc)`, or append `Z` at serialisation
time) — that repairs every consumer at once. Additionally clamp defensively in `formatDuration`:
`if (!Number.isFinite(seconds) || seconds < 0) return '—'`.

**Regression test:** `tdd/qa/test_qa5_timestamps.py` — 3 × `xfail(strict=True)`.

---

### 2. MAJOR — The board never resyncs after a dropped WebSocket, and diverges from the server permanently

**Reproduction**

1. Open the board with a repo selected.
2. Make the backend unreachable (kill the forwarder, or stop the backend).
3. While it is down, change data server-side:
   ```bash
   curl -X PATCH http://localhost:8790/api/cards/$CID -H 'Content-Type: application/json' \
        -d '{"title":"MISSED-WHILE-SOCKET-DOWN","status":"done"}'
   curl -X POST http://localhost:8790/api/repos/$RID/cards -H 'Content-Type: application/json' \
        -d '{"title":"CREATED-WHILE-SOCKET-DOWN"}'
   ```
4. Bring the backend back. Wait well past the 3s reconnect timer. **Do not refresh.**

**What happened.** The socket reconnects correctly — verified, a *subsequent* change arrived live and
moved a card between columns in about a second. But everything missed during the outage is **lost
forever**:

| | server truth | UI, minutes later, socket healthy |
|---|---|---|
| renamed card | `done` / `MISSED-WHILE-SOCKET-DOWN` | `todo` / old title |
| new card | present | **absent** |

Only a manual F5 repairs it, and nothing indicates anything is missing.

**What should happen.** On `onopen`, refetch the snapshot (repos, cards for the selected repo, active
runs, runners). The socket delivers *deltas*; deltas missed during a gap can only be recovered by a
snapshot.

**Root cause.** `frontend/src/lib/stores/websocket.ts` — `ws.onopen` sets `status.set('connected')`
and clears the reconnect timer, and does nothing else.

This is a *documented but unimplemented* contract. `frontend/src/lib/stores/runners.ts:62-66` says of
`load()`:

> Fetch the whole registry. Called on mount **and after a socket reconnect** (deltas that arrived
> while the socket was down were never seen, so the in-memory map is stale by an unknown amount and
> only a snapshot can fix it).

`runnersStore.load()` has exactly one call site in the entire app — `RunnerPanel.svelte:50`, inside
`onMount`. The reconnect half was never wired up, for the runner store or any other.

The same gap leaves the Agent Files panel stale: there is no `agent_file_*` message in
`ServerMessageType` at all, so an agent file created out of band never appears. I watched the panel
list four agent files that no longer existed while omitting one that did.

---

### 3. MAJOR — A dead backend is completely invisible in the UI

**Reproduction.** With the board open and populated, make the backend unreachable. Don't click
anything. Watch.

**What happened.** Nothing. No banner, no badge, no toast, no greyed-out state — the board keeps
rendering its last-known cards as though live. Scraped from the page while the backend was fully
unreachable, the only string matching `/error|failed|offline|disconnect/i` anywhere on it was my own
test card's title.

**What should happen.** A visible, persistent "reconnecting…" indicator whenever the socket is not
`connected`.

**Root cause.** `websocketStore` exposes a `status` store with four states
(`connecting|connected|disconnected|error`) and **zero consumers**:

```
$ grep -rn "websocketStore" frontend/src | grep -v "stores/websocket"
frontend/src/App.svelte:26:    websocketStore.connect();
frontend/src/App.svelte:30:    websocketStore.disconnect();
```

The status is computed and thrown away. Combined with finding 2, a demo can be driven for minutes
against a backend that died, confidently showing stale data.

**Regression test:** `frontend/e2e/qa/qa5-ui-workflow.spec.ts` — `test.fail`.

---

### 4. MAJOR — "Approve" on a card with no branch reports success and files it under Done

**Reproduction — pure UI, no API calls:**

1. Create a repo and a card. It lands in **TO DO**.
2. **Drag the card from TO DO straight onto the IN REVIEW column.**
3. Click the card, then click **✓ Approve**.

**What happened.** The modal closes with no error and the card moves to **DONE**. Server state:

```
status = done    branch = None    pr = None
```

Nothing was branched, nothing ran, nothing was merged. On the board that card is indistinguishable
from work that actually completed. (Asking for the diff on that same card correctly returns
`400 {"detail":"Card has no branch"}` — so the backend *knows* there is nothing there.)

**What should happen.** Either the board refuses the todo→in_review drag, or `approve` refuses a card
with no branch. Silently succeeding is the worst of the three options.

**Root cause.**

- `frontend/src/lib/components/Board.svelte` `handleDrop` special-cases only todo→in_progress; every
  other drag becomes a raw `PATCH {status}`, and the backend accepts any status jump.
- `backend/app/routers/cards.py:435` — `if card.branch_name and repo.is_ingested:` guards the whole
  merge block. When that is false the merge is skipped **and the card is still marked done**, with no
  error and no flag on the response.

The `repo.is_ingested` half of that condition means the same silent no-op also happens for a card
that *does* have a branch, if the repo was never ingested.

**Regression test:** `tdd/qa/test_qa5_card_state_machine.py` — 2 × `xfail(strict=True)`.

---

### 5. MAJOR — One transient backend blip pins "Unknown error" in the sidebar forever

**Reproduction**

1. Load the app at a moment when `GET /api/runners` fails — a backend restart or redeploy. I hit this
   naturally via uvicorn `--reload`.
2. Let the backend recover. Navigate between Board / Pipelines / Specs. Collapse and expand the
   Runners panel. Wait minutes.

**What happened.** The sidebar shows a bare red **`Unknown error`** under the Runners panel and never
stops. Measured with the backend fully healthy again:

```json
{"apiNowStatus": 200, "errorStillShown": true, "errorText": "Unknown error"}
```

Only a full page refresh clears it. Throughout, the panel also shows `0 CONNECTED / 0 IDLE / 0 BUSY`
— claiming an empty fleet rather than an unknown one.

**What should happen.** Retry, or at minimum clear the error on the next successful call, and say
something actionable: "Couldn't reach the server, retrying…".

**Root cause.** `frontend/src/lib/stores/runners.ts` — `error` is only ever cleared inside `load()`,
and `load()` is only called from `RunnerPanel.svelte:50` in `onMount`. After the first failure nothing
re-fetches, so the error is immortal. It is rendered raw at `RunnerPanel.svelte:195`. The message
text itself comes from finding 6.

---

### 6. MAJOR — Acting while the backend is down produces a native alert saying "Unknown error"

**Reproduction.** With the backend unreachable, open a card and click **🚀 Start Work**.

**What happened.** A native browser `alert()` containing exactly:

```
Unknown error
```

No mention that the server is unreachable. (I instrumented `window.alert` to capture the string, so
this is the real message, not a mis-read of an auto-dismissed dialog.)

**What should happen.** "Can't reach the server — check your connection and try again."

**Root cause.** `frontend/src/lib/api/client.ts:15-18`:

```ts
if (!response.ok) {
  const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
  throw new Error(error.detail || `HTTP ${response.status}`);
}
```

A reverse proxy in front of a dead backend answers **502 with an HTML body**, so `response.json()`
throws and the fallback `'Unknown error'` is what the user sees — the status code is discarded before
it can be used. **This is not a dev-server artifact:** `frontend/nginx.conf:11-13` proxies `/api` to
`backend:8000` in the shipped image, so production takes the identical path.

Two related sharp edges in the same three lines:

- FastAPI's `422` responses carry `detail` as an **array of objects**, so `new Error(error.detail)`
  stringifies and the user gets `alert("[object Object]")`.
- The app surfaces *all* errors through native `alert()` — 20 call sites across `CardModal`, `Board`,
  `PipelineRunViewer`, `PipelinesPage`. A modal browser dialog mid-demo is rough on its own.

---

### 7. MAJOR (demo) — There is no narrow-viewport layout; the sidebar swallows a phone screen

**Reproduction.** Load the board and set the viewport to 375×812.

Measured:

```json
{"viewport": {"w": 375}, "sidebar": {"w": 260}, "main": {"w": 115},
 "board": {"w": 83}, "column": {"w": 240},
 "newCardBtn": {"right": 403}, "anyHamburger": false}
```

**What happened.** The sidebar takes **260px of 375 (69%)**, leaving the board an **83px** window onto
240px columns. The `+ New Card` button's right edge lands at **x=403 — 28px off-screen**, so the
primary action is clipped and partly unclickable. "Search cards…" and the column headers are cut off
mid-word. There is no hamburger, no collapse, no way to reclaim the space.

**What should happen.** Below ~768px the sidebar should collapse behind a toggle and the board should
take the full width.

**Root cause.** `frontend/src/App.svelte` — the only narrow-width rule shrinks the sidebar from 320px
to 260px (`@media (max-width: 768px)`). `.app` is `display:flex; height:100vh; overflow:hidden`, so
the main pane simply absorbs the loss.

**Regression test:** `frontend/e2e/qa/qa5-ui-workflow.spec.ts` — 2 × `test.fail`.

---

### 8. MINOR — A long card title is hard-clipped mid-character with no ellipsis

**Reproduction.** Create a card whose title is one long unbroken token — a pasted URL, a branch name,
`'A'.repeat(600)` — and look at it on the board.

Measured on the rendered node:

```json
{"tag":"H3","cls":"card-title","scrollW":6445,"clientW":6445,
 "overflowWrap":"break-word","minWidth":"auto",
 "parent":{"cls":"card-header","clientW":173,"scrollW":6445},
 "card":{"clientW":205,"overflow":"hidden"}}
```

**What happened.** The title lays out **6445px** wide inside a **205px** card and is chopped by
`.card { overflow: hidden }`. About 97% of it is invisible and unreachable — no ellipsis, no tooltip,
no wrap. The good news: the blowout is contained
(`document.body.scrollWidth === document.body.clientWidth === 1440`), so the page layout itself does
not break and no rogue horizontal scrollbar appears.

**What should happen.** Wrap or ellipsise. `overflow-wrap: break-word` is already set but cannot fire,
because the `.card-title` flex item keeps `min-width: auto` and is therefore sized to max-content.

**Root cause.** `frontend/src/lib/components/Card.svelte` — `.card-title` needs `min-width: 0`
(and/or `overflow-wrap: anywhere`) for the existing `break-word` to take effect.

**Regression test:** `frontend/e2e/qa/qa5-ui-workflow.spec.ts` — `test.fail`.

---

### 9. MINOR — A Unicode RTL override in a title silently reverses the displayed text

**Reproduction.** Create a card titled `qa5 ‮EVIL‬ rtl-override`.

**What happened.** The board renders it as **`qa5 LIVE rtl-override`**. The stored title and the
displayed title differ, and the difference is invisible.

**What should happen.** Strip or neutralise bidi control characters (U+202A–U+202E, U+2066–U+2069) in
user-supplied titles, or render titles in an isolate (`unicode-bidi: isolate`).

Low real-world impact for a single-tenant demo tool; listed because it is a genuine
display-vs-stored-value mismatch, not a style opinion.

---

### 10. POLISH — Opening a card creates no history entry, so Back leaves the app

**Reproduction.** Open a card modal. Press the browser Back button.

**What happened.** The modal contributes nothing to history, so Back navigates the whole tab away from
the app — in my session, to the previously visited origin — rather than closing the modal.

**What should happen.** Either push a history entry when a modal opens so Back closes it (what users
expect from a full-screen overlay), or leave it — but note this is the one place where a demo
driver's reflex ("Back to go back") dumps them out of the product entirely.

---

## Verified NOT a bug

Each of these was actively probed and behaved correctly. That is signal too.

| # | Probe | Result |
|---|---|---|
| 1 | **XSS via card title** — created a card titled `qa5 <script>alert('xss')</script> <img src=x onerror=alert(1)>` | Rendered as literal text. No dialog fired (`window.alert` was instrumented, not merely observed), no `<img>` element created. Svelte's default escaping holds. |
| 2 | **Stale form input across modal openings** — typed title + description into New Card, hit Cancel, reopened | Both fields empty. No leak. |
| 3 | **Empty repo name** — clicked Create Repo with the name blank | Blocked by native form validation ("Please fill out this field"); no request sent. |
| 4 | **Double-click Create Card** — `double_click` on the submit button with a title filled in | One card created, not two. The button also shows a disabled `Creating…` state during the request. |
| 5 | **Acting on a card deleted out from under you** — left a card modal open, deleted the card server-side, typed an edit, clicked Save | Clean `alert("Card not found")`, modal stayed open, **the user's typed text was preserved**, Save re-enabled. No data loss, no stack trace. |
| 6 | **WebSocket auto-reconnect** — killed and restored the backend path | Socket reconnected on its own within the 3s timer and live updates resumed immediately (a subsequent PATCH moved a card between columns in ~1s). The *reconnect* works; it is the missing resync that is finding 2. |
| 7 | **Diff on a branchless card** — `GET /api/cards/{id}/diff` with `branch_name = null` | Clean `400 {"detail":"Card has no branch"}`. No traceback, no internal paths leaked into the user-facing string. |
| 8 | **Newlines in a card title** — 20 `\n`-separated lines | Collapsed to normal wrapped text inside the card; no layout break. |
| 9 | **Emoji flood in a title** — 80 × 🎉 plus ZWJ sequences | Wrapped and clipped normally; no layout break, no mojibake. |
| 10 | **Page-level layout blowout from a 6445px title** | Contained — `document.body.scrollWidth === clientWidth`. The board gains no rogue horizontal scrollbar. |
| 11 | **Duplicate rows in the repo sidebar** | Not a product bug. The same repo *name* appeared twice, but those were two distinct repos created by peer QA lanes; `reposStore.updateLocal` keys on `id` correctly. |

## Things I could not break, and coverage gaps

- **Nothing rendered `undefined` or `null`** anywhere I looked. The only bad-value rendering found is
  the `NaNm NaNs` / negative-duration path in finding 1.
- **No spinner that never resolves, and no modal that traps you.** Every modal I opened closed via
  both its ✕ and its Cancel.
- **Coverage gap — streaming/run surfaces.** The QA stack has **zero runners enrolled**, so I could
  not exercise a genuine long-running agent job end to end. That means I never reached the streaming
  log viewer, the "F5 while a run is streaming" case, or a real in-flight cancel. `PipelineRunViewer`
  and `JobStatus` are therefore covered only by source reading in this lane (which is how finding 1's
  duration math was found). **Worth a follow-up pass with a mock runner attached.**

## Overlap with other lanes

`tdd/qa/test_api_fuzz_findings.py` (a peer lane) contains
`test_approve_on_todo_card_is_currently_a_silent_done`, which reaches the same `cards.py:435` hole
from TO DO. My finding 4 documents the **drag-through-IN REVIEW** entry point — the one a mouse can
reach on the board. Both share one fix.

## Regression tests added

| File | Encodes | Marker |
|---|---|---|
| `tdd/qa/qa5_http.py` | Standalone HTTP helpers (no backend import, no shared conftest) | — |
| `tdd/qa/test_qa5_timestamps.py` | Finding 1 | 3 × `xfail(strict=True)` + 1 guard |
| `tdd/qa/test_qa5_card_state_machine.py` | Finding 4 | 2 × `xfail(strict=True)` + 2 guards |
| `frontend/e2e/qa/qa5-ui-workflow.spec.ts` | Findings 3, 7, 8 + 2 pinned non-bugs | 4 × `test.fail` |

Run the pytest lane (verified: `3 passed, 5 xfailed`):

```bash
QA_BASE_URL=http://localhost:8790 python -m pytest \
  tdd/qa/test_qa5_timestamps.py tdd/qa/test_qa5_card_state_machine.py -q
```

The Playwright specs live under `e2e/` but are **opt-in** — they `test.skip` unless `QA5_UI_URL` is
set, so `npm run test:e2e` against the `:8765` lane is unaffected:

```bash
cd frontend
VITE_BACKEND_URL=http://localhost:8790 npx vite --port 5191 --strictPort &
QA5_UI_URL=http://localhost:5191 QA5_API_URL=http://localhost:8790 \
  npx playwright test e2e/qa/qa5-ui-workflow.spec.ts
```

`xfail(strict=True)` and `test.fail()` both mean these **fail loudly when the bug is fixed**, rather
than silently passing.

## Notes for whoever fixes these

`tdd/qa/conftest.py` is shared with other QA lanes and was rewritten by one of them during this run.
My tests therefore import from `tdd/qa/qa5_http.py` and define their own fixtures locally, so they do
not break when that conftest changes again. I created files only under `tdd/qa/`, `frontend/e2e/qa/`
and `upcoming/`, and modified no existing source, test, config or compose file.
