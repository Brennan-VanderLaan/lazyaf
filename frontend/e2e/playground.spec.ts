/**
 * The Agent Playground: the four things the owner reported, and the probes
 * around them.
 *
 * WHY THIS SPEC USES FIXTURES RATHER THAN A LIVE AGENT. Every behaviour here
 * is client-side lifecycle and rendering, and two of them (the autoscroll and
 * the selection it destroys) are TIMING bugs that only appear when the log
 * stream PAUSES. A real agent container cannot be asked to pause on cue, and
 * a uniform firehose hides the bug completely - the pane just sits still and
 * everything passes. So the API is served from `page.route` and
 * `window.EventSource` is replaced with a controllable fake, exactly as
 * `e2e/qa/playground-defects.spec.ts` established. The Svelte component tree
 * and the real store are the things under test, and they are real.
 *
 * The `burst(n, ms)` helper emits n lines and then STOPS, for that reason.
 * Do not "simplify" it into a continuous stream.
 *
 * Payloads are byte-faithful to the QA backend (captured 2026-08-30 from
 * GET /api/repos/{id}/branches, /api/models, /api/playground/{id}/result and
 * the new /api/repos/{id}/playground/sessions).
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5176 \
 *     npx playwright test e2e/playground.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

const REPO_ID = '00000000-0000-4000-8000-0000000pl4y';
const SESSION_ID = '11d4156d-9601-4794-a4c9-e65a4941d1e3';
const OLDER_SESSION_ID = '9f0e1b22-77aa-4c30-9c58-2b6f0a1d4e77';

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
  source: 'session',
};

/** A completed run that answered a question and edited nothing. */
const RESULT_NO_CHANGES = {
  ...RESULT,
  diff: null,
  files_changed: [],
  duration_seconds: 0.9,
};

/** The same run read back after the 30-minute session sweep. */
const RESULT_FROM_RUN = {
  ...RESULT,
  diff: null,
  files_changed: [],
  source: 'run',
};

const HISTORY_ROW = {
  session_id: SESSION_ID,
  run_id: 'aa11bb22-cc33-4d44-9e55-ff6600112233',
  status: 'completed',
  prompt: 'say hello',
  agent: 'mock',
  model: null,
  base_branch: 'main',
  work_branch: 'playground/11d4156d',
  created_at: naiveUtc(-60_000),
  started_at: naiveUtc(-60_000),
  completed_at: naiveUtc(-58_000),
  duration_seconds: 1.585683,
  live: false,
};

const OLDER_HISTORY_ROW = {
  ...HISTORY_ROW,
  session_id: OLDER_SESSION_ID,
  run_id: 'bb22cc33-dd44-4e55-af66-001122334455',
  prompt: 'what does this repo do?',
  created_at: naiveUtc(-600_000),
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
       * The pause at the end is the whole point - see the file header.
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

type Opts = {
  result?: unknown;
  cancelStatus?: number;
  cancelBody?: unknown;
  history?: unknown[];
  /** What GET /api/playground/{id}/status answers, or 404 when omitted. */
  sessionStatus?: unknown;
  /** Collects every request path the page made, for the "did NOT cancel" test. */
  seen?: string[];
  /** The endpoint registry, for the modality panel. Empty when omitted. */
  endpoints?: unknown[];
  /**
   * What GET /api/playground/capabilities answers.
   *
   * Defaults to this build's real projection. Overridable so the "the limits
   * are the server's numbers" test can serve DIFFERENT ones - which is the
   * only way to prove the page reads them rather than reciting its own.
   */
  capabilities?: unknown;
  /** Status for that read. 500 exercises the fail-closed path. */
  capabilitiesStatus?: number;
};

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
      opts.seen?.push(path);

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

      // The endpoint registry behind the model select and the modality panel.
      if (path === '/api/model-endpoints') {
        return route.fulfill(json(opts.endpoints ?? []));
      }

      // MUST precede the `/api/playground/` prefix checks below: this is a
      // literal path, and the catch-all `[]` at the bottom would otherwise
      // answer it with something that is JSON and is not this.
      if (path === '/api/playground/capabilities') {
        if (opts.capabilitiesStatus && opts.capabilitiesStatus >= 400) {
          return route.fulfill(
            json({ detail: 'capability read exploded' }, opts.capabilitiesStatus),
          );
        }
        return route.fulfill(json(opts.capabilities ?? PLAYGROUND_CAPABILITIES));
      }

      if (path === `/api/repos/${REPO_ID}/playground/sessions`) {
        return route.fulfill(json(opts.history ?? []));
      }
      if (path === `/api/repos/${REPO_ID}/playground/test`) {
        return route.fulfill(json({ session_id: SESSION_ID, status: 'running', message: 'Test running in an ephemeral agent container' }));
      }
      // NOT the catch-all: `[]` here would give the store `status: undefined`,
      // which is a different bug from the one under test.
      if (path.startsWith('/api/playground/') && path.endsWith('/status')) {
        return opts.sessionStatus
          ? route.fulfill(json(opts.sessionStatus))
          : route.fulfill(json({ detail: 'Session not found' }, 404));
      }
      if (path === `/api/playground/${OLDER_SESSION_ID}/result`) {
        return route.fulfill(json({ ...RESULT_FROM_RUN, session_id: OLDER_SESSION_ID, logs: 'older run line one\nolder run line two' }));
      }
      if (path.startsWith('/api/playground/') && path.endsWith('/result')) {
        return route.fulfill(json(opts.result ?? RESULT));
      }
      if (path.startsWith('/api/playground/') && path.endsWith('/cancel')) {
        return route.fulfill(json(opts.cancelBody ?? { status: 'cancelled', session_id: SESSION_ID }, opts.cancelStatus ?? 200));
      }
      return route.fulfill(json([]));
    },
  );
}

/**
 * Select the repo in the sidebar, unless the app already did.
 *
 * App.svelte remembers the last selected repo in localStorage, so after a
 * reload the Playground can come back already pointed at it - and a blind
 * `getByText('demo-repo')` click is then ambiguous against the page's own
 * repo badge.
 */
async function ensureRepoSelected(page: Page) {
  const panel = page.getByTestId('config-panel');
  if (await panel.count()) {
    await expect(panel).toBeVisible();
  } else {
    await page.getByTestId('repo-item').filter({ hasText: 'demo-repo' }).first().click();
    await expect(panel).toBeVisible();
  }
  // The branch select is populated asynchronously and defaults to `main`.
  await expect(page.locator('#branch')).toHaveValue('main');
}

/** Boot the shell, pick the one repo, land on a usable Playground. */
async function openPlayground(page: Page) {
  await installFakeSSE(page);
  await page.goto('/#/playground');
  await ensureRepoSelected(page);
}

async function startRun(page: Page, task = 'say hello') {
  await page.getByTestId('task-input').fill(task);
  await page.getByTestId('start-test-btn').click();
  await expect.poll(() => page.evaluate(() => window.__sse.count())).toBeGreaterThan(0);
}

const logsContainer = (page: Page) => page.getByTestId('logs-container');
const statusBadge = (page: Page) => page.getByTestId('playground-status');

/** Fill the log pane, then let the burst end so the pane settles. */
async function fillAndSettle(page: Page, lines = 250) {
  await page.evaluate((n) => window.__sse.burst(n, 6, 'first'), lines);
  await expect
    .poll(() => logsContainer(page).locator('.log-line').count(), { timeout: 20_000 })
    .toBeGreaterThanOrEqual(lines);
  await page.waitForTimeout(400);
}

function metrics(page: Page) {
  return logsContainer(page).evaluate((el) => ({
    scrollTop: el.scrollTop,
    scrollHeight: el.scrollHeight,
    clientHeight: el.clientHeight,
  }));
}

// ==========================================================================
// Scroll: the pane must obey the user, and follow when the user wants it to
// ==========================================================================

test.describe('the output pane scrolls the way a log viewer scrolls', () => {
  test('a pane the user scrolled up stays put when the output pauses', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 250);

    // Scroll well away from the bottom, the way you would to re-read a line.
    await logsContainer(page).evaluate((el) => {
      el.scrollTop = Math.floor(el.scrollHeight * 0.3);
    });
    await page.waitForTimeout(120);
    const parked = await metrics(page);

    // A burst, then a 700ms gap - which is what a real agent does constantly,
    // and what used to slam the pane back to the bottom.
    await page.evaluate(() => window.__sse.burst(40, 6, 'second'));
    await page.waitForTimeout(1200);

    const after = await metrics(page);
    expect(
      after.scrollTop,
      'the pane moved on its own after the user scrolled up',
    ).toBe(parked.scrollTop);
  });

  test('the pane follows new output WHILE it is arriving, not only at the gaps', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate(() => window.__sse.burst(120, 8, 'flow'));
    // Sample mid-burst. The old debounce was cleared and re-armed by every
    // incoming line, so it could NEVER fire while output was flowing: the
    // pane froze at 0 and drifted behind until the stream paused.
    await page.waitForTimeout(450);
    const mid = await metrics(page);
    expect(mid.scrollTop, 'the pane did not move while output was streaming').toBeGreaterThan(0);

    await expect
      .poll(() => logsContainer(page).locator('.log-line').count(), { timeout: 20_000 })
      .toBeGreaterThanOrEqual(120);
    await page.waitForTimeout(300);

    const end = await metrics(page);
    expect(Math.abs(end.scrollTop + end.clientHeight - end.scrollHeight)).toBeLessThanOrEqual(2);
  });

  test('scrolling back to the bottom resumes following', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 200);

    await logsContainer(page).evaluate((el) => { el.scrollTop = 100; });
    await page.waitForTimeout(120);
    await logsContainer(page).evaluate((el) => { el.scrollTop = el.scrollHeight; });
    await page.waitForTimeout(120);

    await page.evaluate(() => window.__sse.burst(30, 6, 'third'));
    await page.waitForTimeout(900);

    const after = await metrics(page);
    expect(
      Math.abs(after.scrollTop + after.clientHeight - after.scrollHeight),
      'the pane did not resume following after the user scrolled back down',
    ).toBeLessThanOrEqual(2);
  });

  test('a scrolled-up pane offers a way back to the newest output', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 200);

    await expect(page.getByTestId('follow-resume')).toHaveCount(0);
    await logsContainer(page).evaluate((el) => { el.scrollTop = 50; });
    await expect(page.getByTestId('follow-resume')).toBeVisible();

    await page.getByTestId('follow-resume').click();
    await page.waitForTimeout(200);
    const after = await metrics(page);
    expect(Math.abs(after.scrollTop + after.clientHeight - after.scrollHeight)).toBeLessThanOrEqual(2);
  });
});

// ==========================================================================
// Selection: a drag must not be blown up by the pane moving under the cursor
// ==========================================================================

test.describe('highlighting output to copy it', () => {
  test('a held drag-selection is not blown up by an autoscroll snap', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 250);

    const box = await logsContainer(page).boundingBox();
    if (!box) throw new Error('the logs pane has no box - the page did not render');

    // Press and drag across a couple of lines, and KEEP HOLDING.
    await page.mouse.move(box.x + 20, box.y + 30);
    await page.mouse.down();
    await page.mouse.move(box.x + 260, box.y + 55, { steps: 8 });

    const held = await page.evaluate(() => ({
      length: (window.getSelection()?.toString() ?? '').length,
      scrollTop: (document.querySelector('[data-testid="logs-container"]') as HTMLElement).scrollTop,
    }));
    expect(held.length, 'the drag selected nothing - the fixture, not the product, is wrong').toBeGreaterThan(0);

    // Output arrives and then pauses while the button is still down. This is
    // what turned a 73-character selection into 1781 characters of text the
    // user never pointed at: the snap moved the content under a stationary
    // cursor and the browser correctly extended the live selection.
    await page.evaluate(() => window.__sse.burst(60, 6, 'while-holding'));
    await page.waitForTimeout(1000);

    const after = await page.evaluate(() => ({
      length: (window.getSelection()?.toString() ?? '').length,
      scrollTop: (document.querySelector('[data-testid="logs-container"]') as HTMLElement).scrollTop,
    }));
    await page.mouse.up();

    expect(after.scrollTop, 'the pane scrolled while a selection was being dragged').toBe(held.scrollTop);
    expect(after.length, 'the selection grew while the user held it still').toBe(held.length);
  });

  test('appending output leaves an existing selection alone', async ({ page }) => {
    // A guard, not a fix: the `{#each}` over log lines must stay UNKEYED.
    // Keying it would replace nodes on every flush and create exactly the
    // bug this page was reported for.
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 40);

    const selected = await page.evaluate(() => {
      const lines = document.querySelectorAll('[data-testid="logs-container"] .log-line');
      const range = document.createRange();
      range.selectNodeContents(lines[3]);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      return sel.toString();
    });
    expect(selected.length).toBeGreaterThan(0);

    await page.evaluate(() => window.__sse.burst(60, 6, 'appended'));
    await page.waitForTimeout(900);

    const still = await page.evaluate(() => window.getSelection()?.toString() ?? '');
    expect(still, 'appending log lines destroyed a selection over earlier ones').toBe(selected);
  });

  test('a finished selection is not scrolled out from under the user', async ({ page }) => {
    // Appending does not destroy a selection (the test above), but following
    // the tail past it scrolls the highlighted text off screen - which is the
    // same problem from the user's side: they highlighted it to copy it.
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await fillAndSettle(page, 200);

    const before = await page.evaluate(() => {
      const pane = document.querySelector('[data-testid="logs-container"]') as HTMLElement;
      const lines = pane.querySelectorAll('.log-line');
      const range = document.createRange();
      range.selectNodeContents(lines[lines.length - 4]);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      return pane.scrollTop;
    });

    await page.evaluate(() => window.__sse.burst(40, 6, 'after-selection'));
    await page.waitForTimeout(1000);

    const after = await logsContainer(page).evaluate((el) => el.scrollTop);
    expect(after, 'the pane scrolled away from text the user had highlighted').toBe(before);
  });

  test('there is a way to copy the output without hand-selecting it', async ({ page, context }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await page.getByTestId('copy-logs-btn').click();
    await expect(page.getByTestId('copy-message')).toContainText('Copied');

    // The Windows clipboard hands text back with CRLF line endings, so this
    // compares line content rather than exact bytes.
    const clipboard = await page.evaluate(() => navigator.clipboard.readText());
    expect(clipboard.replace(/\r\n/g, '\n')).toBe(MOCK_LOGS.join('\n'));
  });
});

// ==========================================================================
// "outputs weren't saved" - reload, navigation, and the run behind them
// ==========================================================================

test.describe('a run outlives the page that started it', () => {
  test('navigating away does NOT cancel the run', async ({ page }) => {
    const seen: string[] = [];
    await mockApi(page, { seen });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);

    await page.getByRole('link', { name: /Board/ }).click();
    await expect(page.getByTestId('playground-page')).toHaveCount(0);
    await page.waitForTimeout(300);

    expect(
      seen.filter((p) => p.endsWith('/cancel')),
      'leaving the page killed the agent container',
    ).toEqual([]);
  });

  test('coming back to a running test shows it still running, with Cancel', async ({ page }) => {
    await mockApi(page, {
      sessionStatus: {
        session_id: SESSION_ID,
        status: 'running',
        started_at: naiveUtc(-3000),
        completed_at: null,
        source: 'session',
      },
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);

    await page.getByRole('link', { name: /Board/ }).click();
    await expect(page.getByTestId('playground-page')).toHaveCount(0);
    await page.getByRole('link', { name: /Playground/ }).click();

    await expect(logsContainer(page)).toContainText('Mock execution complete.');
    await expect(page.getByTestId('cancel-btn')).toBeVisible();
  });

  test('a reload mid-run reattaches, so the run can still be cancelled', async ({ page }) => {
    await mockApi(page, {
      sessionStatus: {
        session_id: SESSION_ID,
        status: 'running',
        started_at: naiveUtc(-3000),
        completed_at: null,
        source: 'session',
      },
    });
    await openPlayground(page);
    await startRun(page);

    const streamsBefore = await page.evaluate(() => window.__sse.count());
    await page.reload();
    await ensureRepoSelected(page);

    // The session id came back from sessionStorage and the stream re-opened:
    // before this, F5 orphaned a live agent container that nobody could stop.
    await expect
      .poll(() => page.evaluate(() => window.__sse.count()))
      .toBeGreaterThanOrEqual(1);
    await expect(page.getByTestId('cancel-btn')).toBeVisible();
    expect(streamsBefore).toBeGreaterThan(0);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await expect(logsContainer(page)).toContainText('Mock execution complete.');
  });

  test("a reload after the run finished restores the run's transcript", async ({ page }) => {
    await mockApi(page, {
      sessionStatus: {
        session_id: SESSION_ID,
        status: 'completed',
        started_at: naiveUtc(-6000),
        completed_at: naiveUtc(-4000),
        source: 'run',
      },
      result: RESULT_FROM_RUN,
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(logsContainer(page)).toContainText('Mock execution complete.');

    await page.reload();
    await ensureRepoSelected(page);

    await expect(
      logsContainer(page),
      'the transcript the user just watched is gone after a reload',
    ).toContainText('Mock execution complete.');
  });

  test('a status the client does not recognise is refused, not adopted', async ({ page }) => {
    // R1. Writing an unknown status into the store renders a page with no
    // badge, no buttons and no explanation - which looks like a broken app
    // rather than like a backend that answered something unexpected.
    await mockApi(page, {
      sessionStatus: {
        session_id: SESSION_ID,
        status: 'quantum-superposition',
        started_at: null,
        completed_at: null,
        source: 'session',
      },
    });
    await openPlayground(page);
    await startRun(page);
    await page.reload();
    await ensureRepoSelected(page);

    await expect(page.getByTestId('playground-status')).toHaveCount(0);
    await expect(logsContainer(page)).toContainText('will appear here');
    expect(
      await page.evaluate(() => sessionStorage.getItem('lazyaf.playground.sessionId')),
      'a session with an unusable status was kept to be retried forever',
    ).toBeNull();
  });

  test('a reconnect does not duplicate the transcript', async ({ page }) => {
    // The server replays the whole buffer on every connect, including the
    // automatic reconnect EventSource performs after a dropped connection.
    // Appending it gave two reconnects three copies of the run.
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await expect(logsContainer(page).locator('.log-line')).toHaveCount(MOCK_LOGS.length);

    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.waitForTimeout(300);

    await expect(logsContainer(page).locator('.log-line')).toHaveCount(MOCK_LOGS.length);
  });
});

// ==========================================================================
// History
// ==========================================================================

test.describe('history', () => {
  test('a repo with no runs says so instead of showing nothing', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await expect(page.getByTestId('playground-history')).toContainText('No runs yet');
  });

  test('the transcript on screen says which prompt produced it', async ({ page }) => {
    // "You cannot see what you asked five minutes ago" was half the history
    // complaint: the prompt appeared nowhere once the run had finished.
    await mockApi(page, { history: [HISTORY_ROW, OLDER_HISTORY_ROW] });
    await openPlayground(page);
    await startRun(page, 'summarise the test suite');
    await expect(page.getByTestId('shown-prompt')).toContainText('summarise the test suite');

    await page.getByTestId('history-item').nth(1).click();
    await expect(logsContainer(page)).toContainText('older run line one');
    await expect(page.getByTestId('shown-prompt')).toContainText('what does this repo do?');
  });

  test('a past run is listed with its prompt and can be reopened', async ({ page }) => {
    await mockApi(page, { history: [HISTORY_ROW, OLDER_HISTORY_ROW] });
    await openPlayground(page);

    const items = page.getByTestId('history-item');
    await expect(items).toHaveCount(2);
    await expect(items.first()).toContainText('say hello');
    await expect(items.nth(1)).toContainText('what does this repo do?');

    await items.nth(1).click();
    await expect(logsContainer(page)).toContainText('older run line one');
  });

  test('a history load that failed says so rather than showing an empty list', async ({ page }) => {
    await mockApi(page);
    // Registered AFTER mockApi on purpose: Playwright matches routes in
    // reverse registration order, so this has to be the later handler to win.
    await page.route(
      (url) => url.pathname.endsWith('/playground/sessions'),
      (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'history table is on fire' }) }),
    );
    await openPlayground(page);

    await expect(page.getByTestId('history-error')).toContainText('history table is on fire');
  });

  test('a run reopened from the durable record says its diff was not retained', async ({ page }) => {
    // R1: `diff: null` from a run record is NOT "the agent changed nothing".
    // The playground branch is deleted once its diff has been computed, so
    // the two cases must not render the same sentence.
    await mockApi(page, { history: [OLDER_HISTORY_ROW] });
    await openPlayground(page);
    await page.getByTestId('history-item').first().click();

    await expect(page.getByTestId('diff-not-retained')).toBeVisible();
    await expect(page.getByTestId('diff-empty')).toHaveCount(0);
  });
});

// ==========================================================================
// Terminal states: a run that finished must LOOK finished
// ==========================================================================

test.describe('a run that reached the end says so', () => {
  test('a run that changed nothing still reports a result', async ({ page }) => {
    await mockApi(page, { result: RESULT_NO_CHANGES });
    await openPlayground(page);
    await startRun(page, 'what does this repo do?');
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));

    await expect(page.getByTestId('diff-empty')).toContainText('No changes were made');
    await expect(page.getByTestId('reset-btn')).toBeVisible();
  });

  test('a failure with no output is shown in the output pane, not buried in the sidebar', async ({ page }) => {
    await mockApi(page, {
      result: {
        ...RESULT,
        status: 'failed',
        diff: null,
        files_changed: [],
        logs: '',
        error:
          "agent step 'Playground agent' needs ANTHROPIC_API_KEY to run the 'claude-code' CLI, but no key is configured - set ANTHROPIC_API_KEY in the backend's environment",
      },
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('complete', 'failed'));

    await expect(page.getByTestId('playground-error')).toContainText('ANTHROPIC_API_KEY');
    // The pane must stop claiming that output "will appear here when you run
    // a test" about a run that has already been and gone.
    await expect(logsContainer(page)).not.toContainText('will appear here');
    await expect(logsContainer(page)).toContainText('produced no output');
  });

  test('the header duration reflects how long the run took', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    // It used to read "(0s)" forever: a no-argument function call in the
    // template gave Svelte nothing to invalidate on.
    await expect.poll(async () => (await statusBadge(page).innerText()).includes('0s'), {
      timeout: 6000,
    }).toBe(false);

    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    // 1.585683s as the server measured it.
    await expect(statusBadge(page)).toContainText('2s');
  });
});

// ==========================================================================
// Cancel
// ==========================================================================

test.describe('cancel', () => {
  test('a cancel the server REFUSED is not reported as success', async ({ page }) => {
    // The backend answers 503 and restores the session to `running` for a
    // reason: the container is what spends money, and "cancelled" for a
    // container that is still working is the worst possible lie to tell.
    await mockApi(page, {
      cancelStatus: 503,
      cancelBody: {
        detail:
          'could not cancel the agent run behind session 11d4156d (docker refused); the container may still be running',
      },
    });
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);

    await page.getByTestId('cancel-btn').click();

    await expect(page.getByTestId('playground-error')).toContainText('may still be running');
    await expect(statusBadge(page)).not.toContainText('Cancelled');
    await expect(
      page.getByTestId('cancel-btn'),
      'the refused cancel could not be retried',
    ).toBeVisible();
  });

  test('a cancel the server accepted lands the run as cancelled', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.getByTestId('cancel-btn').click();

    await expect(statusBadge(page)).toContainText('Cancelled');
    await expect(page.getByTestId('playground-error')).toHaveCount(0);
  });
});

// ==========================================================================
// Refusing at the edge
// ==========================================================================

test.describe('the run button refuses what the backend would refuse', () => {
  test('a whitespace-only prompt cannot start a run', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);

    await page.getByTestId('task-input').fill('   ');
    await expect(page.getByTestId('start-test-btn')).toBeDisabled();

    await page.getByTestId('task-input').fill('do something');
    await expect(page.getByTestId('start-test-btn')).toBeEnabled();
  });

  test('a runner with no model selected cannot start a run', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await page.getByTestId('task-input').fill('do something');
    await expect(page.getByTestId('start-test-btn')).toBeEnabled();

    // The self-hosted runner has no CLI model list; switching to it clears the
    // model rather than leaving a Claude id selected in a Gemini-shaped list.
    await page.locator('#runner-type').selectOption('openai-harness');

    await expect(page.getByTestId('model-select')).toHaveValue('');
    await expect(
      page.getByTestId('start-test-btn'),
      'a run was launchable with no model chosen',
    ).toBeDisabled();
  });
});

// ==========================================================================
// Rendering
// ==========================================================================

test.describe('rendering', () => {
  test('ANSI colour codes are stripped, not printed as mojibake', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);

    await page.evaluate(() => {
      const esc = String.fromCharCode(27);
      window.__sse.emit('log', `${esc}[32mPASS${esc}[0m tests/test_thing.py`);
    });

    const line = logsContainer(page).locator('.log-line').first();
    await expect(line).toHaveText('PASS tests/test_thing.py');
  });

  test('the selected model name is readable, not clipped mid-word', async ({ page }) => {
    // The control used to overflow the 320px panel, so the label read
    // "Claude Sonnet" of "Claude Sonnet 4.5" - you could not tell which model
    // you had picked. Both halves matter: the box must fit the panel, AND the
    // text must fit the box.
    await mockApi(page);
    await openPlayground(page);

    const fit = await page.getByTestId('model-select').evaluate((el) => {
      const select = el as HTMLSelectElement;
      const panel = document.querySelector('[data-testid="config-panel"]') as HTMLElement;
      const style = getComputedStyle(select);
      const probe = document.createElement('span');
      probe.style.cssText = `position:absolute;visibility:hidden;white-space:nowrap;font:${style.font}`;
      probe.textContent = select.options[select.selectedIndex].text;
      document.body.appendChild(probe);
      const textWidth = probe.getBoundingClientRect().width;
      probe.remove();
      return {
        panelOverflow: select.getBoundingClientRect().right - panel.getBoundingClientRect().right,
        room:
          select.clientWidth -
          parseFloat(style.paddingLeft) -
          parseFloat(style.paddingRight),
        textWidth,
      };
    });

    expect(fit.panelOverflow, 'the Model select overflows the configuration panel').toBeLessThanOrEqual(0);
    expect(fit.room, 'the selected model name is clipped inside its own select').toBeGreaterThanOrEqual(fit.textWidth);
  });

  test('the transcript gets at least as much height as the diff', async ({ page }) => {
    await mockApi(page);
    await openPlayground(page);
    await startRun(page);
    await page.evaluate((lines) => window.__sse.emit('logs_batch', lines), MOCK_LOGS);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(page.getByTestId('diff-section')).toBeVisible();

    const heights = await page.evaluate(() => {
      const logs = document.querySelector('.logs-section') as HTMLElement;
      const diff = document.querySelector('.diff-section') as HTMLElement;
      return { logs: logs.getBoundingClientRect().height, diff: diff.getBoundingClientRect().height };
    });
    expect(
      heights.logs,
      'on a page called a playground, the agent output got the smaller box',
    ).toBeGreaterThanOrEqual(heights.diff);
  });
});

// ==========================================================================
// Modalities: what the chosen model can be GIVEN, and what a human may
// attach here (14.5)
// ==========================================================================
//
// THE FAILURE THESE TESTS DEFEND AGAINST does not look like a failure: a file
// accepted and never delivered, producing a right-looking answer from a prompt
// that silently lost half its input. So every assertion below is either "this
// control is disabled" or "the reason it is disabled is the RIGHT one" -
// because a control disabled for the wrong reason sends a human to fix
// something that is not broken.
//
// The endpoint rows here are byte-faithful to `endpoint_read`, and the states
// are driven from the fixture rather than from a probe: the six-state chip
// RENDERING is `endpoints.spec.ts`'s subject, and what this file owns is how
// the Playground consumes it.

function modality(
  name: string,
  state: string,
  extra: Record<string, string | null> = {},
) {
  return { modality: name, state, source: null, reason: null, evidence: null, caveat: null, ...extra };
}

/** One registry row in the shape `endpoint_read` puts on the wire. */
function endpointFixture(name: string, modalities: ReturnType<typeof modality>[] | null) {
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

const TEXT_OK = modality('text', 'supported', { source: 'wire_format' });
const VIDEO_NEVER = modality('video', 'unrepresentable', {
  source: 'wire_format',
  reason: 'wire_format_has_no_video_content_part',
});

/** One endpoint per images-state, so each can be selected in turn. */
const MODALITY_ENDPOINTS = [
  endpointFixture('sees', [
    TEXT_OK,
    modality('images', 'supported', { source: 'wire_probe', reason: 'usage_delta_positive' }),
    modality('audio', 'unsupported', { source: 'wire_probe', reason: 'http_400' }),
    VIDEO_NEVER,
  ]),
  endpointFixture('refuses', [
    TEXT_OK,
    modality('images', 'unsupported', {
      source: 'wire_probe',
      reason: 'http_400',
      evidence: 'This model does not support image input',
    }),
    modality('audio', 'unsupported', { source: 'wire_probe', reason: 'http_400' }),
    VIDEO_NEVER,
  ]),
  endpointFixture('never-asked', [
    TEXT_OK,
    modality('images', 'unprobed'),
    modality('audio', 'unprobed'),
    VIDEO_NEVER,
  ]),
  endpointFixture('drops-silently', [
    TEXT_OK,
    modality('images', 'undetectable', { source: 'wire_probe', reason: 'no_usage_delta' }),
    modality('audio', 'unprobed'),
    VIDEO_NEVER,
  ]),
  // A backend one version behind: it answered, and its answer has no
  // `modalities` list at all. That is a FOURTH unknown, not "unprobed".
  endpointFixture('old-backend', null),
];

/** Byte-faithful to `GET /api/playground/capabilities` in this build. */
const PLAYGROUND_CAPABILITIES = {
  attachment_limits: {
    max_files: 4,
    max_bytes_per_file: 5 * 1024 * 1024,
    max_bytes_total: 8 * 1024 * 1024,
    media_types: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'],
  },
  modalities: [
    {
      modality: 'images',
      attachable: false,
      reason:
        'LazyAF cannot yet deliver an attachment to a model. The harness transcript types every message content as a string.',
    },
    { modality: 'audio', attachable: false, reason: 'Detected, deliberately not offered.' },
    {
      modality: 'video',
      attachable: false,
      reason: 'The OpenAI chat-completions wire format has no video content part.',
    },
  ],
};

/** Point the page at one self-hosted endpoint and wait for the panel. */
async function selectEndpoint(page: Page, name: string) {
  await page.locator('#runner-type').selectOption('openai-harness');
  await page.locator('#model').selectOption(`endpoint:${name}`);
  await expect(page.getByTestId('playground-modalities')).toBeVisible();
}

const attachBtn = (page: Page) => page.getByTestId('attach-images-btn');

test.describe('the playground says what the chosen model can be given', () => {
  test('a self-hosted endpoint gets the SHARED capability panel, not a copy', async ({ page }) => {
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    // `endpoint-capabilities-panel` is CapabilityChecks' own testid. Asserting
    // it here is what proves the Playground renders the one component rather
    // than a second, drifting copy of the six states (R3).
    await expect(page.getByTestId('endpoint-capabilities-panel')).toBeVisible();

    await expect(page.getByTestId('endpoint-cap-images')).toHaveAttribute('data-state', 'supported');
    await expect(page.getByTestId('endpoint-cap-audio')).toHaveAttribute('data-state', 'unsupported');
    await expect(page.getByTestId('endpoint-cap-video')).toHaveAttribute(
      'data-state',
      'unrepresentable',
    );
  });

  test('video says the WIRE FORMAT cannot carry it, not that the model cannot', async ({ page }) => {
    // A chip that is grey forever for an unstated reason is worse than no
    // chip. This is the sentence that stops that.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    const video = page.getByTestId('endpoint-cap-video');
    await expect(video).toBeVisible();
    await expect(video).toContainText(/wire format|content part/i);
  });

  test('a CLI runner gets an explanation rather than an empty strip', async ({ page }) => {
    // An empty capability strip next to a green one reads as "Claude cannot
    // see images", which is false: Claude Code is a CLI agent and its inputs
    // are not a property of anything LazyAF probed.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);

    await expect(page.getByTestId('modality-cli-note')).toBeVisible();
    await expect(page.getByTestId('modality-cli-note')).toContainText('CLI agent');
    await expect(page.getByTestId('endpoint-capabilities-panel')).toHaveCount(0);
  });

  test('choosing the harness with no endpoint yet says nothing has been asked', async ({ page }) => {
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await page.locator('#runner-type').selectOption('openai-harness');

    await expect(page.getByTestId('modality-no-endpoint')).toBeVisible();
    await expect(page.getByTestId('endpoint-capabilities-panel')).toHaveCount(0);
  });
});

test.describe('the attach control is disabled for the RIGHT reason', () => {
  test('never probed: disabled, blamed on the endpoint, and it says what to do', async ({ page }) => {
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'never-asked');

    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-blocked-by', 'endpoint');
    await expect(attachBtn(page)).toHaveAttribute('data-modality-state', 'unprobed');
    // `unprobed` is the one state with a verb attached to it.
    await expect(page.getByTestId('attach-next')).toBeVisible();
  });

  test('probed and refused: disabled, quoting what the server actually said', async ({ page }) => {
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'refuses');

    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-modality-state', 'unsupported');
    await expect(page.getByTestId('attach-reason')).toContainText(
      'This model does not support image input',
    );
  });

  test('undetectable does NOT read like never-probed', async ({ page }) => {
    // THE COLLAPSE THAT WOULD BE A LIE. `unprobed` means nobody asked;
    // `undetectable` means the server took the image, returned 200, and the
    // prompt token count did not move - the request SUCCEEDS and the input
    // vanishes. They disable the same control and call for different actions.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);

    await selectEndpoint(page, 'never-asked');
    const unprobedReason = await page.getByTestId('attach-reason').innerText();

    await page.locator('#model').selectOption('endpoint:drops-silently');
    await expect(attachBtn(page)).toHaveAttribute('data-modality-state', 'undetectable');
    const undetectableReason = await page.getByTestId('attach-reason').innerText();

    expect(undetectableReason).not.toBe(unprobedReason);
  });

  test('a backend with no modality list is not reported as "not supported"', async ({ page }) => {
    // A FOURTH kind of unknown. Probing cannot fix it, so it must not offer
    // Probe - that would send an operator round a loop that never terminates.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'old-backend');

    await expect(page.getByTestId('endpoint-modalities-unreported')).toBeVisible();
    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-blocked-by', 'unreported');
    await expect(page.getByTestId('attach-next')).toHaveCount(0);
  });

  test('an endpoint that CAN see is still refused while LazyAF cannot carry it', async ({ page }) => {
    // Two different facts, and both have to be true. Collapsing them would
    // make "your endpoint cannot see" and "LazyAF cannot send" one sentence
    // when they call for opposite actions.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-blocked-by', 'platform');
    await expect(attachBtn(page)).toHaveAttribute('data-modality-state', 'supported');
    await expect(page.getByTestId('attach-reason')).toContainText('cannot yet deliver');
  });

  test('a CLI runner does not blame the endpoint for the CLI', async ({ page }) => {
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS });
    await openPlayground(page);

    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-blocked-by', 'runner');
  });
});

test.describe('the attach limits are the server’s numbers', () => {
  test('the stated caps come from GET /api/playground/capabilities', async ({ page }) => {
    // R3: a "max 5 MiB" typed into the template beside a `5 * 1024 * 1024` in
    // the validator is two sources of truth, and the half that drifts is
    // always the sentence. Serving DIFFERENT numbers proves the page is
    // reading them rather than reciting its own.
    await mockApi(page, {
      endpoints: MODALITY_ENDPOINTS,
      capabilities: {
        ...PLAYGROUND_CAPABILITIES,
        attachment_limits: {
          max_files: 2,
          max_bytes_per_file: 1024 * 1024,
          max_bytes_total: 2 * 1024 * 1024,
          media_types: ['image/png'],
        },
      },
    });
    await openPlayground(page);

    const limits = page.getByTestId('attach-limits');
    await expect(limits).toContainText('2 files');
    await expect(limits).toContainText('1 MiB');
    await expect(limits).toContainText('2 MiB');
    await expect(limits).toContainText('PNG');
    await expect(limits).not.toContainText('5 MiB');
  });

  test('a failed capability read disables attach and SAYS the read failed', async ({ page }) => {
    // "We could not ask" is not "yes". An optimistic default here is exactly
    // how a file gets accepted that nothing can carry.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS, capabilitiesStatus: 500 });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    await expect(page.getByTestId('attach-capabilities-error')).toBeVisible();
    await expect(attachBtn(page)).toBeDisabled();
    await expect(attachBtn(page)).toHaveAttribute('data-blocked-by', 'platform');
    await expect(page.getByTestId('attach-limits')).toContainText(/unknown/i);
  });

  test('a 200 carrying the wrong shape is refused, not rendered', async ({ page }) => {
    // The catch-all `[]` a dev proxy or a stale backend can answer with. It is
    // JSON and it is not this; storing it would put `undefined` where the
    // template reads `.modalities`, and a page that throws mid-render leaves
    // the attach control with no state at all.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS, capabilities: [] });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    await expect(page.getByTestId('attach-capabilities-error')).toBeVisible();
    await expect(attachBtn(page)).toBeDisabled();
    // The panel itself still rendered - a bad capability read must not take
    // the modality display down with it.
    await expect(page.getByTestId('endpoint-capabilities-panel')).toBeVisible();
  });
});

test.describe('the modality panel does not disturb what already worked', () => {
  test('the pane still follows, still yields, and history still lists, with the panel on screen', async ({
    page,
  }) => {
    // The regression guard. The panel sits in the same scrolling config
    // column as the history list and re-renders on every endpoint store
    // update; none of that may touch the output pane's scroll contract.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS, history: [HISTORY_ROW] });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');

    await startRun(page);
    await fillAndSettle(page, 200);

    // Following still works with the panel present.
    const followed = await metrics(page);
    expect(Math.abs(followed.scrollTop + followed.clientHeight - followed.scrollHeight)).toBeLessThanOrEqual(2);

    // And it still YIELDS to a user who scrolled up.
    await logsContainer(page).evaluate((el) => {
      el.scrollTop = Math.floor(el.scrollHeight * 0.3);
    });
    await page.waitForTimeout(120);
    const parked = await metrics(page);
    await page.evaluate(() => window.__sse.burst(40, 6, 'after-panel'));
    await page.waitForTimeout(1200);
    expect((await metrics(page)).scrollTop, 'the panel re-render moved the pane').toBe(
      parked.scrollTop,
    );

    await expect(page.getByTestId('history-item')).toHaveCount(1);
  });

  test('a live selection in the pane survives the panel re-rendering', async ({ page }) => {
    // The panel is reactive on `$endpointsStore` and on the capability read,
    // both of which land after mount. A re-render that reconciled the log
    // block would destroy a standing selection - the exact bug this page was
    // reported for.
    //
    // The run is LANDED first, deliberately. The model select is disabled
    // while a run is live (it always has been - you may not swap the model out
    // from under a running container), so the endpoint switch this test needs
    // is only reachable once the run is over. That also removes the autoscroll
    // from the picture, leaving the panel re-render as the only thing that
    // could destroy the selection.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS, result: RESULT });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');
    await startRun(page);
    await fillAndSettle(page, 120);

    await page.evaluate(() => window.__sse.emit('complete', 'completed'));
    await expect(page.locator('#model')).toBeEnabled();

    await logsContainer(page).evaluate((el) => {
      const lines = el.querySelectorAll('.log-line');
      const range = document.createRange();
      range.setStart(lines[2], 0);
      range.setEnd(lines[4], lines[4].childNodes.length);
      const selection = document.getSelection();
      selection?.removeAllRanges();
      selection?.addRange(range);
    });
    const before = await page.evaluate(() => document.getSelection()?.toString().length ?? 0);
    expect(before).toBeGreaterThan(0);

    // Switching endpoint re-renders the whole panel.
    await page.locator('#model').selectOption('endpoint:refuses');
    await expect(attachBtn(page)).toHaveAttribute('data-modality-state', 'unsupported');

    const after = await page.evaluate(() => document.getSelection()?.toString().length ?? 0);
    expect(after, 'the panel re-render destroyed the selection').toBe(before);
  });

  test('Reset keeps the attach limits it already read', async ({ page }) => {
    // They are a property of the BUILD, not of the run. Dropping them on Reset
    // would leave the control briefly unable to state its own limits.
    await mockApi(page, { endpoints: MODALITY_ENDPOINTS, result: RESULT });
    await openPlayground(page);
    await selectEndpoint(page, 'sees');
    await startRun(page);
    await page.evaluate(() => window.__sse.emit('complete', 'completed'));

    await page.getByTestId('reset-btn').click();
    await expect(page.getByTestId('attach-limits')).toContainText('5 MiB');
    await expect(page.getByTestId('attach-capabilities-error')).toHaveCount(0);
  });
});
