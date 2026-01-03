# Phase 4: Agent Integration

> **Status**: COMPLETE
> **Goal**: Cards trigger Claude Code, results in PRs (or internal branches)

## Completed Tasks

- [x] Runner entrypoint script (clone from internal server, branch, invoke Claude)
- [x] Claude Code invocation with card context
- [x] Push results back to internal git server
- [x] Job status callbacks (running/completed/failed)
- [x] Link branches back to cards
- [x] WebSocket status updates (job_status, card_updated broadcasts)
- [x] Jobs store in frontend for real-time tracking
- [x] JobStatus component (spinner, duration, logs viewer)
- [x] Background heartbeat thread during long Claude operations
- [x] Streaming output from Claude Code
- [x] Runner auto-reconnect when backend restarts
- [x] Workspace cleanup between job runs
- [x] Handle branch already exists on retry (fetch + merge)

## Deliverable

Creating a card and clicking "Start" produces changes in the internal repo

## Key Architecture

### Runner Flow

1. Backend assigns job to runner
2. Container starts with environment:
   - `REPO_URL` - Git remote URL
   - `BRANCH_NAME` - Feature branch to create
   - `BASE_BRANCH` - Branch to base off (e.g., dev)
   - `CARD_TITLE` - Card title
   - `CARD_DESCRIPTION` - Card description
   - `CALLBACK_URL` - URL to report status
   - `ANTHROPIC_API_KEY` - For Claude Code
3. Runner clones repo, creates branch
4. Runner invokes Claude Code with structured prompt
5. Claude Code implements feature, commits
6. Runner pushes branch to internal git server
7. Runner reports completion to callback URL

### Claude Code Prompt Template

```
You are implementing a feature for this project.

## Feature Request
Title: {card_title}

Description:
{card_description}

## Instructions
1. Implement this feature following existing code patterns
2. Write tests if a test framework is present
3. Commit your changes with a clear message
4. Do not modify unrelated code

## Repository Context
{readme_content_if_available}
```
