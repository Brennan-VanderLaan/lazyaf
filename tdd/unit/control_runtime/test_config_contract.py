"""
Producer <-> consumer contract for the step config file (R3).

The backend's `control_layer.workspace.generate_step_config` is the single
producer of the per-step config file; the in-container runtime's
`control.config.load_step_config` is the single consumer.

INVERTED direction (adversarial-review fix): the assertion is
consumer-keys SUPERSET-OF producer-keys — every key the producer emits must
be consumed under the SAME name with the SAME value (the failure_01
`token`/`working_dir` bug class), and every consumer field the producer does
not emit must be explicitly accounted for. Runtime shipping knobs
(heartbeat interval, log batching) are intentionally NOT transported: they
are module constants, never config fields.
"""
import dataclasses
import json
import sys
from pathlib import Path

# Backend on path (root conftest also does this; keep hermetic)
backend_path = Path(__file__).resolve().parents[3] / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from app.services.control_layer.workspace import generate_step_config

from control import executor, heartbeat
from control.config import StepConfig, load_step_config

# Knobs that deliberately do NOT travel in the config file. They must be
# absent from BOTH sides of the wire: not produced, not a consumer field —
# each lives as a module constant instead (asserted below).
INTENTIONALLY_UNTRANSPORTED = {
    "heartbeat_interval",
    "log_batch_size",
    "log_batch_interval",
}

# Consumer fields allowed to exceed the producer's keys because they carry a
# consumer-side default. "shell" is transported once the producer emits it
# (contract #2); a producer payload that predates the key still loads with
# the documented default "bash". Once the producer emits shell, the extras
# set below collapses to empty and this stays green.
CONSUMER_DEFAULTED = {"shell"}


def _consumer_fields():
    return {f.name for f in dataclasses.fields(StepConfig)}


def _producer_config():
    return generate_step_config(
        step_id="exec-abc",
        step_run_id="sr-def",
        execution_key="run-1:2:sr-def",
        command="pytest ../tdd -m 'not slow'\necho done",
        backend_url="http://backend:8000",
        auth_token="jwt-token-xyz",
        environment={"CI": "1", "FOO": "bar"},
        timeout_seconds=1800,
        working_directory="/workspace/repo/backend",
    )


class TestConfigRoundTrip:
    def test_every_producer_key_is_consumed_with_equal_value(self, tmp_path):
        """Identity mapping is the contract: producer key == consumer
        attribute, value preserved verbatim through the file round trip."""
        produced = _producer_config()

        config_file = tmp_path / "step_config.json"
        config_file.write_text(json.dumps(produced))

        loaded = load_step_config(config_file)

        assert loaded is not None
        for key, value in produced.items():
            assert hasattr(loaded, key), (
                f"producer emits key {key!r} the consumer does not have — "
                "extend StepConfig and the loader together"
            )
            assert getattr(loaded, key) == value, key

    def test_consumer_keys_superset_of_producer_keys(self):
        produced = set(_producer_config().keys())
        consumer = _consumer_fields()

        assert produced <= consumer, (
            f"producer emits keys unknown to the consumer: {produced - consumer}"
        )
        extras = consumer - produced
        assert extras <= CONSUMER_DEFAULTED, (
            f"consumer fields the producer never sends and no one documented: "
            f"{extras - CONSUMER_DEFAULTED}"
        )

    def test_untransported_knobs_are_module_constants_not_config(self):
        """The shipping knobs must not creep back onto the wire OR into the
        consumer dataclass — they live as module constants."""
        produced = set(_producer_config().keys())
        consumer = _consumer_fields()

        assert not (INTENTIONALLY_UNTRANSPORTED & produced)
        assert not (INTENTIONALLY_UNTRANSPORTED & consumer)

        assert isinstance(heartbeat.HEARTBEAT_INTERVAL, float)
        assert isinstance(executor.LOG_BATCH_SIZE, int)
        assert isinstance(executor.LOG_BATCH_INTERVAL, float)

    def test_producer_command_is_a_string(self):
        """The command travels as the raw user script STRING; the runtime
        shell-wraps it (same semantics as local_executor.build_step_command)."""
        produced = _producer_config()
        assert isinstance(produced["command"], str)

    def test_loader_accepts_producer_defaults(self, tmp_path):
        """generate_step_config's own defaults load cleanly too."""
        produced = generate_step_config(
            step_id="e",
            step_run_id="s",
            execution_key="r:0:s",
            command="true",
            backend_url="http://backend:8000",
            auth_token="t",
        )
        config_file = tmp_path / "step_config.json"
        config_file.write_text(json.dumps(produced))

        loaded = load_step_config(config_file)

        assert loaded is not None
        assert loaded.working_directory == "/workspace/repo"
        assert loaded.timeout_seconds == 3600
        assert loaded.environment == {}
        assert loaded.shell == "bash"  # consumer default until produced
