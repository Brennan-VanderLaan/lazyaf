#!/usr/bin/env python3
"""
LazyAF Control Layer - Container-side step execution manager.

Main entrypoint for control-mode step containers (exec'd by entrypoint.sh when
LAZYAF_CONTROL=1). It:
1. Reads step configuration from the CONFIG_PATH env var (the backend writes
   a PER-STEP file /workspace/.control/<step_execution_id>.json and sets
   CONFIG_PATH to it) and DELETES that exact path in a finally that runs on
   EVERY outcome, parse failure included (consume-once: the workspace volume
   persists across steps; a stale config re-executing a previous step is the
   landmine 12.3 exists to kill)
2. Reports "running" status to backend
3. Starts heartbeat thread
4. Executes the command (shell-wrapped, timeout enforced in-container) with
   LAZYAF_TEST_RESULTS_PATH injected PER-STEP into the step environment
   (12.2.6 contract #2: /workspace/.control/test_results.<step_execution_id>
   .json — derived as test_results.<step_id>.json NEXT TO the step config,
   so the same config-collision lesson applies: two steps sharing the
   workspace volume can never race on one manifest path)
5. If the step left a manifest at that path, POSTs it to
   /api/steps/{id}/test-results and deletes the file (consume-once).
   Delivery failure NEVER fails the step — it is recorded loudly in the
   terminal status error instead (the dropped-log-lines pattern).
5b. Ships the USAGE manifest (12.5, protocol channel #4) the same way:
   LAZYAF_USAGE_PATH is injected PER-STEP into the step environment, the
   agent wrapper writes /workspace/.control/usage.<step_execution_id>.json,
   and this process POSTs it to /api/steps/{id}/usage and deletes it — on
   EVERY outcome. run.py owns the timing (wall_clock_ms, container_seconds)
   and the node attribution (role / gpu_node_id / gpu_fraction from
   container env) because it is the ONE component present for script steps
   too. A step that produced no manifest still gets a fallback record, so
   wall-clock and container time are complete across the whole graph.
   Telemetry NEVER fails a step.
6. Reports terminal status (completed / failed / timeout)

The container EXIT CODE remains the backend's ground truth for step outcome;
these reports carry telemetry, logs and liveness.

Environment:
- CONFIG_PATH: Override config file location (default: /workspace/.control/step_config.json)
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

# Container lifetime clock (12.5). Captured at IMPORT, i.e. as early as this
# process can observe itself, and read again when the usage manifest ships.
# Documented as a LOWER BOUND for container_seconds: it excludes the image
# pull and the entrypoint's chown, both of which happen before python starts.
# 12.6 may let the executor supply the true container lifetime when a GPU
# node actually bills for it.
_PROCESS_START = time.monotonic()

if __package__:  # imported as control.run (unit tests)
    from .backend_client import BackendClient
    from .config import load_step_config
    from .executor import execute_command
    from .heartbeat import HeartbeatManager
else:  # executed as a script: python3 /control/run.py
    from backend_client import BackendClient
    from config import load_step_config
    from executor import execute_command
    from heartbeat import HeartbeatManager

# Default config location
DEFAULT_CONFIG_PATH = Path("/workspace/.control/step_config.json")


def test_results_path(config_path: Path, step_id: str) -> Path:
    """Per-step manifest path (12.2.6 contract #2).

    Derived NEXT TO the step config file — in the image that directory is
    /workspace/.control, so the value is
    /workspace/.control/test_results.<step_execution_id>.json. Per-step by
    construction (the config-collision lesson): concurrent steps on one
    workspace volume can never race on a shared manifest path.
    """
    return config_path.parent / f"test_results.{step_id}.json"


def usage_path(config_path: Path, step_id: str) -> Path:
    """Per-step usage manifest path (12.5, cross-agent contract #2).

    Same construction as test_results_path and for the same reason: derived
    NEXT TO the step config (in the image /workspace/.control), per-step by
    name, so two steps sharing one workspace volume can never race on a
    single manifest path — and so a step can never be pointed at another
    step's accounting.
    """
    return config_path.parent / f"usage.{step_id}.json"


def agent_config_path(config_path: Path, step_id: str) -> Path:
    """Per-step AGENT config path (12.5, cross-agent contract #1).

    Written by the backend into the SAME put_archive tar as the step config
    and consumed (deleted) by `runner_common.agent_wrapper` on load. This
    runtime never reads it - it only knows the path so it can act as the
    consume-once BACKSTOP: a wrapper that was SIGKILLed before loading, or a
    step whose command turned out not to be the wrapper at all, must not
    leave a rendered prompt sitting on a workspace volume that outlives the
    step.
    """
    return config_path.parent / f"agent.{step_id}.json"


def sweep_agent_config(path: Path) -> None:
    """Consume-once backstop for the agent config. Never raises.

    Housekeeping only: it reports what it had to clean but NEVER touches the
    step's outcome or its terminal status error - the step that got killed
    before its wrapper read the file is already reporting its own failure.
    """
    try:
        if not path.exists():
            return
        print(
            f"[control] WARNING: agent config {path.name} survived the step "
            "(the wrapper never consumed it); deleting it now",
            file=sys.stderr,
        )
        path.unlink()
    except OSError as e:
        print(
            f"[control] WARNING: could not delete agent config {path}: {e}",
            file=sys.stderr,
        )
    except Exception as e:  # belt-and-braces: housekeeping never escapes
        print(
            f"[control] WARNING: agent config sweep crashed: {e!r}",
            file=sys.stderr,
        )


# --- Manifest wire contract (12.2.6, cross-agent #2) -------------------------
# The ONE pinned shape, mirrored by the shared contract module the tests on
# both sides import (tdd/unit/control_runtime/manifest_contract.py):
#   {"version": 1, "results": [{"lazyaf_test_id": str (non-empty),
#                               "status": "passed"|"failed"|"skipped",
#                               "duration_ms": int|None,
#                               "file_path": str|None}]}
# file_path is REPO-ROOT-relative with "/" separators (cross-agent #3).
MANIFEST_VERSION = 1
MANIFEST_STATUSES = frozenset({"passed", "failed", "skipped"})
MANIFEST_RESULT_KEYS = ("lazyaf_test_id", "status", "duration_ms", "file_path")


def _coerce_result_entry(entry, index, warnings):
    """Normalize one result entry to the pinned shape, or None to SKIP it.

    A step's manifest is UNTRUSTED input (any command can write that path):
    a bad entry is dropped with a loud warning, never propagated to the
    backend and never allowed to break terminal status reporting.
    """
    if not isinstance(entry, dict):
        warnings.append(f"results[{index}] is not an object (dropped)")
        return None

    test_id = entry.get("lazyaf_test_id")
    if not isinstance(test_id, str) or not test_id:
        warnings.append(
            f"results[{index}] has no usable lazyaf_test_id (dropped)"
        )
        return None

    status = entry.get("status")
    if status not in MANIFEST_STATUSES:
        warnings.append(
            f"results[{index}] ({test_id}) has unknown status {status!r} "
            "(dropped)"
        )
        return None

    duration = entry.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        if duration is not None:
            warnings.append(
                f"results[{index}] ({test_id}) duration_ms {duration!r} is "
                "not a number (nulled)"
            )
        duration = None
    else:
        duration = int(duration)
        if duration < 0:
            warnings.append(
                f"results[{index}] ({test_id}) duration_ms was negative "
                "(nulled)"
            )
            duration = None

    file_path = entry.get("file_path")
    if file_path is not None and not isinstance(file_path, str):
        warnings.append(
            f"results[{index}] ({test_id}) file_path is not a string (nulled)"
        )
        file_path = None
    if isinstance(file_path, str):
        file_path = file_path.replace("\\", "/") or None

    extra = set(entry) - set(MANIFEST_RESULT_KEYS)
    if extra:
        warnings.append(
            f"results[{index}] ({test_id}) carried unknown keys "
            f"{sorted(extra)} (dropped from the wire payload)"
        )

    return {
        "lazyaf_test_id": test_id,
        "status": status,
        "duration_ms": duration,
        "file_path": file_path,
    }


def normalize_manifest(raw):
    """Validate/coerce an untrusted manifest into the pinned wire shape.

    Returns ``(manifest_or_None, warnings)``. ``None`` means there is
    nothing worth POSTing (unusable envelope, or every entry dropped);
    ``warnings`` is a list of human-readable strings for the terminal
    status error. NEVER raises — a malformed manifest must not be able to
    stop terminal status reporting.
    """
    warnings: list = []

    if not isinstance(raw, dict):
        return None, [
            f"top level is a {type(raw).__name__}, expected an object with a "
            "'results' list (whole manifest dropped)"
        ]

    results = raw.get("results")
    if not isinstance(results, list):
        return None, [
            f"'results' is {type(results).__name__}, expected a list "
            "(whole manifest dropped)"
        ]

    version = raw.get("version", MANIFEST_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        warnings.append(
            f"'version' {version!r} is not an int (sent as {MANIFEST_VERSION})"
        )
        version = MANIFEST_VERSION

    entries = []
    for index, entry in enumerate(results):
        coerced = _coerce_result_entry(entry, index, warnings)
        if coerced is not None:
            entries.append(coerced)

    if not entries:
        warnings.append("no usable results (nothing sent)")
        return None, warnings

    return {"version": version, "results": entries}, warnings


def ship_test_results(manifest_path: Path, client) -> "str | None":
    """Pick up the step's test-results manifest, POST it, consume the file.

    Returns a warning string when pickup/validation/delivery failed
    (unreadable JSON, malformed shape, retry budget exhausted) — the caller
    appends it to the terminal status error so the drop is LOUD, but
    manifest delivery never changes the step outcome. The file is deleted on
    EVERY path (consume-once, like the step config: the workspace volume
    outlives this step).

    HARD RULE: this function never raises. The manifest is written by the
    step's own command, i.e. arbitrary untrusted bytes; ANY exception here
    is a delivery failure, never a reason for the step to die without
    reporting its terminal status.
    """
    warning = None
    try:
        if not manifest_path.exists():
            return None

        try:
            try:
                with open(manifest_path, "r") as f:
                    raw = json.load(f)
            except (OSError, ValueError) as e:
                warning = (
                    f"[control] WARNING: test results manifest unreadable: {e}"
                )
            else:
                manifest, problems = normalize_manifest(raw)
                if problems:
                    warning = (
                        "[control] WARNING: test results manifest problems: "
                        + "; ".join(problems)
                    )
                if manifest is not None:
                    print(
                        "[control] Shipping test results manifest "
                        f"({len(manifest['results'])} results)...",
                        flush=True,
                    )
                    if not client.send_test_results(manifest):
                        deliver_warn = (
                            "[control] WARNING: test results manifest failed "
                            "to reach backend"
                        )
                        warning = (
                            f"{warning}\n{deliver_warn}"
                            if warning
                            else deliver_warn
                        )
        finally:
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                unlink_warn = (
                    "[control] WARNING: could not delete test results "
                    f"manifest: {e}"
                )
                warning = f"{warning}\n{unlink_warn}" if warning else unlink_warn
    except Exception as e:  # belt-and-braces: NOTHING escapes this function
        crash_warn = (
            f"[control] WARNING: test results manifest handling crashed: {e!r}"
        )
        warning = f"{warning}\n{crash_warn}" if warning else crash_warn

    if warning:
        print(warning, file=sys.stderr)
    return warning


# --- Usage wire contract (12.5, cross-agent #2/#3) --------------------------
# The ONE pinned shape, owned by backend/app/schemas/usage.py (UsageManifest,
# api-surface 2.2) and mirrored by the shared contract module both sides'
# tests import (tdd/unit/control_runtime/usage_contract.py).
#
# OWNERSHIP (R3: one writer per datum):
# - the WRAPPER writes provider/model/model_version/tokens/cost_usd/
#   cost_source/determinism/raw from the CLI's own report
# - THIS FILE writes wall_clock_ms + container_seconds (it is the only
#   component present for script steps too, so timing has exactly one owner)
#   and role / gpu_node_id / gpu_fraction from non-secret container env
# - the SERVER writes step_run_id, pipeline_run_id, gpu-node pricing and the
#   role fallback
USAGE_VERSION = 1
USAGE_PROVIDERS = frozenset(
    {"anthropic", "google", "openai-compatible", "self-hosted"}
)
USAGE_COST_SOURCES = frozenset(
    {"cli-reported", "gpu-node", "estimated", "unknown"}
)
DEFAULT_USAGE_PROVIDER = "self-hosted"
USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)
# Fields the WRAPPER owns; anything else in its manifest is dropped (with a
# warning) rather than forwarded, so an unknown key can never 422 the POST.
USAGE_WRAPPER_KEYS = (
    "provider",
    "model",
    "model_version",
    "cost_usd",
    "cost_source",
    "determinism",
    "raw",
) + USAGE_TOKEN_KEYS
# Fields THIS FILE owns, sourced from non-secret container env.
USAGE_ROLE_ENV = "LAZYAF_ROLE"
USAGE_GPU_NODE_ENV = "LAZYAF_GPU_NODE_ID"
USAGE_GPU_FRACTION_ENV = "LAZYAF_GPU_FRACTION"
USAGE_PROVIDER_ENV = "LAZYAF_USAGE_PROVIDER"


def _env_str(name):
    """Non-empty container env value, or None. Never raises."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_float(name, warnings):
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        warnings.append(f"{name} {raw!r} is not a number (dropped)")
        return None


def _usage_int(value, key, warnings):
    """Coerce one token count to int|None. Bools are NOT ints here."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(f"{key} {value!r} is not a number (nulled)")
        return None
    value = int(value)
    if value < 0:
        warnings.append(f"{key} was negative (nulled)")
        return None
    return value


def _usage_money(value, warnings):
    """Dollars travel as a STRING on the wire (api-surface 0: never a float).

    A float from a hand-written manifest is accepted but STRINGIFIED here;
    the wire never carries a float for money.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        warnings.append(f"cost_usd {value!r} is not a number (nulled)")
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return repr(float(value)) if isinstance(value, float) else str(value)
    warnings.append(f"cost_usd {value!r} is not a number or string (nulled)")
    return None


def normalize_usage_manifest(raw, wall_clock_ms, container_seconds):
    """Build the wire manifest from the wrapper's (untrusted) manifest.

    ``raw`` of None means "no manifest existed" — the script/docker case and
    the killed-before-writing case. Both still yield a COMPLETE record with
    ``cost_source="unknown"``: that is the recorded fact that the provider
    told us nothing, which is not the same as no row at all (M13 counts
    those rows as cost_coverage < 1.0 rather than reporting a quietly
    too-cheap median).

    Returns ``(manifest, warnings)``. NEVER raises: the manifest is written
    by the step's own wrapper, i.e. untrusted bytes, and accounting must
    never be able to stop terminal status reporting.
    """
    warnings = []
    if raw is None:
        source = {}
    elif isinstance(raw, dict):
        source = raw
        version = source.get("version", USAGE_VERSION)
        if version != USAGE_VERSION:
            warnings.append(
                f"manifest version {version!r} != {USAGE_VERSION} "
                "(wrapper fields dropped; posting the fallback record)"
            )
            source = {}
        else:
            unknown = sorted(
                set(source)
                - set(USAGE_WRAPPER_KEYS)
                - {
                    "version",
                    "wall_clock_ms",
                    "container_seconds",
                    "role",
                    "gpu_node_id",
                    "gpu_fraction",
                }
            )
            if unknown:
                warnings.append(
                    f"manifest carried unknown keys {unknown} "
                    "(dropped from the wire payload)"
                )
    else:
        warnings.append(
            f"manifest top level is a {type(raw).__name__}, expected an "
            "object (posting the fallback record)"
        )
        source = {}

    provider = source.get("provider")
    if provider is not None and provider not in USAGE_PROVIDERS:
        warnings.append(f"provider {provider!r} is not a known provider")
        provider = None
    if provider is None:
        env_provider = _env_str(USAGE_PROVIDER_ENV)
        if env_provider is not None and env_provider not in USAGE_PROVIDERS:
            warnings.append(
                f"{USAGE_PROVIDER_ENV}={env_provider!r} is not a known "
                "provider (using the default)"
            )
            env_provider = None
        provider = env_provider or DEFAULT_USAGE_PROVIDER

    cost_source = source.get("cost_source")
    if cost_source is not None and cost_source not in USAGE_COST_SOURCES:
        warnings.append(f"cost_source {cost_source!r} is not a known source")
        cost_source = None
    cost_source = cost_source or "unknown"

    determinism = source.get("determinism")
    if determinism is not None and not isinstance(determinism, dict):
        warnings.append("determinism is not an object (sent as {})")
        determinism = None

    blob = source.get("raw")
    if blob is not None and not isinstance(blob, dict):
        warnings.append("raw is not an object (dropped)")
        blob = None

    strings = {}
    for key in ("model", "model_version"):
        value = source.get(key)
        if value is not None and not isinstance(value, str):
            warnings.append(f"{key} {value!r} is not a string (nulled)")
            value = None
        strings[key] = value

    # role: this file owns it (from container env); the wrapper's value is
    # the fallback so a manifest read by hand stays self-describing. Both are
    # null in 12.5 — M13 fills LAZYAF_ROLE.
    role = _env_str(USAGE_ROLE_ENV)
    if role is None:
        manifest_role = source.get("role")
        role = manifest_role if isinstance(manifest_role, str) else None

    manifest = {
        "version": USAGE_VERSION,
        "provider": provider,
        "model": strings["model"],
        "model_version": strings["model_version"],
        "cost_usd": _usage_money(source.get("cost_usd"), warnings),
        "cost_source": cost_source,
        # run.py-owned: overwrite whatever the wrapper wrote.
        "wall_clock_ms": max(int(wall_clock_ms or 0), 0),
        "container_seconds": container_seconds,
        "gpu_node_id": _env_str(USAGE_GPU_NODE_ENV),
        "gpu_fraction": _env_float(USAGE_GPU_FRACTION_ENV, warnings),
        "determinism": determinism or {},
        "role": role,
        "raw": blob,
    }
    for key in USAGE_TOKEN_KEYS:
        manifest[key] = _usage_int(source.get(key), key, warnings)
    return manifest, warnings


def ship_usage(manifest_path: Path, client, wall_clock_ms, container_seconds):
    """Pick up the step's usage manifest, POST it, consume the file.

    Returns a warning string when something was WRONG (unreadable manifest,
    unknown version, rejected/undeliverable POST) — the caller appends it to
    the terminal status error so the problem is LOUD. A simply ABSENT
    manifest is not a problem: script and docker steps never write one, and
    the fallback record is the designed outcome, so that path prints an
    informational line and returns None rather than decorating every green
    script step's status with a warning.

    HARD RULES, identical to ship_test_results:
    - this function NEVER raises,
    - the file is deleted on EVERY path (consume-once: the workspace volume
      outlives this step),
    - the step's exit code is NEVER changed by anything here.
    """
    warning = None
    raw = None
    try:
        try:
            if manifest_path.exists():
                try:
                    with open(manifest_path, "r") as f:
                        raw = json.load(f)
                except (OSError, ValueError) as e:
                    warning = (
                        f"[control] WARNING: usage manifest unreadable: {e}"
                    )
            else:
                print(
                    "[control] No usage manifest (no agent wrapper ran); "
                    "posting the fallback usage record",
                    flush=True,
                )

            manifest, problems = normalize_usage_manifest(
                raw, wall_clock_ms, container_seconds
            )
            if problems:
                problem_warn = (
                    "[control] WARNING: usage manifest problems: "
                    + "; ".join(problems)
                )
                warning = (
                    f"{warning}\n{problem_warn}" if warning else problem_warn
                )

            print(
                "[control] Shipping usage record "
                f"(provider={manifest['provider']}, "
                f"cost_source={manifest['cost_source']})...",
                flush=True,
            )
            status = client.send_usage(manifest)
            if status is None:
                deliver_warn = (
                    "[control] WARNING: usage record failed to reach backend "
                    "(retry budget exhausted)"
                )
            elif status == 409:
                deliver_warn = (
                    "[control] WARNING: usage record dropped - the step "
                    "execution was already terminal (409)"
                )
            elif not 200 <= status < 300:
                deliver_warn = (
                    f"[control] WARNING: usage record rejected with HTTP "
                    f"{status}"
                )
            else:
                deliver_warn = None
            if deliver_warn:
                warning = (
                    f"{warning}\n{deliver_warn}" if warning else deliver_warn
                )
        finally:
            try:
                manifest_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                unlink_warn = (
                    f"[control] WARNING: could not delete usage manifest: {e}"
                )
                warning = f"{warning}\n{unlink_warn}" if warning else unlink_warn
    except Exception as e:  # belt-and-braces: NOTHING escapes this function
        crash_warn = (
            f"[control] WARNING: usage manifest handling crashed: {e!r}"
        )
        warning = f"{warning}\n{crash_warn}" if warning else crash_warn

    if warning:
        print(warning, file=sys.stderr)
    return warning


def main() -> int:
    """
    Main control layer entry point.

    Returns:
        Exit code (0 = success, non-zero = failure, 124 = timeout)
    """
    # 1. Determine config path
    config_path_str = os.environ.get("CONFIG_PATH")
    config_path = Path(config_path_str) if config_path_str else DEFAULT_CONFIG_PATH

    print(f"[control] Loading config from {config_path}", flush=True)

    # 2. Load configuration; consume-once on EVERY path (parse failure
    # included) — the config and its token must not survive this step.
    config = None
    try:
        config = load_step_config(config_path)
    finally:
        try:
            config_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            print(
                f"[control] WARNING: could not delete step config: {e}",
                file=sys.stderr,
            )

    if config is None:
        print("[control] ERROR: Could not load step config", file=sys.stderr)
        return 1

    print(f"[control] Step ID: {config.step_id}", flush=True)
    print(f"[control] Command: {config.command}", flush=True)

    # 3. Initialize backend client
    client = BackendClient(
        backend_url=config.backend_url,
        step_id=config.step_id,
        auth_token=config.auth_token,
    )

    # 4. Report RUNNING status
    print("[control] Reporting running status...", flush=True)
    if not client.report_status("running"):
        print("[control] WARNING: Could not report running status", file=sys.stderr)

    # 5. Start heartbeat thread (interval is a module constant, not config)
    heartbeat = HeartbeatManager(client)
    heartbeat.start()

    # 6. Handle signals for graceful shutdown
    def handle_signal(signum, frame):
        print(f"\n[control] Received signal {signum}, stopping...", flush=True)
        heartbeat.stop()
        client.report_status(
            "failed", exit_code=-signum, error=f"Killed by signal {signum}"
        )
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # 7. Execute command (timeout enforced inside execute_command).
    # Inject the per-step test-results manifest path (12.2.6 contract #2)
    # into the step environment — platform-owned, so it overrides any
    # user-supplied value: a step must never write another step's manifest.
    manifest_path = test_results_path(config_path, config.step_id)
    config.environment["LAZYAF_TEST_RESULTS_PATH"] = str(manifest_path)

    # Same platform-owned injection for the usage sidecar (12.5, contract
    # #2): the agent wrapper writes usage.<step_execution_id>.json HERE and
    # nowhere else, so a step can never point the platform at another step's
    # accounting.
    usage_manifest_path = usage_path(config_path, config.step_id)
    config.environment["LAZYAF_USAGE_PATH"] = str(usage_manifest_path)

    print(f"[control] Executing command in {config.working_directory}", flush=True)
    print("-" * 60, flush=True)

    command_started = time.monotonic()
    try:
        result = execute_command(config, client)
    except Exception as e:
        print(f"[control] ERROR: Command execution failed: {e}", file=sys.stderr)
        from types import SimpleNamespace

        result = SimpleNamespace(exit_code=1, timed_out=False)
    wall_clock_ms = int((time.monotonic() - command_started) * 1000)

    print("-" * 60, flush=True)

    # 8. Stop heartbeat
    heartbeat.stop()

    # 8.5. Ship the test-results manifest if the step produced one (12.2.6).
    # Runs on EVERY outcome (a timed-out pytest may still have written a
    # complete manifest — the plugin writes atomically). Never changes the
    # step outcome; failures surface in the terminal status error below.
    manifest_warning = ship_test_results(manifest_path, client)

    # 8.6. Ship the usage record (12.5, protocol channel #4) — after the
    # test-results manifest, BEFORE the terminal /status POST (the endpoint
    # 409s a terminal StepExecution). EVERY control-mode step produces a
    # row: an agent step from the wrapper's manifest, a script step from the
    # fallback record. Never changes the step outcome.
    container_seconds = round(time.monotonic() - _PROCESS_START, 3)
    usage_warning = ship_usage(
        usage_manifest_path, client, wall_clock_ms, container_seconds
    )

    # 8.7. Consume-once backstop for the agent config (12.5, contract #1).
    # The wrapper deletes it on load; this covers the paths where it never
    # got that far. Housekeeping only - it can never change the outcome.
    sweep_agent_config(agent_config_path(config_path, config.step_id))

    # 9. Report terminal status
    if result.timed_out:
        status = "timeout"
        error = f"Step exceeded timeout of {config.timeout_seconds}s"
        print(f"[control] Command timed out after {config.timeout_seconds}s", flush=True)
    elif result.exit_code == 0:
        status = "completed"
        error = None
        print("[control] Command completed successfully", flush=True)
    else:
        status = "failed"
        error = f"Command exited with code {result.exit_code}"
        print(f"[control] Command failed with exit code {result.exit_code}", flush=True)

    if manifest_warning:
        error = f"{error}\n{manifest_warning}" if error else manifest_warning

    if usage_warning:
        error = f"{error}\n{usage_warning}" if error else usage_warning

    if client.dropped_log_lines:
        warning = (
            f"[control] WARNING: {client.dropped_log_lines} log lines "
            "failed to reach backend"
        )
        print(warning, file=sys.stderr)
        error = f"{error}\n{warning}" if error else warning

    if not client.report_status(status, exit_code=result.exit_code, error=error):
        print("[control] WARNING: Could not report completion status", file=sys.stderr)

    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
