import type { Repo, RepoCreate, RepoIngest, CloneUrlResponse, BranchesResponse, Card, CardCreate, CardUpdate, Job, JobLogs, Runner, CommitsResponse, DiffResponse, ApproveResponse, RebaseResponse, AgentFile, AgentFileCreate, AgentFileUpdate, Pipeline, PipelineCreate, PipelineUpdate, PipelineRun, PipelineRunCreate, StepLogsResponse, RepoAgent, RepoPipeline, PlaygroundTestRequest, PlaygroundTestResponse, PlaygroundResult, Feature, FeatureCreate, FeatureUpdate, UserStory, UserStoryCreate, UserStoryUpdate, AcceptanceCriterion, AcceptanceCriterionCreate, AcceptanceCriterionUpdate, PromptTemplate, PromptTemplateCreate, PromptTemplateUpdate, DebugSessionInfo, DebugRerunRequest, DebugRerunResponse, DebugJoinToken, DebugResumeRequest, DebugResumeResponse, DebugAbortResponse, DebugExtendRequest, DebugExtendResponse, Experiment, ExperimentSummary, ExperimentDetail, ExperimentCreate, ExperimentUpdate, ExperimentEstimate, ExperimentLaunchResponse, ExperimentAbortResponse, ExperimentResumeResponse, ExperimentCell, Leaderboard } from './types';

const BASE_URL = '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
    throw new Error(error.detail || `HTTP ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}

// Branch info type
export interface BranchInfo {
  name: string;
  sha: string;
  short_sha: string | null;
  is_default: boolean;
  is_orphaned: boolean;
  is_damaged?: boolean;
  missing_objects?: string[];  // List of SHA strings
  objects_checked?: number;
  commit_message: string | null;
  commit_time: number | null;
}

export interface BranchesInfoResponse {
  branches: BranchInfo[];
  total: number;
  orphaned_count: number;
  damaged_count: number;
  default_branch: string;
  remote_url: string | null;
}

export interface SyncResult {
  success: boolean;
  branches: BranchInfo[];
  cleanup: {
    success: boolean;
    deleted_branches: string[];
    errors: string[] | null;
  };
  integrity?: {
    valid: boolean;
    damaged_branches: string[];
  };
  message: string;
}

// Repos
export const repos = {
  list: () => request<Repo[]>('/repos'),
  get: (id: string) => request<Repo>(`/repos/${id}`),
  create: (data: RepoCreate) => request<Repo>('/repos', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  ingest: (data: RepoCreate) => request<RepoIngest>('/repos/ingest', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/repos/${id}`, { method: 'DELETE' }),
  cloneUrl: (id: string) => request<CloneUrlResponse>(`/repos/${id}/clone-url`),
  branches: (id: string) => request<BranchesResponse>(`/repos/${id}/branches`),
  branchesInfo: (id: string, verify: boolean = false) =>
    request<BranchesInfoResponse>(`/repos/${id}/branches/info${verify ? '?verify=true' : ''}`),
  deleteBranch: (repoId: string, branchName: string) =>
    request<{ success: boolean; message: string }>(`/repos/${repoId}/branches/${encodeURIComponent(branchName)}`, {
      method: 'DELETE',
    }),
  cleanupOrphans: (id: string) =>
    request<{ success: boolean; deleted_branches: string[]; errors: string[] | null }>(`/repos/${id}/cleanup-orphans`, {
      method: 'POST',
    }),
  sync: (id: string) => request<SyncResult>(`/repos/${id}/sync`, { method: 'POST' }),
  reinitialize: (id: string) => request<{ success: boolean; message: string }>(`/repos/${id}/reinitialize`, { method: 'POST' }),
  commits: (id: string, branch?: string, limit: number = 20) => {
    const params = new URLSearchParams();
    if (branch) params.set('branch', branch);
    params.set('limit', limit.toString());
    return request<CommitsResponse>(`/repos/${id}/commits?${params}`);
  },
  diff: (id: string, base: string, head: string) =>
    request<DiffResponse>(`/repos/${id}/diff?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`),
};

// Cards
export const cards = {
  list: (repoId: string) => request<Card[]>(`/repos/${repoId}/cards`),
  get: (id: string) => request<Card>(`/cards/${id}`),
  create: (repoId: string, data: CardCreate) => request<Card>(`/repos/${repoId}/cards`, {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: CardUpdate) => request<Card>(`/cards/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/cards/${id}`, { method: 'DELETE' }),
  start: (id: string) => request<Card>(`/cards/${id}/start`, { method: 'POST' }),
  approve: (id: string, targetBranch?: string) => request<ApproveResponse>(`/cards/${id}/approve`, {
    method: 'POST',
    body: JSON.stringify({ target_branch: targetBranch || null }),
  }),
  reject: (id: string) => request<Card>(`/cards/${id}/reject`, { method: 'POST' }),
  retry: (id: string) => request<Card>(`/cards/${id}/retry`, { method: 'POST' }),
  rebase: (id: string, ontoBranch?: string) => request<RebaseResponse>(`/cards/${id}/rebase`, {
    method: 'POST',
    body: JSON.stringify({ onto_branch: ontoBranch || null }),
  }),
  resolveConflicts: (id: string, targetBranch: string | undefined, resolutions: Array<{ path: string; content: string }>) =>
    request<ApproveResponse>(`/cards/${id}/resolve-conflicts`, {
      method: 'POST',
      body: JSON.stringify({ target_branch: targetBranch || null, resolutions }),
    }),
  resolveRebaseConflicts: (id: string, ontoBranch: string | undefined, resolutions: Array<{ path: string; content: string }>) =>
    request<RebaseResponse>(`/cards/${id}/resolve-rebase-conflicts`, {
      method: 'POST',
      body: JSON.stringify({ onto_branch: ontoBranch || null, resolutions }),
    }),
  promoteToFeature: (id: string) => request<Feature>(`/cards/${id}/promote-to-feature`, { method: 'POST' }),
};

// Jobs
export const jobs = {
  get: (id: string) => request<Job>(`/jobs/${id}`),
  cancel: (id: string) => request<Job>(`/jobs/${id}/cancel`, { method: 'POST' }),
  logs: (id: string) => request<JobLogs>(`/jobs/${id}/logs`),
};

// Runners (Phase 12.6): a READ-ONLY projection of the registry. Register /
// heartbeat / claim-a-job / logs / docker-command were the polling pool's
// surface and left with it; a runner now enrolls over `/ws/runner` and its
// logs are step logs, read through the pipeline-run step log routes like
// every other step's. `list()` is the SNAPSHOT half of the store's
// snapshot-then-delta model - there is deliberately no polling helper here.
export const runners = {
  list: () => request<Runner[]>('/runners'),
};

// Agent Files
export const agentFiles = {
  list: () => request<AgentFile[]>('/agent-files'),
  get: (id: string) => request<AgentFile>(`/agent-files/${id}`),
  create: (data: AgentFileCreate) => request<AgentFile>('/agent-files', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: AgentFileUpdate) => request<AgentFile>(`/agent-files/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/agent-files/${id}`, { method: 'DELETE' }),
};

// Pipelines (Phase 9)
export const pipelines = {
  list: (repoId?: string) => {
    const params = repoId ? `?repo_id=${repoId}` : '';
    return request<Pipeline[]>(`/pipelines${params}`);
  },
  listForRepo: (repoId: string) => request<Pipeline[]>(`/repos/${repoId}/pipelines`),
  get: (id: string) => request<Pipeline>(`/pipelines/${id}`),
  create: (repoId: string, data: PipelineCreate) => request<Pipeline>(`/repos/${repoId}/pipelines`, {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: PipelineUpdate) => request<Pipeline>(`/pipelines/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/pipelines/${id}`, { method: 'DELETE' }),
  run: (id: string, data?: PipelineRunCreate) => request<PipelineRun>(`/pipelines/${id}/run`, {
    method: 'POST',
    body: JSON.stringify(data || {}),
  }),
  runs: (id: string, limit: number = 20) => request<PipelineRun[]>(`/pipelines/${id}/runs?limit=${limit}`),
};

// Pipeline Runs (Phase 9)
export const pipelineRuns = {
  list: (filters?: { pipeline_id?: string; status?: string; limit?: number }) => {
    const params = new URLSearchParams();
    if (filters?.pipeline_id) params.set('pipeline_id', filters.pipeline_id);
    if (filters?.status) params.set('status', filters.status);
    if (filters?.limit) params.set('limit', filters.limit.toString());
    return request<PipelineRun[]>(`/pipeline-runs?${params}`);
  },
  get: (runId: string) => request<PipelineRun>(`/pipeline-runs/${runId}`),
  cancel: (runId: string) => request<PipelineRun>(`/pipeline-runs/${runId}/cancel`, { method: 'POST' }),
  stepLogs: (runId: string, stepIndex: number) =>
    request<StepLogsResponse>(`/pipeline-runs/${runId}/steps/${stepIndex}/logs`),
};

// Repo-defined LazyAF files (Phase 9.1b)
export const lazyafFiles = {
  listAgents: (repoId: string, branch?: string) => {
    const params = branch ? `?branch=${encodeURIComponent(branch)}` : '';
    return request<RepoAgent[]>(`/repos/${repoId}/lazyaf/agents${params}`);
  },
  getAgent: (repoId: string, agentName: string, branch?: string) => {
    const params = branch ? `?branch=${encodeURIComponent(branch)}` : '';
    return request<RepoAgent>(`/repos/${repoId}/lazyaf/agents/${encodeURIComponent(agentName)}${params}`);
  },
  listPipelines: (repoId: string, branch?: string) => {
    const params = branch ? `?branch=${encodeURIComponent(branch)}` : '';
    return request<RepoPipeline[]>(`/repos/${repoId}/lazyaf/pipelines${params}`);
  },
  getPipeline: (repoId: string, pipelineName: string, branch?: string) => {
    const params = branch ? `?branch=${encodeURIComponent(branch)}` : '';
    return request<RepoPipeline>(`/repos/${repoId}/lazyaf/pipelines/${encodeURIComponent(pipelineName)}${params}`);
  },
  runPipeline: (repoId: string, pipelineName: string, branch?: string) => {
    const params = branch ? `?branch=${encodeURIComponent(branch)}` : '';
    return request<{ pipeline_id: string; run_id: string; status: string; message: string }>(
      `/repos/${repoId}/lazyaf/pipelines/${encodeURIComponent(pipelineName)}/run${params}`,
      { method: 'POST' }
    );
  },
};

// Health
export const health = {
  check: () => request<{ status: string; app: string }>('/health'),
};

// Playground (Phase 11)
export const playground = {
  start: (repoId: string, data: PlaygroundTestRequest) =>
    request<PlaygroundTestResponse>(`/repos/${repoId}/playground/test`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  status: (sessionId: string) =>
    request<{ session_id: string; status: string; started_at: string | null; completed_at: string | null }>(
      `/playground/${sessionId}/status`
    ),

  cancel: (sessionId: string) =>
    request<{ status: string; session_id: string }>(`/playground/${sessionId}/cancel`, {
      method: 'POST',
    }),

  result: (sessionId: string) => request<PlaygroundResult>(`/playground/${sessionId}/result`),

  // SSE stream URL (used directly with EventSource, not through request())
  streamUrl: (sessionId: string) => `${BASE_URL}/playground/${sessionId}/stream`,
};

// Models API
export interface ModelInfo {
  id: string;
  name: string;
  provider: 'anthropic' | 'google';
  description: string;
}

export interface ModelsListResponse {
  models: ModelInfo[];
  anthropic: ModelInfo[];
  google: ModelInfo[];
}

export const models = {
  list: (refresh: boolean = false) =>
    request<ModelsListResponse>(`/models${refresh ? '?refresh=true' : ''}`),
};

// =============================================================================
// Spec Layer (Phase 12.2.5)
// =============================================================================

export const features = {
  list: () => request<Feature[]>('/features'),
  get: (id: string) => request<Feature>(`/features/${id}`),
  create: (data: FeatureCreate) => request<Feature>('/features', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: FeatureUpdate) => request<Feature>(`/features/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/features/${id}`, { method: 'DELETE' }),
  stories: (id: string) => request<UserStory[]>(`/features/${id}/stories`),
  // Seeds the three north-star Milestone 12 user stories (US-1/US-2/US-3).
  // Response shape is backend-owned; callers reload the feature list after.
  seedMilestone12: () => request<unknown>('/features/seed-milestone12', { method: 'POST' }),
};

export const userStories = {
  // Unfiltered list (all stories across features); pass featureId to filter
  // server-side. The Specs page loads everything in one request and groups
  // client-side (avoids the 1+N per-feature fetch).
  list: (featureId?: string) =>
    request<UserStory[]>(`/user-stories${featureId ? `?feature_id=${encodeURIComponent(featureId)}` : ''}`),
  get: (id: string) => request<UserStory>(`/user-stories/${id}`),
  create: (data: UserStoryCreate) => request<UserStory>('/user-stories', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: UserStoryUpdate) => request<UserStory>(`/user-stories/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/user-stories/${id}`, { method: 'DELETE' }),
  criteria: (id: string) => request<AcceptanceCriterion[]>(`/user-stories/${id}/criteria`),
};

export const criteria = {
  get: (id: string) => request<AcceptanceCriterion>(`/criteria/${id}`),
  create: (data: AcceptanceCriterionCreate) => request<AcceptanceCriterion>('/criteria', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: AcceptanceCriterionUpdate) => request<AcceptanceCriterion>(`/criteria/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/criteria/${id}`, { method: 'DELETE' }),
};

export const promptTemplates = {
  list: () => request<PromptTemplate[]>('/prompt-templates'),
  get: (id: string) => request<PromptTemplate>(`/prompt-templates/${id}`),
  create: (data: PromptTemplateCreate) => request<PromptTemplate>('/prompt-templates', {
    method: 'POST',
    body: JSON.stringify(data),
  }),
  update: (id: string, data: PromptTemplateUpdate) => request<PromptTemplate>(`/prompt-templates/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(data),
  }),
  delete: (id: string) => request<void>(`/prompt-templates/${id}`, { method: 'DELETE' }),
};

// =============================================================================
// Debug Re-Run Mode (Phase 12.7)
//
// `createRerun` is the only call that starts anything; every other verb acts
// on an existing session. There is deliberately NO token accessor here: the
// UI never holds a terminal credential (GET /api/debug/{id} does not return
// one), and `joinToken` exists solely so a future "copy a ready-to-paste
// command" affordance has a mint - the CLI mints its own.
// =============================================================================

export const debug = {
  /** Start a debug re-run of `runId`. Breakpoints are step KEYS (see types.ts). */
  createRerun: (runId: string, data: DebugRerunRequest) =>
    request<DebugRerunResponse>(`/pipeline-runs/${runId}/debug-rerun`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  /** Every non-terminal session. The store's SNAPSHOT half. */
  list: () => request<DebugSessionInfo[]>('/debug'),

  get: (sessionId: string) => request<DebugSessionInfo>(`/debug/${sessionId}`),

  /**
   * Mint a short-lived terminal join credential. Re-mintable by design:
   * a one-time token cannot survive a dropped CLI reconnecting into the same
   * shell. Revocation stays free because the WS upgrade re-reads the session
   * row whatever the JWT says.
   */
  joinToken: (sessionId: string) =>
    request<DebugJoinToken>(`/debug/${sessionId}/join-token`, { method: 'POST' }),

  /** clearRemaining=true drops the remaining breakpoints ("run to completion"). */
  resume: (sessionId: string, clearRemaining: boolean = false) =>
    request<DebugResumeResponse>(`/debug/${sessionId}/resume`, {
      method: 'POST',
      body: JSON.stringify({ clear_remaining: clearRemaining } satisfies DebugResumeRequest),
    }),

  /** Ends the session AND cancels its run. Terminal. */
  abort: (sessionId: string) =>
    request<DebugAbortResponse>(`/debug/${sessionId}/abort`, { method: 'POST' }),

  extend: (sessionId: string, additionalMinutes: number = 30) =>
    request<DebugExtendResponse>(`/debug/${sessionId}/extend`, {
      method: 'POST',
      body: JSON.stringify({ additional_minutes: additionalMinutes } satisfies DebugExtendRequest),
    }),
};

// =============================================================================
// Experiments (Phase 12.6.5)
//
// `dryRun` and `create` are THE SAME ROUTE with a different `dry_run` flag and
// therefore different response shapes - hence two typed methods over one path
// rather than one method with a union return. `dryRun` creates nothing (200);
// `create` creates a draft (201) and still does not spend a cent: dispatch
// only begins at `launch`.
//
// Nothing here launches implicitly. Create -> estimate -> launch are three
// separate calls precisely so the dollars are shown before they are committed.
// =============================================================================

export const experiments = {
  list: (filters?: { status?: string; target_id?: string; repo_id?: string }) => {
    const params = new URLSearchParams();
    if (filters?.status) params.set('status', filters.status);
    if (filters?.target_id) params.set('target_id', filters.target_id);
    if (filters?.repo_id) params.set('repo_id', filters.repo_id);
    const qs = params.toString();
    return request<ExperimentSummary[]>(`/experiments${qs ? `?${qs}` : ''}`);
  },

  get: (id: string) => request<ExperimentDetail>(`/experiments/${id}`),

  /** Create a DRAFT. No cell dispatches until launch(). */
  create: (data: ExperimentCreate) =>
    request<Experiment>('/experiments', {
      method: 'POST',
      body: JSON.stringify({ ...data, dry_run: false }),
    }),

  /**
   * Cost the matrix WITHOUT creating anything. The Launch button in the UI is
   * gated on a fresh result from this call (Phase 12.6.5's headline guardrail).
   */
  dryRun: (data: ExperimentCreate) =>
    request<ExperimentEstimate>('/experiments', {
      method: 'POST',
      body: JSON.stringify({ ...data, dry_run: true }),
    }),

  /** The same estimate, for an already-saved draft. */
  estimate: (id: string) => request<ExperimentEstimate>(`/experiments/${id}/estimate`),

  update: (id: string, data: ExperimentUpdate) =>
    request<Experiment>(`/experiments/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  delete: (id: string) => request<void>(`/experiments/${id}`, { method: 'DELETE' }),

  /**
   * Freeze prompt versions, create the cells, start the pump (202). Callers
   * refetch `get(id)` rather than reading this body.
   */
  launch: (id: string) =>
    request<ExperimentLaunchResponse>(`/experiments/${id}/launch`, { method: 'POST' }),

  /** Cancels PENDING cells only; running cells finish and still count. */
  abort: (id: string) =>
    request<ExperimentAbortResponse>(`/experiments/${id}/abort`, { method: 'POST' }),

  /**
   * Re-pump a stalled experiment (the pump is in-process; a backend restart
   * strands pending cells). `stalled: true` on the detail is what surfaces it.
   */
  resume: (id: string) =>
    request<ExperimentResumeResponse>(`/experiments/${id}/resume`, { method: 'POST' }),

  /** Per-CELL rows: coordinates + status + cost + test counts. */
  results: (id: string) => request<ExperimentCell[]>(`/experiments/${id}/results`),

  /** Per-VARIANT aggregation. Always `ranked: false` in this phase. */
  leaderboard: (id: string) => request<Leaderboard>(`/experiments/${id}/leaderboard`),
};

export const leaderboards = {
  /**
   * Cross-experiment view over every criterion under a feature, including one
   * `variant_index: null` row for ordinary non-experiment runs.
   */
  feature: (featureId: string, experimentIds: string[] = []) => {
    const params = new URLSearchParams();
    for (const id of experimentIds) params.append('experiment_id', id);
    const qs = params.toString();
    return request<Leaderboard>(`/leaderboards/feature/${featureId}${qs ? `?${qs}` : ''}`);
  },
};
