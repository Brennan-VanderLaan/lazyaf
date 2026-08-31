/**
 * E2E: Debug Re-Run Mode (Phase 12.7, standing rule R8)
 *
 * The UI half of the phase exit gate. It drives the whole operator loop
 * through the browser:
 *
 *   a run fails -> "Debug Re-run" on that run -> the modal lists the
 *   pipeline's steps as BREAKPOINT KEYS -> pick one + a commit -> a new run
 *   starts and PAUSES before that step -> the panel says which step is held,
 *   how long the hold has left, and how to join it -> Resume -> the run
 *   continues past the gate and reaches a terminal state, and the session
 *   says WHY it ended.
 *
 * Deliberately NOT covered here, and neither is a gap:
 *  - TERMINAL I/O. Attaching a shell is the CLI's path (tdd/e2e covers it
 *    end-to-end). A browser terminal would be a second implementation of the
 *    frame protocol for no new capability, so there is nothing in the UI to
 *    drive.
 *  - THE REMOTE REFUSAL. `attach_available: false` needs a step scheduled
 *    onto a remote runner mid-pause; that is pinned in the backend contract
 *    suite where it can be provoked deterministically.
 *
 * Prerequisites (see e2e/README.md):
 *   docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
 *     --profile e2e up -d backend-e2e runner-agent-e2e
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, resetBackend, seedBackend, type SeedResponse } from './helpers';

/**
 * Three script steps; the LAST one fails, which is what makes the original
 * run eligible for a debug re-run. The breakpoint goes on the middle step, so
 * proving the pause requires 'prepare' to have actually run first - a gate
 * that fired on the first step (or never) would not produce this state.
 *
 * A GRAPH, since 12.8 P3. It was a v1 `steps` array, and the change is not
 * cosmetic: a breakpoint key IS `StepRun.step_id`, which the executor stamps
 * from the graph node id, so the keys asserted below went from positional
 * ('0','1','2' - meaningful only while a pipeline was a list) to the step ids
 * an author actually writes. That is the whole point of the debug key
 * resolver, and this fixture is where it becomes visible.
 */
const PIPELINE_GRAPH = {
  version: 2,
  entry_points: ['prepare'],
  steps: {
    prepare: {
      id: 'prepare',
      name: 'prepare',
      type: 'script',
      config: { command: 'echo debug-e2e-prepare-done' },
      position: { x: 100, y: 0 },
      timeout: 120,
    },
    build: {
      id: 'build',
      name: 'build',
      type: 'script',
      config: { command: 'echo debug-e2e-build-done' },
      position: { x: 100, y: 150 },
      timeout: 120,
    },
    verify: {
      id: 'verify',
      name: 'verify',
      type: 'script',
      config: { command: 'echo debug-e2e-verify-failing; exit 1' },
      position: { x: 100, y: 300 },
      timeout: 120,
    },
  },
  edges: [
    { id: 'edge_0', from_step: 'prepare', to_step: 'build', condition: 'success' },
    { id: 'edge_1', from_step: 'build', to_step: 'verify', condition: 'success' },
  ],
};

/** Terminal run states, as the run viewer stamps them on data-status. */
const TERMINAL_RUN_STATUS = /^(passed|failed|cancelled)$/;

let seed: SeedResponse;
let pipelineId: string;

test.beforeEach(async ({ request }) => {
  await resetBackend(request);
  seed = await seedBackend(request);

  const response = await request.post(`${BACKEND_URL}/api/repos/${seed.repo.id}/pipelines`, {
    data: { name: 'debug-rerun-pipeline', steps_graph: PIPELINE_GRAPH },
  });
  expect(response.ok(), `pipeline create failed: ${await response.text()}`).toBeTruthy();
  pipelineId = (await response.json()).id;
});

/** Trigger a run over the API and wait for it to reach a terminal status. */
async function runToCompletion(request: Page['request']): Promise<string> {
  const response = await request.post(`${BACKEND_URL}/api/pipelines/${pipelineId}/run`, {
    data: { trigger_type: 'manual' },
  });
  expect(response.ok(), `run trigger failed: ${await response.text()}`).toBeTruthy();
  const runId: string = (await response.json()).id;

  await expect
    .poll(
      async () => {
        const detail = await request.get(`${BACKEND_URL}/api/pipeline-runs/${runId}`);
        return detail.ok() ? (await detail.json()).status : 'unreachable';
      },
      { timeout: 90_000, intervals: [500] }
    )
    .toBe('failed');

  return runId;
}

/** Pipelines page -> seeded repo -> Runs tab. */
async function openRunsTab(page: Page) {
  await page.goto('/#/pipelines');
  const repoItem = page.locator(`[data-testid="repo-item"][data-repo-id="${seed.repo.id}"]`);
  await expect(repoItem).toBeVisible({ timeout: 10_000 });
  await repoItem.click();
  await expect(page.locator('[data-testid="add-pipeline"]')).toBeVisible({ timeout: 5_000 });
  await page.locator('.tab', { hasText: 'Runs' }).click();
}

/**
 * Open the run viewer for the newest run whose trigger badge reads
 * `triggerType`. The runs list is sorted newest-first, and rows carry no run
 * id, so the trigger badge is the addressable identity: `manual` is the
 * original failure, `debug_rerun` is the session's run.
 */
async function openRunViewer(page: Page, triggerType: 'manual' | 'debug_rerun') {
  const runRow = page
    .locator('[data-testid="runs-list"] tbody tr')
    .filter({ has: page.locator('.trigger-badge', { hasText: triggerType }) })
    .first();
  await expect(runRow).toBeVisible({ timeout: 20_000 });
  await runRow.click();
  await expect(page.locator('[data-testid="run-viewer"]')).toBeVisible({ timeout: 10_000 });
}

/** Open the run viewer for the failed run and launch the debug re-run modal. */
async function openDebugModal(page: Page) {
  await openRunsTab(page);
  await openRunViewer(page, 'manual');
  await page.locator('[data-testid="debug-rerun-btn"]').click();
  await expect(page.locator('[data-testid="debug-rerun-modal"]')).toBeVisible({ timeout: 5_000 });
}

test.describe('Debug re-run (R8): breakpoint, pause, resume', () => {
  test('the launcher is offered on a failed run and lists every step as a breakpoint key', async ({
    page,
    request,
  }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    const items = page.locator('[data-testid="breakpoint-item"]');
    await expect(items).toHaveCount(3);

    // A breakpoint key is the GRAPH STEP ID, which is exactly what the
    // executor stamps onto StepRun.step_id. A key the gate can never match is
    // a breakpoint that silently never fires, so the identity is asserted
    // here and not merely implied by the labels. Listing order is
    // entry-point-first traversal, not object-key order.
    await expect(items.nth(0)).toHaveAttribute('data-step-key', 'prepare');
    await expect(items.nth(1)).toHaveAttribute('data-step-key', 'build');
    await expect(items.nth(2)).toHaveAttribute('data-step-key', 'verify');
    await expect(items.nth(1)).toContainText('build');

    // Nothing selected by default: an accidental Enter must not silently
    // arm three breakpoints.
    await expect(page.locator('[data-testid="breakpoint-hint"]')).toContainText(
      'No breakpoints selected'
    );
  });

  test('select-all and clear drive the whole checkbox list', async ({ page, request }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    await page.locator('[data-testid="breakpoints-select-all"]').click();
    await expect(page.locator('[data-testid="breakpoint-hint"]')).toContainText('3 breakpoints');

    await page.locator('[data-testid="breakpoints-clear"]').click();
    await expect(page.locator('[data-testid="breakpoint-hint"]')).toContainText(
      'No breakpoints selected'
    );
  });

  test('the commit choice reveals the branch/sha fields only when it is not the original', async ({
    page,
    request,
  }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    // Default is "same as this run", so the custom inputs stay out of the way.
    await expect(page.locator('[data-testid="custom-commit"]')).toHaveCount(0);

    await page.locator('input[type="radio"]').nth(1).click();
    await expect(page.locator('[data-testid="custom-commit"]')).toBeVisible();

    await page.locator('input[type="radio"]').nth(0).click();
    await expect(page.locator('[data-testid="custom-commit"]')).toHaveCount(0);
  });

  test('a breakpoint pauses the re-run, and the panel says which step, how long, and how to join', async ({
    page,
    request,
  }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    // Break before the MIDDLE step: reaching this pause requires step 0 to
    // have executed first.
    await page
      .locator('[data-testid="breakpoint-item"][data-step-key="build"] input[type="checkbox"]')
      .check();
    await page.locator('[data-testid="start-debug-btn"]').click();

    // The viewer follows the new run, and the panel is the surface that makes
    // the pause honest: the held StepRun still reads `running` (the executor
    // committed and broadcast it before the gate fired), so without this the
    // wedge is indistinguishable from a slow step.
    const panel = page.locator('[data-testid="debug-panel"]');
    await expect(panel).toBeVisible({ timeout: 60_000 });
    await expect(panel).toHaveAttribute('data-status', /^(waiting_at_bp|connected)$/, {
      timeout: 60_000,
    });

    await expect(panel.locator('[data-testid="debug-state-label"]')).toContainText(
      'Paused at breakpoint'
    );
    await expect(panel.locator('[data-testid="debug-current-step"]')).toHaveAttribute(
      'data-step-key',
      'build'
    );
    await expect(panel.locator('[data-testid="debug-current-step"]')).toContainText('build');

    // A bounded pause: the deadline is real and the operator can see it.
    await expect(panel.locator('[data-testid="debug-countdown"]')).toContainText(/\d+:\d{2}/);

    // A LOCAL step is attachable, so the handoff to the CLI is shown rather
    // than a reason it cannot be.
    await expect(panel.locator('[data-testid="debug-join-command"]')).toContainText('lazyaf debug');
    await expect(panel.locator('[data-testid="debug-attach-unavailable"]')).toHaveCount(0);

    await expect(panel.locator('[data-testid="debug-breakpoints-hit"]')).toContainText('Hit: 1');
  });

  test('extend pushes the deadline out while the run stays paused', async ({ page, request }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    await page
      .locator('[data-testid="breakpoint-item"][data-step-key="build"] input[type="checkbox"]')
      .check();
    await page.locator('[data-testid="start-debug-btn"]').click();

    const panel = page.locator('[data-testid="debug-panel"]');
    const countdown = panel.locator('[data-testid="debug-countdown"]');
    await expect(countdown).toBeVisible({ timeout: 60_000 });

    const before = await countdown.textContent();
    await panel.locator('[data-testid="debug-extend-btn"]').click();

    // +30m on a 60m budget: the rendered countdown must grow past what it
    // read a moment ago, and the run must still be held.
    await expect
      .poll(async () => (await countdown.textContent()) !== before, { timeout: 15_000 })
      .toBe(true);
    await expect(panel).toHaveAttribute('data-status', /^(waiting_at_bp|connected)$/);
  });

  test('Resume releases the gate and the re-run walks on to a terminal state', async ({
    page,
    request,
  }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    await page
      .locator('[data-testid="breakpoint-item"][data-step-key="build"] input[type="checkbox"]')
      .check();
    await page.locator('[data-testid="start-debug-btn"]').click();

    const panel = page.locator('[data-testid="debug-panel"]');
    await expect(panel).toHaveAttribute('data-status', /^(waiting_at_bp|connected)$/, {
      timeout: 60_000,
    });

    await panel.locator('[data-testid="debug-resume-btn"]').click();

    // The run walks past the gate. It fails again at `verify` - the debug
    // re-run REPRODUCES the original failure, it does not launder it - so the
    // assertion is "reached a terminal state", not "passed".
    await expect(
      page.locator('[data-testid="run-viewer"] [data-testid="run-status"]')
    ).toHaveAttribute('data-status', TERMINAL_RUN_STATUS, { timeout: 120_000 });

    // R1: a session never ends without saying why.
    await expect(panel.locator('[data-testid="debug-end-reason"]')).toBeVisible({
      timeout: 30_000,
    });
  });

  test('Abort ends the session with a stated reason and stops the run', async ({ page, request }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    await page
      .locator('[data-testid="breakpoint-item"][data-step-key="build"] input[type="checkbox"]')
      .check();
    await page.locator('[data-testid="start-debug-btn"]').click();

    const panel = page.locator('[data-testid="debug-panel"]');
    await expect(panel).toHaveAttribute('data-status', /^(waiting_at_bp|connected)$/, {
      timeout: 60_000,
    });

    await panel.locator('[data-testid="debug-abort-btn"]').click();

    await expect(panel).toHaveAttribute('data-status', /^(ended|timeout)$/, { timeout: 30_000 });
    await expect(panel.locator('[data-testid="debug-end-reason"]')).toContainText(/abort/i);

    // Abort cancels the run it was gating; it does not leave it hanging.
    await expect(
      page.locator('[data-testid="run-viewer"] [data-testid="run-status"]')
    ).toHaveAttribute('data-status', TERMINAL_RUN_STATUS, { timeout: 60_000 });
  });

  test('a paused session survives a full page reload (snapshot, not just deltas)', async ({
    page,
    request,
  }) => {
    await runToCompletion(request);
    await openDebugModal(page);

    await page
      .locator('[data-testid="breakpoint-item"][data-step-key="build"] input[type="checkbox"]')
      .check();
    await page.locator('[data-testid="start-debug-btn"]').click();

    const panel = page.locator('[data-testid="debug-panel"]');
    await expect(panel).toHaveAttribute('data-status', /^(waiting_at_bp|connected)$/, {
      timeout: 60_000,
    });
    // A session parked at a breakpoint broadcasts NOTHING for as long as it
    // is held, so after a reload there is no delta to learn from. Only the
    // snapshot fetch can repopulate the panel - a delta-only store shows an
    // empty panel over a wedged pipeline, which is the single most confusing
    // state this feature can produce.
    await page.reload();
    await openRunsTab(page);
    await openRunViewer(page, 'debug_rerun');

    await expect(page.locator('[data-testid="debug-panel"]')).toHaveAttribute(
      'data-status',
      /^(waiting_at_bp|connected)$/,
      { timeout: 30_000 }
    );

    // Leave nothing holding the e2e backend's executor for the next spec.
    await page.locator('[data-testid="debug-abort-btn"]').click();
    await expect(page.locator('[data-testid="debug-panel"]')).toHaveAttribute(
      'data-status',
      /^(ended|timeout)$/,
      { timeout: 30_000 }
    );
  });
});
