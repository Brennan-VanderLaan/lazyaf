# QA-6 — First-run experience and demo polish

**Target:** isolated QA stack, `http://localhost:8790` (compose project `lazyaf-qa`)
**Date:** 2026-08-30 · **Host TZ:** America/New_York (UTC−4) · **Container TZ:** UTC
**Frontend under test:** vite dev server on `:5199`/`:5175` proxying to `:8790`
(the dev stack on `:8000` and the e2e lane on `:8765` were not touched)

**Regression tests**

| Path | What it covers |
| --- | --- |
| `C:\projects\lazyaf\frontend\e2e\qa\demo-polish.spec.ts` | 12 Playwright specs — 9 `test.fail` (strict xfail: they error if the bug is fixed without deleting the marker), 3 guard rails |
| `C:\projects\lazyaf\tdd\qa\test_demo_polish_api.py` | 11 pytest cases — 5 `xfail(strict=True)`, 6 guard rails |

```bash
# Playwright (never point BACKEND_URL at :8765 or :8000)
cd frontend
BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5175 \
  npx playwright test e2e/qa/demo-polish.spec.ts

# pytest
LAZYAF_QA_URL=http://localhost:8790 python -m pytest tdd/qa/test_demo_polish_api.py
```

Both suites were run twice back-to-back and are stable (`12 passed`, `6 passed 5 xfailed`).

> **Caveat on the shared stack.** Several QA agents were hitting `:8790`
> concurrently and calling `/api/test/reset`, which wiped my rows mid-flight
> more than once. Every UI finding below is therefore reproduced through
> Playwright `page.route` fixtures whose payloads are byte-for-byte what the
> real backend emits (captured from live `POST /api/repos` responses), so the
> repros do not depend on shared backend state. Backend findings were probed
> against the live stack directly.

---

## Ranked findings

### 1. BLOCKER — A running pipeline shows a negative duration (`-14399s`)

**The bug the project already shipped once is still present, unfixed.**

**Repro (no UI needed — this is the whole bug in six lines):**
```bash
curl -s -X POST http://localhost:8790/api/repos \
  -H 'Content-Type: application/json' -d '{"name":"tz-probe"}'
# => {"created_at":"2026-08-30T10:33:35.485909", ...}   <-- no Z, no offset
```
Then in any browser west of UTC:
```js
Math.floor((Date.now() - new Date("2026-08-30T10:33:35.485909").getTime())/1000)
// => -14400
```

**Observed, in the real UI** (Runs tab, a run created that instant):

| Column | Rendered |
| --- | --- |
| Status | `⟳ running` |
| Progress | `0/1` |
| Started | `8/30/2026 10:56 AM` (local wall clock was **6:56 AM**) |
| **Duration** | **`-14399s`** |

The run detail modal shows the same: `Trigger: manual Duration: -14399s`.

**What should happen:** a run started 12 seconds ago reads `12s`.

**Root cause.** Two halves that only break in combination:

1. Every model column defaults to naive UTC —
   `backend/app/models/pipeline.py:47,48,69,75,151`, and the same line in
   `card.py:56`, `repo.py:22`, `job.py:30`, `agent_file.py:17`, `spec.py`,
   `runner.py:86`, `usage.py:138`, `testref.py:73`:
   `mapped_column(DateTime, default=datetime.utcnow)`.
   Pydantic serializes a naive `datetime` with **no timezone designator**.
2. `frontend/src/lib/pages/PipelinesPage.svelte:125-133`:
   ```js
   const startTime = new Date(start).getTime();
   const endTime = end ? new Date(end).getTime() : Date.now();
   ```
   Per ECMA-262, a date-time string *without* an offset is parsed as **local
   time**. `startTime` therefore lands `getTimezoneOffset()` minutes away from
   the truth while `Date.now()` is correct, so the difference is negative
   everywhere west of UTC and inflated everywhere east.

Note `if (seconds < 60) return \`${seconds}s\`` — because −14399 < 60, the UI
prints the raw seconds rather than falling into the `m`/`s` branch. Hence the
bare `-14399s`.

**Fix (one place, not eleven):** emit tz-aware UTC from the backend
(`datetime.now(timezone.utc)`, or a pydantic field serializer that appends `Z`)
so every existing frontend call site becomes correct without being touched.
Patching only the three `formatDuration` copies leaves `formatDate`,
`toLocaleString`, and `connectionAge` still wrong.

**Tests:** `demo-polish.spec.ts` — *"a RUNNING run must not show a negative
duration"*, *"the run detail modal must not show a negative duration"*;
`test_demo_polish_api.py::test_repo_created_at_carries_a_timezone`,
`::test_pipeline_timestamps_carry_a_timezone`.

---

### 2. MAJOR — Every absolute timestamp in the UI is off by the UTC offset

Same root cause as #1, second symptom, and it is on screen constantly.

**Observed:** a run created at 6:56 AM local displays **`8/30/2026 10:56 AM`** in
the Runs table's "Started" column — four hours in the future. During a morning
demo the audience sees timestamps that have not happened yet.

**Affected call sites** (all consume the naive string directly):

- `frontend/src/lib/pages/PipelinesPage.svelte:119-122` — `formatDate`, the "Started" column
- `frontend/src/lib/components/AgentFileModal.svelte:142,146` — `new Date(...).toLocaleString()` for Created/Updated

**What should happen:** `8/30/2026 06:56 AM`.

**Test:** `demo-polish.spec.ts` — *"the Started column must show the run start in local time"*.

---

### 3. MAJOR — Runner sidebar is frozen at "ws 0s"

Same root cause, third symptom, and the *most* visible one: the runner panel is
in the sidebar on **every** page.

**Root cause:** `frontend/src/lib/components/RunnerPanel.svelte:81-89`
```js
const started = Date.parse(runner.connected_at);   // naive -> parsed as local
const seconds = Math.max(0, Math.floor((atMs - started) / 1000));
```
`connected_at` is naive UTC (`backend/app/models/runner.py:86`), so
`atMs - started` is negative and the `Math.max(0, …)` clamp pins it to zero. A
runner that enrolled an hour ago renders **`ws 0s`** for as long as the UTC
offset lasts.

This is worth calling out separately because the clamp *looks* like the
defensive fix — it is the one place someone hardened — but it converts a wrong
negative number into a wrong constant. It should be un-clamped once #1 lands,
otherwise the panel silently stays broken.

**What should happen:** `ws 30m`.

**Test:** `demo-polish.spec.ts` — *"a runner connected 30 minutes ago does not read 'ws 0s'"*.

---

### 4. MAJOR — A long pipeline name pushes the Run button out of reach

**Repro:**
```bash
curl -s -X POST http://localhost:8790/api/repos/$REPO_ID/pipelines \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"$(python -c 'print("Q"*5000)')\",\"steps\":[{\"name\":\"s\",\"type\":\"script\",\"config\":{\"command\":\"true\"}}]}"
# => 201 Created.  No length validation anywhere.
```

**Measured in the rendered card:**

| Metric | Value |
| --- | --- |
| `h3` scrollWidth | **66,516 px** |
| `.pipeline-card` clientWidth | 436 px |
| grid track scrollWidth | 66,643 px (track is 896 px) |
| `document.body` scrollWidth | 1280 px (= clientWidth) |

Because `.card-header` is `display:flex; justify-content:space-between`
(`PipelinesPage.svelte:566-570`), the 66,516 px `<h3>` shoves `.card-actions`
— the **Edit** and **Run** buttons — to x ≈ 66,516. `.pipelines-page` has
`overflow: hidden` (line 362), so they are clipped away rather than reachable
by scrolling. **The pipeline cannot be run from the UI at all.**

**Root cause:** no `max_length` on `PipelineBase.name` /
`RepoBase.name` (`backend/app/schemas/pipeline.py:132-134`,
`backend/app/schemas/repo.py:5-8`), and `.card-header h3` /
`.card-description` (`PipelinesPage.svelte:572-577, 614-619`) declare no
`overflow`, `text-overflow`, or `word-break`.

**What should happen:** bound the field server-side (e.g. `max_length=200`),
and give the heading `overflow:hidden; text-overflow:ellipsis;
white-space:nowrap` plus `min-width:0` on the flex child so the actions keep
their space.

**Tests:** `demo-polish.spec.ts` — *"a very long pipeline name must not overflow
its card"*, *"the Run button stays reachable when the pipeline name is huge"*;
`test_demo_polish_api.py::test_pipeline_name_is_length_bounded`.

---

### 5. MAJOR — Deleted runs never leave the Runs tab, and the spinner never stops

**Repro:** open Pipelines → Runs with one `running` run, then make it vanish
server-side (delete its pipeline, or `POST /api/test/reset`). The row stays,
its **View** button stays live, and the pulsing "active" dot next to the Runs
tab keeps pulsing. Only a hard reload clears it.

**Root cause:** `frontend/src/lib/stores/pipelines.ts:112-123` —
`loadRecent()` merges with `map.set(run.id, run)` and **never evicts**.
`removeRun()` (line 243) and `clear()` (line 258) exist but **no component
calls either** (`grep -rn "activeRunsStore\." --include=*.svelte` returns only
`get`/`updateRun`/`cancel`/`loadRecent`). Consequences:

- `Runs (N)` grows monotonically and can exceed what the backend holds.
- `hasActiveRuns` (line 347) scans that stale Map, so one ghost stuck in
  `running`/`pending` keeps the 3-second poll in `PipelinesPage.svelte:57-66`
  running **forever**.
- Opening a ghost run gives a modal that polls `GET /api/pipeline-runs/{id}`
  every 2s and swallows the 404 —
  `PipelineRunViewer.svelte:42-53` ends with `catch (e) { /* Ignore errors
  during refresh */ }`. The result is a **spinner that never resolves** and
  never explains itself.

**What should happen:** `loadRecent` should reconcile — drop ids the server no
longer returns (at least within the requested window) — and the run modal
should surface a "this run no longer exists" state instead of spinning
silently.

**Test:** `demo-polish.spec.ts` — *"a run that vanishes server-side must
disappear from the Runs tab"*.

---

### 6. MINOR — An empty repo name is accepted and renders an invisible sidebar row

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8790/api/repos \
  -H 'Content-Type: application/json' -d '{"name":""}'
# => 201
```
Whitespace-only (`"   \t  "`) is accepted too. `.repo-name` renders it
verbatim, producing an unlabelled row in the sidebar that is still clickable
and still selectable — a mystery entry a demo viewer cannot identify.

**Root cause:** `RepoBase.name: str` with no constraint,
`backend/app/schemas/repo.py:5-6`.

**What should happen:** `min_length=1` plus a `strip()` validator → 422.

**Tests:** `demo-polish.spec.ts` — *"a repo with an empty name still renders
something clickable"*; `test_demo_polish_api.py::test_repo_name_cannot_be_empty`,
`::test_repo_name_cannot_be_whitespace_only`.

---

### 7. MINOR — QUICKSTART.md step 4 documents a branch name that cannot exist

QUICKSTART.md says:
```bash
git fetch lazyaf
git merge lazyaf/card-123-feature-name
```

Card branches are actually named `lazyaf/{job_id[:8]}` —
`backend/app/routers/cards.py:348` and `:607`,
`backend/app/services/pipeline_executor.py:3613`. So the real branch is e.g.
`lazyaf/3f9a2b1c`, and since `lazyaf` is *also* the remote name, the correct
command is `git merge lazyaf/lazyaf/3f9a2b1c`. Nothing in the codebase ever
produces a `card-123-feature-name` branch.

Someone following the README verbatim gets
`merge: lazyaf/card-123-feature-name - not something we can merge`.

**What should happen:** document the real shape, and call out the
remote-name/branch-prefix collision explicitly since it reads like a typo.

---

### 8. POLISH — "1 steps"

The single-step pipeline a demo starts with reads **`1 steps`**.
`{pipeline.steps.length} steps` is unconditional in both the repo-card and
platform-card branches of `PipelinesPage.svelte` (`.step-count`).
Confirmed rendered value: `"1 steps"`.

**Test:** `demo-polish.spec.ts` — *"a one-step pipeline reads '1 step', not '1 steps'"*.

---

### 9. POLISH — Long card text is clipped with no ellipsis

A 5000-character card title renders as a solid unbroken stripe of glyphs with
**no ellipsis and no affordance** that there is more text. Measured on the
board: title element scrollWidth **45,703 px**, `text-overflow: clip`,
`-webkit-line-clamp: none`. The board itself does *not* overflow
(`document.body` scrollWidth == clientWidth == 1280) because an ancestor
clips — so this is cosmetic, not a layout break, unlike finding #4.

`.card-description` does have `-webkit-line-clamp: 3`
(`Card.svelte:169-171`); the **title** has no clamp and no ellipsis.

---

### 10. POLISH — QUICKSTART button labels do not match the UI

| QUICKSTART says | UI actually shows |
| --- | --- |
| Click "New Card" | `+ New Card` ✔ |
| Click "Create" | `Create Card` |
| then "Start" | `🚀 Start Work` — and only after **reopening** the card, since `canStart` requires `card?.status === 'todo'` (`CardModal.svelte:155`) |

There is also a one-click `🚀 Create & Submit` (`CardModal.svelte:831`) that the
document never mentions and which is what a demo would actually want.

Separately, the **Extras** section presents the runner as opt-in —
"`docker compose up -d runner-agent` starts a runner agent" — but
`runner-agent` carries no `profiles:` key in `docker-compose.yml:54`, so plain
`docker compose up -d` from step 1 already starts it.

---

## Needs confirmation — not counted as a finding

**DB connection-pool exhaustion returns 500 to the UI's own polling.**
The QA backend log contains repeated:
```
GET /api/pipeline-runs?limit=100 HTTP/1.1" 500 Internal Server Error
sqlalchemy.exc.TimeoutError: QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```
This matters because `PipelinesPage` polls `/api/pipeline-runs` every 3 seconds
whenever any run looks active (and per finding #5 that can be *forever*), so a
demo with a few tabs open is a plausible trigger.

**I am not claiming this as a confirmed finding**: the QA stack was under
concurrent load from several QA agents at the time, and I did not isolate a
single-user reproduction. Worth a dedicated load probe before acting on it.

---

## Verified NOT a bug

Fifteen things I actively tried to break that behaved correctly:

1. **Empty states are all populated.** Board (`📁 No Repository Selected` +
   "Select a repository from the sidebar or add a new one to get started"),
   Pipelines (`📋 Select a repository to manage pipelines`), Specs ("No
   features yet…" **plus a `Seed Milestone 12` CTA**), Playground, Runners
   ("No runners connected" + the exact `lazyaf-runner --backend-url` command),
   Agent Files ("Click + to create your first agent"). None render blank or
   error-ish.
2. **Repo with zero commits** shows "No branches yet. Push your repo to get
   started." alongside copy-paste `git remote add` / `git push` commands and a
   `READY` badge. This is the best empty state in the product.
3. **Card with an empty description** renders cleanly — title only, no `null`,
   no stray punctuation.
4. **Zero-step pipeline cannot be run** — `POST /api/pipelines/{id}/run` returns
   `400 {"detail":"Pipeline has no steps defined"}`. This is what prevents the
   `0/0` NaN below.
5. **Progress bar never renders `NaN%`.** `PipelineRunViewer.svelte:154` does
   `(steps_completed / steps_total) * 100` with no guard, which *would* emit
   `width: NaN%` at `steps_total === 0` — but #4 makes that unreachable.
   Verified rendered value: `width: 0%`. (Pinned by a guard-rail test in both
   suites so the guard cannot be removed silently.)
6. **Completed-run durations are correct.** Both operands are naive UTC so the
   offset cancels; a 90-second run renders `1m 30s`.
7. **Test pass/fail summaries are null-guarded** —
   `JobStatus.svelte:138-145` checks `!== null` before each count and hides
   zero-valued fail/skip chips. No `undefined passed`, no `NaN`.
8. **404 bodies are clean sentences.** `/api/repos/not-a-uuid`,
   `/api/pipeline-runs/{missing}`, `/api/pipelines/{missing}` all return
   `{"detail":"… not found"}` with no `Traceback`, no `sqlalchemy`, no `SELECT`,
   no `/app/` paths.
9. **Malformed JSON** → structured `422` (`json_invalid`), not a 500.
10. **Null/mistyped fields** → structured `422` (`string_type`), not a 500.
11. **`limit` query param is validated** — `-5`, `0`, `abc`, and a 21-digit
    integer all return `422`, none 500.
12. **Duplicate prompt-template name** → clean `409 {"detail":"Prompt template
    named '…' already exists"}`. (The log showed a bare 500
    `sqlite3.IntegrityError: UNIQUE constraint failed` earlier in this QA
    window; the parallel implementation wave fixed it mid-session and the
    bind-mounted backend hot-reloaded. Pinned with a guard-rail test.)
13. **Sidebar nav active state is correct.** My first screenshots looked
    off-by-one — Specs page with "Pipelines" lit, Playground page with "Specs"
    lit. That was the CSS `:hover` state trailing the mouse, not the route.
    Reading the DOM confirmed `active` sits on the right anchor
    (`hash:"#/specs"` → `nav-item … active` on `href="#/specs"`).
    **Not a defect** — though hover and active being visually identical is what
    made me chase it.
14. **`connectionAge` guards `NaN`** with `Number.isNaN(started)` before
    formatting, so a garbage `connected_at` yields `''` rather than `NaN`.
    (The *clamp* is still finding #3.)
15. **Board column counts are correct** — three cards in TO DO showed
    `TO DO 3`, `IN PROGRESS 0`, `IN REVIEW 0`. No off-by-one, no `undefined`.

Also probed without result: card titles containing `<script>`, newlines, RTL
overrides (`U+202E`) and zero-width joiners — stored and rendered as inert text,
no injection, no layout escape.

---

## Notes for whoever fixes these

- **Findings 1, 2 and 3 are one fix.** Make the backend emit tz-aware UTC and
  all three symptoms plus the `AgentFileModal` timestamps resolve together.
  Fixing the three `formatDuration` copies individually will look like it
  worked and leave #2 and #3 broken.
- **`formatDuration` is copy-pasted three times** — `PipelinesPage.svelte:125`,
  `PipelineRunViewer.svelte:124`, `JobStatus.svelte:94` — with a fourth
  near-variant in `RunnerPanel.svelte:81` and two more relative-time helpers in
  `RepoInfo.svelte:91` and `BranchManager.svelte:176`. Worth collapsing to one
  helper while touching them.
- `RepoInfo.svelte:91` / `BranchManager.svelte:176` take a **git unix epoch**
  (`timestamp * 1000`), which is timezone-correct and therefore *not* affected
  by finding #1. They are unguarded against future-dated commits (a commit with
  a skewed author date renders `-120m ago`), but I could not produce one through
  the product's own ingest path, so I am not filing it.
