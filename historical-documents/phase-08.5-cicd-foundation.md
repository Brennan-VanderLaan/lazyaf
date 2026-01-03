# Phase 8.5: CI/CD Foundation (Non-AI Steps)

> **Status**: COMPLETE
> **Goal**: Runners can execute scripts and docker commands, not just AI agents

## Problem Being Solved

Currently all "work" must go through an AI agent. Want deterministic CI steps (lint, test, build) that run reliably every time.

## MVP Scope

- [x] Add `step_type` enum: `agent | script | docker`
- [x] Extend Card and Job models with step type and config fields
- [x] Update runner entrypoint to handle all three step types
- [x] Script steps: run shell command in cloned repo, capture output
- [x] Docker steps: run command in specified container image, capture output
- [x] UI: Cards can specify step type with config inputs (command, image, working_dir)
- [x] MCP: `create_card` accepts `step_type`, `command`, `image`, `working_dir` parameters

## Step Type Implementations

```python
# AGENT (existing behavior)
job.step_type = "agent"
job.config = {"runner_type": "claude-code"}
# Runner invokes Claude Code CLI

# SCRIPT (new)
job.step_type = "script"
job.config = {"command": "npm run lint && npm test"}
# Runner executes command directly, captures stdout/stderr

# DOCKER (new)
job.step_type = "docker"
job.config = {"image": "node:20", "command": "npm run build"}
# Runner pulls image, runs command in container, captures output
```

## Explicit Non-Goals (This Phase)

- Pipeline orchestration (just individual steps)
- Webhooks
- Step chaining

## Deliverable

Can create a card with step_type=script or docker that runs commands directly without AI. UI has step type selector with config inputs. MCP create_card supports all step types.

---

## CI/CD Pipeline System Architecture

> **Vision**: Replace GitHub Actions with a local-first, controllable CI/CD system that seamlessly integrates AI agents as just another step type. Fast, testable locally, no YAML maze.

### Architecture Overview

```
+-----------------------------------------------------------------------------+
|                            PIPELINE SYSTEM                                   |
|                                                                              |
|  +-------------+    +-------------+    +-------------+    +-------------+   |
|  |   TRIGGER   | -> |  PIPELINE   | -> |    STEPS    | -> |   RESULTS   |   |
|  |             |    |             |    |             |    |             |   |
|  | - Manual    |    | - Ordered   |    | - Agent     |    | - Pass/Fail |   |
|  | - Webhook   |    | - Branching |    | - Script    |    | - Logs      |   |
|  | - Git Push  |    | - Parallel  |    | - Docker    |    | - Artifacts |   |
|  | - Schedule  |    |   (future)  |    |             |    |             |   |
|  +-------------+    +-------------+    +-------------+    +-------------+   |
|                                                                              |
|  Step Types:                                                                 |
|  +-------------------------------------------------------------------------+ |
|  |  AGENT          |  SCRIPT           |  DOCKER                          | |
|  |  Claude/Gemini  |  Shell commands   |  Command in specified image      | |
|  |  implements     |  lint, test,      |  isolated builds, custom         | |
|  |  features       |  coverage, build  |  environments                    | |
|  +-------------------------------------------------------------------------+ |
+-----------------------------------------------------------------------------+
```

### New Data Models

```python
class StepType(Enum):
    AGENT = "agent"      # Current Claude/Gemini behavior
    SCRIPT = "script"    # Shell command execution
    DOCKER = "docker"    # Command in specified container image

class Pipeline:
    id: UUID
    repo_id: UUID
    name: str
    description: str | None
    steps: list[PipelineStep]  # JSON - ordered steps
    is_template: bool          # Can be reused across repos
    created_at: datetime
    updated_at: datetime

class PipelineStep:
    name: str
    type: StepType
    config: dict               # Type-specific config
    on_success: str            # "next" | "stop" | "trigger:{card_id}" | "merge:{branch}"
    on_failure: str            # "next" | "stop" | "trigger:{card_id}"
    timeout: int               # Seconds, default 300

class PipelineRun:
    id: UUID
    pipeline_id: UUID
    status: RunStatus          # pending/running/passed/failed/cancelled
    trigger_type: str          # "manual" | "webhook" | "card" | "push" | "schedule"
    trigger_ref: str | None    # webhook_id, card_id, branch, etc.
    current_step: int
    steps_completed: int
    steps_total: int
    started_at: datetime
    completed_at: datetime | None

class StepRun:
    id: UUID
    pipeline_run_id: UUID
    step_index: int
    status: RunStatus
    logs: str
    error: str | None
    started_at: datetime
    completed_at: datetime | None
```
