"""
Producer <-> consumer contract for the CURATED SPEC CONTEXT (12.6.6).

The shape itself lives in ONE importable module,
`tdd/unit/control_runtime/spec_context_contract.py`, which BOTH sides import -
the same idiom `usage_contract.py` and `manifest_contract.py` already use for
the 12.5 usage wire. This module drives the two real implementations across a
REAL JSON file in ONE process:

    PRODUCER  app.services.control_layer.workspace.generate_agent_config
        |
        v   /workspace/.control/agent.<step_execution_id>.json
    CONSUMER  runner_common.agent_config.load_agent_config

and pins the parts of the contract that only show up when both sides are
present: the filename and directory constants each side hardcodes, the byte
budget the producer refuses at, and the `None`-means-no-bundle spelling that
keeps a card with no spec links byte-identical to the pre-12.6.6 wire.

The ASSEMBLER's conformance to the same module is asserted in
`tdd/unit/services/test_spec_context_bundle.py`; the WRAPPER's in
`tdd/unit/control_runtime/test_spec_context_injection.py`. One contract, three
call sites, no second copy of the shape.

The conftest of this package already puts both `images/base` and
`runner-common` on sys.path.
"""
import copy
import json
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.services.control_layer import workspace  # noqa: E402
from app.services.control_layer.workspace import (  # noqa: E402
    SPEC_CONTEXT_BYTES_PER_TOKEN,
    SPEC_CONTEXT_MAX_BYTES,
    SPEC_CONTEXT_MAX_TOKENS,
    SPEC_CONTEXT_PATH,
    agent_config_keys,
    estimate_spec_context_tokens,
    generate_agent_config,
    validate_spec_context,
)

from runner_common import agent_config as consumer_module  # noqa: E402
from runner_common.agent_config import load_agent_config  # noqa: E402

from tdd.unit.control_runtime.spec_context_contract import (  # noqa: E402
    BYTES_PER_TOKEN,
    CANONICAL_BUNDLE,
    DROP_RULES,
    INVALID_BUNDLES,
    MAX_BYTES,
    TRUNCATED_BUNDLE,
    assert_bundle_conforms,
    bundle_violations,
    estimated_tokens_for,
)


def _payload(**overrides):
    kwargs = dict(
        agent="claude-code",
        prompt="You are implementing a feature for this project.",
        card_id="c1d2",
        card_title="Add rate limiting to /api/repos",
        repo_id="r9f8",
        branch="lazyaf/9f2a11c4",
    )
    kwargs.update(overrides)
    return generate_agent_config(**kwargs)


def _write(tmp_path, payload, name="agent.exec-1.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


class TestTheSharedContractModule:
    """The contract module is only worth having if it is strict."""

    def test_canonical_and_truncated_bundles_conform(self):
        assert_bundle_conforms(CANONICAL_BUNDLE, "CONTRACT MODULE (canonical)")
        assert_bundle_conforms(TRUNCATED_BUNDLE, "CONTRACT MODULE (truncated)")

    def test_none_is_a_valid_bundle(self):
        """`None` is the ONE spelling of 'this card has no spec context'."""
        assert bundle_violations(None) == []

    @pytest.mark.parametrize(
        "label,value", INVALID_BUNDLES, ids=[label for label, _ in INVALID_BUNDLES]
    )
    def test_every_invalid_bundle_is_rejected(self, label, value):
        assert bundle_violations(value), f"{label!r} slipped through the contract"

    def test_the_estimator_is_the_documented_heuristic(self):
        """Restated in the contract module AND implemented in the backend;
        they must be the same function."""
        assert BYTES_PER_TOKEN == SPEC_CONTEXT_BYTES_PER_TOKEN
        assert MAX_BYTES == SPEC_CONTEXT_MAX_BYTES
        for text in ("", "x", "x" * 4, "x" * 5, "é" * 100, CANONICAL_BUNDLE["markdown"]):
            assert estimated_tokens_for(text) == estimate_spec_context_tokens(text)


class TestBothSidesNameTheSameFile:
    def test_filename_pinned_on_both_sides(self):
        assert (
            workspace.SPEC_CONTEXT_FILENAME
            == consumer_module.SPEC_CONTEXT_FILENAME
        ), (
            "the backend and the wrapper name different files for the curated "
            "bundle - one of them writes somewhere nobody reads"
        )

    def test_dir_matches_the_control_config_dir(self):
        """`workspace.py` hardcodes the directory rather than importing it
        (it must stay importable with no docker dependency). This is what
        makes that duplication safe."""
        from app.services.execution.local_executor import CONTROL_CONFIG_DIR

        assert workspace.SPEC_CONTEXT_DIR == f"/workspace/{CONTROL_CONFIG_DIR}"

    def test_the_path_constant_is_the_two_halves(self):
        assert SPEC_CONTEXT_PATH == (
            f"{workspace.SPEC_CONTEXT_DIR}/{workspace.SPEC_CONTEXT_FILENAME}"
        )

    def test_the_budget_is_stated_in_tokens_and_enforced_in_bytes(self):
        assert SPEC_CONTEXT_MAX_BYTES == (
            SPEC_CONTEXT_MAX_TOKENS * SPEC_CONTEXT_BYTES_PER_TOKEN
        )


class TestRoundTrip:
    def test_spec_context_is_a_top_level_agent_config_key(self):
        assert "spec_context" in agent_config_keys()
        assert "spec_context" in _payload()

    def test_a_populated_bundle_survives_the_file_verbatim(self, tmp_path):
        produced = _payload(spec_context=copy.deepcopy(CANONICAL_BUNDLE))
        assert_bundle_conforms(produced["spec_context"], "PRODUCER (agent config)")

        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        assert_bundle_conforms(loaded.spec_context, "CONSUMER (agent config)")
        assert loaded.spec_context == CANONICAL_BUNDLE
        assert loaded.spec_markdown == CANONICAL_BUNDLE["markdown"]
        assert loaded.has_spec_context is True

    def test_a_truncated_bundle_survives_with_its_drop_names(self, tmp_path):
        produced = _payload(spec_context=copy.deepcopy(TRUNCATED_BUNDLE))
        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        assert loaded.spec_context["truncated"] is True
        assert loaded.spec_context["dropped"] == [
            "criterion_notes",
            "feature_description",
        ]
        assert "truncated to fit" in loaded.spec_markdown

    def test_the_drop_vocabulary_is_the_backends(self):
        """A rule the backend can emit but the contract does not name would
        make `dropped` unreadable to the operator it exists for."""
        import app.services.spec_context as assembler

        source = Path(assembler.__file__).read_text(encoding="utf-8")
        for rule in DROP_RULES:
            assert f'"{rule}"' in source, (
                f"the contract names drop rule {rule!r} but the assembler "
                "never emits it"
            )


class TestNoBundleIsNone:
    def test_absent_key_loads_as_none(self, tmp_path):
        """A pre-12.6.6 backend. Additive optional key, no version bump - a
        bump would strand every runner agent in the field mid-phase."""
        produced = _payload()
        produced.pop("spec_context")
        loaded = load_agent_config(_write(tmp_path, produced))

        assert loaded is not None
        assert loaded.spec_context is None
        assert loaded.spec_markdown is None
        assert loaded.has_spec_context is False

    def test_explicit_null_loads_as_none(self, tmp_path):
        loaded = load_agent_config(_write(tmp_path, _payload(spec_context=None)))

        assert loaded is not None
        assert loaded.spec_context is None
        assert loaded.has_spec_context is False

    def test_no_bundle_still_produces_a_valid_agent_config(self, tmp_path):
        """The clean no-op: the rest of the wire is untouched."""
        produced = _payload(spec_context=None)
        assert produced["spec_context"] is None
        loaded = load_agent_config(_write(tmp_path, produced))
        assert loaded is not None and loaded.prompt == produced["prompt"]


class TestTheProducerRefusesBadBundles:
    def test_oversized_bundle_is_refused_at_dispatch(self):
        """The producer is the LAST gate before the wire.

        A bundle that slipped the assembler's truncation must fail the step
        HERE, with the byte and token budget named, rather than arrive
        oversized and kill the CLI with E2BIG twenty minutes into a paid run.
        """
        huge = {**CANONICAL_BUNDLE, "markdown": "x" * (SPEC_CONTEXT_MAX_BYTES + 1)}
        with pytest.raises(ValueError) as exc:
            _payload(spec_context=huge)
        message = str(exc.value)
        assert str(SPEC_CONTEXT_MAX_BYTES) in message
        assert str(SPEC_CONTEXT_MAX_TOKENS) in message

    def test_a_bundle_exactly_at_the_cap_is_allowed(self):
        at_cap = {**CANONICAL_BUNDLE, "markdown": "x" * SPEC_CONTEXT_MAX_BYTES}
        assert _payload(spec_context=at_cap)["spec_context"]["markdown"]

    def test_the_cap_is_bytes_not_characters(self):
        """A spec written in a multi-byte script cannot blow the argv cap."""
        multibyte = {
            **CANONICAL_BUNDLE,
            "markdown": "é" * (SPEC_CONTEXT_MAX_BYTES // 2 + 1),
        }
        with pytest.raises(ValueError, match="bytes"):
            _payload(spec_context=multibyte)

    def test_empty_bundle_is_refused_because_none_is_the_spelling(self):
        with pytest.raises(ValueError, match="None"):
            _payload(spec_context={})
        with pytest.raises(ValueError, match="None"):
            _payload(spec_context={**CANONICAL_BUNDLE, "markdown": ""})

    def test_non_dict_bundle_is_refused(self):
        with pytest.raises(ValueError, match="object or None"):
            _payload(spec_context="## Spec Context")

    def test_validate_spec_context_accepts_none(self):
        assert validate_spec_context(None) is None


class TestTheConsumerRefusesBadBundles:
    def test_non_dict_spec_context_is_refused_loudly(self, tmp_path, capsys):
        payload = {**_payload(), "spec_context": "## Spec Context"}
        assert load_agent_config(_write(tmp_path, payload)) is None
        assert "spec_context" in capsys.readouterr().err

    def test_non_string_markdown_is_refused_loudly(self, tmp_path, capsys):
        payload = {
            **_payload(),
            "spec_context": {**CANONICAL_BUNDLE, "markdown": {"text": "x"}},
        }
        assert load_agent_config(_write(tmp_path, payload)) is None
        assert "markdown" in capsys.readouterr().err

    def test_a_bundle_with_null_markdown_is_no_bundle_not_a_crash(
        self, tmp_path
    ):
        """Defensive: a hand-edited file must degrade to 'no context', never
        to a wrapper that writes a file containing the string 'None'."""
        payload = {
            **_payload(),
            "spec_context": {**CANONICAL_BUNDLE, "markdown": None},
        }
        loaded = load_agent_config(_write(tmp_path, payload))
        assert loaded is not None
        assert loaded.spec_markdown is None
        assert loaded.has_spec_context is False


class TestNoSecretsRideTheBundle:
    def test_the_secret_scan_covers_the_new_key(self, tmp_path):
        """The agent file is the one file the wrapper opens; the step JWT and
        the provider API key live in the STEP config, which run.py deletes
        before the command starts."""
        blob = json.dumps(_payload(spec_context=copy.deepcopy(CANONICAL_BUNDLE)))
        for forbidden in (
            "auth_token",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "execution_key",
            "backend_url",
        ):
            assert forbidden not in blob
