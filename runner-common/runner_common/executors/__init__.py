"""
Agent executors for different AI backends.

Each executor implements the AgentExecutor protocol and provides
agent-specific CLI invocation logic.
"""

from .base import AgentExecutor, ExecutorConfig, ExecutorResult
from .claude import ClaudeExecutor
from .gemini import GeminiExecutor
from .mock import MockExecutor

__all__ = [
    "AgentExecutor",
    "ExecutorConfig",
    "ExecutorResult",
    "ClaudeExecutor",
    "GeminiExecutor",
    "MockExecutor",
]
