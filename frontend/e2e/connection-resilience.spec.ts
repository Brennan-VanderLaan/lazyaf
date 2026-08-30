/**
 * Connection resilience — QA triage T7, the three symptoms a demo hits.
 *
 *   1. The board NEVER resynced after a dropped socket. Every store fed by the
 *      WebSocket is delta-only once loaded; a delta broadcast during an outage
 *      is not queued anywhere. `ws.onopen` set a status flag and did nothing
 *      else, so a card edited while the backend restarted kept its old title
 *      and old column until someone pressed F5.
 *   2. A dead backend was INVISIBLE. `websocketStore.status` was a fully
 *      computed four-state store with zero consumers, so the board went on
 *      rendering stale rows exactly as if they were live.
 *   3. The offline state offered no way out but a page reload.
 *
 * WHY THIS IS A PLAYWRIGHT SPEC (standing rule R8): none of it is visible to a
 * unit test. The resync logic is unit-tested in `stores/websocket.resync.test.ts`,
 * but "the card on screen changes without a reload after the socket comes back"
 * is a property of the whole app — router, stores, socket lifecycle and DOM —
 * and it is precisely what a live demo exercises.
 *
 * THE FAULT INJECTION IS THE TRANSPORT, NOTHING ELSE. `page.routeWebSocket`
 * intercepts the app's `/ws` connection and either proxies it to the real
 * backend or closes it. The application's own reconnect timer, its own
 * snapshot refetch and the real HTTP API all run untouched — which is exactly
 * what a backend restart looks like from the browser.
 */
import { test, expect, type Page, type WebSocketRoute } from '@playwright/test';

import { BACKEND_URL, createTestRepo, resetBackend, selectRepo } from './helpers';

/** The store's own reconnect delay; waits below must clear it comfortably. */
const RECONNECT_DELAY_MS = 3000;

interface SocketControl {
  /** Close the live socket and refuse every reconnect until `restore()`. */
  cut: () => Promise<void>;
  /** Allow reconnects to reach the backend again. */
  restore: () => void;
  /** How many times the app has dialled since the route was installed. */
  attempts: () => number;
}

/**
 * Put a controllable cut-off in front of the app's `/ws` socket.
 *
 * MUST be called before `page.goto`.
 */
async function interceptSocket(page: Page): Promise<SocketControl> {
  let blocked = false;
  let attempts = 0;
  let live: WebSocketRoute | null = null;

  await page.routeWebSocket(/\/ws$/, (ws) => {
    attempts += 1;
    if (blocked) {
      // A refused connection, the way a stopped backend refuses one.
      ws.close({ code: 1006, reason: 'e2e: backend unreachable' });
      return;
    }
    live = ws;
    ws.connectToServer();
  });

  return {
    cut: async () => {
      blocked = true;
      if (live) {
        await live.close({ code: 1006, reason: 'e2e: backend went away' });
        live = null;
      }
    },
    restore: () => {
      blocked = false;
    },
    attempts: () => attempts,
  };
}

const connectionChip = (page: Page) => page.locator('[data-testid="connection-status"]');
const offlineBanner = (page: Page) => page.locator('[data-testid="connection-banner"]');

test.describe('connection state is visible', () => {
  test.beforeEach(async ({ request }) => {
    await resetBackend(request);
  });

  test('the UI states, continuously, whether the board is live', async ({ page }) => {
    // Finding: `websocketStore.status` had ZERO consumers. There was no
    // element anywhere on the page reporting connection state.
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-conn-visible');

    await page.goto('/');
    await selectRepo(page, repo.name);

    await expect(connectionChip(page)).toBeVisible();
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });
    await expect(connectionChip(page)).toContainText('Live');
    // Nothing is wrong, so nothing shouts.
    await expect(offlineBanner(page)).toHaveCount(0);

    // Guard on the harness itself: if the route pattern ever stops matching
    // the app's socket, every outage test below would silently pass by never
    // cutting anything. This is the assertion that keeps them honest.
    expect(socket.attempts(), 'the /ws route must have intercepted the app socket').toBeGreaterThan(0);
  });

  test('a dead backend raises a banner instead of quietly showing stale rows', async ({
    page,
  }) => {
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-conn-dead');
    await page.request.post(`${BACKEND_URL}/api/repos/${repo.id}/cards`, {
      data: { title: 'still on screen while the backend is gone' },
    });

    await page.goto('/');
    await selectRepo(page, repo.name);
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });

    await socket.cut();

    await expect(connectionChip(page)).toHaveAttribute('data-status', 'disconnected', {
      timeout: 10_000,
    });
    await expect(offlineBanner(page)).toBeVisible();
    await expect(offlineBanner(page)).toContainText('Backend unreachable');
    // The rows are still on screen - that is fine, and honest, BECAUSE the
    // page now says they are stale. It was the silence that was the bug.
    await expect(page.locator('.card-title').first()).toBeVisible();

    // And it keeps trying on its own rather than sitting there dead.
    await expect(page.locator('[data-testid="connection-attempts"]')).toBeVisible({
      timeout: RECONNECT_DELAY_MS * 3,
    });

    socket.restore();
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: RECONNECT_DELAY_MS * 4,
    });
    await expect(offlineBanner(page)).toHaveCount(0);
  });

  test('"Try now" dials immediately instead of waiting out the reconnect timer', async ({
    page,
  }) => {
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-conn-retry');

    await page.goto('/');
    await selectRepo(page, repo.name);
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });

    await socket.cut();
    await expect(offlineBanner(page)).toBeVisible({ timeout: 10_000 });

    socket.restore();
    const before = socket.attempts();
    await page.locator('[data-testid="connection-retry"]').click();

    // Well inside the 3s timer: if this passes on the timer rather than the
    // click, it passes for the wrong reason.
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: RECONNECT_DELAY_MS - 1000,
    });
    expect(socket.attempts()).toBeGreaterThan(before);
  });
});

test.describe('the board resyncs after an outage', () => {
  test.beforeEach(async ({ request }) => {
    await resetBackend(request);
  });

  test('a change made while the socket was down appears WITHOUT a reload', async ({ page }) => {
    // THE headline defect. Everything missed during the gap used to be lost
    // permanently; only F5 recovered it.
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-resync');

    const created = await page.request
      .post(`${BACKEND_URL}/api/repos/${repo.id}/cards`, {
        data: { title: 'BEFORE-THE-OUTAGE', description: 'resync fixture' },
      })
      .then((r) => r.json());

    await page.goto('/');
    await selectRepo(page, repo.name);
    await expect(page.getByText('BEFORE-THE-OUTAGE')).toBeVisible({ timeout: 10_000 });
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });

    await socket.cut();
    await expect(offlineBanner(page)).toBeVisible({ timeout: 10_000 });

    // Mutate an existing card AND create a new one while the browser is deaf.
    // Both are `card_updated` / `card_created` deltas the page never receives.
    await page.request.patch(`${BACKEND_URL}/api/cards/${created.id}`, {
      data: { title: 'CHANGED-DURING-THE-OUTAGE' },
    });
    await page.request.post(`${BACKEND_URL}/api/repos/${repo.id}/cards`, {
      data: { title: 'CREATED-DURING-THE-OUTAGE' },
    });

    // Proof the gap is real: the page cannot have seen either change yet.
    await expect(page.getByText('BEFORE-THE-OUTAGE')).toBeVisible();
    await expect(page.getByText('CREATED-DURING-THE-OUTAGE')).toHaveCount(0);

    socket.restore();

    // No reload anywhere in this test. The snapshot-on-reconnect is the only
    // thing that can close the gap.
    await expect(page.getByText('CHANGED-DURING-THE-OUTAGE')).toBeVisible({
      timeout: RECONNECT_DELAY_MS * 4,
    });
    await expect(page.getByText('CREATED-DURING-THE-OUTAGE')).toBeVisible({
      timeout: RECONNECT_DELAY_MS * 4,
    });
    await expect(page.getByText('BEFORE-THE-OUTAGE')).toHaveCount(0);
  });

  test('a card DELETED during the outage stops being rendered after reconnect', async ({
    page,
  }) => {
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-resync-delete');

    const doomed = await page.request
      .post(`${BACKEND_URL}/api/repos/${repo.id}/cards`, {
        data: { title: 'DELETED-DURING-THE-OUTAGE' },
      })
      .then((r) => r.json());

    await page.goto('/');
    await selectRepo(page, repo.name);
    await expect(page.getByText('DELETED-DURING-THE-OUTAGE')).toBeVisible({ timeout: 10_000 });
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });

    await socket.cut();
    await expect(offlineBanner(page)).toBeVisible({ timeout: 10_000 });

    await page.request.delete(`${BACKEND_URL}/api/cards/${doomed.id}`);
    // A snapshot REPLACES the list; a merge-only resync would leave this row
    // on the board forever, which is the same class of bug one layer down.
    await expect(page.getByText('DELETED-DURING-THE-OUTAGE')).toBeVisible();

    socket.restore();

    await expect(page.getByText('DELETED-DURING-THE-OUTAGE')).toHaveCount(0, {
      timeout: RECONNECT_DELAY_MS * 4,
    });
  });

  test('the runner panel resyncs too, and its error is not pinned forever', async ({ page }) => {
    // `stores/runners.ts` documented "load() is called on mount AND AFTER A
    // SOCKET RECONNECT" since 12.6; only the mount half was wired, and the
    // panel's error was cleared nowhere else - so one blip pinned it until F5.
    const socket = await interceptSocket(page);
    const repo = await createTestRepo(page, 'e2e-resync-runners');

    // The runner panel is the LAST thing in a long sidebar (nav, repo list,
    // repository details, runners, agents). At the default 720px it sits
    // below the fold, and this spec is about the panel's behaviour, not about
    // where it lands - so give the sidebar room rather than fighting scroll.
    // (Sidebar cramping at small viewports is QA finding 7, a separate fix.)
    await page.setViewportSize({ width: 1280, height: 1600 });

    await page.goto('/');
    await selectRepo(page, repo.name);
    await expect(page.locator('[data-testid="runner-panel"]')).toBeVisible();
    await expect(connectionChip(page)).toHaveAttribute('data-status', 'connected', {
      timeout: 10_000,
    });

    // Force the snapshot to fail, then drop the socket: the panel must report
    // the failure rather than silently showing an empty fleet.
    await page.route('**/api/runners', (route) =>
      route.fulfill({ status: 503, contentType: 'text/plain', body: 'runner registry unavailable' }),
    );
    await socket.cut();
    await expect(offlineBanner(page)).toBeVisible({ timeout: 10_000 });

    socket.restore();
    const panelError = page.locator('[data-testid="runner-error"]');
    await expect(panelError).toBeVisible({ timeout: RECONNECT_DELAY_MS * 4 });
    await expect(panelError).toContainText('Could not load runners');
    // The words the user used to get instead of a reason.
    await expect(panelError).not.toContainText('Unknown error');

    // Recoverable in place: unblock the endpoint and press the panel's Retry.
    await page.unroute('**/api/runners');
    const retry = page.locator('[data-testid="runner-error-retry"]');
    await retry.scrollIntoViewIfNeeded();
    await retry.click();
    await expect(panelError).toHaveCount(0, { timeout: 10_000 });
  });
});
