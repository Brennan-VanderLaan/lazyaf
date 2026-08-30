# LazyAF - Implementation Plan

> Visual orchestrator for AI agents to handle feature development via Trello-style cards

## What is LazyAF?

LazyAF is a local-first CI/CD platform that integrates AI agents as first-class citizens. Instead of writing GitHub Actions YAML, you define pipelines with a mix of:

- **Agent steps**: Claude or Gemini implements features, fixes tests, reviews code
- **Script steps**: Traditional shell commands (lint, test, build)
- **Docker steps**: Commands in isolated container images

The core workflow:
1. **Ingest** a repo via CLI (`lazyaf ingest /path/to/repo`)
2. **Create cards** describing features or tasks
3. **Start work** -> Runner clones, executes, pushes to internal git server
4. **Pipeline triggers** -> Tests run, AI fixes failures, auto-merge on success
5. **Land changes** to real remote when ready (`lazyaf land`)

---

## Long-Term Vision: Specification-Driven Development

> Sprint reviews, PRs, and code review evolved to mentor humans. With LLMs in the loop, the leverage shifts from *implementation review* to *specification fidelity* — does the result match the product intent, and can we prove it repeatedly?

LazyAF is moving toward a model where humans **over-specify what the software must do** (features, user stories, acceptance criteria) and LLMs handle implementation. The platform's job is to:

1. **Capture intent** — features, user stories, and acceptance criteria live in a queryable database, not scattered across Jira/Notion/heads.
2. **Tie tests back to intent** — every test in every repo declares which acceptance criterion it covers, and every run reports back. Test history is keyed to `(criterion, commit, model, prompt)`.
3. **Run experiments, not just builds** — re-run the same card across multiple model/prompt combinations and compare pass-rates per criterion. Make model + prompt selection an evidence-driven decision.
4. **Curate context for parallel agents** — the spec DB lets the platform hand each agent the relevant slice of intent (instead of stuffing the whole codebase into a context window).
5. **Enable cross-repo features** — a "feature" can span multiple repos (frontend + backend + infra), and the platform tracks delivery across all of them.

This direction reframes LazyAF as a *platform for software science*: experiments, leaderboards, regression dashboards, and reusable prompt structures — alongside the day-to-day "active project management" view (cards, kanban, pipelines).

The spec layer is being added to Phase 12.x in parallel with the runner architecture refactor (see Phases 12.2.5, 12.2.6, 12.6.5, 12.6.6).

---

## Project Structure

```
lazyaf/
|-- backend/
|   |-- app/
|   |   |-- main.py              # FastAPI app entry point
|   |   |-- config.py            # Settings
|   |   |-- database.py          # SQLAlchemy async setup
|   |   |-- models/              # SQLAlchemy models
|   |   |-- routers/             # API endpoints
|   |   |-- services/            # Business logic
|   |   |-- schemas/             # Pydantic models
|   |   +-- mcp/                 # MCP server for Claude Desktop
|   |-- git_repos/               # Internal bare git repos
|   |-- runner/
|   |   |-- Dockerfile
|   |   +-- entrypoint.py        # Runner execution logic
|   |-- pyproject.toml
|   +-- alembic/                 # DB migrations
|-- cli/                         # LazyAF CLI tool (ingest, land)
|   |-- pyproject.toml
|   +-- lazyaf/cli.py
|-- frontend/
|   |-- src/
|   |   |-- lib/
|   |   |   |-- components/      # Svelte components
|   |   |   |-- stores/          # State management
|   |   |   +-- api/             # API client
|   |   +-- routes/              # Pages
|   |-- package.json
|   +-- vite.config.ts
|-- historical-documents/        # Archived phase documentation
|-- docker-compose.yml
|-- PLAN.md                      # This file
+-- README.md
```

---

## Tech Stack

| Layer | Technology | Purpose |
|-------|------------|---------|
| Frontend | Svelte + Vite | Reactive UI, fast builds |
| Backend | FastAPI | Async Python API |
| Database | SQLite + SQLAlchemy | Simple persistence (PostgreSQL ready) |
| Queue | In-memory | Job management |
| Containers | Docker SDK for Python | Runner isolation |
| Real-time | WebSockets | Status updates |
| Git | Dulwich | Pure Python git server |
| MCP | FastMCP | Claude Desktop integration |

---

## Core Data Models

### Repo
```python
class Repo:
    id: UUID
    name: str
    remote_url: str | None       # Real remote (GitHub/GitLab)
    default_branch: str          # e.g., "dev" or "main"
    is_ingested: bool
```

### Card
```python
class CardStatus(Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    FAILED = "failed"

class Card:
    id: UUID
    repo_id: UUID
    title: str
    description: str
    status: CardStatus
    branch_name: str | None
    step_type: StepType          # agent | script | docker
    step_config: dict            # Type-specific config
```

### Pipeline
```python
class Pipeline:
    id: UUID
    repo_id: UUID
    name: str
    steps: list[PipelineStep]    # Ordered execution
    triggers: list[TriggerConfig]

class PipelineStep:
    name: str
    type: StepType               # agent | script | docker
    config: dict
    on_success: str              # "next" | "stop" | "merge:{branch}"
    on_failure: str              # "next" | "stop" | "trigger:{card_id}"
    continue_in_context: bool    # Preserve workspace
```

---

## Specification Layer Models

> Introduced in Phase 12.2.5. These models capture *what the software must do* and let the platform measure whether AI-generated changes still satisfy intent. Hierarchy is intentionally shallow: `Feature -> UserStory -> AcceptanceCriterion`. Tests and runs are orthogonal entities that join back to criteria.

### Feature
A product capability. Cross-repo by design — a single feature can span frontend, backend, infra, etc.

```python
class Feature:
    id: UUID
    name: str
    description: str             # Markdown, free-form product narrative
    repo_ids: list[UUID]         # Repos this feature touches (one or many)
    status: FeatureStatus        # proposed | active | shipped | deprecated
    owner: str | None            # Free-form (email, handle, team name)
    created_at: datetime
    updated_at: datetime
```

### UserStory
A natural-language behavior expectation in the gherkin spirit (less rigid). Belongs to one feature.

```python
class UserStory:
    id: UUID
    feature_id: UUID
    title: str                   # "User can revoke an API key"
    persona: str | None          # "As a security-conscious admin"
    narrative: str               # Free-form: "When X, then Y, so that Z"
    repo_ids: list[UUID]         # Subset of parent feature's repos
    priority: int                # Simple integer, not story points
    status: StoryStatus          # draft | accepted | in_progress | done | blocked
    created_at: datetime
    updated_at: datetime
```

### AcceptanceCriterion
A single, testable expectation. Natural language; one or more `TestRef`s prove it.

```python
class AcceptanceCriterion:
    id: UUID
    user_story_id: UUID
    description: str             # "Revoked keys return 401 within 60s globally"
    is_required: bool            # Story-blocking vs nice-to-have
    created_at: datetime
```

### TestRef
A pointer from a test in the application repo back to one or more acceptance criteria. The application's test suite emits a manifest declaring its `lazyaf_test_id`s; the platform reconciles that manifest against TestRefs to detect drift (orphaned tests, uncovered criteria).

```python
class TestRef:
    id: UUID                     # Stable platform-side ID
    lazyaf_test_id: str          # Stable repo-side identifier (decorator/sidecar)
    repo_id: UUID
    file_path: str               # e.g., "tests/api/test_keys.py"
    test_name: str               # e.g., "test_revoked_key_returns_401"
    framework: str               # "pytest" | "vitest" | "go-test" | "custom"
    criterion_ids: list[UUID]    # Many-to-many with AcceptanceCriterion
    last_seen_commit: str | None # SHA of latest commit where test was observed
    is_orphaned: bool            # True if test_id no longer found in repo
```

### TestRun
The result of executing one TestRef. Joined to commit, and (when run inside an experiment) to model + prompt.

```python
class TestRun:
    id: UUID
    test_ref_id: UUID
    pipeline_run_id: UUID | None # Pipeline that produced this run
    step_execution_id: UUID | None
    commit_sha: str
    repo_id: UUID
    status: TestStatus           # passed | failed | skipped | error
    duration_ms: int
    output: str | None           # Truncated stdout/stderr or pointer to artifact
    model: str | None            # e.g., "claude-opus-4-7" - set inside experiments
    prompt_template_id: UUID | None
    prompt_version: int | None
    experiment_id: UUID | None
    created_at: datetime
```

### Experiment
A user-defined run that evaluates one or more (model, prompt) tuples against a card / story / feature. Produces TestRuns tagged with the matrix coordinates so leaderboards can aggregate.

```python
class Experiment:
    id: UUID
    name: str
    description: str
    target_type: str             # "card" | "user_story" | "feature"
    target_id: UUID
    matrix: dict                 # {"models": [...], "prompts": [...], "repeat": N}
    status: ExperimentStatus     # draft | running | complete | aborted
    created_by: str
    created_at: datetime
    completed_at: datetime | None
```

### PromptTemplate
A versioned, reusable prompt. Leaderboards rank `(template_id, version, model)` by pass-rate per criterion.

```python
class PromptTemplate:
    id: UUID
    name: str
    purpose: str                 # "implement-from-story" | "fix-failing-test" | etc.
    versions: list[PromptVersion]

class PromptVersion:
    id: UUID
    template_id: UUID
    version: int
    body: str                    # The prompt itself, with placeholders
    placeholders: list[str]      # e.g., ["{story_narrative}", "{failing_test_output}"]
    created_at: datetime
    notes: str | None
```

### StepUsage  *(Phase 12.5 — effort telemetry)*
Per-agent-step resource accounting. Without this, `TestRun` records *what happened*
and nothing records *what it cost* — and the Benchmark harness (Milestone 13) has no
effort axis. It rides the control-layer protocol as a fourth channel alongside
status / logs / test-results, so it must land WITH 12.5's agent-step migration
rather than after it (12.2.6 became a retrofit precisely because 12.3 froze first).

```python
class StepUsage:
    id: UUID
    step_execution_id: UUID
    step_run_id: UUID | None
    provider: str                # "anthropic" | "google" | "openai-compatible" | "self-hosted"
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    cost_usd: Decimal | None     # CLI-reported where available, else derived
    cost_source: str             # "cli-reported" | "gpu-node" | "estimated" | "unknown"
    wall_clock_ms: int
    container_seconds: float | None
    created_at: datetime
```

> **Cost sources (owner decision 2026-08-29):** the agent CLIs report their own
> token counts and dollar cost — the control runtime scrapes that at step end
> (`cost_source="cli-reported"`). Self-hosted / runpod-style nodes have no
> per-token bill, so their dollars come from a node-rate x occupancy model
> (`cost_source="gpu-node"`). Both land on one comparable USD axis; no separate
> pricing table is needed while the CLIs keep reporting cost.

### BenchmarkSuite / BenchmarkCase  *(Milestone 13)*
A corpus of repos pinned at known states, each with a task and a definition of
"solved". Cases are the fixtures a loop is benchmarked against.

```python
class BenchmarkSuite:
    id: UUID
    name: str                    # "core-v1"
    description: str
    tags: list[str]              # verticals covered

class BenchmarkCase:
    id: UUID
    suite_id: UUID
    slug: str                    # "flask-api.missing-pagination"
    repo_id: UUID                # an INGESTED fixture repo (internal git server)
    base_commit_sha: str         # every trial starts here, byte-identical
    task_statement: str          # what the agent is told to do
    vertical: str                # "web-api" | "data-pipeline" | "frontend" | "cli" | ...
    complexity: str              # "trivial" | "small" | "medium" | "large"
    fail_to_pass: list[str]      # lazyaf_test_ids that MUST flip red -> green
    pass_to_pass: list[str]      # lazyaf_test_ids that must STAY green (regression guard)
    user_story_id: UUID | None   # layered criteria: the human-meaningful "why"
    loop_defaults: dict          # {max_iterations, budget_usd, per_step_timeout}
    contamination_risk: str      # "high" (public repo, likely in training data)
                                 # | "medium" | "low" (self-authored / post-cutoff)
    source_url: str | None       # upstream provenance for public fixtures
    license: str | None          # SPDX id - decides what the public bundle may ship
    test_command: str            # the PINNED oracle invocation, e.g. "pytest -q"
    oracle_file_hashes: dict     # {path: sha256} for every file carrying an oracle
                                 # id - an agent that edits the oracle to pass is
                                 # cheating, and this is how the trial detects it
    quarantined_tests: list[str] # ids ejected by the flake screen, kept on the record
    reference_patch: str | None  # gold patch where upstream has one -> enables the
                                 # "is this case solvable at all" control
    solvable_verified: bool      # the gold-patch control passed
    created_at: datetime
```

### StrategyTemplate  *(Milestone 13 — the independent variable)*
A strategy is a graph of activity plus how models are assigned to its roles. It
is DATA: authoring a new strategy means writing a template, not changing code.

```python
class StrategyTemplate:
    id: UUID
    slug: str                    # "planner-fanout-8" | "adversarial-3" | "one-shot"
    description: str
    graph: dict                  # a v2 pipeline graph: steps + edges + fan-out/join.
                                 # Step configs carry ROLE placeholders, not models:
                                 #   {"role": "planner"} / {"role": "worker", "fanout": 8}
    roles: list[str]             # ["planner", "worker", "integrator", "reviewer"]
    loop_policy: dict            # {max_iterations, budget_usd, stop_on}
    parallelism: dict            # {max_concurrent_workers, branch_per_worker: bool}
    variables: dict              # {"K": {"type":"int","default":4,"min":1,"max":32}} -
                                 # what makes planner-fanout-4 and -16 the SAME
                                 # template, so a K-sweep is one template not sixteen
    integration: dict            # HOW parallel work rejoins - itself under test:
                                 # {"policy": "sequential-merge" | "rebase-onto-trunk"
                                 #            | "cherry-pick" | "agent-composed",
                                 #  "on_conflict": "fail" | "resolver-agent" | "human"}
    created_at: datetime
```

> **Why roles, not models:** the owner's leading hypothesis is that a high-end
> model writing instructions for a fan-out of small models beats one big model
> doing everything. That strategy is only expressible if a template says "planner"
> and "worker" and the *trial* binds those roles to concrete models — otherwise
> every model mix is a different template and nothing is comparable.

> **Parallelism is git-native — LazyAF is the bridge.** K workers do not fight
> over one checkout: each gets its own workspace cloned at the case's base commit
> on **its own branch**, works freely, and commits. Integration is then a *git
> merge*, not a file-level reconciliation — which is precisely the substrate this
> platform already is:
>
> | Needed for fan-out | Already shipped |
> |---|---|
> | Per-worker isolation | Internal git server (bare repo per project) + workspace-per-clone at a pinned commit |
> | Independent work | Branch-per-unit-of-work, the model cards have used since Phase 2 |
> | Integration | `git_server.merge_branch` / `rebase_branch` |
> | Conflict handling | `POST /api/cards/{id}/resolve-conflicts` returns STRUCTURED conflicts and accepts resolved contents; `ConflictResolver.svelte` is the human path |
> | Review before merge | The existing approve/reject diff flow |
>
> Two consequences. First, fan-out needs far less new machinery than a
> from-scratch harness would: the orchestrator allocates branches and calls
> merges the platform already performs. Second — and more interesting —
> **conflict resolution is itself an agent-addressable task**, because conflicts
> come back as structured data rather than as a wall of `<<<<<<<` markers. A
> strategy can legitimately say "on conflict, spawn a resolver agent", which is a
> strategy variant nobody can benchmark on a single-sandbox harness.
>
> So **integration policy becomes a measured variable, not a fixed detail**:
> sequential merge, rebase-onto-trunk, cherry-pick, agent-composed integration,
> resolver-on-conflict. Integration conflict rate and resolution cost are
> outcomes of the strategy under test — plausibly the dominant cost of aggressive
> parallelism, and exactly the number that decides whether the pattern is worth
> it.

### Trial / TrialIteration  *(Milestone 13)*
A Trial is one loop run of one case under one (model, prompt, policy) variant.
TrialIteration is the per-cycle record — the cost *curve*, which is the actual
science: not just "did it solve it" but "was iteration 4 worth paying for".

```python
class Trial:
    id: UUID
    experiment_id: UUID | None   # set when part of a matrix fan-out
    benchmark_case_id: UUID
    strategy_template_id: UUID   # THE independent variable
    model_assignment: dict       # {"planner": "claude-opus-5", "worker": "haiku-4.5",
                                 #  "integrator": "claude-sonnet-5"} - a strategy may
                                 # use several models in different roles, so cost is
                                 # attributed PER ROLE from StepUsage, never per trial
    prompt_template_id: UUID | None
    prompt_version: int | None
    loop_policy: dict            # {max_iterations, budget_usd, stop_on}
    status: str                  # running | solved | failed | budget_exhausted | error
    template_variables: dict     # {"K": 16} - a trial that does not record the K it
                                 # ran at cannot be reproduced
    target_met: bool             # all fail_to_pass green at the final commit
    clean: bool                  # zero pass_to_pass broken at the final commit
                                 # solved == target_met AND clean. Storing the halves
                                 # separately is the only way to compute a regression
                                 # rate that is not definitionally zero.
    solved_at_iteration: int | None   # None = never solved (a CENSORED observation,
                                 # not a missing one - see the metrics spec)
    budget_overrun_usd: Decimal  # spend already in flight when the cap hit; recorded
                                 # rather than hidden
    queued_ms: int               # excluded from wall_clock, never silently folded in
    machine_profile: str         # "local-16c-64g" | "runpod-a100" | ... - a speedup
                                 # number is meaningless without the host it ran on
    host_concurrency_limit: int  # what the fan-out was actually ALLOWED to run, which
                                 # is not always the K it asked for
    error_class: str | None      # "infra" | "provider" | "oracle_tampered" |
                                 # "base_state_invalid" - trials that failed for
                                 # reasons that are not the strategy's fault must be
                                 # excludable from denominators, and visibly so
    iterations_used: int
    total_cost_usd: Decimal
    cost_by_role: dict           # {"planner": 0.42, "worker": 1.10, ...}
    total_input_tokens: int
    total_output_tokens: int
    wall_clock_ms: int           # co-headline: parallelism buys latency with money,
                                 # so a cost-only board would rank fan-out as worse
                                 # while hiding the entire point of it
    serial_equivalent_ms: int | None  # summed step time; wall_clock/serial = speedup
    integration_conflicts: int   # merges that did not apply cleanly
    conflicts_resolved: int      # of those, how many a resolver agent/human fixed
    integration_cost_usd: Decimal  # what rejoining the work cost - the tax on
                                 # parallelism, and the number that decides whether
                                 # fan-out actually pays
    base_commit_sha: str
    final_commit_sha: str | None
    branch: str
    # --- provenance: what makes this number falsifiable by someone else ---
    harness_version: str         # git describe of LazyAF at trial time
    image_hashes: dict           # {"lazyaf-base": "1f9bff1a6d1e", ...} - already
                                 # stamped as content-hash labels by build_images.py
    model_version: str | None    # the provider's exact version, not just the family
    determinism: dict            # {temperature, seed, top_p} where exposed
    suite_version: str           # corpus revision the case came from
    created_at: datetime
    completed_at: datetime | None

class TrialIteration:
    id: UUID
    trial_id: UUID
    iteration_index: int
    pipeline_run_id: UUID        # each iteration IS a visible pipeline run
    commit_sha: str | None       # what the agent produced this cycle
    lines_added: int
    lines_removed: int
    files_touched: int
    fail_to_pass_passed: int
    fail_to_pass_total: int
    pass_to_pass_broken: int     # regressions this iteration introduced
    criteria_verified: int
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    duration_ms: int
    created_at: datetime
```

### Card ↔ Spec Links

The existing `Card` model gains optional links into the spec layer. Cards are still the active unit of work; the spec layer is the meta layer of *why*.

```python
class Card:
    # ... existing fields ...
    feature_id: UUID | None          # If this card delivers part of a feature
    user_story_id: UUID | None       # If this card delivers a story
    promotes_to_feature: bool        # Marks card for "promote to feature" workflow
```

A card with neither link is fine — it's a pure work item (e.g., a bug fix, a chore). When work outgrows a card, the user can promote it to a `Feature` and the card becomes the first child story.

---

## API Summary

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/repos` | Repo management |
| `POST /api/repos/ingest` | Ingest local repo |
| `GET/POST /api/repos/{id}/cards` | Card CRUD |
| `POST /api/cards/{id}/start` | Trigger agent work |
| `GET/POST /api/pipelines` | Pipeline CRUD |
| `POST /api/pipelines/{id}/run` | Run pipeline |
| `GET /api/pipeline-runs/{id}` | Run status |
| `/git/{id}.git/*` | Internal git server |
| `/ws` | WebSocket for real-time updates |

**Specification Layer (Phase 12.2.5+, planned):**

| Endpoint | Purpose |
|----------|---------|
| `GET/POST /api/features` | Feature CRUD (cross-repo) |
| `GET/POST /api/features/{id}/stories` | User story CRUD |
| `GET/POST /api/stories/{id}/criteria` | Acceptance criterion CRUD |
| `GET/POST /api/test-refs` | Test reference registry |
| `POST /api/test-refs/reconcile` | Compare repo manifest vs registry |
| `POST /api/test-results/ingest` | Bulk ingest TestRuns from a step |
| `GET /api/criteria/{id}/history` | Pass/fail history per (model, prompt) |
| `GET/POST /api/experiments` | Experiment CRUD + launch |
| `GET /api/experiments/{id}/results` | Matrix results, ready for leaderboard |
| `GET/POST /api/prompts` | Prompt template + version CRUD |
| `GET /api/leaderboards/feature/{id}` | Aggregated pass-rate per (prompt, model) |

Full API: 31 MCP tools for Claude Desktop orchestration (will grow with spec-layer tools).

---

## Agent Guidelines for This Repo

When working on LazyAF, agents should:

1. **Understand the architecture**: Backend (FastAPI) + Frontend (Svelte) + Runners (Docker)
2. **Check existing patterns**: Look at similar routers/services before creating new ones
3. **Run tests after changes**: `pytest` for backend, `npm test` for frontend
4. **Use the internal git server**: Changes go to internal server, not GitHub
5. **Follow the step type model**: All work is agent/script/docker steps
6. **Reference historical docs**: See `historical-documents/` for completed phase details

---

## Completed Phases Summary

Detailed documentation for completed phases is in `historical-documents/`.

| Phase | Name | Status | Key Deliverable |
|-------|------|--------|-----------------|
| 1 | Project Foundation | COMPLETE | `docker-compose up` runs both services |
| 2 | Repo & Card Management | COMPLETE | Create cards on kanban board |
| 3-3.75 | Runner Pool & Git Server | COMPLETE | Internal git server, runner isolation |
| 4 | Agent Integration | COMPLETE | Cards trigger Claude Code |
| 5 | Review Flow | COMPLETE | Approve/reject workflow |
| 6 | Polish | ONGOING | Quality of life improvements |
| 7 | MCP Interface | COMPLETE | 31 tools for Claude Desktop |
| 8 | Test Result Capture | COMPLETE | Test results displayed in UI |
| 8.5 | CI/CD Foundation | COMPLETE | Script/docker step types |
| 9-9.1 | Pipelines | COMPLETE | Multi-step workflows with context |
| 12.0 | Unify Entrypoints | PARTIAL | runner-common package exists + tested; runner images do not import it yet (12.5/12.8) |
| 12.1 | LocalExecutor + State Machine | COMPLETE | Step state machine, idempotency, LocalExecutor, crash recovery |
| 0 | Self-Hosting Bootstrap | COMPLETE | LazyAF runs LazyAF's CI: tiered dogfood pipeline + ci_gate floors/skip baseline, alembic, test-mode API |
| 12.2-INT | Workspace Persistence + Executor Wiring | COMPLETE | Workspace model/service, repo population, router -> LocalExecutor by default, per-step `executor` recorded |
| 12.2.5 | Specification Data Model | COMPLETE | Feature/UserStory/AcceptanceCriterion/PromptTemplate + API + MCP + Specs UI |
| 12.3 | Control Layer & Step Images | COMPLETE | Built lazyaf-* images, in-container control runtime -> live steps API, terminal reconciliation |

---

## Current Status

> Last updated 2026-08-29 (end of session). Plan of record: "Milestone 12 —
> Attempt #3 Roadmap" below. Salvage map + post-mortem of the abandoned first
> attempt: `upcoming/failure_01-salvage-audit.md`.

**Milestone 12 progress: Phase 0, 12.2-INT, 12.2.5 and 12.3 are COMPLETE**, each
signed off by its own dogfood exit gate (LazyAF's CI, running on LazyAF). Twelve
green push-triggered self-hosted runs to date; ~1,520 backend tests + 78 frontend
unit tests green; last stable commit `69f3ef0`, pushed to origin.

| Phase | Status | Evidence |
|-------|--------|----------|
| 0 — Self-hosting bootstrap | COMPLETE | Push -> tiered dogfood pipeline, ci_gate floors + skip baseline enforced |
| 12.2-INT — Workspace persistence + executor wiring | COMPLETE | Steps run in ephemeral containers on persistent volumes; `executor='local'` verified per run |
| 12.2.5 — Specification data model | COMPLETE | Feature/UserStory/AcceptanceCriterion/PromptTemplate + API + MCP + Specs UI, seeded with the three north-star stories |
| 12.3 — Control layer & step images | COMPLETE | Real `lazyaf-{base,claude,test-runner}:dev` images, in-container runtime reporting to `/api/steps/*`, run #11 passed the gate |
| 12.2.6 — Test result tie-back | COMPLETE | A push-triggered run wrote a TestRun joined to criterion fb95f11d: `passed / commit 2a513dd4 / main / us1.pipeline-outcome-gates-branch` |
| 12.4 — Script/docker steps fully ephemeral | COMPLETE | Runners agent-only; script/docker deleted from the three images AND runner-common; DooD anchor retired |
| 12.5 — Agent steps via the control layer | COMPLETE | Runners idle on every default path (asserted); StepUsage live at 1163/7 tokens on the gate |
| 12.6 — RemoteExecutor + runner agents | COMPLETE | 74 dormant contract tests now execute (0 skipped), polling stack DELETED, all 7 polling endpoints 404 on a live probe |
| 12.6.5 / 12.6.6 / 12.7 | IN PROGRESS | Three parallel lanes (experiments, spec-curated context, debug re-run) |
| 12.8 — Cleanup & polish | NOT STARTED | Epilogue; needs the owner's v1-format call |

**Phase 12.6: COMPLETE (2026-08-30).** The 74 contract tests ported dormant in
Phase 0 all execute now, zero skipped, and git diff proves neither file was
edited to fit the implementation - the spec came first by two months and won.
The polling stack is DELETED: no job_queue, no runner_pool (importing them
raises ModuleNotFoundError, policed by test_no_legacy_code with two mechanisms
that cannot silently skip), all seven polling endpoints 404 on a live probe,
and the three monolithic runner images are gone. Steps now run local OR remote
over a WebSocket runner protocol, with the step container still POSTing to
/api/steps/* either way because the step JWT is location-independent.

**Execution today**: script/docker steps flow pipeline_executor -> ExecutionRouter ->
LocalExecutor -> ephemeral control-mode container on a persistent workspace volume,
reporting status/logs to the live steps API, and shipping a test-result
manifest that joins back to acceptance criteria. Agent steps still take the
legacy card -> job -> queue -> polling runner path until 12.5.

**Phases 12.2.6 + 12.4: COMPLETE (2026-08-30).** The tie-back is proven on real
data, not just in tests: a push-triggered dogfood run wrote a TestRun joined to
criterion fb95f11d - `passed | commit 2a513dd4 | branch main |
us1.pipeline-outcome-gates-branch`. 9 TestRefs seeded and linked, 0 orphans.
Suite 1731 passed; gates T1 1657 / T2 60 / T3 17. Two second-order lessons, both
now pinned by tests: 12.3 moved alembic into the image so production boots
self-sufficiently, which meant DEV silently ran a pre-0004 versions directory
and believed itself at head (dev now mounts ./backend/alembic like app code);
and because manifest delivery is deliberately non-fatal to a step, the first
live run shipped three manifests into 404s and still gated clean - the gate now
fails on any manifest delivery problem, closing the R7 gap where 12.2.6 landed
without extending the ratchet to cover itself.

**Still true from the January-era assessment**: Phase 12.0's COMPLETE mark in the
table above remains aspirational — the three runner images still ship monolithic
entrypoints and do not import `runner-common` (adoption lands in 12.5/12.8).

**Abandoned attempt**: branch `failure_01` (12.0 -> 12.7 in two days, 2026-01-03/04).
Reference only — never merge it.

**Phase 0: COMPLETE (2026-08-29).** LazyAF gates LazyAF: a push to the internal
remote triggers the tiered dogfood pipeline (T1 1159 / T2 19 / T3 17 executed,
all three ci_gate floors enforced, run `cda4ddce` PASSED via push trigger).
Definition sync-on-push, trigger dedup, alembic migrations, test-mode API,
12.6 contract suite (dormant), frontend testids + vitest layer, Playwright
dogfood-live spec all landed; 10 confirmed review findings fixed pre-commit.

**Phase 12.2-INT + 12.2.5: COMPLETE (2026-08-29).** Dogfood CI runs end-to-end
on the new architecture: run 71d56980 (push-triggered) executed every step in
ephemeral containers on a persistent workspace volume, verify_executor
confirmed executor='local' for all 6 steps, all tier gates green (T1 1379 /
T2 41 / T3 17), workspace created+cleaned (0 leaked). Spec layer live with
the three north-star stories seeded; live WS step streaming in the UI with a
contract-pinning test. Ten more confirmed review findings fixed pre-commit;
dogfood run cccae257 caught a DooD landmine-2 seam the review missed (fixed).

**Phase 12.3: COMPLETE (2026-08-29).** Real images (lazyaf-{base,claude,
test-runner}:dev, reproducible content-hash builds), in-container control
runtime reporting to the live steps API, one reporting path with terminal
reconciliation, dogfood CI fully on the new stack. Exit gate: run #11 passed
with all tier gates green in control-mode containers and verify_executor
confirming executor='local' + delivered logs. Six dogfood iterations (#5-#11)
each caught a real environment seam host testing could not: repo uid
ownership, async completion timing, socket group across gosu, platform-
dependent tree-hash collation, cross-uid git trust, docker client timeout
under DooD load, sibling-network reachability — all regression-tested now.

**Wave 3 (12.2.6 + 12.4): PAUSED mid-implementation (2026-08-29, quota).**
Partial, unverified agent edits sit uncommitted in the working tree; nothing
pushed. Resume = reconcile the partial tree against the wave's pinned contracts,
finish, review, then dogfood-gate it.

Phase 12.1 deliverables (COMPLETE):
  - `StepExecution` model with unique `execution_key` for idempotency
  - `StepExecutionStatus` enum: pending → assigned → preparing → running → completing → completed/failed/timeout
  - `StepStateMachine` class with valid transition enforcement
  - `ExecutionService` for idempotent get_or_create semantics
  - `LocalExecutor` for Docker-based step execution (spawns containers, streams logs, handles timeouts)
  - Crash recovery: `recover_orphaned_executions()` on backend startup marks orphaned executions as failed
  - Docker socket mounted in docker-compose for local execution mode
  - 92 unit tests, 19 integration tests, 1 skipped (async timeout handling TODO)
  - Chaos tests: OOM handling, Docker unavailable, connection timeouts

The target workflow is now fully functional:
1. Ingest repos via CLI
2. Create cards describing features (or CI steps: script/docker)
3. Start work -> runner clones repo, executes step
4. Card completes -> reaches "in_review" status
5. **Pipeline triggers automatically** (if configured with card_complete trigger)
6. Pipeline runs tests/validation steps
7. **On pass**: Card auto-merged and marked done
8. **On fail**: Card marked failed (user can retry)

---

## Milestone 12 — Attempt #3 Roadmap (2026-08-29)

> Scope decision (owner, 2026-08-29): finish EVERYTHING numbered 12.x — the runner
> architecture arc AND the spec/eval layer (12.2.5, 12.2.6, 12.6.5, 12.6.6).
> 12.9 (Kubernetes) stays future. No external CI, ever: LazyAF gates LazyAF,
> starting on the legacy execution path and ratcheting onto the new architecture
> as it lands. This section supersedes the old "Why this order" note (12.2.6 now
> deliberately follows 12.3 — the protocol froze in January; the retrofit is planned,
> not accidental).

### History (why this is attempt #3)

- Attempt #1 (`failure_01`, 2026-01-03/04): 12.0 -> 12.7 in two days. Collapsed.
  Post-mortem + salvage map: `upcoming/failure_01-salvage-audit.md`.
- Attempt #2 (main, 2026-01-09/13): test infra first, 12.0-12.3 built as clean
  libraries — never wired into the live path.
- Attempt #3 (now): wire the dark libraries, finish the arc, with the platform
  gating its own development the whole way.

### North-star user stories (the e2e layer encodes these; 12.2.5 stores them)

US-1 "Commits land, AI workflows run" (self-hosted CI / dogfood)
     Given a repo ingested into LazyAF with a pipeline bound to a push trigger,
     when I push to the internal remote, the pipeline runs my steps (tests,
     builds, agent steps) in isolated containers, live status/logs stream to the
     UI, and the outcome gates the branch. Milestone acceptance bar: LazyAF runs
     LazyAF's own tdd suite this way — with the execution tiers ACTUALLY
     EXECUTING (see R4; a green run that skipped the Docker tier is a failure).

US-2 "Card dev loop"
     Given a card describing a feature, when I start it, an agent implements it
     on a branch; completion triggers the gating pipeline; on pass the card
     reaches review with a diff; approve merges to target. Continuously covered
     from 12.5 on by a zero-cost mock-agent e2e inside the dogfood suite.

US-3 "Compare bench"
     Given a workflow/card and a set of (model x prompt) variants, when I launch
     a comparison, each variant runs in isolation and I get a side-by-side of
     outcomes (pass-rates per criterion, diffs, cost/time). Named artifact:
     tdd/e2e/test_experiment_matrix.py (12.6.5's exit gate).

Standing note: the owner's remote ambition is runpod.io-style nodes hosting
cheap models (hybrid agentic programming). Such pods often run AS containers
with no Docker socket — so the 12.6 runner-agent protocol must NOT assume
Docker; the agent's executor seam stays pluggable (LocalExecutor today, native
or OpenAI-compatible agents later) without protocol changes.

### Standing rules (distilled from the failure_01 post-mortem — non-negotiable)

R1 NOTHING GOES DARK. New execution code is wired into the default dev compose
   path the day it lands, and the e2e suite runs through it. Routing is
   OBSERVABLE: StepRun records which executor ran it (local|legacy|remote), the
   dogfood pipeline asserts the expected executor per phase via the API, and a
   spy test proves locally-routed steps never enter job_queue. A silent
   fallback to legacy is indistinguishable from success without this.
R2 DELETE ONLY AFTER ACCEPTANCE. The legacy path stays callable until the
   replacement passes the ported contract suites end-to-end; removal is its own
   commit with every consumer (frontend, playground) migrated in it.
R3 ONE SOURCE OF TRUTH PER WIRE CONTRACT. Control-layer payloads, WS runner
   protocol, test manifests: shared pydantic schemas or contract tests pinning
   both sides, plus at least one real round-trip test per contract
   (container -> API -> DB row).
R4 NO FAKE GREEN. No `pass # architecture ensures this` tests. Skip budget is
   a committed baseline (`tdd/skip_baseline.json`: per-file skip reasons);
   the CI gate runs pytest with skip reporting and FAILS if (a) any
   environment-dependent skip appears that is not in the baseline, or (b) a
   tier's executed-test count drops below its committed floor. Un-skipping
   requires shrinking the baseline in the same commit (the ratchet — the 137
   dormant 12.6 contract tests must hit zero skips by 12.6's gate).
   xfail(strict=True) where a target is known-missing.
R5 ASYNC-FIRST. Pipeline runs execute as asyncio tasks with their own session
   scope; HTTP/git-push handlers return a run id immediately. No awaiting
   containers inside request handlers. selectinload every relationship touched
   in async services; typed publish API on the WS manager.
R6 REAL SEAMS IN TESTS. At least one integration test per phase uses named
   volumes (not tmp_path bind mounts). Workspace/mount addressing is an
   explicit enum (volume | bind) — never inferred from path shape; unit test
   feeds a Windows path (C:\...) to prove no misclassification. E2e
   real-backend tier runs workers=1 or per-worker namespaces; the test-reset
   endpoint resets in-memory singletons too. Tests covering broadcast paths
   use the real WS manager with a capturing transport — never an AsyncMock.
R7 SELF-HOSTING RATCHET. The dogfood pipeline definition lives in-repo and
   syncs on push (see Phase 0a); it is upgraded to consume each phase as it
   lands and is the standing acceptance test for the whole arc.
R8 UI SHIPS WITH ITS SPEC. Every phase that ships or rebuilds a UI surface
   ships a Playwright spec for it in the same phase, named in the deliverables.
   "UI shows it live" in an exit gate always means a named spec, never vibes.
   Pure store logic gets vitest coverage (first frontend unit layer).

### Sequencing

Track A = execution arc (risky, strictly ordered).
Track B = spec/eval layer (additive; interleaves, sync points labeled).
Migration policy: alembic migrations land serialized on main only; Track B
rebases before generating. Startup runs `alembic upgrade head`; existing
unversioned dev DBs are stamped at baseline first.

#### Phase 0 — Self-hosting bootstrap + salvage quick wins  [prologue]  ✅ COMPLETE

0a DOGFOOD CI LIVE (US-1 minimal form, on the legacy path):
   - Pipeline-definition sync on push: the receive-pack handler re-reads
     `.lazyaf/pipelines/` from the pushed commit and creates/refreshes the
     materialized platform pipeline (including a new `triggers:` binding in the
     yaml schema) BEFORE trigger matching. Without this, repo-defined pipelines
     are invisible to push triggers and CI changes take effect one push late —
     unacceptable for self-hosted CI.
   - Tiered dogfood suite (one step per tier, not one pytest invocation):
     T1 unit + non-Docker integration (runs in any runner);
     T2 Docker-dependent integration (runs on a dedicated CI runner service
        with the Docker socket mounted — deliberate interim DooD, retired when
        step containers get a socket option in 12.4);
     T3 e2e quick tier with compose-network URLs (not localhost).
   - Gate step: parse junitxml/-rs output; fail on out-of-baseline skips or
     executed-count below committed per-tier floors (R4 mechanism).
   - Wire trigger_dedup into trigger_service (two rapid pushes = one run).
0b SALVAGE QUICK WINS (audit order): sprawl.md (done); data-testid sweep +
   vite proxy env var (hand-apply equivalents in the graph editor); 12.6
   contract suite + RunnerStateMachine ported dormant (baseline'd per R4) with
   IDLE->DEAD and DEAD->DISCONNECTED added; alembic scaffold + clean
   regenerated baseline + upgrade-at-startup + stamp-if-existing, replacing
   database.py's PRAGMA/except-pass hacks. (Before 12.2-INT adds tables.)
0c TEST-HARNESS HARDENING: env-gated test-mode API rebuilt (reset/seed + an
   in-memory singleton reset hook); Playwright harness fixed (workers=1 real
   tier or per-worker namespacing); skip-baseline gate implemented (R4);
   minimal vitest harness for stores; dogfood-live Playwright spec (push ->
   run appears -> status transitions + log lines stream over WS) per R8.
0d HYGIENE: delete stale .pyc ghosts (images/, backend/alembic/versions/);
   retire graph-creep.txt to historical-documents/; delete dead
   PipelinesPanel.svelte; PLAN.md status corrected (done — this document).

   EXIT GATE: a push to the internal remote runs all three tiers through
   LazyAF itself, green, with per-tier executed counts >= committed floors;
   two rapid pushes produce one run; dogfood-live spec passes.

#### Phase 12.2-INT — Workspace persistence + wiring the dark libraries [A]  ✅ COMPLETE

The step both prior attempts died before/at. Deliverables:
   - Alembic-born Workspace model (ADAPT failure_01 schema onto main's
     WorkspaceStatus vocabulary; full run id in volume names).
   - WorkspaceService rebuilt on main's tested locking + state machines
     (failure_01's service is the shape, not the code). Lifecycle includes
     cleanup on completion AND failure, plus a periodic orphan audit task.
   - NETWORK PLUMBING (pulled forward from 12.3 — the clone needs it): named
     network declared in compose; settings-driven network name + backend/git
     URL; LocalExecutor gains a network kwarg.
   - WORKSPACE POPULATION (net-new; no attempt ever built it): helper/init
     container on the named network clones from the internal git server into
     the named volume at /workspace/repo (matching LocalExecutor's
     working_dir, which becomes config-driven).
   - LocalExecutor hardening: shell-wrap script commands (['bash','-c',...] —
     docker-py shlex-splits raw strings); run the ported migration-compat
     matrix (old YAML, missing image, multiline commands, all step types)
     against it; addressing enum per R6.
   - pipeline_executor rewiring per R5: asyncio task per run driving main's
     pipeline_state_machine; ExecutionRouter -> LocalExecutor for script/
     docker steps, default-ON (R1); legacy retained for agent steps (R2).
   - OBSERVABILITY (R1): StepRun.executor field; LocalExecutor status/log
     events persisted incrementally to StepRun + typed WS broadcast; the
     container-logs->StepRun-row round-trip test lands HERE (12.3 re-runs it
     over the control-layer POST path); spy test: locally-routed steps never
     touch job_queue.
   - Until 12.3's base image exists, test-suite.yaml pins a stock image with
     bash/curl/git (python:3.12 full, not slim).

   EXIT GATE: dogfood script steps run through LocalExecutor by default and a
   dogfood step asserts executor='local' for them via the API; named-volume
   clone test passes through the network path; volume create/cleanup balanced
   across success, failure, and backend-restart orphan-sweep scenarios; tier
   floors hold.

#### Phase 12.2.5 — Specification data model  [B]  ✅ COMPLETE

Feature / UserStory / AcceptanceCriterion / PromptTemplate + card links +
promote-to-feature; CRUD routers + minimal UI + MCP spec tools; serialized
alembic migration. The required-criterion-blocks-done rule ships stubbed
(criterion with no TestRuns = not blocking; xfail(strict) marks the real
check) and activates in 12.2.6. Seed with THIS ROADMAP's three user stories.
   EXIT GATE: spec CRUD API tests + spec-UI Playwright spec (R8); the three
   north-star stories queryable via API and MCP.

#### Phase 12.3 — Real step images + control layer  [A]  ✅ COMPLETE

Port failure_01's images/ tree (base + control runtime + backend client +
claude + test-runner) with the audit's contract fixes: LogLine payload
wrapping; token->auth_token / working_dir->working_directory renames; enforce
timeout_seconds; quote the pytest pin; chown-at-entrypoint for volume
ownership; settings-driven backend URL. Build story: compose build targets +
script — no phantom :latest. HOME=/workspace/home persistence contract + the
cross-step tool-persistence test (agent installs tool, script step uses it).
Retire BOTH half-baked variants on main (control_layer/image.py generators,
backend/docker/ copies). Gemini image deferred to 12.5 (failure_01's is
fiction). Dogfood ratchet: test-suite.yaml moves to lazyaf-base (the
install-uv step dissolves into the image).
   EXIT GATE: dogfood steps run in lazyaf-base with control-layer status/
   logs/heartbeat feeding the UI (round-trip test over the POST path); tier
   floors hold.

#### Phase 12.2.6 — Test result tie-back  [B]  ✅ COMPLETE

Manifest channel `/workspace/.control/test_results.json` picked up at step
end -> POST /api/test-results/ingest -> TestRef/TestRun joined to criterion +
commit + model + prompt. Full PLAN 12.2.6 scope: reconcile endpoint + `lazyaf
tests reconcile` CLI + background reconciliation on successful pipelines +
orphan-TestRef lifecycle + GET /api/criteria/{id}/history. pytest plugin in
runner-common (`lazyaf_test_id` marker). Sparkline/history UI deferred to
12.6.5 (stated, not dropped). Activates 12.2.5's blocks-done rule.
   EXIT GATE: LazyAF's own suite annotates a starter set of tests against the
   US-1/2/3 criteria; a dogfood run produces TestRuns joined to criteria; the
   history endpoint returns the series.

#### Phase 12.4 — Script/docker steps fully ephemeral  [A]  ✅ COMPLETE

All script/docker steps through LocalExecutor + step images by default; step
config gains a socket/volume option so the T2 Docker tier runs in ephemeral
containers (retiring the 0a interim DooD runner); THEN remove script/docker
execution from runner entrypoints (own commit, R2).
   EXIT GATE: migration-compat matrix green against the ephemeral path;
   dogfood asserts executor='local' for every script/docker step; removal
   commit contains the entrypoint deletions and nothing else; tier floors.

#### Phase 12.5 — Agent steps via control layer  [A]  ✅ COMPLETE

agent_wrapper rebuilt as a thin shim over runner-common baked into agent
images (failure_01's monolith is reference); config-file contract from the
salvaged agent-step contract test re-targeted at main's protocol; Claude
image, then Gemini + mock images derived from runner-common executors.
Playground migrates off job_queue HERE (resolved from OPEN — it is agent
execution). Polling runners remain only as unused fallback (deletion at 12.6).

**ADDED 2026-08-29 — effort telemetry (do NOT defer past this phase).** Agent
steps moving into control-mode containers is the moment to add the protocol's
fourth channel: `POST /api/steps/{id}/usage` -> `StepUsage` (tokens, CLI-reported
cost, wall-clock, container-seconds; `cost_source` distinguishes CLI-reported
from gpu-node-derived). The control runtime scrapes the agent CLI's own usage
report at step end and ships it like it ships test results. Rationale: 12.2.6
became a retrofit because 12.3 froze the protocol without it — the same mistake
is available here, and Milestone 13's entire cost axis depends on this channel.
   EXIT GATE ADDITION: a mock-agent dogfood step produces a StepUsage row with
   non-null tokens and a cost_source; a step whose CLI reports nothing still
   succeeds with cost_source="unknown" (telemetry never fails a step).
   EXIT GATE: US-2 e2e (mock agent: card -> agent -> gate -> review -> merge)
   green on ephemeral containers AND added to the dogfood suite; the dogfood
   pipeline DOES include a mock-agent step from now on (zero-cost, every
   push); playground works with job_queue idle.

#### Phase 12.6 — RemoteExecutor + runner agents (loopback first)  [A]  ✅ COMPLETE

The ported 137-test contract suite is the spec. RunnerStateMachine (ported in
0b) wired to a DB-backed runner registry with labels + matches_requirements
(re-authored migration); runner_protocol ADAPTed (+auth via step-token
pattern, protocol version, cancel, real execute_step.config schema);
RemoteExecutor rebuilt on the salvaged skeleton (connection registry,
ACK-future, death monitor) with persistence, heartbeat-death reassignment,
double-assign guard, and a real dispatcher for requeued steps; /ws/runner
endpoint with per-message session scope + auth; runner-agent package rebuilt
embedding runner-common/LocalExecutor behind a pluggable executor seam.
NativeOrchestrator explicitly DEFERRED to the manual remote-hardware lane —
the seam and protocol stay Docker-agnostic so runpod-style socketless pods
and OpenAI-compatible agents slot in without protocol changes. Runner panel
rebuilt (snapshot fetch + WS deltas) with a Playwright spec asserting
snapshot-then-delta after reload (R8). Loopback lane: a runner-agent process
on the same host over WS is the tested path; real remote is manual.
THEN, in its own commit, after the contract suite passes end-to-end against
the push path: delete the polling stack (runner_pool, polling entrypoints,
job pull endpoints, job_queue) with frontend + all consumers migrated in the
same commit, and land test_no_legacy_code assertions in that commit (R2).
   EXIT GATE: dogfood suite (incl. the US-2 mock-agent step) runs through a
   loopback runner agent; 137-test contract suite at zero skips (R4 ratchet
   complete); polling stack deleted; runner-panel spec green; tier floors.

#### Phase 12.6.5 — Experiments & leaderboards  [B, needs 12.6 + 12.2.6 + 12.2.5]

Matrix runs (model x prompt x repeat) fanned out through the executor layer;
aggregated pass-rate per AcceptanceCriterion AND cost-to-solve (from StepUsage —
a cheap model needing six iterations vs an expensive one landing first try is
THE comparison, and it is meaningless without the effort axis); leaderboard +
experiment UI
(with the criterion-history sparklines deferred from 12.2.6); MCP
launch_experiment tool; guardrails: dry-run cost/run-count estimate + per-
experiment cap before launch (upgraded from confirm-only per PLAN's open
question — owner veto welcome). Completes US-3.
   EXIT GATE: tdd/e2e/test_experiment_matrix.py green (2 models x 2 prompts
   on mock-model runners, asserts per-variant aggregation); leaderboard +
   experiment-launch Playwright spec (R8).

#### Phase 12.6.6 — Spec-curated agent context  [B, needs 12.2.5 + 12.2.6 + 12.5; measured via 12.6.5]

Per-card context bundle from linked feature/story/criteria INCLUDING related
TestRef file paths ("these tests already cover related criteria — read, don't
duplicate"), token-budget truncation, {spec_context} PromptTemplate
placeholder -> /workspace/.control/spec_context.md via the control layer;
agent wrapper injects it. Measure the effect with a 12.6.5 experiment.
   EXIT GATE: bundle-content tests (incl. TestRef paths + truncation);
   wrapper-injection test; one experiment comparing with/without curation.

#### Phase 12.7 — Debug re-run mode  [A, after 12.5]

Rebuild from spec against the 12.2-INT executor. failure_01 contributes
shelf-ready leaves (debug state machine + tests, schemas, sidecar image) and
UX reference (DebugRerunModal, breakpoint checkboxes, join command). New
work: session-service lifecycle (create actually starts the run; resume
does not end multi-breakpoint sessions), terminal I/O bridge, breakpoint =
pre-step gate in the executor. Sidecar-vs-shell split per the audit.
   EXIT GATE: e2e — failed dogfood-style run re-run with a breakpoint,
   terminal attach, resume to completion; debug UI Playwright spec (R8).

#### Phase 12.8 — Cleanup & polish  [epilogue]

v1 array pipeline format decision (recommend: execution goes graph-only, v1
auto-converts at the API/YAML boundary via array_to_graph; OWNER CONFIRMS
before removal); dogfood pipeline converted to v2 graph; runner-common
adopted everywhere (12.0 finally true); audit PLAN 12.8's regression matrix
against the dogfood suite and backfill gaps (the suite absorbs its intent —
stated, not silent); dead-code sweep; docs; retire completed phase sections
to historical-documents/.

### Decision log (attempt #3)

- 2026-08-29 Scope: all of 12.x; K8s stays future. (Owner)
- 2026-08-29 No external CI ever; self-host ASAP. (Owner)
- 2026-08-29 12.6 loopback-first; remote hardware manual. (Owner)
- 2026-08-29 12.2.6 re-sequenced AFTER 12.3 (protocol froze in January;
  retrofit is deliberate). (Claude, from audit)
- 2026-08-29 Workspace population: helper-container clone over a named
  compose network into /workspace/repo. (Claude — veto welcome)
- 2026-08-29 Interim DooD: dedicated CI runner with Docker socket for the T2
  tier until 12.4's step-container socket option. (Claude — veto welcome)
- 2026-08-29 Playground migrates off job_queue in 12.5. (Claude)
- 2026-08-29 NativeOrchestrator deferred; runner-agent executor seam stays
  pluggable/Docker-agnostic for runpod-style + OpenAI-compatible agents.
  (Claude, honoring owner's hybrid-model ambition)
- 2026-08-29 12.6.5 guardrails: dry-run estimate + per-experiment cap, not
  confirm-only. (Claude — veto welcome)

Decisions made DURING implementation (all shipped and gate-verified):
- 12.3 reporting path: control-mode steps report through the in-container
  runtime to /api/steps/*, which is the SOLE StepRun.logs writer; stock
  images keep the stdout-stream path. Mode is explicit (image label value
  `1` + LAZYAF_CONTROL), never inferred, and a control step whose runtime
  never reported CANNOT pass (StepExecution stuck PREPARING = loud failure).
- 12.3 config delivery: per-step `/workspace/.control/<step_execution_id>.json`
  via put_archive onto a created-but-unstarted container, consumed-once.
  (Per-step, not per-run: the run-scoped path collided under graph fan-out.)
- Step capabilities: `needs: [docker]` sugar translates to the socket mount
  behind an allowlist — one translation site for 12.4 to change. Interim
  runner-service DooD anchor retired when the tiers moved to step containers.
- Images: `lazyaf-{base,claude,test-runner}:dev`, content-hash labels, built
  by `scripts/build_images.py`; T2 preflights `--check` so stale images fail
  loudly instead of silently testing yesterday's runtime. Backend never
  auto-builds; `step_default_image` deliberately stays a pullable image.
- Environment invariants learned from dogfood runs #5-#11 (each now regression
  -tested): population chowns the repo to the step uid; the entrypoint joins
  the socket group before gosu; the tree hash sorts POSIX-normalized paths;
  Docker client timeout must exceed the longest container wait budget.

- 2026-08-29 Benchmark harness scoped as Milestone 13 (corpus + trials +
  effectiveness board), with two mandatory hooks inside 12.x: StepUsage
  telemetry in 12.5 and the cost-to-solve axis in 12.6.5. (Owner ambition,
  Claude scoping)
- 2026-08-29 Benchmark oracle is layered (fail_to_pass/pass_to_pass tests +
  optional linked UserStory criteria); corpus = ingested repos pinned per case;
  loop = sequential pipeline runs driven by a Trial orchestrator (the graph is a
  DAG — no cycles); cost = CLI-reported + GPU-node model. (Owner)

- 2026-08-29 Milestone 13 targets a PUBLIC write-up + reproducible bundle:
  provenance per trial (image hashes, harness/model version, policy), variance
  over N repeats reported natively, controls (base-state + null-agent), public
  fixtures with contamination noted, and headline metrics = cost-to-solve,
  regression rate, iterations-to-solve. (Owner — "I am a scientist at heart")

- 2026-08-29 Benchmark trials vary ARBITRARY STRATEGY GRAPHS (one-shots,
  adversarial fan-outs, planner->K-workers->integrator, gated combinations), not
  a fixed set of loops; models are bound to graph ROLES per trial, so a single
  strategy may mix a high-end planner with cheap parallel workers. Cost is
  attributed per role; wall-clock and integration-conflict rate join the board
  because parallelism trades money for latency. (Owner)
- 2026-08-29 No v0 case study from existing session data — wait for real
  controlled trials with a one-shot baseline. (Owner)

- 2026-08-29 Parallel strategies are GIT-NATIVE: branch + workspace per worker
  off the case base commit, rejoined through the platform's existing
  merge/rebase/resolve-conflicts machinery. Workers never share a checkout, so
  parallelism is not a hazard to engineer around - it is the substrate LazyAF
  already is. Integration POLICY (sequential-merge / rebase / cherry-pick /
  agent-composed, and on-conflict: fail | resolver-agent | human) is part of the
  strategy under test, and integration conflict/resolution cost are measured
  outcomes. Structured conflicts make conflict resolution itself an
  agent-addressable step - a strategy variant a single-sandbox harness cannot
  express. (Owner: "lazyaf is the bridge")

- 2026-08-30 RESOLVED: retire the v1 array pipeline format. (Owner: "retire the
  old pipeline format") The shape is the one 12.8 recommended and the owner
  confirmed: **execution is graph-only**. Concretely:
  - `pipeline_executor` loses its array branch entirely - `is_graph` and every
    two-way fork behind it disappear, leaving ONE path through the executor.
    This is the whole point: the array path is a second execution semantics
    nobody reads, and every graph fix since 12.4 had to be written twice.
  - The array survives ONLY as an authoring convenience at two edges - repo
    YAML (`.lazyaf/pipelines/*.yaml`) and the pipeline API - both converting
    via `array_to_graph` at the boundary. A human writing a five-step pipeline
    should not have to hand-author nodes, edges and positions; a human is not
    the executor.
  - Everything that currently persists `steps=json.dumps([...])` writes a graph
    instead: `trigger_service.upsert_materialized_pipeline`,
    `agent_run` (both card-work sites), `experiment_service`, the test-mode
    seed.
  - `Pipeline.steps` is backfilled into `steps_graph` by a migration and then
    DROPPED (R3: one source of truth per wire contract; R2: deleted after the
    backfill is accepted). `0007_drop_polling_runner_columns.py` is the
    precedent for the SQLite table rebuild.
  - The dogfood pipeline converts to a v2 graph, which is what proves it.
- OPEN: whether `12.0` counts as done at 12.5 (runner-common adopted by agent
  images) or 12.8 (all runners retired) — resolves itself as those land.

---

## Milestone 14 — Self-Hosted & OpenAI-Compatible Model Endpoints

> **The ask (owner, 2026-08-30):** run agents against models we host ourselves —
> ollama and vLLM on bare metal at home, or on runpod.io — so "all sorts of AI
> models" can be in the mix. Push-style: LazyAF reaches out when it wants work
> done, never a long-poll loop.
>
> **Sequencing:** independently valuable for the product (cheap local models
> doing routine work), AND a hard prerequisite for Milestone 13's central
> hypothesis — "a high-end model writes instructions, K cheap models execute in
> parallel" is unmeasurable without cheap models. It can be built in parallel
> with 13.1/13.2; 13.3's headline experiment depends on it.

### The thing that is actually hard

The transport is easy and already push: LazyAF is the HTTP client, so calling
`/v1/chat/completions` involves no polling by anyone. **The hard part is that
ollama and vLLM are inference servers, not agents.** Claude Code and the Gemini
CLI ship their own agent loop — read files, edit, run commands, iterate until
done. A raw OpenAI-compatible endpoint gives you completions and nothing else.
So LazyAF has to supply the loop.

### Decisions (owner, 2026-08-30)

- **We own the loop.** A minimal tool-calling harness in `runner-common`
  (read/write/list files, run shell, apply patches, stop conditions) running
  inside the existing control-mode container, as a new entry in the `EXECUTORS`
  registry beside claude/gemini/mock. Wrapping an existing OSS agent CLI was
  rejected for a specific reason: Milestone 13 makes *loop shape* the
  independent variable, and a loop we do not own is one we cannot vary.
- **All three reachability modes**, because the deployments genuinely differ:
  | Mode | For | How |
  |---|---|---|
  | `runner-local` | Home bare metal behind NAT | A 12.6 runner agent runs on the box hosting ollama; the backend pushes the step there over WS and the step container calls `localhost`. Zero inbound connectivity, no tunnel. |
  | `direct` (default) | runpod, any routable endpoint | The step container calls the endpoint URL itself. |
  | `proxy` | Central auth/logging | The backend brokers the call. Convenient, but it puts inference traffic through the backend and makes it a bottleneck — opt in per endpoint, never the default. |
- **Capability is probed and recorded, not assumed.** Tool-calling support is
  inconsistent across self-hosted models (ollama supports it for some; vLLM
  depends on model plus chat template). Probe at registration, store the result
  on the endpoint, and let the strategy decide — which turns "does tool support
  matter?" from an assumption into something Milestone 13 can measure.
- **Cost: tokens always, dollars when known.** Token counts come from the
  OpenAI-compatible `usage` field. An endpoint may carry a $/hour rate (a
  runpod pod rate, or an estimate for home hardware); when set, cost is
  rate x wall-clock occupancy recorded as `cost_source="gpu-node"`. When unset,
  tokens are recorded and cost stays null, and the board must show WHICH trials
  have real cost data rather than silently mixing them.

### What already anticipates this

`StepUsage.provider` already includes `openai-compatible` and `self-hosted`;
`cost_source` already includes `gpu-node`; and 12.6 deliberately kept the
runner-agent's executor seam Docker-agnostic and pluggable for exactly this.
The work is a new executor plus an endpoint registry, not a re-architecture.

### Phases

- **14.1 — Endpoint registry.** A `ModelEndpoint` entity (name, base_url, model,
  auth style + secret reference, reach mode, optional $/hour, probed
  capabilities), CRUD, a health/capability probe, and secret handling that
  reuses the 12.5 `secret_environment` path so a key never reaches
  `docker inspect`. Endpoints with no auth (typical LAN ollama) are first-class.
- **14.2 — The agent harness.** The tool-calling loop in `runner-common`, with a
  no-tools fallback for models that cannot tool-call, bounded iterations and
  budget, and usage reporting through the existing sidecar so self-hosted runs
  land in `StepUsage` like every other step.
- **14.3 — UI category.** A Model Endpoints surface (register, probe, health,
  capabilities, rate) and endpoint selection wherever an agent is chosen: agent
  step config, card creation, playground, and the 12.6.5 experiment matrix — the
  last one is what lets a matrix mix API and self-hosted models in one run.
- **14.4 — Prove it.** A dogfood step running against a real self-hosted
  endpoint, and the Milestone 13 fan-out hypothesis finally runnable:
  expensive planner, K cheap local workers, measured.

### Phase 14.5 — Runner images with inference baked in

> **The ask (owner, 2026-08-30):** "images available as runners with vllm and
> llama baked in so that you mount your data / cache of models and just go."
> Chosen shape: the COMBINED runner+inference image (not a local compose
> profile). Servers: **vLLM** and **ollama**.

One image = one deployable node. `FROM` the upstream inference server, with the
12.6 runner agent layered on top. You give a pod the model cache, a LazyAF
server URL and a runner token; it starts the inference server, dials OUT over
WebSocket, and its steps call the model on `localhost`. **Zero inbound
connectivity** - which is what makes it work on runpod and behind home NAT
alike, and why this is a runner image rather than a model image.

- `lazyaf-runner-ollama` FROM `ollama/ollama` - mount `~/.ollama`; ollama pulls
  models itself, so "mount your data and go" is literally true.
- `lazyaf-runner-vllm` FROM `vllm/vllm-openai` - mount the HF cache; the
  throughput option and the right one on a rented GPU.

**We do NOT rebuild the inference servers.** Both upstreams publish official
images; forking them means multi-GB pushes on every release plus tracking
CUDA/torch/vLLM compatibility forever. We add a thin layer - the runner agent,
an entrypoint that supervises two processes, and the endpoint declaration - and
inherit their release engineering.

**Windows desktops with idle RTX cards (owner, 2026-08-30).** The target is a
Windows box running Docker Desktop on the WSL2 backend, using the SAME image as
runpod with `--gpus all` (NVIDIA's CUDA-on-WSL driver makes this work), so there
is nothing Windows-specific to maintain. Two decisions attach to it:

- **Dual role, selected per endpoint.** The box can serve models AND execute
  steps; the difference is advertised as labels, not as two deployment shapes.
- **Yield on GPU busy.** LazyAF must not fight the owner for his own GPU. This
  is genuinely new mechanism: the node samples GPU utilization and DRAINS -
  stops accepting new assignments while letting an in-flight step finish - then
  resumes, with hysteresis so a transient spike cannot flap it. The naive
  version (disconnect when busy) is wrong precisely because 14.5 establishes
  that the connection IS the advertisement, so disconnecting would orphan
  running work. Expect a small backend availability flag; silently dropping the
  connection is not acceptable. The UI must show WHY a node is not taking work,
  and a manual force-drain / force-available override is required.

Design points the wiring doc must settle:
- **Two processes, one container.** The entrypoint starts the inference server,
  waits for it to be healthy, then starts the runner agent, and propagates
  signals and exit codes so a dead server does not leave a live runner
  advertising a model that is gone.
- **How the pod's endpoint gets registered.** 14.1 already has `runner-local`
  reach mode plus `requires: {has: ["endpoint:<name>"]}` label matching, so the
  minimum is: the operator registers the endpoint once and the pod advertises
  the matching label from env. Whether the pod may ALSO self-register through
  the API is a real decision - it is the difference between "just go" and a
  node being able to write rows in your control plane.
- **LazyAF builds the heavy images itself** (owner, 2026-08-30). A repo-defined
  pipeline with docker steps builds and pushes the two runner images on a LazyAF
  runner agent, not on GitHub compute. This is dogfooding at the top of the
  stack - the platform ships its own artifacts - and it also sidesteps a real
  problem: a ~45GB vLLM build does not fit a standard GitHub runner, and a
  self-hosted Actions runner on a PUBLIC repo would expose the owner's hardware
  to fork PRs. GitHub keeps the wheel, the small service images, the existing
  step images, secret-scan and release-please; `pr-build.yml` stays
  GitHub-hosted for exactly that reason. GHCR credentials reach the build step
  through 12.5 `secret_environment`, so they never appear in `docker inspect`.
- **Image size is a release problem, not a detail.** A CUDA vLLM image is ~10GB.
  These must not ride the normal per-tag image matrix; they need their own
  trigger, their own cadence, and a documented "build it yourself" path.
- **GPU passthrough** differs between runpod (automatic) and local
  `docker run --gpus`; the docs must not assume either.

### Open questions

- Which model families are worth pinning as known-good in the docs, and do we
  ship a capability matrix or let the probe speak for itself?
- Context-window limits vary wildly on local models; does the harness truncate,
  summarize, or refuse when a repo context exceeds them?
- Concurrency: one local GPU serving K parallel fan-out workers will queue.
  Does the endpoint carry a max-concurrency the scheduler respects?

---

## Milestone 13 — Benchmark & Evaluation Harness

> **The question this answers:** take a repo at a known state, set the AI loop
> loose, and measure what it actually cost to get to a solution — across models,
> prompts, loop policies, and problem verticals. Not "can an agent do this once"
> but "which loop is *effective*, and at what price".
>
> Scoped 2026-08-29 from the owner's benchmark ambition. Milestone 12 builds the
> execution platform; Milestone 13 turns it into an instrument. Two hooks
> (StepUsage in 12.5, the cost axis in 12.6.5) MUST land inside 12.x or this
> milestone starts with a retrofit.

### What already exists to build on

| Need | Already in place |
|------|------------------|
| Deterministic starting state | `populate_workspace(..., commit_sha)` — workspaces already clone at a pinned commit (12.2-INT) |
| Isolated, reproducible execution | Ephemeral control-mode containers on per-run volumes, byte-reproducible images (12.3) |
| Outcome capture per test | TestRef / TestRun tie-back keyed to criterion + commit (12.2.6) |
| Matrix fan-out | `Experiment.matrix = {models, prompts, repeat}` + executor fan-out (12.6.5) |
| Effort capture | `StepUsage` (12.5 — the new hook) |
| Human-meaningful intent | Feature / UserStory / AcceptanceCriterion (12.2.5) |

### Design decisions (owner, 2026-08-29)

- **Success oracle: BOTH, layered.** Each case ships `fail_to_pass` /
  `pass_to_pass` test ids for objective, cheap scoring (SWE-bench-shaped), AND
  may link a UserStory so criteria give partial credit and human-readable
  grouping. The oracle decides *solved*; the criteria explain *what was solved*.
- **Corpus: ingested repos pinned per case.** Fixture repos live in the internal
  git server; each case pins a `base_commit_sha`. Hermetic, offline-capable,
  versioned with the platform, and reuses ingest + population unchanged.
- **The independent variable is the STRATEGY, not the model** (owner,
  2026-08-29). The thesis under test is "these ways of building software with AI
  work better", so trials vary the *loop shape* and treat models as a resource a
  strategy allocates. Models remain a covariate: repeat the comparison across
  model mixes to show an effect is not one model's artifact.
- **A strategy is an arbitrary graph of activity, expressed as data.** Not an
  enum of blessed loops — a `StrategyTemplate` is a pipeline graph (steps, edges,
  fan-out/join, conditions) + a role->model assignment + a loop policy. The
  platform already executes exactly this shape, so adding a strategy is authoring
  data, not code. The catalog is open-ended; representative members:
  - **one-shot** — a single agent step, the naive baseline every claim is measured against
  - **test-first** — write oracle-shaped tests, then implement against them
  - **adversarial** — implement -> fan out N independent reviewers -> join -> fix
  - **planner/executor fan-out** — a high-end model writes instructions, K small
    models execute them in parallel, an integrator merges (the owner's hypothesis:
    buy latency and cost efficiency by spending a little intelligence up front)
  - **gated** — any of the above, plus a real CI gate that must pass before done
  - and combinations, since these compose as graphs rather than as modes
- **Loop driver: the LazyAF pipeline loop.** Each iteration is a real pipeline
  run of the strategy graph — visible, costed, diffable. *Implementation note:*
  the v2 graph is a DAG (`entry_points` + all-upstream-satisfied traversal), so
  iteration is NOT a cycle in the graph: a **Trial orchestrator drives N
  sequential pipeline runs**, one per iteration, feeding the previous iteration's
  failures forward. Fan-out *within* an iteration is native graph behavior and
  needs no new engine work.
- **Cost sources: CLI-reported + GPU-node model.** See `StepUsage`.
- **Audience: a public write-up backed by a reproducible bundle** (owner,
  2026-08-29). Anyone should be able to download the corpus + results and re-run
  them. This makes provenance (image hashes, harness version, model version,
  policy) a hard requirement rather than nice-to-have, and it puts fixture
  LICENSING in scope — the bundle ships git bundles for cases whose license
  permits redistribution and fetch-instructions + a patch for the rest.
- **Contamination: public repos, noted** (owner). Fixtures come from real public
  repos for realism; every case carries `contamination_risk` and the caveat is
  disclosed in the write-up and the bundle. Deliberate trade: realism now,
  honesty about the threat to validity, and the door stays open to adding
  low-risk cases later (the field already distinguishes them).
- **Variance is a platform feature, not an analysis afterthought** (owner). Every
  reported figure is over N repeats with its distribution; the board flags
  comparisons whose intervals overlap instead of ranking noise. `repeat` is
  already an axis of `Experiment.matrix` — the work is aggregation + honest
  presentation, not new execution machinery.
- **Headline metrics** (owner): (1) **cost-to-solve** — median $ per solved case,
  the one number that answers "what did the solution actually cost"; (2)
  **regression rate** — how often a loop breaks `pass_to_pass` while fixing the
  target, i.e. whether you could trust it in real CI; (3) **iterations-to-solve
  distribution** — whether a loop converges or thrashes, and whether late
  iterations ever earn their keep. Solve-rate-at-fixed-budget stays available as
  a normalization but is not the headline.
- **Added for graph strategies:** because strategies differ in *shape*, the board
  also reports **wall-clock-to-solve** and **speedup** (serial-equivalent /
  wall-clock) — a fan-out that costs 3x but finishes in a third of the time is a
  different trade, not a worse result — plus **integration conflict rate** for
  parallel strategies and **cost-by-role**, which is what actually tests the
  "expensive planner, cheap workers" hypothesis.
- **Fairness across differently-shaped strategies** comes from holding the case,
  base commit, oracle, and BUDGET constant: given the same dollars and the same
  wall-clock ceiling, which strategy solves more? That is why fixed-budget
  solve-rate stays in the board even though cost-to-solve is the headline —
  without a shared cap, an unbounded strategy can always "win" by spending more.

### Controls (a benchmark without them proves nothing)

- **Base-state control**: at `base_commit_sha` every `fail_to_pass` test MUST
  fail and every `pass_to_pass` MUST pass — a case whose oracle is already green
  is broken, and `lazyaf bench validate` refuses it (Phase 13.1).
- **Null-agent control**: a trial variant that changes nothing must score 0%
  solved. If it ever scores above zero, the oracle is measuring something other
  than the fix.
- **Determinism disclosure**: whatever the provider exposes (temperature, seed)
  is recorded per trial, so "we could not pin this" is stated rather than hidden.

### Detailed specifications

The implementable detail lives in `docs/milestone-13/` (PLAN.md keeps the shape
and the decisions; the companion docs keep the 3,000 lines an implementer needs,
the same split `historical-documents/` uses for completed phases):

| Document | What it pins |
|---|---|
| `docs/milestone-13/strategy-catalog.md` | The StrategyTemplate graph contract (reserved `lazyaf_*` keys, the six-pass `expand_strategy_graph`, strict two-way role-binding, K-parameterization) and a 7-entry catalog with REAL v2 graph JSON: one-shot, test-first, adversarial-review, planner-fanout (expanded at K=4), planner-fanout-resolver, one-shot-gated, composed-full. Plus the integration-policy x on-conflict matrix, including the cells that are rejected as incoherent. |
| `docs/milestone-13/api-surface.md` | Every endpoint (`/api/bench/*`), the full `POST /api/steps/{id}/usage` contract for Phase 12.5, how the benchmark layer joins the spec layer without double-counting criterion history in the dogfood corpus, MCP tools, `lazyaf bench` CLI, and the indexes the read-heavy board queries need. |
| `docs/milestone-13/phase-specs-and-metrics.md` | Phase deliverables 13.1-13.5 with contract test files and Definition-of-Done checklists; the metrics defined mathematically; the variance/separability rules; and the `METHOD.md` template that ships in every exported bundle. |

**The one thing from those docs that belongs in the plan itself** — because it
governs how every number is allowed to be stated:

> **Cost-to-solve is censored data.** A trial stopped by the budget cap is not a
> missing observation and not infinite cost; it is the statement *"more than what
> was spent"*. So the board never prints a lone median. It prints three numbers
> together: the **paired median** over the case set every compared variant solved
> (difficulty held constant; `INSUFFICIENT` under 5 shared cases), the
> **amortized cost per solve** over ALL trials including failures (what it
> actually costs a user to get one working change), and the **Kaplan-Meier
> censored p50** — which, when survival never reaches 0.5, renders
> `> $X.XX (only N% solved)` rather than inventing a median. If the paired and
> amortized numbers rank differently, the board says so in words instead of
> picking a winner: that variant is cheap when it works and expensive when it
> does not, and that is the finding.

### Phase 13.1 — Corpus & fixtures
`BenchmarkSuite` / `BenchmarkCase` models + CRUD + a `lazyaf bench` CLI to author
cases from a real repo state. A case-validation command that proves a case is
well-formed: at `base_commit_sha` every `fail_to_pass` test FAILS and every
`pass_to_pass` test PASSES (a case whose oracle is already green is broken).
Seed a starter suite spanning verticals x complexity.
   EXIT GATE: `lazyaf bench validate <suite>` green on the starter suite;
   validation catches a deliberately-miswired case.

### Phase 13.2 — Trial orchestrator & loop policy
`StrategyTemplate` + `Trial` / `TrialIteration` + the orchestrator: bind roles to
models, reset to `base_commit_sha` on a fresh branch, run iteration pipelines of
the strategy graph until solved / max_iterations / budget_usd exhausted,
recording per-iteration cost (by role), diff churn, and oracle progress. Budget
enforcement is hard (a trial cannot outspend its cap) and applies across the
whole fan-out, not per worker. Parallel templates allocate **a branch and a workspace per
worker** off the case's base commit and rejoin through the platform's existing
merge/rebase/resolve-conflicts machinery; the template's `integration.policy`
decides how, and `on_conflict` may hand the structured conflict to a resolver
agent. Conflicts, resolutions and integration cost are recorded as trial
outcomes rather than silently dropped.
   EXIT GATE: a mock-model trial on a starter case solves at a known iteration
   with a complete per-iteration cost curve; a deliberately-unsolvable case
   terminates at budget_exhausted without overspending.

### Phase 13.3 — Strategy experiments & the effectiveness board
Extend `Experiment` so a matrix axis is **strategy_template x model_assignment x
repeat** over a suite of cases. Aggregate per strategy, per vertical, per
complexity: solve-rate at a shared budget, median cost-to-solve, cost-by-role,
wall-clock and speedup, regression rate, integration conflict rate, and the
iterations-to-solve distribution. This is the board that answers the actual
question — *which way of working with AI is effective, and what does it cost* —
rather than which vendor's model scores highest.
   EXIT GATE: a 3-strategy matrix (one-shot / adversarial / planner-fanout) over
   a 3-case suite, repeated, produces a board where the SAME case is comparable
   across strategy shapes on cost, wall-clock and regression rate — and the
   one-shot baseline is present in every comparison as the control.

### Phase 13.4 — Variance, controls & the "real or noise" question
Aggregation over N repeats with distributions (median + spread, not means alone);
the board refuses to rank variants whose intervals overlap and says so. Null-agent
and base-state controls run as first-class trial variants. Per-vertical and
per-complexity breakdowns; split-by-`contamination_risk` views so a skeptic can
ask "does the gap survive on low-risk cases?" and get an answer.
   EXIT GATE: a 3-repeat matrix reports distributions, and a deliberately
   noise-level difference is flagged as not-separable rather than ranked.

### Phase 13.5 — The reproducible bundle (publishability)
`lazyaf bench export <suite> --with-results` produces a portable bundle:
corpus (git bundles where licensing permits, fetch-instructions + patch where it
does not), case metadata incl. oracle ids and contamination tags, all trials with
their full provenance block, and a `METHOD.md` stating what was measured, the
controls, the caveats, and the exact commands to re-run it. `lazyaf bench import`
round-trips it, and re-running a bundle on the same harness version reproduces
the case set exactly (results within variance, which is the honest claim).
   EXIT GATE: export -> import on a clean checkout reconstructs the suite and
   replays a trial; the bundle's stated re-run command works verbatim; a bundle
   whose harness/image hashes differ from the current tree says so loudly instead
   of silently comparing apples to oranges.

### Open questions for Milestone 13

- **How many repeats** buy enough signal at what cost? (N is a dial; the answer is
  empirical — measure spread on the starter suite before fixing a default.)
- **Network access during a trial?** Dependency installs say yes; reproducibility
  and cost control say pin a proxy/cache. Leaning: allow, but record it as
  provenance and offer a cached-only mode for published runs.
- **Partial credit** (`fail_to_pass` 3/5) recorded but not headline — the board
  needs one honest number and "solved" is binary. Revisit if partial progress
  turns out to discriminate between loops that binary solve-rate cannot.
- **Licensing per fixture** now that the bundle is public: the export must decide
  per case what it may redistribute (hence `license` on `BenchmarkCase`).
- **How do K workers avoid duplicating each other's work?** The planner's
  instructions are the coordination mechanism; if they partition badly, fan-out
  degrades into K agents solving the same subproblem. Worth measuring directly
  (overlap in files touched across workers) rather than assuming.
- **Does the integrator need to be a strong model?** Cheap planning + cheap
  execution + expensive integration may be the real shape. `cost_by_role` is
  designed to answer this empirically.
- **Which integration policy wins?** Sequential merge is simplest; rebase keeps
  history linear but re-runs conflicts per worker; agent-composed integration
  could dodge conflicts entirely by having one model read K branches and write
  the union. This is a strategy axis, so the harness answers it rather than the
  architecture assuming it.
- **Is conflict rate a function of the PLANNER's quality?** A good instruction
  split should produce near-disjoint diffs. If conflict rate correlates with
  planner model strength, that is a strong, publishable result about where to
  spend intelligence in a fan-out.
- **Fan-out ceiling:** at what K does added parallelism stop helping (or start
  hurting via conflicts)? A sweep over K on one case is a cheap early experiment
  and probably the first genuinely publishable result this harness can produce.


---

## Phase 12: Runner Architecture Refactor

> **Vision**: Runners become execution targets (machines with capabilities), not execution environments. Steps run in ephemeral containers with a shared workspace. Enables multi-image pipelines, hardware-specific runners, and future Kubernetes support.

### Current Problems

1. **Entrypoint divergence**: Claude and Gemini runners are ~1800 lines each, 95% duplicated, features diverging
2. **Docker-in-Docker required**: For `type: docker` steps, runners need Docker socket mounted (gross)
3. **Workspace conflicts**: Multiple pipelines on same runner can destroy each other's workspace
4. **No image flexibility**: Steps inherit the runner's environment, can't use custom images
5. **No hardware affinity**: Can't route steps to specific hardware (embedded devices, GPUs)

### Target Architecture

**Two execution modes:**

```
MODE 1: LOCAL (Backend has Docker access) - Zero latency

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Local          | --> Docker API
    | Executor       |     | Router         |     | Executor       |         |
    +----------------+     +----------------+     +----------------+         |
                                                                             v
                                                                    +----------------+
                                                                    | Step Container |
                                                                    +----------------+

MODE 2: REMOTE (Hardware/distributed runners) - WebSocket push, millisecond latency

    BACKEND
    +----------------+     +----------------+     +----------------+
    | Pipeline       | --> | Execution      | --> | Remote         | <-- WebSocket
    | Executor       |     | Router         |     | Executor       |         |
    +----------------+     +----------------+     +----------------+         |
                                                                             |
            +----------------------------------------------------------------+
            | (push job immediately)
            v
    +----------------+     +----------------+     +----------------+
    | Runner Agent   |     | Runner Agent   |     | Runner Agent   |
    | (Docker host)  |     | (Raspberry Pi) |     | (GPU server)   |
    |                |     |                |     |                |
    | labels:        |     | labels:        |     | labels:        |
    |   arch=amd64   |     |   arch=arm64   |     |   arch=amd64   |
    |   type=docker  |     |   has=gpio     |     |   has=cuda     |
    |                |     |   has=camera   |     |                |
    | [Docker Orch.] |     | [Native Orch.] |     | [Docker Orch.] |
    +----------------+     +----------------+     +----------------+
```

### Key Design Decisions

**Event-driven, not polling**: Old runners polled every 5 seconds. New architecture:
- Local: Backend spawns containers directly (instant)
- Remote: Backend pushes jobs via WebSocket (milliseconds)

**OCI containers for everything** (except embedded hardware):
- System dependencies are container-level concerns
- Even "local" development uses Docker containers
- Native execution only for hardware that can't run Docker (GPIO, sensors)

### Core Concepts

**LocalExecutor**: Backend service that spawns containers directly via Docker SDK. No runner process, no polling - instant execution. This is the default for local development.

**RemoteExecutor**: Backend service that pushes jobs to connected runner agents via WebSocket. For remote Docker hosts, specialized hardware, distributed execution.

**Runner Agent**: Process that runs on remote machines, connects to backend via WebSocket, receives job assignments immediately. Has a local orchestrator (Docker or Native).

**Orchestrator**: How steps actually execute on a runner:
- `DockerOrchestrator`: Runs steps in containers (most common)
- `NativeOrchestrator`: Runs steps directly on host (embedded devices only)
- `KubernetesOrchestrator`: Runs steps as K8s Jobs (future)

**Workspace**: Per-pipeline-run working directory containing:
```
/workspace/
|-- repo/           # Git checkout
|-- home/           # Persistent $HOME (caches, .local/bin survive across steps)
+-- .control/       # Step config, logs, metadata
```

**Control Layer**: Thin wrapper in every step container handling heartbeat, log streaming, status reporting.

### Step Requirements (New Pipeline YAML)

```yaml
steps:
  - name: "Build firmware"
    type: docker
    config:
      image: "arm-toolchain:latest"
      command: "make firmware"
    requires:
      arch: arm64

  - name: "Run tests"
    type: docker
    config:
      image: "lazyaf-test-runner:latest"  # Pre-built with deps
      command: "pytest -v"

  - name: "Flash and test hardware"
    type: script
    config:
      command: "flash-firmware && run-hardware-tests"
    requires:
      has: gpio,camera
      runner_id: pi-workshop-1  # Pin to specific device
```

### Workspace Portability

| Scenario | Strategy |
|----------|----------|
| Same Docker host | Shared named volume (fast) |
| Different machines | Workspace tarball transfer |
| Kubernetes | PersistentVolumeClaim |

---

### Workspace Transfer Protocol

How workspaces move between steps, especially when steps run on different machines.

#### LocalExecutor (backend has Docker access)

Simplest case - all steps run on the same Docker host:

```
Step 1 container --> /workspace volume --> Step 2 container
                          |
                     Named volume persists
                     on Docker host
```

- Volume name: `lazyaf-ws-{pipeline_run_id}`
- All steps mount the same volume
- No transfer needed - it's already there
- Cleanup: Volume deleted when pipeline completes or after timeout

#### RemoteExecutor with DockerOrchestrator (remote Docker host)

Steps run on a remote machine with Docker. Same as LocalExecutor but on the remote host:

- Runner creates named volume on its local Docker
- All steps assigned to that runner share the volume
- **Affinity required**: Steps with `continue_in_context=true` MUST run on same runner

#### RemoteExecutor with NativeOrchestrator (embedded devices)

For devices that can't run Docker (Raspberry Pi GPIO work, bare metal, etc.):

- Workspace is a directory on the filesystem: `/var/lazyaf/workspaces/{pipeline_run_id}/`
- Runner manages the directory directly
- **Affinity required**: Steps with `continue_in_context=true` MUST run on same runner

#### Cross-Machine Workspace Transfer (Tarball Protocol)

When a step MUST run on a different machine than the previous step (different hardware requirements), the workspace is transferred as a tarball:

```
Runner A                      Backend                         Runner B
   |                             |                               |
   |-- step complete             |                               |
   |                             |                               |
   |-- POST /workspace-snapshot -->                              |
   |   (uploads tarball)         |                               |
   |                             |-- stores tarball              |
   |<-- 200 OK ------------------|                               |
   |                             |                               |
   |                             |   (step 2 assigned to B)      |
   |                             |                               |
   |                             |<-- GET /workspace-snapshot ---|
   |                             |   (B requests tarball)        |
   |                             |                               |
   |                             |--- tarball response --------->|
   |                             |                               |
   |                             |   (B extracts, runs step 2)   |
```

The runner's control layer handles upload/download - backend just stores the blob.

**API Endpoints:**
```
POST /api/pipeline-runs/{id}/workspace-snapshot
  Body: multipart/form-data with tarball
  Response: {snapshot_id, size_bytes}

GET /api/pipeline-runs/{id}/workspace-snapshot
  Response: application/gzip tarball

DELETE /api/pipeline-runs/{id}/workspace-snapshot
  (Called on pipeline completion)
```

**Tarball contents:**
```
workspace.tar.gz
|-- repo/              # Git checkout + uncommitted changes
|-- home/              # Persisted HOME (~/.local/bin, caches)
+-- .control/          # Step metadata, logs
```

**When transfer happens:**
- Only when next step has different `requires:` that forces a different runner
- NOT for normal `continue_in_context` on same runner (volume is faster)
- Backend detects machine boundary and triggers upload/download

**Size limits:**
- Default max: 500MB compressed
- Configurable per pipeline: `workspace_transfer_max_mb: 1000`
- Steps producing large artifacts should use dedicated artifact storage (future)

**Failure handling:**
- Upload fails: Step marked failed, pipeline can retry
- Download fails: Step marked failed, pipeline can retry
- Tarball corrupted: Checksum validation, retry from last good snapshot

---

### continue_in_context Semantics

The `continue_in_context` flag controls what persists between pipeline steps.

#### What IS Preserved

| Item | Location | Notes |
|------|----------|-------|
| Workspace files | `/workspace/repo/` | All files, tracked and untracked |
| Build artifacts | `/workspace/repo/` | node_modules, __pycache__, binaries |
| HOME directory | `/workspace/home/` | pip cache, npm cache, installed CLIs |
| Step logs | `/workspace/.lazyaf-context/` | Previous step outputs |
| Git state | `/workspace/repo/.git/` | Uncommitted changes preserved |

#### What is NOT Preserved

| Item | Why | Workaround |
|------|-----|------------|
| Container | Fresh container per step | Use same image if env matters |
| Environment variables | New process, new env | Set in step config or script |
| Running processes | Container dies between steps | Re-start in next step |
| Memory state | Fresh process | Serialize to file if needed |
| Network connections | Fresh container | Re-establish in next step |

#### Example: What Users Should Expect

```yaml
steps:
  - name: "Install deps"
    type: script
    config:
      command: |
        pip install pytest
        export MY_VAR=foo
        echo "done" > /tmp/marker
    continue_in_context: true

  - name: "Run tests"
    type: script
    config:
      command: |
        pytest -v           # Works - pytest in /workspace/home/.local/bin
        echo $MY_VAR        # Empty - env var not preserved
        cat /tmp/marker     # Fails - /tmp is container-local, not in workspace
```

#### Different Images Across Steps

Each step runs in its specified image. The workspace volume is mounted regardless:

```yaml
steps:
  - name: "Build Go binary"
    type: docker
    config:
      image: golang:1.21
      command: go build -o /workspace/repo/myapp
    continue_in_context: true

  - name: "Test with Python"
    type: docker
    config:
      image: python:3.12
      command: python /workspace/repo/integration_test.py
    # /workspace/repo/myapp binary is available!
```

This is intentional - allows heterogeneous pipelines. Caveat: architecture must match (can't build ARM binary on amd64 and run it).

#### Runner Affinity

> **Implementation Status** (as of Phase 11):
> - ✅ Basic affinity: `required_runner_id` enforced in job_queue.dequeue()
> - ✅ `continue_in_context` / `is_continuation` flags working in both runners
> - ✅ `previous_runner_id` passed between pipeline steps
> - ❌ `affinity_timeout` NOT implemented (jobs wait indefinitely for required runner)
> - ❌ No tests for affinity scenarios

When `continue_in_context: true`, the next step REQUIRES the same runner:

```
Step 1 (runner A, continue_in_context=true)
    |
    v
Step 2 (MUST be runner A - has the workspace volume)
```

**Affinity failure handling:**

| Scenario | Behavior |
|----------|----------|
| Runner A still connected | Step 2 assigned to A immediately |
| Runner A temporarily disconnected | Wait up to `affinity_timeout` (default 5 min) |
| Runner A dead (heartbeat timeout) | Pipeline FAILS with clear error |
| Runner A reconnects after timeout | Too late - pipeline already failed |

**Why not fall back to another runner?**
- Workspace state would be inconsistent
- Silent fallback causes confusing failures
- Explicit failure is better than subtle bugs

**Configurable timeout:**
```yaml
pipeline:
  affinity_timeout: 300  # seconds, default 5 minutes
```

#### Forcing Fresh Workspace

To explicitly NOT continue from previous step:

```yaml
steps:
  - name: "Build"
    continue_in_context: true  # Keep workspace

  - name: "Test in clean env"
    continue_in_context: false  # This step gets fresh clone
    # Previous workspace is discarded, fresh git clone
```

---

### Lifecycle State Machines

All state transitions are guarded by locks and idempotency keys to prevent race conditions and duplicate executions.

#### Centralized Locking (Backend is Source of Truth)

**Critical design decision**: All locking and state management happens in the backend database. Runners are stateless clients - they never hold locks or make decisions about state transitions.

**Two execution paths, same locking model:**

| Mode | How Steps Execute | Locking |
|------|-------------------|---------|
| **LocalExecutor** | Backend spawns containers directly via Docker SDK | DB row locks; container death detected via Docker API/exit codes |
| **RemoteExecutor** | Backend pushes jobs to runners via WebSocket | DB row locks + heartbeat timeout for runner death detection |

Both modes use the database as the single source of truth. The difference is how failure is detected:
- LocalExecutor: Docker SDK tells us immediately when a container dies
- RemoteExecutor: Heartbeat timeout (30s) tells us when a runner is unreachable

**RemoteExecutor flow (push-based, not polling):**

```
+-------------------+          +-------------------+          +-------------------+
|  Runner Agent A   |          |      BACKEND      |          |  Runner Agent B   |
|   (stateless)     |          | (source of truth) |          |   (stateless)     |
+-------------------+          +-------------------+          +-------------------+
        |                              |                              |
        |--- WebSocket connect ------->|                              |
        |--- register {labels} ------->|                              |
        |                              |-- DB: find pending step ---->|
        |                              |-- DB: SELECT FOR UPDATE ---->|
        |                              |<- lock acquired, assign -----|
        |<-- push: "execute step X" ---|                              |
        |--- ACK -------------------->|                              |
        |                              |                              |
        |--- heartbeat --------------->|                              |
        |--- heartbeat --------------->|                              |
        |                              |                              |
        |     (network dies)           |                              |
        |       X    X    X            |                              |
        |                              |-- 30s timeout: no heartbeat -|
        |                              |-- DB: mark step pending ---->|
        |                              |                              |
        |                              |<--- WebSocket connect -------|
        |                              |<--- register {labels} -------|
        |                              |-- DB: find pending step ---->|
        |                              |-- push: "execute step X" --->|
```

**Why this works:**
- Backend holds all state in PostgreSQL/SQLite with ACID guarantees
- `SELECT FOR UPDATE` prevents double-assignment race conditions
- Heartbeat timeout (30s) detects dead runners, releases their work
- Runners reconnecting check if their work was reassigned before resuming
- Idempotency keys prevent duplicate execution even with retries

**What runners do:**
- Connect via WebSocket, receive job assignments (pushed, not pulled)
- Send heartbeats every 10s to prove liveness
- Report status transitions (preparing -> running -> completed)
- Stream logs back to backend
- **Never** decide on their own whether to take or release work

**What backend does:**
- All assignment decisions (who gets what step)
- All state transitions (step status, workspace status)
- All lock management (row-level DB locks, not Redis)
- Heartbeat monitoring and dead runner detection
- Work re-queuing when runners die

**Locking implementation:**
- **Step assignment**: `SELECT ... FOR UPDATE` on step row
- **Idempotency**: Unique constraint on `execution_key` column
- **Workspace access**: PostgreSQL advisory locks (SQLite: file locking in dev)
- **No Redis required**: All coordination through the primary database

**Network partition handling:**
| Scenario | Backend Action |
|----------|----------------|
| Runner disconnects mid-job | Wait for heartbeat timeout (30s), return step to `pending` |
| Runner reconnects after timeout | Check if step was reassigned; if yes, abort local work |
| Runner reconnects before timeout | Continue normally, step still assigned to this runner |
| Backend restarts | Runners reconnect, re-register; backend resumes from DB state |
| Double-completion (race) | Idempotency key rejects second completion |
| LocalExecutor container dies | Docker SDK notifies backend immediately; step marked `failed` |

> **Note**: Current implementation uses 90s heartbeat timeout (generous for polling).
> WebSocket push model reduces this to 30s for faster failure detection.

**Debug mode integration**: Debug sessions (Phase 12.7) have extended timeouts (1-4 hours)
and their own state machine. When a step is at a debug breakpoint, the normal heartbeat
timeout is suspended - the debug session timeout applies instead.

#### Step Lifecycle

```
[pending] --> [assigned] --> [preparing] --> [running] --> [completing]
                                                 |              |
                                                 | exit_0       | finalized
                                                 v              v
                                            [timeout]      [completed]
                                                 |
                                                 v
[cancelled] <-- cancel (any state) ------- [failed]
```

| State | Description |
|-------|-------------|
| `pending` | Created, waiting for executor |
| `assigned` | Assigned to runner, awaiting ACK (remote only) |
| `preparing` | Pulling image, setting up workspace |
| `running` | Container executing |
| `completing` | Processing results |
| `completed` | Exit code 0, success |
| `failed` | Non-zero exit or exception |
| `cancelled` | User cancelled |
| `timeout` | Exceeded time limit |

**Idempotency**: Each step execution has an `execution_key = "{pipeline_run_id}:{step_index}:{attempt}"`. Duplicate requests return existing execution.

#### Workspace Lifecycle

```
[creating] --> [ready] <--> [in_use] --> [cleaning] --> [destroyed]
     |                          |
     | create_failed            | audit_detects_orphan
     v                          v
 [failed]                  [orphaned] --> manual cleanup --> destroyed
```

| State | Description |
|-------|-------------|
| `creating` | Volume being created, repo cloning |
| `ready` | Available, no active steps |
| `in_use` | Step(s) currently executing |
| `cleaning` | Pipeline done, destroying volume |
| `destroyed` | Cleaned up (terminal) |
| `orphaned` | Lost track, needs manual cleanup |
| `failed` | Creation failed |

**Locking**:
- Exclusive lock for creation/cleanup
- Shared lock for step execution (allows parallel steps)
- Use count tracks concurrent usage

#### Pipeline Run Lifecycle

```
[pending] --> [preparing] --> [running] --> [completing] --> [completed]
                   |              |
                   | prep_failed  | step_failed
                   v              v
              [failed] <----- [failed]
                   ^
                   | timeout
             [cancelled] <-- cancel (any non-terminal)
```

**Exactly-once step execution**: Pipeline executor skips steps with completed `execution_key`.

**Trigger deduplication**: Triggers have a `trigger_key` (e.g., `push:{repo}:{sha}`). Duplicates within 1 hour are ignored.

#### Runner Lifecycle (Remote Only)

```
[disconnected] --> [connecting] --> [idle] --> [assigned] --> [busy]
       ^                                            |            |
       |                                            | ack_timeout|
       |                                            v            |
       |                                        [dead] <---------+
       |                                            |
       +------- heartbeat_timeout -----------------+
       |                                            |
       +------------- reconnect -------------------+
```

| State | Description |
|-------|-------------|
| `disconnected` | No WebSocket connection |
| `connecting` | WebSocket open, registration pending |
| `idle` | Ready to accept jobs |
| `assigned` | Job sent, awaiting ACK |
| `busy` | Executing step |
| `dead` | Heartbeat timeout, presumed crashed |

**Job recovery**: When runner dies mid-job, step is re-queued if still in `running` state (prevents duplicate if completion was lost).

#### Synchronization Requirements

| Resource | Lock Type | Implementation |
|----------|-----------|----------------|
| Step assignment | Row lock | `SELECT FOR UPDATE` on step row |
| Step execution | Idempotency key | Unique constraint on `execution_key` |
| Workspace access | Shared/Exclusive | PostgreSQL advisory locks (SQLite file lock in dev) |
| Pipeline execution | Exclusive | Row lock on pipeline_run + single executor process |
| Trigger dedup | Time-windowed key | Unique constraint on `trigger_key` + created_at window |

> **No Redis required**: All synchronization uses the primary database. This simplifies
> deployment and eliminates a distributed systems failure mode.

#### Crash Recovery

On backend restart:
1. **Steps**: Find non-terminal steps, reattach to running containers or re-queue
2. **Pipelines**: Resume execution (idempotent - skips completed steps)
3. **Workspaces**: Audit for orphans, cleanup stale volumes
4. **Runners**: Mark as dead, wait for reconnection

---

### Phase 12 Prerequisites: Test Infrastructure

> **Goal**: Establish platform-level test hooks and fixtures BEFORE any Phase 12 implementation begins. Tests define interfaces and expected behavior - they are written FIRST, not as an afterthought.

**This must exist BEFORE any Phase 12 work begins.**

#### Test Fixtures (Create First)

- [ ] `tdd/conftest.py` with shared pytest fixtures:
  - `docker_client` - Connected Docker SDK client (skip tests if unavailable)
  - `test_database` - Fresh SQLite in-memory or temp file per test
  - `async_session` - Async SQLAlchemy session factory
  - `mock_websocket` - Fake WebSocket for protocol testing
  - `temp_workspace` - Creates and cleans up temp directories

#### Platform-Level Test Hooks

- [ ] Docker manipulation helpers (`tdd/shared/docker_helpers.py`):
  - `spawn_test_container(image, command)` - Create container, return handle
  - `kill_container(container_id)` - Force kill
  - `pause_container(container_id)` - Simulate hang
  - `disconnect_network(container_id)` - Network partition
- [ ] Process control helpers (`tdd/shared/process_helpers.py`):
  - `kill_process(pid)` - Simulate crash
  - `send_signal(pid, signal)` - SIGTERM, SIGKILL, SIGSTOP
- [ ] Time manipulation (`tdd/shared/time_helpers.py`):
  - `freeze_time(timestamp)` - For timeout testing
  - `advance_time(seconds)` - Fast-forward for heartbeat tests

#### Mock Infrastructure

- [ ] `MockDockerClient` - Fake Docker SDK for unit tests (no real Docker needed)
- [ ] `MockWebSocket` - Fake WebSocket for protocol tests
- [ ] `TestDatabase` - Fresh database per test with rollback
- [ ] `MockRunner` - Simulates runner behavior for RemoteExecutor tests

#### Chaos Test Infrastructure

- [ ] `ChaosController` class (`tdd/shared/chaos.py`):
  - `inject_failure(type, target, duration)` - Programmatic failure injection
  - Failure types: network_partition, process_kill, disk_full, slow_io
- [ ] Recovery verification helpers:
  - `wait_for_state(entity, expected_state, timeout)`
  - `assert_eventually(condition, timeout, interval)`

**Outcome**: All Phase 12 sub-phases can write tests immediately without infrastructure blockers.

---

### Phase 12 Prerequisites: E2E Test Infrastructure

> **Goal**: Establish end-to-end tests covering full user workflows (frontend + backend + runner) BEFORE Phase 12 refactoring begins. These tests validate the system works correctly and serve as regression protection during architectural changes.

**Why E2E tests before Phase 12?**
- Phase 12 fundamentally changes how steps execute (polling -> push, runners -> containers)
- Unit tests verify components work; E2E tests verify the *system* works
- Catch integration bugs that unit tests miss (WebSocket message ordering, SSE timing, etc.)
- Confidence to refactor: if E2E tests pass after Phase 12, the system still works

#### Key Decision: Mock Executor Type

E2E tests use a **mock executor** that's a first-class executor type alongside `claude` and `gemini`:

```
job_queue → runner → container → mock executor → scripted response through normal flow
```

**NOT** special test-only logic or CLI overrides. The mock executor is a real executor that:
- Gets dispatched through the same runner pathways as claude/gemini
- Runs in the same container infrastructure
- Returns scripted responses based on configuration
- Exercises all the same WebSocket/SSE/status update code paths

```
+-------------+     +---------+     +-----------+     +----------------+
| Test Driver | --> | Backend | --> | Container | --> | Mock Executor  |
|             |     |  (real) |     |   (real)  |     | (type: "mock") |
+-------------+     +---------+     +-----------+     +----------------+
                         |                                    |
                         v                                    v
                    [WebSocket]                       [Scripted Response]
                    [Events]                          [File Changes]
```

**Mock Executor behavior**:
- Reads mock config from `/workspace/.control/mock_config.json`
- Creates file changes as specified in config
- Returns scripted output (reasoning, tool calls, completion)
- Supports streaming mode for playground tests
- Same log streaming, status updates, and completion flow as real executors

---

#### User Story 1: Create and Execute a Card

**As a** developer
**I want to** create a card and start agent work
**So that** I can have AI implement a feature

**Happy Path**:
1. User opens board, selects repo
2. User creates card with title and description
3. User clicks "Start Work"
4. Runner picks up job, clones repo, executes agent
5. Agent makes file changes
6. Card status progresses: `todo` → `in_progress` → `in_review`
7. Diff is visible in card detail

**Acceptance Criteria**:
| Criterion | Verification |
|-----------|--------------|
| Card created in database | GET `/api/cards/{id}` returns 200 |
| Job queued | Job appears in job_queue |
| Runner executes | Container started, logs streaming |
| Status updates via WebSocket | Frontend receives `card_updated` events |
| Diff captured | GET `/api/cards/{id}/diff` returns changes |
| Card reaches `in_review` | Final status = `in_review` |

**Failure Modes to Test**:
| Failure | Expected Behavior |
|---------|-------------------|
| Agent returns non-zero exit | Card status → `failed`, error captured |
| Container timeout | Card status → `failed`, timeout error |
| Agent makes no changes | Card status → `in_review` with empty diff |
| WebSocket disconnect mid-job | Reconnect sees correct final state |

---

#### User Story 2: Review and Approve/Reject Changes

**As a** reviewer
**I want to** see the diff and approve or reject changes
**So that** quality code gets merged

**Happy Path (Approve)**:
1. User opens card in `in_review` status
2. User sees diff with file changes
3. User clicks "Approve"
4. Card status → `done`
5. Changes merged to target branch

**Happy Path (Reject)**:
1. User opens card in `in_review` status
2. User sees diff with file changes
3. User clicks "Reject"
4. Card status → `todo` (or `failed` depending on config)
5. Branch preserved for retry

**Acceptance Criteria**:
| Criterion | Verification |
|-----------|--------------|
| Diff displays correctly | UI shows correct file changes |
| Approve updates status | Card status → `done` |
| Approve triggers merge | Branch merged to default branch |
| Reject updates status | Card status → `todo` |
| WebSocket broadcasts update | Other tabs see status change |

**Failure Modes to Test**:
| Failure | Expected Behavior |
|---------|-------------------|
| Merge conflict on approve | Card status → `failed`, conflict error shown |
| Approve card not in_review | 400 error, status unchanged |
| Concurrent approve attempts | One succeeds, other gets 409 conflict |

---

#### User Story 3: Pipeline Triggers on Card Completion

**As a** developer
**I want** pipelines to auto-trigger when cards complete
**So that** tests run automatically after AI work

**Happy Path**:
1. Pipeline configured with `card_complete` trigger
2. Card reaches `in_review` (or `done` depending on trigger config)
3. Pipeline automatically starts
4. Pipeline steps execute sequentially
5. On pipeline success: trigger action executes (e.g., merge)
6. On pipeline failure: card marked failed

**Acceptance Criteria**:
| Criterion | Verification |
|-----------|--------------|
| Trigger fires on card status | Pipeline run created automatically |
| Trigger context includes card info | `trigger_context` has card_id, branch, commit |
| Pipeline runs correct steps | Steps execute in order |
| on_pass action executes | Merge happens if configured |
| on_fail action executes | Card marked failed if configured |
| WebSocket broadcasts pipeline status | `pipeline_run_status` events sent |

**Failure Modes to Test**:
| Failure | Expected Behavior |
|---------|-------------------|
| Pipeline step fails | Pipeline fails, on_fail action runs |
| Trigger disabled | No pipeline run created |
| No matching pipeline | Card status unchanged |
| Pipeline already running for card | Duplicate trigger ignored (dedup) |

---

#### User Story 4: Agent Playground - Quick Iteration

**As a** developer
**I want to** test agent prompts without full card workflow
**So that** I can iterate quickly on agent behavior

**Happy Path**:
1. User opens Playground tab
2. User selects repo, branch, agent
3. User optionally overrides task description
4. User clicks "Test Once"
5. SSE stream shows agent reasoning in real-time
6. On completion, diff preview shows changes
7. User optionally saves to branch

**Acceptance Criteria**:
| Criterion | Verification |
|-----------|--------------|
| SSE stream connects | `/playground/{session_id}/stream` returns events |
| Tokens stream in real-time | Events arrive before completion |
| Tool calls visible | Tool use events displayed |
| Diff captured on completion | `get_result()` returns diff |
| Save to branch works | Changes pushed to `agent-test/*` branch |
| Cancel stops execution | Container killed, session cleaned up |

**Failure Modes to Test**:
| Failure | Expected Behavior |
|---------|-------------------|
| Agent error mid-stream | Error event sent, session ends gracefully |
| SSE disconnect | Reconnect shows final state |
| Save to non-existent branch | Branch created |
| Cancel during git push | Partial changes not pushed |

---

#### User Story 5: Real-Time Updates Across Tabs

**As a** user with multiple browser tabs open
**I want** all tabs to stay in sync
**So that** I see consistent state

**Happy Path**:
1. User opens board in two tabs
2. Tab 1: Creates card
3. Tab 2: Sees card appear (WebSocket)
4. Tab 1: Starts work on card
5. Tab 2: Sees status change to `in_progress`
6. Tab 1: Card completes
7. Tab 2: Sees status change to `in_review`

**Acceptance Criteria**:
| Criterion | Verification |
|-----------|--------------|
| Card create broadcast | `card_created` event received |
| Card update broadcast | `card_updated` events for status changes |
| Pipeline run broadcast | `pipeline_run_status` events |
| Step run broadcast | `step_run_status` events |
| Repo update broadcast | `repo_updated` events |

**Failure Modes to Test**:
| Failure | Expected Behavior |
|---------|-------------------|
| WebSocket disconnect | Reconnect, state refreshed via REST |
| Missed event during disconnect | REST call catches up |
| High-frequency updates | No events dropped, order preserved |

---

#### E2E Test Structure

```
tdd/e2e/
├── conftest.py                    # E2E-specific fixtures
├── helpers/
│   ├── __init__.py
│   ├── mock_executor.py           # Mock executor configuration helpers
│   ├── browser.py                 # Playwright browser helpers
│   ├── websocket.py               # WebSocket test client
│   └── assertions.py              # E2E-specific assertions
├── test_card_execute.py           # User Story 1: Create and Execute
├── test_review_flow.py            # User Story 2: Review and Approve/Reject
├── test_pipeline_triggers.py      # User Story 3: Pipeline Triggers
├── test_playground.py             # User Story 4: Agent Playground
├── test_realtime_updates.py       # User Story 5: WebSocket Consistency
└── fixtures/
    ├── mock_responses/            # Canned mock executor responses
    │   ├── simple_file_change.json
    │   ├── multi_file_change.json
    │   └── error_response.json
    └── test_repos/                # Minimal git repos for testing
        └── minimal-repo/
```

---

#### Mock Executor Implementation

**Add `mock` as a first-class executor type** alongside `claude` and `gemini`:

The mock executor is added to the runner codebase as a real executor that:

1. Reads mock configuration from `/workspace/.control/mock_config.json`
2. Performs scripted file operations (create, modify, delete)
3. Outputs scripted response with configurable streaming
4. Exits with configured exit code
5. Uses the same log streaming and status reporting as real executors

**Mock Config Format**:
```json
{
  "response_mode": "streaming",
  "file_operations": [
    {
      "action": "create",
      "path": "src/new_file.py",
      "content": "# New file content"
    },
    {
      "action": "modify",
      "path": "src/existing.py",
      "search": "old_code",
      "replace": "new_code"
    }
  ],
  "output_events": [
    {"type": "content", "text": "I'll analyze the code..."},
    {"type": "tool_use", "tool": "Read", "path": "src/main.py"},
    {"type": "content", "text": "Making the requested changes..."},
    {"type": "tool_use", "tool": "Edit", "path": "src/main.py"},
    {"type": "complete", "text": "Done!"}
  ],
  "exit_code": 0,
  "delay_ms": 100
}
```

**Runner Integration**:

```python
# In runner entrypoint - mock executor dispatched same as claude/gemini
if runner_type == "mock":
    from executors.mock_executor import execute_mock
    execute_mock(config_path="/workspace/.control/mock_config.json")
elif runner_type == "claude":
    # existing claude logic
elif runner_type == "gemini":
    # existing gemini logic
```

**Why this approach?**
- No special test-only code paths in production executors
- Mock executor exercises the full runner machinery
- Tests can target `runner_type: "mock"` just like `runner_type: "claude"`
- Same WebSocket events, same status updates, same log streaming

---

#### E2E Test Fixtures

**conftest.py for E2E**:
```python
@pytest.fixture(scope="session")
def e2e_backend():
    """Start backend server for E2E tests."""
    # Start real backend (not ASGI test client)
    # Returns base URL

@pytest.fixture(scope="session")
def e2e_browser():
    """Playwright browser instance."""
    # Returns browser context

@pytest.fixture
def mock_executor_config():
    """Factory for creating mock executor configurations."""
    def _create(file_ops=None, output=None, exit_code=0):
        return MockExecutorConfig(...)
    return _create

@pytest.fixture
def test_repo(e2e_backend):
    """Create and ingest a minimal test repo."""
    # Returns repo data with cleanup

@pytest.fixture
def websocket_client(e2e_backend):
    """WebSocket client for real-time event testing."""
    # Returns connected client
```

---

#### Phase 12 E2E Prerequisites Checklist

- [ ] **Mock Executor**
  - [ ] Add `mock` executor type to runner entrypoint
  - [ ] Implement mock executor logic (read config, apply file ops, stream output)
  - [ ] Define mock config JSON schema
  - [ ] Unit test mock executor standalone

- [ ] **E2E test harness**
  - [ ] Create `tdd/e2e/conftest.py` with fixtures
  - [ ] Backend startup fixture (real server, not ASGI)
  - [ ] Playwright browser fixture
  - [ ] WebSocket client fixture
  - [ ] Test repo fixture with cleanup

- [ ] **User Story 1: Card Execute**
  - [ ] `test_card_create_and_execute_happy_path`
  - [ ] `test_card_execute_agent_error`
  - [ ] `test_card_execute_timeout`
  - [ ] `test_card_execute_no_changes`

- [ ] **User Story 2: Review Flow**
  - [ ] `test_review_approve_happy_path`
  - [ ] `test_review_reject_happy_path`
  - [ ] `test_review_merge_conflict`
  - [ ] `test_review_concurrent_approve`

- [ ] **User Story 3: Pipeline Triggers**
  - [ ] `test_pipeline_auto_trigger_on_card_complete`
  - [ ] `test_pipeline_on_pass_merge`
  - [ ] `test_pipeline_on_fail_marks_card_failed`
  - [ ] `test_pipeline_trigger_deduplication`

- [ ] **User Story 4: Playground**
  - [ ] `test_playground_streaming_output`
  - [ ] `test_playground_diff_capture`
  - [ ] `test_playground_save_to_branch`
  - [ ] `test_playground_cancel`

- [ ] **User Story 5: Real-Time Updates**
  - [ ] `test_websocket_card_created_broadcast`
  - [ ] `test_websocket_card_status_updates`
  - [ ] `test_websocket_reconnect_catches_up`

---

#### Done Criteria for E2E Prerequisites

1. **Mock Executor works**: Can run mock executor with config, produces expected file changes and output
2. **E2E harness runs**: `pytest tdd/e2e/ --collect-only` shows all tests
3. **At least one happy path per user story passes**: Core workflows verified
4. **At least one failure mode per user story passes**: Error handling verified
5. **Tests run in CI**: GitHub Actions workflow for E2E tests
6. **No flaky tests**: Tests pass consistently 10 runs in a row

**Effort Estimate**: 1-2 weeks
**Risk**: Medium (Playwright setup, WebSocket testing can be tricky)
**Outcome**: Confidence to proceed with Phase 12 refactoring

---

### Phase 12.0: Unify Runner Entrypoints
**Goal**: Fix immediate pain, unblock future phases

The current entrypoints are ~1800 lines each with 95% duplication. This phase extracts common code into a shared package.

#### Tests First (Define Contracts)

Write these tests BEFORE implementing the shared modules:

**test_git_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_clone_creates_repo_at_path` | `clone(url, path) -> None` raises on failure |
| `test_checkout_branch_switches` | `checkout(path, branch) -> None` |
| `test_get_current_sha_returns_string` | `get_sha(path) -> str` |
| `test_clone_handles_auth_failure` | Returns specific exception type |

**test_context_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_create_context_dir_structure` | `.lazyaf-context/` has expected subdirs |
| `test_write_context_file_creates_json` | Files are valid JSON |
| `test_read_context_returns_parsed` | Read matches written |

**test_job_helpers.py**
| Test | Defines Contract |
|------|------------------|
| `test_heartbeat_sends_to_backend` | `send_heartbeat(job_id)` hits correct endpoint |
| `test_heartbeat_timeout_raises` | Raises after N seconds |
| `test_status_report_formats_correctly` | Status payload structure |

- [x] Write `test_git_helpers.py` (defines interface) - 17 tests
- [x] Write `test_context_helpers.py` (defines interface) - 20 tests
- [x] Write `test_job_helpers.py` (defines interface) - 23 tests

#### Implementation (Make Tests Pass)

- [x] Create `runner-common/` package structure with stub modules
  - `git_helpers.py` - clone, branch, push, commit operations
  - `context_helpers.py` - `.lazyaf-context/` management
  - `job_helpers.py` - heartbeat, logging, status reporting
  - `executors/` - Agent-specific CLI invocation (ClaudeExecutor, GeminiExecutor, MockExecutor)
  - `entrypoint.py` - Unified runner entrypoint
- [x] Implement `git_helpers.py` to pass tests (17 tests)
- [x] Implement `context_helpers.py` to pass tests (20 tests)
- [x] Implement `job_helpers.py` to pass tests (23 tests)
- [x] Create `executors/` package with Claude/Gemini/Mock executors (23 tests)
- [x] Create unified entrypoint that dispatches by agent type (17 tests)
- [x] Reduce Claude/Gemini-specific code to ~50 lines each (just CLI invocation)

#### Integration Validation

- [x] `test_entrypoint.py`:
  - Claude agent type routes correctly
  - Gemini agent type routes correctly
  - Mock agent type routes correctly
  - Unknown agent type fails with clear error
- [ ] `test_existing_pipelines_still_work.py`:
  - Run actual pipeline with unified entrypoint
  - Compare output to baseline

#### Done Criteria

- [x] All `test_*_helpers.py` tests pass (100 tests total, 1 skipped)
- [x] E2E tests pass with mock runner (9 tests, validates full pipeline flow)
- [x] No regression in existing pipeline behavior (e2e tests confirm)

**Effort**: 2-3 days
**Risk**: Low
**Outcome**: Maintainable entrypoints, foundation for new architecture

---

### Phase 12.1: LocalExecutor + Step State Machine
**Goal**: Instant step execution with proper state management

The fast path - backend spawns containers directly, with full lifecycle tracking.

#### Tests First (Define Contracts)

**test_step_state_machine.py** - Write BEFORE implementing state machine
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_assigned_valid` | Transition allowed |
| `test_pending_to_running_invalid` | Must go through assigned first |
| `test_running_to_completed_on_exit_0` | Exit code 0 = success |
| `test_running_to_failed_on_nonzero` | Exit code != 0 = failure |
| `test_running_to_timeout_on_deadline` | Timeout = specific state |
| `test_cancel_from_any_state` | Cancel always works |
| `test_completed_is_terminal` | No transitions from completed |
| `test_transition_records_timestamp` | State changes have timestamps |

**test_idempotency_keys.py** - Write BEFORE implementing idempotency
| Test | Defines Contract |
|------|------------------|
| `test_execution_key_format` | Format: `{run_id}:{step}:{attempt}` |
| `test_same_key_returns_existing` | Duplicate request = same execution |
| `test_different_attempt_new_execution` | Retry = new execution |

**test_local_executor_contract.py** - Write BEFORE implementing LocalExecutor
| Test | Defines Contract |
|------|------------------|
| `test_execute_step_returns_generator` | `execute_step() -> AsyncGenerator` |
| `test_execute_step_idempotent` | Same key = same result |
| `test_execute_step_spawns_container` | Container created with correct image |
| `test_execute_step_mounts_workspace` | Volume mounted at /workspace |
| `test_execute_step_streams_logs` | Generator yields log lines |
| `test_timeout_kills_container` | Container killed after timeout |
| `test_crash_detection_fails_step` | Container crash = step failed |

- [x] Write `test_step_state_machine.py` (defines state transitions) - 32 tests
- [x] Write `test_idempotency_keys.py` (defines idempotency contract) - 18 tests
- [x] Write `test_local_executor_contract.py` (defines executor interface) - 31 tests

#### Database Migration

- [x] Create `StepExecution` model in `backend/app/models/pipeline.py`
  ```python
  class StepExecution(Base):
      __tablename__ = "step_executions"
      id: str  # UUID
      execution_key: str  # "{pipeline_run_id}:{step_index}:{attempt}" - UNIQUE constraint
      step_run_id: str  # FK to step_runs
      status: str  # pending, assigned, preparing, running, completing, completed, failed, cancelled, timeout
      runner_id: str | None  # Which runner is executing (remote only)
      container_id: str | None  # Docker container ID (local only)
      exit_code: int | None
      error: str | None
      started_at: datetime | None
      completed_at: datetime | None
      created_at: datetime
  ```
- [x] Add unique index on `execution_key` for idempotency

#### Implementation (Make Tests Pass)

- [x] Implement Step state machine in `backend/app/services/execution/state_machine.py`
- [x] Implement idempotency service in `backend/app/services/execution/idempotency.py`
- [x] Create `LocalExecutor` service in `backend/app/services/execution/local_executor.py`
- [x] Docker SDK (`docker` package) already in backend dependencies
- [x] Timeout handling with automatic container kill
- [x] Container crash detection and proper state transition to `failed`
- [x] Real-time log streaming from container
- [x] Mount Docker socket to backend container in docker-compose
- [x] Crash recovery: on startup, find orphaned steps and mark them failed

#### Integration Validation

- [x] `test_local_executor_real_docker.py` (requires Docker):
  - Actually spawns container
  - Actually streams logs
  - Actually detects exit codes
- [x] `test_recovery.py`:
  - Orphaned executions (pending, running, preparing) marked as failed on startup
  - Terminal executions (completed, failed) not affected
  - Recovery sets completed_at timestamp

#### Chaos Tests

- [x] `test_chaos_oom.py` - OOM exit codes (137) detected as failed
- [x] `test_chaos_docker_unavailable.py` - Docker down = graceful error handling
  - Connection refused handled
  - API timeouts handled
  - Image not found handled
  - Resource exhaustion handled

#### Done Criteria

- [x] All state machine unit tests pass (32 tests)
- [x] All idempotency tests pass (18 tests)
- [x] LocalExecutor contract tests pass (31 tests)
- [x] Recovery tests pass (11 tests)
- [x] Integration tests pass with real Docker (8 tests)
- [x] Chaos tests pass (12 tests)
- [x] Total: 111 tests passed, 1 skipped (async timeout needs implementation)

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Local dev is instant with proper state tracking and crash recovery

---

### Phase 12.2: Workspace State Machine & Pipeline Integration
**Goal**: Proper workspace lifecycle with locking and cleanup

#### Tests First (Define Contracts)

**test_workspace_state_machine.py** - Write BEFORE implementing workspace lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_creating_to_ready_on_success` | Volume created = ready |
| `test_creating_to_failed_on_error` | Volume creation fails = failed |
| `test_ready_to_in_use_increments_count` | use_count tracks concurrent access |
| `test_in_use_to_ready_decrements_count` | Step completes = decrement |
| `test_cleaning_requires_zero_use_count` | Can't clean while in use |
| `test_orphaned_detection` | Workspace with no pipeline = orphaned |

**test_workspace_locking.py** - Write BEFORE implementing locking
| Test | Defines Contract |
|------|------------------|
| `test_exclusive_lock_for_create` | Only one creator |
| `test_exclusive_lock_for_cleanup` | Only one cleaner |
| `test_shared_lock_for_execution` | Multiple steps can run |
| `test_lock_timeout_returns_false` | Don't block forever |

**test_execution_router.py** - Write BEFORE implementing router
| Test | Defines Contract |
|------|------------------|
| `test_routes_to_local_when_no_requirements` | Default = LocalExecutor |
| `test_routes_to_remote_when_hardware_required` | `requires: {has: gpio}` = remote |
| `test_returns_executor_handle` | Caller gets async generator |

**test_pipeline_state_machine.py** - Write BEFORE implementing pipeline lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_preparing` | Pipeline starts |
| `test_preparing_to_running` | Workspace ready |
| `test_running_to_completing` | All steps done |
| `test_completing_to_completed` | Cleanup done |
| `test_step_failure_fails_pipeline` | One step fails = pipeline fails |

**test_trigger_deduplication.py** - Write BEFORE implementing dedup
| Test | Defines Contract |
|------|------------------|
| `test_same_trigger_key_within_window_ignored` | Duplicate = no new run |
| `test_same_trigger_key_after_window_allowed` | Window expired = new run |
| `test_trigger_key_format` | Format: `{type}:{repo}:{ref}` |

- [ ] Write `test_workspace_state_machine.py` (defines workspace lifecycle)
- [ ] Write `test_workspace_locking.py` (defines locking semantics)
- [ ] Write `test_execution_router.py` (defines routing contract)
- [ ] Write `test_pipeline_state_machine.py` (defines pipeline lifecycle)
- [ ] Write `test_trigger_deduplication.py` (defines dedup contract)

#### Implementation (Make Tests Pass)

- [ ] Implement Workspace state machine (make workspace tests pass)
- [ ] Create `Workspace` model with state and use_count
  ```python
  class Workspace:
      id: str  # "lazyaf-ws-{pipeline_run_id}"
      status: WorkspaceStatus
      use_count: int  # For concurrent step access
      pipeline_run_id: str
  ```
- [ ] Implement workspace locking (make lock tests pass)
- [ ] Idempotent workspace creation (`get_or_create_workspace`)
- [ ] Create `ExecutionRouter` (make routing tests pass)
- [ ] Update `pipeline_executor.py` to use ExecutionRouter instead of job queue
- [ ] Implement pipeline state machine (make pipeline tests pass)
- [ ] Implement trigger deduplication (make dedup tests pass)
- [ ] Workspace cleanup on pipeline completion
- [ ] Orphan detection: periodic audit finds abandoned workspaces

#### Integration Validation

- [ ] `test_multi_step_pipeline.py`:
  - Step 1 completes, workspace persists
  - Step 2 sees Step 1 artifacts
  - Pipeline completes, workspace cleaned
- [ ] `test_different_images_share_workspace.py`:
  - Step 1 in `golang:1.21`
  - Step 2 in `python:3.12`
  - Workspace contains both outputs
- [ ] `test_workspace_cleanup_on_failure.py`:
  - Pipeline fails mid-execution
  - Workspace still cleaned up (eventually)

#### Chaos Tests

- [ ] `test_concurrent_workspace_access.py` - Multiple steps, same workspace
- [ ] `test_orphan_workspace_recovery.py` - Backend dies, workspace orphaned, recovered on restart

#### Done Criteria

- [ ] Workspace state machine tests pass
- [ ] Locking tests pass (no race conditions)
- [ ] ExecutionRouter tests pass
- [ ] Pipeline state machine tests pass
- [ ] Multi-step integration test passes
- [ ] Orphan recovery test passes

**Effort**: 1.5 weeks
**Risk**: Medium
**Outcome**: Robust workspace lifecycle, exactly-once execution, no orphaned resources

---

### Phase 12.2.5: Specification Data Model
**Goal**: Stand up the spec layer (Feature / UserStory / AcceptanceCriterion / PromptTemplate) with CRUD APIs and a minimal UI. No execution changes yet — just the foundation for Phase 12.2.6 and beyond.

> **[Superseded 2026-08-29 — 12.2.5 shipped alongside 12.2-INT and 12.3 landed first; 12.2.6 is a deliberate retrofit. Kept for design context.]**
>
> **Why now (before 12.3):** Phase 12.3 freezes the Control Layer protocol — what steps report back to the backend. Once the spec models exist, 12.3 can extend that protocol with a test-result manifest channel (see Phase 12.2.6) instead of bolting it on later.

#### Tests First (Define Contracts)

**test_feature_crud.py**
| Test | Defines Contract |
|------|------------------|
| `test_create_feature_returns_id` | POST `/api/features` returns 201 with UUID |
| `test_feature_spans_multiple_repos` | `repo_ids` accepts list, validated against existing repos |
| `test_feature_status_transitions` | proposed -> active -> shipped -> deprecated only |
| `test_delete_feature_cascades_stories` | Removing feature removes orphaned stories |

**test_user_story_crud.py**
| Test | Defines Contract |
|------|------------------|
| `test_story_requires_feature` | Cannot create story without feature_id |
| `test_story_repos_subset_of_feature` | Story repo_ids must be subset of feature.repo_ids |
| `test_story_priority_int` | Priority is plain int, not story points enum |
| `test_story_narrative_freeform` | No gherkin enforcement — markdown OK |

**test_criterion_crud.py**
| Test | Defines Contract |
|------|------------------|
| `test_criterion_requires_story` | Cannot create without story |
| `test_required_blocks_story_done` | Story can't be `done` if any required criterion has no passing TestRun |
| `test_criterion_can_have_no_tests` | Criterion exists without TestRefs (yet) |

**test_prompt_template_versioning.py**
| Test | Defines Contract |
|------|------------------|
| `test_first_version_starts_at_1` | New template gets version 1 |
| `test_new_version_immutable_predecessor` | Old versions cannot be edited |
| `test_placeholders_extracted_from_body` | `{story_narrative}` placeholders auto-detected |

**test_card_spec_links.py**
| Test | Defines Contract |
|------|------------------|
| `test_card_can_link_to_feature` | `feature_id` settable on existing Card |
| `test_card_can_link_to_story` | `user_story_id` settable on existing Card |
| `test_promote_card_creates_feature` | Promotion creates Feature + Story; original Card relinked |

- [ ] Write `test_feature_crud.py`
- [ ] Write `test_user_story_crud.py`
- [ ] Write `test_criterion_crud.py`
- [ ] Write `test_prompt_template_versioning.py`
- [ ] Write `test_card_spec_links.py`

#### Database Migration

- [ ] Alembic migration creating: `features`, `user_stories`, `acceptance_criteria`, `prompt_templates`, `prompt_versions`
- [ ] `feature_repos` join table for cross-repo scope
- [ ] `story_repos` join table
- [ ] Add nullable `feature_id`, `user_story_id`, `promotes_to_feature` columns to `cards`

#### Implementation (Make Tests Pass)

- [ ] Pydantic schemas in `backend/app/schemas/spec.py`
- [ ] SQLAlchemy models in `backend/app/models/spec.py`
- [ ] Service layer in `backend/app/services/spec/` (one module per entity)
- [ ] Routers in `backend/app/routers/spec.py`
- [ ] WebSocket events: `feature_updated`, `story_updated`, `criterion_updated`
- [ ] MCP tools: add spec CRUD to MCP server (so Claude Desktop can author specs)

#### Minimal UI (Frontend)

- [ ] `/specs` route — feature list with status badges
- [ ] Feature detail page — stories + criteria tree (collapsible)
- [ ] Story editor — markdown narrative, criterion checklist
- [ ] Card detail panel: "Linked feature/story" selector + "Promote to feature" button

#### Integration Validation

- [ ] `test_promote_card_to_feature_e2e.py` — full UI flow
- [ ] `test_cross_repo_feature_appears_on_both_repos` — feature shows in repo views for all linked repos
- [ ] `test_mcp_can_create_feature_from_claude_desktop`

#### Done Criteria

- [ ] All CRUD test suites pass
- [ ] UI lets a user define a feature with at least one story and one criterion in under 60s
- [ ] Existing card workflows unchanged (no regressions)

**Effort**: 1.5-2 weeks
**Risk**: Low (data + UI, no execution-path changes)
**Outcome**: Specs exist as first-class entities. Foundation for tying tests + experiments to intent.

> **OPEN QUESTION:** Should `Feature` belong to a higher-level "Project" or "Workspace" entity (multi-tenant org), or live flat at the install level? Defaulting to flat for now; add Workspace if multi-org need emerges.

---

### Phase 12.2.6: Test Result Tie-Back
**Goal**: Tests in application repos declare a stable identifier; runs flow back into LazyAF and join to acceptance criteria, commits, and (later) experiments.

> **Why now (before 12.3):** This phase defines the test-result manifest format. Phase 12.3's Control Layer needs to know about it so the step→backend protocol can carry test results natively.

#### Test Identifier Convention

The platform supports multiple frameworks via a *manifest convention*. The test runner (pytest, vitest, go test, etc.) emits a JSON file at a known path; the control layer ships it back.

**Manifest path:** `/workspace/.control/test_results.json`

**Manifest schema:**
```json
{
  "schema_version": 1,
  "framework": "pytest",
  "commit_sha": "abc123...",
  "results": [
    {
      "lazyaf_test_id": "auth.revoke_key.returns_401",
      "file_path": "tests/api/test_keys.py",
      "test_name": "test_revoked_key_returns_401",
      "status": "passed",
      "duration_ms": 142,
      "output": null
    }
  ]
}
```

**How tests declare their `lazyaf_test_id`:**

| Framework | Mechanism |
|-----------|-----------|
| pytest | `@lazyaf_test("auth.revoke_key.returns_401")` decorator (ships in a tiny `pytest-lazyaf` plugin) |
| vitest / jest | `lazyaf("auth.revoke_key.returns_401", () => { ... })` wrapper |
| go test | `// lazyaf:auth.revoke_key.returns_401` magic comment above the test func |
| Anything else | `lazyaf.tests.json` sidecar mapping `{file::test_name -> lazyaf_test_id}` |

The platform doesn't care which mechanism is used — it only cares that the manifest is correctly emitted.

#### Tests First (Define Contracts)

**test_manifest_schema.py**
| Test | Defines Contract |
|------|------------------|
| `test_valid_manifest_accepted` | Schema-conformant JSON parses |
| `test_invalid_status_rejected` | Status must be passed/failed/skipped/error |
| `test_unknown_test_id_marked_orphan` | `lazyaf_test_id` not in registry → orphan TestRun + warning |
| `test_missing_commit_sha_rejected` | Commit required for traceability |

**test_result_ingestion.py**
| Test | Defines Contract |
|------|------------------|
| `test_ingest_creates_test_runs` | One TestRun per result entry |
| `test_ingest_idempotent_per_step` | Re-ingesting same step's manifest doesn't duplicate |
| `test_ingest_links_to_step_execution` | TestRun.step_execution_id populated |
| `test_ingest_propagates_experiment_context` | If step is part of experiment, model/prompt fields filled |

**test_reconcile_command.py**
| Test | Defines Contract |
|------|------------------|
| `test_reconcile_creates_missing_test_refs` | New tests in repo auto-registered |
| `test_reconcile_marks_disappeared_orphan` | Test removed from repo → TestRef.is_orphaned = true |
| `test_reconcile_per_repo_scoped` | Reconciliation only affects one repo at a time |

**test_criterion_history_query.py**
| Test | Defines Contract |
|------|------------------|
| `test_history_groups_by_commit` | Query returns chronological pass/fail per criterion |
| `test_history_groups_by_model` | Optional `?model=...` filter |
| `test_history_groups_by_prompt_version` | Optional `?prompt_version=...` filter |

- [ ] Write `test_manifest_schema.py`
- [ ] Write `test_result_ingestion.py`
- [ ] Write `test_reconcile_command.py`
- [ ] Write `test_criterion_history_query.py`

#### Implementation (Make Tests Pass)

- [ ] Add `test_refs` and `test_runs` tables (Alembic migration)
- [ ] `POST /api/test-results/ingest` endpoint
- [ ] `POST /api/test-refs/reconcile` endpoint
- [ ] `GET /api/criteria/{id}/history` endpoint
- [ ] Background reconciliation job (run on every successful pipeline)
- [ ] Reference `pytest-lazyaf` plugin in `runner-common/test_plugins/pytest_lazyaf/`
- [ ] CLI: `lazyaf tests reconcile <repo>` for manual sync
- [ ] UI: criterion view shows "last 20 runs" sparkline (pass-rate over time)

#### Integration Validation

- [ ] `test_full_loop.py`:
  - Create criterion in spec UI
  - Add `@lazyaf_test("...")` decorated test in repo
  - Run pipeline that executes pytest
  - Confirm TestRun appears, criterion history updates

- [ ] `test_orphan_detection.py`:
  - Test exists in two commits
  - Removed in third
  - Reconcile against latest commit marks TestRef orphaned

#### Done Criteria

- [ ] Manifest schema documented + JSON Schema published
- [ ] At least the pytest plugin works end-to-end
- [ ] Criterion history endpoint returns data joinable to commits

**Effort**: 1.5-2 weeks
**Risk**: Medium (cross-language test framework support is a long tail — start with pytest only)
**Outcome**: Test results flow back to LazyAF with full provenance. Criteria gain a measurable definition of done.

> **OPEN QUESTIONS:**
> 1. Should `lazyaf_test_id` be a structured dotted path (`feature.story.criterion.assertion`) or an arbitrary string? Defaulting to arbitrary; users can adopt a convention.
> 2. Are skipped tests informational or do they count against criteria? Defaulting to informational (skipped ≠ failed).

---

### Phase 12.3: Control Layer & Step Images
**Goal**: Proper container communication and base images

#### Tests First (Define Contracts)

**test_control_layer_protocol.py** - Write BEFORE implementing control layer
| Test | Defines Contract |
|------|------------------|
| `test_reads_config_from_control_dir` | Config at `/workspace/.control/step_config.json` |
| `test_reports_status_on_start` | POST to `/api/steps/{id}/status` with `running` |
| `test_reports_status_on_complete` | POST with `completed` and exit code |
| `test_streams_logs_to_backend` | POST to `/api/steps/{id}/logs` |
| `test_heartbeat_during_execution` | POST to `/api/steps/{id}/heartbeat` periodically |
| `test_handles_backend_unavailable` | Retries, eventually fails gracefully |
| `test_uploads_test_results_manifest` | If `/workspace/.control/test_results.json` exists at completion, ship to `/api/test-results/ingest` (see 12.2.6) |
| `test_propagates_experiment_context` | step_config carries experiment_id, model, prompt_template_id, prompt_version → forwarded to result ingest |

**test_step_api_endpoints.py** - Write BEFORE implementing API (backend side)
| Test | Defines Contract |
|------|------------------|
| `test_post_status_updates_step` | Status endpoint updates DB |
| `test_post_logs_appends` | Logs endpoint appends to step logs |
| `test_post_heartbeat_updates_timestamp` | Heartbeat extends timeout |
| `test_auth_required` | Endpoints require step token |

**test_base_image_contract.py** - Write BEFORE building base image
| Test | Defines Contract |
|------|------------------|
| `test_python_available` | `python3 --version` works |
| `test_git_available` | `git --version` works |
| `test_control_layer_at_expected_path` | `/control/run.py` exists |
| `test_entrypoint_is_control_layer` | Default entrypoint runs control layer |

**test_home_persistence.py** - Write BEFORE implementing HOME behavior
| Test | Defines Contract |
|------|------------------|
| `test_home_is_workspace_home` | `$HOME` = `/workspace/home` |
| `test_pip_cache_persists` | pip cache survives step boundary |
| `test_local_bin_persists` | `~/.local/bin` survives step boundary |

- [ ] Write `test_control_layer_protocol.py` (defines control layer contract)
- [ ] Write `test_step_api_endpoints.py` (defines API contract)
- [ ] Write `test_base_image_contract.py` (defines image requirements)
- [ ] Write `test_home_persistence.py` (defines HOME behavior)

#### Implementation (Make Tests Pass)

- [ ] Create control layer script (`/control/run.py`) - make protocol tests pass
  - Reads step config from `/workspace/.control/step_config.json`
  - Reports status to backend (running, completed, failed)
  - Streams logs to backend
  - Heartbeat during long operations
  - On step completion, checks for `/workspace/.control/test_results.json` and ships it to `/api/test-results/ingest` with experiment context (Phase 12.2.6 dependency)
- [ ] Create API endpoints - make endpoint tests pass
  - `POST /api/steps/{step_id}/status`
  - `POST /api/steps/{step_id}/logs`
  - `POST /api/steps/{step_id}/heartbeat`
- [ ] Create base image (`lazyaf-base`) - make image contract tests pass
  - Python 3.12-slim + git + curl + control layer
  - `ENTRYPOINT ["python", "/control/run.py"]`
- [ ] Configure HOME persistence - make persistence tests pass
  - `HOME=/workspace/home`
  - pip/npm/uv caches persist across steps
  - `~/.local/bin` for user-installed tools
- [ ] Create agent images inheriting from base
  - `lazyaf-claude`: base + Claude CLI + agent wrapper
  - `lazyaf-gemini`: base + Gemini CLI + agent wrapper

#### Integration Validation

- [ ] `test_agent_script_agent_pipeline.py`:
  - Agent step installs tool via pip
  - Script step uses that tool
  - Agent step sees script output
- [ ] `test_control_layer_reports_failure.py`:
  - Command exits non-zero
  - Control layer reports failed status
  - Backend marks step failed

#### Done Criteria

- [ ] Control layer protocol tests pass
- [ ] API endpoint tests pass
- [ ] Base image passes contract tests
- [ ] HOME persistence tests pass
- [ ] Cross-step integration test passes

**Effort**: 1-1.5 weeks
**Risk**: Medium
**Outcome**: Steps run in proper containers with backend communication

---

### Phase 12.4: Migrate Script/Docker Steps
**Goal**: All non-agent steps use new architecture

#### Tests First (Define Contracts)

**test_step_routing_contract.py** - Write BEFORE implementing routing
| Test | Defines Contract |
|------|------------------|
| `test_script_step_routes_through_orchestrator` | type=script uses new path |
| `test_docker_step_routes_through_orchestrator` | type=docker uses new path |
| `test_custom_image_respected` | `image: foo:bar` uses that image |

**test_migration_compatibility.py** - Write BEFORE migrating
| Test | Defines Contract |
|------|------------------|
| `test_existing_pipeline_yaml_works` | Old format still executes |
| `test_new_pipeline_yaml_works` | New format with images executes |

- [ ] Write `test_step_routing_contract.py` (defines routing behavior)
- [ ] Write `test_migration_compatibility.py` (defines backward compat)

#### Implementation (Make Tests Pass)

- [ ] Pipeline executor routes script/docker steps through orchestrator
- [ ] Remove `execute_script_step` and `execute_docker_step` from runner entrypoints
- [ ] Steps can specify custom images in pipeline YAML
- [ ] Migrate test-suite.yaml to use pre-built image
- [ ] Create example `lazyaf-test-runner` Dockerfile with uv + deps

#### Integration Validation

- [ ] `test_existing_pipelines_work.py` - Run actual existing pipelines
- [ ] `test_multi_image_pipeline.py` - Different images in same pipeline

#### Done Criteria

- [ ] Routing tests pass
- [ ] Backward compatibility tests pass
- [ ] All existing pipelines pass (regression suite)

**Effort**: 1 week
**Risk**: Medium (migration path)
**Outcome**: Script/docker steps don't need runners

---

### Phase 12.5: Migrate Agent Steps
**Goal**: Agent steps also use new architecture

#### Tests First (Define Contracts)

**test_agent_step_contract.py** - Write BEFORE implementing agent migration
| Test | Defines Contract |
|------|------------------|
| `test_agent_step_spawns_container` | Agent runs in container, not runner |
| `test_agent_wrapper_invokes_cli` | Claude CLI called correctly |
| `test_agent_uses_correct_image` | `lazyaf-claude` image used |

**test_polling_removal.py** - Write BEFORE removing polling
| Test | Defines Contract |
|------|------------------|
| `test_no_runner_polling_calls` | Backend doesn't poll runners |
| `test_runners_not_long_lived` | No persistent runner processes |

- [ ] Write `test_agent_step_contract.py` (defines agent execution)
- [ ] Write `test_polling_removal.py` (defines what's removed)

#### Implementation (Make Tests Pass)

- [ ] Agent steps spawn ephemeral containers via orchestrator
- [ ] Agent wrapper script handles Claude/Gemini CLI invocation
- [ ] Remove old runner polling infrastructure
- [ ] Runners no longer long-lived - spawned per step

#### Integration Validation

- [ ] `test_claude_script_gemini_pipeline.py`:
  - Claude step (container)
  - Script step (container)
  - Gemini step (container)
  - All share workspace

#### Done Criteria

- [ ] Agent step contract tests pass
- [ ] Polling removal verified
- [ ] Cross-agent pipeline works

**Effort**: 1-1.5 weeks
**Risk**: Higher (changes agent execution model)
**Outcome**: All step types use unified architecture

---

### Phase 12.6: RemoteExecutor & Runner State Machine
**Goal**: Millisecond-latency job assignment with proper connection lifecycle

Event-driven architecture - no polling, backend pushes jobs immediately.

#### Tests First (Define Contracts)

**test_runner_state_machine.py** - Write BEFORE implementing runner lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_disconnected_to_connecting` | WebSocket opens |
| `test_connecting_to_idle_on_register` | Registration succeeds |
| `test_idle_to_assigned_on_job` | Job pushed to runner |
| `test_assigned_to_busy_on_ack` | Runner acknowledges |
| `test_busy_to_dead_on_timeout` | Heartbeat missed |
| `test_dead_to_connecting_on_reconnect` | Runner reconnects |

**test_websocket_protocol.py** - Write BEFORE implementing WebSocket
| Test | Defines Contract |
|------|------------------|
| `test_register_message_format` | `{"type": "register", "runner_id": ..., "labels": ...}` |
| `test_execute_step_message_format` | `{"type": "execute_step", ...}` |
| `test_ack_required_within_timeout` | 5s ACK timeout |
| `test_heartbeat_interval` | Heartbeat every 10s |
| `test_death_timeout` | 30s without heartbeat = dead |

**test_remote_executor_contract.py** - Write BEFORE implementing RemoteExecutor
| Test | Defines Contract |
|------|------------------|
| `test_register_runner_stores_in_db` | Runner record created |
| `test_execute_step_pushes_via_websocket` | Job pushed immediately |
| `test_ack_timeout_reassigns` | No ACK = try another runner |
| `test_heartbeat_extends_deadline` | Heartbeat resets death timer |
| `test_death_requeues_step` | Dead runner = step back to pending |

**test_job_recovery.py** - Write BEFORE implementing recovery
| Test | Defines Contract |
|------|------------------|
| `test_runner_dies_mid_job_requeues` | Step re-queued |
| `test_runner_reconnects_resumes` | Same runner picks up |
| `test_reconnect_after_reassign_aborts` | Too late = abort local work |

- [ ] Write `test_runner_state_machine.py` (defines runner lifecycle)
- [ ] Write `test_websocket_protocol.py` (defines protocol contract)
- [ ] Write `test_remote_executor_contract.py` (defines executor interface)
- [ ] Write `test_job_recovery.py` (defines recovery contract)

#### Database Migration

- [ ] Write migration test first:
  ```python
  def test_runners_table_created():
      """Migration creates runners table."""
      # Assert columns: id, name, status, labels, current_step_execution_id, ...
  ```
- [ ] Create `runners` table with Alembic migration
  ```python
  class Runner(Base):
      __tablename__ = "runners"
      id: str  # Client-provided or generated UUID
      name: str
      status: str  # disconnected, connecting, idle, assigned, busy, dead
      runner_type: str  # claude-code, gemini, generic
      labels: JSON  # {"arch": "arm64", "has": ["gpio", "camera"]}
      current_step_execution_id: str | None  # FK to step_executions
      last_heartbeat: datetime
      connected_at: datetime | None
      created_at: datetime
  ```

#### Implementation (Make Tests Pass)

- [ ] Implement Runner state machine (make state tests pass)
- [ ] Remove in-memory `runner_pool.py` dict, query database instead
- [ ] Implement WebSocket protocol (make protocol tests pass)
- [ ] Create `RemoteExecutor` service (make executor tests pass)
- [ ] WebSocket endpoint for runner connections (`/ws/runner`)
  - Registration with auth timeout (10s)
  - ACK required for job assignment (5s timeout)
  - Heartbeat monitoring (30s death timeout)
  - Graceful drain for shutdown
- [ ] Implement job recovery (make recovery tests pass)
- [ ] Reconnection handling
  - Same runner_id can reconnect after death
  - Rejects duplicate connections from same runner_id
- [ ] Create `runner-agent` package (runs on target machines)
  - Connects to backend via WebSocket (NAT-friendly)
  - Sends ACK on job receipt
  - Heartbeat thread during execution
  - Auto-reconnect on disconnect
- [ ] `NativeOrchestrator` for embedded devices
  - Runs steps directly on host (no Docker)
  - Git-based workspace sync

#### Integration Validation

- [ ] `test_remote_runner_full_flow.py`:
  - Start runner agent
  - Push job via backend
  - Runner executes
  - Runner reports completion
- [ ] `test_runner_failover.py`:
  - Two runners connected
  - Kill one mid-job
  - Other picks up

#### Chaos Tests (Critical for this phase)

- [ ] `test_runner_disconnect_mid_job.py` - Network partition
- [ ] `test_all_runners_disconnect.py` - Total failure
- [ ] `test_runner_reconnect_race.py` - Reconnect vs reassign race

#### Done Criteria

- [ ] Runner state machine tests pass
- [ ] WebSocket protocol tests pass
- [ ] RemoteExecutor contract tests pass
- [ ] Job recovery tests pass
- [ ] Chaos tests pass

**Effort**: 2 weeks
**Risk**: Medium-High
**Outcome**: Robust remote execution with proper failure handling

**Example runner deployment:**
```bash
# On Raspberry Pi
export LAZYAF_BACKEND_URL="http://192.168.1.100:8000"
export LAZYAF_RUNNER_ID="pi-workshop-1"
export LAZYAF_LABELS="arch=arm64,has=gpio,has=camera"
export LAZYAF_ORCHESTRATOR="native"

python -m lazyaf_runner  # Connects via WebSocket, receives jobs immediately
```

**WebSocket Protocol:**
```
Runner -> Backend: {"type": "register", "runner_id": "...", "labels": {...}}
Backend -> Runner: {"type": "execute_step", "step_id": "...", "image": "...", ...}
Runner -> Backend: {"type": "log", "step_id": "...", "line": "..."}
Runner -> Backend: {"type": "step_complete", "step_id": "...", "exit_code": 0}
```

---

### Phase 12.6.5: Experiments & Model/Prompt Leaderboards
**Goal**: Run the same target (card / story / feature) across a matrix of (model, prompt_template, prompt_version), aggregate TestRuns, surface a leaderboard. Turn LazyAF into a platform for *software science*.

> **Why now (after 12.6):** Once remote execution is stable, fan-out to many parallel runs becomes cheap. Experiments are the high-value workload that demand fan-out.

#### Tests First (Define Contracts)

**test_experiment_lifecycle.py**
| Test | Defines Contract |
|------|------------------|
| `test_create_experiment_validates_matrix` | Matrix must specify at least one model and one prompt |
| `test_launch_creates_pipeline_run_per_cell` | NxM matrix + repeat=R → N*M*R pipeline runs |
| `test_experiment_completes_when_all_runs_terminal` | Status flips to `complete` after last run lands |
| `test_abort_cancels_pending_runs` | Abort cancels queued runs, leaves running ones to finish |

**test_experiment_run_tagging.py**
| Test | Defines Contract |
|------|------------------|
| `test_pipeline_run_carries_experiment_id` | `pipeline_runs.experiment_id` populated |
| `test_test_runs_inherit_matrix_coords` | Each TestRun tagged with model + prompt info |
| `test_step_config_includes_matrix_cell` | Runner sees model+prompt env vars for agent invocation |

**test_leaderboard_aggregation.py**
| Test | Defines Contract |
|------|------------------|
| `test_leaderboard_groups_by_prompt_and_model` | One row per (prompt_template, version, model) |
| `test_leaderboard_per_criterion_pass_rate` | Pass-rate = passed / (passed + failed) |
| `test_leaderboard_filters_skipped` | Skipped tests excluded from rate denominator |
| `test_leaderboard_handles_zero_runs` | Cell with no runs shown as N/A, not 0% |

- [ ] Write `test_experiment_lifecycle.py`
- [ ] Write `test_experiment_run_tagging.py`
- [ ] Write `test_leaderboard_aggregation.py`

#### Implementation (Make Tests Pass)

- [ ] `experiments` and `experiment_runs` tables (Alembic migration)
- [ ] `ExperimentService.launch()` fans out to ExecutionRouter, one pipeline run per matrix cell
- [ ] Extend `step_config` schema with `experiment_context: {experiment_id, model, prompt_template_id, prompt_version}`
- [ ] Agent executors (Claude/Gemini) read `model` from step_config to override default
- [ ] Prompt rendering: `PromptVersion.body` is rendered with `{story_narrative}` etc. resolved from spec layer at experiment-launch time
- [ ] Aggregation queries (criterion pass-rate per matrix cell)
- [ ] UI: `/experiments` route — create + monitor experiments, launch from a card/story/feature
- [ ] UI: leaderboard view per Feature — sortable matrix
- [ ] MCP tool: `launch_experiment` so Claude Desktop can drive evaluations

#### Integration Validation

- [ ] `test_2x2_experiment_e2e.py`:
  - 2 models × 2 prompts × 1 repeat = 4 pipeline runs
  - All complete
  - Leaderboard renders with 4 rows
  - At least one row has pass-rate > 0%

- [ ] `test_experiment_with_failing_prompt.py`:
  - One prompt is intentionally bad
  - Leaderboard correctly ranks it last

#### Done Criteria

- [ ] Experiment lifecycle tests pass
- [ ] Pass-rates correctly computed per cell
- [ ] User can launch a 2x2 experiment from the UI in under 30s

**Effort**: 2-3 weeks
**Risk**: Medium (fan-out cost — need quotas / cost guardrails)
**Outcome**: Evidence-driven model + prompt selection. Regression dashboard per feature.

> **OPEN QUESTIONS:**
> 1. Cost guardrails — a 5x5x3 experiment is 75 agent runs. Do we need per-experiment budget caps + dry-run estimates before launch?
> 2. Should leaderboards be public (anyone in the org sees them) or private to the experiment creator? Defaulting to org-visible.

---

### Phase 12.6.6: Spec-Curated Agent Context
**Goal**: When an agent runs against a card, the platform automatically curates the relevant slice of the spec layer (linked feature, story, criteria, related TestRefs) and injects it into the agent's prompt — instead of relying on the agent to discover intent from the codebase.

> **Why now (after 12.6.5):** Experiments will reveal that prompt content matters more than model choice for many tasks. Spec-curated context is the single biggest lever on prompt quality.

#### The Context Curation Problem

A card linked to a UserStory has natural context: the story narrative, all acceptance criteria, related TestRefs (with file paths!), and the parent Feature's description. Without curation, agents either:
- Get the whole repo dumped in (wastes context, distracts the model), or
- Get only the card title + description (misses critical intent)

Curation gives the agent: *"Here's the story you're delivering. Here are the criteria you must satisfy. Here are the existing tests that already cover related criteria — read them, don't duplicate them."*

#### Tests First (Define Contracts)

**test_context_bundle_assembly.py**
| Test | Defines Contract |
|------|------------------|
| `test_card_with_story_link_pulls_narrative` | Bundle includes full story narrative |
| `test_bundle_includes_all_criteria` | All criteria for the linked story present |
| `test_bundle_includes_related_test_paths` | TestRef file paths surfaced (so agent can read them) |
| `test_bundle_includes_parent_feature_description` | Feature context included |
| `test_bundle_omits_unrelated_features` | No leakage from sibling features |
| `test_bundle_handles_card_without_links` | Falls back to card-only context, no error |
| `test_bundle_size_capped` | Truncates with summary if total > N tokens |

**test_context_injection.py**
| Test | Defines Contract |
|------|------------------|
| `test_bundle_written_to_workspace` | Available at `/workspace/.control/spec_context.md` |
| `test_executor_includes_in_prompt` | Claude/Gemini wrappers prepend spec_context.md to system prompt |
| `test_prompt_template_can_reference` | `{spec_context}` placeholder resolves |

- [ ] Write `test_context_bundle_assembly.py`
- [ ] Write `test_context_injection.py`

#### Implementation (Make Tests Pass)

- [ ] `SpecContextService.build_bundle(card_id) -> str` — assembles markdown
- [ ] Pipeline executor writes bundle to workspace before agent step
- [ ] Update Claude/Gemini executor wrappers to read and prepend
- [ ] Token-budget aware truncation (summarize if oversized)
- [ ] Add `{spec_context}` placeholder support to PromptTemplate rendering

#### Integration Validation

- [ ] `test_agent_uses_curated_context.py`:
  - Card linked to story with 3 criteria + 2 existing TestRefs
  - Agent run completes
  - Logs show agent referenced criteria by name (heuristic check)
  - Diff includes new test that satisfies a criterion

#### Done Criteria

- [ ] Context bundle tests pass
- [ ] Bundle injection works for both Claude and Gemini executors
- [ ] At least one before/after experiment shows improvement on linked-card pass-rate

**Effort**: 1.5-2 weeks
**Risk**: Medium (token-budget tuning is iterative)
**Outcome**: Parallel agents stay coherent because each one gets a precisely-scoped slice of intent. Reduces context-window pressure as the system scales.

> **OPEN QUESTION:** Should the bundle include actual *source code* from related TestRefs (full file content) or just file paths? Defaulting to paths — agent can choose to read what it needs.

---

### Phase 12.7: Debug Re-Run Mode
**Goal**: Re-run failed pipelines with breakpoints for interactive debugging

The primary use case: someone points you at a failed pipeline and you need to figure out what went wrong. Debug mode lets you re-run with breakpoints, inspect state, and iterate.

#### Tests First (Define Contracts)

**test_debug_session_state_machine.py** - Write BEFORE implementing debug lifecycle
| Test | Defines Contract |
|------|------------------|
| `test_pending_to_waiting_on_breakpoint` | Breakpoint hit = waiting |
| `test_waiting_to_connected_on_join` | CLI connects = connected |
| `test_connected_to_ended_on_resume` | Resume = continue |
| `test_timeout_from_waiting` | No connect = timeout |
| `test_timeout_from_connected` | Idle too long = timeout |

**test_debug_api_contract.py** - Write BEFORE implementing API endpoints
| Test | Defines Contract |
|------|------------------|
| `test_create_debug_rerun_returns_session` | POST returns session ID |
| `test_get_debug_session_returns_info` | GET returns commit, runtime, logs |
| `test_resume_continues_pipeline` | POST resume = pipeline continues |
| `test_abort_cancels_pipeline` | POST abort = pipeline cancelled |

**test_breakpoint_execution.py** - Write BEFORE implementing breakpoint behavior
| Test | Defines Contract |
|------|------------------|
| `test_pipeline_pauses_at_breakpoint` | Execution stops |
| `test_workspace_preserved_at_breakpoint` | Files accessible |
| `test_multiple_breakpoints_work` | Can set many breakpoints |

**test_terminal_connection.py** - Write BEFORE implementing terminal
| Test | Defines Contract |
|------|------------------|
| `test_sidecar_mode_spawns_container` | Sidecar container created |
| `test_shell_mode_execs_into_running` | Exec into step container |
| `test_special_commands_work` | @resume, @abort, @status |
| `test_token_required` | Auth enforced |

- [ ] Write `test_debug_session_state_machine.py` (defines debug lifecycle)
- [ ] Write `test_debug_api_contract.py` (defines API contract)
- [ ] Write `test_breakpoint_execution.py` (defines breakpoint behavior)
- [ ] Write `test_terminal_connection.py` (defines terminal protocol)

#### Debug Re-Run Workflow

```
1. User sees failed pipeline → clicks "Debug Re-run"
2. Modal shows:
   - Step list with checkboxes for breakpoints (dynamic, not YAML)
   - Commit selection: "Same as failure (abc123)" OR "Different branch/commit"
3. User starts debug run
4. Pipeline executes until breakpoint
5. UI shows rich context + CLI join command
6. User connects via CLI, inspects, continues or aborts
7. Repeat until done or pipeline completes
```

#### UI at Breakpoint

When a breakpoint is hit, the UI displays:

| Field | Description |
|-------|-------------|
| **Current Commit** | SHA + message of commit being tested |
| **Runtime Info** | Host, orchestrator type, container image, image SHA |
| **Step Info** | Current step name, index, type |
| **Logs** | Full job/pipeline logs up to this point |
| **Join Command** | Pre-populated CLI command to copy/paste |
| **Controls** | Resume, Abort buttons |

#### Two Connection Modes

The CLI supports two ways to connect, depending on what you need:

**1. Sidecar Mode** (inspect filesystem only)
```bash
lazyaf debug <session-id> --sidecar
```
- Spawns a debug sidecar container with workspace volume mounted
- Read-only inspection of checkout, build artifacts, logs
- Useful when step container has exited or you just need to look at files
- Full shell with common tools (vim, git, htop, etc.)

**2. Live Shell Mode** (process in running container)
```bash
lazyaf debug <session-id> --shell
```
- Creates a new process inside the current step container
- Access to full runtime environment (same image, env vars, installed packages)
- Can run the same commands the step would run
- Only available when step container is still running (at breakpoint)

Both modes use WebSocket transport (not SSH) for simplicity.

#### CLI Commands

```bash
# Connect to debug session (default: sidecar if container stopped, shell if running)
lazyaf debug <session-id> --token <token>

# Explicit mode selection
lazyaf debug <session-id> --sidecar --token <token>
lazyaf debug <session-id> --shell --token <token>

# Control commands (from within debug shell or separately)
lazyaf debug <session-id> --resume      # Continue to next breakpoint
lazyaf debug <session-id> --abort       # Cancel the debug run
lazyaf debug <session-id> --status      # Show current state
```

Inside a debug shell, special commands:
```
@resume    # Continue pipeline (alias for --resume)
@abort     # Cancel debug run (alias for --abort)
@status    # Show breakpoint info
@help      # List available commands
```

#### API Endpoints

```
# Start debug re-run from failed pipeline
POST /api/pipeline-runs/{id}/debug-rerun
  Body: {
    breakpoints: ["step-id-1", "step-id-2"],  # Steps to break before
    use_original_commit: bool,                 # true = same commit as failure
    commit_sha: string | null,                 # if use_original_commit=false
    branch: string | null                      # if use_original_commit=false
  }
  Returns: { run_id, debug_session_id }

# Get debug session info (for UI display)
GET /api/debug/{session_id}
  Returns: {
    status: "waiting" | "connected" | "timeout" | "ended",
    current_step: { name, index, type },
    commit: { sha, message },
    runtime: { host, orchestrator, image, image_sha },
    logs: string,
    join_command: string,
    token: string
  }

# Control debug session
POST /api/debug/{session_id}/resume     # Continue to next breakpoint
POST /api/debug/{session_id}/abort      # Cancel debug run
POST /api/debug/{session_id}/extend     # Extend timeout

# WebSocket endpoint for terminal
WS /api/debug/{session_id}/terminal?mode=sidecar|shell&token=<token>
```

#### Debug Session States

```
[pending] --> [waiting_at_bp] --> [connected] --> [ended]
                    |                   |
                    | timeout           | timeout/disconnect
                    v                   v
               [timeout]           [timeout]
```

| State | Description |
|-------|-------------|
| `pending` | Debug run started, executing before first breakpoint |
| `waiting_at_bp` | At breakpoint, waiting for user to connect |
| `connected` | User connected via CLI |
| `timeout` | Session timed out (default 1hr, max 4hr) |
| `ended` | User resumed/aborted, or pipeline completed |

#### Pipeline Run States (Extended)

| State | Description |
|-------|-------------|
| `debug_pending` | Debug re-run created, not yet started |
| `debug_running` | Executing between breakpoints |
| `debug_waiting` | At breakpoint, waiting for user |
| `debug_connected` | User connected, inspecting |

#### Sidecar Container

```dockerfile
FROM ubuntu:22.04
RUN apt-get update && apt-get install -y \
    vim nano less \
    git curl wget \
    htop tree jq \
    python3 python3-pip \
    && rm -rf /var/lib/apt/lists/*

# WebSocket terminal server
COPY debug-terminal-server /usr/local/bin/
ENTRYPOINT ["debug-terminal-server"]
```

Mounts workspace volume at `/workspace`. Lightweight, starts fast.

#### Security

- **One-time tokens**: Generated per debug session, single use
- **Session timeout**: Default 1 hour, max 4 hours, extendable
- **Token expiry**: Tokens expire with session
- **Resource limits**: Debug containers have CPU/memory limits
- **No SSH**: WebSocket only, simpler attack surface
- **Future**: Integrate with auth system when available

#### Implementation Phases

**Phase 12.7a: Core Debug Re-Run (MVP)**
- [ ] `POST /api/pipeline-runs/{id}/debug-rerun` endpoint
- [ ] `DebugSession` model and service
- [ ] Pipeline executor honors breakpoints, pauses execution
- [ ] `GET /api/debug/{session_id}` for session info
- [ ] Resume/abort endpoints
- [ ] UI: "Debug Re-run" button on failed pipelines
- [ ] UI: Breakpoint selector modal
- [ ] UI: Commit selector (original vs custom)
- [ ] UI: Debug panel showing context when at breakpoint

**Phase 12.7b: CLI Connection**
- [ ] `lazyaf debug` command structure
- [ ] WebSocket terminal client in CLI
- [ ] Sidecar mode: spawn debug container, connect
- [ ] Shell mode: exec into running container
- [ ] Special commands (@resume, @abort, @status)
- [ ] Token-based authentication

**Phase 12.7c: Polish**
- [ ] Session timeout management
- [ ] Reconnection handling (resume interrupted session)
- [ ] UI improvements (better log display, status indicators)
- [ ] `--extend` to add time to session
- [ ] Cleanup: remove debug containers on session end

#### Integration Validation

- [ ] `test_e2e_debug_workflow.py`:
  - Pipeline fails
  - Create debug re-run with breakpoint
  - Connect via CLI
  - Inspect workspace
  - Resume
  - Pipeline completes

#### Done Criteria

- [ ] Debug session state machine tests pass
- [ ] API contract tests pass
- [ ] Breakpoint execution tests pass
- [ ] Terminal connection tests pass
- [ ] E2E workflow test passes

**Effort**: 2-3 weeks
**Risk**: Medium
**Outcome**: Operators can re-run failed pipelines with breakpoints and connect via CLI to debug

---

### Phase 12.8: Cleanup & Polish
**Goal**: Remove legacy code, document new model

#### Tests First (Regression Focus)

**test_no_legacy_code.py** - Verify removal is complete
| Test | Validates |
|------|-----------|
| `test_old_entrypoints_removed` | Files don't exist |
| `test_runner_pool_removed` | No polling infrastructure |
| `test_no_docker_in_docker` | No socket mounting in runners |

**test_full_regression_suite.py** - Everything still works

*Pipeline Execution Paths*
| Test | Validates |
|------|-----------|
| `test_pipeline_with_single_step_completes` | Minimal pipeline executes end-to-end |
| `test_pipeline_with_multiple_steps_sequential` | Steps execute in order |
| `test_pipeline_on_success_next_continues` | `on_success: next` advances to next step |
| `test_pipeline_on_success_stop_completes` | `on_success: stop` ends pipeline with passed status |
| `test_pipeline_on_failure_stop_halts` | `on_failure: stop` ends pipeline with failed status |
| `test_pipeline_on_failure_next_continues` | `on_failure: next` continues despite step failure |
| `test_pipeline_cancel_stops_execution` | Cancel marks run cancelled, stops steps |

*Step Type Variations*
| Test | Validates |
|------|-----------|
| `test_script_step_executes_command` | `type: script` runs shell command |
| `test_docker_step_uses_specified_image` | `type: docker` pulls and runs in specified image |
| `test_agent_step_invokes_ai_runner` | `type: agent` dispatches to Claude/Gemini runner |
| `test_step_timeout_enforced` | Step exceeding timeout is killed |
| `test_step_config_passed_to_executor` | step_config JSON reaches executor |

*Executor Modes*
| Test | Validates |
|------|-----------|
| `test_local_executor_spawns_container` | LocalExecutor creates Docker container |
| `test_remote_executor_pushes_via_websocket` | RemoteExecutor sends job over WebSocket |
| `test_execution_router_selects_correct_executor` | Router picks Local vs Remote based on requirements |

*Workspace Continuity*
| Test | Validates |
|------|-----------|
| `test_continue_in_context_preserves_workspace` | `continue_in_context: true` keeps files |
| `test_is_continuation_skips_cleanup` | Continuation step does not reset workspace |
| `test_previous_step_logs_passed_to_next` | Agent sees previous step output |
| `test_different_images_share_workspace` | Step 1 in golang, Step 2 in python, both see files |

*Trigger Mechanisms*
| Test | Validates |
|------|-----------|
| `test_card_complete_trigger_fires` | Card -> done triggers pipeline |
| `test_push_trigger_on_branch_match` | Push to matching branch triggers |
| `test_trigger_disabled_does_not_fire` | enabled: false suppresses trigger |

*WebSocket Broadcasts*
| Test | Validates |
|------|-----------|
| `test_pipeline_run_status_broadcast` | pipeline_run_status event sent |
| `test_step_run_status_broadcast` | step_run_status event sent |
| `test_card_updated_broadcast` | card_updated on status change |

*Error Handling*
| Test | Validates |
|------|-----------|
| `test_step_failure_captured_in_error_field` | Failed step has error message |
| `test_job_failure_updates_card_status` | Failed job -> card status = failed |
| `test_tests_failed_marks_card_failed` | tests_passed=false -> card failed |
| `test_runner_death_requeues_step` | Runner dies -> step returns to pending |

*Recovery Scenarios*
| Test | Validates |
|------|-----------|
| `test_backend_restart_resumes_pipelines` | Running pipelines resume after restart |
| `test_orphan_containers_cleaned_on_startup` | Stale containers killed |
| `test_orphan_steps_marked_failed` | Abandoned steps get failed status |

- [ ] Write `test_no_legacy_code.py` (verifies cleanup)
- [ ] Write `test_full_regression_suite.py` (validates everything works - 30+ tests above)

#### Implementation

- [ ] Remove old runner entrypoints (archive for reference)
- [ ] Update docker-compose for new architecture
- [ ] Remove `runner_pool.py` polling infrastructure
- [ ] Documentation: runner deployment, custom images, step requirements
- [ ] Example Dockerfiles for common step images

#### Done Criteria

- [ ] Legacy removal tests pass
- [ ] Full regression suite passes
- [ ] Documentation reviewed

**Effort**: 1 week
**Outcome**: Clean, documented system

---

### Phase 12.9: Kubernetes Orchestrator (Future)
**Goal**: Same code works on Kubernetes

#### Tests First (Define Contracts)

**test_k8s_orchestrator_contract.py** - Write BEFORE implementing K8s
| Test | Defines Contract |
|------|------------------|
| `test_creates_k8s_job_for_step` | Job resource created |
| `test_uses_pvc_for_workspace` | PVC mounted |
| `test_node_selector_from_labels` | Labels -> node selector |
| `test_job_completion_detected` | Job status watched |

- [ ] Write `test_k8s_orchestrator_contract.py` (defines K8s behavior)

#### Implementation (Make Tests Pass)

- [ ] Implement `KubernetesOrchestrator` (make tests pass)
- [ ] PersistentVolumeClaims for workspaces
- [ ] K8s Jobs for step execution
- [ ] Node selectors based on runner labels
- [ ] Integration tests in K8s environment

#### Done Criteria

- [ ] K8s orchestrator tests pass (mocked)
- [ ] Integration tests pass (real K8s)

**Effort**: 2-3 weeks when needed
**Outcome**: Production-ready K8s deployment