export type CardStatus = 'todo' | 'in_progress' | 'in_review' | 'done' | 'failed';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';
export type RunnerStatus = 'idle' | 'busy' | 'offline';
export type RunnerType = 'any' | 'claude-code' | 'gemini';

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
  runner_type: RunnerType;
  branch_name: string | null;
  pr_url: string | null;
  job_id: string | null;
  completed_runner_type: string | null;
  created_at: string;
  updated_at: string;
}

export interface CardCreate {
  title: string;
  description?: string;
  runner_type?: RunnerType;
}

export interface CardUpdate {
  title?: string;
  description?: string;
  status?: CardStatus;
  runner_type?: RunnerType;
}

export interface Job {
  id: string;
  card_id: string;
  runner_id: string | null;
  runner_type: string | null;
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
  runner_type: string;
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
  runner_type: string;
  env_vars: Record<string, string>;
}

export interface JobLogs {
  logs: string;
  job_id: string;
  status: JobStatus;
}

export interface Commit {
  sha: string;
  short_sha: string;
  message: string;
  author: string;
  timestamp: number;
}

export interface CommitsResponse {
  branch: string;
  commits: Commit[];
  total: number;
}

export interface FileDiff {
  path: string;
  status: 'added' | 'modified' | 'deleted';
  additions: number;
  deletions: number;
  diff: string;
}

export interface DiffResponse {
  base_branch: string;
  head_branch: string;
  base_sha: string;
  head_sha: string;
  commit_count: number;
  files: FileDiff[];
  total_additions: number;
  total_deletions: number;
}

export interface ConflictDetail {
  path: string;
  base_content: string | null;
  ours_content: string | null;
  theirs_content: string | null;
}

export interface MergeResult {
  success: boolean;
  merge_type: 'fast-forward' | 'merge' | null;
  message: string;
  new_sha: string | null;
  error: string | null;
  conflicts?: ConflictDetail[];
}

export interface ApproveResponse {
  card: Card;
  merge_result: MergeResult | null;
}

export interface RebaseResult {
  success: boolean;
  message: string;
  new_sha: string | null;
  error: string | null;
  conflicts?: ConflictDetail[];
}

export interface RebaseResponse {
  card: Card;
  rebase_result: RebaseResult | null;
}

export interface AgentFile {
  id: string;
  name: string;
  content: string;
  description: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentFileCreate {
  name: string;
  content: string;
  description?: string | null;
}

export interface AgentFileUpdate {
  name?: string;
  content?: string;
  description?: string | null;
}
