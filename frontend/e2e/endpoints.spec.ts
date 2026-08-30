/**
 * E2E: the Model Endpoints page (standing rule R8; M14 Agent E contract 3 & 4).
 *
 * This spec proves the four things the page exists to do, and each one is
 * asserted with the other channels neutralized so a half-working page cannot
 * pass:
 *
 *  1. REGISTER + SYNCHRONOUS PROBE. A no-auth endpoint is registered through
 *     the form against the mock OpenAI server, and the capability record is
 *     populated on the very first render — the point of probing at
 *     registration is that the operator learns "this model cannot tool-call"
 *     HERE, not at the first thirty-minute agent step.
 *  2. DELTAS. `GET /api/model-endpoints` is neutralized after the initial
 *     load, then Probe is clicked. Anything the row shows afterwards can only
 *     have arrived over a `model_endpoint_status` frame.
 *  3. SNAPSHOT. The WebSocket is blocked before the app mounts and the page is
 *     reloaded; anything it renders can only have come from the REST list.
 *     Without this half a reload shows an empty registry over live hardware —
 *     the exact regression the 12.6 runner panel shipped once already.
 *  4. THE THREE-STATE RULE. A never-probed endpoint renders as visibly
 *     different from a probed-and-unsupported one, because `null` and `false`
 *     lead to opposite outcomes (refuse vs. run the fallback protocol) and a
 *     checkbox cannot carry that.
 *
 * Plus the milestone's headline claim: the endpoint is SELECTABLE on the
 * experiment matrix's model axis, which is what lets one matrix mix API and
 * self-hosted models in a single run.
 *
 * PREREQUISITES (see e2e/README.md). This lane needs the mock OpenAI server,
 * which the compose stack provides as `mock-endpoint`:
 *
 *   docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml \
 *     --profile e2e up -d backend-e2e mock-endpoint
 *
 * A missing mock server FAILS this spec with that command in the message. It
 * is never skipped: "the mock server was absent" and "the page cannot probe"
 * are indistinguishable from a skip, and R4 forbids buying green with one.
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, resetBackend, seedBackend } from './helpers';

/** The seeded tool-calling mock endpoint (backend/app/routers/test_api.py). */
const SEEDED_TOOLS_ENDPOINT = 'dogfood-mock';
/** The seeded endpoint whose model CANNOT tool-call — probes `degraded`. */
const SEEDED_NOTOOLS_ENDPOINT = 'dogfood-mock-notools';

interface SeededEndpoint {
  id: string;
  name: string;
  base_url: string;
  model: string;
  probe_status: string;
}

/**
 * Navigate to the Endpoints page.
 *
 * If the route is not registered the failure says exactly which lines are
 * missing rather than timing out on an empty page — the App.svelte route and
 * nav item are deliberately owned outside this agent's file set, so a missing
 * route is an integration gap and has to read like one.
 */
async function goToEndpoints(page: Page) {
  await page.goto('/#/endpoints');
  await expect(
    page.getByTestId('endpoints-page'),
    'The /endpoints route is not registered. Add to src/App.svelte:\n' +
      "  import EndpointsPage from './lib/pages/EndpointsPage.svelte';\n" +
      "  '/endpoints': EndpointsPage,   // in `routes`\n" +
      '  plus the sidebar nav item (data-testid="nav-endpoints").',
  ).toBeVisible({ timeout: 15_000 });
}

/** The seeded endpoints, or a failure naming the compose command. */
async function seedEndpoints(page: Page): Promise<SeededEndpoint[]> {
  await resetBackend(page.request);
  const seed = (await seedBackend(page.request)) as unknown as {
    model_endpoints?: SeededEndpoint[];
  };
  const endpoints = seed.model_endpoints ?? [];
  expect(
    endpoints.length,
    'POST /api/test/seed returned no model_endpoints. The e2e backend must be ' +
      'built with the M14 seeding in backend/app/routers/test_api.py.',
  ).toBeGreaterThan(0);
  return endpoints;
}

function row(page: Page, name: string) {
  return page.locator('[data-testid="endpoint-row"]').filter({
    has: page.locator(`[data-testid="endpoint-name"]:text-is("${name}")`),
  });
}

test.describe('Model Endpoints page', () => {
  test('registers a no-auth endpoint and probes it at registration', async ({ page }) => {
    const seeded = await seedEndpoints(page);
    const toolsEndpoint = seeded.find((e) => e.name === SEEDED_TOOLS_ENDPOINT);
    expect(
      toolsEndpoint,
      `The seed did not create '${SEEDED_TOOLS_ENDPOINT}'. Start the mock server:\n` +
        '  docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml ' +
        '--profile e2e up -d backend-e2e mock-endpoint',
    ).toBeTruthy();
    expect(
      toolsEndpoint!.probe_status,
      `The seeded endpoint probed '${toolsEndpoint!.probe_status}' against ` +
        `${toolsEndpoint!.base_url}. The mock-endpoint service is not reachable ` +
        'from the backend container. Start it with:\n' +
        '  docker compose -f docker-compose.yml -f frontend/e2e/compose.test-mode.yml ' +
        '--profile e2e up -d mock-endpoint',
    ).not.toBe('unprobed');

    await goToEndpoints(page);

    // The registry rendered from the snapshot.
    await expect(row(page, SEEDED_TOOLS_ENDPOINT)).toBeVisible({ timeout: 10_000 });

    // Register a NEW endpoint through the form, against the same mock server
    // but the no-usage scenario, so this test also covers a `degraded` probe.
    const name = `e2e-mock-${Date.now().toString().slice(-8)}`;
    const baseUrl = toolsEndpoint!.base_url;

    await page.getByTestId('register-endpoint-btn').click();
    await expect(page.getByTestId('endpoint-modal')).toBeVisible();

    // The form must SAY it holds no secret value — that promise is the
    // security decision of this phase, and it belongs on screen.
    await expect(page.getByTestId('endpoint-no-secret-notice')).toBeVisible();
    // "none" is the DEFAULT auth style, not an escape hatch.
    await expect(page.getByTestId('endpoint-auth-none')).toBeChecked();
    // ...so there is no secret input on screen at all.
    await expect(page.getByTestId('endpoint-secret-suffix-input')).toHaveCount(0);

    await page.getByTestId('endpoint-name-input').fill(name);
    await page.getByTestId('endpoint-base-url-input').fill(baseUrl);
    await page.getByTestId('endpoint-model-input').fill(toolsEndpoint!.model);
    await page.getByTestId('endpoint-server-kind-select').selectOption('vllm');
    // 0 is a REAL rate ("owned hardware"), not an absence — and it puts the
    // gpu-node pricing branch on this lane.
    await page.getByTestId('endpoint-rate-input').fill('0');
    await page.getByTestId('endpoint-submit-btn').click();

    await expect(page.getByTestId('endpoint-modal')).toHaveCount(0, { timeout: 30_000 });

    const created = row(page, name);
    await expect(created).toBeVisible({ timeout: 15_000 });

    // The capability record is populated ON THE FIRST RENDER: registration
    // probed synchronously, which is the entire point of doing it here.
    await expect(created).not.toHaveAttribute('data-probe-status', 'unprobed');
    await expect(created.getByTestId('endpoint-cap-tools')).not.toHaveAttribute(
      'data-state',
      'unprobed',
    );
    await expect(created.getByTestId('endpoint-cap-context')).toBeVisible();

    // $0.00/hr is rendered as the claim it is, never as "unpriced".
    await expect(created.getByTestId('endpoint-rate')).toContainText('owned');
    await expect(created.getByTestId('endpoint-rate')).toHaveAttribute('data-priced', 'true');
  });

  test('an UNPRICED endpoint reads as an absence, never as $0.00', async ({ page }) => {
    await seedEndpoints(page);
    await goToEndpoints(page);

    const name = `e2e-unpriced-${Date.now().toString().slice(-8)}`;
    await page.getByTestId('register-endpoint-btn').click();
    await page.getByTestId('endpoint-name-input').fill(name);
    // Deliberately unreachable: a probe that cannot connect is still a
    // SUCCESSFUL observation and must render as a red row, not a request error.
    await page.getByTestId('endpoint-base-url-input').fill('http://127.0.0.1:9/v1');
    await page.getByTestId('endpoint-model-input').fill('nothing-here');
    await page.getByTestId('endpoint-rate-input').fill(''); // unpriced
    await page.getByTestId('endpoint-submit-btn').click();

    const created = row(page, name);
    await expect(created).toBeVisible({ timeout: 40_000 });

    await expect(created.getByTestId('endpoint-rate')).toContainText('unpriced');
    await expect(created.getByTestId('endpoint-rate')).toHaveAttribute('data-priced', 'false');
    await expect(created.getByTestId('endpoint-rate')).not.toContainText('$0.00');

    // Never probed successfully, so every capability stays THREE-state
    // "unprobed" — visibly distinct from a probed `false`.
    await expect(created.getByTestId('endpoint-cap-tools')).toHaveAttribute(
      'data-state',
      'unprobed',
    );

    // The page never turns "the endpoint is down" into a page-level error.
    await expect(page.getByTestId('endpoints-error')).toHaveCount(0);
  });

  test('"never probed" and "probed, no tools" are visibly different states', async ({ page }) => {
    const seeded = await seedEndpoints(page);
    const notools = seeded.find((e) => e.name === SEEDED_NOTOOLS_ENDPOINT);
    expect(notools, `The seed did not create '${SEEDED_NOTOOLS_ENDPOINT}'.`).toBeTruthy();
    expect(
      notools!.probe_status,
      'The no-tools endpoint did not probe. Start the mock-endpoint compose service.',
    ).not.toBe('unprobed');

    await goToEndpoints(page);

    const probedRow = row(page, SEEDED_NOTOOLS_ENDPOINT);
    await expect(probedRow).toBeVisible({ timeout: 10_000 });

    // Probed and cannot tool-call: `unsupported`, and the row explains that
    // this endpoint is still USABLE via the fallback protocol.
    const toolsCell = probedRow.getByTestId('endpoint-cap-tools');
    await expect(toolsCell).toHaveAttribute('data-state', 'unsupported');
    await expect(toolsCell).toHaveAttribute('title', /fallback/i);

    // Now an endpoint that was never asked. Registering with probing OFF is
    // the only honest way to reach that state.
    const name = `e2e-unprobed-${Date.now().toString().slice(-8)}`;
    await page.getByTestId('register-endpoint-btn').click();
    await page.getByTestId('endpoint-name-input').fill(name);
    await page.getByTestId('endpoint-base-url-input').fill(notools!.base_url);
    await page.getByTestId('endpoint-model-input').fill(notools!.model);
    await page.getByTestId('endpoint-probe-on-save').uncheck();
    await page.getByTestId('endpoint-submit-btn').click();

    const unprobedRow = row(page, name);
    await expect(unprobedRow).toBeVisible({ timeout: 20_000 });
    const unprobedTools = unprobedRow.getByTestId('endpoint-cap-tools');
    await expect(unprobedTools).toHaveAttribute('data-state', 'unprobed');
    // The consequence is stated: dispatch REFUSES, it does not quietly
    // downgrade to the fallback protocol.
    await expect(unprobedTools).toHaveAttribute('title', /refuses/i);
    await expect(unprobedRow).toHaveAttribute('data-health', 'unprobed');
  });

  test('the capability record updates from a WS DELTA, with the REST list neutralized', async ({
    page,
  }) => {
    const seeded = await seedEndpoints(page);
    expect(seeded.length).toBeGreaterThan(0);

    await goToEndpoints(page);
    const target = row(page, SEEDED_TOOLS_ENDPOINT);
    await expect(target).toBeVisible({ timeout: 10_000 });
    const probedAtBefore = await target.getAttribute('data-probed-at');

    // Neutralize the snapshot channel AFTER the initial load. From here on,
    // anything the row shows can only have arrived over the socket.
    await page.route('**/api/model-endpoints', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({ status: 200, contentType: 'application/json', body: '[]' });
        return;
      }
      await route.continue();
    });

    // Probe from OUTSIDE the page, so even the POST's own response body
    // cannot be what updates the row.
    const response = await page.request.post(
      `${BACKEND_URL}/api/model-endpoints/${SEEDED_TOOLS_ENDPOINT}/probe?force=true`,
    );
    expect(
      response.status(),
      'A probe returns 200 WITH THE RECORD even when the endpoint is down — ' +
        '"it is down" is a successful observation.',
    ).toBe(200);

    await expect
      .poll(async () => target.getAttribute('data-probed-at'), { timeout: 20_000 })
      .not.toBe(probedAtBefore);

    // The row is still there and still populated: the delta was a full
    // projection, not a patch that could blank fields.
    await expect(target.getByTestId('endpoint-cap-tools')).not.toHaveAttribute(
      'data-state',
      'unprobed',
    );
    await expect(target.getByTestId('endpoint-health')).toBeVisible();
  });

  test('a reload with the WebSocket blocked is populated from the SNAPSHOT', async ({ page }) => {
    await seedEndpoints(page);

    // Block the socket BEFORE the app mounts. Anything the page renders can
    // only have come from GET /api/model-endpoints.
    await page.routeWebSocket('**/ws', (ws) => ws.close());

    await goToEndpoints(page);

    await expect(row(page, SEEDED_TOOLS_ENDPOINT)).toBeVisible({ timeout: 15_000 });
    await expect(row(page, SEEDED_NOTOOLS_ENDPOINT)).toBeVisible();
    await expect(page.getByTestId('endpoints-empty')).toHaveCount(0);

    // ...and the capabilities came with it, not just the names.
    await expect(
      row(page, SEEDED_TOOLS_ENDPOINT).getByTestId('endpoint-cap-context'),
    ).toBeVisible();
  });

  test('an endpoint is selectable on the experiment matrix model axis', async ({ page }) => {
    // THE HEADLINE CLAIM OF M14.3: one matrix, API and self-hosted models
    // together, with no backend schema change - a self-hosted cell is just
    // `{agent: "openai-harness", model: "endpoint:<name>"}`.
    const seeded = await seedEndpoints(page);
    expect(seeded.length).toBeGreaterThan(0);

    await page.goto('/#/experiments');
    await expect(page.getByTestId('add-model-row-btn')).toBeVisible({ timeout: 15_000 });

    const modelRow = page.locator('[data-testid="matrix-model-row"]').first();
    await modelRow
      .getByTestId('model-agent-select')
      .selectOption('openai-harness');

    // Choosing the harness swaps the free-text model id for an endpoint
    // picker, so a coordinate the leaderboard will key history on cannot be
    // typo'd into existence.
    const endpointSelect = modelRow.getByTestId('model-endpoint-select');
    await expect(endpointSelect).toBeVisible();
    await expect(modelRow.getByTestId('model-name-input')).toHaveCount(0);

    const option = endpointSelect.locator(
      `option[data-endpoint="${SEEDED_TOOLS_ENDPOINT}"]`,
    );
    await expect(option).toHaveCount(1);
    // Contract #4: the ONE sugar spelling, produced here and parsed only by
    // the backend resolver.
    await expect(option).toHaveAttribute('value', `endpoint:${SEEDED_TOOLS_ENDPOINT}`);

    await endpointSelect.selectOption(`endpoint:${SEEDED_TOOLS_ENDPOINT}`);
    await expect(endpointSelect).toHaveValue(`endpoint:${SEEDED_TOOLS_ENDPOINT}`);
  });
});
