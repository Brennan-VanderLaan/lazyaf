"""
The LazyAF agent harness (Milestone 14.2).

An inference server is not an agent. It has no loop, no tools, no notion of
being done, and no way to stop itself. This package supplies all four, so a
self-hosted OpenAI-compatible endpoint (ollama, vLLM, llama.cpp, LM Studio)
can run a real card the same way a vendor CLI does.

``HarnessExecutor`` is the only public name: it is the ``EXECUTORS`` entry the
12.5 wrapper builds, and every other module here is its internals —
``constants`` (every budget, named), ``client`` (the OpenAI-compatible HTTP
client, shared with the runner-local probe), ``tools`` (the six tools and
their sandbox), ``transcript`` (budget-and-elide context management),
``fallback`` (the no-tools text protocol), ``loop`` (the state machine and its
ten stop conditions) and ``executor`` (the executor itself).

Imports nothing from ``backend/app``: this code runs inside a step container
that has no database, no settings and no backend package.
"""
from .executor import HarnessExecutor

__all__ = ["HarnessExecutor"]
