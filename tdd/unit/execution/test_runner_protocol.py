"""Runner protocol surfaces the dormant contract suite does not cover.

``tdd/unit/execution/test_websocket_protocol.py`` is the frozen contract and
must never be edited to fit an implementation. It pins the original message
catalogue and the four timeout constants. Everything Phase 12.6 ADDED on top
of that catalogue is pinned here instead:

- defaulted-field parity (the four-kwarg constructors still work, and the
  new optional fields survive a to_dict/parse round trip)
- the full backend message catalogue through ``create_backend_message``
- the ``normalize_arch`` table (cross-agent contract #5)
- the derived timing constants and the RECEIVE_TIMEOUT < DEATH_TIMEOUT
  relationship that failure_01 left unstated
- ``build_execute_step_config`` and its secret boundary (contract #9)
"""
import sys
from pathlib import Path

import pytest

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.execution.runner_protocol import (
    ACK_TIMEOUT,
    BACKEND_MESSAGE_TYPES,
    DEATH_MONITOR_INTERVAL,
    DEATH_TIMEOUT,
    DISPATCH_SWEEP_INTERVAL,
    DRAIN_GRACE,
    HEARTBEAT_INTERVAL,
    MAX_ASSIGN_ATTEMPTS,
    NO_RUNNER_TIMEOUT,
    PROTOCOL_VERSION,
    RECEIVE_TIMEOUT,
    REGISTRATION_TIMEOUT,
    SUPPORTED_PROTOCOL_VERSIONS,
    AckMessage,
    CancelStepMessage,
    CleanupWorkspaceMessage,
    DrainMessage,
    ErrorMessage,
    ExecuteStepMessage,
    HeartbeatMessage,
    LogMessage,
    PingMessage,
    PongMessage,
    RegisteredMessage,
    RegisterMessage,
    StepCompleteMessage,
    build_execute_step_config,
    create_backend_message,
    is_supported_protocol_version,
    normalize_arch,
    normalize_labels,
    parse_runner_message,
    unsupported_version_message,
    validate_runner_message,
)


class TestDerivedConstants:
    def test_receive_timeout_is_below_death_timeout(self):
        """Deliberate and load-bearing: a read timeout provokes a keepalive
        ping, the death monitor is the SOLE authority on death. failure_01
        had these two values fighting with no stated relationship."""
        assert RECEIVE_TIMEOUT < DEATH_TIMEOUT

    def test_receive_timeout_is_two_heartbeats(self):
        assert RECEIVE_TIMEOUT == HEARTBEAT_INTERVAL * 2

    def test_operational_constants_are_named_not_inline(self):
        assert DEATH_MONITOR_INTERVAL == 5
        assert DISPATCH_SWEEP_INTERVAL == 15
        assert MAX_ASSIGN_ATTEMPTS == 3
        assert NO_RUNNER_TIMEOUT == 300
        assert DRAIN_GRACE == 30

    def test_protocol_version_is_one_and_supported(self):
        assert PROTOCOL_VERSION == 1
        assert PROTOCOL_VERSION in SUPPORTED_PROTOCOL_VERSIONS


class TestDefaultedFieldParity:
    """Every field added beyond the original four is defaulted."""

    def test_register_builds_from_exactly_the_four_documented_kwargs(self):
        msg = RegisterMessage(
            runner_id="runner-1", name="My Runner", runner_type="claude-code", labels={}
        )
        assert msg.protocol_version == PROTOCOL_VERSION
        assert msg.agent_version == ""
        assert msg.token is None
        assert msg.resume is None

    def test_validate_returns_empty_for_the_four_kwarg_payload(self):
        data = {
            "type": "register",
            "runner_id": "runner-1",
            "name": "My Runner",
            "runner_type": "claude-code",
            "labels": {},
        }
        assert validate_runner_message(data) == []

    def test_register_round_trips_with_zero_key_loss(self):
        msg = RegisterMessage(
            runner_id="r1",
            name="n",
            runner_type="generic",
            labels={"arch": "arm64"},
            protocol_version=1,
            agent_version="0.1.0",
            token="secret",
            resume={"step_id": "s1"},
        )
        parsed = parse_runner_message(msg.to_dict())
        assert parsed.to_dict() == msg.to_dict()

    def test_register_parses_without_a_protocol_version(self):
        """A pre-version agent reads as version 1 (section 1.4)."""
        msg = parse_runner_message(
            {"type": "register", "runner_id": "r1", "runner_type": "generic"}
        )
        assert msg.protocol_version == 1

    def test_parse_never_raises_key_error_on_a_partial_payload(self):
        for payload in (
            {"type": "register"},
            {"type": "ack"},
            {"type": "log"},
            {"type": "step_complete"},
        ):
            parse_runner_message(payload)  # must not raise

    def test_log_message_round_trips_seq(self):
        msg = LogMessage(step_id="s1", lines=["a"], seq=7)
        assert parse_runner_message(msg.to_dict()).seq == 7

    def test_step_complete_error_is_always_emitted(self):
        """null on success, never omitted."""
        assert "error" in StepCompleteMessage(step_id="s", exit_code=0).to_dict()

    def test_ping_is_legal_in_both_directions(self):
        assert isinstance(parse_runner_message({"type": "ping"}), PingMessage)
        assert isinstance(create_backend_message("ping"), PingMessage)


class TestValidation:
    def test_exit_code_zero_is_present_not_falsy(self):
        """Membership, not truthiness: a falsiness check here would reject
        every successful step completion."""
        assert validate_runner_message(
            {"type": "step_complete", "step_id": "s", "exit_code": 0}
        ) == []

    def test_empty_lines_list_is_valid(self):
        assert validate_runner_message({"type": "log", "step_id": "s", "lines": []}) == []

    def test_missing_type_reports_the_type_field(self):
        assert validate_runner_message({}) == ["Missing 'type' field"]

    def test_unknown_type_is_reported(self):
        errors = validate_runner_message({"type": "nope"})
        assert errors == ["Unknown message type: nope"]

    def test_register_does_not_require_name_or_labels(self):
        assert validate_runner_message(
            {"type": "register", "runner_id": "r", "runner_type": "t"}
        ) == []

    def test_token_is_not_a_required_field(self):
        """Auth is a TRANSPORT concern (section 1.3): the contract suite
        forbids a required token field, so the handshake carries it."""
        assert validate_runner_message(
            {"type": "register", "runner_id": "r", "runner_type": "t"}
        ) == []

    def test_heartbeat_and_ping_require_nothing(self):
        assert validate_runner_message({"type": "heartbeat"}) == []
        assert validate_runner_message({"type": "ping"}) == []


class TestCreateBackendMessage:
    """All eight backend types build; anything else raises."""

    def test_every_backend_type_is_constructible(self):
        built = {
            "registered": create_backend_message("registered", runner_id="r"),
            "execute_step": create_backend_message(
                "execute_step", step_id="s", execution_key="k", config={}
            ),
            "cancel_step": create_backend_message("cancel_step", step_id="s"),
            "cleanup_workspace": create_backend_message(
                "cleanup_workspace", retain_key="rk"
            ),
            "drain": create_backend_message("drain"),
            "pong": create_backend_message("pong"),
            "ping": create_backend_message("ping"),
            "error": create_backend_message("error", message="boom"),
        }
        assert set(built) == set(BACKEND_MESSAGE_TYPES)
        for name, msg in built.items():
            assert msg.type == name
            assert msg.to_dict()["type"] == name

    def test_types_map_to_the_expected_classes(self):
        assert isinstance(create_backend_message("registered", runner_id="r"), RegisteredMessage)
        assert isinstance(
            create_backend_message("execute_step", step_id="s", execution_key="k", config={}),
            ExecuteStepMessage,
        )
        assert isinstance(create_backend_message("cancel_step", step_id="s"), CancelStepMessage)
        assert isinstance(
            create_backend_message("cleanup_workspace", retain_key="rk"),
            CleanupWorkspaceMessage,
        )
        assert isinstance(create_backend_message("drain"), DrainMessage)
        assert isinstance(create_backend_message("pong"), PongMessage)
        assert isinstance(create_backend_message("error", message="x"), ErrorMessage)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown message type"):
            create_backend_message("nope")


class TestRegisteredCarriesTheTiming:
    """The three-timeout drift cannot recur: the runner is TOLD."""

    def test_registered_carries_heartbeat_interval_and_death_timeout(self):
        data = RegisteredMessage(runner_id="r").to_dict()
        assert data["heartbeat_interval"] == HEARTBEAT_INTERVAL
        assert data["death_timeout"] == DEATH_TIMEOUT
        assert data["protocol_version"] == PROTOCOL_VERSION

    def test_registered_defaults_to_idle_resume(self):
        msg = RegisteredMessage(runner_id="r")
        assert msg.resume_action == "idle"
        assert msg.resume_step_id is None

    def test_registration_timeout_bounds_the_first_frame(self):
        assert REGISTRATION_TIMEOUT == 10
        assert ACK_TIMEOUT == 5


class TestVersionNegotiation:
    def test_absent_version_is_supported(self):
        assert is_supported_protocol_version(None) is True

    def test_version_one_is_supported(self):
        assert is_supported_protocol_version(1) is True

    def test_version_two_is_not(self):
        assert is_supported_protocol_version(2) is False

    def test_error_text_names_both_sides(self):
        message = unsupported_version_message(2)
        assert "1" in message and "2" in message
        assert "backend speaks protocol version(s) 1" in message


class TestNormalizeArch:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("x86_64", "amd64"),
            ("amd64", "amd64"),
            ("x64", "amd64"),
            ("X86_64", "amd64"),
            ("aarch64", "arm64"),
            ("arm64", "arm64"),
            ("armv8", "arm64"),
            ("armv7l", "armv7"),
            ("armhf", "armv7"),
        ],
    )
    def test_alias_table(self, raw, expected):
        assert normalize_arch(raw) == expected

    def test_unknown_value_passes_through_lowercased(self):
        """An unrecognized arch must still be matchable against itself."""
        assert normalize_arch("RISCV64") == "riscv64"

    def test_none_is_empty(self):
        assert normalize_arch(None) == ""

    def test_whitespace_is_stripped(self):
        assert normalize_arch("  x86_64 ") == "amd64"


class TestNormalizeLabels:
    def test_arch_is_canonicalized(self):
        assert normalize_labels({"arch": "x86_64"})["arch"] == "amd64"

    def test_has_is_coerced_to_a_list(self):
        assert normalize_labels({"has": "gpio"})["has"] == ["gpio"]

    def test_other_labels_pass_through_verbatim(self):
        assert normalize_labels({"zone": "Workshop"})["zone"] == "Workshop"

    def test_empty_is_empty(self):
        assert normalize_labels(None) == {}


class TestBuildExecuteStepConfig:
    """Cross-agent contract #2 and #9."""

    def _context(self, **overrides):
        context = {
            "pipeline_run_id": "pr-1",
            "step_run_id": "sr-1",
            "step_index": 3,
            "step_execution_id": "se-1",
            "execution_key": "pr-1:3:1",
            "workspace_volume": "lazyaf-ws-pr-1",
            "retain_key": "pr-1",
            "repo_id": "r1",
            "clone_url": "http://backend:8000/git/r1.git",
            "branch": "main",
            "commit_sha": "2a513dd4",
            "control_mode": True,
        }
        context.update(overrides)
        return context

    def test_top_level_shape(self):
        config = build_execute_step_config(
            {"image": "lazyaf-base:dev", "command": "echo hi", "timeout": 1800},
            self._context(),
            step_config_file={"step_id": "se-1"},
        )
        assert set(config) == {
            "protocol_version",
            "backend_url",
            "workspace",
            "container",
            "control_files",
        }
        assert config["protocol_version"] == PROTOCOL_VERSION

    def test_workspace_carries_the_provisioning_inputs(self):
        config = build_execute_step_config({}, self._context(), step_config_file={})
        workspace = config["workspace"]
        assert workspace["volume"] == "lazyaf-ws-pr-1"
        assert workspace["retain_key"] == "pr-1"
        assert workspace["mount_path"] == "/workspace"
        assert workspace["clone_url"] == "http://backend:8000/git/r1.git"
        assert workspace["branch"] == "main"
        assert workspace["commit_sha"] == "2a513dd4"

    def test_control_mode_command_is_null(self):
        """The runtime reads the command from the config FILE, exactly as on
        the local path."""
        config = build_execute_step_config(
            {"command": "pytest -v"}, self._context(), step_config_file={}
        )
        assert config["container"]["command"] is None

    def test_stdout_mode_carries_the_command(self):
        config = build_execute_step_config(
            {"command": "pytest -v"}, self._context(control_mode=False)
        )
        assert config["container"]["command"] == "pytest -v"

    def test_environment_is_the_local_non_secret_table(self):
        config = build_execute_step_config(
            {"image": "lazyaf-base:dev"}, self._context(), step_config_file={}
        )
        env = config["container"]["environment"]
        assert env["LAZYAF_PIPELINE_RUN_ID"] == "pr-1"
        assert env["LAZYAF_STEP_RUN_ID"] == "sr-1"
        assert env["LAZYAF_STEP_INDEX"] == "3"
        assert env["LAZYAF_EXECUTION_KEY"] == "pr-1:3:1"
        assert env["LAZYAF_CONTROL"] == "1"
        assert env["LAZYAF_USAGE_PROVIDER"] == "self-hosted"
        assert env["CONFIG_PATH"] == "/workspace/.control/se-1.json"
        assert env["HOME"].endswith("/home")

    def test_control_files_are_keyed_by_absolute_path(self):
        config = build_execute_step_config(
            {},
            self._context(),
            step_config_file={"step_id": "se-1"},
            agent_config_file={"provider": "anthropic"},
        )
        assert set(config["control_files"]) == {
            "/workspace/.control/se-1.json",
            "/workspace/.control/agent.se-1.json",
        }

    def test_absent_agent_config_leaves_one_control_file(self):
        config = build_execute_step_config({}, self._context(), step_config_file={"a": 1})
        assert list(config["control_files"]) == ["/workspace/.control/se-1.json"]

    def test_secrets_never_reach_container_environment(self):
        """Cross-agent contract #9: the step JWT and secret_environment live
        ONLY inside control_files - the channel `docker inspect` cannot see.
        The remote twin of 12.5's secret-containment test."""
        sentinel = "sk-ant-SENTINEL-DO-NOT-LEAK"
        config = build_execute_step_config(
            {
                "image": "lazyaf-base:dev",
                "secret_environment": {"ANTHROPIC_API_KEY": sentinel},
            },
            self._context(),
            step_config_file={
                "auth_token": "jwt.sentinel.token",
                "environment": {"ANTHROPIC_API_KEY": sentinel},
            },
        )
        env_blob = repr(config["container"]["environment"])
        assert sentinel not in env_blob
        assert "jwt.sentinel.token" not in env_blob
        # ...and it IS carried, in the one place the agent put_archives.
        assert sentinel in repr(config["control_files"])

    def test_mounts_carry_explicit_addressing(self):
        """R6: never inferred from path shape, on either host."""
        config = build_execute_step_config(
            {
                "mounts": [
                    {
                        "addressing": "bind",
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                    }
                ]
            },
            self._context(),
            step_config_file={},
        )
        assert config["container"]["mounts"] == [
            {
                "addressing": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
                "mode": "rw",
            }
        ]

    def test_gpu_attribution_travels_when_set(self):
        config = build_execute_step_config(
            {},
            self._context(gpu_node_id="runpod-a100-80g", gpu_fraction=0.5),
            step_config_file={},
        )
        env = config["container"]["environment"]
        assert env["LAZYAF_GPU_NODE_ID"] == "runpod-a100-80g"
        assert env["LAZYAF_GPU_FRACTION"] == "0.5"

    def test_gpu_attribution_is_absent_when_unset(self):
        config = build_execute_step_config({}, self._context(), step_config_file={})
        assert "LAZYAF_GPU_NODE_ID" not in config["container"]["environment"]


class TestMessageIdentity:
    """`type` is an identity, not an argument."""

    @pytest.mark.parametrize(
        "msg",
        [
            AckMessage(step_id="s"),
            HeartbeatMessage(),
            LogMessage(step_id="s", lines=[]),
            StepCompleteMessage(step_id="s", exit_code=0),
            PingMessage(),
            PongMessage(),
            DrainMessage(),
            ErrorMessage(message="x"),
        ],
    )
    def test_type_cannot_be_passed_in(self, msg):
        with pytest.raises(TypeError):
            type(msg)(type="forged")  # noqa: A002

    def test_to_dict_puts_type_first(self):
        assert list(AckMessage(step_id="s").to_dict()) == ["type", "step_id"]
