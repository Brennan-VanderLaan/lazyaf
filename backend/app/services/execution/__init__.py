"""
Execution services for step lifecycle management.

This module provides:
- StepStateMachine: State transitions for step executions
- ExecutionService: Idempotent step execution management
- LocalExecutor: Docker-based local step execution
- Recovery: Crash recovery for orphaned step executions
"""
from app.services.execution.state_machine import StepStateMachine, StepExecutionStatus
from app.services.execution.idempotency import (
    ExecutionService,
    generate_execution_key,
    parse_execution_key,
)
from app.services.execution.local_executor import LocalExecutor, DEFAULT_STEP_IMAGE
from app.services.execution.recovery import (
    recover_orphaned_executions,
    get_orphaned_execution_count,
    cleanup_old_completed_executions,
)

__all__ = [
    "StepStateMachine",
    "StepExecutionStatus",
    "ExecutionService",
    "generate_execution_key",
    "parse_execution_key",
    "LocalExecutor",
    "DEFAULT_STEP_IMAGE",
    "recover_orphaned_executions",
    "get_orphaned_execution_count",
    "cleanup_old_completed_executions",
]
