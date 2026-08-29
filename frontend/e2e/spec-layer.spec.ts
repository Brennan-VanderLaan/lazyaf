/**
 * E2E Tests: Spec Layer UI (Phase 12.2.5, standing rule R8)
 *
 * Covers the Specs page and the CardModal story link:
 * 1. Seed Milestone 12 -> the three north-star user stories render with
 *    their acceptance criteria (US-1 dogfood CI, US-2 card dev loop,
 *    US-3 compare bench).
 * 2. Inline creation: feature -> story -> criterion, no deep modals.
 * 3. Linking a card to a user story through the CardModal selector.
 *
 * Prerequisites: the compose e2e stack with LAZYAF_TEST_MODE=true (see
 * e2e/README.md). URLs come from BACKEND_URL/FRONTEND_URL env vars.
 */

import { test, expect, type Page } from '@playwright/test';
import { BACKEND_URL, resetBackend, createTestRepo, selectRepo } from './helpers';

/** Substrings of the three north-star story titles from the PLAN.md roadmap. */
const NORTH_STAR_STORIES = ['Commits land', 'Card dev loop', 'Compare bench'];

async function gotoSpecsPage(page: Page) {
  await page.goto('/#/specs');
  await expect(page.locator('[data-testid="specs-page"]')).toBeVisible({ timeout: 5000 });
}

/** Expand every collapsed feature row currently on the page. */
async function expandAllFeatures(page: Page) {
  const collapsed = page.locator('[data-testid="feature-item"][data-expanded="false"] > .feature-row > [data-testid="feature-toggle"]');
  while (await collapsed.count() > 0) {
    await collapsed.first().click();
  }
}

test.describe('Spec layer (Phase 12.2.5)', () => {
  test.beforeEach(async ({ request }) => {
    await resetBackend(request);
  });

  test('empty spec list offers the Seed Milestone 12 button', async ({ page }) => {
    await gotoSpecsPage(page);
    await expect(page.locator('[data-testid="seed-milestone12-btn"]')).toBeVisible({ timeout: 5000 });
    await expect(page.locator('[data-testid="feature-item"]')).toHaveCount(0);
  });

  test('seeding renders the three north-star stories with criteria', async ({ page }) => {
    await gotoSpecsPage(page);

    await page.locator('[data-testid="seed-milestone12-btn"]').click();

    // Features appear after the seed + reload round-trip
    await expect(page.locator('[data-testid="feature-item"]').first()).toBeVisible({ timeout: 10000 });

    await expandAllFeatures(page);

    // Exactly the three roadmap stories
    const stories = page.locator('[data-testid="story-item"]');
    await expect(stories).toHaveCount(3, { timeout: 10000 });
    for (const title of NORTH_STAR_STORIES) {
      await expect(
        page.locator('[data-testid="story-item"]').filter({ hasText: title })
      ).toBeVisible();
    }

    // Each story expands to at least one acceptance criterion
    for (let i = 0; i < 3; i++) {
      const story = stories.nth(i);
      await story.locator('[data-testid="story-toggle"]').click();
      await expect(story.locator('[data-testid="criterion-item"]').first()).toBeVisible({
        timeout: 5000,
      });
    }
  });

  test('creates a feature, story, and criterion inline', async ({ page }) => {
    await gotoSpecsPage(page);

    // Feature: inline create form, no deep modal
    await page.locator('[data-testid="add-feature-btn"]').click();
    await page.fill('[data-testid="new-feature-title-input"]', 'Inline Feature E2E');
    await page.fill('[data-testid="new-feature-description-input"]', 'Created from the e2e spec');
    await page.locator('[data-testid="create-feature-btn"]').click();

    const feature = page
      .locator('[data-testid="feature-item"]')
      .filter({ hasText: 'Inline Feature E2E' });
    await expect(feature).toBeVisible({ timeout: 5000 });
    // Newly created features auto-expand so a story can be added immediately
    await expect(feature).toHaveAttribute('data-expanded', 'true');
    await expect(feature.locator('[data-testid="feature-story-count"]')).toContainText('0');

    // Story: inline add row inside the expanded feature
    await feature.locator('[data-testid="new-story-title-input"]').fill('Inline Story E2E');
    await feature.locator('[data-testid="add-story-btn"]').click();

    const story = feature
      .locator('[data-testid="story-item"]')
      .filter({ hasText: 'Inline Story E2E' });
    await expect(story).toBeVisible({ timeout: 5000 });
    await expect(feature.locator('[data-testid="feature-story-count"]')).toContainText('1');

    // Criterion: expand the story, inline add row
    await story.locator('[data-testid="story-toggle"]').click();
    await story.locator('[data-testid="new-criterion-text-input"]').fill('E2E criterion holds');
    await story.locator('[data-testid="add-criterion-btn"]').click();

    await expect(
      story.locator('[data-testid="criterion-item"]').filter({ hasText: 'E2E criterion holds' })
    ).toBeVisible({ timeout: 5000 });

    // Survives a reload (persisted, not local-only state)
    await page.reload();
    await expect(
      page.locator('[data-testid="feature-item"]').filter({ hasText: 'Inline Feature E2E' })
    ).toBeVisible({ timeout: 5000 });
  });

  test('links a card to a seeded user story via the CardModal', async ({ page }) => {
    // Seed specs and a repo + card via API (faster than UI for setup)
    const seedResponse = await page.request.post(`${BACKEND_URL}/api/features/seed-milestone12`);
    expect(seedResponse.ok(), `seed-milestone12 failed: ${await seedResponse.text()}`).toBeTruthy();

    const { id: repoId, name: repoName } = await createTestRepo(page, 'e2e-spec-link');
    const cardResponse = await page.request.post(`${BACKEND_URL}/api/repos/${repoId}/cards`, {
      data: { title: 'Spec Link Card', description: 'Link me to a story' },
    });
    expect(cardResponse.ok()).toBeTruthy();
    const card = await cardResponse.json();

    await page.goto('/');
    await selectRepo(page, repoName);

    // Open the card and pick a story in the link selector
    await page.locator('.card').filter({ hasText: 'Spec Link Card' }).click();
    await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();

    const select = page.locator('[data-testid="link-story-select"]');
    await expect(select).toBeVisible({ timeout: 5000 });
    const option = select.locator('option').filter({ hasText: 'Card dev loop' });
    const storyId = await option.getAttribute('value');
    expect(storyId).toBeTruthy();
    await select.selectOption(storyId!);

    await page.locator('[data-testid="save-card-btn"]').click();
    await expect(page.locator('[data-testid="card-modal"]')).not.toBeVisible({ timeout: 5000 });

    // Link persisted on the card (read schema carries the ids)
    const cardCheck = await page.request.get(`${BACKEND_URL}/api/cards/${card.id}`);
    expect(cardCheck.ok()).toBeTruthy();
    const updated = await cardCheck.json();
    expect(updated.user_story_id).toBe(storyId);
    expect(updated.feature_id).toBeTruthy();

    // Reopening the card shows the linked feature/story
    await page.locator('.card').filter({ hasText: 'Spec Link Card' }).click();
    await expect(page.locator('[data-testid="card-modal"]')).toBeVisible();
    await expect(page.locator('[data-testid="card-spec-link"]')).toContainText('Card dev loop', {
      timeout: 5000,
    });
  });
});
