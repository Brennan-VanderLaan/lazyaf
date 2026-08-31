import json
from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import inspect as sa_inspect

from app.models.card import StepType
from app.models.pipeline import ExecutorMode, RunStatus
from app.schemas._datetime import UTCDateTime
from app.schemas._patch import not_null
from app.schemas._strings import Body, Name


# =============================================================================
# Graph-Based Pipeline Models (Phase 1: Graph Creep)
# =============================================================================

class EdgeCondition(str, Enum):
    """Condition that determines when an edge is followed."""
    SUCCESS = "success"
    FAILURE = "failure"
    ALWAYS = "always"


#: Terminal actions a NODE may fire when it completes. Deliberately NOT the
#: v1 vocabulary: `next` and `stop` are FLOW and flow lives on edges. What is
#: left is pure side effect, which is why these are a LIST - v1 could not say
#: "merge and also spawn a fix card", because it had one string for both the
#: effect and the continuation.
TERMINAL_ACTION_PREFIXES = ("trigger:", "merge:")

_TERMINAL_VOCABULARY = "'trigger:{card_id}' or 'merge:{branch}'"


def describe_terminal_action(action: Any) -> str | None:
    """None when `action` is a dispatchable NODE action, else why it is not.

    The graph twin of `describe_step_action` (pipeline_executor), and the
    SINGLE definition of the node-action vocabulary (R3): the schema
    validator below and the executor's dispatcher both call this, so a typo
    is a 422 at the boundary and a named run failure at run time - never a
    silent no-op.
    """
    if not isinstance(action, str):
        return (
            f"step action must be a string, got {type(action).__name__} "
            f"({action!r}); valid node actions are {_TERMINAL_VOCABULARY}"
        )
    if action in ("next", "stop"):
        return (
            f"{action!r} is control FLOW, not a node action; express it with "
            f"a graph edge (or the absence of one). Valid node actions are "
            f"{_TERMINAL_VOCABULARY}"
        )
    if action.startswith("trigger:pipeline:"):
        return (
            "'trigger:pipeline:' is retired (12.8; it had no users and no "
            "execution test). Chain pipelines with a card_complete or push "
            f"trigger. Valid node actions are {_TERMINAL_VOCABULARY}"
        )
    for prefix in TERMINAL_ACTION_PREFIXES:
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


class StepActions(BaseModel):
    """Side effects a node fires when it completes.

    Keyed by the SAME condition vocabulary the edges use (EdgeCondition), so
    the system has one notion of "when" (R3). Actions are effects ONLY: they
    never decide what runs next and they never complete the run. The
    executor fires them, then continues its normal fan-out and its normal
    `_check_all_steps_passed` verdict.
    """
    success: list[str] = []
    failure: list[str] = []
    always: list[str] = []

    @field_validator("success", "failure", "always")
    @classmethod
    def _closed_vocabulary(cls, v: list[str]) -> list[str]:
        for action in v:
            problem = describe_terminal_action(action)
            if problem is not None:
                raise ValueError(problem)
        return v


class PipelineNodePosition(BaseModel):
    """UI position for node graph rendering."""
    x: float
    y: float


class PipelineEdge(BaseModel):
    """Connection between two steps in the pipeline graph."""
    id: str  # Unique edge ID
    from_step: str  # Source step ID
    to_step: str  # Target step ID
    condition: EdgeCondition = EdgeCondition.SUCCESS  # When this edge is followed


class PipelineStepV2(BaseModel):
    """Graph-based step definition with stable ID and position."""
    id: str  # Stable unique identifier (UUID or user-defined)
    name: str
    type: StepType  # script | docker | agent
    config: dict[str, Any] = {}  # Type-specific configuration
    position: Optional[PipelineNodePosition] = None  # UI layout position
    timeout: int = 300  # Seconds
    continue_in_context: bool = False  # If true, next step runs with preserved workspace
    # Side effects this node fires when it completes (12.8). NOT named
    # `on_success`/`on_failure`: routers/pipelines.export_pipeline_yaml
    # already writes those keys on a graph step meaning "the id of the node
    # this success edge points at", so reusing them would put two
    # vocabularies behind one key (R3). Namespacing under `actions.` also
    # makes the condition vocabulary the SAME one the edges use.
    #
    # Purely additive: an absent `actions` is an empty StepActions, so every
    # graph written before this field existed keeps its exact meaning.
    actions: StepActions = Field(default_factory=StepActions)


class PipelineGraphModel(BaseModel):
    """Graph-based pipeline definition supporting parallel execution."""
    steps: dict[str, PipelineStepV2]  # Keyed by step ID
    edges: list[PipelineEdge]
    entry_points: list[str]  # Step IDs that start execution
    version: int = 2  # Schema version for migration

    @model_validator(mode="after")
    def validate_graph_integrity(self):
        """Validate that all edge references exist and entry points are valid."""
        step_ids = set(self.steps.keys())

        # Validate edges reference existing steps
        for edge in self.edges:
            if edge.from_step not in step_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent from_step: '{edge.from_step}'")
            if edge.to_step not in step_ids:
                raise ValueError(f"Edge '{edge.id}' references non-existent to_step: '{edge.to_step}'")

        # Validate entry points exist
        if not self.entry_points:
            raise ValueError("Pipeline must have at least one entry point")

        for entry in self.entry_points:
            if entry not in step_ids:
                raise ValueError(f"Entry point '{entry}' references non-existent step")

        return self

    def get_successors(self, step_id: str, condition: EdgeCondition) -> list[str]:
        """Get step IDs that follow the given step under the specified condition."""
        return [
            edge.to_step
            for edge in self.edges
            if edge.from_step == step_id and edge.condition == condition
        ]

    def get_predecessors(self, step_id: str) -> list[str]:
        """Get step IDs that must complete before the given step can execute."""
        return [edge.from_step for edge in self.edges if edge.to_step == step_id]

    def get_all_successors(self, step_id: str) -> list[str]:
        """Get all step IDs that follow the given step (any condition)."""
        return [edge.to_step for edge in self.edges if edge.from_step == step_id]


# =============================================================================
# Legacy Pipeline Models (Backwards Compatible)
# =============================================================================


class TriggerConfig(BaseModel):
    """Configuration for automatic pipeline triggers."""
    type: str  # "card_complete" | "push"
    config: dict[str, Any] = {}  # Type-specific config
    enabled: bool = True
    on_pass: str = "nothing"  # "nothing" | "merge" | "merge:{branch}"
    on_fail: str = "nothing"  # "nothing" | "fail" | "reject"

    # card_complete config: {status: "done" | "in_review"}
    # push config: {branches: ["main", "dev"]}
    #
    # on_pass actions:
    #   "nothing" - leave card as-is
    #   "merge" - approve and merge the card to default branch
    #   "merge:{branch}" - approve and merge the card to specified branch
    #
    # on_fail actions:
    #   "nothing" - leave card as-is
    #   "fail" - mark card as failed (user can retry)
    #   "reject" - reject card back to todo


class PipelineStepConfig(BaseModel):
    """Configuration for a pipeline step (stored in Pipeline.steps JSON array)."""
    # Stable id for the graph node this step becomes (12.8, §1.6b). Optional
    # because the v1 array never had one and every persisted row predates it;
    # `array_to_graph` falls back to `step_{index}`. It exists so the ids an
    # author already writes in `.lazyaf/pipelines/*.yaml` (PipelineStepYaml.id
    # has always accepted them) SURVIVE the conversion instead of being
    # renamed to `step_0..step_9` - node ids are the context-directory names,
    # the debug breakpoint keys and what a human reads in the graph.
    id: str | None = None
    name: str
    type: StepType
    config: dict[str, Any] = {}  # Type-specific: {command}, {image, command}, {runner_type, title, description}
    on_success: str = "next"  # "next" | "stop" | "trigger:{card_id}" | "merge:{branch}"
    on_failure: str = "stop"  # "next" | "stop" | "trigger:{card_id}"
    timeout: int = 300  # Seconds
    continue_in_context: bool = False  # If true, next step runs in same container with preserved workspace


class PipelineBase(BaseModel):
    # Bare `str` on purpose: PipelineRead inherits this and must keep
    # serializing rows written before the bound existed. The bound goes on
    # the INPUT schemas below. See app/schemas/_strings.py.
    name: str
    description: str | None = None


#: Refusal text for a body that carries BOTH dialects (12.8 §4.4). One
#: definition per request: `steps` is the v1 ARRAY authoring dialect, which
#: the boundary converts with `array_to_graph`; `steps_graph` is the
#: execution definition itself. Before 12.8 the router setattr'd them
#: independently, so `{"steps": [...], "steps_graph": {...}}` persisted an
#: array the executor never read while the graph decided the run - the user's
#: edit silently did nothing (R1). Refusing is the honest answer, and it is
#: the same answer on POST and on PATCH so a client has one rule to learn.
#:
#: WORDED IDENTICALLY to `routers/pipelines.graph_from_request`'s refusal, on
#: purpose and only for as long as both exist. The two are the SAME RULE in
#: two places (R3) and one of them has to go - see the handoff note. Until
#: someone collapses them, this validator runs during body parsing and
#: therefore always wins, so the router's branch is unreachable; keeping the
#: sentence identical means a caller and a test see one claim either way.
_BOTH_DIALECTS = (
    "send either `steps` (the v1 array, converted to a graph at this "
    "boundary) or `steps_graph` (the graph itself), not both: only one of "
    "them can be the definition, and writing both means the one the executor "
    "does not read is silently discarded. Omit `steps` to keep the graph you "
    "sent, or omit `steps_graph` to have the array converted."
)


def _refuse_both_dialects(steps, steps_graph) -> None:
    """Raise when a body carries BOTH a real array and a real graph.

    "Carries" means CARRIES A DEFINITION, not "the key is present": the
    pipeline editor and `tdd/qa/qa3_support.graph_pipeline` both post
    `{"steps": [], "steps_graph": {...}}` because `steps` used to be a NOT
    NULL column that every writer had to name. An empty array is not a
    second definition, so it is not a conflict.
    """
    if steps and steps_graph is not None:
        raise ValueError(_BOTH_DIALECTS)


class PipelineCreate(PipelineBase):
    name: Name
    description: Body | None = None
    steps: list[PipelineStepConfig] = []
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    triggers: list[TriggerConfig] = []
    is_template: bool = False

    @model_validator(mode="after")
    def validate_steps_definition(self):
        """One definition dialect per request (12.8 §4.4).

        Was an explicit no-op that computed two booleans and returned. The
        comment said "if graph is provided, it takes precedence", but nothing
        implemented that and `create_pipeline` wrote both columns.
        """
        _refuse_both_dialects(self.steps, self.steps_graph)
        return self


class PipelineUpdate(BaseModel):
    name: Name | None = None
    description: Body | None = None
    steps: list[PipelineStepConfig] | None = None
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    triggers: list[TriggerConfig] | None = None
    is_template: bool | None = None

    # pipelines.description and .steps_graph are nullable; the rest are
    # NOT NULL and an explicit null used to reach the column as a 500.
    #
    # `steps` LEFT this list at 12.8 P3 and gained `_steps_is_never_null`
    # below instead. It is no longer a column-backed field: the boundary
    # converts it into `steps_graph` and never writes `pipelines.steps`, so
    # `not_null`'s premise ("this maps to a NOT NULL column") stopped being
    # true here - and stays untrue after P6 drops the column, at which point
    # keeping it in this list would read as guarding a column that no longer
    # exists. The WIRE BEHAVIOUR is deliberately unchanged: an explicit
    # `null` was a 422 before and is a 422 now.
    _reject_nulls = not_null("name", "triggers", "is_template")

    @field_validator("steps", mode="before")
    @classmethod
    def _steps_is_never_null(cls, v):
        """`{"steps": null}` is refused, naming `steps` (12.8 P3).

        Not folded into the `_refuse_both_dialects` model validator: a
        cross-field rule reports at the model's `loc`, and a client that sent
        one bad field deserves to be told WHICH one. `mode="before"` keeps
        the refusal ahead of type coercion, exactly as `not_null` does.

        Null is not a way to clear a definition. There is no such thing as a
        pipeline with no definition - `array_to_graph` refuses an empty array
        and `PipelineGraphModel` refuses empty entry points - so the only
        honest reading of `null` here is "I meant to leave this alone", and
        that is spelled by omitting the field.
        """
        if v is None:
            raise ValueError(
                "'steps' cannot be null; omit the field to leave the "
                "definition unchanged, or send a non-empty array (or "
                "`steps_graph`) to replace it"
            )
        return v

    @model_validator(mode="after")
    def validate_steps_definition(self):
        """One definition dialect per request (12.8 §4.4). Same rule as POST."""
        _refuse_both_dialects(self.steps, self.steps_graph)
        return self


class PipelineRead(PipelineBase):
    """What a pipeline looks like on the wire. GRAPH ONLY, since 12.8 P3.

    `steps` is DELETED, not made optional (§4.3). Its `= []` default was the
    R1 hazard of the whole retirement concentrated in one line: a graph
    pipeline, an unparseable row and a row with no definition at all all
    serialized as `steps: []`, so every failure looked exactly like an empty
    pipeline. Deleting the field is what makes `PipelinesPage`'s live "0
    steps for every graph pipeline" bug surface and what makes
    `verify_executor`'s vacuous-pass hole impossible to leave unfixed.

    A derived array projection was considered and rejected: it can only exist
    for a linear graph, so a fan-out would force it to lie or to refuse, and
    a wire field that conditionally refuses is worse than no field.

    The ARRAY authoring dialect is still readable - from
    `GET /api/repos/{id}/lazyaf/pipelines`, which serves the authoring FILE.
    That is the R3-clean split: array = authoring, graph = execution,
    different endpoints.
    """
    id: str
    repo_id: str
    steps_graph: Optional[PipelineGraphModel] = None  # Graph-based definition (v2)
    # Why a definition failed to materialize, or None when it did (12.8,
    # §1.7). `sync_repo_pipelines` swallows every parse exception into a
    # logger.warning and keeps the STALE definition on purpose ("a broken CI
    # file must not break the push"), so a conversion REFUSAL landing there
    # would be dark by construction - the whole "refuse loudly" strategy
    # would become an R1 violation with no channel to surface on. This is
    # that channel: set when conversion refuses, cleared on a successful
    # sync, rendered as a badge, and read by the run guards.
    #
    # Reads `None` until the column lands (the additive ALTER is B3's, and
    # `from_attributes` falls back to the default for an attribute the ORM
    # does not declare yet) - so this is safe to land ahead of it, and is
    # pinned by a test.
    definition_error: str | None = None
    triggers: list[TriggerConfig] = []
    is_template: bool
    created_at: UTCDateTime
    updated_at: UTCDateTime

    @field_validator("steps_graph", mode="before")
    @classmethod
    def parse_steps_graph(cls, v):
        """Parse steps_graph from JSON string if needed."""
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                return PipelineGraphModel(**parsed) if parsed else None
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @field_validator("triggers", mode="before")
    @classmethod
    def parse_triggers(cls, v):
        """Parse triggers from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    class Config:
        from_attributes = True


class StepRunRead(BaseModel):
    id: str
    pipeline_run_id: str
    step_index: int
    step_id: str | None = None  # Graph step ID (v2 pipelines)
    step_name: str
    status: RunStatus
    job_id: str | None = None
    # Which execution path ran this step (R1). Typed as the ExecutorMode enum
    # (cross-file contract #3) so an off-vocabulary value is a loud
    # validation error, never a silently misread string.
    executor: ExecutorMode | None = None
    # Which RUNNER executed this step, when it went remote (12.6). Null on the
    # local path, where the backend spawned the container itself.
    #
    # `executor` says which CODE PATH ran; this says which MACHINE did. They
    # are different claims: a RemoteExecutor that gave up with "no runner
    # matched" still records executor='remote', so the dogfood gate
    # (scripts/verify_executor.py assertion 10) reads this field to tell a
    # completed remote assignment from a remote step that never found a home.
    # It is the assignment compare-and-swap's own output, read back.
    runner_id: str | None = None
    logs: str = ""
    error: str | None = None
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_runner_id(cls, value):
        """Take runner_id from the step's latest StepExecution.

        A StepRun can have several executions (retries, and a remote requeue
        after a runner death). The LAST one carrying a runner is the
        assignment that produced the terminal outcome - the one an operator,
        and `verify_executor.py` assertion 10, mean by "which runner ran
        this step".

        ONLY reads the relationship when it is already EAGER-LOADED. Touching
        an unloaded relationship inside a pydantic validator would emit lazy
        IO from a sync context and raise MissingGreenlet, turning an endpoint
        that never asked for this field into a 500. Endpoints that want the
        field say so with `selectinload(StepRun.executions)`; every other one
        serializes runner_id as None, exactly as it did before this existed.
        """
        if isinstance(value, dict):
            return value
        try:
            state = sa_inspect(value)
            if "executions" in state.unloaded:
                return value
            executions = state.dict.get("executions")
        except Exception:  # pragma: no cover - not an ORM instance
            return value

        runner_id = None
        for execution in executions or ():
            if execution.runner_id:
                runner_id = execution.runner_id
        if runner_id is None:
            return value
        data = {
            field: getattr(value, field, None)
            for field in cls.model_fields
            if field != "runner_id"
        }
        data["runner_id"] = runner_id
        return data

    class Config:
        from_attributes = True


class PipelineRunRead(BaseModel):
    id: str
    pipeline_id: str
    status: RunStatus
    trigger_type: str
    trigger_ref: str | None = None
    trigger_context: dict[str, Any] | None = None  # {branch, commit_sha, card_id, etc.}
    current_step: int
    steps_completed: int
    steps_total: int
    # Graph execution tracking (for parallel execution)
    active_step_ids: list[str] = []  # Steps currently executing in parallel
    completed_step_ids: list[str] = []  # Steps that have completed
    started_at: UTCDateTime | None = None
    completed_at: UTCDateTime | None = None
    created_at: UTCDateTime
    step_runs: list[StepRunRead] = []

    @field_validator("trigger_context", mode="before")
    @classmethod
    def parse_trigger_context(cls, v):
        """Parse trigger_context from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @field_validator("active_step_ids", mode="before")
    @classmethod
    def parse_active_step_ids(cls, v):
        """Parse active_step_ids from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v) or []
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    @field_validator("completed_step_ids", mode="before")
    @classmethod
    def parse_completed_step_ids(cls, v):
        """Parse completed_step_ids from JSON string if needed."""
        if isinstance(v, str):
            try:
                return json.loads(v) or []
            except (json.JSONDecodeError, TypeError):
                return []
        return v if v else []

    class Config:
        from_attributes = True


# =============================================================================
# Trigger vocabulary
# =============================================================================
#
# `PipelineRun.trigger_type` stopped being a free-text label in 12.5: it is
# the DURABLE ROUTING KEY `agent_run.on_run_complete` dispatches on when a run
# finishes. A run stamped `card_work` makes run completion write a Card's
# status, and `trigger_ref` names the card - so an unvalidated string on the
# public run endpoint let any caller drive an arbitrary card to in_review or
# failed by starting a pipeline of their own.
#
# Hence: a closed vocabulary here, and the ad-hoc subset refused by the public
# endpoint (routers/pipelines.run_pipeline) because only the internal ad-hoc
# path may stamp it.

#: Trigger types a caller may ask for on POST /api/pipelines/{id}/run.
PUBLIC_TRIGGER_TYPES = (
    "manual",
    "webhook",
    "card",
    "card_complete",
    "push",
    "schedule",
    "pipeline",
)

#: Trigger types owned by app.services.agent_run (kept in sync with its
#: ADHOC_TRIGGER_TYPES). Settable ONLY by the internal ad-hoc run path.
ADHOC_TRIGGER_TYPES = ("card_work", "playground", "experiment")

#: Stamped ONLY by routers/debug.create_debug_rerun (12.7). Not settable on
#: the public run endpoint: a debug re-run deliberately drops on_pass/on_fail
#: and card routing, so letting a caller stamp it would be a way to launder a
#: run past its own trigger actions.
DEBUG_TRIGGER_TYPES = ("debug_rerun",)

#: Everything PipelineRun.trigger_type is allowed to hold.
KNOWN_TRIGGER_TYPES = PUBLIC_TRIGGER_TYPES + ADHOC_TRIGGER_TYPES + DEBUG_TRIGGER_TYPES


class PipelineRunCreate(BaseModel):
    """Parameters for starting a pipeline run."""
    trigger_type: str = "manual"
    trigger_ref: str | None = None
    trigger_context: dict[str, Any] | None = None  # {branch, commit_sha, card_id, etc.}
    params: dict[str, Any] | None = None  # Optional parameters passed to steps as env vars

    @field_validator("trigger_type")
    @classmethod
    def known_trigger_type(cls, v: str) -> str:
        """Reject anything outside the vocabulary (422).

        A typo used to be persisted verbatim and silently routed nowhere;
        now it is refused at the edge, and the message names the vocabulary
        so the caller does not have to go read the model.
        """
        if v not in KNOWN_TRIGGER_TYPES:
            raise ValueError(
                f"unknown trigger_type {v!r}; valid values are "
                + ", ".join(KNOWN_TRIGGER_TYPES)
            )
        return v


# =============================================================================
# Conversion Utilities
# =============================================================================

class ArrayConversionError(ValueError):
    """A v1 array the graph cannot faithfully hold. Carries every reason.

    Subclasses ValueError on purpose: `array_to_graph` has always raised one
    for the empty array, and pydantic turns a ValueError raised inside a
    validator into a 422 rather than a 500. `.reasons` is the list a caller
    renders (a `definition_error` badge, §1.7); `str(exc)` joins them.
    """

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def _resolved_step_ids(steps: list[PipelineStepConfig]) -> list[str]:
    """The graph node id for each array step: `step.id`, else `step_{i}`.

    Raises ArrayConversionError on an empty id, a duplicate, or an authored
    id that collides with the id generated for a step that has none. This
    runs BEFORE anything is built because `graph_steps` is a dict keyed by
    id: two steps resolving to one id would not be an error at all, it would
    be one step silently overwriting the other (R1).
    """
    authored = [
        (step.id if isinstance(step.id, str) else None) for step in steps
    ]
    resolved: list[str] = []
    reasons: list[str] = []

    for i, raw in enumerate(authored):
        if raw is None:
            resolved.append(f"step_{i}")
            continue
        if not raw.strip():
            reasons.append(
                f"step #{i} ({steps[i].name!r}) declares an empty id "
                f"({raw!r}); give it a non-empty id, or omit `id` entirely "
                f"to get the generated 'step_{i}'"
            )
            # Keep positions aligned so later reasons still name the right
            # step; this list is discarded, we are already refusing.
            resolved.append(f"step_{i}")
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
                f"({steps[first].name!r}) and #{i} ({steps[i].name!r}) both "
                "declare it, and a graph keys its steps by id"
            )
        else:
            authored_index = i if authored[i] else first
            generated_index = first if authored[i] else i
            reasons.append(
                f"step #{authored_index} ({steps[authored_index].name!r}) "
                f"declares id {step_id!r}, which collides with the id "
                f"generated for step #{generated_index} "
                f"({steps[generated_index].name!r}) - a step without an `id` "
                f"becomes 'step_{{index}}'. Rename it, or give step "
                f"#{generated_index} an explicit id too"
            )

    if reasons:
        raise ArrayConversionError(reasons)
    return resolved


def array_to_graph(steps: list[PipelineStepConfig]) -> PipelineGraphModel:
    """Convert a v1 array definition to the graph the executor runs.

    FAITHFUL OR REFUSING, never lossy (12.8 §1.6/§4.2). v1's `on_success` /
    `on_failure` carried two things in one string:

      * FLOW - `next` becomes an edge to the following step; `stop` becomes
        the absence of one. Flow lives on edges and only on edges.
      * EFFECT - `merge:{branch}` / `trigger:{card_id}` become entries in
        the node's `actions`, keyed by the same condition. v1's `_merge_branch`
        and `_trigger_card` BOTH continue to `current_step + 1` after firing,
        so the faithful rendering of a non-final effect is the action AND a
        success/failure edge. On the last step it is the action alone, since
        `_execute_step` guards its continuation with `current_step + 1 <
        len(steps)`.

    Everything else refuses, naming the step, the offending value and the
    vocabulary. Until 12.8 this function emitted an edge only for the literal
    string `"next"` and dropped `merge:` / `trigger:` on the floor - and its
    `if i < len(steps) - 1:` guard meant an action on the LAST step (the
    common "merge when this passes" shape) was never even examined. That
    silence is the R1 violation this whole retirement exists to remove, so
    the one thing this function may never do is convert something it cannot
    represent.

    Raises:
        ArrayConversionError: with every reason it found.
    """
    if not steps:
        raise ArrayConversionError([
            "cannot convert an empty steps array to a graph: a graph must "
            "have at least one entry point, so there is no such thing as an "
            "empty pipeline definition"
        ])

    resolved = _resolved_step_ids(steps)

    graph_steps: dict[str, PipelineStepV2] = {}
    edges: list[PipelineEdge] = []
    reasons: list[str] = []
    #: Did step i emit ANY edge to step i+1? In an array conversion every
    #: edge runs i -> i+1 and step 0 is the sole entry point, so this is the
    #: only thing that can reach step i+1. It is recorded here as we build
    #: rather than recomputed, and it is used ONLY to attribute a defect to
    #: the step that caused it - the decision to refuse comes from
    #: `graph_definition_errors` below, which is the one authority on what
    #: makes a graph runnable (R3).
    continues_to_next: list[bool] = [False] * len(steps)

    for i, step in enumerate(steps):
        step_id = resolved[i]
        next_id = resolved[i + 1] if i + 1 < len(steps) else None
        collected: dict[str, list[str]] = {"success": [], "failure": []}

        for condition, action in (
            (EdgeCondition.SUCCESS, step.on_success),
            (EdgeCondition.FAILURE, step.on_failure),
        ):
            if action == "stop":
                # Flow: this outcome ends the run. No edge. Whether that
                # orphans the tail is decided below, not guessed at here.
                continue

            if action == "next":
                # Flow: continue. On the LAST step v1's own continuation
                # guard made this a no-op, so it is no-edge-and-no-refusal
                # here too - the dogfood pipeline's tenth step is exactly
                # this shape and it must stay convertible.
                if next_id is not None:
                    edges.append(PipelineEdge(
                        id=f"edge_{i}_{condition.value}",
                        from_step=step_id,
                        to_step=next_id,
                        condition=condition,
                    ))
                    continues_to_next[i] = True
                continue

            # Effect. `describe_terminal_action` is the SINGLE definition of
            # the node-action vocabulary (R3), so a form this accepts is one
            # the executor's dispatcher can run, and a form it refuses gets
            # the same message here that a hand-authored graph gets at 422 -
            # including the `trigger:pipeline:` retirement notice.
            problem = describe_terminal_action(action)
            if problem is not None:
                reasons.append(
                    f"step '{step_id}' (#{i}, {step.name!r}) declares "
                    f"on_{condition.value}={action!r}: {problem}"
                )
                continue

            collected[condition.value].append(action)
            if next_id is not None:
                # v1 fired the effect and then ran the next step. Dropping
                # the edge here would silently truncate the pipeline.
                edges.append(PipelineEdge(
                    id=f"edge_{i}_{condition.value}",
                    from_step=step_id,
                    to_step=next_id,
                    condition=condition,
                ))
                continues_to_next[i] = True

        graph_steps[step_id] = PipelineStepV2(
            id=step_id,
            name=step.name,
            type=step.type,
            config=step.config,
            position=PipelineNodePosition(x=100, y=i * 150),  # Vertical layout
            timeout=step.timeout,
            continue_in_context=step.continue_in_context,
            # Constructed, not appended into: `StepActions._closed_vocabulary`
            # runs on construction and not on an in-place `.append`, and the
            # converter's output has to clear the same bar a hand-authored
            # graph does. `describe_terminal_action` has already accepted
            # every entry, so this can only fire if the two ever drift - at
            # which point it is a loud ValidationError here rather than an
            # un-dispatchable action persisted into a definition (R3).
            actions=StepActions(**collected),
        )

    if reasons:
        # Raise on the vocabulary before checking reachability: a refused
        # action emitted no edge, so its step would ALSO be reported as
        # orphaning the tail. That is a consequence, not a second cause, and
        # a reason list padded with consequences hides the one that matters.
        raise ArrayConversionError(reasons)

    graph = PipelineGraphModel(
        steps=graph_steps,
        edges=edges,
        entry_points=[resolved[0]],
        version=2,
    )

    # `graph_definition_errors` is the executor's definition-time check and
    # the single authority on whether a graph can run (R3) - imported
    # lazily-by-value, the way app/schemas/experiment.py and
    # app/schemas/model_endpoint.py already reach into app.services, so the
    # schema layer does not take a module-level dependency on the executor's
    # import graph. Running it HERE is what turns "a mid-array stop orphans
    # the tail" from a run that fails at execution time for the wrong reason
    # into a refusal at the boundary that names the step responsible.
    from app.services.pipeline_executor import graph_definition_errors

    defects = graph_definition_errors(graph.model_dump(mode="json"))
    if defects:
        for i in range(len(steps) - 1):
            if continues_to_next[i]:
                continue
            step = steps[i]
            reasons.append(
                f"step '{resolved[i]}' (#{i}, {step.name!r}) continues on "
                f"neither outcome (on_success={step.on_success!r}, "
                f"on_failure={step.on_failure!r}), which leaves the "
                f"{len(steps) - i - 1} step(s) after it unreachable - a v1 "
                f"array reaches step #{i + 1} only from step #{i}. Move the "
                f"remaining steps into their own pipeline, or let this one "
                f"continue"
            )
        reasons.extend(defects)
        raise ArrayConversionError(reasons)

    return graph
