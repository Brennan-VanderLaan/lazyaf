/**
 * BRANCHES — "I need to be able to see which branches are available in lazyaf".
 *
 * The reported defect, in the owner's own session: he created a repo, pushed
 * it, and Repository Details went on saying "No branches yet. Push your repo
 * to get started." A reload fixed it. The cause was that branches live in the
 * internal repo's REFS — a push moves refs and changes no database row, so
 * nothing was broadcast and the open page had no reason to look again.
 *
 * So the load-bearing assertion here is a REAL `git push`, from a real temp
 * repo, into the real backend, with the page already open and never reloaded.
 * A fixture cannot prove that: the whole question is whether the backend
 * announces a ref write, and whether the panel is listening.
 *
 * The rest is what a human needs to read off the list — which branches exist,
 * which is default, which came from an agent (the `lazyaf/` prefix), and the
 * tip commit — plus the two states that are NOT a list: an empty repo, which
 * must name the remedy, and a failed listing, which must not masquerade as an
 * empty repo.
 *
 * This spec creates and deletes its own repo and never calls /api/test/reset,
 * so it is safe to run against a QA backend other lanes are using.
 *
 * Run with:
 *   cd frontend
 *   BACKEND_URL=http://localhost:8790 FRONTEND_URL=http://localhost:5174 \
 *     npx playwright test e2e/branches.spec.ts
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { test, expect, type Page } from '@playwright/test';

import { BACKEND_URL, selectRepo } from './helpers';

/** A local git repo we can push from. Returned path is disposable. */
function makeLocalRepo(): string {
  const dir = mkdtempSync(join(tmpdir(), 'lazyaf-branches-'));
  const git = (...args: string[]) =>
    execFileSync('git', args, { cwd: dir, stdio: 'pipe' });

  git('init', '-b', 'main');
  git('config', 'user.email', 'e2e@lazyaf.test');
  git('config', 'user.name', 'LazyAF E2E');
  writeFileSync(join(dir, 'README.md'), '# branches spec\n');
  git('add', '-A');
  git('commit', '-m', 'initial commit');
  return dir;
}

/**
 * Commit on `branch` and push it to the repo's internal git URL.
 *
 * Built from BACKEND_URL rather than the clone URL the UI shows: the browser
 * reaches the backend through the vite proxy, so the URL rendered on screen is
 * whatever the backend thinks its own base is, which is not necessarily
 * routable from this process.
 */
function pushBranch(dir: string, repoId: string, branch: string, file: string): string {
  const git = (...args: string[]) =>
    execFileSync('git', args, { cwd: dir, stdio: 'pipe' }).toString();

  try {
    git('checkout', branch);
  } catch {
    git('checkout', '-b', branch);
  }
  writeFileSync(join(dir, file), `work on ${branch}\n`);
  git('add', '-A');
  git('commit', '-m', `work on ${branch}`);
  git('push', `${BACKEND_URL}/git/${repoId}.git`, branch);
  return git('rev-parse', 'HEAD').trim();
}

/** One row of the branch list, by branch name. */
function branchRow(page: Page, name: string) {
  return page.locator(`[data-testid="branch-row"][data-branch="${name}"]`);
}

test.describe('Repository Details branch list', () => {
  let repoId = '';
  let repoName = '';
  let localRepo = '';

  test.beforeEach(async ({ request }) => {
    // An INGESTED repo with nothing pushed to it - exactly the state the owner
    // was in. `ingest` with no path creates the bare repo and stops there.
    repoName = `e2e-branches-${Date.now()}`;
    const response = await request.post(`${BACKEND_URL}/api/repos/ingest`, {
      data: { name: repoName, default_branch: 'main' },
    });
    expect(response.ok(), await response.text()).toBeTruthy();
    repoId = (await response.json()).id;
    localRepo = makeLocalRepo();
  });

  test.afterEach(async ({ request }) => {
    if (repoId) await request.delete(`${BACKEND_URL}/api/repos/${repoId}`);
    if (localRepo) rmSync(localRepo, { recursive: true, force: true });
    repoId = '';
    localRepo = '';
  });

  test('a repo with no branches says so, and names the remedy', async ({ page }) => {
    await page.goto('/');
    await selectRepo(page, repoName);

    const empty = page.getByTestId('branches-empty');
    await expect(empty).toBeVisible();

    // It has to read as a step not yet taken. Naming the state alone was the
    // old copy; what a newcomer needs is where the commands are.
    await expect(empty).toContainText('No branches here yet');
    await expect(empty).toContainText('Push Updates');

    // ...and those commands are on screen, copyable, right below it.
    await expect(page.locator('code', { hasText: `git remote add lazyaf` })).toBeVisible();

    // The empty state is NOT the failure state. If a fetch had failed, the
    // panel would be showing the other one.
    await expect(page.getByTestId('branches-error')).toHaveCount(0);
  });

  test('a push shows up in the list without a reload', async ({ page }) => {
    await page.goto('/');
    await selectRepo(page, repoName);
    await expect(page.getByTestId('branches-empty')).toBeVisible();

    // THE REGRESSION. Everything below happens with the page untouched: no
    // reload, no click, no navigation. If the backend stops broadcasting the
    // ref write, or the panel stops listening, this times out.
    const sha = pushBranch(localRepo, repoId, 'main', 'first.txt');

    await expect(branchRow(page, 'main')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('branches-empty')).toHaveCount(0);

    // Which one is default, and where its tip is.
    await expect(branchRow(page, 'main').getByTestId('branch-badge-default')).toBeVisible();
    await expect(branchRow(page, 'main')).toContainText(sha.slice(0, 7));
  });

  test('an agent branch arrives live and is marked as one', async ({ page }) => {
    pushBranch(localRepo, repoId, 'main', 'first.txt');

    await page.goto('/');
    await selectRepo(page, repoName);
    await expect(branchRow(page, 'main')).toBeVisible({ timeout: 15_000 });

    // The `lazyaf/` prefix is how a human tells agent work from their own,
    // and an agent branch appears while they are looking at the board.
    const agentSha = pushBranch(localRepo, repoId, 'lazyaf/ab12cd34', 'agent.txt');

    const agentRow = branchRow(page, 'lazyaf/ab12cd34');
    await expect(agentRow).toBeVisible({ timeout: 15_000 });
    await expect(agentRow.getByTestId('branch-badge-agent')).toBeVisible();
    await expect(agentRow).toContainText(agentSha.slice(0, 7));
    await expect(agentRow.getByTestId('branch-badge-default')).toHaveCount(0);

    // The count in the section header is part of the answer to "which
    // branches are available", and it counts the agent ones separately.
    await expect(page.getByTestId('branches-section')).toContainText('1 from agents');
  });

  test('a deleted branch disappears live', async ({ page, request }) => {
    pushBranch(localRepo, repoId, 'main', 'first.txt');
    pushBranch(localRepo, repoId, 'lazyaf/short-lived', 'temp.txt');

    await page.goto('/');
    await selectRepo(page, repoName);
    await expect(branchRow(page, 'lazyaf/short-lived')).toBeVisible({ timeout: 15_000 });

    const deleted = await request.delete(
      `${BACKEND_URL}/api/repos/${repoId}/branches/lazyaf/short-lived`,
    );
    expect(deleted.ok(), await deleted.text()).toBeTruthy();

    // A branch the backend no longer has must stop being offered here, or the
    // panel invites a checkout of a ref that does not exist.
    await expect(branchRow(page, 'lazyaf/short-lived')).toHaveCount(0, { timeout: 15_000 });
    await expect(branchRow(page, 'main')).toBeVisible();
  });

  test('selecting a branch shows that branch history', async ({ page }) => {
    pushBranch(localRepo, repoId, 'main', 'first.txt');
    pushBranch(localRepo, repoId, 'lazyaf/has-own-commit', 'agent.txt');

    await page.goto('/');
    await selectRepo(page, repoName);
    await expect(branchRow(page, 'lazyaf/has-own-commit')).toBeVisible({ timeout: 15_000 });

    await branchRow(page, 'lazyaf/has-own-commit').click();

    // The commit only this branch carries.
    await expect(page.locator('.git-graph')).toContainText('work on lazyaf/has-own-commit', {
      timeout: 15_000,
    });
  });

  test('a listing that fails says so instead of claiming the repo is empty', async ({ page }) => {
    // R1. Folded into "no branches", a failed fetch told the user to push a
    // repo they had already pushed - the remedy for a state they were not in.
    await page.route(`**/api/repos/${repoId}/branches`, route =>
      route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'git storage unreadable' }),
      }),
    );

    await page.goto('/');
    await selectRepo(page, repoName);

    const failure = page.getByTestId('branches-error');
    await expect(failure).toBeVisible();
    await expect(failure).toContainText('Could not list branches');
    await expect(failure).toContainText('git storage unreadable');

    // The two states must never be confusable.
    await expect(page.getByTestId('branches-empty')).toHaveCount(0);
    await expect(failure).not.toContainText('No branches here yet');

    // And it offers a way forward rather than only a complaint.
    await expect(failure.getByRole('button', { name: 'Retry' })).toBeVisible();
  });

  test('Retry recovers once the listing works again', async ({ page }) => {
    let failNext = true;
    await page.route(`**/api/repos/${repoId}/branches`, async route => {
      if (failNext) {
        failNext = false;
        await route.fulfill({
          status: 503,
          contentType: 'application/json',
          body: JSON.stringify({ detail: 'backend restarting' }),
        });
        return;
      }
      await route.continue();
    });

    pushBranch(localRepo, repoId, 'main', 'first.txt');

    await page.goto('/');
    await selectRepo(page, repoName);
    await expect(page.getByTestId('branches-error')).toBeVisible();

    await page.getByTestId('branches-error').getByRole('button', { name: 'Retry' }).click();

    await expect(branchRow(page, 'main')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByTestId('branches-error')).toHaveCount(0);
  });
});
