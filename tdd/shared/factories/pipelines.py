"""Graph pipeline fixtures - the ONE place a test builds a v2 definition.

Phase 12.8 retires the v1 `steps` array. Before this module, ten near-identical
pipeline fixtures existed across `tdd/integration/services/**` and
`tdd/unit/services/**` - seven byte-identical `make_repo_and_pipeline`s, plus
`make_linear_pipeline`, `make_pipeline` and a one-step variant - each of them
doing `Pipeline(..., steps=json.dumps(steps))`. Converting them one at a time
would have produced ten bespoke graph builders and ten chances to get the
edges wrong.

`linear_graph` is the load-bearing piece: it takes the SAME `list[dict]` those
helpers already took, so every fixture keeps its existing literal step list
unchanged and only the persist line moves.

WHY THIS IS NOT `app.schemas.pipeline.array_to_graph`
-----------------------------------------------------
It would be one import. It is deliberately not, for two reasons:

1. Coupling every fixture in the tree to the production converter means a
   converter defect makes every fixture wrong in the SAME direction, and the
   converter's own unit suite is then the only thing left checking it. This
   is a second, independent rendering of the same rules; the two disagreeing
   is a signal, and `tdd/unit/shared/test_graph_fixture.py` is where that
   signal fires.
2. `array_to_graph` reaches into `app.services.pipeline_executor` for
   `graph_definition_errors`. A fixture helper that drags the executor's
   import graph (websocket, git_server, workspace, model endpoints) into every
   test module that wants two script steps is not a fixture helper.

So this module is pure: dicts in, dicts out, no DB, no executor, no pydantic.
`make_repo_and_graph_pipeline` is the only thing here that touches a session.

WHAT IT REFUSES
---------------
Every refusal below is a case where rendering "something" would be a silently
wrong fixture - a test that passes while asserting nothing (R1/R4). An unknown
key is the sharpest of them: `json.dumps(steps)` used to carry any key
straight into the column, where `PipelineStepConfig` quietly dropped the ones
it did not know. A graph node is a closed shape, so a typo'd `timout=5` has to
be a loud error here or it becomes a 300-second default nobody notices.
"""
import json
from typing import Any

# app.models is already on sys.path by the time this module is imported:
# `factories/__init__` imports `.models` first, and that is where the backend
# path insertion lives.
from app.models import Repo
from app.models.pipeline import Pipeline

from .base import generate_uuid

#: Keys a v1 step dict may carry that describe the NODE. Mirrors
#: `PipelineStepV2`'s field set minus `actions` (which this helper derives
#: from `on_success`/`on_failure`, never accepts directly - a fixture that
#: wants to hand-author `actions` is authoring a graph and should write the
#: dict itself).
_NODE_KEYS = frozenset({"id", "name", "type", "config", "position", "timeout",
                        "continue_in_context"})
#: Keys that describe FLOW, consumed into edges and actions.
_FLOW_KEYS = frozenset({"on_success", "on_failure"})

#: v1's own defaults, from `PipelineStepConfig`. Repeated rather than imported
#: for the reason in the module docstring.
_DEFAULT_ON_SUCCESS = "next"
_DEFAULT_ON_FAILURE = "stop"
_DEFAULT_TIMEOUT = 300

_TERMINAL_ACTION_PREFIXES = ("trigger:", "merge:")


class GraphFixtureError(ValueError):
    """A step list this helper will not render, and why.

    Distinct from `app.schemas.pipeline.ArrayConversionError` on purpose: this
    one means a TEST is malformed, not that a user's pipeline is.
    """


def _resolve_ids(steps: list[dict], ids: list[str] | None) -> list[str]:
    if ids is not None:
        if len(ids) != len(steps):
            raise GraphFixtureError(
                f"ids has {len(ids)} entries for {len(steps)} steps"
            )
        resolved = list(ids)
    else:
        resolved = [
            str(step["id"]) if step.get("id") else f"step_{i}"
            for i, step in enumerate(steps)
        ]

    seen: dict[str, int] = {}
    for i, step_id in enumerate(resolved):
        if not step_id.strip():
            raise GraphFixtureError(f"step #{i} resolved to an empty id")
        if step_id in seen:
            raise GraphFixtureError(
                f"step #{i} and step #{seen[step_id]} both resolve to id "
                f"{step_id!r}; graph nodes are keyed by id, so one would "
                f"silently overwrite the other"
            )
        seen[step_id] = i
    return resolved


def linear_graph(
    steps: list[dict],
    *,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """A v2 graph dict from the v1 `list[dict]` the old fixtures already took.

    Flow lives on edges, effects live on the node (12.8 §1.2):

      * ``on_success``/``on_failure`` == ``"next"``  -> an edge of that
        condition to the following step. On the LAST step it is a no-op, the
        same no-op v1's `current_step + 1 < len(steps)` guard made of it.
      * ``"stop"`` -> no edge.
      * ``"merge:{branch}"`` / ``"trigger:{card_id}"`` -> an entry in the
        node's ``actions`` under that condition, PLUS the edge, because v1's
        `_merge_branch` and `_trigger_card` both continued to the next step
        after firing. On the last step it is the action alone.

    Args:
        steps: v1 step dicts. `name` and `type` are required; `config`,
            `timeout`, `continue_in_context`, `position`, `id`, `on_success`
            and `on_failure` are optional and take v1's defaults.
        ids: explicit node ids, one per step. Overrides any `id` key. Use it
            when a test asserts on step ids it did not want to write into the
            step dicts themselves.

    Raises:
        GraphFixtureError: on an empty list, an unknown key, a missing
            `name`/`type`, duplicate ids, an unknown action, or a non-final
            step that continues on neither outcome (which would leave the tail
            unreachable and make the run fail for a reason the test did not
            intend).
    """
    if not steps:
        raise GraphFixtureError(
            "cannot build a graph from an empty step list: a graph needs at "
            "least one entry point. A pipeline that never executes a step "
            "does not need a definition at all - leave it unset."
        )

    resolved = _resolve_ids(steps, ids)
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    #: Did step i emit any edge to step i+1? In a linear render that is the
    #: only thing that can reach step i+1, so it is what decides whether the
    #: tail is orphaned.
    continues: list[bool] = [False] * len(steps)

    for i, step in enumerate(steps):
        unknown = set(step) - _NODE_KEYS - _FLOW_KEYS
        if unknown:
            raise GraphFixtureError(
                f"step #{i} carries unknown key(s) {sorted(unknown)}. A graph "
                f"node is a closed shape; the v1 array silently dropped keys "
                f"it did not know and that is exactly the silence 12.8 is "
                f"removing. Known keys: {sorted(_NODE_KEYS | _FLOW_KEYS)}"
            )
        for required in ("name", "type"):
            if not step.get(required):
                raise GraphFixtureError(
                    f"step #{i} has no {required!r}: {step!r}"
                )

        step_id = resolved[i]
        next_id = resolved[i + 1] if i + 1 < len(steps) else None
        actions: dict[str, list[str]] = {"success": [], "failure": [], "always": []}

        for condition, action in (
            ("success", step.get("on_success", _DEFAULT_ON_SUCCESS)),
            ("failure", step.get("on_failure", _DEFAULT_ON_FAILURE)),
        ):
            if action == "stop":
                continue
            if action == "next":
                if next_id is not None:
                    edges.append({
                        "id": f"edge_{i}_{condition}",
                        "from_step": step_id,
                        "to_step": next_id,
                        "condition": condition,
                    })
                    continues[i] = True
                continue
            if isinstance(action, str) and action.startswith("trigger:pipeline:"):
                # Retired at 12.8 (§1.5) - `describe_terminal_action` refuses
                # it by name, so the fixture helper must too, or a test could
                # persist a graph the boundary would have rejected.
                raise GraphFixtureError(
                    f"step #{i} ({step_id!r}) declares on_{condition}="
                    f"{action!r}: 'trigger:pipeline:' is retired (12.8). Chain "
                    f"pipelines with a card_complete or push trigger."
                )
            if not isinstance(action, str) or not any(
                action.startswith(p) and action[len(p):].strip()
                for p in _TERMINAL_ACTION_PREFIXES
            ):
                raise GraphFixtureError(
                    f"step #{i} ({step_id!r}) declares on_{condition}="
                    f"{action!r}, which is not flow ('next'/'stop') and not a "
                    f"node action ('trigger:{{card_id}}' or 'merge:{{branch}}')"
                )
            actions[condition].append(action)
            if next_id is not None:
                edges.append({
                    "id": f"edge_{i}_{condition}",
                    "from_step": step_id,
                    "to_step": next_id,
                    "condition": condition,
                })
                continues[i] = True

        node: dict[str, Any] = {
            "id": step_id,
            "name": step["name"],
            "type": step["type"],
            "config": step.get("config") or {},
            "position": step.get("position") or {"x": 100, "y": i * 150},
            "timeout": step.get("timeout", _DEFAULT_TIMEOUT),
            "continue_in_context": step.get("continue_in_context", False),
            "actions": actions,
        }
        nodes[step_id] = node

    for i in range(len(steps) - 1):
        if not continues[i]:
            raise GraphFixtureError(
                f"step #{i} ({resolved[i]!r}) continues on neither outcome "
                f"(on_success={steps[i].get('on_success', _DEFAULT_ON_SUCCESS)!r}, "
                f"on_failure={steps[i].get('on_failure', _DEFAULT_ON_FAILURE)!r}), "
                f"which leaves the {len(steps) - i - 1} step(s) after it "
                f"unreachable. The executor's coverage check FAILS such a "
                f"run, so this fixture would be red for a reason the test "
                f"never meant to assert."
            )

    return {
        "version": 2,
        "steps": nodes,
        "edges": edges,
        "entry_points": [resolved[0]],
    }


def graph_pipeline_payload(
    steps: list[dict],
    *,
    name: str,
    description: str | None = None,
    is_template: bool = False,
    ids: list[str] | None = None,
) -> dict[str, Any]:
    """A POST /api/repos/{id}/pipelines body carrying a graph, not an array.

    Emits `steps_graph` and NOT `steps`: 12.8 §4.4 makes a body carrying both
    a 422, so a payload builder that always filled in `steps` would make every
    graph create refuse.
    """
    return {
        "name": name,
        "description": description if description is not None else f"{name} (graph fixture)",
        "steps_graph": linear_graph(steps, ids=ids),
        "is_template": is_template,
    }


def graph_json(steps: list[dict], *, ids: list[str] | None = None) -> str:
    """`linear_graph` serialized for the `pipelines.steps_graph` column.

    The column holds a JSON string, not JSON. Every persist site needs this,
    so it lives here rather than as ten `json.dumps(linear_graph(...))`.
    """
    return json.dumps(linear_graph(steps, ids=ids))


async def make_repo_and_graph_pipeline(
    factory,
    steps: list[dict],
    *,
    name: str = "test-pipeline",
    repo_name: str = "test-repo",
    default_branch: str = "main",
    ids: list[str] | None = None,
) -> tuple[Repo, Pipeline]:
    """Persist a repo and a graph pipeline through `factory`, an async session maker.

    Replaces the ten copies of `make_repo_and_pipeline`. `Pipeline.steps` is
    deliberately NOT passed: the column is `nullable=False` with a python-side
    `default="[]"`, so omitting it writes the empty array the row will keep
    until 12.8 P6 drops the column - and it means P6 has nothing to edit here.
    """
    async with factory() as db:
        repo = Repo(
            id=generate_uuid(),
            name=repo_name,
            default_branch=default_branch,
            is_ingested=True,
        )
        pipeline = Pipeline(
            id=generate_uuid(),
            repo_id=repo.id,
            name=name,
            steps_graph=graph_json(steps, ids=ids),
        )
        db.add(repo)
        db.add(pipeline)
        await db.commit()
        await db.refresh(repo)
        await db.refresh(pipeline)
        return repo, pipeline
