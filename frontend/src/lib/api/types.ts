export type CardStatus = 'todo' | 'in_progress' | 'in_review' | 'done' | 'failed';
export type JobStatus = 'queued' | 'running' | 'completed' | 'failed';
// Phase 12.6 cross-agent contract #4: `RunnerState` is the SINGLE status
// vocabulary shared by the state machine, the `runners.status` column, the
// API and this UI. The old `RunnerStatus` ('idle' | 'busy' | 'offline') was
// the polling pool's vocabulary and is gone with it - 'offline' in
// particular has no counterpart: a runner is `disconnected` (we lost the
// socket) or `dead` (it stopped heartbeating), and those are different
// facts an operator needs to tell apart.
export type RunnerState =
  | 'disconnected'
  | 'connecting'
  | 'idle'
  | 'assigned'
  | 'busy'
  | 'dead';

// `RunnerType` is a DIFFERENT axis and deliberately survives: it names the AI
// flavor a card/job wants ('claude-code' | 'gemini' | 'mock'), not a runner's
// lifecycle. A 12.6 runner-agent reports its own free-form `runner_type`
// (default 'generic'), so `Runner.runner_type` below is a plain string.
export type RunnerType = 'any' | 'claude-code' | 'gemini' | 'mock';
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
  // Spec layer links (Phase 12.2.5) - optional so older fixtures stay valid
  feature_id?: string | null;
  user_story_id?: string | null;
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
  // Spec layer links (Phase 12.2.5)
  feature_id?: string | null;
  user_story_id?: string | null;
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

/**
 * One row of `GET /api/runners`, and — byte for byte — one `runner_status`
 * WebSocket delta. The backend produces both from
 * `RunnerRegistry._as_dict`, so the snapshot and the delta cannot drift into
 * two shapes the store would have to reconcile.
 *
 * Mirrors `backend/app/schemas/runner.py::RunnerRead`.
 */
export interface Runner {
  id: string;
  name: string | null;
  /** Free-form, agent-reported. 'generic' by default; NOT a `RunnerType`. */
  runner_type: string;
  status: RunnerState;
  /** Free-form capability labels, e.g. `{arch: 'amd64', has: ['docker']}`. */
  labels: Record<string, unknown>;
  current_step_execution_id: string | null;
  /** Step the in-memory machine believes it is running, when connected. */
  current_step_id?: string | null;
  protocol_version: number | null;
  agent_version: string | null;
  connected_at: string | null;
  last_heartbeat: string | null;
  created_at: string | null;
  /**
   * 'websocket' when THIS backend process holds a live socket for the row,
   * 'none' otherwise. The row alone cannot answer it - an 'idle' status left
   * behind by a crashed process looks identical - so the registry stamps it
   * at snapshot time. The panel renders a stale row as unreachable rather
   * than as an available runner.
   */
  connection: 'websocket' | 'none';
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

// Which execution path ran a step (Phase 12.2-INT). Mirrors the backend's
// ExecutorMode enum in models/pipeline.py: 'local' (ephemeral Docker container
// driven by the backend), 'legacy' (runner job queue), 'remote' (reserved).
export type ExecutorMode = 'local' | 'legacy' | 'remote';

export interface StepRun {
  id: string;
  pipeline_run_id: string;
  step_index: number;
  step_name: string;
  status: RunStatus;
  job_id: string | null;
  // Optional so older fixtures stay valid; null until the step is dispatched.
  executor?: ExecutorMode | null;
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
  runner_type: 'claude-code' | 'gemini' | 'mock';
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

// =============================================================================
// Graph-Based Pipeline Types (Graph Creep)
// =============================================================================

export type EdgeCondition = 'success' | 'failure' | 'always';

export interface PipelineNodePosition {
  x: number;
  y: number;
}

export interface PipelineEdge {
  id: string;
  from_step: string;
  to_step: string;
  condition: EdgeCondition;
}

export interface PipelineStepV2 {
  id: string;
  name: string;
  type: StepType;
  config: StepConfig & {
    runner_type?: RunnerType;
    title?: string;
    description?: string;
    agent_file_ids?: string[];
    prompt_template?: string;
    agent?: string;
  };
  position?: PipelineNodePosition;
  timeout: number;
  continue_in_context?: boolean;
}

export interface PipelineGraphModel {
  steps: Record<string, PipelineStepV2>;
  edges: PipelineEdge[];
  entry_points: string[];  // Derived from edges connected to Start node
  start_position?: { x: number; y: number };  // Position of the Start node
  version: number;
}

// Extended Pipeline interface for graph support
export interface PipelineV2 extends Pipeline {
  steps_graph?: PipelineGraphModel | null;
}

export interface PipelineCreateV2 extends PipelineCreate {
  steps_graph?: PipelineGraphModel;
}

export interface PipelineUpdateV2 extends PipelineUpdate {
  steps_graph?: PipelineGraphModel;
}

// Extended StepRun for graph pipelines
export interface StepRunV2 extends StepRun {
  step_id?: string | null;  // Graph step ID
}

// Extended PipelineRun for parallel execution tracking
export interface PipelineRunV2 extends PipelineRun {
  active_step_ids?: string[];
  completed_step_ids?: string[];
  step_runs: StepRunV2[];
}

// Svelte Flow node data types
export interface PipelineNodeData {
  step: PipelineStepV2;
  status?: RunStatus;
  isEntryPoint: boolean;
  onEdit?: (stepId: string) => void;
  onDelete?: (stepId: string) => void;
}

export interface PipelineEdgeData {
  condition: EdgeCondition;
  isActive?: boolean;
}

// =============================================================================
// Spec Layer Types (Phase 12.2.5)
// Feature -> UserStory -> AcceptanceCriterion (deliberately shallow hierarchy)
// =============================================================================

export type FeatureStatus = 'draft' | 'active' | 'done';

export interface Feature {
  id: string;
  title: string;
  description: string;
  status: FeatureStatus;
  repo_ids: string[];
  created_at?: string;
  updated_at?: string;
}

export interface FeatureCreate {
  title: string;
  description?: string;
  status?: FeatureStatus;
  repo_ids?: string[];
}

export interface FeatureUpdate {
  title?: string;
  description?: string;
  status?: FeatureStatus;
  repo_ids?: string[];
}

export interface UserStory {
  id: string;
  feature_id: string;
  title: string;
  narrative: string;
  status: string;
  priority: number | null;
  created_at?: string;
  updated_at?: string;
}

export interface UserStoryCreate {
  feature_id: string;
  title: string;
  narrative?: string;
  status?: string;
  priority?: number | null;
}

export interface UserStoryUpdate {
  title?: string;
  narrative?: string;
  status?: string;
  priority?: number | null;
}

export interface AcceptanceCriterion {
  id: string;
  user_story_id: string;
  text: string;
  required: boolean;
  notes: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface AcceptanceCriterionCreate {
  user_story_id: string;
  text: string;
  required?: boolean;
  notes?: string | null;
}

export interface AcceptanceCriterionUpdate {
  text?: string;
  required?: boolean;
  notes?: string | null;
}

export interface PromptTemplate {
  id: string;
  name: string;
  description: string;
  content: string;
  created_at?: string;
  updated_at?: string;
}

export interface PromptTemplateCreate {
  name: string;
  description?: string;
  content: string;
}

export interface PromptTemplateUpdate {
  name?: string;
  description?: string;
  content?: string;
}

// =============================================================================
// Debug Re-Run Mode (Phase 12.7)
//
// Shapes mirror backend/app/schemas/debug.py. Two contract points that are
// easy to get wrong and are therefore stated here:
//
//  1. A BREAKPOINT IS A STEP KEY (string), never an index. The backend
//     resolver is `debug_step_key(step_run) = step_run.step_id or
//     str(step_run.step_index)`: graph (v2) steps carry a stable step_id,
//     legacy (v1) steps do not and are addressed by their index rendered as
//     a string. `debugBreakpointOptions()` in stores/debug.ts is the client
//     half of that one resolver - do not derive keys anywhere else.
//     (failure_01 used `list[int]`, which cannot address a graph step.)
//
//  2. THERE IS NO TOKEN ON THE SESSION. `GET /api/debug/{id}` never returns
//     one; a terminal join credential is minted on demand by
//     `POST /api/debug/{id}/join-token` and expires in minutes. The UI never
//     needs one - it shows the join command and the CLI mints its own.
// =============================================================================

/**
 * Debug session lifecycle. Same vocabulary as the backend's `DebugState`
 * enum - deliberately NOT mixed into `RunStatus`: a run's status vocabulary
 * is unchanged by 12.7 and debug state lives on the session row only.
 *
 * `pending` means "executing, not at a breakpoint" - it is also the state a
 * RESUMED session returns to, which is what makes multi-breakpoint runs work.
 */
export type DebugState = 'pending' | 'waiting_at_bp' | 'connected' | 'timeout' | 'ended';

/** The step the run is paused before (null until a breakpoint is hit). */
export interface DebugStepInfo {
  key: string;
  name: string;
  index: number;
  type: string;
}

export interface DebugCommitInfo {
  sha: string;
  message: string;
  /** Set when the re-run tracks a branch head rather than a pinned sha. */
  branch: string;
}

export interface DebugRuntimeInfo {
  host: string;
  orchestrator: string;
  image: string;
  image_sha: string | null;
}

/**
 * Full session projection. This is BOTH the `GET /api/debug/{id}` body and
 * the `debug_session_status` WebSocket payload - one shape, so a delta is a
 * wholesale row replacement and never a field-by-field merge (the same
 * snapshot-then-delta contract the runner panel rides on).
 */
export interface DebugSessionInfo {
  id: string;
  /** The DEBUG re-run this session gates. Indexes the store; required. */
  pipeline_run_id: string;
  /** The original failed run this re-runs, when there was one. */
  original_run_id: string | null;
  status: DebugState;
  current_step: DebugStepInfo | null;
  commit: DebugCommitInfo;
  runtime: DebugRuntimeInfo;
  logs: string;
  join_command: string;
  expires_at: string | null;
  created_at: string | null;
  ended_at: string | null;
  /** 'sidecar' is the only mode 12.7 ships; null before a terminal attaches. */
  connection_mode: string | null;
  /** Every requested breakpoint, as step keys. */
  breakpoints: string[];
  /** Keys whose gate has already fired. */
  breakpoints_hit: string[];
  /** Keys still ahead of the run. */
  breakpoints_pending: string[];
  /**
   * False for a step running on a REMOTE runner: its workspace is a volume
   * on the runner host that the backend's Docker client cannot see, so no
   * sidecar can be attached to it in 12.7. Pause/resume/abort/extend still
   * work - the capability is reduced, loudly, never silently.
   */
  attach_available: boolean;
  attach_unavailable_reason: string | null;
  /** Always set once the session reaches a terminal state (R1). */
  end_reason: string | null;
}

export interface DebugRerunRequest {
  /** Step KEYS. An unknown key is a 400, never a breakpoint that never fires. */
  breakpoints: string[];
  use_original_commit: boolean;
  commit_sha?: string | null;
  branch?: string | null;
  timeout_seconds?: number | null;
}

/** No token: see the header note. The join command is the whole handoff. */
export interface DebugRerunResponse {
  run_id: string;
  debug_session_id: string;
  join_command: string;
}

export interface DebugJoinToken {
  token: string;
  expires_at: string;
  join_command: string;
}

export interface DebugResumeRequest {
  /** true = drop the remaining breakpoints and run to completion. */
  clear_remaining: boolean;
}

export interface DebugResumeResponse {
  status: string;
  /** Key of the next breakpoint the run will pause at, if any. */
  next_breakpoint: string | null;
}

export interface DebugAbortResponse {
  status: string;
  end_reason: string;
}

export interface DebugExtendRequest {
  additional_minutes: number;
}

export interface DebugExtendResponse {
  expires_at: string;
  /** True when the request hit max_timeout_seconds and was trimmed. */
  clamped: boolean;
}

/** One selectable breakpoint in the re-run modal, derived from a pipeline. */
export interface DebugBreakpointOption {
  key: string;
  name: string;
  type: StepType;
  index: number;
}

// =============================================================================
// Experiments / matrix fan-out / leaderboard (Phase 12.6.5)
//
// These shapes MIRROR backend/app/schemas/experiment.py, which is the single
// source of truth for them (R3). Three contract points that are easy to get
// wrong and are therefore stated here rather than discovered later:
//
//  1. MONEY IS A STRING, ALWAYS. The backend keeps Decimal in Python and in
//     the DB and serialises to a decimal string ("7.44"); the UI formats and
//     compares it as text. Parsing dollars into a JS number anywhere outside
//     a display helper reintroduces the float the backend spent a phase
//     avoiding.
//
//  2. AN ABSENT NUMBER IS `null`, NEVER `0`. `pass_rate: null` means "nothing
//     was measured" and carries a `reason`; `0` means "measured, and it
//     failed". The two must never render the same way - that is the whole
//     point of the leaderboard (R1).
//
//  3. THE TWO WS FRAMES ARE SUBSETS OF THE REST ROWS, not separate shapes.
//     `ExperimentStatusFrame` is a subset of `ExperimentSummary` and
//     `ExperimentCellFrame` is a subset of `ExperimentCell`, so a delta is a
//     PARTIAL MERGE into the known row - never a wholesale replacement, which
//     would blank `started_at` / `error` on every transition.
// =============================================================================

export type ExperimentStatus =
  | 'draft'
  | 'running'
  | 'complete'
  | 'aborted'
  | 'budget_exhausted';

/**
 * Cell lifecycle. `failed` and `error` are DIFFERENT FACTS and the UI must
 * keep them apart: `failed` = the cell ran and its suite was red (a real
 * measurement, counted in the leaderboard); `error` = the cell ran and
 * measured NOTHING (infra/agent crash, excluded from denominators and
 * reported as `error_rate`).
 */
export type ExperimentCellStatus =
  | 'pending'
  | 'dispatching'
  | 'running'
  | 'passed'
  | 'failed'
  | 'error'
  | 'cancelled'
  | 'skipped_budget';

/** 12.6.5 targets a card or a user story. `feature` is refused with a 422. */
export type ExperimentTargetType = 'card' | 'user_story';

/** Where a dollar estimate came from. Never omitted, never implied. */
export type EstimateBasis = 'historical-median' | 'partial' | 'no-history';

/** Cell statuses that are terminal (no further transition is coming). */
export const TERMINAL_CELL_STATUSES: readonly ExperimentCellStatus[] = [
  'passed',
  'failed',
  'error',
  'cancelled',
  'skipped_budget',
];

/** Experiment statuses that are terminal. */
export const TERMINAL_EXPERIMENT_STATUSES: readonly ExperimentStatus[] = [
  'complete',
  'aborted',
  'budget_exhausted',
];

/**
 * The `ranked: false` note. Single-sourced here so the component and the
 * Playwright spec assert the same literal (cross-agent contract #6); the
 * backend sends the same string on every leaderboard response and the UI
 * renders what the API sent.
 */
export const NOT_RANKED_NOTE =
  'Reported, not ranked. Ranking requires the paired cluster bootstrap and the ' +
  'separability rule (Milestone 13.4, docs/milestone-13/phase-specs-and-metrics.md ' +
  'Part 2). Sort the table if you like; the platform makes no claim that one ' +
  'variant beats another.';

/**
 * One entry on the model axis. `agent` is REQUIRED: there is no model-name ->
 * CLI inference table, because a guessed agent is a silent fallback (R1).
 * `model: null` is a legal control variant meaning "the CLI's own default".
 */
export interface ExperimentModelAxis {
  agent: string;
  model: string | null;
  label?: string | null;
  /** Escape-hatch overlay merged into the cell's step config. Reserved keys 422. */
  step_config?: Record<string, unknown> | null;
}

/** One entry on the prompt axis. `null` template = the platform default prompt. */
export interface ExperimentPromptAxis {
  prompt_template_id: string | null;
  label?: string | null;
  step_config?: Record<string, unknown> | null;
}

export interface ExperimentMatrix {
  models: ExperimentModelAxis[];
  prompts: ExperimentPromptAxis[];
  repeat: number;
}

/** Optional per-cell verification step - where TestRun evidence comes from. */
export interface ExperimentVerify {
  image: string;
  command: string;
  timeout?: number | null;
}

/** The stored experiment row. */
export interface Experiment {
  id: string;
  name: string;
  description: string;
  target_type: ExperimentTargetType;
  target_id: string;
  repo_id: string;
  /** Null only if a stored matrix ever fails to parse; the API never omits it otherwise. */
  matrix: ExperimentMatrix | null;
  verify: ExperimentVerify | null;
  /** Decimal string. Required and > 0: a cap that can be omitted is not a cap. */
  budget_usd: string;
  max_concurrency: number;
  cell_timeout: number;
  push_branches: boolean;
  status: ExperimentStatus;
  /** What the dry run said at launch, or null for a never-launched draft. */
  estimated_cost_usd: string | null;
  estimate_basis: EstimateBasis | null;
  /**
   * Spend that landed after the cap tripped, because the cap bounds DISPATCH
   * and cannot un-spend cells already in flight. Recorded, shown, never hidden.
   */
  budget_overrun_usd: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
  launched_at: string | null;
  completed_at: string | null;
}

/**
 * Live progress. These keys are exactly the non-identity half of the
 * `experiment_status` WS frame, so applying a frame to a list row is a spread.
 */
export interface ExperimentProgress {
  cells_total: number;
  /** Sparse: statuses with no cells are absent, not zero. */
  by_status: Partial<Record<ExperimentCellStatus, number>>;
  /** Observed spend from StepUsage, decimal string. */
  spend_usd: string;
  budget_usd: string;
  /**
   * Fraction of usage rows with a known cost_source. Below 1.0 the budget is
   * partially UNENFORCED (unknown-cost rows count as zero against the cap),
   * which is why this is on the row rather than buried in a detail view.
   * `null` = no usage rows yet, which is not the same as 0.0.
   */
  cost_coverage: number | null;
  /**
   * True when the experiment says "running" but no cell is live and pending
   * cells remain - i.e. the in-process pump died with the backend. Reported,
   * never hidden; POST /resume is the fix.
   */
  stalled: boolean;
}

/** A row of GET /api/experiments. */
export interface ExperimentSummary extends Experiment, ExperimentProgress {}

/** One matrix cell. */
export interface ExperimentCell {
  id: string;
  experiment_id: string;
  cell_index: number;
  /** cell_index // repeat - repeats of one variant share it. */
  variant_index: number;
  agent: string;
  model: string | null;
  prompt_template_id: string | null;
  /** The frozen prompt version that actually ran (1-based), null for default. */
  prompt_version: number | null;
  label: string | null;
  repeat_index: number;
  /** Convenience mirror for the "open this run" link; NOT the durable link. */
  pipeline_run_id: string | null;
  status: ExperimentCellStatus;
  error: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  /**
   * Measurements joined from StepUsage / TestRun. Present on both the detail's
   * cells and GET /results (one schema, `ExperimentCellRead`) — there are no
   * cost or test columns ON the cell row, so these are always a live join.
   */
  cost_usd: string | null;
  cost_coverage: number | null;
  wall_clock_ms: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  tests_passed: number;
  tests_failed: number;
  tests_skipped: number;
}

/** GET /api/experiments/{id}. */
export interface ExperimentDetail extends ExperimentSummary {
  cells: ExperimentCell[];
}

// -----------------------------------------------------------------------------
// Dry run (the guardrail)
// -----------------------------------------------------------------------------

export interface ExperimentEstimateVariant {
  variant_index: number;
  label: string;
  agent: string;
  model: string | null;
  prompt_template_id: string | null;
  runs: number;
  /**
   * ALWAYS a decimal string, and "0.000000" when `basis` is "no-history" —
   * an unpriced variant contributes nothing to the total. READ `basis`, NOT
   * this field, to decide whether a number may be shown: rendering the zero
   * as "$0.00" is precisely the silent misreport this phase forbids.
   */
  estimate_usd: string;
  basis: EstimateBasis;
  /** How many historical StepUsage rows the median came from. */
  samples: number;
}

/**
 * POST /api/experiments {"dry_run": true} (creates nothing) and
 * GET /api/experiments/{id}/estimate. `estimated_cost_usd` is a LOWER BOUND
 * whenever `estimate_basis` is not "historical-median", and the UI must say so
 * instead of printing a confident total.
 */
export interface ExperimentEstimate {
  cells: number;
  models: number;
  prompts: number;
  repeat: number;
  runs: number;
  estimated_cost_usd: string;
  estimate_basis: EstimateBasis;
  per_variant: ExperimentEstimateVariant[];
  budget_usd: string;
  within_budget: boolean;
  /**
   * Always true: the cap is checked against observed StepUsage before every
   * single dispatch, so an estimate the platform cannot prove never disables
   * enforcement.
   */
  budget_enforced_at_dispatch: boolean;
  /** Rendered VERBATIM. Each one names a real hazard (unpriced model, push fan-out). */
  warnings: string[];
}

// -----------------------------------------------------------------------------
// Requests
// -----------------------------------------------------------------------------

export interface ExperimentCreate {
  name: string;
  description?: string;
  target_type: ExperimentTargetType;
  target_id: string;
  repo_id?: string;
  matrix: ExperimentMatrix;
  verify?: ExperimentVerify | null;
  budget_usd: string;
  max_concurrency?: number;
  cell_timeout?: number;
  push_branches?: boolean;
  created_by?: string | null;
  /** true -> 200 with an ExperimentEstimate and NOTHING is created. */
  dry_run?: boolean;
}

export interface ExperimentUpdate {
  name?: string;
  description?: string;
  budget_usd?: string;
  max_concurrency?: number;
  cell_timeout?: number;
  push_branches?: boolean;
  /** Only accepted while status === "draft"; a launched matrix is frozen (422). */
  matrix?: ExperimentMatrix;
  verify?: ExperimentVerify | null;
}

/**
 * POST /api/experiments/{id}/launch (202). The UI deliberately does NOT read
 * this body - it refetches the detail - so the launch contract cannot break
 * the page.
 */
export interface ExperimentLaunchResponse {
  id: string;
  status: ExperimentStatus;
  cells_created: number;
  dispatched: number;
  estimated_cost_usd: string;
  estimate_basis: EstimateBasis;
  warnings: string[];
}

/** POST /api/experiments/{id}/abort. Running cells are NOT cancelled. */
export interface ExperimentAbortResponse {
  id: string;
  status: ExperimentStatus;
  cancelled: number;
  still_running: number;
}

/** POST /api/experiments/{id}/resume. */
export interface ExperimentResumeResponse {
  id: string;
  status: ExperimentStatus;
  dispatched: number;
  /** Cells stuck in `dispatching` after a restart, put back to `pending`. */
  reset_dispatching: number;
}

// -----------------------------------------------------------------------------
// Leaderboard (12.6.5 REPORTS; it does not rank)
// -----------------------------------------------------------------------------

/** Pass-rate for one acceptance criterion under one variant. */
export interface LeaderboardCriterionRate {
  /** null = tests that ran under no criterion (aggregated, never dropped). */
  criterion_id: string | null;
  criterion_text: string | null;
  passed: number;
  failed: number;
  /** Excluded from the denominator. */
  skipped: number;
  /** null with a `reason` when the denominator is 0. NEVER 0.0 for "no data". */
  pass_rate: number | null;
  reason: string | null;
}

export interface LeaderboardVariant {
  /** -1 on the feature board's "non-experiment runs" baseline row. */
  variant_index: number;
  label: string;
  agent: string;
  model: string | null;
  prompt_template_id: string | null;
  prompt_version: number | null;
  cells_total: number;
  /** Cells that produced a measurement (passed + failed) — the denominator. */
  cells_measured: number;
  cells_errored: number;
  cells_skipped_budget: number;
  error_rate: number;
  /** MACRO average over criteria (equal weight per criterion) — the headline. */
  pass_rate: number | null;
  /** Pooled rate, carried as a footnote so one big criterion cannot own the number. */
  pass_rate_micro: number | null;
  /** Why `pass_rate` is null. Present whenever it is. */
  reason: string | null;
  criteria: LeaderboardCriterionRate[];
  unlinked_tests: LeaderboardCriterionRate | null;
  cost_usd_total: string;
  /** Median, not mean - cost is heavy-tailed at any n. */
  cost_usd_per_run_median: string | null;
  cost_coverage: number | null;
  wall_clock_ms_median: number | null;
  input_tokens_total: number;
  output_tokens_total: number;
  /** n < 3 repeats: point values only, no comparison. */
  insufficient_repeats: boolean;
  /** Rendered verbatim as badges on the row. */
  warnings: string[];
}

/**
 * Both `GET /api/experiments/{id}/leaderboard` and
 * `GET /api/leaderboards/feature/{id}` — one shape, discriminated by which of
 * `experiment_id` / `feature_id` is set. The feature board additionally emits
 * a `variant_index: -1` row labelled "non-experiment runs" for the repo's
 * ordinary CI baseline.
 */
export interface Leaderboard {
  experiment_id: string | null;
  feature_id: string | null;
  /** Always false in 12.6.5. Ranking arrives with 13.4. */
  ranked: boolean;
  /** Rendered VERBATIM and ALWAYS (see NOT_RANKED_NOTE). */
  note: string;
  variants: LeaderboardVariant[];
  /** Pooled coverage across the board; below 0.9 the warnings say so. */
  cost_coverage: number | null;
  warnings: string[];
}

// -----------------------------------------------------------------------------
// WS frames (subsets of the REST rows - see contract point 3 at the top)
// -----------------------------------------------------------------------------

export interface ExperimentStatusFrame extends ExperimentProgress {
  id: string;
  name: string;
  status: ExperimentStatus;
}

export type ExperimentCellFrame = Pick<
  ExperimentCell,
  | 'id'
  | 'experiment_id'
  | 'cell_index'
  | 'variant_index'
  | 'status'
  | 'pipeline_run_id'
  | 'label'
  | 'agent'
  | 'model'
  | 'prompt_template_id'
  | 'prompt_version'
>;
