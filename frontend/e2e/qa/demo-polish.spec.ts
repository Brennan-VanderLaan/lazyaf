/**
 * QA-6 — first-run experience and demo-polish regressions.
 *
 * These specs drive the REAL frontend but serve the API from `page.route`
 * fixtures instead of a live backend. That is deliberate:
 *
 *  - the findings here are RENDERING bugs, so the only thing that has to be
 *    real is the Svelte component tree;
 *  - the QA stack at :8790 is shared by several concurrent QA agents that
 *    call `/api/test/reset`, so any spec depending on durable backend rows
 *    is flaky by construction;
 *  - the payloads below are byte-for-byte what the FastAPI backend actually
 *    emits (naive-UTC `created_at` with no `Z`, verified against
 *    `POST /api/repos` on the QA stack on 2026-08-30).
 *
 * Specs marked `fixme` encode a CONFIRMED bug: they fail against current
 * behaviour on purpose and must be flipped to passing when the bug is fixed.
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5175 \
 *     npx playwright test e2e/qa/demo-polish.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

// --------------------------------------------------------------------------
// Fixtures shaped exactly like the backend's wire format.
// --------------------------------------------------------------------------

const REPO_ID = '00000000-0000-4000-8000-00000000repo'.replace('repo', 'r3p0');

/** Serialize like the backend does: `datetime.utcnow().isoformat()` — NO 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

function repo(over: Record<string, unknown> = {}) {
  return {
    id: REPO_ID,
    name: 'demo-repo',
    remote_url: null,
    default_branch: 'main',
    is_ingested: true,
    internal_git_url: `/git/${REPO_ID}.git`,
    created_at: naiveUtc(),
    ...over,
  };
}

function pipeline(over: Record<string, unknown> = {}) {
  return {
    id: 'p-1',
    repo_id: REPO_ID,
    name: 'nightly',
    description: 'Runs the suite',
    steps: [
      { name: 'build', type: 'script', config: { command: 'make' }, on_success: 'next', on_failure: 'stop', timeout: 300, continue_in_context: false },
    ],
    triggers: [],
    is_template: false,
    created_at: naiveUtc(),
    updated_at: naiveUtc(),
    ...over,
  };
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: 'run-1',
    pipeline_id: 'p-1',
    status: 'running',
    trigger_type: 'manual',
    trigger_ref: null,
    trigger_context: null,
    current_step: 0,
    steps_completed: 0,
    steps_total: 1,
    active_step_ids: [],
    completed_step_ids: [],
    started_at: naiveUtc(),
    completed_at: null,
    created_at: naiveUtc(),
    step_runs: [],
    ...over,
  };
}

/**
 * Intercept every endpoint the shell touches on boot so nothing leaks through
 * to a real backend, then hand the caller a hook for the interesting ones.
 */
async function mockApi(
  page: Page,
  opts: { pipelines?: unknown[]; runs?: unknown[]; repos?: unknown[] } = {},
) {
  const repos = opts.repos ?? [repo()];
  const pipelines = opts.pipelines ?? [];
  const runs = opts.runs ?? [];

  const json = (body: unknown) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  // Match on the PATHNAME, not a glob: `**/api/**` also matches the vite dev
  // server's own module URL `/src/lib/api/client.ts`, and fulfilling that with
  // JSON kills the module graph and boots a blank page.
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
    const path = new URL(route.request().url()).pathname;

    if (path === '/api/repos') return route.fulfill(json(repos));
    if (path === `/api/repos/${REPO_ID}/pipelines`) return route.fulfill(json(pipelines));
    if (path === `/api/repos/${REPO_ID}/lazyaf/pipelines`) return route.fulfill(json([]));
    if (path === `/api/repos/${REPO_ID}/lazyaf/agents`) return route.fulfill(json([]));
    if (path === '/api/pipeline-runs') return route.fulfill(json(runs));
    if (path.startsWith('/api/pipeline-runs/')) return route.fulfill(json(runs[0] ?? run()));
    if (path === '/api/runners') return route.fulfill(json([]));
    if (path === '/api/agent-files') return route.fulfill(json([]));
    if (path === '/api/models') return route.fulfill(json([]));
    if (path === '/api/features') return route.fulfill(json([]));

      // Anything unmodelled: an empty list is the least surprising default.
      return route.fulfill(json([]));
    },
  );
}

/** Boot the shell, pick the one repo, land on the Pipelines page. */
async function openPipelines(page: Page, tab: 'pipelines' | 'runs' = 'pipelines') {
  await page.goto('/#/pipelines');
  await page.getByText('demo-repo', { exact: true }).click();
  if (tab === 'runs') {
    await page.getByRole('button', { name: /^Runs \(/ }).click();
  }
}

// --------------------------------------------------------------------------
// FINDING 1 (BLOCKER) — naive-UTC timestamps parsed as local time.
// --------------------------------------------------------------------------

test.describe('QA-6 finding 1: naive UTC vs local time', () => {
  /**
   * The backend stores and serializes `datetime.utcnow()` with no timezone
   * designator (`"2026-08-30T10:33:35.485909"`). ECMA-262 parses a date-time
   * form WITHOUT an offset as LOCAL time, so `new Date(started_at)` lands
   * `getTimezoneOffset()` minutes away from the truth. For a RUNNING run the
   * other operand is `Date.now()` (correct), so the difference is negative
   * everywhere west of UTC.
   *
   * Root cause: backend emits naive UTC (app/models/*.py `default=datetime.utcnow`)
   * and PipelinesPage.svelte:125-133 subtracts it from `Date.now()`.
   */
  test('a RUNNING run must not show a negative duration', async ({ page }) => {
    await mockApi(page, { pipelines: [pipeline()], runs: [run()] });
    await openPipelines(page, 'runs');

    const duration = page.locator('[data-testid="runs-list"] tbody tr td').nth(4);
    const text = (await duration.textContent())?.trim() ?? '';

    // Current behaviour in America/New_York: "-14400s".
    expect(text, `duration cell rendered "${text}"`).not.toMatch(/-/);
  });

  /**
   * Same root cause, second symptom: the "Started" column shows the UTC clock
   * reading formatted as if it were local, so a run started "just now" is
   * displayed hours in the future.
   */
  test('the Started column must show the run start in local time', async ({ page }) => {
    await mockApi(page, { pipelines: [pipeline()], runs: [run()] });
    await openPipelines(page, 'runs');

    const started = page.locator('[data-testid="runs-list"] tbody tr td').nth(3);
    const text = (await started.textContent())?.trim() ?? '';

    // Reconstruct what the browser thinks the wall clock is right now.
    const nowLocalHour = await page.evaluate(() =>
      new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    );
    const shownHour = text.split(' ').slice(1).join(' ');

    expect(
      shownHour,
      `Started cell says "${shownHour}" but local wall clock is "${nowLocalHour}"`,
    ).toBe(nowLocalHour);
  });

  /**
   * The same three-line `formatDuration` is duplicated verbatim in the run
   * detail modal, so opening a running run reproduces the negative duration
   * a second time.
   * Root cause: PipelineRunViewer.svelte:124-132.
   */
  test('the run detail modal must not show a negative duration', async ({ page }) => {
    await mockApi(page, { pipelines: [pipeline()], runs: [run()] });
    await openPipelines(page, 'runs');
    await page.getByRole('button', { name: 'View' }).first().click();

    const meta = page.locator('[data-testid="run-viewer"] .run-meta');
    const text = (await meta.textContent())?.trim() ?? '';
    expect(text, `run meta rendered "${text}"`).not.toMatch(/Duration:\s*-/);
  });
});

// --------------------------------------------------------------------------
// FINDING 2 (POLISH) — "1 steps".
// --------------------------------------------------------------------------

/**
 * `{pipeline.steps.length} steps` is unconditional, so the single-step
 * pipeline a demo starts with reads "1 steps".
 * Root cause: PipelinesPage.svelte — `.step-count` span, both the repo-card
 * and platform-card branches.
 */
test.fail('a one-step pipeline reads "1 step", not "1 steps"', async ({ page }) => {
  await mockApi(page, { pipelines: [pipeline()] });
  await openPipelines(page);

  const meta = page.locator('[data-testid="pipeline"] .step-count').first();
  await expect(meta).toHaveText('1 step');
});

// --------------------------------------------------------------------------
// FINDING 3 (MAJOR) — unbounded text blows out the layout.
// --------------------------------------------------------------------------

/**
 * The backend applies no length limit to a pipeline name (a 5000-character
 * name is accepted with 201), and `.card-header h3` / `.card-description`
 * declare no `overflow`, `text-overflow`, or `word-break`. An unbreakable
 * 5000-character token therefore escapes its grid track.
 */
test.fail('a very long pipeline name must not overflow its card', async ({ page }) => {
  await mockApi(page, {
    pipelines: [pipeline({ name: 'Q'.repeat(5000), description: 'D'.repeat(5000) })],
  });
  await openPipelines(page);

  const card = page.locator('[data-testid="pipeline"]').first();
  const overflow = await card.evaluate((el) => {
    const parent = el.parentElement!;
    return {
      cardScroll: el.scrollWidth,
      cardClient: el.clientWidth,
      gridScroll: parent.scrollWidth,
      gridClient: parent.clientWidth,
    };
  });

  expect(
    overflow.cardScroll,
    `card scrollWidth ${overflow.cardScroll} exceeds clientWidth ${overflow.cardClient}`,
  ).toBeLessThanOrEqual(overflow.cardClient + 1);
});

/**
 * The consequence that makes finding 3 a MAJOR rather than a cosmetic clip:
 * `.card-header` is `display:flex; justify-content:space-between`, so a
 * 66,000px-wide `<h3>` pushes `.card-actions` — the Edit and Run buttons —
 * far outside the card, where `.pipelines-page { overflow: hidden }` clips
 * them. The pipeline becomes unrunnable from the UI.
 */
test.fail('the Run button stays reachable when the pipeline name is huge', async ({ page }) => {
  await mockApi(page, { pipelines: [pipeline({ name: 'Q'.repeat(5000) })] });
  await openPipelines(page);

  const runBtn = page.locator('[data-testid="pipeline"] .btn-run').first();
  const box = await runBtn.boundingBox();
  const viewport = page.viewportSize()!;

  expect(box, 'Run button has no layout box at all').not.toBeNull();
  expect(
    box!.x,
    `Run button starts at x=${box!.x}px, outside the ${viewport.width}px viewport`,
  ).toBeLessThan(viewport.width);
});

// --------------------------------------------------------------------------
// FINDING 4 (MAJOR) — the runs store is append-only, so the Runs tab shows
// ghosts of runs the backend no longer has.
// --------------------------------------------------------------------------

/**
 * `activeRunsStore.loadRecent()` merges the response into a Map with
 * `map.set(run.id, run)` and never evicts (stores/pipelines.ts:112-123).
 * `removeRun()` and `clear()` exist but no component ever calls them, so a run
 * that disappears server-side — pipeline deleted, `/api/test/reset`, DB
 * rollback — stays on screen with a live "View" button until a hard reload.
 *
 * Worse, `hasActiveRuns` (stores/pipelines.ts:347) scans that same stale Map,
 * so a ghost stuck in `running` keeps the 3-second poll and the pulsing
 * "active" dot alive forever.
 */
test.fail('a run that vanishes server-side must disappear from the Runs tab', async ({ page }) => {
  let serverRuns: unknown[] = [run()];

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
      if (path === '/api/pipeline-runs') return route.fulfill(json(serverRuns));
      if (path === `/api/repos/${REPO_ID}/pipelines`) return route.fulfill(json([pipeline()]));
      return route.fulfill(json([]));
    },
  );

  await openPipelines(page, 'runs');
  await expect(page.locator('[data-testid="runs-list"] tbody tr')).toHaveCount(1);

  // The backend loses the run (pipeline deleted / stack reset). The page is
  // already polling every 3s because the run is "running", so it WILL refetch.
  serverRuns = [];
  await page.waitForTimeout(7000);

  await expect(
    page.locator('[data-testid="runs-list"] tbody tr'),
    'the Runs tab still lists a run the backend no longer has',
  ).toHaveCount(0);
});

// --------------------------------------------------------------------------
// FINDING 5 (MAJOR) — the runner sidebar freezes at "ws 0s".
// --------------------------------------------------------------------------

/**
 * Third symptom of the naive-UTC root cause, and the most visible one because
 * the runner panel is in the sidebar on EVERY page.
 *
 * `connectionAge` (RunnerPanel.svelte:81-89) clamps with `Math.max(0, …)`, so
 * instead of a negative number it pins to zero: a runner that enrolled an hour
 * ago reads "ws 0s" for as long as the UTC offset lasts. The clamp hides the
 * bug rather than fixing it.
 */
test('a runner connected 30 minutes ago does not read "ws 0s"', async ({ page }) => {
  const connectedAt = naiveUtc(-30 * 60 * 1000); // half an hour ago, naive UTC

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
      if (path === '/api/runners') {
        return route.fulfill(
          json([
            {
              id: 'r-1',
              name: 'dogfood loopback agent',
              runner_type: 'generic',
              status: 'idle',
              connection: 'websocket',
              connected_at: connectedAt,
              last_heartbeat: connectedAt,
              labels: {},
              current_job_id: null,
            },
          ]),
        );
      }
      return route.fulfill(json([]));
    },
  );

  await page.goto('/#/');
  const age = page.locator('[data-testid="runner-connection"]').first();
  await expect(age).toBeVisible();
  await expect(age, 'runner connection age is pinned to zero').not.toHaveText(/^\s*ws 0s\s*$/);
});

// --------------------------------------------------------------------------
// FINDING 6 (MINOR) — an empty repo name renders an unlabelled sidebar row.
// --------------------------------------------------------------------------

/**
 * `POST /api/repos {"name": ""}` is accepted with 201 (no min_length on
 * `RepoBase.name`, backend/app/schemas/repo.py:5), and the sidebar renders the
 * name verbatim — so the repo becomes an invisible, unlabelled click target.
 */
test.fail('a repo with an empty name still renders something clickable', async ({ page }) => {
  const json = (body: unknown) => ({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/repos') return route.fulfill(json([repo({ name: '' })]));
      return route.fulfill(json([]));
    },
  );

  await page.goto('/#/');
  const name = page.locator('.repo-name').first();
  await expect(name).toBeVisible();
  const text = (await name.textContent())?.trim() ?? '';
  expect(text, 'sidebar repo row has no visible label').not.toBe('');
});

// --------------------------------------------------------------------------
// Guard rails — these already behave, and must keep behaving.
// --------------------------------------------------------------------------

test('a COMPLETED run shows a correct, non-negative duration', async ({ page }) => {
  // Both operands are naive UTC, so the subtraction cancels the offset out.
  const started = naiveUtc(-90_000);
  await mockApi(page, {
    pipelines: [pipeline()],
    runs: [run({ status: 'passed', steps_completed: 1, started_at: started, completed_at: naiveUtc() })],
  });
  await openPipelines(page, 'runs');

  // The two fixture timestamps are taken microseconds apart, so the floored
  // second can land on 29, 30 or 31 — assert the shape and the sign, not an
  // exact tick.
  const duration = page.locator('[data-testid="runs-list"] tbody tr td').nth(4);
  await expect(duration).toHaveText(/^1m (29|30|31)s$/);
});

test('the run progress bar never renders a NaN width', async ({ page }) => {
  await mockApi(page, { pipelines: [pipeline()], runs: [run()] });
  await openPipelines(page, 'runs');
  await page.getByRole('button', { name: 'View' }).first().click();

  const width = await page
    .locator('[data-testid="run-viewer"] .progress-fill')
    .getAttribute('style');
  expect(width ?? '').not.toContain('NaN');
});

test('the first-run empty states explain what to do next', async ({ page }) => {
  await mockApi(page, { repos: [] });
  await page.goto('/#/pipelines');

  // No repo selected yet: the page must say so rather than render blank.
  await expect(page.getByText('Select a repository to manage pipelines')).toBeVisible();
});
