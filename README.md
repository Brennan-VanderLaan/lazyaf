# LazyAF

Visual orchestrator for AI coding agents. Point it at a repo, describe what you want, watch it work.

**[Get started in 2 minutes →](QUICKSTART.md)**

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Frontend  │────▶│   Backend   │────▶│   Runners   │
│   (Svelte)  │     │  (FastAPI)  │     │ Claude/Gemini│
└─────────────┘     └──────┬──────┘     └─────────────┘
                          │
                    ┌─────▼─────┐
                    │ Git Server │
                    │ (internal) │
                    └───────────┘
```

- **Backend** - REST API, job queue, SSE streaming, SQLite database
- **Frontend** - Board view for cards, pipeline runner, agent playground
- **Runners** - Docker containers running Claude Code or Gemini CLI agents
- **Git Server** - Internal bare repos; agents work on branches, never touch your origin

## Core Concepts

**Cards** - A unit of work. Title, description, assigned agent. Cards go through: `todo → working → in_review → done`. Agents create branches, you review diffs, approve or reject.

**Agents** - Prompt templates with placeholders (`{{title}}`, `{{description}}`). Define in the UI or in your repo under `.lazyaf/agents/*.yaml`.

**Playground** - Test agent prompts without creating cards. Stream output, see diffs, optionally save to a branch.

## Pipelines

Multi-step automation triggered manually, on card completion, or on push.

### Step Types

| Type | Description |
|------|-------------|
| `shell` | Run a command in the repo context |
| `docker` | Run a command in a specified container |
| `agent` | Run an AI agent with a task |

### Triggers

```yaml
triggers:
  - type: card_complete
    config: { status: "in_review" }
    on_pass: merge
    on_fail: reject

  - type: push
    config: { branches: ["main"] }
```

### Defining Pipelines

**In the UI** - Pipelines tab → New Pipeline

**In your repo** - `.lazyaf/pipelines/ci.yaml`:

```yaml
name: ci
description: Run tests on card completion

steps:
  - name: install
    type: shell
    config:
      command: npm install

  - name: test
    type: shell
    config:
      command: npm test
    on_failure: stop

  - name: lint
    type: shell
    config:
      command: npm run lint
```

### Running Pipelines

**UI** - Click "Run" on any pipeline

**API** - `POST /api/pipelines/{id}/run`

**Repo-defined** - `POST /api/repos/{repo_id}/lazyaf/pipelines/{name}/run`

### Step Flow Control

```yaml
on_success: next      # Continue to next step (default)
on_success: stop      # Stop pipeline successfully
on_success: merge:main  # Merge card to branch

on_failure: stop      # Stop pipeline with failure (default)
on_failure: next      # Continue anyway
```

## Project Structure

```
backend/          # FastAPI app
frontend/         # Svelte SPA
runner-claude/    # Claude Code runner
runner-gemini/    # Gemini runner
cli/              # lazyaf CLI (ingest, land, list)
```

## MCP Server

LazyAF exposes an MCP (Model Context Protocol) server so AI agents like Claude can interact with it directly.

**Claude Desktop config** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "lazyaf": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/lazyaf/backend", "python", "-m", "app.mcp"]
    }
  }
}
```

**Available tools:**
- `list_repos` - List all repositories
- `list_cards` - List cards for a repo
- `create_card` - Create a new task for an agent
- `start_card` - Start agent work on a card
- `get_card` - Get card status and details
- `approve_card` - Merge completed work
- `list_pipelines` - List pipelines
- `run_pipeline` - Execute a pipeline
- `get_branches` - List repo branches

Set `LAZYAF_URL` env var if backend isn't at `http://localhost:8000`.

## API

`GET /api/repos` - List repos
`POST /api/repos/{id}/cards` - Create card
`POST /api/cards/{id}/start` - Start agent work
`POST /api/cards/{id}/approve` - Merge to target branch
`GET /api/playground/{session}/stream` - SSE log stream
`POST /api/pipelines/{id}/run` - Run pipeline

Full API docs at `http://localhost:8000/docs`
