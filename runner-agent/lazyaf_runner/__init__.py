"""
LazyAF Runner Agent.

Connects to LazyAF backend via WebSocket and executes steps.
"""

__version__ = "0.1.0"

from .agent import RunnerAgent
from .config import RunnerConfig

__all__ = ["RunnerAgent", "RunnerConfig"]
