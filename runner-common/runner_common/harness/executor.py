"""
``HarnessExecutor`` — the ``EXECUTORS`` entry that makes an inference server
behave like an agent (Milestone 14.2).

IT IS A NEW EXECUTOR, NOT A NEW STEP TYPE. Everything around it is 12.5,
unchanged: the wrapper loads and consumes the agent config, refuses to run as
root, installs the SIGTERM handler, materialises spec context, calls
``executor.execute(...)``, then ``_finish(cfg, result)``, then writes the usage
manifest in a ``finally``.

IT ADDS ZERO FIELDS TO ``ExecutorConfig`` AND ``ExecutorResult``. 12.5's
cross-agent contract #4 survives this milestone intact, because the harness
takes its configuration through the ``EXECUTORS`` BUILDER — exactly as
``ClaudeExecutor(output_format=...)`` and ``MockExecutor(mock_config=...)``
already do.

IT TOUCHES GIT ONCE, READ-ONLY. ``git status --porcelain``, to answer "did the
working tree change" for section 3.5's success rule. There is no ``add``, no
``commit``, no ``checkout``, no ``push`` and no import of ``git_helpers``:
landing work belongs to ``agent_wrapper._finish`` and there is no second
implementation (section 3.6). That is also why ``run_shell`` denies ``git
push`` — letting the model push would create a second, unpoliced path to the
remote and re-fire the push trigger that started the run.
"""
import os
import signal
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from ..executors.base import AgentExecutor, ExecutorConfig, ExecutorResult
from ..usage import (
    SCRAPE_ERROR_KEY,
    SCRAPE_OK_KEY,
    TokenAccumulator,
)
from .client import EndpointFatal, OpenAICompatClient
from .constants import (
    DEFAULT_ASSUMED_CONTEXT,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_MAX_TOTAL_TOKENS,
    EXIT_CONTEXT,
    EXIT_ENDPOINT,
    EXIT_FAILED,
    EXIT_OK,
    HARNESS_COST_SOURCE,
    HARNESS_PROVIDER,
    LOG_PREFIX,
    MAX_MALFORMED_RETRIES,
    MAX_TOOL_CALLS_PER_TURN,
    STOP_EXIT_CODES,
    TOOL_OUTPUT_MAX_BYTES,
    TOOL_SHELL_TIMEOUT,
)
from .fallback import system_prompt
from .loop import (
    HarnessContext,
    human_duration,
    human_tokens,
    resolve_harness_mode,
    run_loop,
)
from .tools import TOOL_ORDER, Sandbox, changed_path_count
from .transcript import ContextFloorUnmeetable, Transcript

RUNNER_TYPE = "openai-harness"

#: Phrases that read as a completion CLAIM. Deliberately small and stated:
#: this heuristic only ever decides whether a step that stopped calling tools
#: is allowed to be green, and section 3.5's change check still has to agree.
#: A magic phrase is never how termination is DECIDED — that is ``finish``.
_FINAL_PHRASES = (
    "task is complete",
    "task is done",
    "is now complete",
    "i have completed",
    "i have implemented",
    "i've implemented",
    "have been implemented",
    "all done",
    "that's everything",
    "thats everything",
    "the change is complete",
    "implementation is complete",
    "successfully implemented",
    "successfully created",
    "finished the task",
)


def looks_final(prose: str) -> bool:
    lowered = " ".join(str(prose or "").lower().split())
    return any(phrase in lowered for phrase in _FINAL_PHRASES)


class HarnessExecutor(AgentExecutor):
    """Drive an OpenAI-compatible endpoint through a real coding task."""

    def __init__(
        self,
        endpoint: Optional[Dict[str, Any]] = None,
        harness: Optional[Dict[str, Any]] = None,
        *,
        client_factory=None,
        env: Optional[Dict[str, str]] = None,
    ):
        self.endpoint = dict(endpoint or {})
        self.harness = dict(harness or {})
        #: Injected by the unit suite; the container gets the real client.
        self._client_factory = client_factory
        self._env = dict(env) if env is not None else None

    # -- identity ----------------------------------------------------------

    @property
    def name(self) -> str:
        return "LazyAF harness"

    @property
    def runner_type(self) -> str:
        return RUNNER_TYPE

    def build_command(self, config: ExecutorConfig):
        """USED ONLY FOR THE ``$ ...`` LOG LINE. No subprocess is ever spawned
        for the model — the harness IS the agent."""
        return [
            "<lazyaf-harness>",
            str(self.endpoint.get("name") or "?"),
            str(self.endpoint.get("model") or "?"),
        ]

    # -- resolved settings -------------------------------------------------

    @property
    def capabilities(self) -> Dict[str, Any]:
        caps = self.endpoint.get("capabilities")
        return caps if isinstance(caps, dict) else {}

    @property
    def pricing(self) -> Dict[str, Any]:
        pricing = self.endpoint.get("pricing")
        return pricing if isinstance(pricing, dict) else {}

    @property
    def mode(self) -> str:
        return resolve_harness_mode(self.endpoint, self.harness)

    def _setting(self, key: str, default):
        value = self.harness.get(key)
        return default if value is None else value

    def _environ(self) -> Dict[str, str]:
        return self._env if self._env is not None else dict(os.environ)

    # -- the run -----------------------------------------------------------

    def execute(
        self,
        config: ExecutorConfig,
        log_callback: Optional[Callable[[str], None]] = None,
        streaming: bool = True,
    ) -> ExecutorResult:
        emit = log_callback or (lambda line: None)
        started = time.monotonic()

        try:
            mode = self.mode
        except ValueError as exc:
            emit(LOG_PREFIX + f"ERROR: {exc}")
            return ExecutorResult(
                success=False, exit_code=EXIT_FAILED, error=str(exc)
            )

        endpoint = self.endpoint
        name = str(endpoint.get("name") or "?")
        model = str(endpoint.get("model") or config.model or "?")
        base_url = str(endpoint.get("base_url") or "")
        if not base_url or not endpoint.get("model"):
            reason = (
                "the endpoint block on the wire carries no base_url/model; the "
                "backend must not dispatch an openai-harness step without one"
            )
            emit(LOG_PREFIX + f"ERROR: {reason}")
            return ExecutorResult(
                success=False, exit_code=EXIT_FAILED, error=reason
            )

        # -- the endpoint key, by NAME, never by value ---------------------
        auth_style = str(endpoint.get("auth_style") or "none")
        auth_env = endpoint.get("auth_env")
        environ = self._environ()
        api_key = None
        if auth_style != "none":
            if not auth_env:
                reason = (
                    f"endpoint '{name}' declares auth_style={auth_style} but the "
                    "wire names no container-side environment variable "
                    "(endpoint.auth_env)"
                )
                emit(LOG_PREFIX + f"ERROR: {reason}")
                return ExecutorResult(
                    success=False, exit_code=EXIT_ENDPOINT, error=reason
                )
            api_key = environ.get(str(auth_env)) or None
            if not api_key:
                # NAMES THE VARIABLE, NEVER THE VALUE. Burning 30 seconds of
                # container start to reach an opaque 401 is the outcome this
                # refusal exists to prevent.
                reason = (
                    f"endpoint '{name}' needs an API key but the container "
                    f"environment variable {auth_env} is unset or empty"
                )
                emit(LOG_PREFIX + f"ERROR: {reason}")
                return ExecutorResult(
                    success=False, exit_code=EXIT_ENDPOINT, error=reason
                )

        # -- budgets --------------------------------------------------------
        max_iterations = int(self._setting("max_iterations", DEFAULT_MAX_ITERATIONS))
        max_total_tokens = int(
            self._setting("max_total_tokens", DEFAULT_MAX_TOTAL_TOKENS)
        )
        time_budget = self._setting("time_budget_seconds", None)
        max_calls = int(
            self._setting("max_tool_calls_per_turn", MAX_TOOL_CALLS_PER_TURN)
        )
        shell_timeout = int(
            self._setting("shell_timeout_seconds", TOOL_SHELL_TIMEOUT)
        )
        output_cap = int(
            self._setting("tool_output_max_bytes", TOOL_OUTPUT_MAX_BYTES)
        )
        require_changes = bool(self._setting("require_changes", True))
        context_window = self.capabilities.get("context_window")
        max_output_tokens = self.capabilities.get("max_output_tokens")

        workdir = Path(config.workspace)
        sandbox = Sandbox(
            workdir=workdir,
            shell_timeout=shell_timeout,
            output_max_bytes=output_cap,
            api_key_env=str(auth_env) if auth_env else None,
            api_key_value=api_key,
            base_env=environ,
        )

        client = (self._client_factory or _default_client_factory)(
            base_url=base_url,
            model=model,
            api_key=api_key,
            auth_style=auth_style,
            auth_header=endpoint.get("auth_header"),
            timeout=float(endpoint.get("request_timeout_seconds") or 300),
            temperature=self.harness.get("temperature", 0),
            top_p=self.harness.get("top_p"),
            seed=self.harness.get("seed"),
            max_output_tokens=int(max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS),
        )

        # -- the FIRST line of every step, before any request (risk register)
        emit(LOG_PREFIX + f"$ {' '.join(self.build_command(config))}")
        emit(
            LOG_PREFIX
            + (
                f"harness: endpoint={name} model={model} mode={mode} "
                f"ctx={context_window if context_window else 'unknown'} "
                f"reach={endpoint.get('reach') or 'direct'} url={base_url}"
            )
        )
        emit(
            LOG_PREFIX
            + (
                f"harness: budgets iterations={max_iterations} "
                f"tokens={max_total_tokens} "
                f"deadline={int(time_budget) if time_budget else 'none'}s "
                f"tools={len(TOOL_ORDER)}"
            )
        )
        if not context_window:
            emit(
                LOG_PREFIX
                + (
                    "WARNING: endpoint declares no context window; assuming "
                    f"{DEFAULT_ASSUMED_CONTEXT} tokens"
                )
            )
        if not time_budget:
            emit(
                LOG_PREFIX
                + (
                    "WARNING: no harness.time_budget_seconds on the wire; the "
                    "soft deadline is disabled and only the container watchdog "
                    "bounds this step"
                )
            )

        accumulator = TokenAccumulator()
        transcript = Transcript(
            system=system_prompt(mode, str(sandbox.workdir), max_iterations),
            task=config.prompt or "",
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            endpoint_name=name,
            log=lambda message: emit(LOG_PREFIX + message),
        )

        # -- stop condition 10, at turn 0, before any spend ----------------
        try:
            transcript.check_floor()
        except ContextFloorUnmeetable as exc:
            emit(LOG_PREFIX + f"ERROR: {exc}")
            return ExecutorResult(
                success=False,
                exit_code=EXIT_CONTEXT,
                error=str(exc),
                usage=self._usage(
                    accumulator=accumulator,
                    counters=None,
                    stop_reason="context_floor",
                    finish_status=None,
                    mode=mode,
                    files_changed=0,
                    model_version=None,
                ),
            )

        use_streaming = bool(streaming) and bool(
            self.capabilities.get("supports_streaming")
        )
        ctx = HarnessContext(
            client=client,
            sandbox=sandbox,
            transcript=transcript,
            accumulator=accumulator,
            endpoint=endpoint,
            mode=mode,
            max_iterations=max_iterations,
            max_total_tokens=max_total_tokens,
            time_budget_seconds=time_budget,
            max_tool_calls_per_turn=max_calls,
            max_malformed_retries=int(
                self._setting("max_malformed_retries", MAX_MALFORMED_RETRIES)
            ),
            streaming=use_streaming,
            log=emit,
        )

        restore = _install_cancel_handler(ctx, emit)
        try:
            outcome = run_loop(ctx)
        except EndpointFatal as exc:  # defensive: run_loop already catches it
            outcome = _endpoint_outcome(ctx, exc)
        finally:
            restore()

        files_changed = changed_path_count(sandbox.workdir)
        success, exit_code, error = self._verdict(
            outcome, files_changed=files_changed, require_changes=require_changes
        )

        emit(
            LOG_PREFIX
            + (
                f"stop: {_stop_label(outcome)} after {outcome.turn} turns, "
                f"{human_tokens(accumulator.input_tokens)} in / "
                f"{human_tokens(accumulator.output_tokens)} out, "
                f"{human_duration(time.monotonic() - started)}"
            )
        )
        if error:
            emit(LOG_PREFIX + f"result: FAILED ({error})")

        self._maybe_write_transcript(transcript, emit)

        return ExecutorResult(
            success=success,
            exit_code=exit_code,
            stdout="",
            stderr="",
            error=error,
            usage=self._usage(
                accumulator=accumulator,
                counters=ctx.counters,
                stop_reason=outcome.stop_reason,
                finish_status=outcome.finish_status,
                mode=ctx.mode,
                files_changed=files_changed,
                model_version=ctx.model_version,
            ),
        )

    # -- section 3.5's table ------------------------------------------------

    def _verdict(self, outcome, *, files_changed: int, require_changes: bool):
        changed = files_changed > 0
        reason = outcome.stop_reason

        if reason == "finish":
            status = (outcome.finish_status or "success").lower()
            if status == "success":
                if changed or not require_changes:
                    return True, EXIT_OK, None
                # THE MOST OPINIONATED LINE IN THIS DESIGN, and deliberate. In
                # a benchmark a no-op that reports success is the most
                # expensive possible failure: it costs almost nothing, scores
                # as solved, and only the oracle catches it. Red step it is.
                return (
                    False,
                    EXIT_FAILED,
                    "the agent reported success but changed no files",
                )
            return (
                False,
                EXIT_FAILED,
                outcome.finish_summary
                or f"the agent finished with status={status}",
            )

        if reason == "model_stopped_calling_tools":
            if looks_final(outcome.prose) and changed:
                return True, EXIT_OK, None
            if changed:
                return (
                    False,
                    EXIT_FAILED,
                    "the agent stopped calling tools without calling finish",
                )
            return (
                False,
                EXIT_FAILED,
                "the agent stopped without calling finish and changed no files",
            )

        return (
            False,
            STOP_EXIT_CODES.get(reason, EXIT_FAILED),
            outcome.error or f"the harness stopped: {reason}",
        )

    # -- section 5.1's manifest --------------------------------------------

    def _usage(
        self,
        *,
        accumulator,
        counters,
        stop_reason: str,
        finish_status: Optional[str],
        mode: str,
        files_changed: int,
        model_version: Optional[str],
    ) -> Dict[str, Any]:
        endpoint = self.endpoint
        gpu_fraction = self.pricing.get("gpu_fraction")
        try:
            max_concurrency = (
                int(round(1.0 / float(gpu_fraction))) if gpu_fraction else None
            )
        except (TypeError, ValueError, ZeroDivisionError):
            max_concurrency = None

        harness_raw: Dict[str, Any] = {
            "endpoint_id": endpoint.get("id"),
            "endpoint_name": endpoint.get("name"),
            "endpoint_reach": endpoint.get("reach"),
            "endpoint_max_concurrency": max_concurrency,
            "endpoint_probe_age_s": self.capabilities.get("probe_age_seconds"),
            "mode": mode,
            "turns": getattr(counters, "turns", 0),
            "turns_without_usage": accumulator.turns_without_usage,
            "stop_reason": stop_reason,
            "finish_status": finish_status,
            # A COUNT MAP, NOT A LIST — precisely so `raw` cannot grow with the
            # transcript and trip usage._cap_raw's 8 KiB ceiling.
            "tool_calls": dict(getattr(counters, "tool_calls", {}) or {}),
            "tool_errors": getattr(counters, "tool_errors", 0),
            "malformed_responses": getattr(counters, "malformed_responses", 0),
            "context_elisions": getattr(counters, "context_elisions", 0),
            "endpoint_http_errors": getattr(counters, "endpoint_http_errors", 0),
            "probe_drift": bool(getattr(counters, "probe_drift", False)),
            "files_changed": files_changed,
        }

        usage: Dict[str, Any] = {
            "provider": HARNESS_PROVIDER,
            "model": endpoint.get("model"),
            "model_version": model_version,
            "input_tokens": accumulator.input_tokens,
            "output_tokens": accumulator.output_tokens,
            "cache_read_tokens": accumulator.cache_read_tokens,
            # No OpenAI-compatible server exposes it. A null is the honest
            # answer; a zero would be a claim.
            "cache_write_tokens": None,
            # THE HARNESS NEVER COMPUTES DOLLARS. Dollars come from
            # gpu_node_id + usage_pricing server-side, and a harness StepUsage
            # row may NEVER carry cost_source="cli-reported".
            "cost_usd": None,
            "cost_source": HARNESS_COST_SOURCE,
            "determinism": {
                "temperature": self.harness.get("temperature", 0),
                "top_p": self.harness.get("top_p"),
                "seed": self.harness.get("seed"),
            },
            "raw": {"harness": harness_raw},
        }

        reason = accumulator.no_usage_reason()
        usage[SCRAPE_OK_KEY] = reason is None
        usage[SCRAPE_ERROR_KEY] = reason
        return usage

    # -- the debug transcript seam (section 12) ----------------------------

    def _maybe_write_transcript(self, transcript, emit) -> None:
        """Off by default, and NEVER fatal.

        Named as a seam rather than shipped as an artifact: making the
        transcript a first-class artifact means a fifth control-layer channel,
        which is a protocol addition, not a flag.
        """
        if not self.harness.get("debug_transcript"):
            return
        environ = self._environ()
        step = environ.get("LAZYAF_STEP_RUN_ID") or "step"
        home = environ.get("HOME") or environ.get("USERPROFILE") or "."
        target = Path(home) / ".lazyaf" / "harness" / f"{step}.jsonl"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(transcript.as_jsonl(), encoding="utf-8")
        except OSError as exc:
            emit(LOG_PREFIX + f"WARNING: could not write the debug transcript: {exc}")
            return
        emit(LOG_PREFIX + f"harness: debug transcript -> {target}")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _default_client_factory(**kwargs):
    return OpenAICompatClient(**kwargs)


def _endpoint_outcome(ctx, exc):
    from .loop import HarnessOutcome

    return HarnessOutcome("endpoint", ctx.counters.turns, error=ctx.client.scrub(str(exc)))


def _stop_label(outcome) -> str:
    if outcome.stop_reason == "finish":
        return f"finish(status={outcome.finish_status})"
    return outcome.stop_reason


def _install_cancel_handler(ctx, emit):
    """Turn SIGTERM into a COOPERATIVE stop (condition 9).

    ``images/base/control/executor.py`` SIGTERMs the process group, waits 5s,
    then SIGKILLs. Rather than let the wrapper's handler ``sys.exit`` out of
    the middle of a tool call, the harness takes the signal itself, finishes
    the current call, and returns normally — so the wrapper's ``finally``
    writes a usage manifest carrying every token actually spent instead of an
    empty partial. The previous handler is restored on the way out, so the
    wrapper's own handler covers everything outside ``execute()``.
    """
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (AttributeError, ValueError, OSError):
        return lambda: None

    def _handle(signum, _frame):
        ctx.cancelled = True
        emit(
            LOG_PREFIX
            + f"received signal {signum}; stopping after the current tool call"
        )

    try:
        signal.signal(signal.SIGTERM, _handle)
    except (AttributeError, ValueError, OSError):
        return lambda: None

    def _restore():
        try:
            signal.signal(signal.SIGTERM, previous)
        except (AttributeError, ValueError, OSError):
            pass

    return _restore
