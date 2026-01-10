# Phase 10: Events & Triggers (COMPLETE)

**Goal**: Enable the Card -> Pipeline -> Merge workflow

## 10a: Card Completion Trigger
- [x] Add `triggers` field to Pipeline model (JSON array of TriggerConfig)
- [x] Add `trigger_context` field to PipelineRun model
- [x] When card status -> done/in_review, check for matching pipelines
- [x] Auto-trigger pipeline with card context (branch, commit, card_id)
- [x] UI: Configure triggers in pipeline editor
- [x] Trigger actions: on_pass (merge/nothing), on_fail (fail/reject/nothing)

## 10b: Auto-Merge Action
- [x] Pipeline completion executes trigger actions from context
- [x] `on_pass: "merge"` - merge card branch to default branch
- [x] `on_fail: "fail"` - mark card as failed
- [x] `on_fail: "reject"` - reject card back to todo
- [x] Merge uses internal git server
- [x] Conflict handling: fail with clear error

## 10c: Push Triggers
- [x] Internal git server captures pushed refs
- [x] Push event fires trigger_service.on_push()
- [x] Pipeline trigger: `{type: "push", config: {branches: ["main", "dev"]}}`
- [x] Branch pattern matching with fnmatch
