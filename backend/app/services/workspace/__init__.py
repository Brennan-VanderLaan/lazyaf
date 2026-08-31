# Workspace services - Phase 12.2
# Provides workspace lifecycle management, locking, and pipeline state machines

from app.services.workspace.worker_key import (
    DEFAULT_WORKER_KEY,
    MAX_WORKER_KEY_LENGTH,
    WORKSPACE_KEY_CONFIG_FIELD,
    validate_worker_key,
    worker_key_for_step,
)

from app.services.workspace.state_machine import (
    WorkspaceStatus,
    WorkspaceStateMachine,
    generate_volume_name,
    parse_volume_name,
    parse_volume_name_parts,
    is_orphaned,
)

from app.services.workspace.locking import (
    LockType,
    Lock,
    LockTimeoutError,
    WorkspaceLockManager,
)

from app.services.workspace.pipeline_state_machine import (
    PipelineStatus,
    PipelineStateMachine,
)

from app.services.workspace.trigger_dedup import (
    generate_trigger_key,
    parse_trigger_key,
    TriggerDeduplicator,
)

from app.services.workspace.execution_router import (
    RoutingDecision,
    ExecutorHandle,
    ExecutionRouter,
)

__all__ = [
    # Workspace lanes (M13-1)
    "DEFAULT_WORKER_KEY",
    "MAX_WORKER_KEY_LENGTH",
    "WORKSPACE_KEY_CONFIG_FIELD",
    "validate_worker_key",
    "worker_key_for_step",
    # Workspace state machine
    "WorkspaceStatus",
    "WorkspaceStateMachine",
    "generate_volume_name",
    "parse_volume_name",
    "parse_volume_name_parts",
    "is_orphaned",
    # Workspace locking
    "LockType",
    "Lock",
    "LockTimeoutError",
    "WorkspaceLockManager",
    # Pipeline state machine
    "PipelineStatus",
    "PipelineStateMachine",
    # Trigger deduplication
    "generate_trigger_key",
    "parse_trigger_key",
    "TriggerDeduplicator",
    # Execution router
    "RoutingDecision",
    "ExecutorHandle",
    "ExecutionRouter",
]
