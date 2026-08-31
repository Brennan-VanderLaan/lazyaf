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
 *  5. THE SIX-STATE MODALITY RULE, which is (4) widened. An input modality can
 *     be supported, refused, never asked, asked-and-the-probe-broke,
 *     asked-and-the-answer-does-not-decide-it, or not expressible on this wire
 *     format at all. No two of those may render alike, and the one an operator
 *     meets first - "not probed", which is what EVERY endpoint registered
 *     before modality detection shipped reads - must never look like "not
 *     supported".
 *
 * WHERE THE SIX STATES COME FROM, and why it differs from (1)-(4). Those drive
 * real hardware because they are claims about DETECTION. The six-state test is
 * a claim about DISPLAY: given state X on the wire, does the page render it
 * distinguishably from state Y. Three of the six cannot be summoned from a
 * healthy box on demand (you cannot ask a working server for a probe timeout),
 * so the registry is served from an intercepted `GET /api/model-endpoints` and
 * the assertion is that the RENDERING never collapses. The provenance half -
 * that these states are real probe output - is pinned separately, against the
 * live backend, by the last test in this file.
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

// =============================================================================
// The six modality states, and the collapses that would each be a lie.
// =============================================================================

/**
 * The six states, paired with the state each one must NOT be confused with.
 *
 * `unprobed`/`unsupported` and `undetectable`/`unsupported` are the two pairs
 * the whole display exists to keep apart; `unprobed`/`probe_failed` is the
 * third, and it is the subtle one because BOTH are `null` in the column and
 * both refuse at dispatch - they differ only in what the human should do next.
 */
const MODALITY_STATES = [
  'supported',
  'unsupported',
  'unprobed',
  'probe_failed',
  'undetectable',
  'unrepresentable',
] as const;

function modality(
  name: string,
  state: string,
  extra: Record<string, string | null> = {},
) {
  return {
    modality: name,
    state,
    source: null,
    reason: null,
    evidence: null,
    caveat: null,
    ...extra,
  };
}

/** One registry row, in the shape `endpoint_read` puts on the wire. */
function endpointFixture(
  name: string,
  modalities: ReturnType<typeof modality>[] | null,
  overrides: Record<string, unknown> = {},
) {
  const capsOverride = (overrides.capabilities ?? {}) as Record<string, unknown>;
  delete overrides.capabilities;
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
      ...(modalities === null ? {} : { modalities }),
      ...capsOverride,
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
    ...overrides,
  };
}

/**
 * One row per state, so all six are on screen at once - which is the only way
 * to assert that no two of them RENDER alike rather than merely that each one
 * renders.
 */
const SIX_STATE_REGISTRY = [
  endpointFixture('m-supported', [
    modality('text', 'supported', { source: 'wire_format' }),
    modality('images', 'supported', { source: 'wire_probe', reason: 'usage_delta_positive' }),
    modality('audio', 'unsupported', { source: 'wire_probe', reason: 'http_400' }),
    modality('video', 'unrepresentable', {
      source: 'wire_format',
      reason: 'wire_format_has_no_video_content_part',
    }),
  ]),
  endpointFixture('m-refused', [
    modality('text', 'supported', { source: 'wire_format' }),
    modality('images', 'unsupported', {
      source: 'wire_probe',
      reason: 'http_400',
      evidence: 'This model does not support image input',
    }),
    modality('audio', 'unsupported', { source: 'wire_probe', reason: 'http_400' }),
    modality('video', 'unrepresentable', { source: 'wire_format' }),
  ]),
  endpointFixture(
    'm-never-asked',
    [
      modality('text', 'supported', { source: 'wire_format' }),
      modality('images', 'unprobed'),
      modality('audio', 'unprobed'),
      modality('video', 'unrepresentable', { source: 'wire_format' }),
    ],
    // The realistic shape of a row registered before modality detection: the
    // PROTOCOL capabilities are answered, the modalities are not.
    { capabilities: { probe_status: 'ok' } },
  ),
  endpointFixture('m-probe-broke', [
    modality('text', 'supported', { source: 'wire_format' }),
    modality('images', 'probe_failed', {
      source: 'wire_probe',
      reason: 'timeout',
      evidence: 'read timed out after 20s',
    }),
    modality('audio', 'probe_failed', { source: 'wire_probe', reason: 'deadline_exhausted' }),
    modality('video', 'unrepresentable', { source: 'wire_format' }),
  ]),
  endpointFixture('m-silent-drop', [
    modality('text', 'supported', { source: 'wire_format' }),
    modality('images', 'undetectable', {
      source: 'wire_probe',
      reason: 'no_usage_delta',
      caveat: 'accepted_200_but_prompt_tokens_unchanged',
    }),
    modality('audio', 'unsupported', { source: 'wire_probe', reason: 'http_400' }),
    modality('video', 'unrepresentable', { source: 'wire_format' }),
  ]),
  // A backend older than modality detection: no `modalities` key at all. NOT
  // the same fact as "unprobed" - a probe cannot add a field to a projection.
  endpointFixture('m-backend-cannot-answer', null),
];

/** Serve a fixed registry and block the socket, so the page is deterministic. */
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

test.describe('modality display: six states, no two alike', () => {
  test('every state renders with its own glyph, its own border and its own words', async ({
    page,
  }) => {
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);
    await expect(row(page, 'm-supported')).toBeVisible({ timeout: 15_000 });

    // Where each state can be found, on the compact table chips.
    const chip = {
      supported: row(page, 'm-supported').getByTestId('endpoint-cap-images'),
      unsupported: row(page, 'm-refused').getByTestId('endpoint-cap-images'),
      unprobed: row(page, 'm-never-asked').getByTestId('endpoint-cap-images'),
      probe_failed: row(page, 'm-probe-broke').getByTestId('endpoint-cap-images'),
      undetectable: row(page, 'm-silent-drop').getByTestId('endpoint-cap-images'),
      unrepresentable: row(page, 'm-supported').getByTestId('endpoint-cap-video'),
    };

    // 1. Each chip carries its own state, machine-readably.
    for (const state of MODALITY_STATES) {
      await expect(
        chip[state],
        `no images/video chip is rendering '${state}'`,
      ).toHaveAttribute('data-state', state);
    }

    // 2. GLYPH: all six differ. Colour is never the only channel, so the text
    //    content alone has to separate them.
    const glyphs: Record<string, string> = {};
    for (const state of MODALITY_STATES) {
      glyphs[state] = ((await chip[state].textContent()) ?? '').replace(/\s/g, '');
    }
    expect(
      new Set(Object.values(glyphs)).size,
      `two modality states render the same glyph: ${JSON.stringify(glyphs)}`,
    ).toBe(MODALITY_STATES.length);

    // 3. BORDER SHAPE: the non-colour channel. `unprobed` (dashed) and
    //    `undetectable` (dotted) are deliberately the same grey, so if the
    //    border collapsed they would be indistinguishable on a mono display.
    const borderOf = async (state: (typeof MODALITY_STATES)[number]) =>
      chip[state].evaluate((el) => getComputedStyle(el).borderTopStyle);
    expect(await borderOf('unprobed')).not.toBe(await borderOf('undetectable'));
    expect(await borderOf('unprobed')).not.toBe(await borderOf('unsupported'));
    expect(await borderOf('undetectable')).not.toBe(await borderOf('unsupported'));

    // 4. WORDS: the tooltip on each chip says something different, and says
    //    the thing that matters about that state.
    const titleOf = async (state: (typeof MODALITY_STATES)[number]) =>
      (await chip[state].getAttribute('title')) ?? '';

    // THE HEADLINE COLLAPSE. "never asked" must not read as "asked, and no".
    const unprobedTitle = await titleOf('unprobed');
    const unsupportedTitle = await titleOf('unsupported');
    expect(unprobedTitle).not.toBe(unsupportedTitle);
    expect(unprobedTitle).toMatch(/not probed/i);
    expect(unprobedTitle).toMatch(/NOT "not supported"|not "not supported"/i);
    expect(unprobedTitle).toMatch(/probe/i);
    expect(unsupportedTitle).toMatch(/refused/i);

    // The subtle collapse: both null in the column, different next action.
    const failedTitle = await titleOf('probe_failed');
    expect(failedTitle).not.toBe(unprobedTitle);
    expect(failedTitle).toMatch(/probe failed/i);
    expect(failedTitle).toMatch(/read the reason/i);
    // A failed probe is UNKNOWN, never "no".
    expect(failedTitle).toMatch(/never "no"/i);

    // The dangerous collapse: a request that succeeds while doing nothing.
    const undetectableTitle = await titleOf('undetectable');
    expect(undetectableTitle).toMatch(/undetectable/i);
    expect(undetectableTitle).toMatch(/200/);
    expect(undetectableTitle).toMatch(/discard|vanish/i);

    // Video is not "unknown", it is not expressible - and it says why.
    const videoTitle = await titleOf('unrepresentable');
    expect(videoTitle).toMatch(/wire format/i);
    expect(videoTitle).toMatch(/any endpoint/i);
  });

  test('the expanded panel spells out the reason the chip only hints at', async ({ page }) => {
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);

    // Expanding a row is how an operator reads probe detail on this page.
    await row(page, 'm-refused').getByTestId('endpoint-name').click();
    const panel = page.getByTestId('endpoint-capabilities-panel');
    await expect(panel).toBeVisible();

    const images = panel.getByTestId('endpoint-cap-images');
    await expect(images).toHaveAttribute('data-state', 'unsupported');
    await expect(images.getByTestId('endpoint-cap-images-state')).toHaveText('not supported');
    // The server's own words, quoted rather than paraphrased.
    await expect(images.getByTestId('endpoint-cap-images-provenance')).toContainText(
      'This model does not support image input',
    );
    await expect(images.getByTestId('endpoint-cap-images-provenance')).toContainText('http_400');

    // ...and video, in the same panel, states the protocol fact.
    const video = panel.getByTestId('endpoint-cap-video');
    await expect(video).toHaveAttribute('data-state', 'unrepresentable');
    await expect(video.getByTestId('endpoint-cap-video-state')).toHaveText('not expressible');
    // Nothing to do about it: no "next action" is offered for a fact about
    // the wire format, because there is no action that would change it.
    await expect(video.getByTestId('endpoint-cap-video-next')).toHaveCount(0);
  });

  test('the panel keeps the BORDER channel alive, so grey states stay apart', async ({ page }) => {
    // `unprobed` and `undetectable` are the same grey ON PURPOSE - both are
    // "we do not know" - so the border SHAPE is the only channel separating
    // them, and a stylesheet that flattens it collapses the two states while
    // every colour assertion still passes. It has already happened once: both
    // `.cap` and `.line` set `border: 1px solid transparent` for layout, and
    // at equal specificity whichever comes last wins.
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);

    const borderIn = async (endpointName: string, modalityName: string) => {
      await row(page, endpointName).getByTestId('endpoint-name').click();
      const panel = page.getByTestId('endpoint-capabilities-panel');
      await expect(panel).toBeVisible();
      const style = await panel
        .getByTestId(`endpoint-cap-${modalityName}`)
        .evaluate((el) => {
          const cs = getComputedStyle(el);
          return { style: cs.borderTopStyle, color: cs.borderTopColor };
        });
      // Collapse again so the next lookup has exactly one panel on screen.
      await row(page, endpointName).getByTestId('endpoint-name').click();
      await expect(panel).toHaveCount(0);
      return style;
    };

    const unprobed = await borderIn('m-never-asked', 'images');
    const undetectable = await borderIn('m-silent-drop', 'images');
    const unsupported = await borderIn('m-refused', 'images');

    // A transparent border is the failure mode: it renders as no border at
    // all, which is how the channel dies silently.
    for (const [name, seen] of Object.entries({ unprobed, undetectable, unsupported })) {
      expect(
        seen.color,
        `the ${name} panel row has a transparent border - the shape channel ` +
          'is dead and only colour is left to carry the state',
      ).not.toMatch(/rgba\(0, 0, 0, 0\)/);
    }

    expect(
      unprobed.style,
      'unprobed and undetectable render the same border shape AND the same ' +
        'grey, so nothing on screen tells them apart',
    ).not.toBe(undetectable.style);
    expect(unprobed.style).not.toBe(unsupported.style);
    expect(undetectable.style).not.toBe(unsupported.style);
  });

  test('the edit modal shows what you are about to invalidate, before you invalidate it', async ({
    page,
  }) => {
    // Editing base_url / model / server_kind / auth nulls the WHOLE capability
    // record, modalities included. An operator who cannot see what is there
    // cannot weigh what a re-probe will cost them, so the same component the
    // table renders is shown read-only above the probe-on-save checkbox.
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);

    await row(page, 'm-supported').getByTestId('endpoint-edit-btn').click();
    await expect(page.getByTestId('endpoint-modal')).toBeVisible();

    const current = page.getByTestId('endpoint-current-capabilities');
    await expect(current).toBeVisible();
    await expect(current.getByTestId('endpoint-cap-images')).toHaveAttribute(
      'data-state',
      'supported',
    );
    await expect(current.getByTestId('endpoint-cap-video')).toHaveAttribute(
      'data-state',
      'unrepresentable',
    );
    // It is a READ-ONLY display of an observation, not an editable field, so
    // it offers no probe verb of its own inside the form.
    await expect(current.getByTestId('endpoint-cap-probe-btn')).toHaveCount(0);

    // Changing an identity field warns, and the warning names the modalities
    // explicitly - otherwise "the capability record" reads as tools-only.
    await page.getByTestId('endpoint-model-input').fill('some-other-model');
    const warning = page.getByTestId('endpoint-capability-reset-warning');
    await expect(warning).toBeVisible();
    await expect(warning).toContainText(/images/i);
    await expect(warning).toContainText(/not probed/i);
    await expect(warning).toContainText(/not supported/i);
  });

  test('an unknown modality is ACTIONABLE where it is read, not only in the toolbar', async ({
    page,
  }) => {
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);

    // The row whose modalities were never asked offers the verb, and names
    // how many questions are outstanding.
    const neverAsked = row(page, 'm-never-asked');
    const probeHere = neverAsked.getByTestId('endpoint-cap-probe-btn');
    await expect(probeHere).toBeVisible();
    await expect(probeHere).toHaveAttribute('data-unanswered', '2');
    await expect(probeHere).toHaveAttribute('title', /images/);
    await expect(probeHere).toHaveAttribute('title', /audio/);

    // A fully answered row offers nothing: there is no question left to ask,
    // so the button is absent rather than present-and-pointless.
    await expect(
      row(page, 'm-supported').getByTestId('endpoint-cap-probe-btn'),
    ).toHaveCount(0);

    // Video alone never makes a row actionable - probing cannot answer a
    // question about the wire format, and offering the verb would send an
    // operator round a loop that can never terminate.
    const refused = row(page, 'm-refused');
    await expect(refused.getByTestId('endpoint-cap-video')).toHaveAttribute(
      'data-state',
      'unrepresentable',
    );
    await expect(refused.getByTestId('endpoint-cap-probe-btn')).toHaveCount(0);
  });

  test('a backend that cannot answer says so, and does NOT say "not probed"', async ({ page }) => {
    await serveRegistry(page, SIX_STATE_REGISTRY);
    await goToEndpoints(page);

    const legacy = row(page, 'm-backend-cannot-answer');
    const note = legacy.getByTestId('endpoint-modalities-unreported');
    await expect(note).toBeVisible();
    // Three things it must NOT be mistaken for, said out loud.
    await expect(note).toHaveAttribute('title', /not "no image support"/i);
    await expect(note).toHaveAttribute('title', /never probed/i);
    await expect(note).toHaveAttribute('title', /update the backend/i);

    // No modality chips are invented for it - a blank is honest here and a
    // grey "unprobed" chip would not be.
    await expect(legacy.getByTestId('endpoint-cap-images')).toHaveCount(0);
    await expect(legacy.getByTestId('endpoint-cap-video')).toHaveCount(0);
    // ...and Probe is not offered, because Probe cannot add a field to an
    // older backend's projection.
    await expect(legacy.getByTestId('endpoint-cap-probe-btn')).toHaveCount(0);
  });
});

// =============================================================================
// The same display, against the LIVE backend. This is the provenance half:
// whatever the page shows here is real probe output, not a fixture.
// =============================================================================

test.describe('capability display against real probe output', () => {
  test('the inline Probe is offered beside an unknown, and a FAILED probe leaves it unknown', async ({
    page,
  }) => {
    // Deliberately unreachable, and deliberately NOT dependent on the mock
    // server: this test asserts two things that must hold whatever is (or is
    // not) listening at the other end -
    //   (a) an unanswered capability carries the verb where it is READ, and
    //   (b) a probe that FAILED leaves the capability UNKNOWN, never FALSE.
    // (b) is the load-bearing half. If a failed probe wrote `false`, every
    // unreachable box would silently claim it cannot do things nobody ever
    // managed to ask it about.
    await resetBackend(page.request);
    await goToEndpoints(page);

    const name = `e2e-inline-probe-${Date.now().toString().slice(-8)}`;
    await page.getByTestId('register-endpoint-btn').click();
    await page.getByTestId('endpoint-name-input').fill(name);
    await page.getByTestId('endpoint-base-url-input').fill('http://127.0.0.1:9/v1');
    await page.getByTestId('endpoint-model-input').fill('nothing-here');
    await page.getByTestId('endpoint-probe-on-save').uncheck();
    await page.getByTestId('endpoint-submit-btn').click();

    const created = row(page, name);
    await expect(created).toBeVisible({ timeout: 20_000 });
    await expect(created.getByTestId('endpoint-cap-tools')).toHaveAttribute(
      'data-state',
      'unprobed',
    );

    // (a) THE VERB IS BESIDE THE UNKNOWN, not only in the toolbar at the far
    //     right of a table that scrolls.
    const probeHere = created.getByTestId('endpoint-cap-probe-btn');
    await expect(probeHere).toBeVisible();
    const unanswered = Number(await probeHere.getAttribute('data-unanswered'));
    expect(unanswered, 'tools, stream and usage are all unanswered here').toBeGreaterThanOrEqual(3);
    await expect(probeHere).toHaveAttribute('title', /tools/);

    await probeHere.click();

    // The probe RAN and its outcome is reported on the row - clicking the
    // inline verb also opens the detail row, because `probe_failed` and
    // `undetectable` are answers you have to read the reason for.
    // The notice lives in the sibling detail row, which is a separate <tr>
    // and so is NOT inside the row locator. The backend was reset above, so
    // this endpoint is the only one on the page.
    const notice = page.getByTestId('endpoint-notice');
    await expect(notice).toBeVisible({ timeout: 40_000 });
    await expect(notice).toContainText(/unreachable/i);

    // (b) AND THE CAPABILITY IS STILL UNKNOWN. A probe that could not connect
    //     is not evidence that the model cannot tool-call.
    await expect(created.getByTestId('endpoint-cap-tools')).toHaveAttribute(
      'data-state',
      'unprobed',
    );
    await expect(created.getByTestId('endpoint-cap-tools')).toHaveAttribute('title', /refuses/i);
    // ...so the question is still open, and the verb is still offered.
    await expect(created.getByTestId('endpoint-cap-probe-btn')).toBeVisible();

    // The page never turned "the endpoint is down" into a page-level error.
    await expect(page.getByTestId('endpoints-error')).toHaveCount(0);
  });

  test('a live row always says SOMETHING honest about modalities, never nothing', async ({
    page,
  }) => {
    // The invariant that holds whether or not the backend half has shipped:
    // exactly one of {the modality chips, the "this backend cannot answer"
    // note} is rendered. Never both, and never a blank where a modality
    // should be - a blank is the state that reads as "no".
    await seedEndpoints(page);
    await goToEndpoints(page);

    const target = row(page, SEEDED_TOOLS_ENDPOINT);
    await expect(target).toBeVisible({ timeout: 15_000 });

    const group = target.getByTestId('endpoint-modalities');
    const unreported = target.getByTestId('endpoint-modalities-unreported');
    const groupCount = await group.count();
    const unreportedCount = await unreported.count();

    expect(
      groupCount + unreportedCount,
      'The capability cell rendered NEITHER the modality chips nor the ' +
        '"this backend does not report modalities" note. A blank there is ' +
        'the one rendering that reads as "no image support".',
    ).toBe(1);

    if (groupCount === 1) {
      // The backend reports modalities: every chip must carry a state from
      // the six-state vocabulary, and video must be the unrepresentable one
      // (this wire format has no video content part, on any server).
      for (const name of ['images', 'audio', 'video']) {
        const cell = target.getByTestId(`endpoint-cap-${name}`);
        await expect(cell, `no chip rendered for modality '${name}'`).toHaveCount(1);
        const state = await cell.getAttribute('data-state');
        expect(
          MODALITY_STATES as readonly string[],
          `endpoint-cap-${name} rendered state '${state}', which is not in ` +
            'the six-state vocabulary this UI knows how to explain.',
        ).toContain(state!);
      }
      await expect(target.getByTestId('endpoint-cap-video')).toHaveAttribute(
        'data-state',
        'unrepresentable',
      );
    }
  });
});
