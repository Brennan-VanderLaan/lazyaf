"""Backfill every array-only pipeline into a graph; add `definition_error` (12.8 P4).

Phase 12.8 retires the v1 array pipeline format. By the time this revision
runs, every WRITER already writes `steps_graph` and every READER has stopped
reading `steps` - but the rows already in the database still carry only the
array. This revision is what makes "no row lacks a graph" true, which is the
precondition for 0015 dropping the column and for the executor's v1 fork
being deleted.

Two things, and deliberately only two:

- `pipelines.definition_error` (Text, nullable). The channel a REFUSAL
  surfaces on (12.8 s1.7). `sync_repo_pipelines` swallows every parse
  exception into a `logger.warning` and keeps the STALE definition on
  purpose ("a broken CI file must not break the push"), so a conversion
  refusal landing there would be dark by construction. This column is set by
  `upsert_materialized_pipeline` when conversion refuses, cleared on a
  successful sync, served on `PipelineRead`, and read by both run guards - a
  pipeline carrying one refuses to START rather than silently running the
  definition it happened to have before.

- A backfill of `steps` -> `steps_graph` for every row that has no graph.

**No column is dropped and no table is rebuilt.** This is a pure additive
ALTER plus an UPDATE, which is the lowest-risk shape available: `steps` is
never read destructively and never modified, so a row that has been
backfilled still carries its original array and nothing is lost. The column
drop is a SEPARATE revision (0015) that lands only after the acceptance gate
- that split is what buys R2 ("delete only after acceptance").

WHAT DOWNGRADE DOES, said plainly rather than implied (0007's register): it
drops `definition_error` and it does NOT un-backfill. A `steps_graph` value
does not record who wrote it, so NULLing the backfilled ones would mean
NULLing author-written graphs too - destroying real definitions to undo a
fill that cost nothing. Nothing is lost by leaving them: every backfilled
graph is a faithful rendering of the array still sitting in the same row.
The SCHEMA change here is fully reversible; the data fill is deliberately
one-way and harmless.

THE CONVERTER BELOW IS A FROZEN COPY AND MUST STAY ONE
------------------------------------------------------
`backend/alembic/env.py` puts `app` on the path, so
`from app.schemas.pipeline import array_to_graph` would WORK. The argument
against it is not that it fails - it is that it rots. A migration is a
historical record and must produce the same output for the same old row no
matter which commit the operator runs it from; `array_to_graph` is live code
that will keep changing to serve the two authoring edges, and 0015 may
delete the v1 vocabulary it speaks entirely. A revision that called it would
be a revision whose meaning changes retroactively. The chain's precedent is
unanimous: zero `from app` imports across all thirteen revisions, and 0012
spells out `DEFAULT_WORKER_KEY` for the same reason.

So `_array_to_graph` below is a deliberate, plain-dict, no-pydantic copy of
`app.schemas.pipeline.array_to_graph` as it stood when this revision was
written. **Do not refactor it to import the live one, and do not "fix" it
when the live one changes.** It must keep working after the live one is
gone. `tdd/integration/test_migrations_pipeline_retirement.py` pins that the
two agreed on the day this was authored, and its docstring says that the
correct response to that test going red is to delete the test, never to edit
this file.

FAITHFUL OR REFUSING, NEVER LOSSY
---------------------------------
v1's `on_success`/`on_failure` carried two things in one string: FLOW
(`next` -> an edge, `stop` -> the absence of one) and EFFECT (`merge:`,
`trigger:` -> the node's `actions`). A conversion that dropped the effect -
which is what `array_to_graph` did before 12.8 - is precisely the silent
capability loss this milestone exists to remove, and re-introducing it HERE,
in the one place that touches every historical row at once, would be the
worst possible place to re-introduce it. So an array this cannot represent
faithfully is collected and RAISED, naming the pipeline id, its name and the
reason. A half-converted definition is never written.

An unparseable `steps` with no graph is also a refusal. That value is
invisible to the running application today (`PipelineRead.parse_steps`,
`parse_steps` and `parse_steps_graph` all swallowed it to `[]`/`None`), so
this migration is the first thing that ever looks at it.

Guard note (same as 0002/0004/0005/0006/0007/0009/0010/0011/0012): a
pre-alembic database adopted at startup is healed by
`Base.metadata.create_all` - which builds the CURRENT model schema,
`definition_error` included - before it is stamped and upgraded. Every step
here is therefore guarded by a column-existence check and the revision is
re-runnable: the backfill only ever touches rows whose `steps_graph` is NULL
or empty, so a second run is a no-op.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-31

"""
import json
import logging
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0014'
down_revision: Union[str, Sequence[str], None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")


# =============================================================================
# FROZEN CONVERTER - a copy of app.schemas.pipeline.array_to_graph as it
# stood on 2026-08-31. See the module docstring: this must not be refactored
# into an import and must not be updated when the live one changes.
# =============================================================================

#: Must equal app.models.card.StepType's members. Spelled out because a
#: migration may not import live code, and because a row carrying a type
#: outside this set is one `PipelineGraphModel` would refuse to parse - so
#: converting it would produce a graph the application cannot read back.
_STEP_TYPES = ('agent', 'script', 'docker')

#: Must equal app.schemas.pipeline.TERMINAL_ACTION_PREFIXES.
_TERMINAL_ACTION_PREFIXES = ('trigger:', 'merge:')

_TERMINAL_VOCABULARY = "'trigger:{card_id}' or 'merge:{branch}'"

#: Defaults for the raw dict read. They must match PipelineStepConfig's
#: field defaults, because a persisted step may omit any of them and the
#: pydantic path would have supplied these.
_DEFAULT_ON_SUCCESS = 'next'
_DEFAULT_ON_FAILURE = 'stop'
_DEFAULT_TIMEOUT = 300
_DEFAULT_CONTINUE_IN_CONTEXT = False


class _ArrayConversionError(Exception):
    """A v1 array this migration cannot faithfully hold. Carries every reason.

    The frozen twin of `app.schemas.pipeline.ArrayConversionError`.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _describe_terminal_action(action: Any) -> str | None:
    """None when `action` is a dispatchable NODE action, else why it is not.

    Frozen copy of `app.schemas.pipeline.describe_terminal_action`.
    """
    if not isinstance(action, str):
        return (
            f"step action must be a string, got {type(action).__name__} "
            f"({action!r}); valid node actions are {_TERMINAL_VOCABULARY}"
        )
    if action in ('next', 'stop'):
        return (
            f"{action!r} is control FLOW, not a node action; express it with "
            f"a graph edge (or the absence of one). Valid node actions are "
            f"{_TERMINAL_VOCABULARY}"
        )
    if action.startswith('trigger:pipeline:'):
        return (
            "'trigger:pipeline:' is retired (12.8; it had no users and no "
            "execution test). Chain pipelines with a card_complete or push "
            f"trigger. Valid node actions are {_TERMINAL_VOCABULARY}"
        )
    for prefix in _TERMINAL_ACTION_PREFIXES:
        if action.startswith(prefix):
            if action[len(prefix):].strip():
                return None
            return (
                f"node action {action!r} names {prefix!r} with an empty "
                f"target; valid node actions are {_TERMINAL_VOCABULARY}"
            )
    return (
        f"unknown node action {action!r}; valid node actions are "
        f"{_TERMINAL_VOCABULARY}"
    )


def _step_name(step: dict, index: int) -> str:
    """A step's name for use in a message, never raising on a broken row."""
    name = step.get('name') if isinstance(step, dict) else None
    return name if isinstance(name, str) else f'<step #{index}>'


def _resolved_step_ids(steps: list[dict]) -> list[str]:
    """The graph node id for each array step: `step['id']`, else `step_{i}`.

    Frozen copy of `app.schemas.pipeline._resolved_step_ids`. Runs BEFORE
    anything is built because the graph's `steps` is a dict keyed by id: two
    steps resolving to one id would not be an error at all, it would be one
    step silently overwriting the other (R1).
    """
    authored: list[str | None] = []
    for step in steps:
        raw = step.get('id') if isinstance(step, dict) else None
        authored.append(raw if isinstance(raw, str) else None)

    resolved: list[str] = []
    reasons: list[str] = []

    for i, raw in enumerate(authored):
        if raw is None:
            resolved.append(f'step_{i}')
            continue
        if not raw.strip():
            reasons.append(
                f"step #{i} ({_step_name(steps[i], i)!r}) declares an empty "
                f"id ({raw!r}); give it a non-empty id, or omit `id` "
                f"entirely to get the generated 'step_{i}'"
            )
            # Keep positions aligned so later reasons still name the right
            # step; this list is discarded, we are already refusing.
            resolved.append(f'step_{i}')
            continue
        resolved.append(raw)

    first_seen: dict[str, int] = {}
    for i, step_id in enumerate(resolved):
        first = first_seen.get(step_id)
        if first is None:
            first_seen[step_id] = i
            continue
        if authored[first] and authored[i]:
            reasons.append(
                f"duplicate step id {step_id!r}: steps #{first} "
                f"({_step_name(steps[first], first)!r}) and #{i} "
                f"({_step_name(steps[i], i)!r}) both declare it, and a graph "
                "keys its steps by id"
            )
        else:
            authored_index = i if authored[i] else first
            generated_index = first if authored[i] else i
            reasons.append(
                f"step #{authored_index} "
                f"({_step_name(steps[authored_index], authored_index)!r}) "
                f"declares id {step_id!r}, which collides with the id "
                f"generated for step #{generated_index} "
                f"({_step_name(steps[generated_index], generated_index)!r}) "
                "- a step without an `id` becomes 'step_{index}'. Rename it, "
                f"or give step #{generated_index} an explicit id too"
            )

    if reasons:
        raise _ArrayConversionError(reasons)
    return resolved


def _describe_step_shape(step: Any, index: int) -> str | None:
    """None when the raw dict can become a graph node, else why it cannot.

    The pydantic path got this for free: `PipelineStepConfig` requires a
    `name` and a `type` inside `StepType`, and `PipelineStepV2` re-asserts
    both. Reading raw dicts, this migration has to say it out loud - and it
    must REFUSE rather than write a node the application would then fail to
    parse back out of the column.
    """
    if not isinstance(step, dict):
        return (
            f"step #{index} is not an object: {step!r}; a v1 step is a JSON "
            "object with at least `name` and `type`"
        )
    name = step.get('name')
    if not isinstance(name, str) or not name.strip():
        return (
            f"step #{index} declares no usable `name` ({name!r}); every "
            "graph node must be nameable"
        )
    step_type = step.get('type')
    if step_type not in _STEP_TYPES:
        return (
            f"step #{index} ({name!r}) declares type {step_type!r}, which is "
            f"not one of {', '.join(repr(t) for t in _STEP_TYPES)}"
        )
    return None


def _array_to_graph(steps: list[dict]) -> dict:
    """Convert a v1 array definition to the graph dict the executor runs.

    FROZEN COPY of `app.schemas.pipeline.array_to_graph` (12.8 s4.2/s4.7),
    written against plain dicts so this revision has no pydantic dependency
    and no live-code import. See the module docstring.

      * FLOW - `next` becomes an edge to the following step; `stop` becomes
        the absence of one. Flow lives on edges and only on edges.
      * EFFECT - `merge:{branch}` / `trigger:{card_id}` become entries in the
        node's `actions`, keyed by the same condition. v1's `_merge_branch`
        and `_trigger_card` BOTH continued to `current_step + 1` after
        firing, so the faithful rendering of a non-final effect is the action
        AND an edge. On the LAST step it is the action alone, since
        `_execute_step` guarded its continuation with
        `current_step + 1 < len(steps)`.

    Raises:
        _ArrayConversionError: with every reason it found.
    """
    if not steps:
        raise _ArrayConversionError([
            "cannot convert an empty steps array to a graph: a graph must "
            "have at least one entry point, so there is no such thing as an "
            "empty pipeline definition"
        ])

    shape_reasons = [
        problem
        for problem in (
            _describe_step_shape(step, i) for i, step in enumerate(steps)
        )
        if problem is not None
    ]
    if shape_reasons:
        # Before id resolution: a step that is not even an object cannot be
        # asked for its id, and a reason list that leads with a consequence
        # hides the cause.
        raise _ArrayConversionError(shape_reasons)

    resolved = _resolved_step_ids(steps)

    graph_steps: dict[str, dict] = {}
    edges: list[dict] = []
    reasons: list[str] = []
    #: Did step i emit ANY edge to step i+1? In an array conversion every
    #: edge runs i -> i+1 and step 0 is the sole entry point, so this is the
    #: only thing that can reach step i+1. Recorded as we build, and used
    #: ONLY to attribute an unreachable tail to the step that caused it.
    continues_to_next: list[bool] = [False] * len(steps)

    for i, step in enumerate(steps):
        step_id = resolved[i]
        next_id = resolved[i + 1] if i + 1 < len(steps) else None
        collected: dict[str, list[str]] = {'success': [], 'failure': []}

        raw_actions = (
            ('success', step.get('on_success', _DEFAULT_ON_SUCCESS)),
            ('failure', step.get('on_failure', _DEFAULT_ON_FAILURE)),
        )
        for condition, action in raw_actions:
            if action == 'stop':
                # Flow: this outcome ends the run. No edge. Whether that
                # orphans the tail is decided below, not guessed at here.
                continue

            if action == 'next':
                # Flow: continue. On the LAST step v1's own continuation
                # guard made this a no-op, so it is no-edge-and-no-refusal
                # here too - every persisted pipeline in this repo ends on
                # exactly this shape and they must all stay convertible.
                if next_id is not None:
                    edges.append({
                        'id': f'edge_{i}_{condition}',
                        'from_step': step_id,
                        'to_step': next_id,
                        'condition': condition,
                    })
                    continues_to_next[i] = True
                continue

            # Effect.
            problem = _describe_terminal_action(action)
            if problem is not None:
                reasons.append(
                    f"step '{step_id}' (#{i}, {_step_name(step, i)!r}) "
                    f"declares on_{condition}={action!r}: {problem}"
                )
                continue

            collected[condition].append(action)
            if next_id is not None:
                # v1 fired the effect and then ran the next step. Dropping
                # the edge here would silently truncate the pipeline.
                edges.append({
                    'id': f'edge_{i}_{condition}',
                    'from_step': step_id,
                    'to_step': next_id,
                    'condition': condition,
                })
                continues_to_next[i] = True

        config = step.get('config')
        timeout = step.get('timeout', _DEFAULT_TIMEOUT)
        continue_in_context = step.get(
            'continue_in_context', _DEFAULT_CONTINUE_IN_CONTEXT
        )
        graph_steps[step_id] = {
            'id': step_id,
            'name': step['name'],
            'type': step['type'],
            'config': config if isinstance(config, dict) else {},
            # Floats, matching PipelineNodePosition's `x: float` / `y: float`
            # so a backfilled graph is byte-identical to one the live
            # converter would have produced. Vertical layout.
            'position': {'x': 100.0, 'y': float(i * 150)},
            'timeout': timeout if isinstance(timeout, int) else _DEFAULT_TIMEOUT,
            'continue_in_context': bool(continue_in_context),
            'actions': {
                'success': collected['success'],
                'failure': collected['failure'],
                'always': [],
            },
        }

    if reasons:
        # Raise on the vocabulary before checking reachability: a refused
        # action emitted no edge, so its step would ALSO be reported as
        # orphaning the tail. That is a consequence, not a second cause, and
        # a reason list padded with consequences hides the one that matters.
        raise _ArrayConversionError(reasons)

    entry_points = [resolved[0]]
    graph = {
        'steps': graph_steps,
        'edges': edges,
        'entry_points': entry_points,
        'version': 2,
    }

    # The frozen twin of the `graph_definition_errors` call the live
    # converter makes. A linear array conversion cannot produce a dangling
    # edge, a self-edge or a cycle - every edge runs i -> i+1 over ids that
    # were just resolved - so the ONLY defect it can produce is an
    # unreachable step, which is what a mid-array `stop` does. Reachability
    # is computed by walking, not by trusting `continues_to_next`, so this
    # measures the graph that is about to be written rather than the
    # bookkeeping that built it.
    successors: dict[str, list[str]] = {}
    for edge in edges:
        successors.setdefault(edge['from_step'], []).append(edge['to_step'])
    reachable: set[str] = set()
    frontier = list(entry_points)
    while frontier:
        node = frontier.pop()
        if node in reachable:
            continue
        reachable.add(node)
        frontier.extend(successors.get(node, ()))

    orphans = [step_id for step_id in graph_steps if step_id not in reachable]
    if orphans:
        for i in range(len(steps) - 1):
            if continues_to_next[i]:
                continue
            step = steps[i]
            reasons.append(
                f"step '{resolved[i]}' (#{i}, {_step_name(step, i)!r}) "
                f"continues on neither outcome "
                f"(on_success={step.get('on_success', _DEFAULT_ON_SUCCESS)!r}, "
                f"on_failure={step.get('on_failure', _DEFAULT_ON_FAILURE)!r}), "
                f"which leaves the {len(steps) - i - 1} step(s) after it "
                f"unreachable - a v1 array reaches step #{i + 1} only from "
                f"step #{i}. Move the remaining steps into their own "
                f"pipeline, or let this one continue"
            )
        reasons.extend(
            f"step '{step_id}' is unreachable: no entry point names it and "
            "no edge leads to it"
            for step_id in orphans
        )
        raise _ArrayConversionError(reasons)

    return graph


# =============================================================================
# The revision
# =============================================================================

_REMEDY = (
    "Fix each definition above (edit the pipeline's `steps`, or give it a "
    "`steps_graph` directly) and run the upgrade again; or delete the "
    "pipeline if it is dead. 12.8 removed the array executor, so a "
    "definition that cannot be represented as a graph is one this system "
    "can no longer run - converting it anyway, by dropping the part that "
    "does not fit, is the exact silent capability loss this migration "
    "exists to prevent."
)


def _has_graph(value: Any) -> bool:
    """True when the row already carries a graph definition.

    The empty-string case matters: it is what the now-dead
    `Pipeline.has_graph_definition()` tested, and a row holding `''` has no
    graph however NOT NULL the column looks.
    """
    return isinstance(value, str) and value != ''


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns('pipelines')}

    if 'definition_error' not in columns:
        op.add_column(
            'pipelines', sa.Column('definition_error', sa.Text(), nullable=True)
        )

    if 'steps' not in columns:
        # Already past 0015, or an adopted database healed by create_all
        # after the column was retired. There is no array left to read, so
        # there is nothing to backfill - and saying so is better than a
        # SELECT that raises on a schema this revision is meant to tolerate.
        logger.info(
            "0014: `pipelines.steps` is absent, so there is no v1 array to "
            "backfill; only `definition_error` was ensured"
        )
        # ...but say whether that left anything undefined. This is the one
        # branch where the revision cannot do its job, so it must not exit
        # silently claiming it did: a row with no array and no graph is a
        # pipeline nothing can run, and the operator finding out here beats
        # finding out from a run that fails with "step definition not found".
        graphless = bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM pipelines "
                "WHERE steps_graph IS NULL OR steps_graph = ''"
            )
        ).scalar()
        if graphless:
            logger.warning(
                "0014: %s pipeline(s) have no graph and there is no `steps` "
                "column left to build one from. They are alive and editable "
                "but not runnable until something authors a definition",
                graphless,
            )
        return

    rows = bind.execute(
        sa.text("SELECT id, name, steps, steps_graph FROM pipelines")
    ).mappings().all()

    converted = 0
    skipped = 0
    left_null: list[str] = []
    refusals: list[str] = []

    for row in rows:
        pipeline_id = row['id']
        name = row['name']

        if _has_graph(row['steps_graph']):
            skipped += 1
            continue

        raw = row['steps']
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # No definition at all. There is no legal alternative to leaving
            # the graph NULL: an "empty graph" is unrepresentable by
            # construction (`validate_graph_integrity` rejects empty
            # `entry_points`), so the row stays alive and definition-less
            # rather than being invented into something runnable.
            left_null.append(f"{pipeline_id} ({name!r})")
            continue

        try:
            steps = json.loads(raw)
        except (TypeError, ValueError) as exc:
            refusals.append(
                f"  - {pipeline_id} ({name!r}): `steps` is not valid JSON "
                f"and the row has no `steps_graph` to fall back on: {exc}"
            )
            continue

        if not isinstance(steps, list):
            refusals.append(
                f"  - {pipeline_id} ({name!r}): `steps` is a "
                f"{type(steps).__name__}, not a JSON array, and the row has "
                "no `steps_graph` to fall back on"
            )
            continue

        if not steps:
            left_null.append(f"{pipeline_id} ({name!r})")
            continue

        try:
            graph = _array_to_graph(steps)
        except _ArrayConversionError as exc:
            refusals.append(
                f"  - {pipeline_id} ({name!r}): "
                + "; ".join(exc.reasons)
            )
            continue

        bind.execute(
            sa.text(
                "UPDATE pipelines SET steps_graph = :graph WHERE id = :id"
            ),
            {"graph": json.dumps(graph), "id": pipeline_id},
        )
        converted += 1

    if refusals:
        # Raised AFTER every row has been examined, so one upgrade attempt
        # names every definition that needs a human rather than making the
        # operator discover them one restart at a time. The UPDATEs already
        # issued roll back with the migration's own transaction.
        raise RuntimeError(
            f"0014: refusing to convert {len(refusals)} pipeline "
            "definition(s) to the graph format:\n"
            + "\n".join(refusals)
            + "\n"
            + _REMEDY
        )

    if converted or skipped or left_null:
        logger.info(
            "0014: backfilled %s pipeline definition(s) from the v1 array; "
            "%s already had a graph and were left byte-for-byte alone; %s had "
            "no definition at all and keep a NULL graph",
            converted,
            skipped,
            len(left_null),
        )
    if left_null:
        logger.warning(
            "0014: %s pipeline(s) carry no definition in either column and "
            "are left with steps_graph NULL: %s. They are alive and "
            "editable; they cannot be RUN until something authors a "
            "definition, which is what they were already doing",
            len(left_null),
            ", ".join(left_null),
        )

    in_flight = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM pipeline_runs "
            "WHERE status IN ('pending', 'running')"
        )
    ).scalar()
    if in_flight:
        logger.warning(
            "0014: %s pipeline run(s) are pending/running across this "
            "cutover. They started on the v1 array path, so their StepRuns "
            "carry step_id = NULL and they will fail with 'step definition "
            "not found' the next time they dispatch. They are NOT rewritten: "
            "backfilling step_runs.step_id = 'step_' || step_index is only "
            "correct if the pipeline still has the steps it had when the run "
            "started, and writing one that does not match today's graph "
            "would be this migration inventing history (the objection 0007 "
            "raised when it refused to relabel executor='legacy')",
            in_flight,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops `definition_error` and NOTHING ELSE. The backfill is deliberately
    not reversed: `steps_graph` does not record who wrote it, so undoing the
    fill would mean NULLing author-written graphs as well as backfilled
    ones - destroying real definitions to undo a fill that took nothing
    away. Every backfilled row still carries its original `steps` array,
    untouched, so the old shape is fully readable after this runs.
    """
    inspector = sa.inspect(op.get_bind())
    columns = {col['name'] for col in inspector.get_columns('pipelines')}

    if 'definition_error' in columns:
        # batch mode for SQLite, matching 0007's drop idiom. `pipelines`
        # carries no indexes; the inbound FK from pipeline_runs.pipeline_id
        # is re-established by the rebuild.
        with op.batch_alter_table('pipelines') as batch_op:
            batch_op.drop_column('definition_error')
