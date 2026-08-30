/**
 * QA-5 lane: UI workflow abuse — regressions for findings that are only
 * observable in the rendered page.
 *
 * OPT-IN ONLY. These specs sit under e2e/ so they are collected by the
 * default playwright.config.ts, but they no-op unless QA5_UI_URL is set.
 * That keeps `npm run test:e2e` (which drives the :8765 e2e lane) completely
 * unaffected.
 *
 * To run against the isolated QA sandbox:
 *
 *   # backend: docker compose -p lazyaf-qa -f docker-compose.qa.yml up -d
 *   cd frontend
 *   VITE_BACKEND_URL=http://localhost:8790 npx vite --port 5191 --strictPort &
 *   QA5_UI_URL=http://localhost:5191 QA5_API_URL=http://localhost:8790 \
 *     npx playwright test e2e/qa/qa5-ui-workflow.spec.ts
 *
 * `test.fail()` is the Playwright equivalent of xfail(strict=True): the spec
 * FAILS the run if the behaviour it encodes is ever fixed, so these scream
 * rather than silently passing.
 */
import { test, expect, type Page } from '@playwright/test';

const UI_URL = process.env.QA5_UI_URL || '';
const API_URL = process.env.QA5_API_URL || 'http://localhost:8790';

test.skip(!UI_URL, 'QA-5 UI lane is opt-in: set QA5_UI_URL to run it');

/** Create a throwaway repo + card straight through the API. */
async function seed(page: Page, cardTitle: string) {
  const name = `qa5-ui-${Date.now().toString(36)}`;
  const repo = await page.request
    .post(`${API_URL}/api/repos`, { data: { name, default_branch: 'main' } })
    .then((r) => r.json());
  await page.request.post(`${API_URL}/api/repos/${repo.id}/cards`, {
    data: { title: cardTitle, description: 'QA-5 fixture' },
  });
  return repo;
}

async function openRepo(page: Page, repoName: string) {
  await page.goto(UI_URL);
  await page.getByText(repoName, { exact: true }).click();
  await expect(page.locator('.card-title').first()).toBeVisible({ timeout: 15_000 });
}

test.describe('QA-5 finding 8 (MINOR): long card titles are hard-clipped', () => {
  test.fail(
    true,
    'QA finding 8: .card-title is a flex item with min-width:auto, so it ' +
      'grows to max-content (~6445px measured) and is clipped by ' +
      '.card{overflow:hidden} with no ellipsis and no way to read the rest',
  );

  test('a long unbroken title stays inside its card', async ({ page }) => {
    const title = 'qa5-longword-' + 'A'.repeat(600);
    const repo = await seed(page, title);
    await openRepo(page, repo.name);

    const heading = page.locator('.card-title', { hasText: 'qa5-longword-' });
    const metrics = await heading.evaluate((el) => {
      const card = el.closest('.card') as HTMLElement | null;
      return {
        titleScrollWidth: el.scrollWidth,
        cardClientWidth: card ? card.clientWidth : -1,
        textOverflow: getComputedStyle(el).textOverflow,
      };
    });

    // The title must not extend far beyond the card that contains it.
    expect(
      metrics.titleScrollWidth,
      `title lays out ${metrics.titleScrollWidth}px wide inside a ` +
        `${metrics.cardClientWidth}px card; the overflow is clipped with ` +
        `text-overflow:${metrics.textOverflow} so most of it is unreadable`,
    ).toBeLessThanOrEqual(metrics.cardClientWidth * 2);
  });
});

test.describe('QA-5 finding 7 (MAJOR, demo): no narrow-viewport layout', () => {
  test.fail(
    true,
    'QA finding 7: App.svelte media queries only shrink the sidebar ' +
      '(260px min at <=768px). There is no hamburger and no collapse, so on ' +
      'a 375px phone the sidebar takes 69% of the viewport and the board is ' +
      'left an 83px window onto 240px columns',
  );

  test('the primary action stays on screen at phone width', async ({ page }) => {
    const repo = await seed(page, 'qa5 narrow-viewport card');
    await page.setViewportSize({ width: 375, height: 812 });
    await openRepo(page, repo.name);

    const newCard = page.getByRole('button', { name: /New Card/ });
    const box = await newCard.boundingBox();
    expect(box, 'the + New Card button should be laid out').not.toBeNull();
    expect(
      (box!.x + box!.width),
      `"+ New Card" extends to x=${box!.x + box!.width} in a 375px viewport, ` +
        `so it is clipped off the right edge`,
    ).toBeLessThanOrEqual(375);
  });

  test('the sidebar does not eat the viewport at phone width', async ({ page }) => {
    const repo = await seed(page, 'qa5 sidebar width card');
    await page.setViewportSize({ width: 375, height: 812 });
    await openRepo(page, repo.name);

    const sidebar = await page.locator('.sidebar').boundingBox();
    expect(
      sidebar!.width / 375,
      `sidebar occupies ${Math.round((sidebar!.width / 375) * 100)}% of a ` +
        `375px viewport with no way to collapse it`,
    ).toBeLessThan(0.5);
  });
});

test.describe('QA-5 finding 3 (MAJOR): backend outage is invisible', () => {
  test.fail(
    true,
    'QA finding 3: websocketStore.status has ZERO consumers (App.svelte only ' +
      'calls connect()/disconnect()). With the backend unreachable the board ' +
      'keeps rendering stale data as if live — no banner, no badge, no toast',
  );

  test('the page shows a connection indicator the user can find', async ({ page }) => {
    const repo = await seed(page, 'qa5 connection indicator card');
    await openRepo(page, repo.name);

    // Any of the shapes a reasonable implementation would use.
    const indicator = page.locator(
      '[data-testid*="connection"], [class*="connection-status"], ' +
        '[class*="ws-status"], [aria-live="polite"][class*="status"]',
    );
    await expect(
      indicator.first(),
      'no element anywhere reports live-connection state to the user',
    ).toBeVisible({ timeout: 5_000 });
  });
});

test.describe('QA-5 verified NOT a bug — pinned so it stays fixed', () => {
  test('HTML in a card title is escaped, not executed', async ({ page }) => {
    const payload = `qa5 <script>alert('xss')</script> <img src=x onerror=alert(1)>`;
    const repo = await seed(page, payload);

    let dialogFired = false;
    page.on('dialog', async (d) => {
      dialogFired = true;
      await d.dismiss();
    });

    await openRepo(page, repo.name);
    const card = page.locator('.card-title', { hasText: "<script>" });
    await expect(card).toBeVisible();
    await expect(card).toHaveText(payload);
    expect(dialogFired, 'an injected title must never open a dialog').toBe(false);
    expect(
      await page.locator('img[src="x"]').count(),
      'an injected <img> must not become a real element',
    ).toBe(0);
  });

  test('the New Card form does not leak input between openings', async ({ page }) => {
    const repo = await seed(page, 'qa5 form reset card');
    await openRepo(page, repo.name);

    await page.getByRole('button', { name: /New Card/ }).click();
    const title = page.getByPlaceholder('What needs to be done?');
    await title.fill('STALE-INPUT-CANARY');
    await page.getByRole('button', { name: 'Cancel' }).click();

    await page.getByRole('button', { name: /New Card/ }).click();
    await expect(
      page.getByPlaceholder('What needs to be done?'),
      'a cancelled draft must not reappear in the next New Card modal',
    ).toHaveValue('');
  });
});
