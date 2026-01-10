# Phase 11: Agent Playground (MVP Complete)

**Goal**: Rapid experimentation with agent prompts without going through full card/job development loops

**Problem Being Solved**: When developing or refining agent prompts, you need to iterate quickly. Currently, testing an agent requires creating a card, starting it, waiting for completion, reviewing the diff - a slow loop. The Agent Playground provides immediate feedback on agent behavior.

## Key Architecture Decision

**Use Existing Runner Infrastructure**: Direct Claude/Gemini API calls cannot modify files - they only return text. Real agent behavior (file changes, diffs) requires the existing Docker runner infrastructure (Claude Code CLI, aider, etc.). The Playground reuses `job_queue` with a special `is_playground=True` flag.

## MVP Scope (Phase 11a-11c)

- **Test Once mode only** (defer Test Continuous to later)
- **Branch input mode only** (test against real branch, like card execution)
- **Real-time streaming** (agent reasoning + tool calls via `--output-format stream-json`)
- **Branch creation opt-in** (run against source branch, save to new branch only if user wants)

## Architecture

```
+-----------------------------------------------------------------------+
|                      AGENT PLAYGROUND TAB                              |
+-----------------------------------------------------------------------+
|                                                                        |
|  +----------------------+  +--------------------------------------+   |
|  |   AGENT CONFIG       |  |      STREAMING AGENT OUTPUT          |   |
|  |                      |  |                                      |   |
|  |  Repo: [dropdown]    |  |  I'll analyze the code structure     |   |
|  |  Agent: [dropdown]   |  |  and make the requested changes.     |   |
|  |  Runner: Claude/Gem  |  |                                      |   |
|  |                      |  |  [Tool: Read] src/utils.ts           |   |
|  |  Branch: [dropdown]  |  |  [Tool: Edit] src/utils.ts:42        |   |
|  |                      |  |                                      |   |
|  |  +----------------+  |  |  The function now handles edge       |   |
|  |  | Task Override  |  |  |  cases for empty input...           |   |
|  |  | (optional)     |  |  |                                      |   |
|  |  +----------------+  |  +--------------------------------------+   |
|  |                      |                                             |
|  |  [> Test Once]       |  +--------------------------------------+   |
|  |  [x Cancel]          |  |         DIFF PREVIEW                 |   |
|  |                      |  |                                      |   |
|  |  [ ] Save to branch  |  |  src/utils.ts                        |   |
|  |  agent-test/...      |  |  - const old = "foo";                |   |
|  |                      |  |  + const new = "bar";                |   |
|  +----------------------+  +--------------------------------------+   |
|                                                                        |
+-----------------------------------------------------------------------+
```

## How It Works

1. User selects repo, branch, agent, runner type
2. Optionally overrides task description
3. Clicks "Test Once"
4. Backend creates a playground job (ephemeral, no DB card)
5. Runner picks up job, executes agent with **streaming output**:
   ```bash
   claude -p --output-format stream-json --include-partial-messages "..."
   ```
6. Runner reads stdout line-by-line, forwards JSON events to backend
7. Backend relays events to frontend via SSE (tokens, tool calls, reasoning)
8. On completion, backend captures diff from runner
9. Diff displayed in UI
10. If "Save to branch" checked, changes pushed to `agent-test/<name>-NNN`

## Streaming Architecture

Claude Code CLI supports `--output-format stream-json` which emits real-time JSON events:
- `content_block_delta` - streaming text tokens
- `tool_use` - tool calls (Edit, Bash, etc.)
- `message_stop` - completion

The runner captures these and forwards to the playground session, giving users visibility into agent reasoning as it happens.

## Implementation Phases

### Phase 11a: Foundation (MVP)
- [x] Add `is_playground`, `playground_session_id`, `playground_save_branch` to QueuedJob
- [x] Create `PlaygroundService` with `start_test()`, `stream_logs()`, `get_result()`
- [x] Create `/playground/*` REST endpoints
- [x] SSE streaming for runner logs
- [x] Runner checks `is_playground` flag, skips card updates if true (Claude only, Gemini TODO)

### Phase 11b: Frontend - Test Once Mode
- [x] Add Playground tab to navigation
- [x] Create `PlaygroundPage.svelte`
- [x] Create `playground` store
- [x] Repo/branch/agent/runner selectors
- [x] Task override textarea
- [x] "Test Once" button
- [x] Log stream display (reuse LogViewer patterns)
- [x] Connect to SSE endpoint

### Phase 11c: Diff & Save
- [x] Backend captures diff after runner completes
- [x] Diff preview panel in frontend
- [x] "Save to branch" checkbox
- [x] Branch name input with auto-generate (`agent-test/<agent>-NNN`)
- [x] Push changes to save branch on completion

### Phase 11d: Cancellation
- [x] Backend: `cancel_test()` updates status (actual process kill TODO)
- [x] Frontend: Cancel button
- [x] Graceful cleanup
- [x] Status indicators

### Phase 11e: Polish (Deferred)
- [ ] Keyboard shortcuts (Ctrl+Enter for Test Once)
- [ ] Log auto-scroll with pause on hover
- [ ] Copy diff to clipboard
- [ ] Branch cleanup UI (list/delete `agent-test/*` branches)
- [ ] Error handling and retry

## Future Enhancements (Not MVP)

- **Test Continuous mode**: Auto-run after typing stops (2.5s debounce)
- **Sample text input**: Quick prompt validation without full branch
- **File-specific input**: Test against single file
- **Side-by-side comparison**: Claude vs Gemini results
- **Run history**: Persist playground runs for comparison

## Files Created

| File | Purpose |
|------|---------|
| `backend/app/services/playground_service.py` | Core service |
| `backend/app/routers/playground.py` | REST + SSE endpoints |
| `backend/app/schemas/playground.py` | Request/response models |
| `frontend/src/routes/PlaygroundPage.svelte` | Page component |
| `frontend/src/lib/stores/playground.ts` | State management |
| `frontend/src/lib/components/Playground.svelte` | Main UI |

## Files Modified

| File | Changes |
|------|---------|
| `backend/app/services/job_queue.py` | Add playground fields to QueuedJob |
| `backend/app/main.py` | Mount playground router |
| `frontend/src/App.svelte` | Add Playground tab to nav |
| `frontend/src/lib/api/client.ts` | Add playground API methods |
| `frontend/src/lib/api/types.ts` | Add playground types |
| Runner code | Check `is_playground` flag, handle accordingly |
