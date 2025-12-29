export type CardStatus = 'todo' | 'in_progress' | 'in_review' | 'done' | 'failed';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';
export type RunnerStatus = 'idle' | 'busy' | 'offline';

export interface Repo {
  id: string;
  name: string;
  remote_url: string | null;
  default_branch: string;
  is_ingested: boolean;
  internal_git_url: string;
  created_at: string;
}

export interface RepoCreate {
  name: string;
  remote_url?: string | null;
  default_branch?: string;
}

export interface RepoIngest {
  id: string;
  name: string;
  internal_git_url: string;
  clone_url: string;
}

export interface CloneUrlResponse {
  clone_url: string;
  is_ingested: boolean;
}

export interface BranchInfo {
  name: string;
  commit: string;
  is_default: boolean;
  is_lazyaf: boolean;
}

export interface BranchesResponse {
  branches: BranchInfo[];
  default_branch: string | null;
  total: number;
}

export interface Card {
  id: string;
  repo_id: string;
  title: string;
  description: string;
  status: CardStatus;
  branch_name: string | null;
  pr_url: string | null;
  job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface CardCreate {
  title: string;
  description?: string;
}

export interface CardUpdate {
  title?: string;
  description?: string;
  status?: CardStatus;
}

export interface Job {
  id: string;
  card_id: string;
  runner_id: string | null;
  status: JobStatus;
  logs: string;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface Runner {
  id: string;
  name: string;
  status: RunnerStatus;
  current_job_id: string | null;
  last_heartbeat: string;
  registered_at: string;
  log_count: number;
}

export interface PoolStatus {
  total_runners: number;
  idle_runners: number;
  busy_runners: number;
  offline_runners: number;
  queued_jobs: number;
  pending_jobs: number;
}

export interface RunnerLogs {
  logs: string[];
  total: number;
}

export interface DockerCommand {
  command: string;
  command_with_secrets: string;
  image: string;
  env_vars: Record<string, string>;
}
