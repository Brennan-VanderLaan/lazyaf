"""
Test framework detection and result parsing.

Detects test frameworks and parses their output to extract
pass/fail/skip counts for reporting.
"""

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TestResults:
    """Results from running tests."""
    tests_run: bool
    tests_passed: bool
    pass_count: int
    fail_count: int
    skip_count: int
    output: str

    def to_dict(self) -> dict:
        """Convert to dictionary for API reporting."""
        return {
            "tests_run": self.tests_run,
            "tests_passed": self.tests_passed,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "skip_count": self.skip_count,
            "output": self.output,
        }


def detect_test_framework(workspace: Path | str) -> str | None:
    """
    Detect which test framework is used in a project.

    Args:
        workspace: Path to the workspace/repo

    Returns:
        Framework name or None if not detected
    """
    workspace = Path(workspace)

    # Python - pytest
    if (workspace / "pytest.ini").exists():
        return "pytest"
    if (workspace / "pyproject.toml").exists():
        pyproject = (workspace / "pyproject.toml").read_text()
        if "[tool.pytest" in pyproject:
            return "pytest"

    # Python - check for test files
    if list(workspace.glob("**/test_*.py")) or list(workspace.glob("**/*_test.py")):
        return "pytest"

    # JavaScript/TypeScript - Jest
    if (workspace / "jest.config.js").exists() or (workspace / "jest.config.ts").exists():
        return "jest"
    if (workspace / "package.json").exists():
        pkg = (workspace / "package.json").read_text()
        if '"jest"' in pkg:
            return "jest"

    # JavaScript/TypeScript - Vitest
    if (workspace / "vitest.config.ts").exists() or (workspace / "vitest.config.js").exists():
        return "vitest"

    # Go
    if list(workspace.glob("**/*_test.go")):
        return "go-test"

    # Rust
    if (workspace / "Cargo.toml").exists():
        return "cargo-test"

    return None


def parse_pytest_output(output: str) -> TestResults:
    """
    Parse pytest output for test results.

    Args:
        output: Combined stdout/stderr from pytest

    Returns:
        TestResults with parsed counts
    """
    # Default values
    pass_count = 0
    fail_count = 0
    skip_count = 0
    tests_passed = True

    # Look for summary line like: "5 passed, 2 failed, 1 skipped in 1.23s"
    summary_pattern = r"(\d+)\s+passed"
    match = re.search(summary_pattern, output)
    if match:
        pass_count = int(match.group(1))

    fail_pattern = r"(\d+)\s+failed"
    match = re.search(fail_pattern, output)
    if match:
        fail_count = int(match.group(1))
        tests_passed = False

    skip_pattern = r"(\d+)\s+skipped"
    match = re.search(skip_pattern, output)
    if match:
        skip_count = int(match.group(1))

    # Also check for error pattern
    if "error" in output.lower() and fail_count == 0:
        # Collection error or similar
        fail_count = 1
        tests_passed = False

    tests_run = pass_count + fail_count + skip_count > 0

    return TestResults(
        tests_run=tests_run,
        tests_passed=tests_passed,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        output=output,
    )


def parse_jest_output(output: str) -> TestResults:
    """
    Parse Jest output for test results.

    Args:
        output: Combined stdout/stderr from jest

    Returns:
        TestResults with parsed counts
    """
    pass_count = 0
    fail_count = 0
    skip_count = 0
    tests_passed = True

    # Jest format: "Tests:  2 passed, 1 failed, 3 total"
    tests_pattern = r"Tests:\s+(?:(\d+)\s+passed)?(?:,\s*)?(?:(\d+)\s+failed)?(?:,\s*)?(?:(\d+)\s+skipped)?(?:,\s*)?(\d+)\s+total"
    match = re.search(tests_pattern, output)
    if match:
        if match.group(1):
            pass_count = int(match.group(1))
        if match.group(2):
            fail_count = int(match.group(2))
            tests_passed = False
        if match.group(3):
            skip_count = int(match.group(3))

    tests_run = pass_count + fail_count + skip_count > 0

    return TestResults(
        tests_run=tests_run,
        tests_passed=tests_passed,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        output=output,
    )


def parse_go_test_output(output: str) -> TestResults:
    """
    Parse go test output for test results.

    Args:
        output: Combined stdout/stderr from go test

    Returns:
        TestResults with parsed counts
    """
    pass_count = 0
    fail_count = 0
    skip_count = 0

    # Go test shows "--- PASS:" and "--- FAIL:" for each test
    pass_count = len(re.findall(r"--- PASS:", output))
    fail_count = len(re.findall(r"--- FAIL:", output))
    skip_count = len(re.findall(r"--- SKIP:", output))

    # Also check for "ok" and "FAIL" package summaries
    if "FAIL" in output:
        tests_passed = False
    else:
        tests_passed = fail_count == 0

    tests_run = pass_count + fail_count + skip_count > 0

    return TestResults(
        tests_run=tests_run,
        tests_passed=tests_passed,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        output=output,
    )


def parse_cargo_test_output(output: str) -> TestResults:
    """
    Parse cargo test output for test results.

    Args:
        output: Combined stdout/stderr from cargo test

    Returns:
        TestResults with parsed counts
    """
    pass_count = 0
    fail_count = 0
    skip_count = 0

    # Cargo test: "test result: ok. 5 passed; 0 failed; 2 ignored"
    pattern = r"test result:.*?(\d+)\s+passed;\s*(\d+)\s+failed;\s*(\d+)\s+ignored"
    match = re.search(pattern, output)
    if match:
        pass_count = int(match.group(1))
        fail_count = int(match.group(2))
        skip_count = int(match.group(3))

    tests_passed = fail_count == 0
    tests_run = pass_count + fail_count + skip_count > 0

    return TestResults(
        tests_run=tests_run,
        tests_passed=tests_passed,
        pass_count=pass_count,
        fail_count=fail_count,
        skip_count=skip_count,
        output=output,
    )


def parse_test_output(output: str, framework: str | None = None) -> TestResults:
    """
    Parse test output based on framework.

    Args:
        output: Test command output
        framework: Framework name (auto-detects from output if not provided)

    Returns:
        TestResults with parsed counts
    """
    # Try to auto-detect framework from output if not specified
    if framework is None:
        if "pytest" in output.lower() or "passed" in output and "failed" in output:
            framework = "pytest"
        elif "Tests:" in output and "total" in output:
            framework = "jest"
        elif "--- PASS:" in output or "--- FAIL:" in output:
            framework = "go-test"
        elif "test result:" in output:
            framework = "cargo-test"

    if framework == "pytest":
        return parse_pytest_output(output)
    elif framework == "jest" or framework == "vitest":
        return parse_jest_output(output)
    elif framework == "go-test":
        return parse_go_test_output(output)
    elif framework == "cargo-test":
        return parse_cargo_test_output(output)

    # Generic fallback - just report that tests ran
    has_pass = "pass" in output.lower()
    has_fail = "fail" in output.lower()

    return TestResults(
        tests_run=has_pass or has_fail,
        tests_passed=not has_fail,
        pass_count=1 if has_pass else 0,
        fail_count=1 if has_fail else 0,
        skip_count=0,
        output=output,
    )


def should_run_tests_command(step_config: dict) -> str | None:
    """
    Determine the test command to run based on step config.

    Args:
        step_config: Step configuration dict

    Returns:
        Test command string or None if tests shouldn't run
    """
    # Check if step explicitly disables tests
    if step_config.get("skip_tests"):
        return None

    # Check for explicit test command
    test_cmd = step_config.get("test_command")
    if test_cmd:
        return test_cmd

    # Check if this is a test step type
    step_type = step_config.get("type", "")
    if step_type == "test":
        return step_config.get("command", "pytest -v")

    return None
