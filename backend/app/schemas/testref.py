"""
Pydantic schemas for the test tie-back layer (Phase 12.2.6).

The manifest schema here is ONE SOURCE OF TRUTH for pinned contract #1 (R3):
the pytest plugin (runner_common/pytest_lazyaf.py) writes it, the control
runtime ships it verbatim, and POST /api/steps/{id}/test-results validates
it with these models.
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


# -----------------------------------------------------------------------------
# Manifest (pinned contract #1)
# -----------------------------------------------------------------------------

class TestResultEntry(BaseModel):
    lazyaf_test_id: str
    status: Literal["passed", "failed", "skipped"]
    duration_ms: int | None = None
    file_path: str | None = None


class TestResultsManifest(BaseModel):
    # Literal pins the schema version: an unknown version is a 422, never a
    # silent partial parse.
    version: Literal[1]
    results: list[TestResultEntry]


class TestIngestResponse(BaseModel):
    results_received: int
    test_runs_created: int
    test_runs_updated: int
    orphan_refs_created: int


# -----------------------------------------------------------------------------
# TestRef / TestRun reads
# -----------------------------------------------------------------------------

class TestRefRead(BaseModel):
    id: str
    lazyaf_test_id: str
    repo_id: str
    file_path: str | None = None
    criterion_id: str | None = None
    status: Literal["active", "orphan"]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CriterionHistoryEntry(BaseModel):
    """One TestRun in a criterion's history series (joined via TestRef)."""
    id: str
    test_ref_id: str
    lazyaf_test_id: str
    pipeline_run_id: str
    step_run_id: str | None = None
    commit_sha: str
    branch: str | None = None
    status: str
    duration_ms: int | None = None
    model: str | None = None
    prompt_template_id: str | None = None
    created_at: datetime


# -----------------------------------------------------------------------------
# Reconcile (pinned contract #6)
# -----------------------------------------------------------------------------

class ReconcileRefItem(BaseModel):
    lazyaf_test_id: str
    file_path: str | None = None


class ReconcileRequest(BaseModel):
    repo_id: str
    refs: list[ReconcileRefItem]


class ReconcileResponse(BaseModel):
    created: int
    updated: int
    orphaned: int
