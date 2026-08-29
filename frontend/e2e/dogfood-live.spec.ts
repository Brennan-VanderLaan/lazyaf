/**
 * E2E: dogfood-live (Phase 0c exit gate, standing rule R8)
 *
 * Proves "the UI shows it live": a pipeline run executed for real by the
 * runner-mock-e2e services appears in the UI, its status and step progress
 * stream over the WebSocket, and log lines from the actually-executed script
 * show up in the run viewer - all without a page reload.
 *
 * Two tiers of proof:
 *  1. WS-only: the browser's REST access to /api/pipeline-runs is neutralized
 *     with page.route, so every update the runs list shows can only have
 *     arrived over the WebSocket.
 *  2. Run viewer: the full pending -> running -> passed step sequence is
 *     recorded from the DOM, and the log viewer must contain output that only
 *     exists if the script really ran (a shell-computed marker, not the
 *     command text).
 *
 * Prerequisites (see e2e/README.md):
 *   docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
 *     --profile e2e up -d backend-e2e runner-mock-e2e
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, resetBackend, seedBackend, type SeedResponse } from './helpers';

// Marker computed by the shell at execution time. The runner logs the command
// text too ("Executing script step: ..."), so asserting on the EXPANDED value
// proves the step actually executed rather than merely being echoed back.
const PROOF_MARKER = 'dogfood-live-proof-42';
const PROOF_COMMAND = 'echo "dogfood-live-proof-$((6*7))"';

// Each step sleeps long enough that the UI (2s viewer poll, WS pushes) is
// guaranteed to observe the running state at least once.
const STEP_SLEEP_SECONDS = 3;

const PIPELINE_STEPS = [1, 2, 3].map((n) => ({
  name: `Dogfood step ${n}`,
  type: 'script',
  config: {
    command:
      n === 1
        ? `${PROOF_COMMAND}; sleep ${STEP_SLEEP_SECONDS}; echo dogfood-live-step-${n}-done`
        : `echo dogfood-live-step-${n}; sleep ${STEP_SLEEP_SECONDS}; echo dogfood-live-step-${n}-done`,
  },
  on_success: 'next',
  on_failure: 'stop',
  timeout: 120,
}));

let seed: SeedResponse;
let pipelineId: string;

test.beforeEach(async ({ request }) => {
  await resetBackend(request);
  seed = await seedBackend(request);

  const response = await request.post(`${BACKEND_URL}/api/repos/${seed.repo.id}/pipelines`, {
    data: { name: 'dogfood-live-pipeline', steps: PIPELINE_STEPS },
  });
  expect(response.ok(), `pipeline create failed: ${await response.text()}`).toBeTruthy();
  pipelineId = (await response.json()).id;
});

// Navigate to the pipelines page and select the seeded repo.
async function openPipelinesForRepo(page: Page) {
  await page.goto('/#/pipelines');
  const repoItem = page.locator(`[data-testid="repo-item"][data-repo-id="${seed.repo.id}"]`);
  await expect(repoItem).toBeVisible({ timeout: 10_000 });
  await repoItem.click();
  await expect(page.locator('[data-testid="add-pipeline"]')).toBeVisible({ timeout: 5_000 });
}

test.describe('Dogfood live (R8): pipeline run streams to the UI', () => {
  test('run appears in the runs list and status + step progress stream over WS with REST polling blocked', async ({ page, request }) => {
    // The WS connects on app mount; capture it to prove the channel exists.
    const wsPromise = page.waitForEvent('websocket', (ws) => ws.url().endsWith('/ws'));
    await openPipelinesForRepo(page);
    await wsPromise;

    await page.locator('.tab', { hasText: 'Runs' }).click();
    await expect(page.locator('.empty-state')).toContainText('No pipeline runs yet');

    // Neutralize the browser's REST reads of run state. From here on, anything
    // the runs list renders can only have arrived over the WebSocket.
    // (Playwright's own `request` fixture bypasses page routes, so triggering
    // the run below still works.)
    await page.route('**/api/pipeline-runs**', async (route) => {
      const url = route.request().url();
      if (/\/api\/pipeline-runs\/[^/?]+/.test(url)) {
        await route.abort(); // per-run detail/logs - nothing should ask for these here
      } else {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
      }
    });

    const runResponse = await request.post(`${BACKEND_URL}/api/pipelines/${pipelineId}/run`, {
      data: { trigger_type: 'manual' },
    });
    expect(runResponse.ok(), `run trigger failed: ${await runResponse.text()}`).toBeTruthy();

    // The run row must appear without any reload - pushed over WS.
    const runRow = page.locator('[data-testid="runs-list"] tbody tr').first();
    await expect(runRow).toBeVisible({ timeout: 20_000 });
    await expect(runRow.locator('.status-badge')).toContainText('running');

    // Step progress streams live: steps_completed increments per finished step.
    // Catching an intermediate value proves per-step updates, not just a final
    // "passed" push.
    await expect(runRow.locator('.progress-text-mini')).toHaveText(/^[12]\/3$/, { timeout: 60_000 });
    await expect(runRow.locator('.progress-text-mini')).toHaveText('3/3', { timeout: 60_000 });
    await expect(runRow.locator('.status-badge')).toContainText('passed', { timeout: 30_000 });

    await page.unroute('**/api/pipeline-runs**');
  });

  test('run viewer shows a step going pending -> running -> passed and real log lines, live', async ({ page }) => {
    await openPipelinesForRepo(page);

    // Run via the UI: opens the run viewer immediately with the fresh run.
    await page
      .locator(`[data-testid="pipeline"][data-pipeline-id="${pipelineId}"]`)
      .locator('.btn-run')
      .click();
    await expect(page.locator('[data-testid="run-viewer"]')).toBeVisible({ timeout: 10_000 });

    // Step 0 starts executing immediately and completes live, no reload.
    const step0 = page.locator('[data-testid="step"][data-step-index="0"]');
    await expect(step0).toHaveAttribute('data-status', 'running', { timeout: 30_000 });
    await expect(step0).toHaveAttribute('data-status', 'passed', { timeout: 60_000 });

    // Record the full visible lifecycle of the LAST step. The legacy path
    // creates a StepRun the moment the step starts (as "running"), so a
    // step's UI "pending" state is its absence from the timeline while the
    // run is underway. Sampling at 200ms against UI updates that arrive at
    // >= 2s intervals cannot miss a shown state, so the ordered chain
    // pending(absent) -> running -> passed must be observed - a run that
    // failed or skipped a visible state produces a different chain.
    const step2 = page.locator('[data-testid="step"][data-step-index="2"]');
    const seen: string[] = [];
    await expect
      .poll(
        async () => {
          const status =
            (await step2.count()) === 0
              ? 'pending'
              : await step2.getAttribute('data-status', { timeout: 150 }).catch(() => null);
          if (status && seen[seen.length - 1] !== status) seen.push(status);
          return seen.join(' > ');
        },
        { timeout: 90_000, intervals: [200] }
      )
      .toBe('pending > running > passed');

    // Log lines appear in the viewer: click the finished step and require the
    // shell-computed marker, which only exists in real execution output.
    await step0.click();
    const logs = page.locator('[data-testid="logs"]');
    await expect(logs).toBeVisible({ timeout: 10_000 });
    await expect(logs).toContainText(PROOF_MARKER, { timeout: 30_000 });
    await expect(logs).toContainText('dogfood-live-step-1-done');

    // The remaining steps complete and the run passes - still no reload.
    await expect(page.locator('[data-testid="step"][data-status="passed"]')).toHaveCount(3, {
      timeout: 90_000,
    });
    await expect(
      page.locator('[data-testid="run-viewer"] [data-testid="run-status"]')
    ).toHaveAttribute('data-status', 'passed', { timeout: 30_000 });
  });
});
