import type { Repo, RepoCreate, RepoIngest, CloneUrlResponse, BranchesResponse, Card, CardCreate, CardUpdate, Job, JobLogs, Runner, PoolStatus, DockerCommand, RunnerLogs, CommitsResponse, DiffResponse, ApproveResponse, RebaseResponse, AgentFile, AgentFileCreate, AgentFileUpdate } from './types';

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
};

// Jobs
export const jobs = {
  get: (id: string) => request<Job>(`/jobs/${id}`),
  cancel: (id: string) => request<Job>(`/jobs/${id}/cancel`, { method: 'POST' }),
  logs: (id: string) => request<JobLogs>(`/jobs/${id}/logs`),
};

// Runners
export const runners = {
  list: () => request<Runner[]>('/runners'),
  status: () => request<PoolStatus>('/runners/status'),
  logs: (runnerId: string, offset: number = 0) =>
    request<RunnerLogs>(`/runners/${runnerId}/logs?offset=${offset}`),
  dockerCommand: (withSecrets: boolean = false) =>
    request<DockerCommand>(`/runners/docker-command?with_secrets=${withSecrets}`),
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

// Health
export const health = {
  check: () => request<{ status: string; app: string }>('/health'),
};
