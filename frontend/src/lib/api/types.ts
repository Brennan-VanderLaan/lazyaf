export type CardStatus = 'todo' | 'in_progress' | 'in_review' | 'done' | 'failed';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';
export type RunnerStatus = 'idle' | 'busy' | 'offline';

export interface Repo {
  id: string;
  name: string;
  path: string;
  remote_url: string | null;
  default_branch: string;
  created_at: string;
}

export interface RepoCreate {
  name: string;
  path: string;
  remote_url?: string | null;
  default_branch?: string;
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
  container_id: string | null;
  status: RunnerStatus;
  current_job_id: string | null;
  last_heartbeat: string;
}
