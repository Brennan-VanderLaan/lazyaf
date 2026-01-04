/**
 * Page Object Models for E2E Tests
 *
 * Encapsulates page interactions and selectors.
 * Using Page Object pattern for maintainability.
 */

import { Page, Locator, expect } from '@playwright/test';

// ==============================================================================
// Base Page
// ==============================================================================

export class BasePage {
  readonly page: Page;
  readonly body: Locator;

  constructor(page: Page) {
    this.page = page;
    this.body = page.locator('body');
  }

  /**
   * Navigate to a path.
   */
  async goto(path: string = '/') {
    await this.page.goto(path);
  }

  /**
   * Wait for page to be fully loaded.
   */
  async waitForLoad() {
    await this.page.waitForLoadState('networkidle');
  }

  /**
   * Check if page has content (not blank).
   */
  async hasContent(): Promise<boolean> {
    const content = await this.body.textContent();
    return (content?.length || 0) > 0;
  }

  /**
   * Get all console errors collected on the page.
   */
  async collectConsoleErrors(action: () => Promise<void>): Promise<string[]> {
    const errors: string[] = [];
    const handler = (msg: { type: () => string; text: () => string }) => {
      if (msg.type() === 'error') {
        errors.push(msg.text());
      }
    };

    this.page.on('console', handler);
    await action();
    this.page.off('console', handler);

    return errors;
  }
}

// ==============================================================================
// Board Page (Main kanban board)
// ==============================================================================

export class BoardPage extends BasePage {
  // Selectors
  readonly repoSelector: Locator;
  readonly todoColumn: Locator;
  readonly inProgressColumn: Locator;
  readonly inReviewColumn: Locator;
  readonly doneColumn: Locator;
  readonly addCardButton: Locator;
  readonly cards: Locator;

  constructor(page: Page) {
    super(page);
    // Note: Update selectors based on actual UI structure
    this.repoSelector = page.locator('[data-testid="repo-selector"], .repo-selector');
    this.todoColumn = page.locator('[data-column="todo"], .column-todo');
    this.inProgressColumn = page.locator('[data-column="in_progress"], .column-in-progress');
    this.inReviewColumn = page.locator('[data-column="in_review"], .column-in-review');
    this.doneColumn = page.locator('[data-column="done"], .column-done');
    this.addCardButton = page.locator('[data-testid="add-card"], button:has-text("Add Card")');
    this.cards = page.locator('[data-testid="card"], .card');
  }

  /**
   * Navigate to board page.
   */
  async goto() {
    await this.page.goto('/');
  }

  /**
   * Select a repository.
   */
  async selectRepo(repoName: string) {
    await this.repoSelector.click();
    await this.page.locator(`text="${repoName}"`).click();
  }

  /**
   * Get all cards in a column.
   */
  getCardsInColumn(column: 'todo' | 'in_progress' | 'in_review' | 'done'): Locator {
    const columnLocator = {
      todo: this.todoColumn,
      in_progress: this.inProgressColumn,
      in_review: this.inReviewColumn,
      done: this.doneColumn,
    }[column];
    return columnLocator.locator('.card, [data-testid="card"]');
  }

  /**
   * Click on a card by title.
   */
  async clickCard(title: string) {
    await this.page.locator(`.card:has-text("${title}")`).click();
  }

  /**
   * Check if a card exists with the given title.
   */
  async hasCard(title: string): Promise<boolean> {
    return this.page.locator(`.card:has-text("${title}")`).isVisible();
  }

  /**
   * Wait for a card to appear in a specific column.
   */
  async waitForCardInColumn(
    title: string,
    column: 'todo' | 'in_progress' | 'in_review' | 'done',
    options: { timeout?: number } = {}
  ) {
    const columnLocator = {
      todo: this.todoColumn,
      in_progress: this.inProgressColumn,
      in_review: this.inReviewColumn,
      done: this.doneColumn,
    }[column];

    await expect(
      columnLocator.locator(`.card:has-text("${title}")`)
    ).toBeVisible({ timeout: options.timeout || 10000 });
  }
}

// ==============================================================================
// Card Modal
// ==============================================================================

export class CardModal extends BasePage {
  readonly modal: Locator;
  readonly titleInput: Locator;
  readonly descriptionInput: Locator;
  readonly stepTypeSelect: Locator;
  readonly commandInput: Locator;
  readonly startButton: Locator;
  readonly approveButton: Locator;
  readonly rejectButton: Locator;
  readonly closeButton: Locator;
  readonly statusBadge: Locator;
  readonly jobStatus: Locator;

  constructor(page: Page) {
    super(page);
    this.modal = page.locator('[data-testid="card-modal"], .card-modal, .modal');
    this.titleInput = this.modal.locator('input[name="title"], [data-testid="title-input"]');
    this.descriptionInput = this.modal.locator(
      'textarea[name="description"], [data-testid="description-input"]'
    );
    this.stepTypeSelect = this.modal.locator('select[name="step_type"]');
    this.commandInput = this.modal.locator('input[name="command"], textarea[name="command"]');
    this.startButton = this.modal.locator('button:has-text("Start"), [data-testid="start-btn"]');
    this.approveButton = this.modal.locator(
      'button:has-text("Approve"), [data-testid="approve-btn"]'
    );
    this.rejectButton = this.modal.locator(
      'button:has-text("Reject"), [data-testid="reject-btn"]'
    );
    this.closeButton = this.modal.locator('.close-btn, button:has-text("Close"), [aria-label="Close"]');
    this.statusBadge = this.modal.locator('.status-badge, [data-testid="status"]');
    this.jobStatus = this.modal.locator('.job-status, [data-testid="job-status"]');
  }

  /**
   * Check if modal is visible.
   */
  async isVisible(): Promise<boolean> {
    return this.modal.isVisible();
  }

  /**
   * Wait for modal to appear.
   */
  async waitForVisible(options: { timeout?: number } = {}) {
    await expect(this.modal).toBeVisible({ timeout: options.timeout || 5000 });
  }

  /**
   * Fill card details.
   */
  async fill(options: { title?: string; description?: string; command?: string }) {
    if (options.title) {
      await this.titleInput.fill(options.title);
    }
    if (options.description) {
      await this.descriptionInput.fill(options.description);
    }
    if (options.command) {
      await this.commandInput.fill(options.command);
    }
  }

  /**
   * Select step type.
   */
  async selectStepType(type: 'script' | 'docker' | 'agent') {
    await this.stepTypeSelect.selectOption(type);
  }

  /**
   * Start the card.
   */
  async start() {
    await this.startButton.click();
  }

  /**
   * Approve the card.
   */
  async approve() {
    await this.approveButton.click();
  }

  /**
   * Close the modal.
   */
  async close() {
    await this.closeButton.click();
  }

  /**
   * Wait for status to change.
   */
  async waitForStatus(status: string, options: { timeout?: number } = {}) {
    await expect(this.statusBadge).toContainText(status, {
      timeout: options.timeout || 10000,
    });
  }
}

// ==============================================================================
// Pipelines Page
// ==============================================================================

export class PipelinesPage extends BasePage {
  readonly pipelineList: Locator;
  readonly addPipelineButton: Locator;
  readonly runsList: Locator;

  constructor(page: Page) {
    super(page);
    this.pipelineList = page.locator('[data-testid="pipeline-list"], .pipeline-list');
    this.addPipelineButton = page.locator(
      'button:has-text("Add Pipeline"), [data-testid="add-pipeline"]'
    );
    this.runsList = page.locator('[data-testid="runs-list"], .runs-list');
  }

  /**
   * Navigate to pipelines page.
   */
  async goto() {
    await this.page.goto('/#/pipelines');
  }

  /**
   * Get all pipeline items.
   */
  getPipelines(): Locator {
    return this.pipelineList.locator('.pipeline-item, [data-testid="pipeline"]');
  }

  /**
   * Click on a pipeline by name.
   */
  async clickPipeline(name: string) {
    await this.pipelineList.locator(`:has-text("${name}")`).click();
  }

  /**
   * Run a pipeline by name.
   */
  async runPipeline(name: string) {
    const pipeline = this.pipelineList.locator(`:has-text("${name}")`);
    await pipeline.locator('button:has-text("Run")').click();
  }
}

// ==============================================================================
// Pipeline Run Viewer
// ==============================================================================

export class PipelineRunViewer extends BasePage {
  readonly viewer: Locator;
  readonly statusBadge: Locator;
  readonly progressBar: Locator;
  readonly stepsList: Locator;
  readonly logsArea: Locator;
  readonly cancelButton: Locator;

  constructor(page: Page) {
    super(page);
    this.viewer = page.locator('[data-testid="run-viewer"], .run-viewer, .modal');
    this.statusBadge = this.viewer.locator('.run-status, [data-testid="run-status"]');
    this.progressBar = this.viewer.locator('.progress-bar');
    this.stepsList = this.viewer.locator('.steps-list, [data-testid="steps"]');
    this.logsArea = this.viewer.locator('.logs, [data-testid="logs"]');
    this.cancelButton = this.viewer.locator('button:has-text("Cancel"), .btn-cancel');
  }

  /**
   * Get all step items.
   */
  getSteps(): Locator {
    return this.stepsList.locator('.step-item, [data-testid="step"]');
  }

  /**
   * Click on a step by name.
   */
  async clickStep(name: string) {
    await this.stepsList.locator(`:has-text("${name}")`).click();
  }

  /**
   * Get current progress text.
   */
  async getProgress(): Promise<string> {
    return (await this.viewer.locator('.progress-text').textContent()) || '';
  }

  /**
   * Wait for run to complete.
   */
  async waitForCompletion(options: { timeout?: number } = {}) {
    await expect(this.statusBadge).toContainText(/passed|failed|cancelled/i, {
      timeout: options.timeout || 60000,
    });
  }

  /**
   * Wait for a specific status.
   */
  async waitForStatus(
    status: 'pending' | 'running' | 'passed' | 'failed' | 'cancelled',
    options: { timeout?: number } = {}
  ) {
    await expect(this.statusBadge).toContainText(status, {
      timeout: options.timeout || 30000,
    });
  }
}

// ==============================================================================
// Navigation
// ==============================================================================

export class Navigation extends BasePage {
  readonly boardLink: Locator;
  readonly pipelinesLink: Locator;
  readonly playgroundLink: Locator;

  constructor(page: Page) {
    super(page);
    // Note: Update based on actual navigation structure
    this.boardLink = page.locator('a[href="/"], nav a:has-text("Board")');
    this.pipelinesLink = page.locator('a[href="#/pipelines"], nav a:has-text("Pipelines")');
    this.playgroundLink = page.locator('a[href="#/playground"], nav a:has-text("Playground")');
  }

  /**
   * Navigate to board.
   */
  async goToBoard() {
    await this.boardLink.click();
  }

  /**
   * Navigate to pipelines.
   */
  async goToPipelines() {
    await this.pipelinesLink.click();
  }
}

// ==============================================================================
// Factory functions
// ==============================================================================

export function createBoardPage(page: Page): BoardPage {
  return new BoardPage(page);
}

export function createCardModal(page: Page): CardModal {
  return new CardModal(page);
}

export function createPipelinesPage(page: Page): PipelinesPage {
  return new PipelinesPage(page);
}

export function createPipelineRunViewer(page: Page): PipelineRunViewer {
  return new PipelineRunViewer(page);
}

export function createNavigation(page: Page): Navigation {
  return new Navigation(page);
}
