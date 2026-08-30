# README overhaul - maintainer notes

Written alongside the README rewrite. Kept OUT of the published README on
purpose: these are unverified claims, contradictions found in the tree, and
product-level onboarding suggestions - useful to act on, not to publish.

## Notes

### Claims I could not verify, and what I did about them

- **Exact quickstart commands.** `QUICKSTART.md`, `docker-compose.yml`, `.env.example`, and
  `docker-compose.release.yml` were all being edited by other lanes while I wrote this
  (`.env.example` changed on disk mid-session; `.github/workflows/` and `.github/scripts/`
  appeared empty and then materialised). I deliberately describe the *shape* of the two
  compose paths and link to QUICKSTART rather than printing commands that may be wrong by
  the time this ships. QUICKSTART landed its rewrite while I was writing and I reconciled
  against it (pull-from-GHCR is the default path; the CLI is `pip install ./cli` and
  explicitly **not** on PyPI; `scripts/preflight.py` now exists). **Re-check "Getting
  started" against QUICKSTART one more time before publishing.**
- **The step-image retag step.** QUICKSTART's release path requires pulling the five
  `lazyaf-*` images and retagging them to `:dev`, because the backend looks them up by local
  tag and refuses to pull. I did not put that in the README — it is exactly the kind of
  detail that belongs in QUICKSTART only — but it is a sharp edge worth knowing about; see
  the product-changes list below.
- **Default ports.** I used 5173/8000 per the current compose files; `.env.example` now
  introduces `LAZYAF_FRONTEND_PORT` / `LAZYAF_BACKEND_PORT`, so I hedged with "default".
- **`~1,700 tests`.** Taken from PLAN.md's own reporting of the last dogfood run (suite 1731
  passed; tier floors T1 1657 / T2 60 / T3 17). Recount before publishing if you want a
  precise number, or drop the figure.
- **"41 MCP tools"** is a count of `@mcp.tool` decorators in `backend/app/mcp/server.py`. I
  did not run the server to confirm all 41 register.
- **A `runner` compose profile.** The new `.env.example` refers to "the `runner` profile",
  but the `docker-compose.yml` I read defines `runner-agent` with no profile. I avoided
  naming a profile in the README; reconcile those two files.

### Where the codebase contradicts the old README (and PLAN.md)

These are the corrections that motivated the rewrite. Each is verified in code:

1. **There is no job queue and no polling runner pool.** The old README's architecture
   diagram shows `Backend → Runners (Claude/Gemini)` and its bullet list says the backend is
   a "job queue". Commit `dce14e3 "12.6: delete the polling stack (R2)"` removed it.
   `routers/runners.py` is now read-only over a WebSocket-backed registry and says so in its
   docstring; `services/workspace/execution_router.py` raises on `executor: legacy` because
   the path no longer exists; `runner-claude/`, `runner-gemini/`, and `runner-mock/` are
   deleted in the working tree. The `Job` model survives only as the card modal's read
   model — it has no lease, priority, or attempt columns, and nothing dequeues from it.
2. **Card statuses are wrong in the old README.** It says `todo → working → in_review →
   done`. The enum in `backend/app/models/card.py` is `todo | in_progress | in_review | done
   | failed`.
3. **Agent steps do not name an agent *file*.** The old README implies `agent:` selects a
   prompt template. `pipeline_executor.py:325` validates `agent:` against
   `{claude-code, gemini, mock}` and raises "there is no default" on anything else. The
   persona comes from `prompt_template` or `agent_file_ids`. Anyone copying the old README's
   mental model writes a pipeline that fails at dispatch.
4. **`step_type: script` is dead on cards.** `cards.py` 400s a card whose step type is
   `script` or `docker` (`_reject_unrunnable_step_type`, since 12.4). Cards are agent work
   now.
5. **The MCP server is not mounted on the FastAPI app.** `backend/app/mcp/server.py`'s own
   docstring claims an "SSE endpoint mounted at `/mcp`"; there is no such mount in
   `main.py`. It only runs standalone over stdio. **That docstring should be fixed.**
6. **PLAN.md's "Current Status" section is itself stale** — it still says 12.5 and 12.6 are
   NOT STARTED and that "agent steps still take the legacy card → job → queue → polling
   runner path", but HEAD is `62eb7b0 "12.6: loopback lane, ratchet, frontend, docs"` and
   both phases have landed. PLAN.md was last committed at 02:11 today, before those commits.
   Since the README now points newcomers at PLAN.md's Current Status, **that block needs an
   update before this ships**, or a first-time reader gets a description of an architecture
   that was deleted. PLAN.md's "Project Structure", "Tech Stack" ("Queue | In-memory"), and
   "API Summary" tables are stale for the same reason.
7. **`docker-compose.release.yml`'s header comment** (written by the release lane, today)
   describes the backend as carrying "the job queue". Same deletion applies.

### Product changes that would make onboarding better

Ordered by how much I think they'd help a stranger:

1. **Surface cost in the UI.** `StepUsage` is fully built — model, ingestion endpoint,
   per-run rollup at `GET /api/pipeline-runs/{run_id}/usage` — and a whole-frontend grep for
   `cost` / `token` / `usage` returns zero hits. "See what that run cost you" is one of the
   strongest reasons to choose this over a generic CI system, and right now a visitor cannot
   see it at all. A number in the run header would do it.
2. **Ship a working example repo, or a "try it" seed.** The current first-run experience is
   "ingest a repo you already have, then write YAML from scratch". A `lazyaf demo` command,
   or an example repo with a `.lazyaf/pipelines/hello.yaml` and an agent that does something
   visible with the `mock` agent (no API key needed), would let someone see the whole loop in
   two minutes without spending a token. The `mock` agent already makes this free — nothing
   else needs building.
3. **Render a live run on the graph.** `PipelineGraphEditor` already accepts `readonly`,
   `stepStatuses`, `activeStepIds`, and `completedStepIds`, and no caller passes them. Runs
   are shown as a linear list instead. Watching a DAG light up is the single most legible
   demo this product has, and it looks close to free.
4. **Say something about auth in the product, not just the docs.** No authentication
   anywhere, plus well-known default secrets for the step JWT and the runner enrolment
   token, plus an unauthenticated git server. A startup banner when the stack is bound to
   anything other than loopback with default secrets would be cheap and would stop someone
   putting this on a VPS by accident.
5. **The pull-and-retag step for step images is the roughest edge in onboarding.** A new
   user has to pull five images from GHCR and `docker tag` each one to `<name>:dev` before a
   single card or pipeline will run, because the backend resolves step images by local tag
   and (correctly) refuses to pull on their behalf. QUICKSTART handles it and preflight
   prints the commands, which is good — but it is still a manual loop over five images in
   step 5 of a quickstart. Making the release compose reference the registry tags directly,
   or shipping a one-line `scripts/pull_images.py` next to `build_images.py`, would remove
   the most likely place for a first-time user to stall.
6. **Two small always-on endpoints look like they should be gated.**
   `POST /api/repos/{repo_id}/test-setup` is documented "TEST ONLY" but is not behind
   `LAZYAF_TEST_MODE` (unlike `/api/test/*`), and `POST /api/features/seed-milestone12` is a
   dev seed on the public surface — its button is also the empty state of the Specs page,
   which reads as demo scaffolding to a new user.
7. **Repo-defined pipelines are read-only in the UI** with a 📁 icon as the entire
   explanation. A new user who writes `.lazyaf/pipelines/ci.yaml`, sees it listed, and cannot
   click into it will assume something is broken. A one-line "defined in your repo at
   `<path>` — edit the file and push" would fix it.
8. **The Specs page has no notion of tests** even though the tie-back is built and working.
   A criterion currently renders as `{text, required, notes}`. Showing "3 tests, last run
   green" per criterion — the data is already in `GET /api/criteria/{id}/history` — would
   make the spec layer legible as a feature rather than as a notes app.
