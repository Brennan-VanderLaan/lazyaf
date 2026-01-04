/**
 * API Helpers for E2E Tests
 *
 * Utilities for interacting with the backend test API.
 * Used by real tier tests to:
 * - Reset database between tests
 * - Seed test data
 * - Create test entities
 */

import { APIRequestContext, expect } from '@playwright/test';

// Read at runtime to ensure env var is available
const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';

// ==============================================================================
// Types
// ==============================================================================

export interface TestRepo {
  id: string;
  name: string;
  default_branch: string;
  is_ingested: boolean;
}

export interface TestCard {
  id: string;
  repo_id: string;
  title: string;
  status: string;
  step_type: string;
}

export interface TestPipeline {
  id: string;
  repo_id: string;
  name: string;
  steps_count: number;
}

export interface SeedResult {
  success: boolean;
  message: string;
  created: Record<string, unknown>;
}

export interface ResetResult {
  success: boolean;
  message: string;
  tables_cleared: string[];
}

export interface TestState {
  repos: number;
  cards: number;
  jobs: number;
  pipelines: number;
  pipeline_runs: number;
}

// ==============================================================================
// Test API Client
// ==============================================================================

export class TestApiClient {
  private baseUrl: string;
  private request: APIRequestContext;

  constructor(request: APIRequestContext, baseUrl: string = BACKEND_URL) {
    this.baseUrl = baseUrl;
    this.request = request;
  }

  /**
   * Reset the database to a clean state.
   * Should be called before each test or test file.
   */
  async reset(): Promise<ResetResult> {
    const response = await this.request.post(`${this.baseUrl}/api/test/reset`);
    expect(response.ok(), `Reset failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Seed the database with a predefined scenario.
   */
  async seed(
    scenario: 'basic' | 'card_workflow' | 'pipeline_workflow' = 'basic',
    options: Record<string, unknown> = {}
  ): Promise<SeedResult> {
    const response = await this.request.post(`${this.baseUrl}/api/test/seed`, {
      data: { scenario, options },
    });
    expect(response.ok(), `Seed failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Get current database state (entity counts).
   */
  async getState(): Promise<TestState> {
    const response = await this.request.get(`${this.baseUrl}/api/test/state`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * Create a test repo directly.
   */
  async createRepo(name: string = 'test-repo', defaultBranch: string = 'main'): Promise<TestRepo> {
    const response = await this.request.post(`${this.baseUrl}/api/test/repos`, {
      data: { name, default_branch: defaultBranch },
    });
    expect(response.ok(), `Create repo failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Create a test card in a repo.
   */
  async createCard(
    repoId: string,
    options: {
      title?: string;
      description?: string;
      step_type?: 'script' | 'docker';
      step_config?: Record<string, unknown>;
    } = {}
  ): Promise<TestCard> {
    const response = await this.request.post(`${this.baseUrl}/api/test/cards`, {
      data: {
        repo_id: repoId,
        title: options.title || 'Test Card',
        description: options.description || 'Test card description',
        step_type: options.step_type || 'script',
        step_config: options.step_config,
      },
    });
    expect(response.ok(), `Create card failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Create a test pipeline in a repo.
   */
  async createPipeline(
    repoId: string,
    options: {
      name?: string;
      description?: string;
      steps?: Array<{
        name: string;
        type: 'script' | 'docker';
        config: Record<string, unknown>;
        on_success?: string;
        on_failure?: string;
        timeout?: number;
      }>;
    } = {}
  ): Promise<TestPipeline> {
    const response = await this.request.post(`${this.baseUrl}/api/test/pipelines`, {
      data: {
        repo_id: repoId,
        name: options.name || 'Test Pipeline',
        description: options.description || 'Test pipeline for E2E',
        steps: options.steps || [],
      },
    });
    expect(response.ok(), `Create pipeline failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Check if test API is available.
   */
  async isAvailable(): Promise<boolean> {
    try {
      const response = await this.request.get(`${this.baseUrl}/api/test/health`);
      return response.ok();
    } catch {
      return false;
    }
  }
}

// ==============================================================================
// Production API Client (for real API calls)
// ==============================================================================

export class ProductionApiClient {
  private baseUrl: string;
  private request: APIRequestContext;

  constructor(request: APIRequestContext, baseUrl: string = BACKEND_URL) {
    this.baseUrl = baseUrl;
    this.request = request;
  }

  /**
   * Get health status.
   */
  async health(): Promise<{ status: string; app: string }> {
    const response = await this.request.get(`${this.baseUrl}/health`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * List all repos.
   */
  async listRepos(): Promise<unknown[]> {
    const response = await this.request.get(`${this.baseUrl}/api/repos`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * Get a specific repo.
   */
  async getRepo(repoId: string): Promise<unknown> {
    const response = await this.request.get(`${this.baseUrl}/api/repos/${repoId}`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * List cards for a repo.
   */
  async listCards(repoId: string): Promise<unknown[]> {
    const response = await this.request.get(`${this.baseUrl}/api/repos/${repoId}/cards`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * Get a specific card.
   */
  async getCard(cardId: string): Promise<unknown> {
    const response = await this.request.get(`${this.baseUrl}/api/cards/${cardId}`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * Start a card (trigger job).
   */
  async startCard(cardId: string): Promise<unknown> {
    const response = await this.request.post(`${this.baseUrl}/api/cards/${cardId}/start`);
    expect(response.ok(), `Start card failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Approve a card (merge branch).
   */
  async approveCard(cardId: string, targetBranch?: string): Promise<unknown> {
    const response = await this.request.post(`${this.baseUrl}/api/cards/${cardId}/approve`, {
      data: { target_branch: targetBranch },
    });
    expect(response.ok(), `Approve card failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * List pipelines.
   */
  async listPipelines(repoId?: string): Promise<unknown[]> {
    const url = repoId
      ? `${this.baseUrl}/api/repos/${repoId}/pipelines`
      : `${this.baseUrl}/api/pipelines`;
    const response = await this.request.get(url);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }

  /**
   * Run a pipeline.
   */
  async runPipeline(
    pipelineId: string,
    options: { trigger_type?: string; trigger_ref?: string } = {}
  ): Promise<unknown> {
    const response = await this.request.post(
      `${this.baseUrl}/api/pipelines/${pipelineId}/run`,
      { data: options }
    );
    expect(response.ok(), `Run pipeline failed: ${await response.text()}`).toBeTruthy();
    return response.json();
  }

  /**
   * Get pipeline run status.
   */
  async getPipelineRun(runId: string): Promise<unknown> {
    const response = await this.request.get(`${this.baseUrl}/api/pipeline-runs/${runId}`);
    expect(response.ok()).toBeTruthy();
    return response.json();
  }
}

// ==============================================================================
// Convenience functions
// ==============================================================================

/**
 * Create a TestApiClient from Playwright's request context.
 */
export function createTestApi(request: APIRequestContext): TestApiClient {
  return new TestApiClient(request);
}

/**
 * Create a ProductionApiClient from Playwright's request context.
 */
export function createApi(request: APIRequestContext): ProductionApiClient {
  return new ProductionApiClient(request);
}

/**
 * Wait for a condition with polling.
 */
export async function waitFor<T>(
  fn: () => Promise<T>,
  options: {
    condition?: (result: T) => boolean;
    timeout?: number;
    interval?: number;
    message?: string;
  } = {}
): Promise<T> {
  const {
    condition = () => true,
    timeout = 30000,
    interval = 500,
    message = 'Condition not met',
  } = options;

  const startTime = Date.now();

  while (Date.now() - startTime < timeout) {
    try {
      const result = await fn();
      if (condition(result)) {
        return result;
      }
    } catch {
      // Ignore errors, keep polling
    }
    await new Promise(resolve => setTimeout(resolve, interval));
  }

  throw new Error(`Timeout: ${message}`);
}
