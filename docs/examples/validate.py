#!/usr/bin/env python3
"""Validate every example in docs/examples/ against the real schema.

    cd backend && uv run python ../docs/examples/validate.py

WHY THIS EXISTS
    An example that does not parse is worse than no example: it costs a
    reader the time to find out. So the catalog is not asserted to be valid,
    it is CHECKED, against `app.schemas.lazyaf_yaml.PipelineYaml` - the same
    class `trigger_service.sync_repo_pipelines` constructs when a push lands.

WHAT IT CHECKS
  1. Every docs/examples/pipelines/*.yaml parses and validates as a
     PipelineYaml.
  2. Every `type: agent` step names an agent in the executor's vocabulary
     (pipeline_executor.DEFAULT_AGENT_IMAGE), and every `openai-harness`
     step names an endpoint the resolver can parse
     (model_endpoints.resolve.parse_endpoint_reference). There is no default
     for either, by design, so an example that omits one would fail at
     dispatch rather than at parse.
  3. Every example survives `schemas.pipeline.array_to_graph` - the 12.8
     converter, which is FAITHFUL OR REFUSING, and since 12.8 P5 the ONLY
     authority on the on_success / on_failure vocabulary: the executor's
     `describe_step_action` was deleted with the array path it guarded, and
     the converter refuses strictly more than it did (an unknown action, a
     retired `trigger:pipeline:`, an empty target, a duplicate id, AND a
     `stop` that orphans the tail, which the old check accepted). An example
     that cannot be converted is one that does not run.
  4. Every full pipeline YAML fenced in catalog.md appears VERBATIM in one of
     the files - the fences and the files cannot drift apart.
  5. Nothing in docs/examples/ has been copied into .lazyaf/pipelines/, which
     is live and runs on the next push.

Exit 0 = every example is valid. Exit 1 = at least one is not.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PIPELINES = HERE / "pipelines"
CATALOG = HERE / "catalog.md"
LIVE = REPO / ".lazyaf" / "pipelines"

sys.path.insert(0, str(REPO / "backend"))

from app.schemas.lazyaf_yaml import PipelineYaml  # noqa: E402
from app.schemas.pipeline import (  # noqa: E402
    ArrayConversionError,
    PipelineStepConfig,
    array_to_graph,
)
from app.services.pipeline_executor import (  # noqa: E402
    DEFAULT_AGENT_IMAGE,
    HARNESS_AGENT,
)
from app.services.model_endpoints.resolve import (  # noqa: E402
    parse_endpoint_reference,
)

FENCE = re.compile(r"^```yaml\n(.*?)^```$", re.DOTALL | re.MULTILINE)


def check_pipeline(label: str, text: str) -> list[str]:
    """Every problem with one pipeline document, as a list of strings."""
    problems: list[str] = []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"{label}: not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return [f"{label}: parses to {type(data).__name__}, not a mapping"]

    try:
        pipeline = PipelineYaml(**data)
    except Exception as exc:
        return [f"{label}: does not validate as PipelineYaml: {exc}"]

    for index, step in enumerate(pipeline.steps):
        where = f"{label}: step #{index} ({step.name!r})"
        if step.type not in ("script", "docker", "agent"):
            problems.append(
                f"{where}: unknown step type {step.type!r} "
                "(script, docker, agent)"
            )
        if step.type != "agent":
            continue
        agent = step.config.get("agent") or step.config.get("runner_type")
        if agent not in DEFAULT_AGENT_IMAGE:
            problems.append(
                f"{where}: agent {agent!r} is not in the executor's "
                f"vocabulary {sorted(DEFAULT_AGENT_IMAGE)}"
            )
        elif agent == HARNESS_AGENT and not parse_endpoint_reference(step.config):
            problems.append(
                f"{where}: agent {HARNESS_AGENT!r} names no endpoint "
                "(config.endpoint, or config.model: 'endpoint:<name>')"
            )

    # 3: the converter is the vocabulary authority as well as the shape one.
    try:
        array_to_graph(
            [PipelineStepConfig(**step.model_dump()) for step in pipeline.steps]
        )
    except ArrayConversionError as exc:
        for reason in exc.reasons:
            problems.append(f"{label}: array_to_graph refuses it: {reason}")
    except Exception as exc:  # a shape the converter did not expect at all
        problems.append(f"{label}: array_to_graph raised {exc!r}")

    return problems


def main() -> int:
    problems: list[str] = []

    files = sorted(PIPELINES.glob("*.yaml"))
    if not files:
        print(f"FAIL: no examples found in {PIPELINES}", file=sys.stderr)
        return 1

    bodies: dict[str, str] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        bodies[path.name] = text
        problems.extend(check_pipeline(path.name, text))

    # 4: the fences in the prose and the files on disk are the same bytes.
    fenced = 0
    if CATALOG.exists():
        catalog = CATALOG.read_text(encoding="utf-8")
        for match in FENCE.finditer(catalog):
            block = match.group(1)
            if not block.lstrip().startswith(("name:", "#")):
                continue  # a fragment, not a whole pipeline
            if "\nsteps:" not in block and not block.startswith("steps:"):
                continue
            fenced += 1
            problems.extend(check_pipeline(f"catalog.md fence #{fenced}", block))
            if not any(block in body for body in bodies.values()):
                problems.append(
                    f"catalog.md fence #{fenced} does not appear verbatim in "
                    "any docs/examples/pipelines/*.yaml - the prose and the "
                    "files have drifted"
                )

    # 5: an example must never be armed by accident.
    if LIVE.is_dir():
        for path in sorted(LIVE.glob("*.y*ml")):
            if path.read_text(encoding="utf-8") in bodies.values():
                problems.append(
                    f"{path} is a byte-for-byte copy of a docs/examples "
                    "pipeline. Files in .lazyaf/pipelines/ are LIVE and run on "
                    "the next push."
                )

    for problem in problems:
        print(f"  {problem}", file=sys.stderr)

    if problems:
        print(
            f"\nFAIL: {len(problems)} problem(s) across {len(files)} example "
            f"file(s) and {fenced} catalog fence(s).",
            file=sys.stderr,
        )
        return 1

    print(
        f"OK: {len(files)} example pipeline(s) and {fenced} catalog fence(s) "
        f"validate against PipelineYaml."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
