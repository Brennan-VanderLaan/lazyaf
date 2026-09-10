/**
 * CARD MODEL SELECTION — one control, and the pairing it must never break.
 *
 * A card carries TWO fields that decide what runs it: `runner_type` (the
 * agent) and `step_config.model` (the model that agent drives). They used to
 * be two independent controls, which made it trivial to save a pairing that
 * cannot dispatch — `runner_type: claude-code` with
 * `model: 'endpoint:local-4090'`. Nothing refuses that combination:
 * `_resolve_step_endpoint` only reads the `endpoint:` sugar for the harness
 * runner, so the literal string is handed to the Claude CLI as a model name
 * and the step dies inside the container with an unhelpful error.
 *
 * So the picker is one control that writes both fields, and the assertions
 * below are all on what LANDS ON THE CARD, read back over the API — not on
 * what the `<select>` displays. A picker that looks right and saves a pairing
 * dispatch cannot run is exactly the defect this replaced.
 *
 * The subtlest case is the third one: a card whose saved model is no longer on
 * offer (retired from `/api/models`, an endpoint since deleted, or a list that
 * has not landed yet). A `<select>` whose value matches no option silently
 * displays its FIRST option, so dropping the stored pair would show one model,
 * then SAVE that different model the next time anyone pressed Save — changing
 * which model runs the card by touching nothing. That is the quietest possible
 * data loss and it gets its own test.
 *
 * The registry is served from an intercepted `GET /api/model-endpoints` (the
 * same pattern as endpoints.spec.ts) so the self-hosted rows are deterministic
 * and this spec does not depend on what is registered on the box it runs on.
 *
 * This spec creates its own repo and cards and never calls /api/test/reset, so
 * it is safe to run against a QA backend other lanes are using.
 *
 * Run with:
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5174 \
 *     npx playwright test e2e/card-model.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

import { BACKEND_URL, createTestRepo, selectRepo } from './helpers';

/** The one sugar spelling `resolve_step_endpoint` parses. */
const ENDPOINT_PREFIX = 'endpoint:';

/**
 * A registry row shaped like `GET /api/model-endpoints` returns.
 *
 * Probed, healthy and enabled, because an endpoint in any other state is
 * rendered DISABLED by `toOption` and cannot be selected — that behaviour is
 * endpoints.spec.ts's subject, not this one's.
 */
function endpointRow(name: string) {
  return {
    id: `fixture-${name}`,
    name,
    description: null,
    base_url: 'http://mock:8099/v1',
    model: 'mock-model',
    server_kind: 'vllm',
    auth_style: 'none',
    auth_secret_ref: null,
    auth_header_name: null,
    secret_present: true,
    reach: 'direct',
    runner_label: null,
    runner_count: null,
    rate_usd_hour: '0.000000',
    gpu_node_id: `endpoint:${name}`,
    gpu_fraction: 1,
    priced: true,
    max_concurrency: 1,
    request_timeout_seconds: 300,
    context_window: null,
    context_window_source: 'probe',
    max_output_tokens: 4096,
    capabilities: {
      supports_tools: true,
      supports_streaming: true,
      reports_usage: true,
      context_window: 32768,
      max_output_tokens: 4096,
      probe_status: 'ok',
      probed_at: '2026-08-31T07:00:00Z',
      probed_from: 'backend',
      probe_age_seconds: 60,
      stale: false,
    },
    pricing: { gpu_node_id: `endpoint:${name}`, gpu_fraction: 1, priced: true },
    health: 'healthy',
    probe_detail: {},
    consecutive_failures: 0,
    last_success_at: null,
    last_error: null,
    warning: null,
    enabled: true,
    in_flight: 0,
    created_at: '2026-08-31T07:00:00Z',
    updated_at: '2026-08-31T07:00:00Z',
  };
}

/** Serve a fixed endpoint registry and block the socket, so the page is deterministic. */
async function serveRegistry(page: Page, rows: unknown[]) {
  await page.routeWebSocket('**/ws', (ws) => ws.close());
  await page.route('**/api/model-endpoints', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(rows),
      });
      return;
    }
    await route.continue();
  });
}

/** Create a todo agent card over the API — the model picker only shows for those. */
async function createCard(page: Page, repoId: string, title: string, stepConfig?: unknown) {
  const response = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
    data: {
      title,
      description: 'card-model spec',
      step_type: 'agent',
      ...(stepConfig === undefined ? {} : { step_config: stepConfig }),
    },
  });
  expect(response.ok(), `card create failed: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function readCard(page: Page, cardId: string) {
  const response = await page.request.get(`${BACKEND_URL}/api/cards/${cardId}`);
  expect(response.ok(), `card read failed: ${await response.text()}`).toBeTruthy();
  return response.json();
}

async function openCard(page: Page, title: string) {
  await page.locator('.card').filter({ hasText: title }).click();
  await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();
  await expect(page.getByTestId('card-model-select')).toBeVisible({ timeout: 10_000 });
}

async function saveCard(page: Page) {
  await page.getByTestId('save-card-btn').click();
  await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 10_000 });
}

test.describe('card model selection writes a pairing that can dispatch', () => {
  test('picking a self-hosted endpoint saves the harness runner AND the endpoint sugar', async ({
    page,
  }) => {
    await serveRegistry(page, [endpointRow('local-4090')]);
    const { id: repoId, name: repoName } = await createTestRepo(page, 'e2e-card-model-endpoint');
    const card = await createCard(page, repoId, 'Endpoint Card');

    await page.goto('/');
    await selectRepo(page, repoName);
    await openCard(page, 'Endpoint Card');

    const select = page.getByTestId('card-model-select');
    await expect(select.locator(`option[value="${ENDPOINT_PREFIX}local-4090"]`)).toHaveCount(1);
    await select.selectOption(`${ENDPOINT_PREFIX}local-4090`);
    await saveCard(page);

    // BOTH halves, and they agree: the harness is the only agent that parses
    // the `endpoint:` spelling, so any other runner_type here cannot dispatch.
    const saved = await readCard(page, card.id);
    expect(saved.runner_type).toBe('openai-harness');
    expect(saved.step_config?.model).toBe(`${ENDPOINT_PREFIX}local-4090`);
  });

  test('picking a hosted model saves the agent that can actually drive it', async ({ page }) => {
    await serveRegistry(page, []);
    const { id: repoId, name: repoName } = await createTestRepo(page, 'e2e-card-model-hosted');
    const card = await createCard(page, repoId, 'Hosted Card');

    await page.goto('/');
    await selectRepo(page, repoName);
    await openCard(page, 'Hosted Card');

    const select = page.getByTestId('card-model-select');
    // Take a real hosted option from the rendered list rather than hardcoding a
    // model id: `/api/models` is a moving vocabulary and pinning one name here
    // would make this spec fail on a model rename rather than on a defect.
    const hosted = select.locator('optgroup option').first();
    await expect(hosted).toBeAttached({ timeout: 10_000 });
    const choice = await hosted.getAttribute('value');
    expect(choice, 'no hosted model options were rendered').toBeTruthy();

    await select.selectOption(choice!);
    await saveCard(page);

    const saved = await readCard(page, card.id);
    // The option value is `<runner>` + separator + `<model>`; whatever the
    // separator is, the card must come back holding the SAME pair, and it must
    // not be the endpoint sugar under a CLI agent — the defect this replaced.
    expect(choice).toContain(saved.runner_type);
    expect(saved.step_config?.model).toBeTruthy();
    expect(choice).toContain(saved.step_config.model);
    expect(saved.step_config.model.startsWith(ENDPOINT_PREFIX)).toBe(false);
  });

  test('a saved model that is no longer on offer survives a Save that never touched it', async ({
    page,
  }) => {
    // The card holds an endpoint the registry does NOT return — the "endpoint
    // since deleted" case. Nothing in the offered list matches it.
    await serveRegistry(page, [endpointRow('still-here')]);
    const { id: repoId, name: repoName } = await createTestRepo(page, 'e2e-card-model-stranded');
    const card = await createCard(page, repoId, 'Stranded Card', {
      model: `${ENDPOINT_PREFIX}deleted-endpoint`,
    });
    const patched = await page.request.patch(`${BACKEND_URL}/api/cards/${card.id}`, {
      data: { runner_type: 'openai-harness' },
    });
    expect(patched.ok(), `card patch failed: ${await patched.text()}`).toBeTruthy();

    const before = await readCard(page, card.id);
    expect(before.step_config?.model).toBe(`${ENDPOINT_PREFIX}deleted-endpoint`);

    await page.goto('/');
    await selectRepo(page, repoName);
    await openCard(page, 'Stranded Card');

    // It is VISIBLE and selected, not silently replaced by the first option.
    const stored = page.getByTestId('card-model-stored');
    await expect(stored).toBeAttached({ timeout: 10_000 });
    await expect(stored).toContainText('deleted-endpoint');
    await expect(page.getByTestId('card-model-select')).toHaveValue(
      `${ENDPOINT_PREFIX}deleted-endpoint`,
    );

    // Save without touching the picker: the card must be unchanged.
    await saveCard(page);

    const after = await readCard(page, card.id);
    expect(after.step_config?.model).toBe(`${ENDPOINT_PREFIX}deleted-endpoint`);
    expect(after.runner_type).toBe('openai-harness');
  });

  test('an empty registry says why the self-hosted group is missing', async ({ page }) => {
    // R1: an absent optgroup is otherwise indistinguishable from a broken page.
    await serveRegistry(page, []);
    const { id: repoId, name: repoName } = await createTestRepo(page, 'e2e-card-model-none');
    await createCard(page, repoId, 'No Endpoints Card');

    await page.goto('/');
    await selectRepo(page, repoName);
    await openCard(page, 'No Endpoints Card');

    await expect(page.getByTestId('card-model-no-endpoints')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByTestId('card-model-no-endpoints')).toContainText('Endpoints page');
    await expect(
      page.getByTestId('card-model-select').locator(`option[value^="${ENDPOINT_PREFIX}"]`),
    ).toHaveCount(0);
  });
});
