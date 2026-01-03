"""
Execution services for Phase 12.1 & 12.2.

Phase 12.1:
- StepStateMachine: Tracks step execution state transitions
- IdempotencyStore: Ensures at-most-once execution semantics
- LocalExecutor: Spawns containers directly for instant execution

Phase 12.2:
- WorkspaceStateMachine: Manages workspace lifecycle
- WorkspaceLock: Shared/exclusive locking for workspaces
- ExecutionRouter: Routes steps to Local or Remote executors
- PipelineStateMachine: Manages pipeline run lifecycle
- TriggerDeduplicator: Prevents duplicate pipeline triggers
"""

from .step_state import (
    StepState,
    StepStateMachine,
    StepStateTransition,
    InvalidTransitionError,
)

from .idempotency import (
    ExecutionKey,
    IdempotencyStore,
    ExecutionResult as IdempotencyResult,
)

from .local_executor import (
    LocalExecutor,
    ExecutionConfig,
    ExecutionResult,
    ExecutionError,
    ContainerNotFoundError,
    TimeoutError,
)

# Phase 12.2 additions
from .workspace_state import (
    WorkspaceState,
    WorkspaceStateMachine,
    WorkspaceStateTransition,
    InvalidWorkspaceTransitionError,
)

from .workspace_locking import (
    LockType,
    WorkspaceLock,
    LockAcquisitionError,
    acquire_workspace_lock,
    release_workspace_lock,
    workspace_lock,
)

from .router import (
    ExecutorType,
    StepRequirements,
    RoutingDecision,
    ExecutionRouter,
)

from .pipeline_state import (
    PipelineRunState,
    PipelineStateMachine,
    PipelineStateTransition,
    InvalidPipelineTransitionError,
)

from .trigger_dedup import (
    TriggerKey,
    TriggerCheckResult,
    TriggerDeduplicator,
    DuplicateTriggerError,
)

__all__ = [
    # Step state (12.1)
    "StepState",
    "StepStateMachine",
    "StepStateTransition",
    "InvalidTransitionError",
    # Idempotency (12.1)
    "ExecutionKey",
    "IdempotencyStore",
    "IdempotencyResult",
    # LocalExecutor (12.1)
    "LocalExecutor",
    "ExecutionConfig",
    "ExecutionResult",
    "ExecutionError",
    "ContainerNotFoundError",
    "TimeoutError",
    # Workspace state (12.2)
    "WorkspaceState",
    "WorkspaceStateMachine",
    "WorkspaceStateTransition",
    "InvalidWorkspaceTransitionError",
    # Workspace locking (12.2)
    "LockType",
    "WorkspaceLock",
    "LockAcquisitionError",
    "acquire_workspace_lock",
    "release_workspace_lock",
    "workspace_lock",
    # Execution router (12.2)
    "ExecutorType",
    "StepRequirements",
    "RoutingDecision",
    "ExecutionRouter",
    # Pipeline state (12.2)
    "PipelineRunState",
    "PipelineStateMachine",
    "PipelineStateTransition",
    "InvalidPipelineTransitionError",
    # Trigger dedup (12.2)
    "TriggerKey",
    "TriggerCheckResult",
    "TriggerDeduplicator",
    "DuplicateTriggerError",
]
