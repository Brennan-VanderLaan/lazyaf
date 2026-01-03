# Phase 8: Test Result Capture

> **Status**: COMPLETE
> **Goal**: Visibility into whether agent-produced code passes tests

## Problem Being Solved

Agents complete work but may have broken tests. No visibility into test results, and manual checking is tedious. Want LazyAF to be the source of truth for test status, not GHA.

## MVP Scope

- [x] Runner detects test framework (package.json scripts, pytest.ini, etc.)
- [x] Runner runs tests after Claude Code completes (similar to docker entrypoint script)
- [x] Test results stored on Job model (pass_count, fail_count, output)
- [x] Test summary displayed in CardModal when viewing completed cards
- [x] Cards with failing tests get "failed" status with test output visible
- [x] Test output included in job logs

## Explicit Non-Goals (This Phase)

- Separate test dashboard (use existing UI)
- Automatic retry on test failure (manual retry exists)
- Coverage reports
- Test trend analysis
- External CI integration (this IS the CI)

## Key Files

- `backend/runner/entrypoint.py` - Add test detection and execution
- `backend/app/models/job.py` - Add test result fields
- `frontend/src/lib/components/CardModal.svelte` - Display test results

## Test Framework Detection

```python
# Priority order for detection:
1. package.json -> scripts.test -> "npm test"
2. pytest.ini / pyproject.toml [tool.pytest] -> "pytest"
3. Cargo.toml -> "cargo test"
4. go.mod -> "go test ./..."
5. Makefile with test target -> "make test"
```

## Decision Points

- If no tests detected: treat as "no tests" (not pass or fail)
- Test failure blocks card from going to "in_review" (stays as "failed")
- Test timeout: 5 minutes default, configurable per repo

## Deliverable

After agent work, see "Tests: 42 passed, 3 failed" in card modal. Failed tests = failed card.
