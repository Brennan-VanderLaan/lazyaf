"""
12.5's cross-agent contract #4 is still true after Milestone 14
(wave 8 cross-agent contract #6).

``ExecutorConfig`` and ``ExecutorResult`` GAIN NOTHING for the harness. The
harness takes its whole configuration — the endpoint block and the harness
budgets, both straight off the wire — through the ``EXECUTORS`` BUILDER,
exactly as ``ClaudeExecutor(output_format=...)`` and
``MockExecutor(mock_config=...)`` already do.

WHY THIS IS PINNED RATHER THAN TRUSTED: the executor dataclasses are the one
shape every runner image and every future agent shares. A field added here for
one agent's convenience is a field every other agent must then understand, and
12.5 spent a whole wave establishing that they hold exactly one new thing
(``ExecutorResult.usage``) and no more.
"""
import dataclasses
import inspect

from runner_common import agent_wrapper
from runner_common.executors import ExecutorConfig, ExecutorResult
from runner_common.harness import HarnessExecutor
from runner_common.harness.executor import RUNNER_TYPE

#: The 12.5 field sets, frozen. Changing either of these lists is a
#: cross-agent contract change and needs the wave that owns it.
EXECUTOR_CONFIG_FIELDS = (
    "workspace",
    "prompt",
    "model",
    "agents_json",
    "timeout",
    "env",
)
EXECUTOR_RESULT_FIELDS = (
    "success",
    "exit_code",
    "stdout",
    "stderr",
    "error",
    "usage",
)


def test_executor_config_gained_nothing():
    names = tuple(field.name for field in dataclasses.fields(ExecutorConfig))
    assert names == EXECUTOR_CONFIG_FIELDS


def test_executor_result_gained_nothing():
    names = tuple(field.name for field in dataclasses.fields(ExecutorResult))
    assert names == EXECUTOR_RESULT_FIELDS


def test_the_harness_takes_its_configuration_through_the_builder():
    signature = inspect.signature(HarnessExecutor.__init__)
    assert "endpoint" in signature.parameters
    assert "harness" in signature.parameters


def test_the_vocabulary_gained_exactly_one_entry_container_side():
    assert sorted(agent_wrapper.EXECUTORS) == [
        "claude-code",
        "gemini",
        "mock",
        "openai-harness",
    ]


def test_the_builder_hands_the_wire_blocks_straight_through():
    class Cfg:
        agent = "openai-harness"
        stream = True
        mock_config = None
        endpoint = {"name": "local-4090", "model": "m"}
        harness = {"mode": "tools"}

    executor = agent_wrapper.EXECUTORS["openai-harness"](Cfg())
    assert isinstance(executor, HarnessExecutor)
    assert executor.endpoint == {"name": "local-4090", "model": "m"}
    assert executor.harness == {"mode": "tools"}
    assert executor.runner_type == RUNNER_TYPE == "openai-harness"


def test_make_executor_resolves_the_harness_from_the_one_mapping():
    class Cfg:
        agent = "openai-harness"
        stream = False
        mock_config = None
        endpoint = {"name": "x", "model": "m"}
        harness = {}

    assert isinstance(agent_wrapper.make_executor(Cfg()), HarnessExecutor)


def test_build_command_is_only_a_log_line_and_spawns_nothing():
    """No subprocess is ever spawned for the model — the harness IS the agent."""
    executor = HarnessExecutor({"name": "local-4090", "model": "qwen"}, {})
    assert executor.build_command(None) == [
        "<lazyaf-harness>",
        "local-4090",
        "qwen",
    ]
    source = inspect.getsource(HarnessExecutor.execute)
    assert "super().execute" not in source
