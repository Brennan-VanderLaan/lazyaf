"""
Execution services for Phase 12.1 - LocalExecutor + Step State Machine.

This package provides:
- StepStateMachine: Tracks step execution state transitions
- IdempotencyStore: Ensures at-most-once execution semantics
- LocalExecutor: Spawns containers directly for instant execution
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

__all__ = [
    # Step state
    "StepState",
    "StepStateMachine",
    "StepStateTransition",
    "InvalidTransitionError",
    # Idempotency
    "ExecutionKey",
    "IdempotencyStore",
    "IdempotencyResult",
    # LocalExecutor
    "LocalExecutor",
    "ExecutionConfig",
    "ExecutionResult",
    "ExecutionError",
    "ContainerNotFoundError",
    "TimeoutError",
]
