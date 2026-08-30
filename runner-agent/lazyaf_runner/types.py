"""Plain data shapes every orchestrator speaks - Phase 12.6, section 4.2.

HARD CONSTRAINT, pinned by ``tests/test_orchestrator_seam.py``: this module and
``orchestrator/base.py`` import nothing from ``docker``. They are the seam a
socketless runpod-style pod plugs a ``NativeOrchestrator`` into without touching
the wire protocol, so a docker import here would silently preclude the very
thing the seam exists for.

Imports are stdlib only: dataclasses, typing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

#: Where the backend addresses control files. ``execute_step.config`` keys them
#: by absolute in-container path; the tar builder wants basenames.
CONTROL_FILE_ROOT = "/workspace/.control"

#: What an orchestrator calls to emit RUNNER-ORIGIN log lines. It must be
#: non-blocking: the agent's sink appends to a bounded queue and returns.
LogSink = Callable[[Sequence[str]], None]


class MountRejected(ValueError):
    """A mount request this runner refuses to honor.

    Two causes, both deliberate: addressing was not declared explicitly (R6 -
    volume vs bind is never inferred from path shape), or a bind source is not
    on this runner's allowlist. A backend must not be able to bind arbitrary
    host paths on a machine it does not own.
    """


@dataclass(frozen=True)
class MountRequest:
    """One mount from ``execute_step.config.container.mounts``."""

    addressing: str  # "volume" | "bind"
    source: str
    target: str
    mode: str = "rw"

    @classmethod
    def from_config(cls, raw: Any) -> "MountRequest":
        if not isinstance(raw, dict):
            raise MountRejected(
                f"mount config must be a dict, got {type(raw).__name__}"
            )
        addressing = raw.get("addressing")
        if addressing not in ("volume", "bind"):
            raise MountRejected(
                "mount config requires explicit 'addressing' ('volume' | 'bind') - "
                "addressing is never inferred from path shape"
            )
        if "source" not in raw or "target" not in raw:
            raise MountRejected("mount config requires 'source' and 'target'")
        return cls(
            addressing=addressing,
            source=str(raw["source"]),
            target=str(raw["target"]),
            mode=str(raw.get("mode", "rw")),
        )


@dataclass(frozen=True)
class StepAssignment:
    """An ``execute_step`` frame, parsed.

    The accessors below are the ONLY place the agent reaches into the config
    dict, so a backend-side shape change (section 3.2) breaks in one file
    instead of five.
    """

    step_id: str
    execution_key: str
    config: dict = field(default_factory=dict)

    # --- top-level sections ------------------------------------------------
    @property
    def protocol_version(self) -> int:
        try:
            return int(self.config.get("protocol_version", 1))
        except (TypeError, ValueError):
            return 1

    @property
    def backend_url(self) -> str:
        return str(self.config.get("backend_url") or "")

    @property
    def workspace(self) -> dict:
        return self.config.get("workspace") or {}

    @property
    def container(self) -> dict:
        return self.config.get("container") or {}

    @property
    def control_files(self) -> dict:
        return self.config.get("control_files") or {}

    # --- workspace ---------------------------------------------------------
    @property
    def volume(self) -> str:
        return str(self.workspace.get("volume") or "")

    @property
    def retain_key(self) -> str:
        return str(self.workspace.get("retain_key") or "")

    @property
    def mount_path(self) -> str:
        return str(self.workspace.get("mount_path") or "/workspace")

    @property
    def repo_id(self) -> str:
        return str(self.workspace.get("repo_id") or "")

    @property
    def clone_url(self) -> str:
        return str(self.workspace.get("clone_url") or "")

    @property
    def branch(self) -> str:
        return str(self.workspace.get("branch") or "main")

    @property
    def commit_sha(self) -> str | None:
        return self.workspace.get("commit_sha") or None

    # --- container ---------------------------------------------------------
    @property
    def image(self) -> str:
        return str(self.container.get("image") or "")

    @property
    def command(self) -> Any:
        return self.container.get("command")

    @property
    def working_dir(self) -> str:
        return str(self.container.get("working_dir") or "/workspace/repo")

    @property
    def timeout(self) -> int:
        try:
            return int(self.container.get("timeout", 300))
        except (TypeError, ValueError):
            return 300

    @property
    def memory_limit(self) -> str | None:
        return self.container.get("memory_limit")

    @property
    def environment(self) -> dict:
        env = self.container.get("environment") or {}
        return {str(k): str(v) for k, v in env.items()}

    @property
    def control_mode(self) -> bool:
        return bool(self.container.get("control_mode"))

    @property
    def mounts(self) -> list[MountRequest]:
        return [MountRequest.from_config(raw) for raw in (self.container.get("mounts") or [])]

    # --- logging -----------------------------------------------------------
    def redacted_summary(self) -> dict:
        """What the agent is allowed to log about an assignment (section 4.3).

        NEVER the config itself: ``control_files`` carries the step JWT and
        ``secret_environment``. Only key NAMES, the image, the volume and the
        resolved backend URL. Pinned by ``tests/test_secret_hygiene.py``.
        """
        return {
            "step_id": self.step_id,
            "config_keys": sorted(self.config.keys()),
            "container_keys": sorted(self.container.keys()),
            "control_file_paths": sorted(self.control_files.keys()),
            "image": self.image,
            "volume": self.volume,
            "backend_url": self.backend_url,
        }


@dataclass(frozen=True)
class StepOutcome:
    """A terminal step result, ready to become ``step_complete``.

    ``error`` is ALWAYS carried, ``None`` on success - never omitted, matching
    the wire contract (section 1.2).
    """

    exit_code: int
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and self.error is None


__all__ = [
    "CONTROL_FILE_ROOT",
    "LogSink",
    "MountRejected",
    "MountRequest",
    "StepAssignment",
    "StepOutcome",
]
