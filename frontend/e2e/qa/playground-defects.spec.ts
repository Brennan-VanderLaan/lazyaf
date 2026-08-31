/**
 * QA-7 - the Agent Playground: the four defects the owner reported himself,
 * plus the adversarial probes around them.
 *
 * Like `demo-polish.spec.ts`, these specs drive the REAL frontend but serve
 * the API from `page.route` fixtures, and they replace `window.EventSource`
 * with a controllable fake. That is deliberate:
 *
 *  - every finding here is a CLIENT-SIDE rendering/lifecycle bug, so the only
 *    thing that has to be real is the Svelte component tree and the store;
 *  - the log stream has to be driven line-by-line WITH KNOWN TIMING, which a
 *    real agent container cannot give you - and timing is the whole bug (see
 *    the note on `burst` below);
 *  - the QA stack at :8790 is shared by several concurrent QA agents that
 *    call `/api/test/reset`, so anything depending on durable rows is flaky.
 *
 * Payloads below are byte-faithful to the QA backend, captured on 2026-08-30
 * from `GET /api/repos/{id}/branches`, `GET /api/models`,
 * `GET /api/repos/{id}/commits` and `GET /api/playground/{id}/result`.
 *
 * Specs marked `fixme` encode a CONFIRMED bug: they fail against current
 * behaviour on purpose and must be flipped to passing when the bug is fixed.
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5176 \
 *     npx playwright test e2e/qa/playground-defects.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

const REPO_ID = '00000000-0000-4000-8000-0000000pl4y';
const SESSION_ID = '11d4156d-9601-4794-a4c9-e65a4941d1e3';

/** Serialize like the backend does: `datetime.utcnow().isoformat()` - NO 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

const REPO = {
  id: REPO_ID,
  name: 'demo-repo',
  remote_url: null,
  default_branch: 'main',
  is_ingested: true,
  internal_git_url: `/git/${REPO_ID}.git`,
  created_at: naiveUtc(),
};

const BRANCHES = {
  branches: [
    { name: 'lazyaf/seed-review', commit: 'b910296', is_default: false, is_lazyaf: true },
    { name: 'main', commit: '275f887', is_default: true, is_lazyaf: false },
  ],
  default_branch: 'main',
  total: 2,
};

const MODEL_LIST = [
  { id: 'claude-sonnet-4-5-20250929', name: 'Claude Sonnet 4.5', provider: 'anthropic', description: 'Fast, 1M context' },
  { id: 'claude-opus-4-5-20250929', name: 'Claude Opus 4.5', provider: 'anthropic', description: 'Most powerful' },
];
const MODELS = { models: MODEL_LIST, anthropic: MODEL_LIST, google: [] };

/** The 13 lines a `runner_type: mock` playground run really emits. */
const MOCK_LOGS = [
  '[agent] agent=mock model=<default> stream=True workdir=/workspace/repo backend=http://backend-qa:8000',
  '[agent] spec context: none (no spec links for this card)',
  '[mock] Using default config',
  '[mock] Starting mock execution',
  '[mock] Creating file: .lazyaf-mock-marker',
  '[AI] Mock executor starting...',
  '[AI] Applying mock file operations...',
  '[AI] Mock execution complete.',
  '[mock] Mock execution completed successfully',
  '[agent] on branch playground/11d4156d',
  "[agent] committed the agent's changes",
  '[agent] pushing playground/11d4156d...',
  '[agent] pushed playground/11d4156d',
];

const RESULT = {
  session_id: SESSION_ID,
  status: 'completed',
  diff: '--- a/.lazyaf-mock-marker\n+++ b/.lazyaf-mock-marker\n@@ -0,0 +1 @@\n+# Mock executor ran\n',
  files_changed: ['.lazyaf-mock-marker'],
  branch_saved: null,
  error: null,
  logs: MOCK_LOGS.join('\n'),
  duration_seconds: 1.585683,
};

// --------------------------------------------------------------------------
// A controllable EventSource, so the log stream can be driven line by line.
// --------------------------------------------------------------------------

declare global {
  interface Window {
    __sse: {
      count(): number;
      closed(): boolean;
      emit(type: string, data: unknown): void;
      /**
       * Emit `n` lines every `ms`, THEN STOP.
       *
       * The pause at the end is the point. A real agent emits in bursts with
       * gaps while it thinks, and PlaygroundPage's autoscroll is a 100 ms
       * debounce: while lines keep arriving the pending timer is cleared and
       * re-armed and never fires, so the pane does not follow the output at
       * all. The moment the burst stops, the last timer survives its 100 ms
       * and slams the pane to the bottom. A uniform firehose hides this
       * completely - the pane just sits still and every spec passes.
       */
      burst(n: number, ms: number, prefix: string): void;
    };
  }
}

async function installFakeSSE(page: Page) {
  await page.addInitScript(() => {
    const instances: any[] = [];

    class FakeEventSource {
      url: string;
      readyState = 1;
      onerror: ((e: any) => void) | null = null;
      private listeners = new Map<string, Function[]>();
      constructor(url: string) {
        this.url = url;
        instances.push(this);
      }
      addEventListener(type: string, fn: Function) {
        const arr = this.listeners.get(type) ?? [];
        arr.push(fn);
        this.listeners.set(type, arr);
      }
      removeEventListener(type: string, fn: Function) {
        const arr = this.listeners.get(type) ?? [];
        this.listeners.set(type, arr.filter((f) => f !== fn));
      }
      close() {
        this.readyState = 2;
      }
      __emit(type: string, payload: unknown) {
        if (this.readyState === 2) return;
        const ev = new MessageEvent(type, {
          data: JSON.stringify({ type, data: payload, timestamp: new Date().toISOString() }),
        });
        for (const fn of this.listeners.get(type) ?? []) fn(ev);
      }
    }

    (window as any).EventSource = FakeEventSource;
    (window as any).__sse = {
      count: () => instances.length,
      closed: () => instances[instances.length - 1]?.readyState === 2,
      emit: (type: string, data: unknown) => instances[instances.length - 1]?.__emit(type, data),
      burst: (n: number, ms: number, prefix: string) => {
        let i = 0;
        const h = setInterval(() => {
          if (i >= n) {
            clearInterval(h);
            return;
          }
          const pad = new Array(31).join('.');
          instances[instances.length - 1]?.__emit('log', prefix + ' ' + String(i).padStart(4, '0') + ' ' + pad);
          i += 1;
        }, ms);
      },
    };
  });
}

// --------------------------------------------------------------------------
// API fixtures
// --------------------------------------------------------------------------

type Opts = { result?: unknown; cancelStatus?: number; cancelBody?: unknown };

async function mockApi(page: Page, opts: Opts = {}) {
  const json = (body: unknown, status = 200) => ({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });

  // Match on the PATHNAME, not a glob: `**/api/**` also matches the vite dev
  // server's own module URL `/src/lib/api/client.ts`, and fulfilling that
  // with JSON kills the module graph and boots a blank page.
  await page.route(
    (url) => url.pathname.startsWith('/api/'),
    async (route) => {
      const path = new URL(route.request().url()).pathname;

      if (path === '/api/repos') return route.fulfill(json([REPO]));
      if (path === `/api/repos/${REPO_ID}/branches`) return route.fulfill(json(BRANCHES));
      if (path === '/api/models') return route.fulfill(json(MODELS));

      // The sidebar's RepoInfo panel reads `.commits.length` and `.clone_url`
      // straight off these responses. Answering them with the catch-all `[]`
      // throws inside RepoInfo's effect, and that thrown effect stops the
      // WHOLE flush - the Playground's own `disabled` attribute then never
      // updates again and every spec below fails for the wrong reason.
      if (path === `/api/repos/${REPO_ID}/clone-url`) {
        return route.fulfill(json({ clone_url: `http://localhost:8790/git/${REPO_ID}.git`, is_ingested: true }));
      }
      if (path === `/api/repos/${REPO_ID}/commits`) {
        return route.fulfill(json({ branch: 'main', commits: [{ sha: '275f8877a977bce4dba868a1bbf8f5352235e5b3', short_sha: '275f8877', message: 'Seed commit', author: 'LazyAF Test <test@lazyaf.local>', timestamp: 0 }], total: 1 }));
      }
      if (path === `/api/repos/${REPO_ID}/branches/info`) {
        return route.fulfill(json({ branches: [], total: 0, orphaned_count: 0, damaged_count: 0 }));
      }

      if (path === `/api/repos/${REPO_ID}/playground/test`) {
        return route.fulfill(json({ session_id: SESSION_ID, status: 'running', message: 'Test running in an ephemeral agent container' }));
      }
      if (path === `/api/playground/${SESSION_ID}/result`) {
        return route.fulfill(json(opts.result ?? RESULT));
      }
      if (path === `/api/playground/${SESSION_ID}/cancel`) {
        return route.fulfill(json(opts.cancelBody ?? { status: 'cancelled', session_id: SESSION_ID }, opts.cancelStatus ?? 200));
      }
      return route.fulfill(json([]));
    },
  );
}

/** Boot the shell, pick the one repo, land on a usable Playground. */
async function openPlayground(page: Page) {
  await installFakeSSE(page);
  await page.goto('/#/playground');
  await page.getByText('demo-repo', { exact: true }).click();
  await expect(page.getByTestId('config-panel')).toBeVisible();
  // The branch select is populated asynchronously and defaults to `main`.
  await expect(page.locator('#branch')).toHaveValue('main');
}

async function startRun(page: Page, task = 'say hello') {
  await page.getByTestId('task-input').fill(task);
  await page.getByTestId('start-test-btn').click();
  await expect.poll(() => page.evaluate(() => window.__sse.count())).toBeGreaterThan(0);
}

const logsContainer = (page: Page) => page.getByTestId('logs-container');
const statusBadge = (page: Page) => page.locator('.playground-page .page-header .status-badge');

/** Fill the log pane, then let the burst end so the pane settles. */
async function fillAndSettle(page: Page, lines = 250) {
  await page.evaluate((n) => window.__sse.burst(n, 6, 'first'), lines);
  await expect.poll(() => logsContainer(page).locator('.log-line').count(), { timeout: 20_000 }).toBeGreaterThanOrEqual(lines);
  await page.waitForTimeout(400);
}

// ==========================================================================
// (a) "outputs weren't saved"
// ==========================================================================

test.describe("QA-7 (a): a finished run's output must still be there later", () => {
  /**
   * PASSES today, and is here to stay passing: `playgroundStore` is a
   * module-level singleton, so a route change does not clear the logs. The
   * owner's "outputs weren't saved" is the reload case below, plus the
   * navigate-away cancel further down.
   */
  test('output survives leaving the Playground and coming back', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await page.getByRole('link', { name: /Board/ }).click();
    await expect(page.getByTestId('playground-page')).toHaveCount(0);
    await page.getByRole('link', { name: /Playground/ }).click();

    await expect(
      logsContainer(page),
      'the agent output the user just watched is gone after a round trip through another page',
    ).toContainText('Mock execution complete.');
  });

  /**
   * CONFIRMED BUG. Nothing about a session is written anywhere the browser
   * can find it again: `PlaygroundState` lives only in the store's closure,
   * and nothing is put in localStorage/sessionStorage (verified empty).
   *
   * The output itself IS durable server-side - the run leaves a PipelineRun
   * (`trigger_type='playground'`, `trigger_ref=<session_id>`) whose StepRun
   * `logs` column holds the full transcript. This is a READ-PATH gap, not a
   * persistence gap.
   */
  test.fixme('output survives a page reload', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await page.reload();
    await page.getByText('demo-repo', { exact: true }).click();

    await expect(
      logsContainer(page),
      'a browser refresh loses the run entirely - the session id is only ever held in JS memory',
    ).toContainText('Mock execution complete.');
  });

  /**
   * CONFIRMED BUG, and the expensive one: reloading DURING a run orphans the
   * agent container. The session stays alive server-side for the 30-minute
   * TTL, but the page comes back with no session id, so there is no Cancel
   * button and no way to reach it - it just runs, and bills, unwatched.
   */
  test.fixme('a reload mid-run can still reach (and cancel) the running session', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('status', 'running'));
    await page.evaluate(() => window.__sse.emit('logs_batch', ['[agent] working...']));
    await expect(page.getByTestId('cancel-btn')).toBeVisible();

    await page.reload();
    await page.getByText('demo-repo', { exact: true }).click();

    await expect(
      page.getByTestId('cancel-btn'),
      'after a refresh the agent container is still running and the UI offers no way to stop it',
    ).toBeVisible();
  });
});

// ==========================================================================
// (b) "there was no history"
// ==========================================================================

test.describe('QA-7 (b): previous runs must be reachable', () => {
  /**
   * CONFIRMED BUG. There is no history UI of any kind on this page.
   *
   * As with (a), the DATA already exists: the prompt is on the hidden
   * `__lazyaf_adhoc__:playground:<id>` pipeline row as
   * `steps[0].config.task`, and the transcript is on the StepRun. What is
   * missing is a list endpoint and a panel.
   */
  test.fixme('a completed run is listed in a run history', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page, 'first question');

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await expect(
      page.getByTestId('playground-history'),
      'nothing on the page records that a run ever happened',
    ).toBeVisible();
    await expect(page.getByTestId('playground-history')).toContainText('first question');
  });

  /** CONFIRMED BUG: `startTest()` resets `logs: []`, overwriting run N-1. */
  test.fixme('starting a second run does not destroy the first one', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page, 'first question');
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await startRun(page, 'second question');
    await page.evaluate(() => window.__sse.emit('log', 'second run line'));

    await expect(
      page.getByTestId('playground-history'),
      'the first run is overwritten in place with no way back to it',
    ).toContainText('first question');
  });
});

// ==========================================================================
// (c) "output wouldn't scroll up"
// ==========================================================================

test.describe('QA-7 (c): the user must be able to read back through output', () => {
  /**
   * CONFIRMED BUG, with the mechanism nailed down.
   *
   * PlaygroundPage.svelte:79-86 debounces the autoscroll by 100 ms, and the
   * timeout callback re-checks only `logsContainer` - NOT `autoScroll`. So
   * the snap that was armed by the last log line before the user scrolled
   * still fires, and it fires ~100 ms after the output pauses, which is
   * constantly during a real run.
   *
   * Traced behaviour (250-line burst, wheel up, then a 700 ms gap):
   *   parked at 5317 -> t=1209ms one scroll event -> 6731 (the bottom).
   */
  test.fixme('a pane the user scrolled up must stay put when the output pauses', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page);

    const c = logsContainer(page);
    await c.hover();
    await page.mouse.wheel(0, -100000);
    const parked = await c.evaluate((el) => el.scrollTop);
    expect(parked, 'the wheel gesture should have moved the pane off the bottom').toBeLessThan(200);

    // The agent thinks for a moment, then emits again - the ordinary shape
    // of a real run.
    await page.waitForTimeout(700);
    await page.evaluate(() => window.__sse.burst(60, 6, 'second'));
    await page.waitForTimeout(900);

    const after = await c.evaluate((el) => el.scrollTop);
    expect(
      after,
      `the pane was parked at ${parked} and the pause in the output snapped it to ${after}`,
    ).toBeLessThan(parked + 200);
  });

  /**
   * CONFIRMED BUG, second symptom of the same debounce: while output is
   * actually flowing the pane does NOT follow it, because every incoming
   * line clears the pending timer before it can fire. The user watching a
   * live run sees the output freeze, then jump.
   */
  test.fixme('the pane follows live output instead of freezing then jumping', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page);

    const c = logsContainer(page);
    await page.evaluate(() => window.__sse.burst(120, 8, 'second'));
    await page.waitForTimeout(500);

    const mid = await c.evaluate((el) => ({ top: el.scrollTop, h: el.scrollHeight, ch: el.clientHeight }));
    expect(
      mid.h - mid.top - mid.ch,
      `mid-burst the pane is ${mid.h - mid.top - mid.ch}px behind the newest line`,
    ).toBeLessThan(100);
  });
});

// ==========================================================================
// (d) "couldn't highlight parts to copy it because of the data refreshing"
// ==========================================================================

test.describe('QA-7 (d): a text selection must survive incoming output', () => {
  /**
   * PASSES today. Kept as the guard that rules out the obvious suspect: the
   * unkeyed `{#each logs as log}` at PlaygroundPage.svelte:464 APPENDS - it
   * does not re-key or replace existing nodes, so Svelte never touches the
   * text node a selection is anchored in. DOM churn is NOT the cause of (d).
   * Do not "fix" (d) by keying this block.
   */
  test('a selection made in earlier output survives appended lines', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 60);

    const selected = await page.evaluate(() => {
      const line = document.querySelectorAll('[data-testid="logs-container"] .log-line')[5];
      const range = document.createRange();
      range.selectNodeContents(line);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      return sel.toString();
    });
    expect(selected).toContain('first 0005');

    await page.evaluate(() => window.__sse.burst(60, 6, 'later'));
    await expect.poll(() => logsContainer(page).locator('.log-line').count()).toBeGreaterThanOrEqual(120);
    await page.waitForTimeout(400);

    const still = await page.evaluate(() => window.getSelection()?.toString() ?? '');
    expect(still, 'appending log lines must not disturb an existing selection').toContain('first 0005');
  });

  /**
   * CONFIRMED BUG - and the actual cause of (d). It is the SAME autoscroll
   * snap as (c), not a re-render.
   *
   * With the mouse button still down, the burst ends, the 100 ms timer fires
   * and scrolls the pane to the bottom. The browser extends the live
   * selection to whatever is now under the stationary cursor, so a careful
   * two-line highlight becomes a 24x larger block of the wrong text.
   *
   * Measured: 73 chars mid-drag -> 1781 chars after the snap.
   */
  test.fixme('a two-line drag-selection is not blown up by an autoscroll snap', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page);

    const box = (await logsContainer(page).boundingBox())!;
    await page.mouse.move(box.x + 30, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 260, box.y + box.height / 2 + 24, { steps: 8 });
    const midDrag = await page.evaluate(() => window.getSelection()?.toString() ?? '');
    expect(midDrag.length, 'the drag should have selected roughly two lines').toBeGreaterThan(20);

    // The agent emits another short burst while the button is still down.
    await page.evaluate(() => window.__sse.burst(40, 6, 'second'));
    await page.waitForTimeout(700);
    await page.mouse.up();

    const final = await page.evaluate(() => window.getSelection()?.toString() ?? '');
    expect(
      final.length,
      `the user selected ${midDrag.length} chars and ended up with ${final.length} - the pane scrolled out from under the cursor`,
    ).toBeLessThan(midDrag.length * 2);
  });

  /**
   * CONFIRMED GAP. Hand-selecting is the ONLY way to get the transcript out
   * of this pane - the output header offers just "Clear". A Copy button
   * would make (d) mostly moot on its own.
   */
  test.fixme('there is a way to copy the output without hand-selecting it', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await expect(
      page.locator('.logs-section').getByRole('button', { name: /copy/i }),
      'the output panel offers only "Clear" - there is no way to copy the transcript',
    ).toBeVisible();
  });
});

// ==========================================================================
// Beyond the four: adversarial probes.
// ==========================================================================

test.describe('QA-7: adversarial probes', () => {
  /**
   * CONFIRMED BUG. PlaygroundPage.svelte:240-244 cancels the run in
   * `onDestroy`, so clicking Board/Pipelines mid-run kills the agent
   * container the user is paying for, with no warning and no undo.
   */
  test.fixme('navigating away must not cancel a running test', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('status', 'running'));
    await expect(page.getByTestId('cancel-btn')).toBeVisible();

    const cancels: string[] = [];
    page.on('request', (r) => {
      if (r.url().includes('/cancel')) cancels.push(r.url());
    });

    await page.getByRole('link', { name: /Board/ }).click();
    await page.waitForTimeout(400);

    expect(
      cancels,
      'clicking another page killed the agent container the user was paying for',
    ).toHaveLength(0);
  });

  /**
   * CONFIRMED BUG, and an R1 violation. The backend goes out of its way to
   * turn an un-cancellable run into a 503 ("the container may still be
   * running", playground_service.cancel_test). The store throws that away -
   * `catch { /* Ignore cancel errors *\/ }` in stores/playground.ts - and
   * writes "cancelled" anyway. The user is told the thing stopped while it
   * is still running and still spending.
   */
  test.fixme('a cancel the server REFUSED is not reported as success', async ({ page }) => {
    await mockApi(page, {
      cancelStatus: 503,
      cancelBody: { detail: 'could not cancel the agent run behind session 11d4156d (docker kill failed); the container may still be running' },
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('status', 'running'));
    await page.getByTestId('cancel-btn').click();
    await page.waitForTimeout(600);

    await expect(
      page.locator('.error-message'),
      'the 503 was swallowed and the badge says "Cancelled" while the container runs on',
    ).toContainText('still be running');
  });

  test('a failed run shows the error, not just a status word', async ({ page }) => {
    await mockApi(page, {
      result: { ...RESULT, status: 'failed', diff: null, files_changed: [], error: 'image preflight failed: pull access denied for lazyaf/agent-claude' },
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS.slice(0, 3));
    await page.evaluate(() => window.__sse.emit('complete', 'failed'));

    await expect(page.getByTestId('config-panel')).toContainText('pull access denied');
  });

  /**
   * CONFIRMED BUG. `hasResult` requires a diff, an error, or changed files.
   * An agent that answers a question without editing anything satisfies
   * none of them, so the run ends with NO Changes panel, NO "No changes were
   * made" message and NO Reset button - the most ordinary playground use
   * finishes with the page looking like nothing happened.
   */
  test.fixme('a run that changed nothing still says so', async ({ page }) => {
    await mockApi(page, { result: { ...RESULT, diff: null, files_changed: [] } });
    await openPlayground(page);
    await startRun(page, 'what does this repo do?');
    await page.evaluate(() => window.__sse.emit('logs_batch', ['[agent] reading the repo', '[AI] This repo is a CI platform.']));
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await page.waitForTimeout(400);

    await expect(
      page.locator('.diff-empty'),
      'a completed run that touched no files renders no Changes section at all',
    ).toBeVisible();
  });

  /** CONFIRMED BUG: the header duration is stuck at 0s for every run. */
  test.fixme('the header duration reflects how long the run took', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('status', 'running'));
    await page.waitForTimeout(2500);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await page.waitForTimeout(400);

    await expect(
      statusBadge(page),
      'a run of more than two seconds is reported as "(0s)"',
    ).not.toContainText('(0s)');
  });

  test('a very long single log line does not push the page sideways', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    const huge = 'x'.repeat(40000);
    await page.evaluate((l) => window.__sse.emit('log', l), huge);
    await expect(logsContainer(page).locator('.log-line')).toHaveCount(1);

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow, 'the page itself scrolls horizontally').toBeLessThanOrEqual(1);
  });

  /**
   * CONFIRMED BUG. Agent CLIs colour their output; those bytes reach the
   * pane verbatim, so a real Claude Code run renders "[32mPASS[0m".
   */
  test.fixme('ANSI colour codes are rendered, not printed as mojibake', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('log', '\u001b[32mPASS\u001b[0m tests/test_thing.py'));
    await expect(logsContainer(page).locator('.log-line')).toHaveCount(1);
    await expect(
      logsContainer(page).locator('.log-line'),
      'raw escape sequences reach the user as unreadable control characters',
    ).not.toContainText('[32m');
  });

  /**
   * CONFIRMED BUG: the guard is `!$playgroundStore.taskOverride`, so three
   * spaces is a valid prompt and starts a real container.
   */
  test.fixme('a whitespace-only prompt cannot start a run', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await expect(page.getByTestId('start-test-btn')).toBeDisabled();
    await page.getByTestId('task-input').fill('   ');
    await expect(
      page.getByTestId('start-test-btn'),
      'a whitespace-only prompt is accepted and burns a container',
    ).toBeDisabled();
  });

  test('a cancelled run keeps the output the user already saw', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS.slice(0, 5));
    await expect(logsContainer(page)).toContainText('Starting mock execution');

    await page.getByTestId('cancel-btn').click();
    await expect(logsContainer(page)).toContainText('Starting mock execution');
  });
});
