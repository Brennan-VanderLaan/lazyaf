/**
 * QA — live data must not destroy what the user is doing.
 *
 * The owner's complaint was "I couldn't highlight parts to copy it because of
 * the data refreshing". Three separate mechanisms did that, and each one gets
 * a spec here:
 *
 *  1. a whole log body rendered as ONE text node, replaced on every 2s/3s
 *     refresh — any selection over it collapsed (PipelineRunViewer, JobStatus);
 *  2. a list rewritten by POSITION rather than by identity — the row under the
 *     cursor became a different row between aiming and clicking
 *     (run step timeline, diff file list, board columns);
 *  3. a refresh that unmounted the thing being read and put a spinner or an
 *     error in its place (DiffViewer, JobStatus, PipelineRunViewer).
 *
 * These drive the REAL frontend but serve the API from `page.route` fixtures.
 * That is deliberate and follows the convention set by demo-polish.spec.ts:
 * the findings are RENDERING behaviour, so only the Svelte component tree has
 * to be real, and the QA stack at :8790 is shared with other QA agents that
 * call `/api/test/reset` at will.
 *
 * A note on method, because it is easy to write a version of these that passes
 * for the wrong reason: a selection is asserted by reading
 * `window.getSelection().toString()` before and after the refresh, NOT by
 * counting nodes. A pane can keep its node count and still have had every text
 * node rewritten underneath the selection.
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5181 \
 *     npx playwright test e2e/qa/live-update-state.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

const REPO_ID = 'aaaaaaaa-0000-4000-8000-00000000a001';
const RUN_ID = 'bbbbbbbb-0000-4000-8000-00000000b001';
const CARD_ID = 'cccccccc-0000-4000-8000-00000000c001';
const JOB_ID = 'dddddddd-0000-4000-8000-00000000d001';

/** Serialize like the backend does: `datetime.utcnow().isoformat()` — NO 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

function repo(over: Record<string, unknown> = {}) {
  return {
    id: REPO_ID,
    name: 'live-repo',
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

function stepRun(stepIndex: number, name: string, over: Record<string, unknown> = {}) {
  return {
    id: `sr-${stepIndex}`,
    pipeline_run_id: RUN_ID,
    step_index: stepIndex,
    step_name: name,
    status: 'passed',
    executor: 'local',
    job_id: null,
    logs: null,
    error: null,
    started_at: naiveUtc(-5000),
    completed_at: naiveUtc(-1000),
    created_at: naiveUtc(-5000),
    ...over,
  };
}

function run(over: Record<string, unknown> = {}) {
  return {
    id: RUN_ID,
    pipeline_id: 'p-1',
    status: 'running',
    trigger_type: 'manual',
    trigger_ref: null,
    trigger_context: null,
    current_step: 0,
    steps_completed: 1,
    steps_total: 3,
    active_step_ids: [],
    completed_step_ids: [],
    started_at: naiveUtc(-5000),
    completed_at: null,
    created_at: naiveUtc(-5000),
    step_runs: [],
    ...over,
  };
}

function card(over: Record<string, unknown> = {}) {
  return {
    id: CARD_ID,
    repo_id: REPO_ID,
    title: 'Add the thing',
    description: 'Please add the thing.',
    status: 'in_review',
    runner_type: 'any',
    step_type: 'agent',
    step_config: null,
    prompt_template: null,
    agent_file_ids: null,
    branch_name: 'lazyaf/add-the-thing',
    pr_url: null,
    job_id: JOB_ID,
    completed_runner_type: 'claude-code',
    pipeline_run_id: null,
    pipeline_step_index: null,
    created_at: naiveUtc(-60_000),
    updated_at: naiveUtc(-10_000),
    ...over,
  };
}

function diffResponse(paths: string[]) {
  return {
    base_branch: 'main',
    head_branch: 'lazyaf/add-the-thing',
    base_sha: 'a'.repeat(40),
    head_sha: 'b'.repeat(40),
    commit_count: 1,
    total_additions: paths.length,
    total_deletions: 0,
    files: paths.map((path, i) => ({
      path,
      status: 'modified',
      additions: 1,
      deletions: 0,
      diff: `diff --git a/${path} b/${path}\n--- a/${path}\n+++ b/${path}\n@@ -1,2 +1,3 @@\n context line ${i}\n+added line ${i}\n context tail ${i}\n`,
    })),
  };
}

/**
 * Mutable server state. Tests mutate these between refreshes to simulate the
 * live updates that used to destroy the user's work; every route reads them
 * fresh, so a change lands on the very next poll.
 */
interface Server {
  steps: ReturnType<typeof stepRun>[];
  runStatus: string;
  stepLogs: Map<number, string>;
  /** Step indexes whose log endpoint should currently fail. */
  failingStepLogs: Set<number>;
  jobLogs: string;
  jobLogsFail: boolean;
  diffPaths: string[];
  /** Every step-logs step_index requested, in order. */
  stepLogRequests: number[];
}

async function mockApi(page: Page, server: Server, opts: { cards?: unknown[] } = {}) {
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
      const url = new URL(route.request().url());
      const path = url.pathname;
      const currentRun = () => run({ status: server.runStatus, step_runs: server.steps });

      if (path === '/api/repos') return route.fulfill(json([repo()]));
      if (path === `/api/repos/${REPO_ID}/pipelines`) return route.fulfill(json([pipeline()]));
      if (path === `/api/repos/${REPO_ID}/cards`) return route.fulfill(json(opts.cards ?? []));
      if (path === `/api/repos/${REPO_ID}/clone-url`) {
        return route.fulfill(json({ clone_url: `http://localhost/git/${REPO_ID}.git`, is_ingested: true }));
      }
      if (path === `/api/repos/${REPO_ID}/branches`) {
        return route.fulfill(json({
          branches: [
            { name: 'main', commit: 'a'.repeat(40), is_default: true },
            { name: 'lazyaf/add-the-thing', commit: 'b'.repeat(40), is_default: false },
          ],
          default_branch: 'main',
          total: 2,
        }));
      }
      if (path === `/api/repos/${REPO_ID}/commits`) {
        return route.fulfill(json({ branch: 'main', commits: [], total: 0 }));
      }
      if (path === `/api/repos/${REPO_ID}/diff`) {
        return route.fulfill(json(diffResponse(server.diffPaths)));
      }

      const stepLogMatch = path.match(/^\/api\/pipeline-runs\/[^/]+\/steps\/(\d+)\/logs$/);
      if (stepLogMatch) {
        const index = Number(stepLogMatch[1]);
        server.stepLogRequests.push(index);
        if (server.failingStepLogs.has(index)) {
          return route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'log store unreachable' }),
          });
        }
        const step = server.steps.find(s => s.step_index === index);
        return route.fulfill(json({
          step_index: index,
          step_name: step?.step_name ?? `Step ${index}`,
          logs: server.stepLogs.get(index) ?? '',
          error: null,
          status: step?.status ?? 'passed',
        }));
      }

      if (path === '/api/pipeline-runs') return route.fulfill(json([currentRun()]));
      if (path.startsWith('/api/pipeline-runs/')) return route.fulfill(json(currentRun()));

      if (path === `/api/jobs/${JOB_ID}/logs`) {
        if (server.jobLogsFail) {
          return route.fulfill({
            status: 503,
            contentType: 'application/json',
            body: JSON.stringify({ detail: 'runner log store unreachable' }),
          });
        }
        return route.fulfill(json({ job_id: JOB_ID, logs: server.jobLogs, status: 'running' }));
      }
      if (path === `/api/jobs/${JOB_ID}`) {
        return route.fulfill(json({
          id: JOB_ID,
          card_id: CARD_ID,
          runner_id: 'runner-1',
          status: 'running',
          logs: '',
          error: null,
          started_at: naiveUtc(-30_000),
          completed_at: null,
          created_at: naiveUtc(-30_000),
        }));
      }

      // Anything unmodelled: an empty list is the least surprising default.
      return route.fulfill(json([]));
    },
  );
}

function freshServer(over: Partial<Server> = {}): Server {
  return {
    steps: [stepRun(0, 'build'), stepRun(1, 'test', { status: 'running', completed_at: null })],
    runStatus: 'running',
    stepLogs: new Map(),
    failingStepLogs: new Set(),
    jobLogs: '',
    jobLogsFail: false,
    diffPaths: ['src/one.ts', 'src/two.ts', 'src/three.ts', 'src/four.ts'],
    stepLogRequests: [],
    ...over,
  };
}

function logBody(count: number, prefix = 'line'): string {
  return Array.from({ length: count }, (_, i) => `${prefix} ${i}`).join('\n') + '\n';
}

/** Boot the shell, pick the one repo, land on the Pipelines page's Runs tab. */
async function openRunViewer(page: Page) {
  await page.goto('/#/pipelines');
  await page.getByText('live-repo', { exact: true }).click();
  await page.getByRole('button', { name: /^Runs \(/ }).click();
  await page.locator('[data-testid="runs-list"] tbody tr').first().click();
  await expect(page.locator('[data-testid="run-viewer"]')).toBeVisible();
}

/**
 * Put a live selection across two rendered log lines, exactly where a
 * drag-select would land, and return what the user would copy.
 */
async function selectAcrossLines(page: Page, selector: string, first: number, last: number) {
  return page.evaluate(
    ({ selector, first, last }) => {
      const nodes = [...document.querySelectorAll(selector)];
      const startText = nodes[first]?.firstChild;
      const endText = nodes[last]?.firstChild;
      if (!startText || !endText) throw new Error(`no log lines at ${first}/${last} for ${selector}`);
      const range = document.createRange();
      range.setStart(startText, 2);
      range.setEnd(endText, (endText.textContent ?? '').length);
      const sel = window.getSelection()!;
      sel.removeAllRanges();
      sel.addRange(range);
      return sel.toString();
    },
    { selector, first, last },
  );
}

const currentSelection = (page: Page) => page.evaluate(() => window.getSelection()!.toString());

// --------------------------------------------------------------------------
// 1. Log panes: one node per line, so an append cannot eat a selection.
// --------------------------------------------------------------------------

test.describe('run viewer log pane', () => {
  test('renders one node per log line instead of one text node for the whole body', async ({ page }) => {
    const server = freshServer();
    server.stepLogs.set(0, logBody(6));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"][data-step-index="0"]').click();
    const lines = page.locator('[data-testid="logs"] .log-line');
    await expect(lines).toHaveCount(6);
    await expect(lines.first()).toHaveText('line 0');
    // A trailing newline terminates the last line; it is not an empty line.
    await expect(lines.last()).toHaveText('line 5');
  });

  test('a selection over log output survives the 2s refresh that appends new lines', async ({ page }) => {
    const server = freshServer();
    server.stepLogs.set(0, logBody(8));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"][data-step-index="0"]').click();
    await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(8);

    const before = await selectAcrossLines(page, '[data-testid="logs"] .log-line', 2, 3);
    expect(before, 'the fixture must produce a non-empty selection').not.toBe('');

    // The step keeps talking, and the viewer's own 2s poll picks it up.
    server.stepLogs.set(0, logBody(20));
    await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(20, { timeout: 10_000 });

    expect(
      await currentSelection(page),
      'new output appended below must not disturb the text the user highlighted',
    ).toBe(before);
  });

  test('the log pane does not lose lines when the REST snapshot overtakes the live tail', async ({ page }) => {
    // The two sources are views of one stream. Swapping between them used to
    // rewrite every line; the pane must only ever gain lines.
    const server = freshServer();
    server.stepLogs.set(0, logBody(4));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"][data-step-index="0"]').click();
    await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(4);

    const counts: number[] = [];
    for (const size of [9, 9, 14, 14]) {
      server.stepLogs.set(0, logBody(size));
      await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(size, { timeout: 10_000 });
      counts.push(await page.locator('[data-testid="logs"] .log-line').count());
    }

    expect(counts).toEqual([9, 9, 14, 14]);
  });

  test('a failed log refresh keeps the logs already on screen and says the refresh failed', async ({ page }) => {
    const server = freshServer();
    server.stepLogs.set(0, logBody(5));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"][data-step-index="0"]').click();
    await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(5);

    server.failingStepLogs.add(0);
    await expect(page.locator('[data-testid="logs-stale"]')).toBeVisible({ timeout: 10_000 });
    // R1: loud about the failure, and the transcript is still readable.
    await expect(page.locator('[data-testid="logs"] .log-line')).toHaveCount(5);
    await expect(page.locator('[data-testid="logs"] .log-line').first()).toHaveText('line 0');
  });
});

// --------------------------------------------------------------------------
// 2. Row identity: a step is chosen by step_index, never by row position.
// --------------------------------------------------------------------------

test.describe('run viewer step timeline', () => {
  test('clicking a step fetches THAT step, even when the indexes are not contiguous', async ({ page }) => {
    // A parallel/conditional pipeline where step 1 has not reported yet: the
    // second ROW is step_index 2. Selecting by position asked for step 1.
    const server = freshServer({
      steps: [stepRun(0, 'build'), stepRun(2, 'e2e')],
    });
    server.stepLogs.set(0, logBody(3, 'build'));
    server.stepLogs.set(2, logBody(3, 'e2e'));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"]').nth(1).click();

    await expect(page.locator('.step-details-header h3')).toHaveText('e2e');
    await expect(page.locator('[data-testid="logs"] .log-line').first()).toHaveText('e2e 0');
    expect(
      server.stepLogRequests,
      'the viewer must ask for step_index 2, the step the row represents',
    ).toContain(2);
    expect(server.stepLogRequests).not.toContain(1);
  });

  test('a step reporting late does not move the user onto a different step', async ({ page }) => {
    const server = freshServer({
      steps: [stepRun(0, 'build'), stepRun(2, 'e2e')],
    });
    server.stepLogs.set(0, logBody(3, 'build'));
    server.stepLogs.set(1, logBody(3, 'test'));
    server.stepLogs.set(2, logBody(3, 'e2e'));
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('[data-testid="step"]').nth(1).click();
    await expect(page.locator('.step-details-header h3')).toHaveText('e2e');

    // The missing middle step reports and sorts into place between the two.
    server.steps = [stepRun(0, 'build'), stepRun(1, 'test'), stepRun(2, 'e2e')];
    await expect(page.locator('[data-testid="step"]')).toHaveCount(3, { timeout: 10_000 });

    await expect(
      page.locator('.step-details-header h3'),
      'the heading must still name the step the user opened',
    ).toHaveText('e2e');
    await expect(page.locator('[data-testid="logs"] .log-line').first()).toHaveText('e2e 0');
    await expect(page.locator('[data-testid="step"][data-step-index="2"]')).toHaveClass(/selected/);
  });

  test('the step rows stay in step_index order', async ({ page }) => {
    // The store re-sorts on insert, but a REST payload need not be ordered.
    const server = freshServer({
      steps: [stepRun(2, 'e2e'), stepRun(0, 'build'), stepRun(1, 'test')],
    });
    await mockApi(page, server);
    await openRunViewer(page);

    const indexes = await page.locator('[data-testid="step"]').evaluateAll(
      (rows) => rows.map(r => Number((r as HTMLElement).dataset.stepIndex)),
    );
    expect(indexes).toEqual([0, 1, 2]);
  });
});

// --------------------------------------------------------------------------
// 3. A selection drag that leaves the modal must not close it.
// --------------------------------------------------------------------------

test.describe('run viewer modal dismissal', () => {
  test('releasing a selection drag on the backdrop does not close the viewer', async ({ page }) => {
    const server = freshServer();
    server.stepLogs.set(0, logBody(6));
    await mockApi(page, server);
    await openRunViewer(page);
    await page.locator('[data-testid="step"][data-step-index="0"]').click();

    const line = page.locator('[data-testid="logs"] .log-line').nth(2);
    const box = (await line.boundingBox())!;
    // Press inside the log pane and release out on the dimmed backdrop, which
    // is what selecting to the end of a line actually looks like.
    await page.mouse.move(box.x + 10, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 120, box.y + box.height / 2, { steps: 5 });
    await page.mouse.move(20, box.y + 80, { steps: 10 });
    await page.mouse.up();

    await expect(
      page.locator('[data-testid="run-viewer"]'),
      'a drag that started inside the modal must not dismiss it',
    ).toBeVisible();
  });

  test('a plain click on the backdrop still closes the viewer', async ({ page }) => {
    const server = freshServer();
    await mockApi(page, server);
    await openRunViewer(page);

    await page.locator('.modal-backdrop').click({ position: { x: 8, y: 8 } });
    await expect(page.locator('[data-testid="run-viewer"]')).toBeHidden();
  });
});

// --------------------------------------------------------------------------
// 4. The card modal's job logs and diff.
// --------------------------------------------------------------------------

test.describe('card modal live panes', () => {
  async function openCard(page: Page) {
    await page.goto('/#/');
    await page.getByText('live-repo', { exact: true }).click();
    await page.locator('[data-testid="card"]').first().click();
    await expect(page.locator('[data-testid="card-modal"], .modal').first()).toBeVisible();
  }

  test('a selection in the job log pane survives the 3s poll', async ({ page }) => {
    const server = freshServer();
    server.jobLogs = logBody(8, 'job');
    await mockApi(page, server, { cards: [card()] });
    await openCard(page);

    await page.locator('[data-testid="logs-btn"]').click();
    const lines = page.locator('[data-testid="job-logs-body"] .log-line');
    await expect(lines).toHaveCount(8);

    const before = await selectAcrossLines(page, '[data-testid="job-logs-body"] .log-line', 1, 2);
    expect(before).not.toBe('');

    server.jobLogs = logBody(18, 'job');
    await expect(lines).toHaveCount(18, { timeout: 10_000 });

    expect(
      await currentSelection(page),
      'the 3s job-log poll must not collapse a selection over lines that did not change',
    ).toBe(before);
  });

  test('a failed job-log poll keeps the logs on screen and names the failure', async ({ page }) => {
    const server = freshServer();
    server.jobLogs = logBody(5, 'job');
    await mockApi(page, server, { cards: [card()] });
    await openCard(page);

    await page.locator('[data-testid="logs-btn"]').click();
    await expect(page.locator('[data-testid="job-logs-body"] .log-line')).toHaveCount(5);

    server.jobLogsFail = true;
    await expect(page.locator('[data-testid="logs-error"]')).toBeVisible({ timeout: 10_000 });
    await expect(
      page.locator('[data-testid="job-logs-body"] .log-line'),
      'one failed poll must not delete the transcript the user is reading',
    ).toHaveCount(5);
  });

  test('refreshing the diff keeps the files the user opened and closed', async ({ page }) => {
    const server = freshServer();
    await mockApi(page, server, { cards: [card()] });
    await openCard(page);

    const viewer = page.locator('[data-testid="diff-viewer"]');
    await expect(viewer).toBeVisible();
    // Auto-expanded: the first three of four files.
    const fileItems = viewer.locator('[data-testid="file-item"]');
    await expect(fileItems).toHaveCount(4);

    // The user collapses the first file and opens the fourth.
    await fileItems.nth(0).locator('.file-header').click();
    await fileItems.nth(3).locator('.file-header').click();
    const expandedBefore = await viewer.locator('[data-testid="file-item"] .file-diff').count();
    expect(expandedBefore).toBe(3); // two, three, four

    await viewer.locator('.btn-refresh').click();
    // The refresh must not take the diff off screen while it reloads.
    await expect(viewer).toBeVisible();

    await expect
      .poll(async () => viewer.locator('[data-testid="file-item"] .file-diff').count())
      .toBe(3);
    await expect(
      viewer.locator('[data-testid="file-item"]').nth(0).locator('.file-diff'),
      'the file the user collapsed must stay collapsed across a refresh',
    ).toHaveCount(0);
    await expect(
      viewer.locator('[data-testid="file-item"]').nth(3).locator('.file-diff'),
      'the file the user opened must stay open across a refresh',
    ).toHaveCount(1);
  });
});
