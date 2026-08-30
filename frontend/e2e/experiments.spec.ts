/**
 * E2E: Experiments — matrix fan-out and the leaderboard (Phase 12.6.5, R8).
 *
 * The assertions that earn this file's existence are the GUARDRAIL ones. An
 * experiment page that lists rows prettily but lets someone launch a 60-cell
 * matrix without ever seeing the bill is the failure this phase exists to
 * prevent, so:
 *
 *  - Launch is asserted DISABLED before the dry run and ENABLED only after it,
 *    and asserted to re-disable when the matrix is edited underneath a stale
 *    estimate. That last one is the whole gate: a fresh-looking panel next to
 *    an enabled button for a different matrix is exactly the lie.
 *  - The dry-run warnings and the `ranked: false` note are asserted VERBATIM.
 *    Both are strings the platform uses to refuse to oversell a number, and a
 *    paraphrase is a different claim.
 *  - A variant with no test evidence is asserted to render "N/A", and asserted
 *    NOT to render "0%". Those are different facts and the board must not blur
 *    them.
 *
 * Prerequisites: the compose e2e stack with LAZYAF_TEST_MODE=true and the mock
 * runner (see e2e/README.md):
 *
 *   docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
 *     --profile e2e up -d backend-e2e runner-mock-e2e
 *
 * Nothing here is skipped when a prerequisite is missing — `resetBackend`
 * fails loudly with the startup command (R4).
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, resetBackend, seedBackend, type SeedResponse } from './helpers';
import { NOT_RANKED_NOTE } from '../src/lib/api/types';

async function gotoExperiments(page: Page) {
  await page.goto('/#/experiments');
  await expect(page.locator('[data-testid="experiments-page"]')).toBeVisible({ timeout: 5000 });
}

/** A prompt template, so the prompt axis has a real second entry. */
async function createTemplate(page: Page, name: string): Promise<string> {
  const response = await page.request.post(`${BACKEND_URL}/api/prompt-templates`, {
    data: {
      name,
      description: 'e2e experiment axis',
      content: 'Implement {{title}}. Notes: {{description}}',
    },
  });
  expect(response.ok(), `create prompt-template failed: ${await response.text()}`).toBeTruthy();
  return (await response.json()).id;
}

/**
 * Create + launch a matrix through the API. Used where the UI flow is NOT the
 * assertion (the launch flow itself is exercised through the form below), so
 * those specs stay about what they claim to be about.
 */
async function launchViaApi(
  page: Page,
  seed: SeedResponse,
  overrides: Record<string, unknown> = {}
): Promise<string> {
  const body = {
    name: 'api-launched matrix',
    target_type: 'card',
    target_id: seed.cards[0].id,
    repo_id: seed.repo.id,
    matrix: {
      models: [
        { agent: 'mock', model: 'mock-a', label: 'a' },
        { agent: 'mock', model: 'mock-b', label: 'b' },
      ],
      prompts: [{ prompt_template_id: null, label: 'default' }],
      repeat: 1,
    },
    budget_usd: '10.00',
    max_concurrency: 2,
    ...overrides,
  };
  const created = await page.request.post(`${BACKEND_URL}/api/experiments`, { data: body });
  expect(created.ok(), `create experiment failed: ${await created.text()}`).toBeTruthy();
  const experiment = await created.json();

  const launched = await page.request.post(
    `${BACKEND_URL}/api/experiments/${experiment.id}/launch`
  );
  expect(launched.ok(), `launch failed: ${await launched.text()}`).toBeTruthy();
  return experiment.id;
}

test.describe('Experiments (Phase 12.6.5)', () => {
  test.beforeEach(async ({ request }) => {
    await resetBackend(request);
  });

  test('empty state renders and the nav item reaches the page', async ({ page }) => {
    await page.goto('/');
    const nav = page.locator('[data-testid="nav-experiments"]');
    await expect(nav).toBeVisible({ timeout: 5000 });
    await nav.click();

    await expect(page.locator('[data-testid="experiments-page"]')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[data-testid="experiments-empty"]')).toBeVisible();
    await expect(page.locator('[data-testid="experiment-item"]')).toHaveCount(0);
  });

  test('LAUNCH IS GATED ON THE DRY RUN: disabled, then enabled, then re-disabled on edit', async ({
    page,
  }) => {
    const seed = await seedBackend(page.request);
    const templateId = await createTemplate(page, 'e2e-axis-template');

    await gotoExperiments(page);
    await page.locator('[data-testid="experiment-create-btn"]').click();

    await page.fill('[data-testid="experiment-name-input"]', 'gate check');
    await page.selectOption('[data-testid="experiment-repo-select"]', seed.repo.id);
    await page.selectOption('[data-testid="experiment-target-select"]', seed.cards[0].id);

    // 2x2: two model rows, two prompt rows.
    await page.locator('[data-testid="add-model-row-btn"]').click();
    const modelRows = page.locator('[data-testid="matrix-model-row"]');
    await expect(modelRows).toHaveCount(2);
    await modelRows.nth(0).locator('[data-testid="model-agent-select"]').selectOption('mock');
    await modelRows.nth(0).locator('[data-testid="model-name-input"]').fill('mock-a');
    await modelRows.nth(1).locator('[data-testid="model-agent-select"]').selectOption('mock');
    await modelRows.nth(1).locator('[data-testid="model-name-input"]').fill('mock-b');

    await page.locator('[data-testid="add-prompt-row-btn"]').click();
    const promptRows = page.locator('[data-testid="matrix-prompt-row"]');
    await expect(promptRows).toHaveCount(2);
    await promptRows.nth(1).locator('[data-testid="prompt-template-select"]').selectOption(templateId);

    await page.fill('[data-testid="repeat-input"]', '2');
    await expect(page.locator('[data-testid="matrix-cell-count"]')).toHaveText('8');

    // Before the dry run: no panel, no launch.
    await expect(page.locator('[data-testid="dry-run-panel"]')).toHaveCount(0);
    await expect(page.locator('[data-testid="launch-btn"]')).toBeDisabled();
    await expect(page.locator('[data-testid="launch-gate-note"]')).toBeVisible();

    await page.locator('[data-testid="dry-run-btn"]').click();

    const panel = page.locator('[data-testid="dry-run-panel"]');
    await expect(panel).toBeVisible({ timeout: 15000 });
    await expect(page.locator('[data-testid="dry-run-cells"]')).toHaveText('8');
    await expect(page.locator('[data-testid="dry-run-runs"]')).toHaveText('8');
    // The basis is always stated: an estimate without one is an opinion.
    await expect(page.locator('[data-testid="dry-run-basis"]')).toBeVisible();
    const basis = await page.locator('[data-testid="dry-run-basis"]').getAttribute('data-basis');
    expect(['historical-median', 'partial', 'no-history']).toContain(basis);
    // A dry run creates NOTHING.
    await expect(page.locator('[data-testid="experiment-item"]')).toHaveCount(0);

    await expect(page.locator('[data-testid="launch-btn"]')).toBeEnabled();

    // Every warning the backend sent renders verbatim, none summarised away.
    const estimateResponse = await page.request.post(`${BACKEND_URL}/api/experiments`, {
      data: {
        name: 'gate check',
        target_type: 'card',
        target_id: seed.cards[0].id,
        repo_id: seed.repo.id,
        matrix: {
          models: [
            { agent: 'mock', model: 'mock-a', label: null },
            { agent: 'mock', model: 'mock-b', label: null },
          ],
          prompts: [
            { prompt_template_id: null, label: null },
            { prompt_template_id: templateId, label: null },
          ],
          repeat: 2,
        },
        budget_usd: '5.00',
        max_concurrency: 2,
        push_branches: false,
        dry_run: true,
      },
    });
    expect(estimateResponse.ok()).toBeTruthy();
    const estimate = await estimateResponse.json();
    for (const warning of estimate.warnings as string[]) {
      await expect(
        page.locator('[data-testid="dry-run-warning"]').filter({ hasText: warning })
      ).toHaveCount(1);
    }

    // THE GATE: edit the matrix under the estimate and Launch must close again.
    await page.fill('[data-testid="repeat-input"]', '3');
    await expect(page.locator('[data-testid="matrix-cell-count"]')).toHaveText('12');
    await expect(page.locator('[data-testid="launch-btn"]')).toBeDisabled();
    await expect(page.locator('[data-testid="launch-gate-note"]')).toBeVisible();
  });

  test('launching a mock matrix from the form drives every cell to a terminal state', async ({
    page,
  }) => {
    const seed = await seedBackend(page.request);

    await gotoExperiments(page);
    await page.locator('[data-testid="experiment-create-btn"]').click();

    await page.fill('[data-testid="experiment-name-input"]', 'mock 2x1');
    await page.selectOption('[data-testid="experiment-repo-select"]', seed.repo.id);
    await page.selectOption('[data-testid="experiment-target-select"]', seed.cards[0].id);

    await page.locator('[data-testid="add-model-row-btn"]').click();
    const modelRows = page.locator('[data-testid="matrix-model-row"]');
    await modelRows.nth(0).locator('[data-testid="model-agent-select"]').selectOption('mock');
    await modelRows.nth(0).locator('[data-testid="model-name-input"]').fill('mock-a');
    await modelRows.nth(1).locator('[data-testid="model-agent-select"]').selectOption('mock');
    await modelRows.nth(1).locator('[data-testid="model-name-input"]').fill('mock-b');
    await page.fill('[data-testid="repeat-input"]', '1');

    await page.locator('[data-testid="dry-run-btn"]').click();
    await expect(page.locator('[data-testid="dry-run-panel"]')).toBeVisible({ timeout: 15000 });
    await page.locator('[data-testid="launch-btn"]').click();

    // The matrix appears with one chip per cell...
    const chips = page.locator('[data-testid="cell-chip"]');
    await expect(chips).toHaveCount(2, { timeout: 30000 });

    // ...and every chip reaches a terminal status. `error` is deliberately
    // accepted here: this spec proves the board TRACKS cells to completion,
    // not that the mock agent succeeded (the backend exit gate owns that).
    await expect
      .poll(
        async () => {
          const statuses = await chips.evaluateAll(nodes =>
            nodes.map(n => n.getAttribute('data-status'))
          );
          return statuses.every(s =>
            ['passed', 'failed', 'error', 'cancelled', 'skipped_budget'].includes(s ?? '')
          );
        },
        { timeout: 90000, message: 'cells never reached a terminal status' }
      )
      .toBe(true);

    // The list row agrees with the matrix, and the experiment is terminal.
    const item = page.locator('[data-testid="experiment-item"]').first();
    await expect(item.locator('[data-testid="experiment-cell-progress"]')).toContainText('2/2');
    await expect
      .poll(async () => item.getAttribute('data-status'), { timeout: 30000 })
      .not.toBe('running');
  });

  test('the leaderboard reports without ranking, and shows N/A rather than 0% with no evidence', async ({
    page,
  }) => {
    const seed = await seedBackend(page.request);
    const templateId = await createTemplate(page, 'e2e-leaderboard-template');

    // 2 models x 2 prompts = 4 variants, no verify step => no test evidence.
    await launchViaApi(page, seed, {
      name: 'leaderboard matrix',
      matrix: {
        models: [
          { agent: 'mock', model: 'mock-a', label: 'a' },
          { agent: 'mock', model: 'mock-b', label: 'b' },
        ],
        prompts: [
          { prompt_template_id: null, label: 'default' },
          { prompt_template_id: templateId, label: 'template' },
        ],
        repeat: 1,
      },
    });

    await gotoExperiments(page);
    await page.locator('[data-testid="experiment-item"]').first().click();

    await expect
      .poll(
        async () =>
          page.locator('[data-testid="experiment-item"]').first().getAttribute('data-status'),
        { timeout: 90000, message: 'experiment never finished' }
      )
      .not.toBe('running');

    await page.locator('[data-testid="tab-leaderboard"]').click();
    await expect(page.locator('[data-testid="leaderboard-table"]')).toBeVisible({ timeout: 15000 });

    // One row per variant.
    await expect(page.locator('[data-testid="leaderboard-row"]')).toHaveCount(4);

    // The refusal-to-rank note renders VERBATIM. This is the same literal the
    // backend sends and the frontend single-sources in api/types.ts.
    const note = page.locator('[data-testid="leaderboard-not-ranked-note"]');
    await expect(note).toBeVisible();
    expect((await note.innerText()).replace(/\s+/g, ' ').trim()).toBe(
      NOT_RANKED_NOTE.replace(/\s+/g, ' ').trim()
    );

    // No verify step ran, so nothing was measured: N/A, never 0%.
    const rates = page.locator('[data-testid="leaderboard-pass-rate"]');
    for (let i = 0; i < (await rates.count()); i++) {
      const text = await rates.nth(i).innerText();
      expect(text).toContain('N/A');
      expect(text).not.toContain('0%');
    }
  });

  test('abort cancels pending cells and leaves running ones to finish', async ({ page }) => {
    const seed = await seedBackend(page.request);

    // Deliberately wider than the concurrency so cells are still pending when
    // the abort lands — otherwise this asserts nothing.
    await launchViaApi(page, seed, {
      name: 'abort matrix',
      max_concurrency: 1,
      matrix: {
        models: [
          { agent: 'mock', model: 'mock-a', label: 'a' },
          { agent: 'mock', model: 'mock-b', label: 'b' },
        ],
        prompts: [{ prompt_template_id: null, label: 'default' }],
        repeat: 3,
      },
    });

    await gotoExperiments(page);
    await page.locator('[data-testid="experiment-item"]').first().click();
    await expect(page.locator('[data-testid="cell-chip"]')).toHaveCount(6, { timeout: 15000 });

    await page.locator('[data-testid="abort-experiment-btn"]').click();

    // Pending cells become cancelled; nothing is left pending.
    await expect
      .poll(
        async () => {
          const statuses = await page
            .locator('[data-testid="cell-chip"]')
            .evaluateAll(nodes => nodes.map(n => n.getAttribute('data-status')));
          return statuses.filter(s => s === 'pending' || s === 'dispatching').length;
        },
        { timeout: 60000, message: 'pending cells were never cancelled' }
      )
      .toBe(0);

    await expect(page.locator('[data-testid="cell-chip"][data-status="cancelled"]').first()).toBeVisible();

    await expect
      .poll(
        async () =>
          page.locator('[data-testid="experiment-item"]').first().getAttribute('data-status'),
        { timeout: 90000 }
      )
      .toBe('aborted');
  });
});
