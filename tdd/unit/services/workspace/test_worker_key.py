"""
Unit tests for workspace LANE keys (M13-1).

The lane key is the axis that lets one pipeline run own K independent
checkouts. Two properties are load-bearing and pinned here:

1. Absence means the DEFAULT lane. Every pipeline that predates M13 says
   nothing about lanes, and must keep landing in the one workspace it
   already has.
2. A key that is PRESENT but unusable is a loud error, never a silent
   coercion to the default. Quietly handing a worker the trunk checkout is
   the exact bug this milestone exists to eliminate (R1).
"""
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.workspace.worker_key import (
    DEFAULT_WORKER_KEY,
    MAX_WORKER_KEY_LENGTH,
    WORKSPACE_KEY_CONFIG_FIELD,
    validate_worker_key,
    worker_key_for_step,
)


class TestWorkerKeyForStep:
    @pytest.mark.parametrize(
        "config",
        [
            None,
            {},
            {"runner_type": "claude-code"},
            {WORKSPACE_KEY_CONFIG_FIELD: None},
            {WORKSPACE_KEY_CONFIG_FIELD: ""},
            {WORKSPACE_KEY_CONFIG_FIELD: "   "},
        ],
        ids=["none", "empty", "unrelated", "null", "blank", "whitespace"],
    )
    def test_absent_lane_is_the_default_lane(self, config):
        """The overwhelmingly common case: no lane named, trunk checkout."""
        assert worker_key_for_step(config) == DEFAULT_WORKER_KEY

    def test_explicit_key_is_returned_verbatim(self):
        assert worker_key_for_step({WORKSPACE_KEY_CONFIG_FIELD: "w1"}) == "w1"

    def test_surrounding_whitespace_is_stripped(self):
        """`"w1"` and `" w1 "` must not become two different checkouts."""
        assert worker_key_for_step({WORKSPACE_KEY_CONFIG_FIELD: " w1 "}) == "w1"

    def test_the_default_key_may_be_named_explicitly(self):
        config = {WORKSPACE_KEY_CONFIG_FIELD: DEFAULT_WORKER_KEY}
        assert worker_key_for_step(config) == DEFAULT_WORKER_KEY

    def test_a_present_but_unusable_key_raises_rather_than_defaulting(self):
        """The author asked for a lane. Silently giving them the trunk would
        be a green run measuring the wrong thing."""
        with pytest.raises(ValueError, match="must be a string"):
            worker_key_for_step({WORKSPACE_KEY_CONFIG_FIELD: 3})

    def test_overlong_key_from_config_raises_with_the_value(self):
        key = "w" * (MAX_WORKER_KEY_LENGTH + 1)
        with pytest.raises(ValueError, match=str(MAX_WORKER_KEY_LENGTH + 1)):
            worker_key_for_step({WORKSPACE_KEY_CONFIG_FIELD: key})


class TestValidateWorkerKey:
    def test_accepts_an_ordinary_key(self):
        assert validate_worker_key("integrate") == "integrate"

    def test_accepts_a_key_at_the_length_limit(self):
        key = "w" * MAX_WORKER_KEY_LENGTH
        assert validate_worker_key(key) == key

    @pytest.mark.parametrize(
        "bad", [None, 1, 1.5, True, ["w1"], {"w": 1}], ids=type
    )
    def test_non_strings_raise_and_name_the_type(self, bad):
        with pytest.raises(ValueError, match="must be a string"):
            validate_worker_key(bad)

    def test_empty_string_raises_and_points_at_none(self):
        """`""` is a caller bug, not "unspecified" — None is unspecified."""
        with pytest.raises(ValueError, match="non-empty"):
            validate_worker_key("")

    def test_overlong_key_raises_with_the_offending_value(self):
        key = "x" * (MAX_WORKER_KEY_LENGTH + 20)
        with pytest.raises(ValueError) as exc:
            validate_worker_key(key)
        # The message must carry the value: a truncated key would name a
        # DIFFERENT checkout, so the author has to see what they wrote.
        assert key in str(exc.value)
        assert str(MAX_WORKER_KEY_LENGTH) in str(exc.value)

    def test_the_column_width_and_the_limit_agree(self):
        """A key the validator accepts must fit the column that stores it —
        otherwise the loud rejection just moves to the database."""
        from app.models.workspace import Workspace

        assert Workspace.__table__.c.worker_key.type.length == MAX_WORKER_KEY_LENGTH
