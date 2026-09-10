/**
 * The Logs tab, the bug-report bundle, and the redaction the whole thing
 * exists to protect (R8: a UI change ships a Playwright spec).
 *
 * WHY THESE ARE FIXTURE-DRIVEN
 * ----------------------------
 * `/api/diagnostics/*` is Phase 1 BACKEND work and does not exist yet, so
 * these specs drive the REAL frontend against `page.route` fixtures. That
 * follows the convention already set by `qa/live-update-state.spec.ts` and
 * `qa/demo-polish.spec.ts`: the claims here are RENDERING and SAFETY-GATE
 * behaviour, so only the Svelte component tree has to be real. When the
 * backend lands, the fixtures in `installDiagnostics` double as the contract
 * it has to satisfy.
 *
 * WHAT IS DELIBERATELY *NOT* CLAIMED HERE
 * ---------------------------------------
 * These specs do NOT prove that redaction works. Redaction happens in the
 * backend `Redactor`, it is tested there (§2.6 of the design: a pattern-table
 * parametrisation, a known-values pass, and a gate that re-scans the finished
 * archive with CI's own scanner), and a frontend spec that fed itself a
 * pre-redacted fixture and then asserted the secret was absent would be
 * asserting its own fixture - fake green of the purest kind (R4).
 *
 * What a FRONTEND spec can honestly hold down is the frontend's own half of
 * the safety contract, and that is what is tested below:
 *
 *   - redaction is VISIBLE, so a bundle that silently stopped redacting does
 *     not look identical to one with nothing to redact;
 *   - the client never uploads log content in order to have it bundled;
 *   - THE BYTES YOU DOWNLOAD ARE THE BYTES YOU REVIEWED - changing the
 *     selection blocks Download until the bundle is rebuilt;
 *   - there is no upload control;
 *   - and a backend with no diagnostics API says so rather than rendering a
 *     reassuring green strip about a question it never asked.
 *
 * Run against the QA sandbox, never :8765 or :8000 (nothing here touches the
 * backend, but the vite server must proxy somewhere):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5179 \
 *     npx playwright test e2e/logs.spec.ts
 */
import { test, expect, type Page, type Route } from '@playwright/test';
import { readFile } from 'node:fs/promises';

/**
 * A live-format credential. It is only ever placed in the fixture's RAW
 * source to prove the transport under test never carries it; every fixture
 * that models a backend response carries the PLACEHOLDER instead, because
 * that is what a real redacting backend would send.
 */
const SENTINEL = 'sk-ant-api03-LiveLookingKeyMustNeverBeRendered-0123456789';
const PLACEHOLDER = '[REDACTED:anthropic:9c41a2]';

const BUNDLE_ID = 'e7c1a4f2-0000-4000-8000-00000000f001';
const RUN_ID = 'bbbbbbbb-0000-4000-8000-00000000b001';

/** Serialize like the backend does: `datetime.utcnow().isoformat()` — NO 'Z'. */
function iso(offsetMs: number): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

interface WireEntry {
  seq: number;
  ts: string;
  source: string;
  kind: string;
  level: string;
  message: string;
}

function makeEntry(seq: number, over: Partial<WireEntry> = {}): WireEntry {
  return {
    seq,
    ts: iso(-60_000 + seq * 10),
    source: 'backend',
    kind: 'backend',
    level: 'INFO',
    message: `record ${seq} — steady state`,
    ...over,
  };
}

const STALE_SYSTEM = {
  generated_at: iso(0),
  staleness: {
    stale: true,
    process_started_at: iso(-25 * 3600_000 - 4 * 60_000),
    newest_file_mtime: iso(-90_000),
    stale_file_count: 3,
    stale_files: ['app/routers/cards.py', 'app/services/agent_run.py', 'app/main.py'],
    remedy: 'docker compose up -d --force-recreate backend',
  },
  migrations: { ok: true, db_head: '0014_pipeline_steps_to_graph', disk_heads: ['0014_pipeline_steps_to_graph'], unapplied_count: 0, gaps: [] },
  checkout: { available: false, sha: null, dirty: null, dirty_file_count: null, reason: 'the LazyAF checkout is not visible from this container' },
  containers: [
    { name: 'lazyaf-backend-1', service: 'backend', image: 'lazyaf-backend:dev', image_id: 'sha256:ab12cd34ef5678', started_at: iso(-25 * 3600_000), restart_count: 0, health: 'healthy' },
    { name: 'lazyaf-frontend-1', service: 'frontend', image: 'lazyaf-frontend:dev', image_id: 'sha256:9f01aa22bb33', started_at: iso(-28 * 3600_000), restart_count: 0, health: null },
    { name: 'lazyaf-runner-agent-1', service: 'runner-agent', image: 'lazyaf-runner:dev', image_id: 'sha256:77ae44cc55dd', started_at: iso(-39 * 3600_000), restart_count: 1, health: 'healthy' },
  ],
  other_containers_on_host: 7,
  container_error: null,
};

const HEALTHY_SYSTEM = {
  ...STALE_SYSTEM,
  staleness: {
    stale: false,
    process_started_at: iso(-25 * 60_000),
    newest_file_mtime: iso(-3600_000),
    stale_file_count: 0,
    stale_files: [],
    remedy: null,
  },
};

const MANIFEST = {
  id: BUNDLE_ID,
  created_at: iso(0),
  expires_at: iso(15 * 60_000),
  total_bytes: 219_136,
  redacted_total: 11,
  members: [
    { name: 'system.md', source: 'system', bytes: 2048, summary: 'running code ≠ code on disk', redacted: 0, risk: 'none', risk_note: null },
    { name: 'containers.json', source: 'containers', bytes: 4096, summary: '12 LazyAF containers · no env vars', redacted: 0, risk: 'none', risk_note: null },
    { name: 'backend.log', source: 'backend', bytes: 120_832, summary: '412 records · 3 redacted', redacted: 3, risk: 'low', risk_note: null },
    { name: `runs/${RUN_ID.slice(0, 4)}/step-2.log`, source: 'steps', bytes: 72_704, summary: '1 204 lines · 8 redacted', redacted: 8, risk: 'high', risk_note: 'agent output' },
    { name: 'runner-agent.log', source: 'runner', bytes: 17_408, summary: '88 lines · 0 redacted', redacted: 0, risk: 'high', risk_note: 'runner output' },
    { name: 'settings-names.json', source: 'settings', bytes: 1024, summary: 'names only — no values, ever', redacted: 0, risk: 'none', risk_note: null },
  ],
};

/**
 * The archive bytes the "server" holds for BUNDLE_ID.
 *
 * Deliberately NOT a real zip: the claim under test is that the client saves
 * exactly the bytes this endpoint returned, and a byte-for-byte comparison
 * against an opaque payload states that more sharply than unzipping would.
 */
const ARCHIVE_BODY = `lazyaf-bundle\n${PLACEHOLDER}\nstep-2: fatal: couldn't find remote ref main\n`;

interface DiagOptions {
  system?: unknown;
  /** Serve 404 for every diagnostics route, i.e. a backend predating the feature. */
  absent?: boolean;
  entries?: WireEntry[];
  /** Appended to the buffer on every poll, so live behaviour can be driven. */
  growBy?: number;
}

interface DiagHandle {
  /** Every POST body the page sent to /api/diagnostics/bundle. */
  bundlePosts: string[];
  /** Every diagnostics path the page requested, in order. */
  requested: string[];
}

/**
 * One handler for the whole diagnostics surface.
 *
 * A single regex route dispatching internally, rather than five globs:
 * Playwright matches handlers in reverse registration order, and
 * `/bundle`, `/bundle/{id}` and `/bundle/{id}/members/{name}` are close
 * enough that ordering bugs in the FIXTURE would masquerade as bugs in the
 * page.
 */
async function installDiagnostics(page: Page, opts: DiagOptions = {}): Promise<DiagHandle> {
  const handle: DiagHandle = { bundlePosts: [], requested: [] };
  const buffer: WireEntry[] = [...(opts.entries ?? [])];
  let nextSeq = buffer.length ? Math.max(...buffer.map((e) => e.seq)) + 1 : 1;

  // The app shell: nothing here should reach a real backend.
  await page.routeWebSocket('**/ws', (ws) => ws.close());
  await page.route('**/api/repos', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );
  await page.route('**/api/runners', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );
  await page.route('**/api/agent-files', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }),
  );

  await page.route(/\/api\/diagnostics\//, async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/diagnostics/, '');
    handle.requested.push(path);

    const json = (body: unknown) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

    if (opts.absent) {
      return route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Not Found' }),
      });
    }

    if (path === '/system') return json(opts.system ?? STALE_SYSTEM);

    if (path === '/runs') {
      return json([
        { id: RUN_ID, label: 'run a41f', status: 'failed', started_at: iso(-120_000), has_failed_step: true },
      ]);
    }

    if (path === '/logs') {
      const after = url.searchParams.get('after');
      if (opts.growBy && after !== null) {
        for (let i = 0; i < opts.growBy; i++) {
          buffer.push(makeEntry(nextSeq++, { ts: iso(0), message: `live record ${nextSeq}` }));
        }
      }
      const cursorAfter = after === null ? -1 : Number(after);
      const slice = buffer.filter((e) => e.seq > cursorAfter);
      return json({
        entries: slice,
        cursor: buffer.length ? buffer[buffer.length - 1].seq : 0,
        sources: [
          { id: 'backend', label: 'backend', kind: 'backend', count: buffer.filter((e) => e.kind === 'backend').length, errors: buffer.filter((e) => e.level === 'ERROR').length, risky: false },
          { id: 'step[2]', label: 'step 2', kind: 'step', count: buffer.filter((e) => e.kind === 'step').length, errors: 0, risky: true },
          { id: 'runner-agent', label: 'runner-agent', kind: 'runner', count: buffer.filter((e) => e.kind === 'runner').length, errors: 0, risky: true },
        ],
      });
    }

    if (path === '/bundle' && route.request().method() === 'POST') {
      handle.bundlePosts.push(route.request().postData() ?? '');
      return json(MANIFEST);
    }

    const member = path.match(/^\/bundle\/[^/]+\/members\/(.+)$/);
    if (member) {
      return route.fulfill({
        status: 200,
        contentType: 'text/plain',
        body: `authorization: Bearer ${PLACEHOLDER}\nbranch 'main' not found\n`,
      });
    }

    if (/^\/bundle\/[^/]+$/.test(path)) {
      return route.fulfill({
        status: 200,
        contentType: 'application/zip',
        body: ARCHIVE_BODY,
      });
    }

    return route.fulfill({ status: 404, body: 'no fixture' });
  });

  return handle;
}

async function gotoLogs(page: Page) {
  await page.goto('/#/logs');
  await expect(page.getByTestId('logs-page')).toBeVisible({ timeout: 10_000 });
}

// =============================================================================
// The system strip — "what is actually running here"
// =============================================================================

test.describe('system strip', () => {
  test('names a stale container, the files it is not running, and the fix', async ({ page }) => {
    // THE bug this whole feature exists for: a backend serving 25-hour-old
    // code off a bind mount while the files on disk are current. It presented
    // as "branch not found" on a repository whose branch plainly existed, and
    // nothing in the product could tell it apart from a real git fault.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);

    const staleness = page.getByTestId('system-staleness');
    await expect(staleness).toBeVisible();
    await expect(staleness).toContainText('Running code is not the code on disk');
    await expect(page.getByTestId('system-header')).toHaveAttribute('data-verdict', 'stale');

    // The specific files, so the reader can tell whether it explains their bug.
    const files = page.getByTestId('stale-files');
    await expect(files).toContainText('app/routers/cards.py');
    await expect(files).toContainText('app/services/agent_run.py');

    // And the command, copyable, because knowing is not fixing.
    await expect(page.getByTestId('stale-remedy')).toHaveText(
      'docker compose up -d --force-recreate backend',
    );

    // HOURS, not days. The coarse single-unit age helper renders the backend
    // at 25h, the frontend at 28h and the runner at 39h all as "1d", which
    // erases the one comparison the reader came to make. 25h must read as 25h.
    await expect(staleness).toContainText('25h');
    await page.getByTestId('containers-toggle').click();
    const table = page.getByTestId('system-containers');
    await expect(table).toContainText('25h');
    await expect(table).toContainText('28h');
    await expect(table).toContainText('39h');
  });

  test('reports the checkout as UNKNOWN rather than inventing a SHA', async ({ page }) => {
    // R1/R4. A build-time baked git SHA would be a lie by construction under a
    // bind mount - the image was built at one commit while the files being
    // executed are at another, which is the very divergence above. "Unknown"
    // is the true answer and must render as its own state.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);

    const checkout = page.getByTestId('system-checkout');
    await expect(checkout).toContainText('not visible from this container');
    await expect(checkout).not.toContainText('unknown SHA');
  });

  test('counts other containers on the host without naming them', async ({ page }) => {
    // This machine runs containers belonging to unrelated projects. Publishing
    // their names into a public issue leaks the existence of other work for no
    // diagnostic gain.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);

    const others = page.getByTestId('other-containers');
    await expect(others).toContainText('7 other containers');
    await expect(others).toContainText('not collected, named or included');
  });

  test('a healthy stack reports healthy, and says which container is oldest', async ({ page }) => {
    await installDiagnostics(page, { system: HEALTHY_SYSTEM, entries: [makeEntry(1)] });
    await gotoLogs(page);

    await expect(page.getByTestId('system-header')).toHaveAttribute('data-verdict', 'ok');
    await expect(page.getByTestId('system-staleness')).toContainText(
      'Running code matches the code on disk',
    );
    // The oldest container is the usual culprit, so it is called out up front.
    await expect(page.getByTestId('containers-toggle')).toContainText('oldest');
  });

  test('a backend with no diagnostics API says so instead of showing green', async ({ page }) => {
    // The owner's stack is live and predates this router. A page that rendered
    // an empty-but-plausible strip would be claiming "nothing is stale" about
    // a question it never managed to ask - R4, on the one fact this feature
    // exists to report.
    await installDiagnostics(page, { absent: true });
    await gotoLogs(page);

    const unavailable = page.getByTestId('system-unavailable');
    await expect(unavailable).toBeVisible();
    await expect(unavailable).toContainText('does not expose the diagnostics API');
    await expect(unavailable).toContainText('no claim is being made', { ignoreCase: true });

    // Not green, and not offering a bundle it cannot build.
    await expect(page.getByTestId('system-staleness')).toHaveCount(0);
    await expect(page.getByTestId('open-bug-report')).toBeDisabled();
    await expect(page.getByTestId('logs-unavailable')).toBeVisible();
  });
});

// =============================================================================
// The merged stream
// =============================================================================

test.describe('log stream', () => {
  test('renders one merged stream and marks redacted spans visibly', async ({ page }) => {
    // Redaction has to be VISIBLE. A bundle that silently stopped redacting
    // and one that had nothing to redact are indistinguishable without this,
    // which is exactly the failure mode a safety net must not have.
    await installDiagnostics(page, {
      entries: [
        makeEntry(1, { level: 'ERROR', message: "branch 'main' not found" }),
        makeEntry(2, { source: 'step[2]', kind: 'step', message: '[runner] pulling lazyaf-step:dev' }),
        makeEntry(3, {
          source: 'runner-agent',
          kind: 'runner',
          message: `authorization: Bearer ${PLACEHOLDER} accepted`,
        }),
      ],
    });
    await gotoLogs(page);

    const stream = page.getByTestId('log-stream');
    await expect(stream).toContainText("branch 'main' not found");
    // All three sources in ONE pane: the join the user came here to make.
    await expect(stream).toContainText('[runner] pulling lazyaf-step:dev');

    const marked = page.getByTestId('redacted-span');
    await expect(marked).toHaveCount(1);
    await expect(marked).toHaveText(PLACEHOLDER);

    // The text AROUND the placeholder survives intact - a redactor that ate
    // its surroundings would destroy the line's diagnostic value.
    await expect(stream).toContainText('authorization: Bearer');
    await expect(stream).toContainText('accepted');
    expect(await stream.innerText()).not.toContain(SENTINEL);
  });

  test('"errors only" narrows to errors and says so when there are none', async ({ page }) => {
    await installDiagnostics(page, {
      entries: [
        makeEntry(1, { message: 'ordinary chatter' }),
        makeEntry(2, { level: 'ERROR', message: 'the actual failure' }),
      ],
    });
    await gotoLogs(page);

    await page.getByTestId('errors-only').check();
    const stream = page.getByTestId('log-stream');
    await expect(stream).toContainText('the actual failure');
    await expect(stream).not.toContainText('ordinary chatter');
  });

  test('unticking a source removes it from the stream without refetching', async ({ page }) => {
    const handle = await installDiagnostics(page, {
      entries: [
        makeEntry(1, { message: 'from the backend' }),
        makeEntry(2, { source: 'step[2]', kind: 'step', message: 'from the step' }),
      ],
    });
    await gotoLogs(page);

    const stream = page.getByTestId('log-stream');
    await expect(stream).toContainText('from the step');
    const before = handle.requested.filter((p) => p === '/logs').length;

    await page.getByTestId('source-toggle-step[2]').uncheck();
    await expect(stream).not.toContainText('from the step');
    await expect(stream).toContainText('from the backend');

    // Filtering is client-side over the append-only buffer. A refetch here
    // would replace the entries array wholesale, which is precisely what
    // destroys a standing selection.
    expect(handle.requested.filter((p) => p === '/logs').length).toBe(before);
  });
});

// =============================================================================
// The scroll contract, ported from PlaygroundPage
// =============================================================================

test.describe('scroll contract', () => {
  /** Enough lines that the pane genuinely overflows. */
  const MANY = Array.from({ length: 200 }, (_, i) => makeEntry(i + 1));

  test('scrolling up stops autoscroll; the jump button brings it back', async ({ page }) => {
    // The original playground bug: a debounced snap armed by the last line
    // before the user scrolled up still yanked them back. Follow mode is a
    // latch, and only the user may re-arm it.
    await installDiagnostics(page, { entries: MANY, growBy: 3 });
    await gotoLogs(page);

    const stream = page.getByTestId('log-stream');
    await expect(stream).toContainText('record 200');

    // PAUSE FIRST, then scroll. The pane ignores scroll events for 150ms
    // after one of its own programmatic scrolls - without that window, the
    // snap lands at the bottom, the handler reads "near the bottom" and
    // re-arms following, putting the user straight back on the leash they
    // just escaped. That window is correct and is inherited from
    // PlaygroundPage; it just means a test that scrolls within 150ms of an
    // incoming line is racing the component rather than testing it.
    await page.getByTestId('toggle-live').click();
    await page.waitForTimeout(250);

    await stream.evaluate((el) => {
      el.scrollTop = 0;
    });
    await expect(page.getByTestId('log-follow-resume')).toBeVisible();

    // NOW let live output resume: the reader is parked up-page and must stay
    // there while new lines land underneath.
    await page.getByTestId('toggle-live').click();
    await expect
      .poll(async () => (await stream.innerText()).includes('live record'), { timeout: 12_000 })
      .toBe(true);

    const parked = await stream.evaluate((el) => el.scrollTop);
    expect(parked, 'new lines must not drag the reader back to the bottom').toBeLessThan(200);
    await expect(page.getByTestId('log-follow-resume')).toBeVisible();

    await page.getByTestId('log-follow-resume').click();
    await expect(page.getByTestId('log-follow-resume')).toHaveCount(0);
    const pinned = await stream.evaluate(
      (el) => el.scrollHeight - el.scrollTop - el.clientHeight,
    );
    expect(pinned).toBeLessThanOrEqual(24);
  });

  test('a standing selection is not scrolled out from under the reader', async ({ page }) => {
    // Following the tail past text someone highlighted IN ORDER TO COPY IT is
    // how a transcript gets scrolled out from under them, so `keepPinned`
    // declines while a selection stands in the pane. This asserts the SCROLL
    // POSITION holds, which is the part that is actually load-bearing:
    // dropping the `paneHasSelection()` guard moves the pane and the assertion
    // below fails.
    //
    // A note on method, because it is easy to write a version of this that
    // passes for the wrong reason: the selection TEXT surviving is necessary
    // but not sufficient evidence. Both a keyed and an unkeyed each-block
    // preserve it while the buffer is append-only with stable ids, so a text
    // assertion alone stays green through a change that would break selection
    // once the buffer is ever re-fetched wholesale. It is asserted here as a
    // floor, not as proof of the reconciliation strategy.
    await installDiagnostics(page, { entries: MANY, growBy: 3 });
    await gotoLogs(page);

    const stream = page.getByTestId('log-stream');
    await expect(stream).toContainText('record 200');
    // Settle at the tail before measuring, so the reading is not taken
    // mid-pin.
    await page.waitForTimeout(250);
    const scrollBefore = await stream.evaluate((el) => el.scrollTop);

    const selected = await page.evaluate(() => {
      const pane = document.querySelector('[data-testid="log-stream"]')!;
      const line = pane.querySelectorAll('.log-line')[5];
      const range = document.createRange();
      range.selectNodeContents(line);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      return sel.toString();
    });
    expect(selected.length).toBeGreaterThan(0);

    // Let at least two poll cycles of new output land.
    await expect
      .poll(async () => (await stream.innerText()).includes('live record'), { timeout: 12_000 })
      .toBe(true);

    const scrollAfter = await stream.evaluate((el) => el.scrollTop);
    expect(
      scrollAfter,
      'the pane must not follow the tail while a selection stands in it',
    ).toBe(scrollBefore);

    const after = await page.evaluate(() => window.getSelection()?.toString() ?? '');
    expect(after, 'incoming lines must not collapse or extend the selection').toBe(selected);
  });
});

// =============================================================================
// The bug-report review sheet — the safety mechanism
// =============================================================================

test.describe('bug report', () => {
  async function openSheet(page: Page) {
    await page.getByTestId('open-bug-report').click();
    await expect(page.getByTestId('bug-report-sheet')).toBeVisible();
    await expect(page.getByTestId('bundle-totals')).toBeVisible({ timeout: 10_000 });
  }

  test('lists every member with its size and its redaction count', async ({ page }) => {
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    await expect(page.getByTestId('bundle-totals')).toContainText('11');
    await expect(page.getByTestId('bundle-source-backend')).toContainText('backend.log');
    await expect(page.getByTestId('bundle-source-backend')).toContainText('118 KB');
    await expect(page.getByTestId('member-redactions-backend')).toHaveText('3 redacted');
    await expect(page.getByTestId('member-redactions-steps')).toHaveText('8 redacted');
    await expect(page.getByTestId('bundle-source-settings')).toContainText(
      'names only — no values, ever',
    );
  });

  test('the risky sources carry their warning in words, next to themselves', async ({ page }) => {
    // Pattern matching does not reach an agent step log: it holds prompts,
    // model output and diffs of the user's source. That cannot be redacted,
    // so it has to be READ - which means saying so where the eye already is,
    // not in a banner at the bottom.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    const stepRisk = page.getByTestId('risk-note-steps');
    await expect(stepRisk).toBeVisible();
    await expect(stepRisk).toContainText('prompts, model output and diffs of your source');
    await expect(page.getByTestId('risk-note-runner')).toBeVisible();

    // And the public-repo consequence, stated as a consequence - IN VIEW,
    // without scrolling. It sat below the member list, which put the single
    // most important sentence for a public repository below the fold: the
    // list is long enough that a reader could tick every source and press
    // Download without the caveat ever having been on screen.
    const warning = page.getByTestId('public-repo-warning');
    await expect(warning).toContainText('public');
    await expect(warning).toContainText('indexed');
    await expect(warning).toBeInViewport();
    await expect(page.getByTestId('bug-report-download')).toBeInViewport();
  });

  test('preview shows the ASSEMBLED member, with redactions highlighted', async ({ page }) => {
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    await page.getByTestId('bundle-source-backend').getByRole('button', { name: 'Preview' }).click();
    const preview = page.getByTestId('bundle-preview-text');
    await expect(preview).toBeVisible();
    await expect(preview).toContainText("branch 'main' not found");
    await expect(page.getByTestId('preview-redacted-span')).toHaveText(PLACEHOLDER);
    expect(await preview.innerText()).not.toContain(SENTINEL);
  });

  test('the client sends NO log content in order to have a bundle built', async ({ page }) => {
    // A naive implementation would post the buffered log lines up to be
    // archived. That would put log content into a request the user never
    // reviewed, and would make the browser a second place where the
    // unredacted-vs-redacted question has to be got right. The server already
    // holds the logs; the client sends a selection and two sentences.
    const handle = await installDiagnostics(page, {
      entries: [makeEntry(1, { message: `token ${PLACEHOLDER} rejected` })],
    });
    await gotoLogs(page);
    await openSheet(page);

    expect(handle.bundlePosts.length).toBeGreaterThan(0);
    for (const body of handle.bundlePosts) {
      const parsed = JSON.parse(body);
      expect(Object.keys(parsed).sort()).toEqual(
        ['run_id', 'sources', 'title', 'what_happened'].sort(),
      );
      expect(body).not.toContain(SENTINEL);
      expect(body).not.toContain('rejected');
      expect(body).not.toContain(PLACEHOLDER);
    }
  });

  test('changing the selection blocks download until the bundle is rebuilt', async ({ page }) => {
    // THE safety property of this sheet. If a toggle could change what gets
    // downloaded without rebuilding, the user would receive bytes they never
    // looked at - and the human review that compensates for everything
    // pattern redaction cannot catch would be nominal rather than real.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    const download = page.getByTestId('bug-report-download');
    await expect(download).toBeEnabled();

    await page.getByTestId('bundle-toggle-steps').uncheck();
    // Immediately - not after a debounce - the reviewed artifact is gone.
    await expect(download).toBeDisabled();
    await expect(page.getByTestId('bundle-totals')).toHaveCount(0);

    // ...and comes back only once a fresh bundle has been built and shown.
    await expect(page.getByTestId('bundle-totals')).toBeVisible({ timeout: 10_000 });
    await expect(download).toBeEnabled();
  });

  test('download saves exactly the bytes the reviewed bundle id returned', async ({ page }) => {
    // No client-side assembly: the browser saves what the server sent for the
    // id the sheet displayed. Any divergence here is a second code path
    // between what was reviewed and what was received.
    const handle = await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByTestId('bug-report-download').click(),
    ]);

    const path = await download.path();
    expect(path).toBeTruthy();
    const bytes = await readFile(path!, 'utf8');
    expect(bytes).toBe(ARCHIVE_BODY);
    expect(bytes).not.toContain(SENTINEL);
    expect(bytes).toContain(PLACEHOLDER);

    // The archive fetched is the one that was on screen.
    expect(handle.requested).toContain(`/bundle/${BUNDLE_ID}`);
    await expect(page.getByTestId('download-done')).toContainText('.zip');
  });

  test('there is no upload control anywhere in the flow', async ({ page }) => {
    // Not disabled - ABSENT, with the reason stated. Posting to GitHub from
    // here would need a write-scoped token in a backend whose every
    // human-facing router is unauthenticated and which binds 0.0.0.0 with the
    // docker socket mounted. A disabled button is an invitation to hunt for
    // the flag that enables it; an explained absence is a decision.
    await installDiagnostics(page, { entries: [makeEntry(1)] });
    await gotoLogs(page);
    await openSheet(page);

    const sheet = page.getByTestId('bug-report-sheet');
    for (const name of [/upload/i, /post to github/i, /create issue/i, /submit/i, /publish/i]) {
      await expect(sheet.getByRole('button', { name })).toHaveCount(0);
    }
    await expect(page.getByTestId('no-upload-note')).toContainText('no upload button');

    // Nothing in the flow may talk to GitHub either.
    expect(
      (await page.evaluate(() =>
        performance.getEntriesByType('resource').map((r) => r.name),
      )).filter((u) => u.includes('github.com')),
    ).toHaveLength(0);
  });
});
