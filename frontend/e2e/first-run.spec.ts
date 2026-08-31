/**
 * FIRST RUN — the cold-open journey, from an install with nothing in it to a
 * board that has told the newcomer what to do next.
 *
 * This is the regression guard for onboarding guidance (R8). Every assertion
 * here is about something a STRANGER needs on screen: a control where there
 * was only prose, a reason where a failure was silent, a sentence naming what
 * a "card" is. If someone later trims one of these strings back out, this
 * spec is what says so.
 *
 * WHY FIXTURES, NOT A LIVE BACKEND. The rest of the top-level e2e tier talks
 * to a real stack, but a cold open is by definition "the repo list is empty",
 * and the shared QA backend is reset and re-seeded by other agents while a
 * spec runs — a cold open cannot be held still there. Everything asserted
 * below is frontend guidance, so `page.route` is both sufficient and the only
 * way to make it deterministic. Payload shapes are byte-faithful to the QA
 * stack on 2026-08-30 (GET /api/repos, /clone-url, /branches, /cards).
 *
 * The one thing this spec deliberately does NOT do is run an agent. A real
 * first agent run needs a model credential, which is precisely what a cold
 * install lacks (probe finding L2-01) — so the journey is asserted up to and
 * including the guidance that warns about it, which is the part this lane
 * owns and can keep honest.
 *
 * Run with:
 *   cd frontend
 *   FRONTEND_URL=http://localhost:5182 npx playwright test e2e/first-run.spec.ts
 */
import { test, expect, type Page } from '@playwright/test';

/** The backend serializes `datetime.utcnow().isoformat()` — no trailing 'Z'. */
function naiveUtc(offsetMs = 0): string {
  return new Date(Date.now() + offsetMs).toISOString().replace('Z', '');
}

interface FixtureRepo {
  id: string;
  name: string;
  remote_url: string | null;
  default_branch: string;
  is_ingested: boolean;
  internal_git_url: string;
  created_at: string;
}

interface InstallState {
  repos: FixtureRepo[];
  /** repoId -> cards. Absent means "this repo has no cards". */
  cards: Record<string, unknown[]>;
  /** repoId -> branch names. Absent means "nothing has been pushed yet". */
  branches: Record<string, string[]>;
  /** When set, POST /api/repos/ingest refuses with this FastAPI 422 message. */
  refuseIngestWith?: string;
  /** When true, GET /api/repos fails the way an unreachable backend does. */
  reposUnreachable?: boolean;
}

function emptyInstall(): InstallState {
  return { repos: [], cards: {}, branches: {} };
}

function makeRepo(name: string, id: string): FixtureRepo {
  return {
    id,
    name,
    remote_url: null,
    default_branch: 'main',
    is_ingested: true,
    internal_git_url: `/git/${id}.git`,
    created_at: naiveUtc(),
  };
}

/**
 * One handler for the whole API surface, dispatching on pathname.
 *
 * Deliberately NOT several `page.route` globs: Playwright resolves overlapping
 * routes in reverse registration order, which makes `**./api/repos` vs
 * `**./api/repos/ingest` an ordering puzzle that breaks silently when someone
 * adds a route. Anything unrouted returns a loud 404 naming the method and
 * path, so a missing fixture shows up as a visible failure rather than a hang.
 *
 * The matcher is a PREDICATE, not a glob. A glob of `** /api/** ` also matches
 * the dev server's own module URLs — `/src/lib/api/client.ts` contains
 * `/api/` — so it answers the app's source files with fixture JSON and the
 * page renders blank white with no error. Match the backend prefix exactly.
 */
async function serveInstall(page: Page, state: InstallState) {
  await page.route((url) => url.pathname.startsWith('/api/'), async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === '/api/repos' && method === 'GET') {
      if (state.reposUnreachable) {
        return route.abort('connectionrefused');
      }
      return route.fulfill({ json: state.repos });
    }

    if (path === '/api/repos/ingest' && method === 'POST') {
      if (state.refuseIngestWith) {
        return route.fulfill({
          status: 422,
          json: {
            detail: [
              {
                type: 'string_too_long',
                loc: ['body', 'name'],
                msg: state.refuseIngestWith,
              },
            ],
          },
        });
      }
      const body = request.postDataJSON() as { name: string };
      const repo = makeRepo(body.name, `11111111-2222-3333-4444-${String(state.repos.length).padStart(12, '0')}`);
      state.repos.push(repo);
      return route.fulfill({
        json: {
          id: repo.id,
          name: repo.name,
          internal_git_url: repo.internal_git_url,
          clone_url: `http://localhost:8790${repo.internal_git_url}`,
        },
      });
    }

    const repoMatch = path.match(/^\/api\/repos\/([^/]+)(\/[^?]*)?$/);
    if (repoMatch && method === 'GET') {
      const [, repoId, sub] = repoMatch;
      const repo = state.repos.find((r) => r.id === repoId);
      if (!repo) return route.fulfill({ status: 404, json: { detail: 'Repository not found' } });

      if (!sub) return route.fulfill({ json: repo });

      if (sub === '/clone-url') {
        return route.fulfill({
          json: { clone_url: `http://localhost:8790${repo.internal_git_url}`, is_ingested: true },
        });
      }
      if (sub === '/branches') {
        const names = state.branches[repoId] ?? [];
        return route.fulfill({
          json: {
            branches: names.map((name) => ({
              name,
              commit: '275f8877a977bce4dba868a1bbf8f5352235e5b3',
              is_default: name === repo.default_branch,
              is_lazyaf: name.startsWith('lazyaf/'),
            })),
            default_branch: repo.default_branch,
            total: names.length,
          },
        });
      }
      if (sub === '/cards') {
        return route.fulfill({ json: state.cards[repoId] ?? [] });
      }
      if (sub === '/commits') {
        return route.fulfill({ json: { commits: [], total: 0 } });
      }
    }

    if ((path === '/api/runners' || path === '/api/agent-files') && method === 'GET') {
      return route.fulfill({ json: [] });
    }

    return route.fulfill({
      status: 404,
      json: { detail: `first-run.spec.ts has no fixture for ${method} ${path}` },
    });
  });
}

test.describe('first run — the cold open', () => {
  test('the board teaches the next three steps and offers a control', async ({ page }) => {
    await serveInstall(page, emptyInstall());
    await page.goto('/');

    const board = page.getByTestId('no-repo');
    await expect(board).toBeVisible();

    // COMPATIBILITY, deliberate. The heading and the `.no-repo` class are kept
    // exactly as they were: `card-workflow.spec.ts` asserts both, and the
    // owner knows this screen — the confusing part was never the title, it
    // was the missing control and the impossible instruction. Guidance was
    // added around it rather than renaming what already exists.
    await expect(page.locator('.no-repo')).toContainText('No Repository Selected');

    // The sidebar is simultaneously asserting there is nothing to select...
    await expect(page.locator('.repo-empty')).toHaveText('No repositories added yet');
    // ...so the board must not send a stranger looking for one.
    await expect(board).not.toContainText('Select a repository from the sidebar');

    // Exactly one thing to press, and it says what it does.
    const control = board.locator('button, a');
    await expect(control).toHaveCount(1);
    await expect(control).toHaveText('Add your first repository');

    // The three steps that were nowhere in the product before.
    const steps = board.getByTestId('first-run-steps').locator('li');
    await expect(steps).toHaveCount(3);
    await expect(steps.nth(0)).toContainText('Add a repository');
    await expect(steps.nth(1)).toContainText('Push your code');
    await expect(steps.nth(2)).toContainText('Create a card');

    // A card must be defined where it is first mentioned, not after it fails.
    await expect(steps.nth(2)).toContainText('one task you hand to an agent');

    // L2-01: the credential that dead-ends the journey is named UP FRONT,
    // instead of ten minutes later inside a failed card.
    await expect(board).toContainText('ANTHROPIC_API_KEY');
  });

  test('the board control opens and focuses the real add-repo form', async ({ page }) => {
    await serveInstall(page, emptyInstall());
    await page.goto('/');

    // The form is not open to begin with — the `+` is the only other way in.
    await expect(page.getByTestId('repo-name-input')).toHaveCount(0);

    await page.getByTestId('first-run-add-repo').click();

    const nameInput = page.getByTestId('repo-name-input');
    await expect(nameInput).toBeVisible();
    // Sent here from the other side of the screen, so the cursor comes too.
    await expect(nameInput).toBeFocused();
  });

  test('every field in the add-repo form is labelled', async ({ page }) => {
    await serveInstall(page, emptyInstall());
    await page.goto('/');
    await page.getByTestId('first-run-add-repo').click();

    // Placeholder-as-label vanishes the moment you type, and the default
    // branch field is pre-filled with "main" so its placeholder never showed
    // at all — a box containing a word, with nothing saying what it is.
    for (const [id, label] of [
      ['repo-name', 'Repository name'],
      ['repo-remote-url', 'Remote URL'],
      ['repo-default-branch', 'Default branch'],
    ]) {
      const labelEl = page.locator(`label[for="${id}"]`);
      await expect(labelEl).toBeVisible();
      await expect(labelEl).toContainText(label);
      await expect(page.locator(`#${id}`)).toHaveCount(1);
    }
  });
});

test.describe('first run — when the first action fails', () => {
  /**
   * R1. This was a bare `try/finally` with no `catch`: a refused create
   * flipped the button from "Creating..." back to "Create Repo" and said
   * NOTHING, leaving the newcomer with a button that appears to do nothing on
   * the very first click they make. The store already re-throws for this.
   */
  test('a refused repo create says why, in the backend\'s own words', async ({ page }) => {
    const state = emptyInstall();
    state.refuseIngestWith = 'String should have at most 200 characters';
    await serveInstall(page, state);
    await page.goto('/');

    await page.getByTestId('first-run-add-repo').click();
    await page.getByTestId('repo-name-input').fill('a-name-the-backend-will-refuse');
    await page.getByRole('button', { name: 'Create Repo' }).click();

    const error = page.getByTestId('repo-create-error');
    await expect(error).toBeVisible();
    await expect(error).toContainText('String should have at most 200 characters');

    // The form stays open with the typed name in it, so the refusal is
    // something you can act on rather than start over from.
    await expect(page.getByTestId('repo-name-input')).toHaveValue('a-name-the-backend-will-refuse');

    // And the board must NOT also claim the repo list is unreachable. Caught
    // by looking at a screenshot, not by a test: the board first read
    // `reposStore.error`, which is the last error from ANY repo operation, so
    // a refused CREATE painted "Could not load your repositories" across the
    // page while the form underneath was already explaining the real problem.
    await expect(page.getByTestId('repos-unavailable')).toHaveCount(0);
    await expect(page.getByTestId('no-repo')).toBeVisible();
  });

  /**
   * R1 again, the other direction: an unreachable backend must not be dressed
   * up as an empty account. "Add your first repository" is a lie when the
   * reason you see no repositories is that nothing answered.
   */
  test('an unreachable backend is not reported as an empty account', async ({ page }) => {
    const state = emptyInstall();
    state.reposUnreachable = true;
    await serveInstall(page, state);
    await page.goto('/');

    const failed = page.getByTestId('repos-unavailable');
    await expect(failed).toBeVisible();
    await expect(failed).toContainText('Could not load your repositories');
    await expect(page.getByTestId('no-repo')).toHaveCount(0);
    await expect(page.getByTestId('first-run-add-repo')).toHaveCount(0);
  });
});

test.describe('first run — the first repository', () => {
  test('creating it lands on a board that says what a card is', async ({ page }) => {
    await serveInstall(page, emptyInstall());
    await page.goto('/');

    await page.getByTestId('first-run-add-repo').click();
    await page.getByTestId('repo-name-input').fill('my-project');
    await page.getByRole('button', { name: 'Create Repo' }).click();

    // The new repo is selected for you, so the board is the next thing seen.
    await expect(page.locator('.board-header h1')).toContainText('my-project');

    const hint = page.getByTestId('empty-board-hint');
    await expect(hint).toBeVisible();
    // Four empty columns and a `+ New Card` button teach nothing on their
    // own. Name the noun, the verb, and where the result turns up.
    await expect(hint).toContainText('one task you hand to an agent');
    await expect(hint).toContainText('Create & Submit');
    await expect(hint).toContainText('In Review');
    // And where the push commands are, since they are below the fold.
    await expect(hint).toContainText('Repository Details');
  });

  /**
   * Guidance that outstays its welcome is nagging. This one is bound to the
   * state it explains, so it has no dismiss button and needs none.
   *
   * HONEST LABEL: unlike its eight siblings, this test also passed against the
   * pre-fix code — trivially, because no hint existed to be absent. It is a
   * FORWARD guard (nobody may make the hint unconditional), not evidence of a
   * bug that was fixed. The eight others were each confirmed to fail against
   * the original files before this lane's changes (R4).
   */
  test('the board hint goes away once the repository has a card', async ({ page }) => {
    const state = emptyInstall();
    const repo = makeRepo('has-a-card', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee');
    state.repos.push(repo);
    state.branches[repo.id] = ['main'];
    state.cards[repo.id] = [
      {
        id: 'cccccccc-dddd-eeee-ffff-000000000000',
        repo_id: repo.id,
        title: 'Seed card (todo)',
        description: 'Deterministic seed card ready to start',
        status: 'todo',
        runner_type: 'mock',
        step_type: 'agent',
        step_config: { task: 'Write a short greeting to README.md' },
        prompt_template: null,
        agent_file_ids: null,
        branch_name: null,
        pr_url: null,
        job_id: null,
        created_at: naiveUtc(-60_000),
        updated_at: naiveUtc(-60_000),
      },
    ];
    await serveInstall(page, state);
    await page.goto('/');

    await page.locator('.repo-item').first().click();
    await expect(page.locator('.board-header h1')).toContainText('has-a-card');
    await expect(page.getByTestId('empty-board-hint')).toHaveCount(0);
  });

  /**
   * L2-04 / L4-07 / PG-16 — one fact, filed by three lenses. A refresh used to
   * drop you back on the empty state with no breadcrumb, which on a cold
   * install reads as "it forgot everything I just did".
   */
  test('the repository you selected survives a reload', async ({ page }) => {
    await serveInstall(page, emptyInstall());
    await page.goto('/');

    await page.getByTestId('first-run-add-repo').click();
    await page.getByTestId('repo-name-input').fill('remembered-repo');
    await page.getByRole('button', { name: 'Create Repo' }).click();
    await expect(page.locator('.board-header h1')).toContainText('remembered-repo');

    await page.reload();

    await expect(page.locator('.board-header h1')).toContainText('remembered-repo');
    await expect(page.getByTestId('no-repo')).toHaveCount(0);
    await expect(page.locator('.repo-item.selected')).toHaveCount(1);
  });

  /**
   * L2-10. `Or checkout the branch directly:` sat OUTSIDE the `{#if
   * selectedBranch}` guard holding its command, so a repo with nothing pushed
   * yet — exactly a new user's state — rendered a label introducing nothing,
   * which reads as a command that failed to load.
   */
  test('a repository with nothing pushed shows no orphaned instructions', async ({ page }) => {
    const state = emptyInstall();
    const repo = makeRepo('nothing-pushed', 'ffffffff-0000-1111-2222-333333333333');
    state.repos.push(repo);
    // No entry in state.branches: the bare repo exists, nothing was pushed.
    await serveInstall(page, state);
    await page.goto('/');

    await page.locator('.repo-item').first().click();
    await expect(page.locator('.board-header h1')).toContainText('nothing-pushed');

    const panel = page.locator('.repo-info-panel');
    await expect(panel).toBeVisible();
    await expect(panel).toContainText('No branches yet');
    await expect(panel).not.toContainText('Or checkout the branch directly');
  });
});
