"""
Runner Common - the AGENT WRAPPER surface (Phase 12.6).

This package used to be the polling runner: a job-claiming `entrypoint`, the
`.lazyaf-context` directory management in `context_helpers`, and the
backend-callback plumbing in `job_helpers`. All three left with the polling
stack - a step's status, logs, heartbeat, test results and usage now travel
the in-container control runtime's `POST /api/steps/*` calls, which work
identically whether the container was started by the backend or by a remote
runner agent.

What survives is what an AGENT STEP actually needs inside its container:

- git_helpers:   git operations (clone, checkout, push)
- agent_config:  reading the agent config file the executor writes
- agent_wrapper: the entry point an agent step container execs
- executors:     agent-specific CLI invocation (claude, gemini, mock)
- usage:         scraping the CLI's own token/cost report
- pytest_lazyaf: the pytest plugin that ties test results back to a step

The package still installs into the agent images unchanged; a broken import
here fails every agent step image build, which is why
tdd/unit/services/test_no_legacy_code.py imports each surviving module.
"""

from . import agent_config
from . import agent_wrapper
from . import executors
from . import git_helpers
from . import usage

__all__ = [
    "agent_config",
    "agent_wrapper",
    "executors",
    "git_helpers",
    "usage",
]
