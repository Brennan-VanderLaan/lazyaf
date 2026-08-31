"""Runner WebSocket wire protocol - Phase 12.6.

Single source of truth for every frame that crosses the runner socket (R3).
Plain ``dataclasses``, no pydantic: the contract suite
(``tdd/unit/execution/test_websocket_protocol.py``) constructs these classes
directly and asserts on ``to_dict()`` keys, so the shapes here ARE the spec.

Three constraints inherited from that suite and NOT negotiable:

1. ``RegisterMessage(runner_id=..., name=..., runner_type=..., labels={})``
   constructs from exactly those four kwargs and ``validate_runner_message``
   returns ``[]`` for a register carrying only those four. Auth and protocol
   version therefore cannot be REQUIRED message fields - auth happens at the
   HTTP upgrade (``authenticate_runner_connection``), and an absent
   ``protocol_version`` is read as 1.
2. ``to_dict()`` assertions check named keys, never key COUNT. Extra fields
   are legal, so every field added beyond the original four is defaulted.
3. ``parse_runner_message`` never raises ``KeyError``: every field is read
   with ``.get()`` and a default. The endpoint always calls
   ``validate_runner_message`` first; parse is not the gate.

Why ``type`` is ``field(init=False)``: it is an identity, not an argument.
A caller can neither forge nor omit it, and ``to_dict()`` always emits it
first.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any, Union

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# 1.1 Constants
# -----------------------------------------------------------------------------
#: Wire version the backend speaks. Bump ONLY when a required key changes
#: shape in `execute_step.config`; a new OPTIONAL key is not a bump.
PROTOCOL_VERSION = 1
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1})

REGISTRATION_TIMEOUT = 10   # runner must send register within 10s of connect
ACK_TIMEOUT = 5             # runner must ACK an assignment within 5s
HEARTBEAT_INTERVAL = 10     # runner sends heartbeat every 10s
DEATH_TIMEOUT = 30          # no heartbeat for 30s => dead

# Derived / operational. NAMED, never inline literals: failure_01 had a bare
# `5` for the read deadline fighting a 30s death timeout with no stated
# relationship between them.
#
# RECEIVE_TIMEOUT < DEATH_TIMEOUT is deliberate and must stay so: a read
# timeout provokes a server `ping` (keepalive); the death monitor is the SOLE
# authority on death.
RECEIVE_TIMEOUT = HEARTBEAT_INTERVAL * 2   # 20s: server-side read deadline
DEATH_MONITOR_INTERVAL = 5                 # death sweep tick
DISPATCH_SWEEP_INTERVAL = 15               # dispatcher self-heal tick
MAX_ASSIGN_ATTEMPTS = 3                    # per step, before failing it
NO_RUNNER_TIMEOUT = 300                    # no matching runner for 5min => fail
DRAIN_GRACE = 30                           # drain: finish current step, then close

# Back-pressure
MAX_MESSAGE_BYTES = 1_048_576
MAX_LOG_LINES_PER_MESSAGE = 500
MAX_LOG_LINE_BYTES = 16_384
INBOUND_BUDGET_MESSAGES = 200              # per connection per 10s window
INBOUND_BUDGET_WINDOW = 10                 # seconds, the rolling window above

#: Close codes (section 1.5). 1000 is normal / drain complete.
CLOSE_NORMAL = 1000
CLOSE_REGISTRATION_TIMEOUT = 4000
CLOSE_INVALID_REGISTRATION = 4001
CLOSE_UNSUPPORTED_VERSION = 4002
CLOSE_AUTH_FAILED = 4003
CLOSE_DUPLICATE_CONNECTION = 4004
CLOSE_BACK_PRESSURE = 4005
CLOSE_DRAINING = 4009

#: Reconnect actions the backend can put on `registered.resume_action`
#: (job_recovery.on_runner_reconnect returns exactly these).
RESUME_IDLE = "idle"
RESUME_CONTINUE = "continue"
RESUME_ABORT = "abort"


# -----------------------------------------------------------------------------
# 1.2 Message catalogue
# -----------------------------------------------------------------------------

class _MessageMixin:
    """Shared serialization for every protocol frame.

    Deliberately NOT a dataclass: a dataclass base with no fields still
    participates in field ordering, and every subclass wants `type` last in
    its own field list (it carries a default) but FIRST on the wire.
    """

    type: str

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"type": self.type}
        for f in fields(self):  # type: ignore[arg-type]
            if f.name == "type":
                continue
            data[f.name] = getattr(self, f.name)
        return data


# --- Runner -> Backend -------------------------------------------------------

@dataclass
class RegisterMessage(_MessageMixin):
    """First frame, sent within REGISTRATION_TIMEOUT of connect.

    Only `runner_id` is structurally required: everything else is defaulted
    so a pre-version agent and the four-kwarg contract constructor both work.
    Wire-level requirements (runner_id AND runner_type) are enforced by
    `validate_runner_message`, which is where a protocol error belongs.
    """
    runner_id: str
    name: str = ""
    runner_type: str = "generic"
    labels: dict = field(default_factory=dict)
    #: Absent on the wire => 1 (a pre-version agent). See section 1.4.
    protocol_version: int = PROTOCOL_VERSION
    agent_version: str = ""
    #: Last-resort auth channel; used ONLY when neither the Authorization
    #: header nor the ?token= query parameter is present.
    token: str | None = None
    #: {"step_id": ...} - the assignment this runner still believes it holds.
    #: Makes the reconnect protocol speakable on the wire (section 2.6).
    resume: dict | None = None
    type: str = field(default="register", init=False)


@dataclass
class AckMessage(_MessageMixin):
    """Assignment accepted. Must arrive within ACK_TIMEOUT."""
    step_id: str
    type: str = field(default="ack", init=False)


@dataclass
class HeartbeatMessage(_MessageMixin):
    """Sent every HEARTBEAT_INTERVAL, always, including mid-step."""
    type: str = field(default="heartbeat", init=False)


@dataclass
class LogMessage(_MessageMixin):
    """RUNNER-ORIGIN log lines only (section 1.6).

    The step container POSTs its own logs to /api/steps/{id}/logs with the
    step JWT. This frame carries the lines a step container CANNOT emit
    because it does not exist yet or failed to start.

    `seq` is optional and forensic: a gap across a reconnect produces a
    visible "[runner] WARNING: log seq gap" line rather than silent loss.
    """
    step_id: str
    lines: list = field(default_factory=list)
    seq: int | None = None
    type: str = field(default="log", init=False)


@dataclass
class StepCompleteMessage(_MessageMixin):
    """Terminal outcome, exactly once per assignment.

    `error` is ALWAYS emitted, null on success - never omitted, so a consumer
    never has to distinguish "absent" from "no error".
    """
    step_id: str
    exit_code: int
    error: str | None = None
    type: str = field(default="step_complete", init=False)


@dataclass
class PingMessage(_MessageMixin):
    """Liveness probe. Legal in BOTH directions (section 1.2): a runner may
    probe the backend, and the backend sends one after RECEIVE_TIMEOUT as a
    keepalive. One class, one `type` string, no direction-specific twin."""
    type: str = field(default="ping", init=False)


# --- Backend -> Runner -------------------------------------------------------

@dataclass
class RegisteredMessage(_MessageMixin):
    """Registration accepted.

    Carries `heartbeat_interval` and `death_timeout` so the runner learns the
    server's timing from the server instead of configuring it independently.
    failure_01 had the agent's 10s, the server's 20s read deadline and the
    30s death timeout drifting apart with nothing to reconcile them; the
    three-timeout drift cannot recur while the runner is told.

    `resume_action` is the answer to `register.resume`, straight from
    JobRecoveryService.on_runner_reconnect: "idle" | "continue" | "abort".
    An "abort" is followed immediately by cancel_step{reason:"reassigned"}.
    """
    runner_id: str
    protocol_version: int = PROTOCOL_VERSION
    heartbeat_interval: int = HEARTBEAT_INTERVAL
    death_timeout: int = DEATH_TIMEOUT
    resume_action: str = RESUME_IDLE
    resume_step_id: str | None = None
    type: str = field(default="registered", init=False)


@dataclass
class ExecuteStepMessage(_MessageMixin):
    """Assignment. `config` is produced ONLY by build_execute_step_config."""
    step_id: str
    execution_key: str
    config: dict = field(default_factory=dict)
    type: str = field(default="execute_step", init=False)


@dataclass
class CancelStepMessage(_MessageMixin):
    """User cancel, reassignment abort, or run failure."""
    step_id: str
    reason: str = ""
    type: str = field(default="cancel_step", init=False)


@dataclass
class CleanupWorkspaceMessage(_MessageMixin):
    """Run completed: the agent may reap the volume for this retain_key."""
    retain_key: str
    type: str = field(default="cleanup_workspace", init=False)


@dataclass
class DrainMessage(_MessageMixin):
    """Graceful shutdown: stop accepting, finish the current step, close."""
    reason: str = ""
    type: str = field(default="drain", init=False)


@dataclass
class PongMessage(_MessageMixin):
    """Reply to ping / heartbeat."""
    type: str = field(default="pong", init=False)


@dataclass
class ErrorMessage(_MessageMixin):
    """Protocol / validation / auth error.

    `fatal=True` means the runner must NOT retry (auth, unsupported protocol
    version). A non-fatal error keeps the connection open: one malformed
    mid-session frame must never kill a live step.
    """
    message: str
    code: str | None = None
    fatal: bool = False
    type: str = field(default="error", init=False)


RunnerMessage = Union[
    RegisterMessage,
    AckMessage,
    HeartbeatMessage,
    LogMessage,
    StepCompleteMessage,
    PingMessage,
]

BackendMessage = Union[
    RegisteredMessage,
    ExecuteStepMessage,
    CancelStepMessage,
    CleanupWorkspaceMessage,
    DrainMessage,
    PongMessage,
    PingMessage,
    ErrorMessage,
]


# -----------------------------------------------------------------------------
# 1.8 Message helpers
# -----------------------------------------------------------------------------

#: Required-field table. An empty tuple means "no required fields".
#: NOTE: register requires runner_id and runner_type - NOT name, NOT labels.
RUNNER_MESSAGE_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "register": ("runner_id", "runner_type"),
    "ack": ("step_id",),
    "heartbeat": (),
    "ping": (),
    "log": ("step_id", "lines"),
    "step_complete": ("step_id", "exit_code"),
}

BACKEND_MESSAGE_TYPES: tuple[str, ...] = (
    "registered",
    "execute_step",
    "cancel_step",
    "cleanup_workspace",
    "drain",
    "pong",
    "ping",
    "error",
)


def parse_runner_message(data: dict) -> RunnerMessage:
    """Build the typed frame for an inbound runner message.

    Raises ValueError("Missing message type") when 'type' is absent and
    ValueError(f"Unknown message type: {t}") otherwise. Every field is read
    with .get() and a default, so parse never raises KeyError on a partial
    payload - validate_runner_message is the gate, parse is the constructor.
    """
    msg_type = data.get("type")
    if not msg_type:
        raise ValueError("Missing message type")

    if msg_type == "register":
        return RegisterMessage(
            runner_id=data.get("runner_id", ""),
            name=data.get("name", "") or "",
            runner_type=data.get("runner_type", "") or "",
            labels=data.get("labels") or {},
            protocol_version=_as_int(data.get("protocol_version"), PROTOCOL_VERSION),
            agent_version=data.get("agent_version", "") or "",
            token=data.get("token"),
            resume=data.get("resume"),
        )
    if msg_type == "ack":
        return AckMessage(step_id=data.get("step_id", ""))
    if msg_type == "heartbeat":
        return HeartbeatMessage()
    if msg_type == "ping":
        return PingMessage()
    if msg_type == "log":
        return LogMessage(
            step_id=data.get("step_id", ""),
            lines=list(data.get("lines") or []),
            seq=data.get("seq"),
        )
    if msg_type == "step_complete":
        return StepCompleteMessage(
            step_id=data.get("step_id", ""),
            exit_code=_as_int(data.get("exit_code"), 0),
            error=data.get("error"),
        )

    raise ValueError(f"Unknown message type: {msg_type}")


def validate_runner_message(data: dict) -> list[str]:
    """Return the list of problems with an inbound frame; [] means valid.

    Membership, not truthiness: `exit_code: 0` and `lines: []` are PRESENT
    and therefore valid. A falsiness check here would reject every successful
    step completion.
    """
    msg_type = data.get("type")
    if not msg_type:
        return ["Missing 'type' field"]

    required = RUNNER_MESSAGE_REQUIRED_FIELDS.get(msg_type)
    if required is None:
        return [f"Unknown message type: {msg_type}"]

    return [f"Missing '{name}' field" for name in required if name not in data]


def create_backend_message(msg_type: str, **kwargs) -> BackendMessage:
    """Build a backend->runner frame by type name.

    The one constructor the endpoint uses, so an unknown type is a loud
    ValueError at the call site instead of a silently unsent frame.
    """
    if msg_type == "registered":
        return RegisteredMessage(**kwargs)
    if msg_type == "execute_step":
        return ExecuteStepMessage(**kwargs)
    if msg_type == "cancel_step":
        return CancelStepMessage(**kwargs)
    if msg_type == "cleanup_workspace":
        return CleanupWorkspaceMessage(**kwargs)
    if msg_type == "drain":
        return DrainMessage(**kwargs)
    if msg_type == "pong":
        return PongMessage(**kwargs)
    if msg_type == "ping":
        return PingMessage(**kwargs)
    if msg_type == "error":
        return ErrorMessage(**kwargs)

    raise ValueError(f"Unknown message type: {msg_type}")


def is_supported_protocol_version(version: int | None) -> bool:
    """An absent version is a pre-version agent and reads as 1 (section 1.4)."""
    if version is None:
        return True
    return version in SUPPORTED_PROTOCOL_VERSIONS


def unsupported_version_message(offered: int) -> str:
    """The exact wording of the version-mismatch error frame."""
    speaks = ", ".join(str(v) for v in sorted(SUPPORTED_PROTOCOL_VERSIONS))
    return f"backend speaks protocol version(s) {speaks}, runner offered {offered}"


# -----------------------------------------------------------------------------
# Architecture normalization (cross-agent contract #5)
# -----------------------------------------------------------------------------

_ARCH_ALIASES: dict[str, str] = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "x64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv8": "arm64",
    "armv7l": "armv7",
    "armhf": "armv7",
}


def normalize_arch(value: Any) -> str:
    """Canonicalize a machine architecture string.

    Applied BACKEND-SIDE to both register labels and parsed requirements, so
    there is exactly one implementation and the agent ships raw
    ``platform.machine()`` (R3). An unknown value passes through lowercased
    rather than being dropped - an unrecognized arch must still be matchable
    against itself.
    """
    if value is None:
        return ""
    text = str(value).strip().lower()
    return _ARCH_ALIASES.get(text, text)


def normalize_labels(labels: dict | None) -> dict:
    """Normalize a runner's advertised labels for storage and matching.

    Only `arch` is canonicalized; `has` is coerced to a list so a single
    string ("has=gpio") and a repeated key both compare as sets. Everything
    else is stored verbatim - free-form labels are the point.
    """
    if not labels:
        return {}
    normalized = dict(labels)
    if "arch" in normalized:
        normalized["arch"] = normalize_arch(normalized["arch"])
    if "has" in normalized:
        normalized["has"] = _as_list(normalized["has"])
    return normalized


# -----------------------------------------------------------------------------
# 3.2 execute_step.config - the sole producer (cross-agent contract #2)
# -----------------------------------------------------------------------------

def build_execute_step_config(
    step_config: dict,
    exec_context: dict,
    step_config_file: dict | None = None,
    agent_config_file: dict | None = None,
) -> dict:
    """Assemble the `execute_step.config` payload (section 3.2).

    The ONLY producer of that dict. It lives here, next to the message class
    that carries it, so the backend cannot drift from the agent's consumer.

    Args:
        step_config: the pipeline step's config block (image, command,
            timeout, mounts, memory_limit, environment, ...). Secrets in
            `secret_environment` are NOT read here - they belong inside the
            control file the caller already produced.
        exec_context: the executor's execution_context - pipeline_run_id,
            step_run_id, step_index, step_id (the graph node id, absent on a
            marker StepRun), step_execution_id, execution_key,
            workspace_volume, and the workspace provisioning inputs
            (repo_id, clone_url, branch, commit_sha, retain_key).
        step_config_file: verbatim `generate_step_config` output, or None for
            a non-control-mode step.
        agent_config_file: verbatim `generate_agent_config` output, or None.

    Secret boundary (cross-agent contract #9): the step JWT and
    `secret_environment` appear ONLY inside `control_files`, which the agent
    writes into the volume via put_archive. They never enter
    `container.environment`, which is what `docker inspect` shows.
    """
    # Lazy: keeps this module importable with no docker/config dependency,
    # which is what lets the dormant contract suite import it standalone.
    from app.config import get_settings
    from app.services.execution.local_executor import (
        AGENT_CONFIG_PREFIX,
        CONTROL_CONFIG_DIR,
        DEFAULT_USAGE_PROVIDER,
        MountSpec,
    )

    settings = get_settings()
    backend_url = getattr(settings, "container_backend_url", "http://backend:8000")
    step_execution_id = exec_context["step_execution_id"]
    execution_key = exec_context.get("execution_key", "")
    control_mode = bool(exec_context.get("control_mode"))

    timeout = step_config.get("timeout", 300)
    working_dir = step_config.get("working_dir", settings.step_working_dir)
    user_env = step_config.get("environment", {}) or {}

    control_root = f"/workspace/{CONTROL_CONFIG_DIR}"
    config_path = f"{control_root}/{step_execution_id}.json"
    agent_path = f"{control_root}/{AGENT_CONFIG_PREFIX}{step_execution_id}.json"

    # Exactly LocalExecutor's NON-SECRET env table, plus CONFIG_PATH.
    #
    # REQUESTED EDIT (flagged in the wave report): local_executor.py builds
    # this table inline. Two builders for one table is a drift risk; the fix
    # is to extract `build_step_environment(step_config, execution_context)`
    # in local_executor.py and call it from both. local_executor.py is not
    # owned by this wave's split, so the duplication is deliberate and
    # named rather than silently introduced.
    environment: dict[str, str] = {
        "HOME": settings.step_home_dir,
        **{str(k): str(v) for k, v in user_env.items()},
        "LAZYAF_PIPELINE_RUN_ID": str(exec_context["pipeline_run_id"]),
        "LAZYAF_STEP_RUN_ID": str(exec_context["step_run_id"]),
        "LAZYAF_STEP_INDEX": str(exec_context["step_index"]),
        "LAZYAF_EXECUTION_KEY": str(execution_key),
        "LAZYAF_BACKEND_URL": backend_url,
        "LAZYAF_CONTROL": "1" if control_mode else "0",
        "LAZYAF_USAGE_PROVIDER": str(
            step_config.get("usage_provider") or DEFAULT_USAGE_PROVIDER
        ),
    }
    # LAZYAF_STEP_ID (12.8) - see the note in local_executor.py. It matters
    # MORE on this lane than on the local one: the remote step's container is
    # created on another host by an agent that never sees the graph, so the
    # node id its author wrote is only knowable in-container if it travels
    # here. scripts/verify_executor.py runs on the local lane today, but
    # `requires:` could move it tomorrow and the gate must not care which.
    for key, source in (
        ("LAZYAF_STEP_ID", exec_context.get("step_id")),
        ("LAZYAF_ROLE", step_config.get("role")),
        ("LAZYAF_GPU_NODE_ID", exec_context.get("gpu_node_id")),
        ("LAZYAF_GPU_FRACTION", exec_context.get("gpu_fraction")),
    ):
        if source not in (None, ""):
            environment[key] = str(source)
    if control_mode:
        environment["CONFIG_PATH"] = config_path

    # Mounts travel with EXPLICIT addressing (R6) - never inferred from path
    # shape on the far side either. A remote agent rejects any `bind` not on
    # its own allowlist: a backend must not be able to bind arbitrary host
    # paths on a machine it does not own.
    mounts: list[dict] = []
    for raw in step_config.get("mounts", []) or []:
        spec = MountSpec.from_config(raw)
        mounts.append(
            {
                "addressing": spec.addressing.value,
                "source": spec.source,
                "target": spec.target,
                "mode": spec.mode,
            }
        )

    control_files: dict[str, dict] = {}
    if step_config_file is not None:
        control_files[config_path] = step_config_file
    if agent_config_file is not None:
        control_files[agent_path] = agent_config_file

    return {
        "protocol_version": PROTOCOL_VERSION,
        "backend_url": backend_url,
        "workspace": {
            "volume": exec_context.get("workspace_volume"),
            "retain_key": exec_context.get("retain_key")
            or exec_context.get("pipeline_run_id"),
            "mount_path": "/workspace",
            "repo_id": exec_context.get("repo_id"),
            "clone_url": exec_context.get("clone_url"),
            "branch": exec_context.get("branch"),
            "commit_sha": exec_context.get("commit_sha"),
        },
        "container": {
            "image": step_config.get("image", settings.step_default_image),
            # None in control mode: the runtime reads the command out of the
            # config FILE, exactly as on the local path.
            "command": None if control_mode else step_config.get("command"),
            "working_dir": working_dir,
            "timeout": timeout,
            "memory_limit": step_config.get("memory_limit"),
            "mounts": mounts,
            "environment": environment,
            "control_mode": control_mode,
        },
        "control_files": control_files,
    }


# -----------------------------------------------------------------------------
# small coercions
# -----------------------------------------------------------------------------

def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "REGISTRATION_TIMEOUT",
    "ACK_TIMEOUT",
    "HEARTBEAT_INTERVAL",
    "DEATH_TIMEOUT",
    "RECEIVE_TIMEOUT",
    "DEATH_MONITOR_INTERVAL",
    "DISPATCH_SWEEP_INTERVAL",
    "MAX_ASSIGN_ATTEMPTS",
    "NO_RUNNER_TIMEOUT",
    "DRAIN_GRACE",
    "MAX_MESSAGE_BYTES",
    "MAX_LOG_LINES_PER_MESSAGE",
    "MAX_LOG_LINE_BYTES",
    "INBOUND_BUDGET_MESSAGES",
    "INBOUND_BUDGET_WINDOW",
    "CLOSE_NORMAL",
    "CLOSE_REGISTRATION_TIMEOUT",
    "CLOSE_INVALID_REGISTRATION",
    "CLOSE_UNSUPPORTED_VERSION",
    "CLOSE_AUTH_FAILED",
    "CLOSE_DUPLICATE_CONNECTION",
    "CLOSE_BACK_PRESSURE",
    "CLOSE_DRAINING",
    "RESUME_IDLE",
    "RESUME_CONTINUE",
    "RESUME_ABORT",
    "RegisterMessage",
    "AckMessage",
    "HeartbeatMessage",
    "LogMessage",
    "StepCompleteMessage",
    "PingMessage",
    "RegisteredMessage",
    "ExecuteStepMessage",
    "CancelStepMessage",
    "CleanupWorkspaceMessage",
    "DrainMessage",
    "PongMessage",
    "ErrorMessage",
    "RunnerMessage",
    "BackendMessage",
    "RUNNER_MESSAGE_REQUIRED_FIELDS",
    "BACKEND_MESSAGE_TYPES",
    "parse_runner_message",
    "validate_runner_message",
    "create_backend_message",
    "is_supported_protocol_version",
    "unsupported_version_message",
    "normalize_arch",
    "normalize_labels",
    "build_execute_step_config",
]
