"""
Workspace LANES: which checkout a step runs in (M13-1).

Until this module existed a pipeline run owned exactly ONE workspace, so
"which run" and "which checkout" were the same question. They are not.
The owner's headline hypothesis - a high-end model plans, several small
models execute in parallel, and they integrate through git commits and
merges rather than by touching one checkout - needs K parallel steps of one
run to hold K INDEPENDENT working trees. A lane key is the axis that makes
that expressible.

The key is CALLER-SUPPLIED, defaulting to ``DEFAULT_WORKER_KEY``. It is
deliberately NOT derived from the step:

- Not ``step_id``. The expanded S4 graph (docs/milestone-13/strategy-
  catalog.md) has six steps - ``plan``, ``worker_1..worker_4``,
  ``integrate`` - but wants FIVE checkouts: one trunk (shared by ``plan``
  and ``integrate``, which must merge into what ``plan`` read) plus four
  worker lanes. Keying on the step id also turns today's ordinary 3-step
  linear pipeline into three volumes, three clones, three populate
  containers. Which NODE runs and which CHECKOUT it runs in are different
  facts.
- Not a worker index. An index only exists inside a fan-out; the trunk lane
  has none, so an index scheme needs a sentinel anyway - a lane key with
  extra steps - and it cannot name a lane an orchestrator cares about
  (``w1`` vs ``reviewer_2`` vs ``integrate``).
- Not ``StepRun.id`` / ``step_execution_id``. Those change on every retry
  attempt, and a retry of the same graph node must land in the SAME
  checkout. The lane is a property of the WORK, not of the attempt.

``worker_key_for_step`` reads the lane out of a step's ``config`` dict.
``config`` already carries free-form ``lazyaf_*`` keys (see
docs/milestone-13/api-surface.md), so no pipeline schema changes here.
Milestone 13's template expander is what will WRITE ``lazyaf_workspace``
alongside ``lazyaf_branch.mode: "per_worker"``; this module only reads it.
"""
from __future__ import annotations

#: The lane every step runs in unless it says otherwise. Chosen so that
#: ``generate_volume_name(run_id, DEFAULT_WORKER_KEY)`` is byte-identical to
#: the pre-M13-1 ``generate_volume_name(run_id)`` - the single-worker path
#: keeps its volume, its lock key and its cleanup path across the upgrade.
DEFAULT_WORKER_KEY = "default"

#: The step-config field that names a lane.
WORKSPACE_KEY_CONFIG_FIELD = "lazyaf_workspace"

#: Matches models/workspace.py's ``worker_key`` column width. A key longer
#: than the column could not be stored, so it is refused at the boundary
#: instead of being silently truncated into a DIFFERENT lane (R1).
MAX_WORKER_KEY_LENGTH = 64


def validate_worker_key(worker_key: object) -> str:
    """Return ``worker_key`` unchanged, or raise loudly.

    Refuses non-strings, the empty string, and anything longer than
    ``MAX_WORKER_KEY_LENGTH``. Never coerces: a truncated or stringified key
    would name a different checkout than the caller asked for, and silently
    handing a worker the wrong working tree is the exact class of bug this
    whole change exists to eliminate.

    ``None`` is NOT accepted here - callers resolve "unspecified" to
    ``DEFAULT_WORKER_KEY`` before validating, so that "no lane given" and
    "a lane given as null" cannot be confused.
    """
    if not isinstance(worker_key, str):
        raise ValueError(
            f"worker_key must be a string, got {type(worker_key).__name__}: "
            f"{worker_key!r}"
        )
    if not worker_key:
        raise ValueError(
            "worker_key must be a non-empty string (use None, or omit the "
            f"argument, for the {DEFAULT_WORKER_KEY!r} lane)"
        )
    if len(worker_key) > MAX_WORKER_KEY_LENGTH:
        raise ValueError(
            f"worker_key is {len(worker_key)} characters, over the "
            f"{MAX_WORKER_KEY_LENGTH}-character limit: {worker_key!r}"
        )
    return worker_key


def worker_key_for_step(step_config: dict | None) -> str:
    """The workspace lane a step runs in, read from its ``config``.

    An absent, null or blank ``lazyaf_workspace`` field means "the default
    lane" - which is what every step in every pipeline that predates M13
    says, and why this returns ``DEFAULT_WORKER_KEY`` rather than raising.
    A field that is PRESENT but not a usable key is a loud ValueError
    (validate_worker_key): the pipeline author asked for a lane and must be
    told the lane is unusable, not quietly given the trunk.
    """
    if not step_config:
        return DEFAULT_WORKER_KEY
    raw = step_config.get(WORKSPACE_KEY_CONFIG_FIELD)
    if raw is None:
        return DEFAULT_WORKER_KEY
    if isinstance(raw, str) and not raw.strip():
        return DEFAULT_WORKER_KEY
    if isinstance(raw, str):
        raw = raw.strip()
    return validate_worker_key(raw)
