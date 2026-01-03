# Phase 7: MCP Interface

> **Status**: COMPLETE
> **Goal**: Allow LLM clients (Claude Desktop, etc.) to orchestrate LazyAF via Model Context Protocol

## Problem Being Solved

Want to interact with LazyAF from chat interfaces - "create a card for adding dark mode" without opening the UI.

## MVP Scope (31 tools total)

### Core Tools
- [x] `list_repos` - returns repo names, IDs, and ingest status
- [x] `list_cards` - returns cards for a repo with status, branch, PR info
- [x] `create_card` - creates a card given repo_id, title, description
- [x] `get_card` - returns full card details including job logs
- [x] `start_card` - triggers agent work on a card
- [x] `get_job_logs` - fetch logs from a job run
- [x] `get_runner_status` - check runner pool availability

### Card Actions
- [x] `approve_card` - approve and merge a card
- [x] `reject_card` - reject card back to todo
- [x] `retry_card` - retry a failed card
- [x] `update_card` - update card details
- [x] `delete_card` - delete a card

### Pipeline Tools (with trigger support)
- [x] `list_pipelines` - list pipelines for a repo
- [x] `create_pipeline` - create pipeline with steps and triggers
- [x] `get_pipeline` - get pipeline details
- [x] `update_pipeline` - update pipeline including triggers
- [x] `delete_pipeline` - delete a pipeline
- [x] `run_pipeline` - trigger a pipeline run
- [x] `get_pipeline_run` - get run status with step details
- [x] `list_pipeline_runs` - list runs with filters
- [x] `cancel_pipeline_run` - cancel a running pipeline
- [x] `get_step_logs` - get logs for a specific step

### Agent Files
- [x] `list_agent_files` - list platform agent files
- [x] `get_agent_file` - get agent file content
- [x] `create_agent_file` - create new agent file
- [x] `update_agent_file` - update agent file
- [x] `delete_agent_file` - delete agent file

### Git/Branch Tools
- [x] `list_branches` - list repo branches
- [x] `get_diff` - get diff between branches

### Repo-Defined Assets
- [x] `list_repo_agents` - list .lazyaf/agents/
- [x] `list_repo_pipelines` - list .lazyaf/pipelines/

### Resource
- `repos://list` - formatted repo list

## Explicit Non-Goals (This Phase)

- HTTP/SSE transport (stdio only for MVP)
- Runner management via MCP
- Complex auth (local tool assumption)

## Key Files

```
backend/
+-- mcp/
    |-- __init__.py
    |-- __main__.py       # Entry point for stdio transport
    +-- server.py         # MCP server implementation (all tools)
```

## Tech Stack

- `mcp` Python package with FastMCP for server implementation
- stdio transport for Claude Desktop compatibility
- Thin wrapper around existing FastAPI services

## Deliverable

Full LazyAF orchestration from Claude Desktop - create cards, manage pipelines, configure triggers, review and approve work
