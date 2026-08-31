/**
 * QA-7 (LENS 2) — the brand-new user, opening LazyAF for the first time.
 *
 * These specs encode the cold-open journey: what the first screen offers, and
 * what happens when the very first action a newcomer takes goes wrong.
 *
 * Like `demo-polish.spec.ts`, the API is served from `page.route` fixtures
 * rather than a live backend. The QA stack at :8790 is shared by several
 * concurrent QA agents that call `/api/test/reset`, so any spec depending on
 * durable backend rows is flaky by construction. Payloads below are
 * byte-for-byte what FastAPI emits, verified against the QA stack on
 * 2026-08-30 (`POST /api/repos/ingest`, `GET /api/repos`).
 *
 * Specs marked `fixme` encode a CONFIRMED bug: they assert the behaviour a
 * newcomer needs, they fail against current behaviour on purpose, and they
 * must be flipped to passing when the bug is fixed. Nothing here is weakened
 * to go green (R4).
 *
 * Run with (never point this at :8765 or :8000):
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5177 \
 *     npx playwright test e2e/qa/newcomer-journey.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

/** Serialize like the backend does: `datetime.utcnow().isoformat()` — NO 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

/**
 * A cold, empty install: every collection the sidebar and board read is [].
 * This is precisely what a stranger's first paint is driven by.
 */
async function serveEmptyInstall(page: Page) {
  const empty = ['**/api/repos', '**/api/runners', '**/api/agent-files'];
  for (const route of empty) {
    await page.route(route, (r) => r.fulfill({ json: [] }));
  }
}

test.describe('LENS 2 — cold open', () => {
  test.beforeEach(async ({ page }) => {
    await serveEmptyInstall(page);
  });

  /**
   * The board's empty state offers two ways forward and one of them is
   * impossible on a cold install: there is no repository to select. The other
   * half ("add a new one") names no control and points nowhere — the only
   * control that does it is an unlabelled `+` glyph in the sidebar.
   */
  test.fixme(
    'board empty state does not tell a newcomer to select a repo that cannot exist',
    async ({ page }) => {
      await page.goto('/');

      const noRepo = page.getByTestId('no-repo');
      await expect(noRepo).toBeVisible();

      // The sidebar is simultaneously asserting there is nothing to select.
      await expect(page.locator('.repo-empty')).toHaveText('No repositories added yet');

      // So the board must not send the newcomer to look for one.
      await expect(noRepo).not.toContainText('Select a repository from the sidebar');
    }
  );

  /**
   * An empty state is the highest-leverage teaching surface in an app. The
   * board's is the FIRST thing a stranger sees and it contains no control at
   * all: the entire next step is a 20px `+` in the sidebar chrome.
   * `EndpointsPage` shows the team can do this well; the board should match.
   */
  test.fixme('board empty state offers a control, not just prose', async ({ page }) => {
    await page.goto('/');

    const noRepo = page.getByTestId('no-repo');
    await expect(noRepo).toBeVisible();
    await expect(noRepo.locator('button, a')).toHaveCount(1);
  });
});

test.describe('LENS 2 — the first action a newcomer takes', () => {
  test.beforeEach(async ({ page }) => {
    await serveEmptyInstall(page);
  });

  /** Enter in the name field must submit — newcomers never reach for the button. */
  test('pressing Enter in the repo name field submits the form', async ({ page }) => {
    let ingested = false;
    await page.route('**/api/repos/ingest', async (route) => {
      ingested = true;
      await route.fulfill({
        status: 201,
        json: {
          id: 'a1b2c3d4-0000-4000-8000-000000000001',
          name: 'my-first-project',
          internal_git_url: '/git/a1b2c3d4-0000-4000-8000-000000000001.git',
          clone_url: 'http://localhost:8790/git/a1b2c3d4-0000-4000-8000-000000000001.git',
        },
      });
    });
    await page.route('**/api/repos/a1b2c3d4-*', (route) =>
      route.fulfill({
        json: {
          id: 'a1b2c3d4-0000-4000-8000-000000000001',
          name: 'my-first-project',
          remote_url: null,
          default_branch: 'main',
          is_ingested: true,
          internal_git_url: '/git/a1b2c3d4-0000-4000-8000-000000000001.git',
          created_at: naiveUtc(),
        },
      })
    );

    await page.goto('/');
    await page.getByTestId('add-repo-btn').click();
    await page.getByTestId('repo-name-input').fill('my-first-project');
    await page.getByTestId('repo-name-input').press('Enter');

    await expect.poll(() => ingested).toBe(true);
  });

  /**
   * CONFIRMED BUG. `RepoSelector.handleAdd` wraps the create in `try/finally`
   * with no `catch`, and the component never renders `reposStore.error`. A
   * rejected ingest therefore leaves the form open, the button back on
   * "Create Repo", and NOTHING on screen saying why — the failure is visible
   * only as an unhandled rejection in devtools. That is a dark failure (R1)
   * on the very first action a new user takes.
   *
   * 422 is not hypothetical: the backend caps `name` at 200 characters, so
   * pasting anything long into the name field lands exactly here.
   */
  test.fixme('a rejected repo create tells the user why', async ({ page }) => {
    await page.route('**/api/repos/ingest', (route) =>
      route.fulfill({
        status: 422,
        json: {
          detail: [
            {
              type: 'string_too_long',
              loc: ['body', 'name'],
              msg: 'String should have at most 200 characters',
            },
          ],
        },
      })
    );

    await page.goto('/');
    await page.getByTestId('add-repo-btn').click();
    await page.getByTestId('repo-name-input').fill('A'.repeat(400));
    await page.getByRole('button', { name: 'Create Repo' }).click();

    // The button must settle back out of its pending label...
    await expect(page.getByRole('button', { name: 'Create Repo' })).toBeEnabled();
    // ...and the reason must be on screen, not only in the console.
    await expect(page.locator('.repo-selector')).toContainText(/200 characters|could not|failed/i);
  });

  /**
   * The form's only guidance is "After creating, push your local repo to the
   * internal git URL." — a forward reference to a URL that is not on screen
   * yet and is never named. A newcomer has no way to know where it appears.
   */
  test.fixme('the add-repo hint names where the internal git URL will appear', async ({ page }) => {
    await page.goto('/');
    await page.getByTestId('add-repo-btn').click();

    const hint = page.locator('.form-hint');
    await expect(hint).toBeVisible();
    await expect(hint).toContainText(/Repository Details|below|sidebar/i);
  });
});

/** One ingested repo, no cards — the state right after a newcomer's first push. */
const REPO = {
  id: 'a1b2c3d4-0000-4000-8000-000000000001',
  name: 'my-first-project',
  remote_url: null,
  default_branch: 'main',
  is_ingested: true,
  internal_git_url: '/git/a1b2c3d4-0000-4000-8000-000000000001.git',
  created_at: naiveUtc(),
};

async function serveOneRepo(page: Page) {
  await page.route('**/api/repos', (r) => r.fulfill({ json: [REPO] }));
  for (const p of ['**/api/runners', '**/api/agent-files']) {
    await page.route(p, (r) => r.fulfill({ json: [] }));
  }
  await page.route(`**/api/repos/${REPO.id}/cards*`, (r) => r.fulfill({ json: [] }));
  await page.route(`**/api/repos/${REPO.id}/branches*`, (r) => r.fulfill({ json: { branches: [] } }));
  await page.route(`**/api/repos/${REPO.id}/clone-url`, (r) =>
    r.fulfill({ json: { clone_url: 'http://localhost:8790/git/x.git', is_ingested: true } })
  );
}

test.describe('LENS 2 — keeping your place', () => {
  test.beforeEach(async ({ page }) => {
    await serveOneRepo(page);
  });

  /**
   * CONFIRMED BUG. `selectedRepoId` is a bare `writable(null)` — not persisted,
   * not in the URL. Selecting a repo leaves the address bar on `/`, so a
   * refresh (or a restored tab, or a shared link) drops the newcomer back onto
   * the "No Repository Selected" dead end with no breadcrumb back.
   */
  test.fixme('a refresh keeps the repo you selected', async ({ page }) => {
    await page.goto('/');
    await page.locator('.repo-item').first().click();
    await expect(page.locator('.board-header h1')).toContainText(REPO.name);

    await page.reload();

    await expect(page.locator('.board-header h1')).toContainText(REPO.name);
    await expect(page.getByTestId('no-repo')).toHaveCount(0);
  });

  /**
   * CONFIRMED BUG. With the 320px sidebar, the four status columns need
   * ~1440px. At 1280x800 — a very common laptop and projector size — the board
   * overflows by ~204px and the DONE column is sliced down its middle, which
   * reads as broken rather than scrollable.
   */
  test.fixme('all four board columns fit at 1280x800', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto('/');
    await page.locator('.repo-item').first().click();
    await expect(page.locator('.column')).toHaveCount(4);

    // `.board` is the grid that scrolls (`overflow-x: auto`). Resolve it
    // explicitly rather than defaulting a missing node to zero — a selector
    // typo must fail this test, not quietly pass it.
    const overflow = await page.evaluate(() => {
      const s = document.querySelector('[data-testid="board"]') as HTMLElement | null;
      if (!s) throw new Error('board scroller not found');
      return s.scrollWidth - s.clientWidth;
    });
    expect(overflow).toBe(0);
  });

  /**
   * The board a newcomer reaches after their first push: a repo, no cards,
   * four empty columns and nothing that says what a card is or that "+ New
   * Card" is how you hand work to an agent. `EndpointsPage` shows the house
   * style for this; the board's busiest empty state has none of it.
   */
  test.fixme('an empty board teaches what a card is', async ({ page }) => {
    await page.goto('/');
    await page.locator('.repo-item').first().click();
    await expect(page.locator('.column')).toHaveCount(4);

    await expect(page.locator('.board')).toContainText(/card|task|agent/i);
  });
});
