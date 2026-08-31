/**
 * E2E Tests: the pipeline LIST card (Phase 12.8 P3).
 *
 * The card is where the v1 array retirement is visible to a user, and it is
 * where the retirement's first live bug was hiding: `PipelinesPage` counted
 * `pipeline.steps` - the v1 ARRAY - which every graph pipeline has persisted
 * as `[]` since graphs landed. Every graph pipeline's card therefore read
 * "0 steps", with no type chips and no step preview, and nothing failed
 * anywhere. Nothing failed because nothing looked: no spec had ever asserted
 * what a pipeline card says about its own steps.
 *
 * These specs look. They drive the REAL backend (the graph goes in through
 * the API and comes back out through the page) except where a field can only
 * be produced by a repo sync, which is called out where it happens.
 *
 * Prerequisites: the compose e2e stack (see e2e/README.md). URLs come from
 * BACKEND_URL/FRONTEND_URL env vars.
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, createTestRepo, goToPipelinesPage } from './helpers';

type StepSeed = { id: string; name: string; type: 'script' | 'docker' | 'agent' };

/**
 * A linear graph, written the way the API takes it.
 *
 * The step ids are DELIBERATELY inserted in a different order from the
 * execution order in `entryPointFirst` below, because `Object.keys()` order
 * is what the card would fall back to if the shared `graphStepOrder` were
 * ever bypassed.
 */
function linearGraph(steps: StepSeed[]) {
  const graphSteps: Record<string, unknown> = {};
  steps.forEach((step, i) => {
    graphSteps[step.id] = {
      id: step.id,
      name: step.name,
      type: step.type,
      config: step.type === 'docker'
        ? { image: 'ubuntu:latest', command: 'echo hi' }
        : step.type === 'agent'
          ? { title: 'Do the thing', description: '' }
          : { command: `echo ${step.id}` },
      position: { x: 100, y: i * 150 },
      timeout: 300,
    };
  });
  return {
    steps: graphSteps,
    edges: steps.slice(0, -1).map((step, i) => ({
      id: `edge_${i}`,
      from_step: step.id,
      to_step: steps[i + 1].id,
      condition: 'success',
    })),
    entry_points: steps.length ? [steps[0].id] : [],
    version: 2,
  };
}

async function createGraphPipeline(
  page: Page,
  repoId: string,
  name: string,
  steps: StepSeed[],
) {
  const response = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/pipelines`, {
    data: { name, steps_graph: linearGraph(steps) },
  });
  expect(response.ok(), `POST pipeline "${name}": ${await response.text()}`).toBeTruthy();
  return response.json();
}

function card(page: Page, name: string) {
  return page.locator('.pipeline-card').filter({ hasText: name });
}

test.describe('Pipeline card - step count and types come from the graph', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-cards');
  });

  test('a graph pipeline card counts its real steps, not "0 steps"', async ({ page }) => {
    await createGraphPipeline(page, repo.id, 'Three Steps', [
      { id: 'build', name: 'build', type: 'script' },
      { id: 'test', name: 'test', type: 'script' },
      { id: 'ship', name: 'ship', type: 'docker' },
    ]);

    await goToPipelinesPage(page, repo.name);

    const target = card(page, 'Three Steps');
    await expect(target).toBeVisible({ timeout: 5000 });
    await expect(target.locator('[data-testid="pipeline-step-count"]')).toHaveText('3 steps');
  });

  test('a one-step pipeline reads "1 step", not "1 steps"', async ({ page }) => {
    await createGraphPipeline(page, repo.id, 'Just One', [
      { id: 'only', name: 'only', type: 'script' },
    ]);

    await goToPipelinesPage(page, repo.name);
    await expect(card(page, 'Just One').locator('[data-testid="pipeline-step-count"]'))
      .toHaveText('1 step');
  });

  test('type chips are the distinct step types in the graph', async ({ page }) => {
    await createGraphPipeline(page, repo.id, 'Mixed Types', [
      { id: 'a', name: 'a', type: 'script' },
      { id: 'b', name: 'b', type: 'docker' },
      { id: 'c', name: 'c', type: 'script' },
      { id: 'd', name: 'd', type: 'agent' },
    ]);

    await goToPipelinesPage(page, repo.name);

    const chips = card(page, 'Mixed Types').locator('[data-testid="pipeline-step-type"]');
    // Distinct, and in first-appearance order - three chips for four steps.
    await expect(chips).toHaveText(['script', 'docker', 'agent']);
  });

  test('the step preview is in execution order, not object-key order', async ({ page }) => {
    // Insertion order here is the REVERSE of execution order, so a card that
    // simply walked `Object.keys(steps)` would list "ship" first.
    const graph = linearGraph([
      { id: 'build', name: 'build', type: 'script' },
      { id: 'test', name: 'test', type: 'script' },
      { id: 'ship', name: 'ship', type: 'script' },
    ]);
    const reordered: Record<string, unknown> = {};
    for (const id of ['ship', 'test', 'build']) reordered[id] = (graph.steps as any)[id];
    graph.steps = reordered;

    const response = await page.request.post(`${BACKEND_URL}/api/repos/${repo.id}/pipelines`, {
      data: { name: 'Ordered', steps_graph: graph },
    });
    expect(response.ok()).toBeTruthy();

    await goToPipelinesPage(page, repo.name);

    const chips = card(page, 'Ordered').locator('.step-chip');
    await expect(chips).toHaveText(['1. build', '2. test', '3. ship']);
  });
});

test.describe('Pipeline card - the honest failure states', () => {
  let repo: { id: string; name: string };

  test.beforeEach(async ({ page }) => {
    repo = await createTestRepo(page, 'e2e-cards');
  });

  /**
   * `definition_error` is set by `upsert_materialized_pipeline` when a repo
   * YAML parses but will not convert to a graph, and it is the ONLY channel
   * that refusal has: `sync_repo_pipelines` deliberately swallows the failure
   * and keeps the stale definition so a broken CI file cannot break a push.
   *
   * Producing one for real needs a repo, a commit and a push, which is the
   * boundary lane's ground. What this spec owns is the other half: that the
   * field, when it arrives, is RENDERED rather than dropped on the floor - so
   * the route is intercepted and the row is served with the error set.
   */
  test('a definition_error is rendered on the card', async ({ page }) => {
    const created = await createGraphPipeline(page, repo.id, 'Broken Sync', [
      { id: 'a', name: 'a', type: 'script' },
    ]);

    await page.route(
      (url) => url.pathname === `/api/repos/${repo.id}/pipelines`,
      async (route) => {
        const response = await route.fetch();
        const rows = await response.json();
        for (const row of rows) {
          if (row.id === created.id) {
            row.definition_error =
              "step 'tier1': 'trigger:pipeline:' is retired (12.8)";
          }
        }
        await route.fulfill({ response, json: rows });
      },
    );

    await goToPipelinesPage(page, repo.name);

    const badge = card(page, 'Broken Sync').locator('[data-testid="pipeline-definition-error"]');
    await expect(badge).toBeVisible();
    await expect(badge).toContainText('trigger:pipeline:');
  });

  /**
   * "0 steps" and "this pipeline has no definition" are different facts and
   * must not read the same. They read the same for the entire life of the
   * bug this wave fixed, which is exactly how it stayed invisible.
   */
  test('a pipeline with no graph says so, rather than reading "0 steps"', async ({ page }) => {
    const created = await createGraphPipeline(page, repo.id, 'No Definition', [
      { id: 'a', name: 'a', type: 'script' },
    ]);

    await page.route(
      (url) => url.pathname === `/api/repos/${repo.id}/pipelines`,
      async (route) => {
        const response = await route.fetch();
        const rows = await response.json();
        for (const row of rows) {
          if (row.id === created.id) row.steps_graph = null;
        }
        await route.fulfill({ response, json: rows });
      },
    );

    await goToPipelinesPage(page, repo.name);

    const count = card(page, 'No Definition').locator('[data-testid="pipeline-step-count"]');
    await expect(count).toHaveText('No steps defined');
    await expect(count).not.toHaveText('0 steps');
  });

  /**
   * The editor must not offer a Save button over a pipeline whose definition
   * it could not load: `graph` starts empty and an empty graph is a savable
   * thing, so the canvas would quietly offer to replace a real definition
   * with nothing.
   */
  test('the editor refuses to open a pipeline with no graph, and does not offer Save', async ({ page }) => {
    const created = await createGraphPipeline(page, repo.id, 'Unopenable', [
      { id: 'a', name: 'a', type: 'script' },
    ]);

    await page.route(
      (url) => url.pathname === `/api/pipelines/${created.id}`,
      async (route) => {
        const response = await route.fetch();
        const row = await response.json();
        row.steps_graph = null;
        row.definition_error = 'unknown node action \'explode\'';
        await route.fulfill({ response, json: row });
      },
    );

    await page.goto(`/#/pipelines/${created.id}/edit`);

    await expect(page.locator('[data-testid="editor-error"]')).toContainText('no graph definition');
    await expect(page.locator('[data-testid="editor-error"]')).toContainText('explode');
    await expect(page.locator('[data-testid="editor-unloadable"]')).toBeVisible();
    await expect(page.locator('.graph-editor')).toHaveCount(0);
    await expect(page.locator('[data-testid="save-pipeline"]')).toBeDisabled();
  });
});

test.describe('Pipeline card - repo pipelines keep the authoring array', () => {
  /**
   * The array is NOT dead: `GET /api/repos/{id}/lazyaf/pipelines` serves the
   * authoring FILE, where a human writes a list of steps, and its card
   * renders that list. Array = authoring, graph = execution, two endpoints -
   * and a sweep that "finished the job" by making this card read a graph
   * would break the one place the array is still the right answer.
   */
  test('a repo pipeline card counts the steps in its YAML', async ({ page }) => {
    const repo = await createTestRepo(page, 'e2e-cards');

    await page.route(
      (url) => url.pathname === `/api/repos/${repo.id}/lazyaf/pipelines`,
      async (route) => route.fulfill({
        json: [{
          name: 'from-yaml',
          description: 'lives in .lazyaf/pipelines/',
          source: 'repo',
          filename: 'from-yaml.yaml',
          steps: [
            { name: 'lint', type: 'script', config: { command: 'ruff' }, on_success: 'next', on_failure: 'stop', timeout: 300 },
            { name: 'unit', type: 'script', config: { command: 'pytest' }, on_success: 'next', on_failure: 'stop', timeout: 300 },
          ],
        }],
      }),
    );

    await goToPipelinesPage(page, repo.name);

    const repoCard = page.locator('.pipeline-card.repo-card').filter({ hasText: 'from-yaml' });
    await expect(repoCard).toBeVisible();
    await expect(repoCard.locator('.step-count')).toHaveText('2 steps');
  });
});
