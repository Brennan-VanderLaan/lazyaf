/**
 * RENDERING, THEME AND KEYBOARD regressions.
 *
 * Scope: the four full-page routes (Pipelines, Specs, Experiments, Endpoints)
 * and the pipeline graph editor. Everything here encodes a defect that was
 * MEASURED in a browser before it was fixed; the comment on each test records
 * the number so nobody has to re-derive it.
 *
 * Like the specs in e2e/qa/, these drive the REAL frontend but serve the API
 * from `page.route` fixtures rather than a live backend. The findings are
 * rendering bugs, so the only thing that has to be real is the Svelte component
 * tree — and the shared QA stack is reset by other agents mid-run, which makes
 * anything depending on durable rows flaky by construction.
 *
 * Payload shapes follow the backend's wire format (naive-UTC `created_at` with
 * no `Z`, exactly like `datetime.utcnow().isoformat()`).
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5191 \
 *     npx playwright test e2e/rendering.spec.ts
 */
import { test, expect, type Page, type ConsoleMessage } from '@playwright/test';

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------

const REPO_ID = '00000000-0000-4000-8000-0000000rend0';
const PIPELINE_ID = '11111111-1111-4111-8111-111111111111';

/** Serialize like the backend does: `datetime.utcnow().isoformat()` — NO 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

function repo(over: Record<string, unknown> = {}) {
  return {
    id: REPO_ID,
    name: 'rendering-repo',
    remote_url: null,
    default_branch: 'main',
    is_ingested: true,
    internal_git_url: `/git/${REPO_ID}.git`,
    created_at: naiveUtc(),
    ...over,
  };
}

/**
 * A one-step pipeline, in the shape `GET /api/pipelines/{id}` returns since
 * 12.8 P3: a GRAPH, and no `steps` array at all.
 *
 * It used to be a v1 row (`steps: [...]`, `steps_graph: null`) that relied on
 * `PipelineEditorPage.convertLegacyToGraph` to build the graph on the fly.
 * That function is gone with the array - conversion happens once, at the YAML
 * boundary - so a fixture shaped like that now loads NOTHING and the editor
 * (correctly) refuses to open it. The position is the one the converter used
 * to invent, { x: 100, y: 0 }, so every layout assertion below is measuring
 * the same geometry it always was.
 */
function graphPipeline(over: Record<string, unknown> = {}) {
  return {
    id: PIPELINE_ID,
    repo_id: REPO_ID,
    name: 'nightly',
    description: 'Runs the suite',
    steps_graph: {
      version: 2,
      entry_points: ['step_0'],
      steps: {
        step_0: {
          id: 'step_0',
          name: 'Echo',
          type: 'script',
          config: { command: 'echo hello' },
          position: { x: 100, y: 0 },
          timeout: 300,
          continue_in_context: false,
        },
      },
      edges: [],
    },
    definition_error: null,
    triggers: [],
    is_template: false,
    created_at: naiveUtc(),
    updated_at: naiveUtc(),
    ...over,
  };
}

function endpoint(over: Record<string, unknown> = {}) {
  return {
    id: 'ep-1',
    name: 'local-4090',
    server_kind: 'ollama',
    model: 'qwen2.5-coder:32b',
    base_url: 'http://192.168.1.50:11434/v1',
    reach: 'direct',
    health: 'healthy',
    enabled: true,
    priced: true,
    rate_usd_hour: 0.35,
    gpu_fraction: 1,
    gpu_node_id: 'node-a',
    auth_style: 'none',
    auth_secret_ref: null,
    secret_present: true,
    in_flight: 0,
    max_concurrency: 1,
    consecutive_failures: 0,
    runner_count: null,
    runner_label: null,
    warning: null,
    last_error: null,
    context_window_source: 'probe',
    probe_detail: {},
    capabilities: {
      probe_status: 'ok',
      probed_at: naiveUtc(-60_000),
      probed_from: 'backend',
      context_window: 32768,
      tools: 'yes',
      streaming: 'yes',
      usage: 'yes',
    },
    ...over,
  };
}

/**
 * Intercept every endpoint the shell touches on boot so nothing leaks to a real
 * backend.
 *
 * Match on the PATHNAME, not a glob: `**​/api/**` also matches the vite dev
 * server's own module URL `/src/lib/api/client.ts`, and fulfilling that with
 * JSON kills the module graph and boots a blank page.
 */
async function mockApi(
  page: Page,
  opts: { endpoints?: unknown[]; pipeline?: unknown; pipelines?: unknown[] } = {},
) {
  const json = (body: unknown) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      const path = new URL(route.request().url()).pathname;

      if (path === '/api/repos') return route.fulfill(json([repo()]));

      // The sidebar's RepoInfo mounts as soon as a repo is selected and reads
      // `response.clone_url` / `.branches` / `.commits` off these three. The
      // catch-all `[]` below is NOT a valid shape for them: RepoInfo then
      // throws "Cannot read properties of undefined (reading 'length')" inside
      // an effect, and ONE thrown effect stops the whole Svelte 5 flush — after
      // which unrelated pages stop updating. Model them explicitly.
      if (path === `/api/repos/${REPO_ID}/clone-url`) {
        return route.fulfill(json({ clone_url: `http://localhost/git/${REPO_ID}.git` }));
      }
      if (path === `/api/repos/${REPO_ID}/branches`) {
        return route.fulfill(
          json({ branches: [{ name: 'main', commit: 'abc1234', is_default: true }] }),
        );
      }
      if (path === `/api/repos/${REPO_ID}/commits`) {
        return route.fulfill(json({ commits: [] }));
      }

      if (path === `/api/repos/${REPO_ID}/pipelines`) {
        return route.fulfill(json(opts.pipelines ?? []));
      }
      if (path === `/api/pipelines/${PIPELINE_ID}`) {
        return route.fulfill(json(opts.pipeline ?? graphPipeline()));
      }
      if (path === '/api/model-endpoints') return route.fulfill(json(opts.endpoints ?? []));
      if (path === '/api/models') return route.fulfill(json([]));
      if (path === '/api/features') return route.fulfill(json([]));
      if (path === '/api/experiments') return route.fulfill(json([]));

      // Anything unmodelled: an empty list is the least surprising default.
      return route.fulfill(json([]));
    },
  );
}

/**
 * A platform pipeline row, as `/api/repos/{id}/pipelines` returns it since
 * 12.8 P3 - i.e. with a graph and no `steps` array. These rows only ever
 * populate the list for the console-cleanliness and layout specs, so the
 * graph is the smallest legal one.
 */
function platformPipeline(id: string, name: string) {
  return {
    id,
    repo_id: REPO_ID,
    name,
    description: null,
    steps_graph: {
      version: 2,
      entry_points: ['step_0'],
      steps: {
        step_0: {
          id: 'step_0',
          name: 'Echo',
          type: 'script',
          config: { command: 'echo hello' },
          position: { x: 100, y: 0 },
          timeout: 300,
          continue_in_context: false,
        },
      },
      edges: [],
    },
    definition_error: null,
    triggers: [],
    is_template: false,
    created_at: naiveUtc(),
    updated_at: naiveUtc(),
  };
}

/** Collect everything the page logs, so a spec can assert on a clean console. */
function captureConsole(page: Page): { errors: string[]; warnings: string[] } {
  const errors: string[] = [];
  const warnings: string[] = [];
  page.on('console', (msg: ConsoleMessage) => {
    // Vite's own dev-server chatter is not application output.
    if (msg.text().startsWith('[vite]')) return;
    if (msg.type() === 'error') errors.push(msg.text());
    if (msg.type() === 'warning') warnings.push(msg.text());
  });
  page.on('pageerror', (err) => errors.push(`pageerror: ${err.message}`));
  return { errors, warnings };
}

/** Open the graph editor for the legacy pipeline and wait for both nodes. */
async function openGraphEditor(page: Page) {
  await page.goto(`/#/pipelines/${PIPELINE_ID}/edit`);
  await expect(page.locator('[data-testid="graph-editor"]')).toBeVisible();
  await expect(page.locator('.svelte-flow__node-start')).toBeVisible();
  await expect(page.locator('[data-testid="graph-node-step_0"]')).toBeVisible();
}

// --------------------------------------------------------------------------
// Console health
// --------------------------------------------------------------------------

test.describe('console is clean', () => {
  /**
   * The graph editor logged "Use $state.raw for edges to prevent performance
   * issues." on every mount — the only console warning the app produced on any
   * route.
   *
   * It was NOT a reactivity problem. Svelte Flow guesses at deep reactivity by
   * calling `structuredClone(edges[0])` and treating a throw as proof of a
   * proxy (node_modules/@xyflow/svelte/dist/lib/store/initial-store.svelte.js);
   * our edges carried `onConditionChange` / `onDelete` closures in `data`, and
   * a function is what actually made the clone throw. The handlers now travel
   * through the GRAPH_ACTIONS context (components/graph/actions.ts), leaving
   * `data` serialisable. Switching to `$state.raw` would NOT have silenced it.
   */
  test('opening the graph editor logs nothing', async ({ page }) => {
    const log = captureConsole(page);
    await mockApi(page);
    await openGraphEditor(page);

    expect(log.errors, 'console errors').toEqual([]);
    expect(log.warnings, 'console warnings').toEqual([]);
  });

  for (const [name, hash] of [
    ['pipelines', '#/pipelines'],
    ['specs', '#/specs'],
    ['experiments', '#/experiments'],
    ['endpoints', '#/endpoints'],
  ] as const) {
    test(`the ${name} page logs nothing`, async ({ page }) => {
      const log = captureConsole(page);
      await mockApi(page, { endpoints: [endpoint()] });
      await page.goto(`/${hash}`);
      await page.getByText('rendering-repo', { exact: true }).first().click();
      // Let any deferred effect / fetch settle before judging the console.
      await page.waitForTimeout(1000);

      expect(log.errors, 'console errors').toEqual([]);
      expect(log.warnings, 'console warnings').toEqual([]);
    });
  }
});

// --------------------------------------------------------------------------
// Graph editor layout
// --------------------------------------------------------------------------

test.describe('graph editor layout', () => {
  /**
   * The START disc sat ON the first step's lower-left corner for every legacy
   * pipeline. Exact arithmetic: the v1->v2 conversion puts step_0 at
   * { x: 100, y: 0 } (it was `PipelineEditorPage.convertLegacyToGraph`; since
   * 12.8 it is `array_to_graph` at the YAML boundary, same coordinates) while
   * the Start node defaulted to { x: 50, y: 50 } at 64x64 — 50+64=114 runs past
   * the step's x=100, and the boxes overlapped by 14x21px. Measured on the QA
   * stack before the fix; deterministic, not a layout race.
   *
   * The default is now { x: 0, y: 0 }: same row as step_0, 36px clear of it.
   * It is deliberately NOT negative — `fitView` does not reliably reframe this
   * canvas (on a client-side route into the editor the viewport transform stays
   * translate(0,0) scale(1)), and under an identity transform a node at a
   * negative x is simply off the left edge.
   */
  test('the START node does not overlap the first step', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    const start = await page.locator('.svelte-flow__node-start').boundingBox();
    const step = await page.locator('[data-testid="graph-node-step_0"]').boundingBox();
    if (!start || !step) throw new Error('graph nodes did not render a box');

    const overlapX = Math.min(start.x + start.width, step.x + step.width) - Math.max(start.x, step.x);
    const overlapY =
      Math.min(start.y + start.height, step.y + step.height) - Math.max(start.y, step.y);

    expect(
      overlapX > 0 && overlapY > 0,
      `START ${JSON.stringify(start)} overlaps step ${JSON.stringify(step)} by ${overlapX}x${overlapY}px`,
    ).toBe(false);
  });

  /**
   * The Start -> first step edge must be drawn as a CONNECTOR, not as a
   * self-loop.
   *
   * ConditionEdge decided "self-loop" from screen distance
   * (`|sourceX - targetX| < 50 && |sourceY - targetY| < 50`) rather than from
   * the edge's own node ids, so any two DISTINCT nodes sitting close together
   * got a circular loop drawn between them. Moving Start clear of step_0 put
   * its source handle 36px from step_0's target handle and tripped exactly
   * that: the edge rendered as a teardrop hanging above the two nodes.
   *
   * A self-loop path is `M x y C ...` back to its own start; a bezier
   * connector ends somewhere else. Comparing the path's start and end points
   * distinguishes them without depending on the exact curve.
   */
  test('the Start edge is drawn as a connector, not a self-loop', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    const d = await page.locator('.svelte-flow__edge path.svelte-flow__edge-path').first().getAttribute('d');
    if (!d) throw new Error('the Start edge rendered no path');

    const numbers = d.match(/-?\d+(\.\d+)?/g)?.map(Number) ?? [];
    expect(numbers.length, `edge path had too few coordinates: ${d}`).toBeGreaterThanOrEqual(4);
    const [startX, startY] = numbers;
    const endX = numbers[numbers.length - 2];
    const endY = numbers[numbers.length - 1];

    const returnsToItsOrigin =
      Math.abs(startX - endX) < 1 && Math.abs(startY - endY) < 1;
    expect(returnsToItsOrigin, `edge path returns to its own start: ${d}`).toBe(false);
  });

  /**
   * The other half of the same change: an edge that really does start and end
   * on the SAME node must still be drawn as a loop. Identity is now the test,
   * so this is the case that must keep working.
   */
  test('a genuine self-loop is still drawn as a loop', async ({ page }) => {
    await mockApi(page, {
      pipeline: {
        ...graphPipeline(),
        steps_graph: {
          version: 2,
          entry_points: ['step_0'],
          steps: {
            step_0: {
              id: 'step_0',
              name: 'Retry',
              type: 'script',
              config: { command: 'echo hi' },
              position: { x: 300, y: 120 },
              timeout: 300,
              continue_in_context: false,
            },
          },
          edges: [
            { id: 'edge_self', from_step: 'step_0', to_step: 'step_0', condition: 'failure' },
          ],
        },
      },
    });
    await page.goto(`/#/pipelines/${PIPELINE_ID}/edit`);
    await expect(page.locator('[data-testid="graph-node-step_0"]')).toBeVisible();

    const selfEdge = page.locator('.svelte-flow__edge[data-id="edge_self"] path.svelte-flow__edge-path');
    const d = await selfEdge.first().getAttribute('d');
    if (!d) throw new Error('the self edge rendered no path');

    const numbers = d.match(/-?\d+(\.\d+)?/g)?.map(Number) ?? [];
    const [startX, startY] = numbers;
    const endX = numbers[numbers.length - 2];
    const endY = numbers[numbers.length - 1];

    expect(
      Math.abs(startX - endX) < 1 && Math.abs(startY - endY) < 1,
      `a self-loop path must return to its own start, got: ${d}`,
    ).toBe(true);
  });

  /** Both nodes have to be inside the canvas, not parked off its left edge. */
  test('both nodes render inside the canvas', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    const canvas = await page.locator('[data-testid="graph-canvas"]').boundingBox();
    if (!canvas) throw new Error('graph canvas did not render a box');

    for (const selector of ['.svelte-flow__node-start', '[data-testid="graph-node-step_0"]']) {
      const box = await page.locator(selector).boundingBox();
      if (!box) throw new Error(`${selector} did not render a box`);
      expect(box.x, `${selector} left edge vs canvas left edge`).toBeGreaterThanOrEqual(canvas.x - 1);
      expect(
        box.x + box.width,
        `${selector} right edge vs canvas right edge`,
      ).toBeLessThanOrEqual(canvas.x + canvas.width + 1);
    }
  });
});

// --------------------------------------------------------------------------
// Keyboard
// --------------------------------------------------------------------------

test.describe('keyboard', () => {
  /**
   * The step node carries role="button" and tabindex="0" but only had an
   * `ondblclick` handler, so it took focus, announced itself as a button, and
   * then ignored every key — a dead end, and double-click is the only route the
   * palette's own tip advertises.
   */
  test('Enter on a focused step node opens its config', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    await page.locator('[data-testid="graph-node-step_0"]').focus();
    await page.keyboard.press('Enter');

    await expect(page.locator('[data-testid="step-config-modal"]')).toBeVisible();
  });

  /** Same element, Space — the other key a role="button" must honour. */
  test('Space on a focused step node opens its config', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    await page.locator('[data-testid="graph-node-step_0"]').focus();
    await page.keyboard.press(' ');

    await expect(page.locator('[data-testid="step-config-modal"]')).toBeVisible();
  });

  /**
   * The palette items also claim role="button" and take tab focus, but their
   * only handlers were dragstart/dragend — and a drag cannot be performed from
   * a keyboard, so Enter did nothing at all.
   */
  test('Enter on a palette item adds that step', async ({ page }) => {
    await mockApi(page);
    await openGraphEditor(page);

    await page.locator('[data-testid="palette-item-docker"]').focus();
    await page.keyboard.press('Enter');

    // The modal opens on the type that was activated, so its docker-only
    // fields are what proves the right step was added rather than a default.
    const modal = page.locator('[data-testid="step-config-modal"]');
    await expect(modal).toBeVisible();
    await expect(modal).toContainText('Docker Image');
  });

  /**
   * Escape closes the register/edit endpoint dialog. Every other modal in the
   * app does this (CardModal, AgentFileModal, graph/StepConfigModal,
   * debug/DebugRerunModal); this one had no keydown handler anywhere, so the
   * only way out was the small "x" — measured, and it silently breaks a habit
   * the rest of the app teaches.
   */
  test('Escape closes the register-endpoint dialog', async ({ page }) => {
    await mockApi(page, { endpoints: [endpoint()] });
    await page.goto('/#/endpoints');

    await page.locator('[data-testid="register-endpoint-btn"]').click();
    const modal = page.locator('[data-testid="endpoint-modal"]');
    await expect(modal).toBeVisible();

    await page.keyboard.press('Escape');
    await expect(modal).toBeHidden();
  });

  /** Escape must not close the endpoints page's step-config-free happy path. */
  test('Escape with no dialog open does nothing visible', async ({ page }) => {
    await mockApi(page, { endpoints: [endpoint()] });
    await page.goto('/#/endpoints');
    await expect(page.locator('[data-testid="endpoints-table"]')).toBeVisible();

    await page.keyboard.press('Escape');

    await expect(page.locator('[data-testid="endpoints-table"]')).toBeVisible();
    await expect(page.locator('[data-testid="endpoint-modal"]')).toHaveCount(0);
  });
});

// --------------------------------------------------------------------------
// Theme
// --------------------------------------------------------------------------

test.describe('theme', () => {
  test.use({ colorScheme: 'light' });

  /**
   * LazyAF has exactly one theme and it is dark — the whole palette is defined
   * unconditionally on `:root` in App.svelte with no light variant. app.css
   * still carried the Vite scaffold's `color-scheme: light dark`, so on a
   * machine whose OS is set to light (the Windows default) everything the
   * BROWSER paints rather than the stylesheet came out light: screenshotted,
   * the radio controls in this dialog rendered as white discs on the dark form.
   */
  test('native controls follow the dark UI on a light-OS machine', async ({ page }) => {
    await mockApi(page);
    await page.goto('/#/endpoints');
    await page.locator('[data-testid="register-endpoint-btn"]').click();
    await expect(page.locator('[data-testid="endpoint-modal"]')).toBeVisible();

    const schemes = await page.evaluate(() => {
      const radio = document.querySelector('.modal input[type=radio]');
      const select = document.querySelector('.modal select');
      return {
        root: getComputedStyle(document.documentElement).colorScheme,
        radio: radio ? getComputedStyle(radio).colorScheme : null,
        select: select ? getComputedStyle(select).colorScheme : null,
      };
    });

    expect(schemes.root, 'documentElement color-scheme').toBe('dark');
    expect(schemes.radio, 'radio color-scheme').toBe('dark');
    expect(schemes.select, 'select color-scheme').toBe('dark');
  });

  /**
   * app.css also carried the scaffold's `@media (prefers-color-scheme: light)`
   * override, which flipped `:root { color }` to #213547 and
   * `button { background-color }` to #f9f9f9 on the dark chrome. Nothing was
   * picking either up when it was removed — every button sets its own
   * background — but the first one that forgot would have rendered
   * white-on-dark for light-OS users and looked perfect on a dark-mode dev
   * machine. This asserts the trap stays disarmed.
   */
  test('no element inherits a light-mode scaffold colour', async ({ page }) => {
    await mockApi(page, { endpoints: [endpoint()] });
    await page.goto('/#/endpoints');
    await expect(page.locator('[data-testid="endpoints-table"]')).toBeVisible();

    const leaks = await page.evaluate(() => ({
      // #213547 — the scaffold's light-mode :root color.
      darkTextOnDarkChrome: [...document.querySelectorAll('body *')].filter(
        (el) => getComputedStyle(el).color === 'rgb(33, 53, 71)',
      ).length,
      // #f9f9f9 — the scaffold's light-mode button background.
      nearWhiteButtons: [...document.querySelectorAll('button')].filter(
        (b) => getComputedStyle(b).backgroundColor === 'rgb(249, 249, 249)',
      ).length,
    }));

    expect(leaks.darkTextOnDarkChrome, 'elements using the light-mode :root color').toBe(0);
    expect(leaks.nearWhiteButtons, 'buttons using the light-mode button background').toBe(0);
  });
});

// --------------------------------------------------------------------------
// Layout at real window sizes
// --------------------------------------------------------------------------

test.describe('layout', () => {
  /**
   * Eight columns do not fit beside the 320px sidebar. Measured at 1280x800:
   * the endpoints table is 1130px inside an 894px scroller, so Probe / Edit /
   * Delete started at x=1239 — past the right edge of the window. The table
   * does scroll inside `.table-scroll`, but that scrollbar is faint and easy to
   * miss, so a page's primary per-row actions simply looked absent on a normal
   * laptop. At 1024 the overflow is 452px.
   *
   * The actions column is now sticky to the right of the scroller.
   */
  for (const [width, height] of [
    [1280, 800],
    [1024, 768],
  ] as const) {
    test(`endpoint row actions are on screen at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height });
      await mockApi(page, { endpoints: [endpoint()] });
      await page.goto('/#/endpoints');
      await expect(page.locator('[data-testid="endpoints-table"]')).toBeVisible();

      for (const testid of [
        'endpoint-probe-btn',
        'endpoint-edit-btn',
        'endpoint-delete-btn',
      ]) {
        const box = await page.locator(`[data-testid="${testid}"]`).boundingBox();
        if (!box) throw new Error(`${testid} did not render a box`);
        expect(box.x, `${testid} left edge at ${width}px`).toBeGreaterThanOrEqual(0);
        expect(box.x + box.width, `${testid} right edge at ${width}px`).toBeLessThanOrEqual(width);
      }
    });
  }

  /**
   * With the pinned actions column in place, the columns that scroll UNDER it
   * are whatever does not fit — and the Enabled toggle is a control, not a
   * label, so it should not be the thing that goes under at the width this is
   * demoed at. Wrapping the header labels, trimming the URL sub-line (which
   * already ellipsised, with a title tooltip) and tightening the cell padding
   * brought a 1042px table down to 894px, exactly the space available beside
   * the sidebar at 1280x800.
   *
   * This asserts the fit for a representative row. An unusually long endpoint
   * name will still push the last columns under the pinned actions — that is
   * the pinning working as intended, not a regression.
   */
  test('the whole endpoints table fits at 1280x800', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await mockApi(page, { endpoints: [endpoint()] });
    await page.goto('/#/endpoints');
    await expect(page.locator('[data-testid="endpoints-table"]')).toBeVisible();

    const fit = await page.evaluate(() => {
      const scroller = document.querySelector('.table-scroll')!;
      return { client: scroller.clientWidth, scroll: scroller.scrollWidth };
    });
    expect(
      fit.scroll - fit.client,
      `endpoints table overflows its scroller by ${fit.scroll - fit.client}px`,
    ).toBeLessThanOrEqual(0);

    // ...and therefore the enable/disable toggle is on screen too.
    const box = await page.locator('[data-testid="endpoint-enabled-toggle"]').boundingBox();
    if (!box) throw new Error('enabled toggle did not render a box');
    expect(box.x + box.width, 'enabled toggle right edge').toBeLessThanOrEqual(1280);
  });

  /**
   * `overflow: hidden` on the page wrappers turned "off-screen" into
   * "unreachable". The sidebar keeps 260px of a 375px viewport, the page then
   * gets 115px, and the primary action laid out at x=484 ("+ New Pipeline") was
   * clipped away with no scrollbar anywhere on the page to reach it — measured:
   * documentElement.scrollWidth stayed at 375 because every parent clipped.
   *
   * The pages now scroll on the x axis, so the action is always reachable. The
   * proper narrow-width answer is a collapsing sidebar; that lives in
   * App.svelte and is NOT what this asserts.
   */
  for (const [name, hash, action] of [
    ['pipelines', '#/pipelines', 'add-pipeline'],
    ['specs', '#/specs', 'add-feature-btn'],
  ] as const) {
    test(`the ${name} primary action is reachable at 375px`, async ({ page }) => {
      await page.setViewportSize({ width: 375, height: 812 });
      await mockApi(page);
      await page.goto(`/${hash}`);
      await page.getByText('rendering-repo', { exact: true }).first().click();

      const button = page.locator(`[data-testid="${action}"]`);
      await expect(button).toHaveCount(1);

      // The point of the fix: whatever does not fit can still be scrolled to
      // and clicked, rather than being clipped out of existence.
      await button.scrollIntoViewIfNeeded();
      const box = await button.boundingBox();
      if (!box) throw new Error(`${action} did not render a box after scrolling to it`);
      // 1px of slack: sub-pixel layout puts a flush-right edge at 375.078.
      // Before the fix this same measurement was x=484..576 with no scroll
      // container anywhere able to reach it, so the tolerance cannot mask it.
      expect(box.x, `${action} left edge at 375px`).toBeGreaterThanOrEqual(-1);
      expect(box.x + box.width, `${action} right edge at 375px`).toBeLessThanOrEqual(376);
    });
  }

  /** No page may blow the document out sideways at a normal laptop width. */
  for (const [name, hash] of [
    ['pipelines', '#/pipelines'],
    ['specs', '#/specs'],
    ['experiments', '#/experiments'],
    ['endpoints', '#/endpoints'],
  ] as const) {
    test(`the ${name} page does not overflow the document at 1280px`, async ({ page }) => {
      await page.setViewportSize({ width: 1280, height: 800 });
      await mockApi(page, { endpoints: [endpoint()] });
      await page.goto(`/${hash}`);
      await page.getByText('rendering-repo', { exact: true }).first().click();
      await page.waitForTimeout(500);

      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow, 'documentElement horizontal overflow').toBeLessThanOrEqual(0);
    });
  }
});

// --------------------------------------------------------------------------
// Live-data row identity
// --------------------------------------------------------------------------

test.describe('live updates keep row identity', () => {
  /**
   * `pipelinesStore` is rewritten by `pipeline_updated` / `pipeline_deleted`
   * frames and `activeRunsStore` is replaced wholesale by the 3s
   * `loadRecent()` poll. Both `{#each}` blocks were UNKEYED, so Svelte reused
   * each element BY POSITION: measured on the runs table, the identical <tr>
   * node that read "trig-1" afterwards read "trig-NEW", every row below having
   * shifted inside its existing element. A click already in flight then lands
   * on a different run — and on the pipelines grid the equivalent misfire is
   * pressing Run on the wrong pipeline, which starts a paid agent container.
   *
   * The stores are module singletons, so the spec drives the REAL one through
   * the dev server's own module URL rather than mocking a WebSocket.
   */
  test('deleting the first pipeline does not rewrite the cards below it', async ({ page }) => {
    await mockApi(page, {
      pipelines: [
        platformPipeline('pl-alpha', 'alpha'),
        platformPipeline('pl-bravo', 'bravo'),
        platformPipeline('pl-charlie', 'charlie'),
      ],
    });
    await page.goto('/#/pipelines');
    await page.getByText('rendering-repo', { exact: true }).first().click();

    const cards = page.locator('[data-testid="pipeline"]');
    await expect(cards).toHaveCount(3);

    // Tag the DOM node that is currently showing "charlie".
    await page.evaluate(() => {
      const charlie = [...document.querySelectorAll('[data-testid="pipeline"]')].find((el) =>
        el.textContent?.includes('charlie'),
      );
      if (!charlie) throw new Error('charlie card not found');
      (charlie as HTMLElement).dataset.marked = 'charlie-node';
    });

    // A `pipeline_deleted` frame removes the FIRST pipeline. This is the real
    // WebSocket path — stores/websocket.ts calls `deleteLocal` on that frame,
    // and `handleServerMessage` is the exact function the socket dispatches to,
    // so this exercises the shipped code rather than a stand-in.
    await page.evaluate(async () => {
      const ws: any = await import('/src/lib/stores/websocket.ts');
      ws.handleServerMessage({ type: 'pipeline_deleted', payload: { id: 'pl-alpha' } });
    });

    await expect(cards).toHaveCount(2);

    // Unkeyed, Svelte reused elements by position: charlie's element was the
    // one destroyed, and the element that had been bravo's was rewritten to
    // read "charlie". Keyed, charlie's own element survives and still says so.
    const marked = page.locator('[data-marked="charlie-node"]');
    await expect(marked, 'charlie\'s original DOM node survived the delete').toHaveCount(1);
    await expect(marked, 'the node that was charlie still reads charlie').toContainText('charlie');
  });
});
