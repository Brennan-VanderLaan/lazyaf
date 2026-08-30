# Frontend E2E suite (Playwright, real-backend tier)

Every spec here runs against a **live backend** — the docker-compose `e2e`
profile — through a vite dev server. Nothing is mocked below the browser.

## Running

```bash
# 1. Backend + runners (from repo root). The override enables the env-gated
#    test-mode API (/api/test/reset, /api/test/seed) that the specs depend on.
docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
  --profile e2e up -d backend-e2e runner-mock-e2e

# 2. Tests (from frontend/). Playwright starts its own vite dev server on
#    :5174 proxying to the backend, so nothing else to start — and nothing
#    else may be listening on :5174 (strictPort + no server reuse: a stray
#    process there fails the run loudly instead of being silently reused).
npm run test:e2e            # full suite
npm run test:e2e:dogfood    # just the dogfood-live spec (Phase 0 exit gate)

# 3. Teardown
docker compose --profile e2e down
```

Or use the orchestrated flow: `.\scripts\test.ps1 e2e` / `./scripts/test.sh e2e`
(builds + starts + tears down the compose stack for you; Playwright owns the
vite server either way).

## Configuration

| Env var        | Default                 | Meaning                                  |
|----------------|-------------------------|------------------------------------------|
| `BACKEND_URL`  | `http://localhost:8765` | The e2e backend (compose `backend-e2e`)  |
| `FRONTEND_URL` | `http://localhost:5174` | The vite server the browser talks to     |

Defaults match the compose e2e profile. The dedicated `:5174` port means a
developer's normal `npm run dev` (`:5173` → backend `:8000`) never collides
with this lane.

## Server-startup choice (webServer vs external)

- **Frontend: Playwright-owned, exclusively.** `playwright.config.ts` has a
  `webServer` block that starts vite on `FRONTEND_URL`'s port with
  `VITE_BACKEND_URL` pointed at `BACKEND_URL`. `reuseExistingServer: false`
  plus `--strictPort` means a stray process already on the e2e port (a
  forgotten dev server, possibly proxying to the WRONG backend) fails the run
  loudly instead of being silently reused. Nothing else pre-starts this
  server — the `scripts/test.*` wrappers only manage the compose stack.
- **Backend: external.** Playwright's `webServer` cannot tear down a compose
  stack, and half-owning its lifecycle would hide failures. Instead the
  helpers fail loudly — with the exact startup command — when the backend is
  unreachable or missing `LAZYAF_TEST_MODE`.

## Workers = 1 is load-bearing

The whole suite shares one backend, one SQLite DB, and a global
`/api/test/reset` that also wipes in-memory singletons (job queue, runner
pool, WS connections). Parallel workers would reset state out from under each
other — this is post-mortem landmine 8 from attempt #1, which raised workers
against a shared-DB global reset one hour before the branch was abandoned.
Do not raise `workers` for this tier without per-worker namespacing.

## Specs

- `dogfood-live.spec.ts` — **Phase 0c exit-gate spec (R8).** A real pipeline
  run (script steps executed by the backend's LocalExecutor) appears in the
  UI; run
  status and per-step progress are asserted with the browser's REST access to
  `/api/pipeline-runs` blocked, so the updates can only have streamed over the
  WebSocket; the run viewer shows a step's full pending → running → passed
  sequence and log lines containing shell-computed output (proof of real
  execution), all without a reload.
- `runners.spec.ts` — **Phase 12.6 runner panel (R8).** Proves both halves
  of the panel's snapshot-then-delta model, each with the OTHER channel
  neutralized: the runner appears from `GET /api/runners` with the WebSocket
  blocked, then transitions idle → assigned → busy → idle from
  `runner_status` frames with the snapshot route serving `[]`, and a reload
  MID-STEP shows the busy runner immediately. Needs `runner-agent-e2e`; a
  missing agent fails the spec with the compose command rather than skipping.
- `card-workflow.spec.ts` — board/card flows (predates the testid sweep; uses
  CSS-class selectors).
- `graph-pipeline.spec.ts` — graph editor flows (several tests skipped:
  SvelteFlow drag interactions don't respond to Playwright's synthetic mouse
  events; see comments in the spec).

## Known seams

- The run viewer (`PipelineRunViewer.svelte`) refreshes via a 2s REST poll and
  the frontend ignores `step_run_status` WS messages (`websocket.ts`), while
  the `pipeline_run_status` WS payload carries no `step_runs`. Run-level
  status and `steps_completed` ARE WS-pushed (that's what the WS-only test
  pins); per-step detail inside the viewer is not yet. When the frontend
  starts consuming `step_run_status`, tighten `dogfood-live.spec.ts` to run
  its viewer assertions under the same REST blockade.
