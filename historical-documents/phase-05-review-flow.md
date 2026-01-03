# Phase 5: Review Flow

> **Status**: COMPLETE
> **Goal**: Complete the human review loop

## Completed Tasks

- [x] Job logs viewer (real-time polling, expandable panel)
- [x] Error handling and retry (retry button on failed cards)
- [x] Git graph visualization (commit history per branch)
- [x] Branch selector in RepoInfo panel
- [x] Diff viewer in CardModal for code review
- [x] "Approve" action moves card to done
- [x] "Reject" action resets card to todo
- [ ] "Approve" actually merges branch to default (later - requires merge logic)

## Deliverable

Full loop from card -> agent work -> review -> approve/reject works

## User Workflow

1. User creates a card describing a feature
2. User clicks "Start" to trigger agent work
3. Agent implements feature and pushes to internal git server
4. Card moves to "In Review" status
5. User reviews:
   - Views job logs to understand what agent did
   - Views diff to see code changes
   - Views git graph to see commit history
6. User approves or rejects:
   - **Approve**: Card moves to "Done"
   - **Reject**: Card returns to "Todo" for another attempt

## Key Components

- **JobLogsViewer**: Real-time streaming of agent output
- **DiffViewer**: Side-by-side or unified diff view
- **GitGraph**: Commit history visualization
- **BranchSelector**: Switch between branches to review
