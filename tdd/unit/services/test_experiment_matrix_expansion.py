"""
Unit tests for matrix expansion and launch validation (Phase 12.6.5).

Two claims, both load-bearing:

1. **Expansion is deterministic and its arithmetic is the API contract.**
   `cell_index = ((model_i * n_prompts) + prompt_i) * repeat + repeat_i`,
   `variant_index = cell_index // repeat`. The UI grid renders straight off
   these and the leaderboard groups on `variant_index`, so a reordering here
   would silently relabel results.

2. **Every refusal names the offending value.** A guardrail whose message you
   cannot act on is a guardrail people route around, so each validation test
   asserts the message content, not just the exception type.
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.experiment import EXPERIMENT_MAX_CELLS, EXPERIMENT_MAX_CONCURRENCY
from app.schemas.experiment import (
    AGENT_VOCABULARY,
    RESERVED_STEP_CONFIG_KEYS,
    ExperimentCreate,
    MatrixSpec,
    VerifySpec,
)
from app.services.experiment_service import expand_matrix


_UNSET = object()


def matrix(models=_UNSET, prompts=_UNSET, repeat=1) -> MatrixSpec:
    # Sentinel, not `or`: an explicitly EMPTY axis is the case under test and
    # must not be quietly replaced by the default.
    return MatrixSpec.model_validate(
        {
            "models": [{"agent": "mock", "model": "m1"}]
            if models is _UNSET
            else models,
            "prompts": [{"prompt_template_id": None}]
            if prompts is _UNSET
            else prompts,
            "repeat": repeat,
        }
    )


def create_payload(**overrides):
    payload = {
        "name": "opus vs haiku",
        "target_type": "card",
        "target_id": "card-1",
        "matrix": {
            "models": [{"agent": "mock", "model": "m1"}],
            "prompts": [{"prompt_template_id": None}],
            "repeat": 1,
        },
        "budget_usd": "5.00",
    }
    payload.update(overrides)
    return payload


class TestExpansion:
    def test_two_by_two_by_three_makes_twelve_cells(self):
        cells = expand_matrix(
            matrix(
                models=[
                    {"agent": "mock", "model": "a"},
                    {"agent": "mock", "model": "b"},
                ],
                prompts=[
                    {"prompt_template_id": "t1"},
                    {"prompt_template_id": None},
                ],
                repeat=3,
            )
        )
        assert len(cells) == 12
        assert len({c.cell_index for c in cells}) == 12
        assert len({c.variant_index for c in cells}) == 4

    def test_cell_index_formula_is_the_contract(self):
        cells = expand_matrix(
            matrix(
                models=[
                    {"agent": "mock", "model": "a"},
                    {"agent": "mock", "model": "b"},
                    {"agent": "mock", "model": "c"},
                ],
                prompts=[{"prompt_template_id": "t1"}, {"prompt_template_id": "t2"}],
                repeat=2,
            )
        )
        by_index = {c.cell_index: c for c in cells}
        for model_i, model in enumerate(("a", "b", "c")):
            for prompt_i, template in enumerate(("t1", "t2")):
                for repeat_i in range(2):
                    index = ((model_i * 2) + prompt_i) * 2 + repeat_i
                    cell = by_index[index]
                    assert cell.model == model
                    assert cell.prompt_template_id == template
                    assert cell.repeat_index == repeat_i
                    assert cell.variant_index == index // 2

    def test_repeats_share_a_variant_index(self):
        cells = expand_matrix(matrix(repeat=4))
        assert {c.variant_index for c in cells} == {0}
        assert sorted(c.repeat_index for c in cells) == [0, 1, 2, 3]

    def test_expansion_is_stable_across_calls(self):
        spec = matrix(
            models=[{"agent": "mock", "model": "a"}, {"agent": "gemini"}],
            prompts=[{"prompt_template_id": "t1"}, {"prompt_template_id": None}],
            repeat=2,
        )
        first = expand_matrix(spec)
        second = expand_matrix(spec)
        assert [c.cell_index for c in first] == [c.cell_index for c in second]
        assert [c.model for c in first] == [c.model for c in second]

    def test_null_model_and_null_template_are_real_control_variants(self):
        cells = expand_matrix(
            matrix(
                models=[{"agent": "claude-code", "model": None}],
                prompts=[{"prompt_template_id": None}],
            )
        )
        assert cells[0].model is None
        assert cells[0].prompt_template_id is None
        assert cells[0].agent == "claude-code"

    def test_labels_fall_back_to_coordinates(self):
        cells = expand_matrix(
            matrix(
                models=[{"agent": "mock", "model": "m1", "label": "cheap"}],
                prompts=[{"prompt_template_id": None, "label": "v2"}],
            )
        )
        assert cells[0].label == "cheap / v2"

    def test_unlabelled_variant_still_names_itself(self):
        cells = expand_matrix(matrix())
        assert "mock" in cells[0].label
        assert "platform default" in cells[0].label

    def test_overlays_merge_models_first_prompts_second(self):
        cells = expand_matrix(
            matrix(
                models=[
                    {
                        "agent": "mock",
                        "model": "m1",
                        "step_config": {"delay_ms": 10, "shared": "from-model"},
                    }
                ],
                prompts=[
                    {
                        "prompt_template_id": None,
                        "step_config": {"shared": "from-prompt"},
                    }
                ],
            )
        )
        assert cells[0].step_config == {"delay_ms": 10, "shared": "from-prompt"}

    def test_overlay_is_per_cell_not_shared_state(self):
        cells = expand_matrix(
            matrix(
                models=[
                    {"agent": "mock", "step_config": {"a": 1}},
                    {"agent": "mock", "step_config": {"b": 2}},
                ]
            )
        )
        assert cells[0].step_config == {"a": 1}
        assert cells[1].step_config == {"b": 2}


class TestMatrixValidation:
    def test_empty_models_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            matrix(models=[])
        assert "matrix.models" in str(exc.value)

    def test_empty_prompts_is_refused(self):
        with pytest.raises(ValidationError) as exc:
            matrix(prompts=[])
        assert "matrix.prompts" in str(exc.value)

    def test_repeat_below_one_is_refused_and_names_the_value(self):
        with pytest.raises(ValidationError) as exc:
            matrix(repeat=0)
        assert "repeat must be >= 1" in str(exc.value)
        assert "got 0" in str(exc.value)

    def test_cell_cap_names_the_computed_count(self):
        with pytest.raises(ValidationError) as exc:
            matrix(
                models=[{"agent": "mock", "model": f"m{i}"} for i in range(11)],
                prompts=[{"prompt_template_id": None} for _ in range(3)],
                repeat=7,
            )
        message = str(exc.value)
        assert "231 cells" in message
        assert str(EXPERIMENT_MAX_CELLS) in message

    def test_unknown_agent_names_the_value_and_the_legal_set(self):
        with pytest.raises(ValidationError) as exc:
            matrix(models=[{"agent": "gpt-9"}])
        message = str(exc.value)
        assert "'gpt-9'" in message
        for agent in AGENT_VOCABULARY:
            assert agent in message

    def test_agent_vocabulary_is_the_agent_run_one(self):
        """R3: one vocabulary, not a re-spelling that can drift."""
        from app.services.agent_run import AGENT_BY_RUNNER_TYPE

        assert set(AGENT_VOCABULARY) == set(AGENT_BY_RUNNER_TYPE.values())

    def test_agent_is_required_no_model_name_inference(self):
        """A guessed CLI is a silent fallback (R1) and is unfalsifiable once
        the run is over."""
        with pytest.raises(ValidationError):
            MatrixSpec.model_validate(
                {
                    "models": [{"model": "claude-opus-5"}],
                    "prompts": [{"prompt_template_id": None}],
                }
            )

    @pytest.mark.parametrize("key", sorted(RESERVED_STEP_CONFIG_KEYS))
    def test_reserved_overlay_key_is_refused_by_name(self, key):
        """An overlay that could rewrite the axis the matrix varies is the
        definition of dark. Naming it in a 422 is not."""
        with pytest.raises(ValidationError) as exc:
            matrix(models=[{"agent": "mock", "step_config": {key: "x"}}])
        assert repr(key) in str(exc.value)

    def test_reserved_keys_are_refused_on_the_prompt_axis_too(self):
        with pytest.raises(ValidationError) as exc:
            matrix(prompts=[{"prompt_template_id": None, "step_config": {"model": "x"}}])
        assert "'model'" in str(exc.value)

    def test_reserved_set_covers_every_key_the_builder_would_drop(self):
        """No key may vanish silently between the matrix and the container.

        `build_agent_step_config` drops its own reserved keys from an `extra`
        overlay, so anything in that set which this schema does not REFUSE
        would be accepted at create time and then disappear. `mock_config` is
        the one exception: the service pops it and passes it through the
        builder's named parameter, so it genuinely arrives.
        """
        from app.services.agent_run import _RESERVED_STEP_CONFIG_KEYS

        would_be_dropped = set(_RESERVED_STEP_CONFIG_KEYS) - {"mock_config"}
        assert would_be_dropped <= set(RESERVED_STEP_CONFIG_KEYS)

    def test_mock_config_is_allowed_in_an_overlay(self):
        """It is the escape hatch that makes mock-agent experiments
        scriptable; it is not an axis the matrix varies."""
        spec = matrix(
            models=[
                {"agent": "mock", "step_config": {"mock_config": {"delay_ms": 5}}}
            ]
        )
        assert expand_matrix(spec)[0].step_config["mock_config"] == {"delay_ms": 5}


class TestCreateValidation:
    def test_budget_is_required(self):
        payload = create_payload()
        payload.pop("budget_usd")
        with pytest.raises(ValidationError):
            ExperimentCreate.model_validate(payload)

    def test_zero_budget_is_refused_with_a_reason(self):
        with pytest.raises(ValidationError) as exc:
            ExperimentCreate.model_validate(create_payload(budget_usd="0"))
        assert "budget_usd must be > 0" in str(exc.value)

    def test_negative_budget_is_refused(self):
        with pytest.raises(ValidationError):
            ExperimentCreate.model_validate(create_payload(budget_usd="-1"))

    @pytest.mark.parametrize("value", [0, EXPERIMENT_MAX_CONCURRENCY + 1, -3])
    def test_concurrency_bounds_name_the_value(self, value):
        with pytest.raises(ValidationError) as exc:
            ExperimentCreate.model_validate(create_payload(max_concurrency=value))
        assert f"got {value}" in str(exc.value)

    def test_feature_target_type_is_refused_at_the_schema(self):
        with pytest.raises(ValidationError):
            ExperimentCreate.model_validate(create_payload(target_type="feature"))

    def test_blank_name_is_refused(self):
        with pytest.raises(ValidationError):
            ExperimentCreate.model_validate(create_payload(name="   "))

    def test_push_branches_defaults_false(self):
        assert ExperimentCreate.model_validate(create_payload()).push_branches is False

    def test_dry_run_defaults_false(self):
        assert ExperimentCreate.model_validate(create_payload()).dry_run is False

    def test_budget_parses_as_decimal_not_float(self):
        from decimal import Decimal

        payload = ExperimentCreate.model_validate(create_payload(budget_usd="0.30"))
        assert payload.budget_usd == Decimal("0.30")
        assert isinstance(payload.budget_usd, Decimal)


class TestVerifyValidation:
    def test_verify_is_optional(self):
        assert ExperimentCreate.model_validate(create_payload()).verify is None

    def test_blank_command_is_refused(self):
        with pytest.raises(ValidationError):
            VerifySpec.model_validate({"image": "python:3.11", "command": "  "})

    def test_non_positive_timeout_is_refused_by_name(self):
        with pytest.raises(ValidationError) as exc:
            VerifySpec.model_validate(
                {"image": "python:3.11", "command": "pytest", "timeout": 0}
            )
        assert "verify.timeout must be > 0" in str(exc.value)

    def test_default_timeout(self):
        spec = VerifySpec.model_validate({"image": "i", "command": "pytest"})
        assert spec.timeout == 900
