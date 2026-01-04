export type CardStatus = 'todo' | 'in_progress' | 'in_review' | 'done' | 'failed';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';
export type RunnerStatus = 'idle' | 'busy' | 'offline';
export type RunnerType = 'any' | 'claude-code' | 'gemini';
export type StepType = 'agent' | 'script' | 'docker';

export interface StepConfig {
  command?: string;        // For script/docker steps
  image?: string;          // For docker steps
  working_dir?: string;    // For script steps
  env?: Record<string, string>;  // For docker steps
  volumes?: string[];      // For docker steps
}

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
  step_type: StepType;
  step_config: StepConfig | null;
  prompt_template: string | null;
  agent_file_ids: string[] | null;
  branch_name: string | null;
  pr_url: string | null;
  job_id: string | null;
  completed_runner_type: string | null;
  // Pipeline association
  pipeline_run_id: string | null;
  pipeline_step_index: number | null;
  created_at: string;
  updated_at: string;
}

export interface CardCreate {
  title: string;
  description?: string;
  runner_type?: RunnerType;
  step_type?: StepType;
  step_config?: StepConfig | null;
  prompt_template?: string | null;
  agent_file_ids?: string[] | null;
}

export interface CardUpdate {
  title?: string;
  description?: string;
  status?: CardStatus;
  runner_type?: RunnerType;
  step_type?: StepType;
  step_config?: StepConfig | null;
  prompt_template?: string | null;
  agent_file_ids?: string[] | null;
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
  // Step type and config (Phase 8.5)
  step_type: StepType;
  step_config: StepConfig | null;
  // Test result fields (Phase 8)
  tests_run: boolean;
  tests_passed: boolean | null;
  test_pass_count: number | null;
  test_fail_count: number | null;
  test_skip_count: number | null;
  test_output: string | null;
}

export interface Runner {
  id: string;
  name: string;
  runner_type: string;
  status: RunnerStatus;
  current_job_id: string | null;
  current_job_title: string | null;
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

// Pipeline types (Phase 9)
export type RunStatus = 'pending' | 'running' | 'passed' | 'failed' | 'cancelled';
export type TriggerType = 'card_complete' | 'push';

export type TriggerAction = 'nothing' | 'merge' | 'reject';

export interface TriggerConfig {
  type: TriggerType;
  config: {
    status?: 'done' | 'in_review';  // For card_complete triggers
    branches?: string[];  // For push triggers
  };
  enabled: boolean;
  on_pass: string;  // "nothing" | "merge" | "merge:{branch}"
  on_fail: string;  // "nothing" | "fail" | "reject"
}

export interface PipelineStepConfig {
  id?: string;  // Optional step ID for context directory references
  name: string;
  type: StepType;
  config: StepConfig & {
    runner_type?: RunnerType;
    title?: string;
    description?: string;
    // Agent step fields (Phase 9.1c)
    agent_file_ids?: string[];    // Platform agent file IDs to use
    prompt_template?: string;     // Custom prompt template
    agent?: string;               // Repo-defined agent reference (e.g., "test-fixer")
  };
  on_success: string;  // "next" | "stop" | "trigger:{card_id}" | "merge:{branch}"
  on_failure: string;  // "next" | "stop" | "trigger:{card_id}"
  timeout: number;
  continue_in_context?: boolean;  // If true, next step runs in same container with preserved workspace
}

export interface Pipeline {
  id: string;
  repo_id: string;
  name: string;
  description: string | null;
  steps: PipelineStepConfig[];
  triggers: TriggerConfig[];
  is_template: boolean;
  created_at: string;
  updated_at: string;
}

export interface PipelineCreate {
  name: string;
  description?: string;
  steps: PipelineStepConfig[];
  triggers?: TriggerConfig[];
  is_template?: boolean;
}

export interface PipelineUpdate {
  name?: string;
  description?: string;
  steps?: PipelineStepConfig[];
  triggers?: TriggerConfig[];
  is_template?: boolean;
}

export interface StepRun {
  id: string;
  pipeline_run_id: string;
  step_index: number;
  step_name: string;
  status: RunStatus;
  job_id: string | null;
  logs: string;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface TriggerContext {
  branch?: string;
  commit_sha?: string;
  card_id?: string;
  card_title?: string;
  old_sha?: string;
  push_ref?: string;
}

export interface PipelineRun {
  id: string;
  pipeline_id: string;
  status: RunStatus;
  trigger_type: string;
  trigger_ref: string | null;
  trigger_context: TriggerContext | null;
  current_step: number;
  steps_completed: number;
  steps_total: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  step_runs: StepRun[];
}

export interface PipelineRunCreate {
  trigger_type?: string;
  trigger_ref?: string;
  trigger_context?: TriggerContext;
  params?: Record<string, unknown>;
}

export interface StepLogsResponse {
  step_index: number;
  step_name: string;
  logs: string;
  error: string | null;
  status: RunStatus;
}

// Repo-defined agents and pipelines (Phase 9.1b)
export interface RepoAgent {
  name: string;
  description: string | null;
  prompt_template: string;
  source: 'repo' | 'platform';
  branch?: string;
  filename?: string;
}

export interface RepoPipeline {
  name: string;
  description: string | null;
  steps: PipelineStepConfig[];
  source: 'repo' | 'platform';
  branch?: string;
  filename?: string;
}

// Merged agent for UI (combines platform AgentFile with RepoAgent)
export interface MergedAgent {
  id?: string;  // Only for platform agents
  name: string;
  description: string | null;
  content?: string;  // Platform agent content
  prompt_template?: string;  // Repo agent template
  source: 'repo' | 'platform';
}

// Playground types (Phase 11)
export type PlaygroundStatus = 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled';

// Model options for each runner type
export type ClaudeModel = 'claude-sonnet-4-5-20250929' | 'claude-opus-4-5-20250929' | 'claude-sonnet-4-20250514' | 'claude-haiku-4-5-20251001';
export type GeminiModel = 'gemini-2.5-flash' | 'gemini-2.5-pro' | 'gemini-3-flash-preview' | 'gemini-3-pro-preview';
export type AgentModel = ClaudeModel | GeminiModel;

export interface PlaygroundTestRequest {
  agent_id?: string | null;
  repo_agent_name?: string | null;
  runner_type: 'claude-code' | 'gemini';
  model?: AgentModel | null;  // Specific model to use
  branch: string;
  task_override?: string | null;
  save_to_branch?: string | null;
}

export interface PlaygroundTestResponse {
  session_id: string;
  status: string;
  message: string;
}

export interface PlaygroundResult {
  session_id: string;
  status: string;
  diff: string | null;
  files_changed: string[];
  branch_saved: string | null;
  error: string | null;
  logs: string;
  duration_seconds: number | null;
}

export interface PlaygroundLogEvent {
  type: 'log' | 'tool' | 'status' | 'complete' | 'error' | 'ping';
  data: string;
  timestamp: string;
}

// Debug session types (Phase 12.7)
export type DebugSessionStatus = 'pending' | 'waiting_at_bp' | 'connected' | 'timeout' | 'ended';

export interface DebugSession {
  id: string;
  pipeline_run_id: string;
  status: DebugSessionStatus;
  current_step_index: number | null;
  current_step_name: string | null;
  expires_at: string | null;
}

export interface DebugStepInfo {
  name: string;
  index: number;
  type: string;
}

export interface DebugCommitInfo {
  sha: string;
  message: string;
}

export interface DebugRuntimeInfo {
  host: string;
  orchestrator: string;
  image: string;
  image_sha: string | null;
}

export interface DebugSessionInfo {
  id: string;
  status: DebugSessionStatus;
  current_step: DebugStepInfo | null;
  commit: DebugCommitInfo;
  runtime: DebugRuntimeInfo;
  logs: string;
  join_command: string;
  token: string;
  expires_at: string | null;
}

export interface DebugRerunRequest {
  breakpoints: number[];
  use_original_commit: boolean;
  commit_sha?: string;
  branch?: string;
}

export interface DebugRerunResponse {
  run_id: string;
  debug_session_id: string;
  token: string;
}
