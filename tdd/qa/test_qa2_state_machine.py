"""QA-2: illegal state transitions and lifecycle abuse.

Every test here speaks HTTP to a running stack (default http://localhost:8790,
override with ``LAZYAF_QA_BASE_URL``). Run with:

    python -m pytest -c tdd/qa/pytest.ini tdd/qa/test_qa2_state_machine.py

Tests that ENCODE A DEFECT are marked ``xfail(strict=True)``: they describe
the behaviour the platform SHOULD have, fail today, and will turn into a
loud XPASS the moment the bug is fixed.

Tests without the marker are regression locks on behaviour that is already
correct - a state guard that gets loosened later must break something.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qa2_support import (  # noqa: E402
    api,
    branch_names,
    card_status,
    concurrent,
    get_card,
    get_job,
    get_run,
    make_card,
    make_pipeline,
    pipeline_run_count,
    seed_repo,
    start_card,
    wait_for_card,
    wait_for_run,
)

pytestmark = pytest.mark.qa


@pytest.fixture(scope="module")
def repo():
    repo_id, _ = seed_repo()
    return repo_id


# ===========================================================================
# QA2-01  approve has no status guard at all
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-01: POST /api/cards/{id}/approve has no status guard "
        "(backend/app/routers/cards.py approve_card). A card in 'todo' that "
        "was never started, has no branch, no job and no diff is marked "
        "'done' with 200 OK and fires card_complete triggers."
    ),
)
def test_approve_refuses_a_card_that_never_ran(repo):
    card_id = make_card(repo, "approve-never-ran")
    status, body = api("POST", f"/api/cards/{card_id}/approve", {})
    assert status == 400, (
        f"approving a never-started card returned {status} {body}; "
        f"card is now {card_status(card_id)!r}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-01b: approving an already-'done' card succeeds again "
        "and re-fires its card_complete triggers, so a double-click on "
        "Approve produces two identical verification runs."
    ),
)
def test_repeated_approve_does_not_refire_card_complete_triggers(repo):
    pipeline_id = make_pipeline(
        repo,
        "verify-on-done",
        "echo verifying",
        triggers=[
            {"type": "card_complete", "enabled": True, "config": {"status": "done"}}
        ],
    )
    card_id = make_card(repo, "trigger-refire")
    before = pipeline_run_count(pipeline_id)

    for _ in range(3):
        api("POST", f"/api/cards/{card_id}/approve", {})
        time.sleep(1.5)
    time.sleep(3)

    after = pipeline_run_count(pipeline_id)
    assert after - before <= 1, (
        f"three approvals of one card started {after - before} verification "
        "runs; reaching 'done' once should trigger at most one"
    )


# ===========================================================================
# QA2-02  reject has no status guard, and abandons a live run
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-02: POST /api/cards/{id}/reject has no status guard "
        "(backend/app/routers/cards.py reject_card). Rejecting an "
        "in_progress card sends it back to 'todo' and clears branch_name "
        "while the agent run keeps executing; the Job row is then stranded "
        "at 'running' forever because agent_run._complete_card_work's "
        "stale-completion guard returns early without landing the Job."
    ),
)
def test_reject_of_a_running_card_does_not_strand_its_job(repo):
    card_id = make_card(repo, "reject-mid-run", seconds=30)
    start_card(card_id)
    time.sleep(6)
    job_id = get_card(card_id)[1]["job_id"]
    assert get_job(job_id)["status"] == "running"

    status, _ = api("POST", f"/api/cards/{card_id}/reject")
    assert status in (200, 400, 409)

    # Well past the 30s mock run: the Job must not still claim to be running.
    time.sleep(50)
    job = get_job(job_id)
    assert job["status"] != "running", (
        "the Job of a rejected card is stuck at 'running' with "
        f"completed_at={job.get('completed_at')!r} - the card modal polls it "
        "every 3s and spins forever"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-02b: reject sends an in_progress card to 'todo' "
        "without cancelling its run, so /start accepts it again and a second "
        "agent container runs concurrently against the same repo, leaving an "
        "orphan lazyaf/<jobid> branch no card references."
    ),
)
def test_reject_then_restart_does_not_run_two_agents_at_once(repo):
    card_id = make_card(repo, "reject-restart", seconds=25)
    start_card(card_id)
    time.sleep(5)
    first_job = get_card(card_id)[1]["job_id"]

    api("POST", f"/api/cards/{card_id}/reject")
    status, body = api("POST", f"/api/cards/{card_id}/start")

    if status != 200:
        return  # refused - the guard exists, nothing to assert
    second_job = body["job_id"]
    time.sleep(4)
    both_live = (
        get_job(first_job)["status"] in ("queued", "running")
        and get_job(second_job)["status"] in ("queued", "running")
    )
    assert not both_live, (
        f"jobs {first_job[:8]} and {second_job[:8]} are both live for one card"
    )


# ===========================================================================
# QA2-03  start / retry are read-check-write races (double click)
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-03: start_card checks `card.status != 'todo'` and "
        "then writes, with no row lock or conditional UPDATE "
        "(backend/app/routers/cards.py start_card). Two concurrent POSTs "
        "both pass the check, so one double-click starts two agent runs, "
        "two Jobs and two branches; the card keeps only one job_id."
    ),
)
def test_double_click_start_creates_only_one_job(repo):
    card_id = make_card(repo, "double-start", seconds=10)
    results = concurrent(lambda: api("POST", f"/api/cards/{card_id}/start"), n=2)
    accepted = [body for status, body in results if status == 200]
    assert len(accepted) == 1, (
        "both concurrent /start calls were accepted; job ids "
        f"{[b.get('job_id') for b in accepted]}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-03b: retry_card has the same read-check-write race "
        "as start (backend/app/routers/cards.py retry_card); double-clicking "
        "Retry on an in_review card starts two agent runs."
    ),
)
def test_double_click_retry_creates_only_one_job(repo):
    card_id = make_card(repo, "double-retry", seconds=8)
    start_card(card_id)
    wait_for_card(card_id, ("in_review", "failed"))
    results = concurrent(lambda: api("POST", f"/api/cards/{card_id}/retry"), n=2)
    accepted = [body for status, body in results if status == 200]
    assert len(accepted) == 1, (
        "both concurrent /retry calls were accepted; job ids "
        f"{[b.get('job_id') for b in accepted]}"
    )


# ===========================================================================
# QA2-04  PATCH status is an unguarded state-machine bypass (the kanban drag)
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-04: PATCH /api/cards/{id} writes `status` straight "
        "through (backend/app/routers/cards.py update_card), and the kanban "
        "board's drag handler calls exactly that "
        "(frontend/src/lib/components/Board.svelte handleDrop). Dragging a "
        "running card into the Done column marks it done without merging "
        "anything and strands its Job at 'running' forever."
    ),
)
def test_patching_a_running_card_to_done_lands_its_job(repo):
    card_id = make_card(repo, "drag-to-done", seconds=25)
    start_card(card_id)
    time.sleep(5)
    job_id = get_card(card_id)[1]["job_id"]

    status, _ = api("PATCH", f"/api/cards/{card_id}", {"status": "done"})
    assert status in (200, 400, 409)

    time.sleep(45)
    job = get_job(job_id)
    assert job["status"] != "running", (
        "card says 'done' while its Job still says 'running' - the card and "
        "its job disagree and the modal spinner never resolves"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-04b: dragging a card to Done marks it done without "
        "merging its branch, so the board claims work landed that is still "
        "sitting unmerged on lazyaf/<jobid>."
    ),
)
def test_patching_a_card_to_done_merges_its_branch(repo):
    card_id = make_card(repo, "drag-unmerged", files={"dragged.txt": "payload"})
    start_card(card_id)
    card = wait_for_card(card_id, ("in_review", "failed"))
    assert card["status"] == "in_review", card
    branch = card["branch_name"]

    api("PATCH", f"/api/cards/{card_id}", {"status": "done"})

    status, diff = api(
        "GET", f"/api/repos/{repo}/diff?base=main&head={branch}"
    )
    assert status == 200, diff
    assert not diff.get("files"), (
        f"card is 'done' but branch {branch} still differs from main by "
        f"{len(diff.get('files') or [])} file(s) - nothing was merged"
    )


# ===========================================================================
# QA2-05  PATCH with an explicit null on a NOT NULL column is a 500
# ===========================================================================

@pytest.mark.parametrize("field", ["status", "runner_type", "step_type", "title", "description"])
@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-05: update_card applies model_dump(exclude_unset=True) "
        "verbatim (backend/app/routers/cards.py update_card), so an explicit "
        "JSON null on a NOT NULL column reaches the UPDATE and raises an "
        "unhandled sqlite3.IntegrityError -> 500 with a plain-text "
        "'Internal Server Error' body."
    ),
)
def test_patch_null_on_a_required_field_is_a_client_error(repo, field):
    card_id = make_card(repo, f"null-{field}")
    status, body = api("PATCH", f"/api/cards/{card_id}", {field: None})
    assert status < 500, f"PATCH {field}=null returned {status}: {body!r}"


# ===========================================================================
# QA2-06  resolve-conflicts merges with no conflict, on any card status
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-06: POST /api/cards/{id}/resolve-conflicts never "
        "checks that a conflict exists, nor the card's status "
        "(backend/app/routers/cards.py resolve_conflicts). It force-merges "
        "the caller's file contents into the default branch - including "
        "files that exist in no branch - on a card that is already 'done'."
    ),
)
def test_resolve_conflicts_refuses_when_there_is_no_conflict(repo):
    card_id = make_card(repo, "no-conflict", files={"clean.txt": "agent wrote this"})
    start_card(card_id)
    wait_for_card(card_id, ("in_review", "failed"))

    approve_status, _ = api("POST", f"/api/cards/{card_id}/approve", {})
    assert approve_status == 200

    status, body = api(
        "POST",
        f"/api/cards/{card_id}/resolve-conflicts",
        {
            "resolutions": [
                {"path": "clean.txt", "content": "OVERWRITTEN with no conflict"},
                {"path": "never_existed.txt", "content": "invented by the caller"},
            ]
        },
    )
    assert status == 400, (
        "resolve-conflicts force-merged arbitrary content into the default "
        f"branch with no conflict present: {body}"
    )


# ===========================================================================
# QA2-07  deleting a pipeline mid-run destroys the run and leaks its container
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-07: DELETE /api/pipelines/{id} has no in-flight-run "
        "check (backend/app/routers/pipelines.py delete_pipeline). It "
        "cascade-deletes a RUNNING PipelineRun, so the run 404s instantly, "
        "/cancel can no longer reach it, and the step container is left "
        "behind exited instead of being removed."
    ),
)
def test_deleting_a_pipeline_with_a_live_run_is_refused(repo):
    pipeline_id = make_pipeline(repo, "delete-mid-run", "echo start; sleep 30; echo end")
    status, run = api("POST", f"/api/pipelines/{pipeline_id}/run", {})
    assert status == 200, run
    run_id = run["id"]
    time.sleep(4)

    del_status, _ = api("DELETE", f"/api/pipelines/{pipeline_id}")
    assert del_status in (400, 409), (
        f"deleting a pipeline with a live run returned {del_status}; the run "
        f"is now {get_run(run_id)['status']!r}"
    )


# ===========================================================================
# QA2-08  deleting a repo mid-run strands its Job at 'running' forever
# ===========================================================================

@pytest.mark.xfail(
    strict=True,
    reason=(
        "QA finding QA2-08: DELETE /api/repos/{id} has no in-flight-run "
        "check (backend/app/routers/repos.py delete_repo). It deletes the "
        "git storage and cascade-deletes the Card while its agent run is "
        "executing; the Job row survives, still 'running', pointing at a "
        "card that no longer exists, and nothing cancels the run."
    ),
)
def test_deleting_a_repo_with_a_live_run_does_not_strand_a_job():
    repo_id, _ = seed_repo()
    card_id = make_card(repo_id, "repo-delete-mid-run", seconds=25)
    start_card(card_id)
    time.sleep(6)
    job_id = get_card(card_id)[1]["job_id"]

    status, _ = api("DELETE", f"/api/repos/{repo_id}")
    assert status in (204, 400, 409)

    time.sleep(45)
    job = get_job(job_id)
    assert job["status"] != "running", (
        "an orphan Job is permanently 'running' for a card and repo that "
        "were both deleted"
    )


# ===========================================================================
# Regression locks: behaviour that is CORRECT today and must stay correct
# ===========================================================================

@pytest.mark.parametrize("bad", ["in_progress", "in_review", "done", "failed"])
def test_start_is_refused_from_every_non_todo_status(repo, bad):
    card_id = make_card(repo, f"start-guard-{bad}")
    api("PATCH", f"/api/cards/{card_id}", {"status": bad})
    status, body = api("POST", f"/api/cards/{card_id}/start")
    assert status == 400
    assert "todo" in body["detail"]


@pytest.mark.parametrize("bad", ["todo", "in_progress", "done"])
def test_retry_is_refused_from_every_non_retryable_status(repo, bad):
    card_id = make_card(repo, f"retry-guard-{bad}")
    api("PATCH", f"/api/cards/{card_id}", {"status": bad})
    status, body = api("POST", f"/api/cards/{card_id}/retry")
    assert status == 400
    assert bad in body["detail"]


def test_cancelling_a_finished_pipeline_run_is_refused(repo):
    pipeline_id = make_pipeline(repo, "cancel-finished", "echo hi")
    status, run = api("POST", f"/api/pipelines/{pipeline_id}/run", {})
    assert status == 200
    run_id = run["id"]
    wait_for_run(run_id, ("passed", "failed", "cancelled"))

    for _ in range(2):
        status, body = api("POST", f"/api/pipeline-runs/{run_id}/cancel")
        assert status == 400
        assert body["detail"] == "Pipeline run cannot be cancelled"


def test_concurrent_cancels_of_a_live_run_leave_it_consistently_cancelled(repo):
    pipeline_id = make_pipeline(repo, "cancel-race", "echo start; sleep 30; echo end")
    status, run = api("POST", f"/api/pipelines/{pipeline_id}/run", {})
    assert status == 200
    run_id = run["id"]
    time.sleep(4)

    results = concurrent(lambda: api("POST", f"/api/pipeline-runs/{run_id}/cancel"), n=3)
    assert all(s in (200, 400) for s, _ in results), results

    final = get_run(run_id)
    assert final["status"] == "cancelled"
    for step in final.get("step_runs", []):
        assert step["status"] in ("cancelled", "passed", "failed")
        assert step.get("completed_at") is not None


def test_cancelling_a_card_job_lands_the_card_in_failed(repo):
    """The one lifecycle path that gets this right - lock it down."""
    card_id = make_card(repo, "cancel-job", seconds=30)
    start_card(card_id)
    time.sleep(6)
    job_id = get_card(card_id)[1]["job_id"]

    status, job = api("POST", f"/api/jobs/{job_id}/cancel")
    assert status == 200
    assert job["status"] == "failed"
    assert job["error"] == "Cancelled by user"
    assert job["completed_at"] is not None
    assert card_status(card_id) == "failed"

    status, body = api("POST", f"/api/jobs/{job_id}/cancel")
    assert status == 400
    assert body["detail"] == "Job cannot be cancelled"

    # And it stays failed: the straggler run must not walk it into in_review.
    time.sleep(35)
    assert card_status(card_id) == "failed"
    assert get_job(job_id)["status"] == "failed"


def test_every_branch_operation_refuses_cleanly_once_the_branch_is_gone(repo):
    card_id = make_card(repo, "branch-gone", files={"gone.txt": "x"})
    start_card(card_id)
    card = wait_for_card(card_id, ("in_review", "failed"))
    assert card["status"] == "in_review"
    branch = card["branch_name"]

    status, _ = api("DELETE", f"/api/repos/{repo}/branches/{branch}")
    assert status == 200

    for method, path, body in (
        ("POST", f"/api/cards/{card_id}/approve", {}),
        ("GET", f"/api/cards/{card_id}/diff", None),
        ("POST", f"/api/cards/{card_id}/rebase", {}),
        (
            "POST",
            f"/api/cards/{card_id}/resolve-conflicts",
            {"resolutions": [{"path": "z.txt", "content": "z"}]},
        ),
    ):
        status, payload = api(method, path, body)
        assert status == 400, f"{method} {path} -> {status} {payload}"
        assert "not found" in payload["detail"].lower()

    assert card_status(card_id) == "in_review"


def test_running_a_pipeline_on_an_uningested_repo_is_refused():
    status, repo = api("POST", "/api/repos", {"name": "qa2-uningested"})
    assert status == 201
    pipeline_id = make_pipeline(repo["id"], "empty", "echo hi")
    status, body = api("POST", f"/api/pipelines/{pipeline_id}/run", {})
    assert status == 400
    assert "ingested" in body["detail"]
    api("DELETE", f"/api/repos/{repo['id']}")


def test_card_status_enum_is_enforced(repo):
    card_id = make_card(repo, "enum-guard")
    for bad in ("banana", "DONE", "in-progress", 7):
        status, _ = api("PATCH", f"/api/cards/{card_id}", {"status": bad})
        assert status == 422, f"status={bad!r} was accepted"
    assert card_status(card_id) == "todo"


def test_job_id_cannot_be_spoofed_through_patch(repo):
    """job_id is not on CardUpdate, so a caller cannot repoint a card's job."""
    card_id = make_card(repo, "job-spoof")
    status, body = api(
        "PATCH",
        f"/api/cards/{card_id}",
        {"job_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert status == 200
    assert body["job_id"] is None
