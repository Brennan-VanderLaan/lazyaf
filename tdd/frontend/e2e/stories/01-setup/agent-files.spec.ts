/**
 * E2E Tests - Agent Files Management
 *
 * Story: As a user, I need to create and manage agent prompt templates
 * that define how AI agents approach tasks.
 *
 * Run with: pnpm test:e2e --grep "Agent Files"
 */

import { test, expect } from '@playwright/test';

test.describe('Agent Files Management', () => {
  test.describe('Agent Files Panel', () => {
    test.skip('agent files panel is accessible', async ({ page }) => {
      await page.goto('/');

      // Find agent files panel or navigation to it
      const agentPanel = page.locator('[data-testid="agent-panel"]').or(
        page.locator('.agent-panel')
      );

      // Either visible directly or accessible via navigation
    });

    test.skip('shows list of existing agent files', async ({ page }) => {
      // Navigate to agent files
      // Assert: list of agents visible
      // Assert: each shows name and description
    });

    test.skip('shows empty state when no agents exist', async ({ page }) => {
      // With no agents
      // Assert: empty state message
      // Assert: "Create agent" prompt
    });
  });

  test.describe('Create Agent File', () => {
    test.skip('can open create agent modal', async ({ page }) => {
      // Click create agent button
      // Assert: modal opens
    });

    test.skip('agent form has name, description, and content fields', async ({ page }) => {
      // Open modal
      // Assert: name input
      // Assert: description textarea
      // Assert: content/prompt textarea
    });

    test.skip('name is normalized to CLI-safe format', async ({ page }) => {
      // Type "My Cool Agent!" in name
      // Assert: preview shows "my-cool-agent"
    });

    test.skip('can create agent with valid data', async ({ page }) => {
      // Fill form
      // Submit
      // Assert: modal closes
      // Assert: agent appears in list
    });

    test.skip('shows validation error for empty name', async ({ page }) => {
      // Try to submit without name
      // Assert: error shown
    });
  });

  test.describe('Edit Agent File', () => {
    test.skip('can open edit modal for existing agent', async ({ page }) => {
      // Click agent in list
      // Assert: edit modal opens with existing data
    });

    test.skip('can update agent content', async ({ page }) => {
      // Open edit
      // Change content
      // Save
      // Assert: changes persisted
    });

    test.skip('can delete agent', async ({ page }) => {
      // Open agent
      // Click delete
      // Confirm
      // Assert: agent removed from list
    });
  });

  test.describe('Agent Selection in Cards', () => {
    test.skip('card form shows agent selector', async ({ page }) => {
      // Open card create/edit modal
      // Assert: agent selector visible
    });

    test.skip('can attach agent to card', async ({ page }) => {
      // Create/edit card
      // Select agent from dropdown
      // Save
      // Assert: card shows attached agent
    });

    test.skip('shows both platform and repo agents', async ({ page }) => {
      // Open agent selector
      // Assert: platform agents listed
      // Assert: repo agents listed (if repo has .lazyaf/agents/)
      // Assert: repo agents marked with "repo" badge
    });
  });
});
