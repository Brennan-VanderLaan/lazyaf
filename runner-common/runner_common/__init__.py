"""
runner-common: Shared utilities for LazyAF runners.

This package contains code shared between Claude and Gemini runners,
extracted as part of Phase 12.0 to eliminate duplication.
"""

from .git_helpers import (
    GitError,
    clone,
    checkout,
    get_sha,
    push,
    commit,
    get_diff,
    get_current_branch,
    has_uncommitted_changes,
    add,
    fetch,
    merge,
    config_set,
)

from .context_helpers import (
    CONTEXT_DIR,
    init_context_directory,
    write_step_log,
    read_step_log,
    update_context_metadata,
    get_context_metadata,
    cleanup_context_directory,
    get_previous_step_logs,
    commit_context_changes,
)

from .job_helpers import (
    JobHelpers,
    HeartbeatError,
    ConnectionError,
    send_heartbeat,
    report_status,
    complete_job,
    send_log,
    poll_for_job,
)

from .command_helpers import (
    run_command,
    run_command_streaming,
    cleanup_workspace,
)

from .test_helpers import (
    TestResults,
    detect_test_framework,
    parse_test_output,
    parse_pytest_output,
    parse_jest_output,
    parse_go_test_output,
    parse_cargo_test_output,
    should_run_tests_command,
)

from .base_runner import (
    BaseRunner,
    JobResult,
)

from .claude_runner import ClaudeRunner
from .gemini_runner import GeminiRunner
from .entrypoint import get_runner, main as run_entrypoint

__version__ = "0.1.0"

__all__ = [
    # git_helpers
    "GitError",
    "clone",
    "checkout",
    "get_sha",
    "push",
    "commit",
    "get_diff",
    "get_current_branch",
    "has_uncommitted_changes",
    "add",
    "fetch",
    "merge",
    "config_set",
    # context_helpers
    "CONTEXT_DIR",
    "init_context_directory",
    "write_step_log",
    "read_step_log",
    "update_context_metadata",
    "get_context_metadata",
    "cleanup_context_directory",
    "get_previous_step_logs",
    "commit_context_changes",
    # job_helpers
    "JobHelpers",
    "HeartbeatError",
    "ConnectionError",
    "send_heartbeat",
    "report_status",
    "complete_job",
    "send_log",
    "poll_for_job",
    # command_helpers
    "run_command",
    "run_command_streaming",
    "cleanup_workspace",
    # test_helpers
    "TestResults",
    "detect_test_framework",
    "parse_test_output",
    "parse_pytest_output",
    "parse_jest_output",
    "parse_go_test_output",
    "parse_cargo_test_output",
    "should_run_tests_command",
    # base_runner
    "BaseRunner",
    "JobResult",
    # runners
    "ClaudeRunner",
    "GeminiRunner",
    "get_runner",
    "run_entrypoint",
]
