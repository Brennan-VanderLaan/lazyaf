"""The sole writer of StepRun.logs in control mode - Phase 12.6.

Two channels now carry log lines for one step:

    the step CONTAINER  -> POST /api/steps/{id}/logs  (HTTP, step JWT)
    the runner AGENT    -> `log` frame on the runner WebSocket

They are genuinely different data. The container's lines are the step's own
stdout. The agent's lines are the ones a container **cannot** emit because it
does not exist yet or failed to start - "[runner] pulling
lazyaf-test-runner:dev", "[runner] ERROR: docker daemon unreachable". Without
the second channel, a remote step that dies before it runs has no way to
explain itself.

Two channels, ONE writer (R3). The alternative - a second append+broadcast
implementation inside `ws_runners.py` - is exactly the duplication 12.2.6 and
12.5 spent two phases removing from the test-results and usage channels.

Append semantics per channel, and the difference is the whole reason `source`
exists:

    source="container"  lines are appended VERBATIM. The control runtime's
                        wire contract is that content already carries its
                        trailing newline; adding one here would double-space
                        every remote step's log.
    source="runner"     each line is prefixed "[runner] " and newline-
                        terminated, so the two interleaved streams are
                        distinguishable by eye in one blob.

Ordering across the two streams is guaranteed STRUCTURALLY, not by
best-effort timestamps: the agent emits `[runner]` lines only BEFORE
`container.start()` and AFTER the container exits, so the two streams cannot
overlap in time and append order IS real order.
"""
from __future__ import annotations

import logging

from sqlalchemy import func, select, update

from app.models.pipeline import StepRun
from app.services.websocket import manager

logger = logging.getLogger(__name__)

#: Prefix stamped on runner-origin lines. Also what the loopback gate greps
#: for to prove the agent (not the container) produced output.
RUNNER_LINE_PREFIX = "[runner] "

SOURCE_CONTAINER = "container"
SOURCE_RUNNER = "runner"


class StepRunMissing(LookupError):
    """The StepExecution points at a StepRun row that no longer exists.

    A distinct exception rather than a `None` return: the HTTP router turns
    this into a 404 and the WS endpoint drops the frame with a WARN, and
    neither should have to distinguish "no lines" from "no such step run".
    """

    def __init__(self, step_run_id: str):
        self.step_run_id = step_run_id
        super().__init__(f"step run {step_run_id} not found")


def format_lines(lines: list[str], source: str) -> list[str]:
    """Render raw lines for storage in the per-source shape.

    Pure and public so both callers - and their tests - can assert on the
    exact bytes without a database.
    """
    if source == SOURCE_RUNNER:
        return [f"{RUNNER_LINE_PREFIX}{line.rstrip(chr(10))}\n" for line in lines]
    return list(lines)


async def append_step_logs(
    db,
    execution,
    lines: list[str],
    *,
    source: str = SOURCE_CONTAINER,
) -> int:
    """Append `lines` to the StepRun behind `execution` and broadcast them.

    Args:
        db: an AsyncSession.
        execution: the StepExecution the lines belong to (only
            `step_run_id` is read - the caller owns auth and the
            terminal-write rejection).
        lines: raw log content. For `source="container"` these are the
            POSTed contents, newline included. For `source="runner"` they
            are bare lines.
        source: "container" | "runner" (see the module docstring).

    Returns:
        The number of lines appended.

    Raises:
        StepRunMissing: the execution's StepRun row is gone.

    Efficiency, inherited verbatim from the 12.3 router this replaces: ONE
    string join, ONE targeted SQL append (`logs = COALESCE(logs,'') ||
    :chunk`, so a multi-megabyte blob is never read-modify-written), ONE
    commit, ONE `step_log_batch` frame carrying the whole batch.
    """
    if source not in (SOURCE_CONTAINER, SOURCE_RUNNER):
        raise ValueError(
            f"unknown log source {source!r} (known: {SOURCE_CONTAINER}, {SOURCE_RUNNER})"
        )

    # Address the StepRun WITHOUT loading its (potentially large) log blob.
    result = await db.execute(
        select(StepRun.pipeline_run_id, StepRun.step_index).where(
            StepRun.id == execution.step_run_id
        )
    )
    step_run_row = result.one_or_none()
    if step_run_row is None:
        raise StepRunMissing(execution.step_run_id)

    if not lines:
        return 0

    contents = format_lines(list(lines), source)

    await db.execute(
        update(StepRun)
        .where(StepRun.id == execution.step_run_id)
        .values(logs=func.coalesce(StepRun.logs, "") + "".join(contents))
        .execution_options(synchronize_session=False)
    )
    await db.commit()

    # Broadcast AFTER the commit: a frame the DB has not accepted yet is a
    # line the UI shows and a reload loses.
    #
    # One `step_log_batch` frame per call, lines rstripped of their trailing
    # newline to match what the frontend renders per line. NOT
    # `publish_step_logs` (which fans out one `step_log` frame per line) -
    # the 12.3 router contract is one batch frame per POST and the frontend
    # consumes `step_log_batch` alongside `step_log`.
    await manager.publish_step_log_batch(
        step_run_row.pipeline_run_id,
        step_run_row.step_index,
        [content.rstrip("\n") for content in contents],
    )

    return len(contents)


__all__ = [
    "append_step_logs",
    "format_lines",
    "StepRunMissing",
    "RUNNER_LINE_PREFIX",
    "SOURCE_CONTAINER",
    "SOURCE_RUNNER",
]
