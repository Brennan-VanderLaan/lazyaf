# Phase 9: Pipelines (including 9.1 Polish)

> **Status**: COMPLETE
> **Goal**: Chain steps with conditional logic into reusable workflows

## Problem Being Solved

Want multi-step workflows: lint -> test -> build, with AI fixes on failure

---

## Phase 9: Core Pipelines

### MVP Scope

- [x] Pipeline entity with ordered steps
- [x] Steps can be inline (script/docker) or reference cards (agent)
- [x] Success/failure branching per step:
  - `next` - continue to next step
  - `stop` - halt pipeline (success or failure based on step result)
  - `trigger:{card_id}` - spawn AI agent card to fix issues
  - `trigger:pipeline:{pipeline_id}` - Run target pipeline
  - `merge:{branch}` - auto-merge on success
- [x] Extend Script step to allow for multi-line bash scripts
- [x] Pipeline execution engine (runs steps sequentially)
- [x] Pipeline run tracking (status, current step, logs per step)
- [x] UI: Pipeline builder/editor
- [x] UI: Pipeline runs viewer with step-by-step status
- [x] MCP tools: `create_pipeline`, `run_pipeline`, `get_pipeline_run`

### Example Pipeline

```json
{
  "name": "PR Validation",
  "steps": [
    {"name": "Lint", "type": "script", "config": {"command": "npm run lint"}, "on_failure": "trigger:ai-fix-lint"},
    {"name": "Test", "type": "script", "config": {"command": "npm test"}, "on_failure": "trigger:ai-fix-tests"},
    {"name": "Build", "type": "docker", "config": {"image": "node:20", "command": "npm run build"}, "on_failure": "stop"}
  ]
}
```

### Explicit Non-Goals (This Phase)

- Parallel step execution
- Webhook triggers
- Scheduled runs

### Deliverable

Can define and run multi-step pipelines with AI fallback on failures

---

## Phase 9.1: Pipeline Polish

### 9.1a: Context Sharing Clarity (COMPLETE)

- [x] `continue_in_context` flag on steps (workspace preserved)
- [x] `is_continuation` flag (skip clone on subsequent steps)
- [x] `previous_step_logs` passed to agent steps
- [x] Document workspace behavior in UI (tooltip on checkbox)
- [x] Log clearly what context each step receives

### 9.1b: Pipeline & Agent File Persistence (COMPLETE)

> **Goal**: Store pipelines AND agents in repo as `.lazyaf/` directory

#### Directory Structure

```
.lazyaf/
|-- pipelines/
|   |-- test-suite.yaml       # One pipeline per file
|   |-- deploy.yaml
|   +-- nightly-build.yaml
+-- agents/
    |-- test-fixer.yaml       # One agent per file
    |-- code-reviewer.yaml
    +-- unity-specialist.yaml
```

#### Git-Native Behavior

The `.lazyaf/` directory is **live from the repo**, not a one-time import:
- [x] UI reads `.lazyaf/` from HEAD of selected branch (via git server)
- [x] Pipelines/agents update automatically when branch HEAD changes
- [x] External commits (manual edits, other tools) reflected in UI immediately
- [x] No CLI export/import commands - just edit files in repo directly
- [x] Repo IS the source of truth (no DB copies of repo-defined items)

#### Lookup Precedence

When resolving agent references (e.g., `agent: "test-fixer"`):
1. Check `.lazyaf/agents/test-fixer.yaml` in repo (working branch)
2. Fall back to platform-defined agent if not found in repo

### 9.1c: Pipeline/Card Agent Parity (COMPLETE)

- [x] Add `agent_file_ids` to pipeline step config (select agents)
- [x] Add `prompt_template` to pipeline step config
- [x] Update PipelineEditor to show agent selector for agent steps
- [x] Update pipeline executor to pass agent files to job

### 9.1d: Pipeline Context Directory (COMPLETE)

> **Goal**: Enable chain-of-thought across pipeline steps via committed context

#### Implementation

- [x] Pipeline executor creates `.lazyaf-context/` on first step
- [x] Step logs written with naming based on step ID (if set) or index
- [x] Metadata written to `.lazyaf-context/metadata.json`
- [x] Context directory committed after each step
- [x] Merge action adds cleanup commit (removes `.lazyaf-context/`)
- [x] Users can squash-merge to keep upstream history clean

#### Log File Naming

- With ID: `.lazyaf-context/id_<id>_NNN.log` (e.g., `id_tests_001.log`)
- Without ID: `.lazyaf-context/step_NNN_<name>.log` (e.g., `step_002_quick_lint.log`)

#### Directory Structure (committed to feature branch)

```
.lazyaf-context/
|-- id_tests_001.log               # Step with id="tests" (stable reference)
|-- step_002_quick_lint.log        # Step without ID (index-based)
|-- id_analyze_003.log             # Step with id="analyze"
|-- id_analyze_003.md              # Agent's notes (optional, agent-written)
|-- test_results.json              # Structured test data (if available)
|-- decisions.md                   # Accumulated decisions/learnings (agents can append)
+-- metadata.json                  # {pipeline_run_id, steps_completed, step_id_map, ...}
```

#### Key Behaviors

- Agents can READ from context (previous step logs, notes from earlier agents)
- Agents can WRITE to context (leave notes, analysis, decisions for future steps)
- Context accumulates across commits on the feature branch
- **On merge**: pipeline adds a final commit that removes `.lazyaf-context/`
- **User choice**: squash-merge to collapse all commits (including cleanup) into one clean commit
