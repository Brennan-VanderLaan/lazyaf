/**
 * E2E: the runner panel (standing rule R8; Phase 12.6, Agent E contract 4).
 *
 * The 12.6 panel is SNAPSHOT-THEN-DELTA, and the whole point of this spec is
 * that BOTH halves are proved separately, because a store can look correct
 * with either one missing:
 *
 *  - Deltas without a snapshot: the panel is empty on every page load over a
 *    live fleet, and only fills in when some runner happens to change state.
 *    A quiet idle fleet broadcasts nothing for hours. (This is the exact
 *    regression the salvaged attempt shipped.)
 *  - A snapshot without deltas: the panel is a photograph. It was right when
 *    the page loaded and is a lie a second later, which is worse than empty.
 *
 * So each half is asserted with the OTHER channel neutralized:
 *
 *  1. SNAPSHOT: the WebSocket is blocked before the app mounts. Anything the
 *     panel renders can only have come from `GET /api/runners`.
 *  2. DELTAS: `GET /api/runners` is neutralized AFTER the initial load. Every
 *     idle -> assigned -> busy -> idle transition the panel shows can only
 *     have come over `runner_status` frames.
 *  3. RELOAD MID-STEP: with the runner BUSY, reload the page and require the
 *     panel to be populated immediately - the assertion PLAN.md names, and
 *     the one that fails if the snapshot fetch is ever deleted again.
 *
 * PREREQUISITES (see e2e/README.md). This lane needs a runner agent enrolled
 * against the e2e backend, which the compose e2e profile provides:
 *
 *   docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
 *     --profile e2e up -d backend-e2e runner-agent-e2e
 *
 * A missing agent FAILS this spec with that command in the message. It is
 * never skipped: "no runner was connected" and "the panel does not render
 * connected runners" are indistinguishable from a skip, and R4 forbids
 * buying green with a skip.
 */

import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import { BACKEND_URL, resetBackend, seedBackend, type SeedResponse } from './helpers';

/**
 * The label the e2e runner agent carries and nothing else does. Pinning on a
 * LABEL rather than on a runner id keeps the spec working when the agent's
 * hostname-derived id changes between hosts.
 */
const REMOTE_LANE_LABEL = 'remote-lane';

/** Long enough that the UI is guaranteed to observe `busy` and to survive a reload mid-step. */
const STEP_SLEEP_SECONDS = 20;

const PINNED_STEP = {
  name: 'Remote lane probe',
  type: 'script',
  config: {
    image: 'lazyaf-base:dev',
    requires: { has: [REMOTE_LANE_LABEL] },
    command: `echo remote-lane-probe-start; sleep ${STEP_SLEEP_SECONDS}; echo remote-lane-probe-done`,
  },
  on_success: 'next',
  on_failure: 'stop',
  timeout: 120,
};

interface RunnerRow {
  id: string;
  status: string;
  connection: string;
  labels: Record<string, unknown>;
}

let seed: SeedResponse;
let pipelineId: string;
let loopbackRunnerId: string;

/** Read the registry snapshot straight from the API (bypasses page routes). */
async function fetchRunners(request: APIRequestContext): Promise<RunnerRow[]> {
  const response = await request.get(`${BACKEND_URL}/api/runners`);
  expect(response.ok(), `GET /api/runners failed: ${await response.text()}`).toBeTruthy();
  return response.json();
}

function hasRemoteLaneLabel(runner: RunnerRow): boolean {
  const has = runner.labels?.has;
  return Array.isArray(has) && has.includes(REMOTE_LANE_LABEL);
}

/**
 * Wait for the loopback agent to be enrolled and idle. Fails loudly with the
 * compose command rather than skipping.
 */
async function waitForLoopbackRunner(request: APIRequestContext): Promise<string> {
  const deadline = Date.now() + 60_000;
  let last: RunnerRow[] = [];
  while (Date.now() < deadline) {
    last = await fetchRunners(request);
    const match = last.find(
      (r) => hasRemoteLaneLabel(r) && r.connection === 'websocket' && r.status === 'idle'
    );
    if (match) return match.id;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(
    `No idle runner carrying label has=${REMOTE_LANE_LABEL} enrolled within 60s.\n` +
      `GET /api/runners returned: ${JSON.stringify(last)}\n` +
      `Start the e2e runner agent with:\n` +
      `  docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml ` +
      `--profile e2e up -d backend-e2e runner-agent-e2e`
  );
}

/** The panel row for the loopback runner. Present on every route (sidebar). */
function runnerRow(page: Page) {
  return page.locator(`[data-testid="runner-item"][data-runner-id="${loopbackRunnerId}"]`);
}

async function readPanelStatus(page: Page): Promise<string | null> {
  return runnerRow(page).getAttribute('data-status');
}

test.beforeEach(async ({ request }) => {
  await resetBackend(request);
  seed = await seedBackend(request);

  // The reset wipes DB rows and in-memory singletons - including the runner
  // registry - so the agent has to be back and IDLE before anything is
  // dispatched to it. Its own reconnect backoff does that work; this waits
  // for it rather than assuming it.
  loopbackRunnerId = await waitForLoopbackRunner(request);

  const response = await request.post(`${BACKEND_URL}/api/repos/${seed.repo.id}/pipelines`, {
    data: { name: 'runner-panel-pinned-pipeline', steps: [PINNED_STEP] },
  });
  expect(response.ok(), `pipeline create failed: ${await response.text()}`).toBeTruthy();
  pipelineId = (await response.json()).id;
});

test.describe('Runner panel (R8): snapshot on load, deltas over WS', () => {
  test('renders the connected runner from the SNAPSHOT with the WebSocket blocked', async ({
    page,
  }) => {
    // Block the socket before the app mounts. The panel has no other live
    // channel, so whatever it shows came from GET /api/runners.
    await page.routeWebSocket(/\/ws$/, (ws) => ws.close());

    await page.goto('/');
    await expect(page.locator('[data-testid="runner-panel"]')).toBeVisible({ timeout: 10_000 });

    const row = runnerRow(page);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row).toHaveAttribute('data-status', 'idle');
    await expect(row).toHaveAttribute('data-connection', 'websocket');

    // The labels the dispatcher matches on are what makes a pin debuggable,
    // so the panel has to show them.
    await expect(row.locator('[data-testid="runner-labels"]')).toContainText(
      `has=${REMOTE_LANE_LABEL}`
    );
  });

  test('transitions idle -> assigned -> busy -> idle from WS DELTAS with the snapshot route neutralized', async ({
    page,
    request,
  }) => {
    await page.goto('/');
    await expect(runnerRow(page)).toHaveAttribute('data-status', 'idle', { timeout: 15_000 });

    // From here the browser cannot re-read the registry. Every state the
    // panel shows below can only have arrived as a `runner_status` frame.
    // (Playwright's `request` fixture bypasses page routes, so the API reads
    // and the run trigger still work.)
    await page.route('**/api/runners', (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    );

    // Record every distinct status the DOM passes through.
    const seen: string[] = [];
    const record = async () => {
      const status = await readPanelStatus(page);
      if (status && seen[seen.length - 1] !== status) seen.push(status);
    };
    await record();

    const runResponse = await request.post(`${BACKEND_URL}/api/pipelines/${pipelineId}/run`, {
      data: { trigger_type: 'manual' },
    });
    expect(runResponse.ok(), `run trigger failed: ${await runResponse.text()}`).toBeTruthy();

    // Sample fast enough to catch `assigned`, which lives only for the
    // round trip between the CAS commit and the agent's ACK.
    const deadline = Date.now() + 90_000;
    while (Date.now() < deadline) {
      await record();
      if (seen.includes('busy') && seen[seen.length - 1] === 'idle' && seen.length > 1) break;
      await page.waitForTimeout(100);
    }

    expect(
      seen,
      `panel never showed the runner busy from WS deltas; observed: ${seen.join(' -> ')}`
    ).toContain('busy');
    expect(
      seen[seen.length - 1],
      `panel did not return the runner to idle; observed: ${seen.join(' -> ')}`
    ).toBe('idle');
    // The whole point of the delta channel: the panel moved without the
    // snapshot route, which has been serving [] this entire test.
    expect(seen.length).toBeGreaterThan(1);
  });

  test('a RELOAD MID-STEP shows the busy runner immediately (the snapshot half)', async ({
    page,
    request,
  }) => {
    await page.goto('/');
    await expect(runnerRow(page)).toHaveAttribute('data-status', 'idle', { timeout: 15_000 });

    const runResponse = await request.post(`${BACKEND_URL}/api/pipelines/${pipelineId}/run`, {
      data: { trigger_type: 'manual' },
    });
    expect(runResponse.ok(), `run trigger failed: ${await runResponse.text()}`).toBeTruthy();

    await expect(runnerRow(page)).toHaveAttribute('data-status', 'busy', { timeout: 60_000 });

    // Reload while the step is still running. A delta-only store shows an
    // empty panel here until the NEXT transition - which for a step with
    // ~20s left is an eternity in operator time, and forever for an idle
    // fleet.
    await page.reload();

    const row = runnerRow(page);
    await expect(row).toBeVisible({ timeout: 10_000 });
    await expect(row).toHaveAttribute('data-status', 'busy');
    await expect(row.locator('[data-testid="runner-current-step"]')).toBeVisible();

    // And the reloaded page is still live: it must still see the step finish.
    await expect(row).toHaveAttribute('data-status', 'idle', { timeout: 90_000 });
  });
});
