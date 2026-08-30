# Wave 4 - Phase 12.5 Wiring Design: agent steps on the control layer, plus the usage channel

Status: DESIGN - implementers build from this verbatim.
Inputs: `PLAN.md` Phase 12.5 + R1-R8, `docs/milestone-13/api-surface.md` section 2
(BINDING - the usage channel ships in this phase or it becomes a retrofit),
`upcoming/wave2-123-wiring.md` (12.3 - this design extends exactly that machinery
and reuses its vocabulary), `backend/app/services/execution/local_executor.py`,
`images/base/control/*`, `backend/app/services/control_layer/workspace.py`,
`backend/app/routers/steps.py`, `runner-common/runner_common/**`,
`backend/app/services/playground_service.py`, `backend/app/services/pipeline_executor.py`,
`backend/app/services/workspace/execution_router.py`.

---

## 0. Ground truth found during recon (read before arguing with the design)

- Control mode is live and load-bearing. `_prepare_control_mode` (pipeline_executor)
  decides mode at dispatch, creates the `StepExecution` row + step JWT, and stamps
  `exec_context["control_mode"]`. LocalExecutor `create -> put_archive -> start`,
  config at `/workspace/.control/<step_execution_id>.json` announced via `CONFIG_PATH`,
  consumed-once by `run.py`. The token is in the FILE, never in `docker inspect` env.
- `images/base/control/executor.py` does `env = os.environ.copy(); env.update(config.environment)`
  before `Popen`. **That is the secret channel**: anything in the config file's
  `environment` reaches the step process without ever entering the container's
  inspectable env. No new mechanism is needed for API keys.
- `run.py` already owns a per-step sidecar-manifest pattern: it injects
  `LAZYAF_TEST_RESULTS_PATH` into `config.environment`, executes, then ships and
  deletes the manifest on EVERY outcome, and never lets manifest handling change the
  step's exit code. **The usage channel is a second instance of this exact pattern.**
- `run.py` deletes `step_config.json` in a `finally` BEFORE the command runs. So the
  step process can never read the step JWT - which is why the agent payload must be a
  SIBLING file, not extra keys on the step config (section 2).
- `runner_common.executors` (`ClaudeExecutor` / `GeminiExecutor` / `MockExecutor` over
  `AgentExecutor` + `ExecutorConfig` + `ExecutorResult`) is the tested agent layer.
  `runner_common.entrypoint.execute_agent_step` is the monolith around it (poll, clone,
  prompt, commit, push, run tests, complete_job) - only the executors are salvage.
- `runner-{claude,gemini,mock}/entrypoint.py` are byte-forked copies of the same
  monolith; those images do NOT install runner-common (stated in
  `reject_non_agent_step`'s docstring). They are legacy-only after this phase.
- `ExecutionRouter` sends `agent` -> legacy with reason `agent-steps-legacy-until-12.5`.
  That string is the single line this phase flips.
- `StepExecution.step_run_id` is a hard FK to `StepRun` -> `PipelineRun`. **The control
  endpoints structurally require a PipelineRun.** Playground and cards therefore cannot
  "just call the executor"; they need a run (section 4).
- Workspace: one named volume per `PipelineRun`, cloned at `branch`/`commit_sha` from
  `http://backend:8000/git/{repo_id}.git` by the helper container. The internal git
  server accepts unauthenticated `git-receive-pack`, so a step container can push.
- `settings.anthropic_api_key` / `gemini_api_key` already exist and compose already
  passes `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` into the **backend**. No compose change
  is needed to get keys to agent steps.
- `scripts/build_images.py` builds `base -> claude -> test-runner` from
  `images/<dir>` contexts with a single-level parent-hash chain, tolerating CRLF and
  Windows path ordering (dogfood runs #8/#9 paid for both).
- `tdd/unit/control_runtime/conftest.py` ALREADY puts both `images/base` and
  `runner-common` on `sys.path`. Producer/consumer contract tests for the agent
  payload and the usage manifest belong there, next to `manifest_contract.py`.

---

## 1. THE AGENT IMAGE STORY

### 1.1 The image tree

| Image | From | Adds | Purpose |
|---|---|---|---|
| `lazyaf-base:dev` | `python:3.12-slim` | control runtime, entrypoint, HOME env, `lazyaf.control-layer=1` | unchanged |
| **`lazyaf-agent-base:dev`** (new) | `lazyaf-base:dev` | runner-common installed system-wide, `LABEL lazyaf.agent-runtime=1` | agent runtime; **also the mock agent image** |
| `lazyaf-claude:dev` | **`lazyaf-agent-base:dev`** (re-parented) | Node 20 + `@anthropic-ai/claude-code` | claude-code steps |
| **`lazyaf-gemini:dev`** (new) | `lazyaf-agent-base:dev` | Node 20 + `@google/gemini-cli` | gemini steps |
| `lazyaf-test-runner:dev` | `lazyaf-base:dev` | unchanged | tier steps |

`agent: mock` resolves to `lazyaf-agent-base:dev`. A fourth image whose only content is
"agent-base minus nothing" is a rebuild cost with no payload; the mock executor needs
python + runner-common and that is precisely agent-base.

### 1.2 How runner-common gets into the image: STAGED CONTEXT (decided)

Not vendored (a second copy of the executors in git is the R3 violation this phase
exists to remove), not a bind (images must be self-contained for 12.6 remote nodes),
not a pre-built wheel checked into `images/` (wheels are not byte-reproducible, so the
content-hash label would churn on every build and `--check` would always say stale).

`scripts/build_images.py` assembles a **temporary build context**:

```python
IMAGES = [
    # (subdir, image name, parent subdir | None, extra_context)
    ("base",        "lazyaf-base",        None,         []),
    ("agent-base",  "lazyaf-agent-base",  "base",       [(REPO_ROOT / "runner-common", "runner-common")]),
    ("claude",      "lazyaf-claude",      "agent-base", []),
    ("gemini",      "lazyaf-gemini",      "agent-base", []),
    ("test-runner", "lazyaf-test-runner", "base",       []),
]

STAGE_EXCLUDE = ("__pycache__", ".egg-info", "dist", ".venv", "uv.lock", "tests")

def stage_context(image_dir: Path, extras: list[tuple[Path, str]]) -> Path:
    """Copy image_dir/* plus each extra source into a fresh temp dir.

    Excludes STAGE_EXCLUDE so build-irrelevant churn (test edits, lockfile
    bumps, caches) can never change the content hash - and so editing
    runner-common/tests does NOT force an agent-image rebuild.
    """
```

- Hash is `tree_hash(staged_dir, extra=parent_hash)` - the SAME function, so the
  POSIX-path sort and CRLF normalization fixes apply to the staged runner-common too.
- `parent_hash` becomes a `dict[subdir, hash]` lookup (the current single-level "chain
  base" special case dies), so `claude` chains `agent-base` which chains `base`.
- The staged dir is removed in a `finally`. `--check` stages and hashes without building.

`images/agent-base/Dockerfile`:

```dockerfile
FROM lazyaf-base:dev
# runner-common is STAGED into this build context by scripts/build_images.py;
# there is no copy of it under images/ in git (R3: one source for the executors).
COPY runner-common/ /opt/runner-common/
# PIP_USER=0 overrides the baked PIP_USER=1 (which targets the runtime volume):
# build-time installs must land in the image, not in /workspace.
RUN PIP_USER=0 pip install --no-cache-dir /opt/runner-common
ARG CONTENT_HASH=dev
LABEL lazyaf.agent-runtime=1
LABEL lazyaf.content-hash=$CONTENT_HASH
```

`lazyaf.control-layer=1` is inherited from base, so `image_supports_control_layer`
needs no change. `lazyaf.agent-runtime=1` is a separate, positive declaration used by
one preflight assertion (section 2.4) - never by mode selection.

### 1.3 How `/control/run.py` invokes the wrapper

**It does not know it is an agent step.** `run.py` executes `config.command` exactly as
it executes a script. For an agent step the BACKEND puts a fixed command in the config:

```
"command": "python3 -m runner_common.agent_wrapper"
```

That is the whole invocation contract. Consequences, all of them good:

- Zero change to `run.py`'s dispatch, timeout watchdog, log pump, or shell wrapping.
- The wrapper is an ordinary child process: the in-container watchdog kills its process
  group on timeout, its stdout is the step's log stream, its exit code is the step's
  exit code.
- The wrapper is `runner_common.agent_wrapper` - a MODULE in the tested package, not a
  file copied into `/control`. One packaging mechanism, tests live in
  `runner-common/tests/`, and 12.6's remote runner-agent invokes the identical module.

### 1.4 API keys without `docker inspect` exposure

New optional step-config key consumed by LocalExecutor: **`secret_environment`**.

| Destination | Contents |
|---|---|
| Docker `environment=` kwarg (inspectable) | `HOME`, `user_env`, `LAZYAF_PIPELINE_RUN_ID`, `LAZYAF_STEP_RUN_ID`, `LAZYAF_STEP_INDEX`, `LAZYAF_EXECUTION_KEY`, `LAZYAF_BACKEND_URL`, `LAZYAF_CONTROL`, `CONFIG_PATH`, `LAZYAF_USAGE_PROVIDER`, `LAZYAF_ROLE`, `LAZYAF_GPU_NODE_ID` - **never a secret** |
| Step config FILE `environment` (0600, consume-once) | `user_env` + **`secret_environment`** + `LAZYAF_AGENT_CONFIG_PATH` |

Rules, enforced in code:

1. `secret_environment` present + `control_mode` false => the step FAILS at dispatch
   with `"secrets require control mode"`. A secret must never be able to silently
   downgrade onto the stdout path where it would land in container env.
2. LocalExecutor never merges `secret_environment` into `run_kwargs["environment"]`.
   A T2 test inspects the created container and asserts the secret VALUE appears in no
   env entry, label, or command, while the same value is present in the put_archive tar.
3. `pipeline_executor` builds it per agent type from settings:

| agent | secret_environment | missing-key behavior |
|---|---|---|
| `claude-code` | `{"ANTHROPIC_API_KEY": settings.anthropic_api_key}` | fail the step at dispatch: `"agent step 'X' needs ANTHROPIC_API_KEY (set it in the backend environment)"` |
| `gemini` | `{"GEMINI_API_KEY": settings.gemini_api_key}` | same, naming GEMINI_API_KEY |
| `mock` | `{}` | n/a |

Failing at dispatch beats burning 30 seconds of container startup to reach an opaque
CLI auth error, and it keeps the key name out of the step logs.

---

## 2. THE AGENT STEP CONFIG CONTRACT

### 2.1 Two files, not more keys (decided)

`run.py` deletes `step_config.json` before the command starts (consume-once, 12.3). An
agent payload carried inside it would therefore be unreadable by the wrapper. Splitting
also keeps the step JWT and the API key out of any file the wrapper opens.

| File | Producer | Consumer | Deleted by |
|---|---|---|---|
| `/workspace/.control/<step_execution_id>.json` | `control_layer.workspace.generate_step_config` (unchanged shape) | `run.py` | `run.py`, before exec |
| `/workspace/.control/agent.<step_execution_id>.json` | `control_layer.workspace.generate_agent_config` (NEW) | `runner_common.agent_wrapper` | wrapper on load; `run.py` unlinks at shutdown as backstop |
| `/workspace/.control/usage.<step_execution_id>.json` | wrapper | `run.py` (ships + deletes) | `run.py`, every outcome |

Both config files travel in the SAME put_archive tar onto the created-but-not-started
container. Per-step filenames by construction (the 12.3 collision lesson).

### 2.2 The step config for an agent step (existing producer, no shape change)

```json
{
  "step_id":           "0f1c9d5e-...",
  "step_run_id":       "8a44c1b2-...",
  "execution_key":     "<run_id>:3:<step_run_id>",
  "command":           "python3 -m runner_common.agent_wrapper",
  "backend_url":       "http://backend:8000",
  "auth_token":        "<step JWT>",
  "environment": {
    "LAZYAF_AGENT_CONFIG_PATH": "/workspace/.control/agent.0f1c9d5e-....json",
    "ANTHROPIC_API_KEY":        "sk-ant-..."
  },
  "timeout_seconds":   1800,
  "working_directory": "/workspace/repo",
  "shell":             "bash"
}
```

`LAZYAF_USAGE_PATH` is NOT here: like `LAZYAF_TEST_RESULTS_PATH` it is platform-owned
and injected by `run.py` into `config.environment` at exec time (section 3.2), so a
step can never point the platform at another step's manifest.

### 2.3 The agent config (NEW producer `generate_agent_config`)

```json
{
  "version": 1,
  "agent":   "claude-code",
  "model":   "claude-haiku-4-5",
  "stream":  true,
  "prompt":  "You are implementing a feature for this project.\n\n## Feature Request\n...",
  "agents_json": "{\"test-fixer\": {\"description\": \"...\", \"prompt\": \"...\"}}",
  "task": {
    "card_id":          "c1d2...",
    "card_title":       "Add rate limiting to /api/repos",
    "card_description": "...",
    "step_index":       3,
    "step_name":        "implement"
  },
  "context": {
    "previous_step_name": "plan",
    "previous_step_logs": "...",
    "previous_step_logs_truncated": false
  },
  "repo": {
    "repo_id":     "r9f8...",
    "workdir":     "/workspace/repo",
    "base_branch": "main",
    "branch":      "lazyaf/9f2a11c4",
    "remote_url":  "http://backend:8000/git/r9f8....git"
  },
  "commit": {
    "enabled":      true,
    "message":      "feat: Add rate limiting to /api/repos\n\nImplemented by LazyAF agent",
    "author_name":  "LazyAF Agent",
    "author_email": "agent@lazyaf.local",
    "push":         true,
    "allow_empty":  false
  },
  "mock_config": null,
  "role": null
}
```

| Field | Why an agent step needs it and a script step does not |
|---|---|
| `agent` | selects the executor from `runner_common.executors`; also selects the default image backend-side |
| `model` | `ExecutorConfig.model` -> `--model`; the M13 comparison axis |
| `prompt` | the work itself. **Rendered backend-side** (section 2.5) |
| `agents_json` | resolved `agent_file_ids` -> `claude --agents`; the backend owns AgentFile/agent_resolver, the container has no DB |
| `stream` | claude `--output-format stream-json --verbose` vs `json` (section 3.3) |
| `task` | commit message, forensics, and the card join |
| `context.previous_step_logs` | the legacy `.lazyaf-context/` channel, replaced by a DB-sourced field (section 2.6). Capped at **32 KiB**, head-truncated with an explicit marker and `previous_step_logs_truncated: true` |
| `repo` | the agent must commit and push; scripts do not |
| `commit` | whether/how to land the work; the US-2 branch |
| `mock_config` | deterministic mock behavior, so the dogfood ratchet costs nothing |
| `role` | M13 fan-out attribution. Written as `null` in 12.5; on the wire NOW so M13 is not a retrofit |

Consumer side (`runner_common/agent_config.py`) mirrors 12.3's `config.py`: `version`
pinned to `1`, unknown version is a loud stderr failure and exit 1 (a wrapper that
half-understands its instructions is worse than one that refuses), missing required key
prints the reason, never a silent `None`.

### 2.4 How the runtime dispatches to the right executor

```python
# runner_common/agent_wrapper.py  (sketch - the real file is ~150 lines)
EXECUTORS = {"claude-code": ClaudeExecutor, "gemini": GeminiExecutor, "mock": MockExecutor}

def main() -> int:
    cfg_path  = Path(os.environ["LAZYAF_AGENT_CONFIG_PATH"])
    usage_path = os.environ.get("LAZYAF_USAGE_PATH")
    try:
        cfg = load_agent_config(cfg_path)          # consume-once
    finally:
        _unlink_quietly(cfg_path)
    if cfg is None:
        return 1

    if os.name == "posix" and os.getuid() == 0:
        # claude --dangerously-skip-permissions refuses to run as root, and a
        # root-owned $HOME poisons the next step on the shared volume.
        print("[agent] ERROR: wrapper is running as root; the image entrypoint "
              "must gosu down to uid 1000", file=sys.stderr)
        return 1

    executor = EXECUTORS[cfg.agent]()
    ec = ExecutorConfig(
        workspace=Path(cfg.repo["workdir"]),
        prompt=cfg.prompt,
        model=cfg.model,
        agents_json=cfg.agents_json,
        timeout=None,          # the control runtime's watchdog is the ONE timeout owner
        env={},
    )
    started = time.monotonic()
    result = None
    try:
        result = executor.execute(ec, log_callback=_emit, streaming=cfg.stream)
        return _finish(cfg, result)
    finally:
        write_usage_manifest(usage_path, cfg, result, elapsed_ms(started))  # NEVER raises
```

- `cfg.agent` not in `EXECUTORS` -> stderr + exit 1. There is no default agent.
- `timeout=None` is deliberate: `images/base/control/executor.py` already SIGTERMs then
  SIGKILLs the process group at `timeout_seconds`. Two timeout owners is how a step ends
  up half-killed with no manifest.
- The wrapper installs a `SIGTERM` handler that writes the partial usage manifest and
  then exits `128+15`, so a graceful watchdog kill still yields telemetry. A SIGKILL is
  covered by `run.py`'s fallback record (section 3.4).
- Commit/push (`_finish`) happens AFTER the usage manifest is queued for write, and uses
  `runner_common.git_helpers` (`configure_git`, `push`) unchanged. A push failure fails
  the step; it does not lose the usage row.

### 2.5 Prompt rendering moves backend-side

`backend/app/services/agent_prompt.py` (new) renders the prompt: same placeholder
vocabulary as `runner_common.entrypoint.build_prompt` (`{{title}}`, `{{description}}`),
same default template, same `## Previous Step Output` section. The README-scraping
branch is DROPPED on the control path - the agent can read the repo itself, and 12.6.6
replaces that slot with curated spec context.

Rationale: the backend already owns `PromptTemplate`, card fields and (at 12.6.6) the
spec bundle; a container that re-templates is a second source of truth for the most
important string in the system. `runner_common.entrypoint.build_prompt` stays untouched
for the legacy path and dies in the 12.6 deletion commit (section 5.2). A unit test
pins the placeholder set on the backend side so the two cannot drift on the part that
matters.

### 2.6 Dispatch changes (`pipeline_executor` + `ExecutionRouter` + `LocalExecutor`)

1. `ExecutionRouter.decide`: `agent` -> `RoutingDecision("local", "agent-default-local")`.
   `executor: legacy` on an agent step stays legal (WARNING) - it is the last remaining
   legacy escape hatch and R2 requires it to stay callable.
2. `_build_local_execution_config`, when `step_type == "agent"`:
   - `command = "python3 -m runner_common.agent_wrapper"` (users never write it),
   - `image = step_config.get("image") or DEFAULT_AGENT_IMAGE[agent]`,
   - `secret_environment = _agent_secret_env(agent)`,
   - `agent_payload = <the dict in 2.3>` stashed on `exec_step_config["agent"]`,
   - `timeout` default for agent steps raised to **1800s** (script default 300 is a
     rounding error for an agent).
3. `_prepare_control_mode`: `control: false` on an agent step RAISES. An agent step in
   stdout mode would run the wrapper with no config file - fail loudly at dispatch.
4. `LocalExecutor`, control mode: when `step_config["agent"]` is present, produce the
   second file via `generate_agent_config`, add it to the same tar, and set
   `environment["LAZYAF_AGENT_CONFIG_PATH"]` **in the config file only**.

| Agent | Default image |
|---|---|
| `claude-code` | `lazyaf-claude:dev` |
| `gemini` | `lazyaf-gemini:dev` |
| `mock` | `lazyaf-agent-base:dev` |

Preflight (run start) additionally asserts the resolved agent image carries
`lazyaf.agent-runtime=1`; a user pointing an agent step at `lazyaf-test-runner:dev`
gets one clear message instead of `ModuleNotFoundError: runner_common`.

---

## 3. THE USAGE CHANNEL (protocol channel #4)

Implements `docs/milestone-13/api-surface.md` section 2 in full. Non-negotiable rule
threaded through every decision below: **telemetry never fails a step.**

### 3.1 Ownership split (R3: one writer per datum)

| Datum | Owner |
|---|---|
| `provider`, `model`, `model_version`, all token counts, `cost_usd`, `cost_source`, `determinism`, `raw` | the **wrapper** (from the CLI's own report) |
| `wall_clock_ms`, `container_seconds` | **`run.py`** - it is the only component present for script steps too, so timing has exactly one owner |
| `role`, `gpu_node_id`, `gpu_fraction` | **`run.py`**, from `LAZYAF_ROLE` / `LAZYAF_GPU_NODE_ID` / `LAZYAF_GPU_FRACTION` container env (non-secret; set by the executor, empty in 12.5) |
| `step_run_id`, `pipeline_run_id`, `cost_usd` when `cost_source == "gpu-node"`, `role` fallback | the **server** |

`run.py` overwrites the wrapper's timing fields if present. `container_seconds` is
measured from `run.py` process start to the usage POST - documented as a LOWER BOUND
(it excludes image pull and the entrypoint chown); 12.6 may let the executor supply the
true container lifetime when a GPU node actually bills for it.

### 3.2 Runtime mechanics (mirrors 12.2.6 exactly)

```python
# images/base/control/run.py
def usage_path(config_path: Path, step_id: str) -> Path:
    return config_path.parent / f"usage.{step_id}.json"
...
config.environment["LAZYAF_USAGE_PATH"] = str(usage_path(config_path, config.step_id))
...
# step 8.6, right after ship_test_results, BEFORE the terminal /status POST
usage_warning = ship_usage(usage_path_, client, config, result, container_seconds)
```

`ship_usage` obeys the same hard rules as `ship_test_results`: never raises, deletes the
file on every path, warnings are appended to the terminal status `error` (loud, not
silent), and the step's exit code is untouched.

`BackendClient.send_usage(manifest)` uses the **tight** budget (`LOG_MAX_RETRIES=3`,
`LOG_TOTAL_TIMEOUT=15.0`) - shutdown-time delivery must never wedge a step. A 409 is a
non-retryable drop with a WARN.

### 3.3 Where each CLI's numbers come from

`ExecutorResult` gains one optional field: `usage: dict | None = None`. Each executor
fills it; the wrapper stays dumb. Scrapers live in `runner_common/usage.py`.

| Agent | Invocation | Scrape | If nothing |
|---|---|---|---|
| `claude-code` | `claude -p <prompt> --dangerously-skip-permissions --output-format stream-json --verbose [--model M] [--agents J]` | last stdout line parsing as a JSON object with `type == "result"` (or carrying `total_cost_usd`): `total_cost_usd`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`, `usage.cache_creation_input_tokens`, `model`. `cost_source="cli-reported"` | `cost_source="unknown"`, tokens null |
| `gemini` | `gemini -p <prompt> --yolo` (unchanged) | tolerant regex over stdout+stderr for input/output/total token counts in the CLI's usage summary. Dollars are not reported -> `cost_usd=null`, `cost_source="unknown"` even when tokens are found | `cost_source="unknown"`, tokens null |
| `mock` | no CLI | `MockExecutor` emits usage directly: `provider="self-hosted"`, `model="mock"`, `input_tokens=len(prompt)//4`, `output_tokens=sum(len(e.text)//4)`, `cost_usd="0.000000"`, `cost_source="cli-reported"`, `raw={"mock": true}` | n/a - deterministic by construction |
| script / docker | no wrapper | no manifest; `run.py` posts the fallback record | `provider` from `LAZYAF_USAGE_PROVIDER` (default `self-hosted`), `cost_source="unknown"` |

**Deviation from api-surface 2.3, stated deliberately:** the binding doc says claude is
invoked with `--output-format json`. `stream-json --verbose` emits the SAME final result
object (same `total_cost_usd`, same `usage` block) as newline-delimited events, so the
contract's substance is met, while `json` would make a 20-minute agent step completely
dark in the UI - unacceptable under R1. The wrapper's log callback renders each event to
one human-readable line and passes non-JSON lines through verbatim; `stream: false` in
the agent config falls back to `--output-format json` and the scraper handles both
shapes. The mock is `cost_source="cli-reported"` on purpose: its cost is genuinely
known to be zero, `provider="self-hosted"` + `model="mock"` + `raw.mock` make it
unambiguous, and a dogfood ratchet that exercises the `unknown` branch would leave the
real branch untested.

The gemini scraper is speculative until a real run is captured. That is safe by
construction: a miss costs one `cost_source="unknown"` row, never a red step. First real
gemini run: capture stdout to `runner-common/tests/fixtures/gemini_usage_*.txt` and
tighten the regex in the same commit.

### 3.4 When the CLI reports nothing

```
manifest missing / unparseable / unknown version
  -> WARN into the step logs (visible), appended to terminal status error
  -> POST {version:1, provider:<LAZYAF_USAGE_PROVIDER|self-hosted>,
           cost_source:"unknown", cost_usd:null, tokens:null,
           wall_clock_ms, container_seconds}
  -> exit code unchanged
POST fails (network / 5xx)  -> 3 tries, <=15s total, then WARN and continue
POST returns 409 (terminal) -> drop, WARN, continue
```

Every control-mode step - including every script step in the dogfood pipeline - produces
a `StepUsage` row from day one. `cost_source="unknown"` is a recorded fact ("the
provider told us nothing"), not a gap; M13's board counts those rows as
`cost_coverage < 1.0` and warns rather than reporting a quietly-too-cheap median.

### 3.5 Endpoint

`POST /api/steps/{step_id}/usage` in `backend/app/routers/steps.py` - same module, same
`verify_step_auth`, same `_reject_terminal_writes`, verbatim from api-surface 2.1:

```python
@router.post("/{step_id}/usage", response_model=UsageIngestResponse)
async def ingest_step_usage(
    step_id: str,
    request: UsageManifest,
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
) -> UsageIngestResponse:
    execution = await verify_step_auth(step_id, authorization, db)
    _reject_terminal_writes(execution)
    usage = await ingest_usage(db, execution, request)
    return UsageIngestResponse(
        usage_id=usage.id,
        cost_usd=str(usage.cost_usd) if usage.cost_usd is not None else None,
        cost_source=usage.cost_source,
    )
```

`backend/app/schemas/usage.py` is the single source of truth for the wire shape
(`UsageManifest` exactly as api-surface 2.2, `version: Literal[1]`, so an unknown
version is a 422 and never a partial parse). The runtime side is pinned by
`tdd/unit/control_runtime/usage_contract.py` - the same shared-contract-module pattern
as `manifest_contract.py`, imported by both sides' tests in one process.

Reads (12.5 ships two; the trials rollup is M13's):

| Method + path | Module | Notes |
|---|---|---|
| `GET /api/steps/{step_id}/usage` | `routers/steps.py` | operator/UI, unauthenticated like the rest of the operator API |
| `GET /api/pipeline-runs/{run_id}/usage` | `routers/pipelines.py` | rollup grouped by role, `by_source` counts, `cost_coverage` |

### 3.6 Model + migration

```python
# backend/app/models/usage.py
class StepUsage(Base):
    __tablename__ = "step_usages"
    __table_args__ = (
        # Idempotency key: a retrying runtime UPDATES, never double-bills.
        Index("ix_step_usages_step_execution_id", "step_execution_id", unique=True),
        # The run rollup is read-heavy and groups by role (api-surface s6).
        Index("ix_step_usages_pipeline_run_id_role", "pipeline_run_id", "role"),
    )
    id:                 Mapped[str]            # uuid4
    step_execution_id:  Mapped[str]            # FK step_executions.id, UNIQUE
    step_run_id:        Mapped[str | None]     # FK step_runs.id
    pipeline_run_id:    Mapped[str | None]     # DENORMALIZED - the rollup must not join
    provider:           Mapped[str]            # String(32)
    model:              Mapped[str | None]     # String(128)
    model_version:      Mapped[str | None]     # String(128)
    input_tokens:       Mapped[int | None]
    output_tokens:      Mapped[int | None]
    cache_read_tokens:  Mapped[int | None]
    cache_write_tokens: Mapped[int | None]
    cost_usd:           Mapped[Decimal | None] # Numeric(18, 6)
    cost_source:        Mapped[str]            # String(16)
    wall_clock_ms:      Mapped[int]
    container_seconds:  Mapped[float | None]
    gpu_node_id:        Mapped[str | None]     # String(64)
    gpu_fraction:       Mapped[float | None]
    role:               Mapped[str | None]     # String(64) - M13 attribution
    determinism:        Mapped[str]            # JSON text, default "{}"
    raw:                Mapped[str | None]     # JSON text, <= 8 KiB
    created_at / updated_at
```

- `Numeric(18, 6)` on SQLite stores as REAL. The service quantizes to 6dp on write; a
  unit test pins that `"0.1841"` round-trips as `Decimal("0.184100")`. Float64 carries
  15-16 significant digits, so summing thousands of sub-dollar rows is exact at 6dp.
- **`trial_iteration_id` is deliberately NOT added in 12.5.** Nothing writes it and
  there is no table to reference; an orphan column buys nothing. `role` IS added,
  because `role` is on the frozen wire NOW - that is exactly the retrofit line
  api-surface 2.6 draws.
- Migration `backend/alembic/versions/0005_step_usage.py`, `down_revision = "0004"`,
  with 0004's `inspector.has_table` / `has_index` guards (a pre-alembic dev DB is healed
  by `create_all` before it is stamped). `tdd/integration/test_migrations.py` pins parity.

### 3.7 Server-side derivation (`backend/app/services/usage_ingestion.py`)

`ingest_usage(db, execution, manifest)`:

1. Load `StepRun` (for `step_run_id`, `pipeline_run_id`) from `execution.step_run_id`.
2. `role` resolution: `manifest.role` -> the step's configured `role` (carried in the
   agent config, `null` in 12.5) -> `None`. The third source named by api-surface 2.6
   (`StepRun.step_config["experiment_context"]["role"]`) arrives WITH M13's column;
   `_resolve_role` has the seam and a comment, not a stub branch.
3. `cost_usd` precedence, server-side: manifest `cost_usd` -> `cli-reported`; else
   `gpu_node_id` with a configured rate -> `usage_pricing.gpu_node_cost_usd(...)` and
   `gpu-node`; else `unknown` with `cost_usd = None`. `estimated` stays in the
   vocabulary and is written by nothing.
4. `raw` capped at 8 KiB: truncate and append `{"_truncated": true}`, never reject.
5. Idempotent upsert on `step_execution_id`.

`backend/app/services/usage_pricing.py` is api-surface 2.5 verbatim, with rates from
`settings.gpu_node_rates` (env `LAZYAF_GPU_NODE_RATES`, JSON, default `{}`). No table,
no token-price table. Nothing in 12.5 sets `LAZYAF_GPU_NODE_ID`, so the gpu-node branch
is exercised by API tests with a hand-built manifest - real code on a real path, not a
`pass # architecture ensures this` (R4).

---

## 4. PLAYGROUND (AND CARDS): AD-HOC AGENT RUNS

**Decision: migrate, per PLAN ("Playground migrates off job_queue HERE - it is agent
execution"). Cards migrate in the same wave, because the 12.5 exit gate demands US-2
(card -> agent -> gate -> review -> merge) green ON EPHEMERAL CONTAINERS, and because
doing it here is what makes 12.6's deletion commit contain only deletions (R2).**

### 4.1 What provides the workspace and the step execution

`StepExecution` -> `StepRun` -> `PipelineRun` is a hard FK chain, and the workspace
service is keyed by `pipeline_run_id`. So playground and cards get a **real, visible
PipelineRun** with exactly one agent step. This is not a workaround: M13's
`TrialIteration` already specifies `pipeline_run_id  # each iteration IS a visible
pipeline run`. Ad-hoc agent work becoming a first-class run is the shape M13 needs.

`backend/app/services/agent_run.py` (new, ~150 lines):

```python
ADHOC_PREFIX = "__lazyaf_adhoc__"

async def start_adhoc_agent_run(
    db, repo, *, trigger_type, trigger_ref, base_branch, work_branch,
    agent, model, prompt_template, task, agent_file_ids, mock_config,
    commit_enabled, push_branch, timeout=1800,
) -> PipelineRun:
    """Create an ephemeral single-agent-step pipeline and start it.

    Reuses pipeline_executor.start_pipeline verbatim: workspace volume,
    StepRun, StepExecution, control mode, logs, test-results, usage and the
    existing WS frames all come for free. trigger_type is 'card_work' or
    'playground'; trigger_ref is the card id / session id.
    """
```

- It writes one ephemeral `Pipeline` row named `f"{ADHOC_PREFIX}:{trigger_type}:{ref[:8]}"`
  with `steps` = a single agent step, `triggers = "[]"`, then calls
  `pipeline_executor.start_pipeline(... trigger_context={"branch": base_branch, ...})`.
- `GET /api/pipelines` filters names starting with `ADHOC_PREFIX`. The RUNS stay
  visible - that is the point.
- Rejected alternative: a `PipelineRun.steps_snapshot` column so ad-hoc runs need no
  Pipeline row. It is the better long-term shape (it also fixes "pipeline edited
  mid-run"), but it changes step loading for EVERY run in the phase that is already
  moving agent execution. Parked as a 12.6/12.7 candidate; the ad-hoc Pipeline rows are
  one row per card start and cascade-delete with their runs.

### 4.2 Completion, durably

`trigger_type` gains two values: `card_work`, `playground`. `_complete_pipeline` ends
with one call:

```python
await agent_run.on_run_complete(db, pipeline_run, success)   # no-ops on other trigger types
```

Durable because it routes on existing persisted columns (`trigger_type` / `trigger_ref`),
not an in-memory registry that a restart loses.

| trigger_type | on success | on failure |
|---|---|---|
| `card_work` | `card.status = "in_review"`, `completed_runner_type = agent`, `job.status = "completed"`, WS card+job frames, then `trigger_service.on_card_status_change(...)` - **that is the "gate" in US-2** | `card.status = "failed"`, `job.status = "failed"`, error from `StepRun.error` |
| `playground` | `playground_service.update_status("completed")` + `set_result(diff=git_repo_manager.get_diff(repo_id, base_branch, work_branch), files_changed=...)`; the throwaway branch is deleted unless `save_branch` was requested | `update_status("failed", error=...)` |

Playground always pushes to `playground/<session_id[:8]>` so the diff is computed
SERVER-SIDE from the internal git server (`git_repo_manager.get_diff`, already used by
`GET /api/cards/{id}/diff`) instead of racing the workspace volume's lifetime.

Playground SSE keeps working because `manager.publish_step_logs` gains a local observer
registry (`register_run_log_observer(run_id, cb)` / `unregister_run_log_observer`) - the
WS manager is already the single place every log frame passes through (R3), so this is
~20 lines and no new coupling in `routers/steps.py`. `playground_service` registers on
start and unregisters on completion/reset.

### 4.3 Cards

`POST /api/cards/{id}/start` and `/retry` stop calling `job_queue.enqueue`. They still
create the `Job` row (`card.job_id` and `lazyaf/{job_id[:8]}` are load-bearing for the
existing UI and the jobs API) and then call `agent_run.start_card_work(...)`. `Job` rows
are written by `agent_run` from the run's state - queued/running/completed/failed. No
frontend change, therefore no new Playwright spec is owed under R8.

---

## 5. WHAT THE RUNNERS STILL DO AFTER 12.5

### 5.1 The answer: nothing, on any default path

| Path | Before 12.5 | After 12.5 |
|---|---|---|
| script / docker steps | local (12.4) | local |
| agent steps in pipelines | legacy queue | **local, control mode** |
| playground | legacy queue | **local, control mode (ad-hoc run)** |
| card start / retry | legacy queue | **local, control mode (ad-hoc run)** |
| `_trigger_card` fix-card action | legacy queue | **local, control mode (ad-hoc run)** |
| explicit `executor: legacy` on an agent step | legacy queue | legacy queue (kept, WARNING) |

The runners keep their compose services and current replica counts. Setting them to 0
would be deletion-by-config and would make 12.6's acceptance untestable. They sit idle;
`job_queue` idleness is ASSERTED rather than assumed:

- `tdd/unit/services/test_no_legacy_enqueue.py` - spy on `job_queue.enqueue`; card
  start, card retry, playground start, and an agent pipeline step each enqueue **zero**
  jobs (R1: a silent fallback to legacy is indistinguishable from success).
- `scripts/verify_executor.py` additionally asserts `GET /api/runners/stats` reports
  `queued_jobs == 0` for the dogfood run.
- One deliberately-kept legacy test dispatches an agent step with `executor: legacy`
  against the mock runner (slow lane) so the R2 escape hatch cannot rot.

### 5.2 Exact preconditions 12.6 needs before the polling stack is deleted

Deletion commit contents (own commit, nothing else in it):
`runner_pool.py`, `job_queue.py`, `QueuedJob`, the job-pull endpoints in
`routers/runners.py` + `routers/jobs.py`, `runner-{claude,gemini,mock}/`
(Dockerfiles + entrypoints), `runner_common/entrypoint.py`,
`runner_common/job_helpers.py`, `runner_common/context_helpers.py`,
`runner_common.entrypoint.build_prompt` (superseded by `agent_prompt.py`),
`ExecutionRouter`'s `executor: legacy` branch and `ExecutorMode.LEGACY`, the runner
services in `docker-compose.yml`, and the `is_playground` / `playground_*` fields on the
job wire.

Preconditions, all of which must be TRUE and asserted before that commit:

1. 137-test 12.6 contract suite green end-to-end at zero skips (R4 ratchet complete).
2. Dogfood suite - including the mock-agent step of section 6 - green through a
   **loopback runner agent**, with `StepRun.executor == "remote"` asserted via the API.
3. `test_no_legacy_enqueue` still green, plus a new `test_no_legacy_code` asserting no
   import of `job_queue` / `runner_pool` survives anywhere (landed in the same commit).
4. US-2 e2e green on the remote path, not just local.
5. Every consumer migrated IN that commit: frontend runner panel rebuilt with its
   Playwright spec (R8), `routers/jobs.py` reads only `Job` rows written by
   `agent_run`, no caller of `is_playground` remains.
6. `runner-common` still installs into the agent images after `entrypoint.py` and
   `job_helpers.py` are removed (the wrapper, executors, usage, git_helpers and
   `pytest_lazyaf` are the surviving surface) - asserted by a rebuild in the same commit.

---

## 6. DOGFOOD RATCHET (R7)

Two additions, both zero-cost, both on every push.

**(a) A real agent step in `.lazyaf/pipelines/test-suite.yaml`**, inserted between
`tier3` and `verify-executor`:

```yaml
  # US-2 continuous coverage (12.5): a REAL agent step through the REAL control
  # runtime, executed by the mock executor - so the agent path (agent config
  # file, wrapper dispatch, usage manifest, POST /api/steps/{id}/usage) is
  # exercised on every push at zero API cost. commit.enabled is FALSE: the
  # dogfood run must never push to its own repo.
  - id: "mock-agent"
    name: "US-2 mock agent step (zero cost)"
    type: agent
    config:
      agent: mock
      commit: false
      task: "Write .lazyaf-dogfood/agent-ran into the workspace"
      mock_config:
        response_mode: streaming
        delay_ms: 10
        file_operations:
          - action: create
            path: .lazyaf-dogfood/agent-ran
            content: "12.5 mock agent step\n"
        output_events:
          - {type: content, text: "Analyzing the workspace..."}
          - {type: complete, text: "Done."}
        exit_code: 0
    on_success: next
    on_failure: stop
    timeout: 300
```

**(b) `scripts/verify_executor.py` grows the 12.5 assertions** (it stays on
`lazyaf-base:dev` with `control: false` - the gate must not depend on the runtime it
verifies):

1. every agent StepRun in this run has `executor == "local"`;
2. that step has a `StepUsage` row (`GET /api/steps/{step_execution_id}/usage`) with
   non-null `input_tokens` and `output_tokens` and a non-null `cost_source`;
3. `GET /api/pipeline-runs/{run_id}/usage` reports a `StepUsage` row for EVERY step of
   the run (script steps included, `cost_source == "unknown"` accepted) - so a silently
   dropped usage channel fails the push;
4. `GET /api/runners/stats` reports `queued_jobs == 0`.

**(c) US-2 e2e joins T3.** `tdd/e2e/test_us2_card_loop.py` (not marked `slow`) is picked
up by `run_tier.py T3`'s `../tdd/e2e -m "not slow"` selection with no config change; T3
gains the same `build_images.py --check` preflight T2 has, because it now spawns
`lazyaf-agent-base:dev` containers through the compose backend's socket. Tier floors in
`tdd/tier_floors.json` move up in the same commit (R4 ratchet).

---

## 7. RISK REGISTER (the 12.3-era environment seams)

| Seam | Inherited by agent images? | What pins it |
|---|---|---|
| **uid ownership** (fresh named volumes are root-owned; put_archive files need uid 1000) | YES, unchanged - the base `entrypoint.sh` chown + `find /workspace/.control -name '*.json' -exec chown` already covers `agent.<id>.json` because it matches `*.json` | Existing entrypoint. NEW: the wrapper hard-fails when `os.getuid() == 0` (claude refuses `--dangerously-skip-permissions` as root, and a root-owned `$HOME/.claude` poisons the next step on the shared volume). T2 test asserts the wrapper's effective uid is 1000 and that `$HOME/.claude` is created 1000-owned |
| **socket groups across gosu** | NO - agent steps never declare `needs: [docker]`, so `/var/run/docker.sock` is not mounted and the `usermod -aG` branch is inert | Unchanged mechanism; the T2/T3 tier steps remain its only exercisers. An agent step that ever needs the socket picks up the identical, already-fixed path |
| **network reachability** | YES plus a NEW requirement: agent containers need EGRESS to `api.anthropic.com` / Google, not just `backend:8000`. Compose's `lazyaf-network` bridge has a default gateway, so egress works today | The dispatch-time key check (section 1.4) turns "no key" into a clear message; the wrapper logs the resolved backend URL and the agent name before invoking, so a DNS/egress failure is one grep away. Mock agent steps need NO egress - which is exactly why the dogfood ratchet uses one |
| **timeouts under DooD** | YES, amplified: agent steps default to 1800s where scripts default to 300s | ONE timeout owner (`executor.py`'s watchdog, process-group SIGTERM -> 5s -> SIGKILL, exit 124); `ExecutorConfig.timeout=None` in the wrapper by contract. Executor-side backstop stays `timeout + CONTROL_MODE_TIMEOUT_GRACE`. Unit test: a fake CLI that ignores SIGTERM is killed at the deadline AND `run.py` still POSTs a usage row (`cost_source="unknown"`) plus a `timeout` status |
| **tree-hash determinism** | YES, with new inputs: the staged context now includes runner-common sources | `tree_hash` is unchanged (POSIX-normalized sort key, CRLF normalization) and is computed over the STAGED tree. `STAGE_EXCLUDE` drops `__pycache__`, `*.egg-info`, `dist`, `.venv`, `uv.lock`, `tests`. Unit test: staging the same tree twice yields the same hash; a CRLF variant hashes identically; editing `runner-common/tests/**` does NOT change the agent-base hash while editing `runner_common/executors/claude.py` DOES |
| NEW: **secret leakage** | n/a | `secret_environment` never enters `run_kwargs["environment"]`; T2 test greps the created container's `inspect` output (env, labels, cmd) for the secret value and asserts absence, then asserts presence in the put_archive tar |
| NEW: **prompt duplication** (backend renderer vs `runner_common.build_prompt`) | n/a | Two live renderers is a known, time-boxed duplication: the runner one is legacy-only and is named in the 12.6 deletion list (section 5.2). Backend-side unit test pins the placeholder vocabulary |

---

## 8. WAVE SPLIT - 4 agents, disjoint file ownership

Launch order note: A and B are independent and start together; C depends on A's
`runner_common.usage` + B's `UsageManifest` schema landing (both are day-1 deliverables);
D depends on C's `agent_run.on_run_complete` hook site.

### Agent A - agent runtime and images

**Owns exclusively:** `runner-common/runner_common/agent_wrapper.py` (new),
`runner-common/runner_common/agent_config.py` (new),
`runner-common/runner_common/usage.py` (new),
`runner-common/runner_common/executors/{base,claude,gemini,mock}.py`,
`images/agent-base/**` (new), `images/claude/Dockerfile`, `images/gemini/Dockerfile`
(new), `scripts/build_images.py`, `runner-common/tests/**`.

**Test contract:**
1. `test_agent_config.py` - version pinning, missing-key reasons printed to stderr,
   consume-once delete on load AND on parse failure.
2. `test_agent_wrapper.py` - dispatch to each executor by `agent`; unknown agent exits 1;
   `getuid()==0` exits 1; usage manifest written in a `finally` even when the executor
   raises; SIGTERM handler writes a partial manifest; `commit.enabled=false` performs no
   git operations.
3. `test_usage_scrape.py` - claude `stream-json` fixture yields tokens+cost+
   `cli-reported`; claude with no result event yields `unknown`; gemini fixture yields
   tokens with `cost_usd=null`; mock yields deterministic non-zero tokens.
4. `test_build_images_staging.py` - staging determinism, CRLF equivalence, exclude list,
   three-level parent-hash chain (`base -> agent-base -> claude`).
5. `tdd/unit/control_runtime/test_image_contract.py` (text assertions, no docker):
   agent-base declares `lazyaf.agent-runtime=1`, installs runner-common with
   `PIP_USER=0`, and claude/gemini are `FROM lazyaf-agent-base:dev`.

### Agent B - usage channel backend

**Owns exclusively:** `backend/app/models/usage.py` (new), `backend/app/models/__init__.py`
(export only), `backend/app/schemas/usage.py` (new),
`backend/alembic/versions/0005_step_usage.py` (new),
`backend/app/services/usage_ingestion.py` (new), `backend/app/services/usage_pricing.py`
(new), the `/usage` endpoints in `backend/app/routers/steps.py`, the run-rollup endpoint
in `backend/app/routers/pipelines.py`, `backend/app/config.py` (adds `gpu_node_rates`
only), `tdd/unit/control_runtime/usage_contract.py` (shared contract module),
`tdd/unit/models/test_usage_model.py`, `tdd/integration/api/test_usage_ingestion.py`.

**Test contract:**
1. Auth parity - missing header 401, wrong-step token 401/403, terminal execution 409
   (same matrix as `/test-results`).
2. Idempotency - two POSTs for one `step_execution_id` leave exactly one row, updated.
3. Cost precedence - manifest cost wins; `gpu_node_id` + configured rate computes
   `gpu-node`; neither yields `unknown` with null cost.
4. `raw` over 8 KiB is truncated with a marker, not rejected; unknown `version` is 422.
5. `Decimal("0.1841")` round-trips as `Decimal("0.184100")` through SQLite.
6. `role` resolution order; a null role inside a rollup lands in `"unattributed"`.
7. Migration parity in `tdd/integration/test_migrations.py` (0005 head, guarded upgrade).

### Agent C - control runtime shipping, producers, dispatch

**Owns exclusively:** `images/base/control/run.py`, `images/base/control/backend_client.py`,
`backend/app/services/control_layer/workspace.py`,
`backend/app/services/execution/local_executor.py`,
`backend/app/services/pipeline_executor.py`,
`backend/app/services/workspace/execution_router.py`,
`backend/app/services/websocket.py` (log-observer registry only),
`backend/app/services/agent_prompt.py` (new),
`tdd/unit/control_runtime/test_usage_shipping.py`,
`tdd/unit/services/test_agent_step_dispatch.py`,
`tdd/unit/services/test_agent_prompt.py`,
`tdd/integration/services/test_agent_step_container.py`.

**Test contract:**
1. `ship_usage` never raises on any malformed input; deletes the file on every path;
   drops on 409 with a WARN; failures land in the terminal status `error`.
2. `run.py` POSTs the fallback usage record when no manifest exists (script-step case)
   and overwrites wrapper-supplied timing fields.
3. Producer<->consumer round trip: `generate_agent_config()` output loads through
   `runner_common.agent_config.load_agent_config` with zero key loss (R3), in one
   process, using the existing `control_runtime` conftest sys.path setup.
4. Routing: `agent` -> local; `executor: legacy` on an agent step still legacy with a
   WARNING; `control: false` on an agent step RAISES; unknown agent type RAISES.
5. Secret containment (T2, real docker): created container's `inspect` contains the
   secret value nowhere; the put_archive tar contains it; both config files land in
   `/workspace/.control` owned 1000:1000 and are gone after the step.
6. Full agent round trip (T2, named volume, `lazyaf-agent-base:dev`, mock agent): logs
   arrive via `POST /logs`, a `StepExecution` exists, a `StepUsage` row appears with
   non-null tokens, `StepRun.executor == "local"`, file operations landed in the repo.

### Agent D - ad-hoc runs, playground, cards, ratchet

**Owns exclusively:** `backend/app/services/agent_run.py` (new),
`backend/app/services/playground_service.py`, `backend/app/routers/playground.py`,
`backend/app/routers/cards.py`, `backend/app/routers/pipelines.py` (ad-hoc list filter
only - coordinate with B, which owns the rollup endpoint in the same file: B adds at the
bottom, D edits the list handler), `.lazyaf/pipelines/test-suite.yaml`,
`scripts/verify_executor.py`, `scripts/run_tier.py` (T3 preflight only),
`tdd/tier_floors.json`, `tdd/e2e/test_us2_card_loop.py` (new),
`tdd/integration/api/test_playground_control_mode.py` (new),
`tdd/unit/services/test_no_legacy_enqueue.py` (new),
`tdd/unit/scripts/test_verify_executor.py`.

**Test contract:**
1. `test_no_legacy_enqueue` - card start, card retry, playground start and an agent
   pipeline step produce ZERO `job_queue.enqueue` calls (spy, real objects).
2. Playground start creates a PipelineRun with `trigger_type == "playground"`, a hidden
   ad-hoc Pipeline row, and one agent StepRun; `GET /api/pipelines` does not list it.
3. Playground SSE receives log lines that arrived through `POST /api/steps/{id}/logs`
   (real WS manager with a capturing transport, never an AsyncMock - R6).
4. Playground completion sets a diff computed from the git server and deletes the
   throwaway branch unless `save_branch` was requested.
5. Card start -> mock agent -> branch pushed -> `card.status == "in_review"` -> gating
   pipeline triggered by `on_card_status_change` -> approve merges (the full US-2 chain,
   `tdd/e2e/test_us2_card_loop.py`, T3, mock agent, zero cost).
6. `verify_executor` unit tests for the four new assertions, including the negative case
   (a run with a missing StepUsage row fails the gate).

---

## 9. CROSS-AGENT CONTRACTS (pin these first; they are the only shared surfaces)

1. **Agent config file** - `/workspace/.control/agent.<step_execution_id>.json`,
   announced via `LAZYAF_AGENT_CONFIG_PATH` inside the STEP CONFIG FILE's `environment`
   (never container env). Producer C, consumer A, round-trip test owned by C.
2. **Usage manifest file** - `/workspace/.control/usage.<step_execution_id>.json`,
   announced via `LAZYAF_USAGE_PATH` injected by `run.py` into `config.environment`.
   Writer A, shipper C, validator B.
3. **`UsageManifest` wire shape** - `backend/app/schemas/usage.py` is the source; both
   sides' tests import `tdd/unit/control_runtime/usage_contract.py`. Owner B.
4. **`ExecutorResult.usage: dict | None`** - one new optional dataclass field. Owner A;
   nothing else may add fields to that dataclass this wave.
5. **Agent vocabulary + default images** - `claude-code` / `gemini` / `mock`;
   `lazyaf-claude:dev` / `lazyaf-gemini:dev` / `lazyaf-agent-base:dev`. Backend map
   owned by C, images by A.
6. **`secret_environment` step-config key** - honored by LocalExecutor (C), populated by
   `pipeline_executor` (C) and `agent_run` (D). File-only, never container env.
7. **`trigger_type` values `card_work` / `playground`** and the one-line
   `agent_run.on_run_complete(db, pipeline_run, success)` call site at the end of
   `_complete_pipeline`. Call site landed by C, module implemented by D.
8. **`manager.register_run_log_observer` / `unregister_run_log_observer`** - added by C
   in `websocket.py`, consumed by D in `playground_service`.

---

## 10. Seams left open on purpose

- `role` is on the wire and in the schema but is `null` everywhere in 12.5. M13 fills it
  and adds `trial_iteration_id` with the trials table.
- `LAZYAF_GPU_NODE_ID` / `LAZYAF_GPU_FRACTION` are read by `run.py` and priced by the
  server, but nothing sets them until 12.6 puts steps on self-hosted nodes.
- `{spec_context}` prompt injection is 12.6.6's; `agent_prompt.py` is where it lands, and
  `context` in the agent config is where it travels.
- `PipelineRun.steps_snapshot` (which would retire ad-hoc Pipeline rows and fix
  edited-mid-run) is a 12.6/12.7 candidate, deliberately not this phase.
- Heartbeat-driven deadline extension is still telemetry-only (12.3 limitation); agent
  steps make it matter more, but the 1800s default plus the executor backstop covers
  12.5. Revisit when a real agent step needs to run past its timeout.
- Debug re-run mode (12.7) will want the agent config file preserved rather than
  consumed; that is a `debug: true` flag on the step, not a change to consume-once.
