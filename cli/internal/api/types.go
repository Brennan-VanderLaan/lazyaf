package api

// Typed response shapes, mirroring backend/app/schemas/{repo,debug,testref}.py
// and the dict backend/app/routers/repos.py:381-385 returns for branches.
// Field names are the wire names; a field the server adds is ignored, a
// field it removes reads as the zero value - the same tolerance the Python
// CLI had with dicts, made explicit.

// Repo is schemas/repo.py RepoRead.
type Repo struct {
	ID             string  `json:"id"`
	Name           string  `json:"name"`
	RemoteURL      *string `json:"remote_url"`
	DefaultBranch  string  `json:"default_branch"`
	IsIngested     bool    `json:"is_ingested"`
	InternalGitURL string  `json:"internal_git_url"`
	CreatedAt      string  `json:"created_at"`
}

// RepoCreate is schemas/repo.py RepoCreate (the ingest request body).
type RepoCreate struct {
	Name          string  `json:"name"`
	RemoteURL     *string `json:"remote_url,omitempty"`
	DefaultBranch string  `json:"default_branch,omitempty"`
	Path          *string `json:"path,omitempty"`
}

// RepoIngest is schemas/repo.py RepoIngest (the ingest response).
type RepoIngest struct {
	ID             string `json:"id"`
	Name           string `json:"name"`
	InternalGitURL string `json:"internal_git_url"`
	CloneURL       string `json:"clone_url"`
}

// BranchInfo is one entry of GET /api/repos/{id}/branches (routers/repos.py:372-379).
type BranchInfo struct {
	Name      string `json:"name"`
	Commit    string `json:"commit"`
	IsDefault bool   `json:"is_default"`
	IsLazyaf  bool   `json:"is_lazyaf"`
}

// Branches is the whole answer of GET /api/repos/{id}/branches.
type Branches struct {
	Branches      []BranchInfo `json:"branches"`
	DefaultBranch string       `json:"default_branch"`
	Total         int          `json:"total"`
}

// CloneURL is GET /api/repos/{id}/clone-url.
type CloneURL struct {
	CloneURL string `json:"clone_url"`
}

// ReconcileRefItem / ReconcileRequest / ReconcileResponse are
// schemas/testref.py's pinned contract #6.
type ReconcileRefItem struct {
	LazyafTestID string  `json:"lazyaf_test_id"`
	FilePath     *string `json:"file_path"`
}

type ReconcileRequest struct {
	RepoID string             `json:"repo_id"`
	Refs   []ReconcileRefItem `json:"refs"`
}

type ReconcileResponse struct {
	Created  int `json:"created"`
	Updated  int `json:"updated"`
	Orphaned int `json:"orphaned"`
}

// The debug schemas (schemas/debug.py).

type DebugRerunRequest struct {
	Breakpoints       []string `json:"breakpoints"`
	UseOriginalCommit bool     `json:"use_original_commit"`
	CommitSHA         *string  `json:"commit_sha,omitempty"`
	Branch            *string  `json:"branch,omitempty"`
	TimeoutSeconds    *int     `json:"timeout_seconds,omitempty"`
}

type DebugRerunResponse struct {
	RunID          string `json:"run_id"`
	DebugSessionID string `json:"debug_session_id"`
	JoinCommand    string `json:"join_command"`
}

type DebugStepInfo struct {
	Key   string `json:"key"`
	Name  string `json:"name"`
	Index int    `json:"index"`
	Type  string `json:"type"`
}

type DebugCommitInfo struct {
	SHA     string `json:"sha"`
	Message string `json:"message"`
	Branch  string `json:"branch"`
}

type DebugRuntimeInfo struct {
	Host         string  `json:"host"`
	Orchestrator string  `json:"orchestrator"`
	Image        string  `json:"image"`
	ImageSHA     *string `json:"image_sha"`
}

// DebugSessionInfo carries NO token (schemas/debug.py's module docstring):
// the join credential is minted on demand by POST /join-token.
type DebugSessionInfo struct {
	ID                      string           `json:"id"`
	PipelineRunID           string           `json:"pipeline_run_id"`
	OriginalRunID           *string          `json:"original_run_id"`
	Status                  string           `json:"status"`
	CurrentStep             *DebugStepInfo   `json:"current_step"`
	Commit                  DebugCommitInfo  `json:"commit"`
	Runtime                 DebugRuntimeInfo `json:"runtime"`
	Logs                    string           `json:"logs"`
	JoinCommand             string           `json:"join_command"`
	ExpiresAt               *string          `json:"expires_at"`
	CreatedAt               *string          `json:"created_at"`
	EndedAt                 *string          `json:"ended_at"`
	Breakpoints             []string         `json:"breakpoints"`
	BreakpointsHit          []string         `json:"breakpoints_hit"`
	BreakpointsPending      []string         `json:"breakpoints_pending"`
	AttachAvailable         bool             `json:"attach_available"`
	AttachUnavailableReason *string          `json:"attach_unavailable_reason"`
	ConnectionMode          *string          `json:"connection_mode"`
	EndReason               *string          `json:"end_reason"`
}

type DebugJoinTokenResponse struct {
	Token       string `json:"token"`
	ExpiresAt   string `json:"expires_at"`
	JoinCommand string `json:"join_command"`
}

type DebugResumeRequest struct {
	ClearRemaining bool `json:"clear_remaining"`
}

type DebugResumeResponse struct {
	Status         string  `json:"status"`
	NextBreakpoint *string `json:"next_breakpoint"`
}

type DebugExtendRequest struct {
	AdditionalMinutes int `json:"additional_minutes"`
}

type DebugExtendResponse struct {
	ExpiresAt string `json:"expires_at"`
	Clamped   bool   `json:"clamped"`
}

type DebugAbortResponse struct {
	Status    string `json:"status"`
	EndReason string `json:"end_reason"`
}
