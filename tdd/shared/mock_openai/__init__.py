"""A stdlib OpenAI-compatible server for testing the LazyAF agent harness (M14).

CI MUST NOT NEED A GPU (wave8 section 8.1). Everything the harness, the
capability probe, the endpoint registry, the token accumulator and the
gpu-node pricing path do can be exercised against a server that speaks the
OpenAI wire format deterministically - so T1, T2, T3 and the dogfood lane all
run against THIS, and a real ollama/vLLM box is only ever needed to prove the
network hop (14.4, manual).

Public surface:

    from tdd.shared.mock_openai import MockOpenAIServer, SCENARIOS

    with MockOpenAIServer() as srv:
        base_url = srv.base_url("happy_tools")     # .../happy_tools/v1
        ...

Run it as a service (this is what the compose `mock-endpoint` does):

    python -m tdd.shared.mock_openai --host 0.0.0.0 --port 8099

Design notes that matter to anyone extending it:

* **It is STATELESS per request.** The turn number is derived from the number
  of `assistant` messages in the request body, never from a session table, so
  replays, retries and two concurrent steps against one server cannot
  interfere. The one exception is `flaky_5xx`, which needs a counter to fail
  twice - it is documented at its definition and resettable via
  `POST /_control/reset`.
* **One scenario per URL PREFIX**, so one process serves every scenario at
  once: `http://mock-endpoint:8099/<scenario>/v1`. `/v1` with no prefix is the
  process default scenario.
* Token counts GROW with the turn number on purpose - see
  `MOCK_PROMPT_TOKENS_PER_TURN` in `scenarios.py`. That is what makes
  "the accumulator summed across turns" a checkable claim rather than a hope
  (verify_executor assertion 13).
"""
from .scenarios import (  # noqa: F401
    ACTION_SCRIPT_LENGTH,
    DEFAULT_TARGET_PATH,
    MOCK_AUDIO_PROMPT_TOKENS,
    MOCK_COMPLETION_TOKENS_PER_TURN,
    MOCK_IMAGE_PROMPT_TOKENS,
    MOCK_MODALITY_BASE_PROMPT_TOKENS,
    MOCK_MODELS,
    MOCK_MODEL_CONTEXT_WINDOW,
    MOCK_PROMPT_TOKENS_PER_TURN,
    MODALITY_POLICIES,
    MODALITY_SCENARIO_NAMES,
    OLLAMA_CAPABILITY_VOCABULARY,
    OLLAMA_SHOW_CAPABILITIES,
    SCENARIOS,
    ModalityPolicy,
    Turn,
    content_part_types,
    expected_summed_tokens,
    largest_single_turn_tokens,
    plan_show,
    plan_turn,
    turn_number,
)
from .server import MockOpenAIServer, build_server  # noqa: F401

__all__ = [
    "ACTION_SCRIPT_LENGTH",
    "DEFAULT_TARGET_PATH",
    "MOCK_AUDIO_PROMPT_TOKENS",
    "MOCK_COMPLETION_TOKENS_PER_TURN",
    "MOCK_IMAGE_PROMPT_TOKENS",
    "MOCK_MODALITY_BASE_PROMPT_TOKENS",
    "MOCK_MODELS",
    "MOCK_MODEL_CONTEXT_WINDOW",
    "MOCK_PROMPT_TOKENS_PER_TURN",
    "MODALITY_POLICIES",
    "MODALITY_SCENARIO_NAMES",
    "ModalityPolicy",
    "MockOpenAIServer",
    "OLLAMA_CAPABILITY_VOCABULARY",
    "OLLAMA_SHOW_CAPABILITIES",
    "SCENARIOS",
    "Turn",
    "build_server",
    "content_part_types",
    "expected_summed_tokens",
    "largest_single_turn_tokens",
    "plan_show",
    "plan_turn",
    "turn_number",
]
