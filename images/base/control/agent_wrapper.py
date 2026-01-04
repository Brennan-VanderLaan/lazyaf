#!/usr/bin/env python3
"""
LazyAF Agent Wrapper - Handles agent step execution lifecycle.

This script is invoked by the control layer for agent steps. It:
1. Reads agent config from step_config.json
2. Clones repository and creates feature branch
3. Builds prompt from title, description, previous logs
4. Invokes agent CLI (Claude or Gemini)
5. Commits any uncommitted changes
6. Pushes branch to origin

The control layer handles heartbeat and status reporting - this just runs the agent.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional


# Default config location (same as control layer)
DEFAULT_CONFIG_PATH = Path("/workspace/.control/step_config.json")

# Context directory for pipeline state
CONTEXT_DIR = ".lazyaf-context"


def log(msg: str):
    """Log a message to stdout (captured by control layer)."""
    print(f"[agent] {msg}", flush=True)


def run_command(cmd: list, cwd: str = None) -> tuple:
    """Run a command and return exit_code, stdout, stderr."""
    log(f"$ {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if result.stdout:
        for line in result.stdout.strip().split("\n")[:20]:  # Limit output
            log(f"  {line}")
    if result.stderr:
        for line in result.stderr.strip().split("\n")[:10]:
            log(f"  [stderr] {line}")
    return result.returncode, result.stdout, result.stderr


def run_command_streaming(cmd: list, cwd: str = None) -> tuple:
    """Run a command with real-time output streaming."""
    import threading

    log(f"$ {' '.join(cmd)}")

    stdout_lines = []
    stderr_lines = []

    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    def read_stdout():
        for line in process.stdout:
            line = line.rstrip('\n')
            stdout_lines.append(line)
            print(f"  {line}", flush=True)

    def read_stderr():
        for line in process.stderr:
            line = line.rstrip('\n')
            stderr_lines.append(line)
            print(f"  [stderr] {line}", flush=True)

    stdout_thread = threading.Thread(target=read_stdout, daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, daemon=True)

    stdout_thread.start()
    stderr_thread.start()

    process.wait()

    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)

    return process.returncode, '\n'.join(stdout_lines), '\n'.join(stderr_lines)


def load_agent_config(config_path: Path) -> Optional[dict]:
    """Load agent configuration from step_config.json."""
    try:
        if not config_path.exists():
            log(f"Config file not found: {config_path}")
            return None

        with open(config_path) as f:
            config = json.load(f)

        # Validate required fields for agent steps
        required = ["step_id", "backend_url"]
        for field in required:
            if field not in config:
                log(f"Missing required field: {field}")
                return None

        return config
    except Exception as e:
        log(f"Failed to load config: {e}")
        return None


def normalize_agent_name(name: str) -> str:
    """Normalize agent name to CLI-safe format (lowercase, hyphenated)."""
    normalized = name.lower().strip()
    normalized = re.sub(r'[^a-z0-9-]', '-', normalized)
    normalized = re.sub(r'-+', '-', normalized)
    normalized = re.sub(r'^-|-$', '', normalized)
    return normalized


def build_agents_json(agent_files: list) -> Optional[str]:
    """Build JSON string for --agents CLI flag from agent files."""
    if not agent_files:
        return None

    agents_dict = {}
    for agent_file in agent_files:
        name = agent_file.get("name", "")
        content = agent_file.get("content", "")
        description = agent_file.get("description", "")

        if not name or not content:
            log(f"Skipping invalid agent file: {name}")
            continue

        cli_name = normalize_agent_name(name)
        if not cli_name:
            log(f"Skipping agent with invalid name: {name}")
            continue

        agents_dict[cli_name] = {
            "description": description or f"Agent: {cli_name}",
            "prompt": content,
        }
        log(f"  Configured agent: @{cli_name}")

    if not agents_dict:
        return None

    return json.dumps(agents_dict)


def fetch_agent_files(backend_url: str, agent_file_ids: list) -> list:
    """Fetch agent files from backend by their IDs."""
    if not agent_file_ids:
        return []

    try:
        import requests
        response = requests.post(
            f"{backend_url}/api/agent-files/batch",
            json=agent_file_ids,
            timeout=10,
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        log(f"Failed to fetch agent files: {e}")
        return []


def setup_git_config():
    """Configure git user for commits."""
    run_command(["git", "config", "--global", "user.email", "lazyaf@localhost"])
    run_command(["git", "config", "--global", "user.name", "LazyAF Agent"])
    run_command(["git", "config", "--global", "init.defaultBranch", "main"])


def clone_repository(repo_url: str, workspace: Path, is_continuation: bool) -> bool:
    """Clone repository or use existing workspace for continuation."""
    if is_continuation and workspace.exists():
        log("Continuing from previous step - using existing workspace")
        return True

    if not repo_url:
        if workspace.exists():
            log("Using existing repository")
            return True
        log("ERROR: No repository URL and workspace doesn't exist")
        return False

    log(f"Cloning {repo_url}...")

    if workspace.exists():
        run_command(["rm", "-rf", str(workspace)])

    workspace.parent.mkdir(parents=True, exist_ok=True)

    exit_code, _, _ = run_command(["git", "clone", repo_url, str(workspace)])
    if exit_code != 0:
        log("ERROR: Failed to clone repository")
        return False

    return True


def setup_branch(workspace: Path, branch_name: str, base_branch: str) -> Optional[str]:
    """Create or checkout feature branch. Returns base commit SHA."""
    log(f"Creating branch {branch_name} from {base_branch}...")

    # Fetch all refs
    run_command(["git", "fetch", "--all"], cwd=str(workspace))

    # Try to checkout base branch
    exit_code, _, _ = run_command(["git", "checkout", base_branch], cwd=str(workspace))
    if exit_code != 0:
        # Try origin/<base_branch>
        log(f"Could not checkout {base_branch}, trying origin/{base_branch}...")
        exit_code, _, _ = run_command(
            ["git", "checkout", "-b", base_branch, f"origin/{base_branch}"],
            cwd=str(workspace)
        )
        if exit_code != 0:
            # Use current branch
            exit_code, stdout, _ = run_command(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(workspace)
            )
            if exit_code == 0:
                base_branch = stdout.strip()
                log(f"Using current branch: {base_branch}")

    # Store base commit
    exit_code, stdout, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(workspace))
    base_commit = stdout.strip() if exit_code == 0 else None
    log(f"Base commit: {base_commit[:8] if base_commit else 'unknown'}")

    # Check if branch exists on remote
    exit_code, ls_output, _ = run_command(
        ["git", "ls-remote", "--heads", "origin", branch_name],
        cwd=str(workspace)
    )
    branch_exists_on_remote = exit_code == 0 and branch_name in ls_output

    if branch_exists_on_remote:
        log(f"Branch {branch_name} exists on remote, fetching...")
        run_command(["git", "fetch", "origin", branch_name], cwd=str(workspace))
        run_command(["git", "checkout", "-b", branch_name, f"origin/{branch_name}"], cwd=str(workspace))

        # Merge latest from base
        log(f"Merging latest from {base_branch}...")
        exit_code, _, stderr = run_command(
            ["git", "merge", base_branch, "-m", f"Merge {base_branch} into {branch_name}"],
            cwd=str(workspace)
        )
        if exit_code != 0:
            log(f"Merge failed, resetting to base: {stderr}")
            run_command(["git", "checkout", base_branch], cwd=str(workspace))
            run_command(["git", "branch", "-D", branch_name], cwd=str(workspace))
            run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))
    else:
        run_command(["git", "checkout", "-b", branch_name], cwd=str(workspace))

    return base_commit


def build_prompt(config: dict, workspace: Path) -> str:
    """Build agent prompt from config."""
    title = config.get("title", "")
    description = config.get("description", "")
    prompt_template = config.get("prompt_template")
    previous_step_logs = config.get("previous_step_logs")

    if prompt_template:
        # Replace placeholders in template
        prompt = prompt_template.replace("{{title}}", title).replace("{{description}}", description)
        log("Using custom prompt template")
    else:
        # Default prompt
        prompt = f"""# Task: {title}

{description}

## Guidelines
- Make the minimal changes needed to accomplish the task
- Commit your changes with a clear message
- If you encounter issues, document them in code comments
"""
        log("Using default prompt template")

    # Append previous step logs if available
    if previous_step_logs:
        prompt += f"""

## Previous Step Output
The previous pipeline step produced the following output:
```
{previous_step_logs}
```

Use this context when completing the current task.
"""
        log("Added previous step logs to prompt")

    return prompt


def build_claude_command(prompt: str, config: dict, agents_json: Optional[str]) -> list:
    """Build Claude Code CLI command."""
    cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]

    # Add model if specified
    model = config.get("model")
    if model:
        cmd.extend(["--model", model])
        log(f"Using model: {model}")

    # Add agents JSON if available
    if agents_json:
        cmd.extend(["--agents", agents_json])
        log(f"Added --agents flag")

    return cmd


def build_gemini_command(prompt: str, config: dict) -> list:
    """Build Gemini agent command."""
    # Gemini uses aider or a Python script
    return ["python", "-m", "gemini_agent", prompt]


def commit_and_push(workspace: Path, branch_name: str, title: str, base_commit: Optional[str]) -> bool:
    """Commit any uncommitted changes and push branch."""
    # Check for uncommitted changes
    exit_code, stdout, _ = run_command(["git", "status", "--porcelain"], cwd=str(workspace))
    has_uncommitted = bool(stdout.strip())

    if has_uncommitted:
        log("Committing uncommitted changes...")
        run_command(["git", "add", "-A"], cwd=str(workspace))
        run_command(
            ["git", "commit", "-m", f"feat: {title}\n\nImplemented by LazyAF agent"],
            cwd=str(workspace),
        )

    # Check if we have any new commits
    exit_code, stdout, _ = run_command(["git", "rev-parse", "HEAD"], cwd=str(workspace))
    current_commit = stdout.strip() if exit_code == 0 else None

    if base_commit and current_commit and base_commit == current_commit:
        log("No changes made by agent")
        return True

    # Count commits ahead of base
    if base_commit:
        exit_code, stdout, _ = run_command(
            ["git", "rev-list", "--count", f"{base_commit}..HEAD"],
            cwd=str(workspace)
        )
        commit_count = stdout.strip() if exit_code == 0 else "?"
        log(f"Branch has {commit_count} new commit(s)")

    # Push branch
    log(f"Pushing branch {branch_name}...")
    exit_code, _, _ = run_command(
        ["git", "push", "-u", "origin", branch_name],
        cwd=str(workspace),
    )
    if exit_code != 0:
        log("ERROR: Failed to push branch")
        return False

    return True


def init_context_directory(workspace: Path, pipeline_run_id: str) -> Path:
    """Initialize context directory for a pipeline run."""
    from datetime import datetime

    context_path = workspace / CONTEXT_DIR
    context_path.mkdir(exist_ok=True)

    metadata = {
        "pipeline_run_id": pipeline_run_id,
        "steps_completed": [],
        "created_at": datetime.utcnow().isoformat(),
    }
    metadata_file = context_path / "metadata.json"
    if not metadata_file.exists():
        metadata_file.write_text(json.dumps(metadata, indent=2))
        log(f"Initialized context directory: {context_path}")

    return context_path


def main() -> int:
    """Main agent wrapper entry point."""
    # Load config
    config_path_str = os.environ.get("CONFIG_PATH")
    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH

    log(f"Loading agent config from {config_path}")
    config = load_agent_config(config_path)
    if config is None:
        return 1

    # Extract agent-specific config
    agent_type = config.get("agent_type", "claude-code")
    repo_url = config.get("repo_url")
    branch_name = config.get("branch_name", "lazyaf/agent-work")
    base_branch = config.get("base_branch", "main")
    is_continuation = config.get("is_continuation", False)
    agent_file_ids = config.get("agent_file_ids", [])
    backend_url = config.get("backend_url", "http://localhost:8000")
    pipeline_run_id = config.get("pipeline_run_id")

    # Determine workspace path
    if pipeline_run_id:
        workspace = Path(f"/workspace/{pipeline_run_id[:8]}/repo")
    else:
        workspace = Path("/workspace/repo")

    log("=" * 50)
    log("AGENT EXECUTION")
    log(f"  Agent type: {agent_type}")
    log(f"  Branch: {branch_name}")
    log(f"  Base: {base_branch}")
    log(f"  Continuation: {is_continuation}")
    log("=" * 50)

    try:
        # 1. Configure git
        setup_git_config()

        # 2. Clone repository
        if not clone_repository(repo_url, workspace, is_continuation):
            return 1

        # 3. Initialize context directory if pipeline
        if pipeline_run_id and not is_continuation:
            init_context_directory(workspace, pipeline_run_id)

        # 4. Setup branch
        base_commit = setup_branch(workspace, branch_name, base_branch)

        # 5. Fetch and build agent files
        agents_json = None
        if agent_file_ids:
            agent_files = fetch_agent_files(backend_url, agent_file_ids)
            agents_json = build_agents_json(agent_files)
            if agents_json:
                log(f"Built agents JSON for {len(agent_files)} agent(s)")

        # 6. Build prompt
        prompt = build_prompt(config, workspace)

        # 7. Build and execute agent command
        if agent_type in ("claude-code", "claude", "any"):
            cmd = build_claude_command(prompt, config, agents_json)
        elif agent_type == "gemini":
            cmd = build_gemini_command(prompt, config)
        else:
            log(f"Unknown agent type: {agent_type}, defaulting to Claude")
            cmd = build_claude_command(prompt, config, agents_json)

        log("Invoking agent (streaming output)...")
        exit_code, stdout, stderr = run_command_streaming(cmd, cwd=str(workspace))

        if exit_code != 0:
            log(f"Agent failed with exit code {exit_code}")
            return exit_code

        # 8. Commit and push changes
        title = config.get("title", "Agent changes")
        if not commit_and_push(workspace, branch_name, title, base_commit):
            return 1

        log("Agent execution completed successfully")
        return 0

    except Exception as e:
        log(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
