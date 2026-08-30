"""
The harness state machine and its TEN enumerated stop conditions
(Milestone 14.2, design section 3.2).

THIS FILE IS THE ANSWER TO "model output that never terminates". An inference
server is not an agent: nothing bounds it, nothing decides it is done, nothing
notices it has been re-reading the same file for twenty minutes. So the loop
supplies all three, and every exit from it is one of ten named, tested,
logged conditions — four budgets, three loop-detectors, one termination tool,
one signal, and one refusal that fires before a single token is spent.

The load-bearing one is number 4, the SOFT DEADLINE at
``step_timeout - HARNESS_TIME_RESERVE``. It is a deliberate refinement of
12.5's "ONE timeout owner" rule and not a violation of it:
``images/base/control/executor.py`` remains the only component that KILLS
anything. The harness sets a soft deadline strictly inside that hard one and
treats crossing it as an ordinary stop, so it still gets to commit its partial
work, write the usage manifest and exit with a meaningful code — instead of
being SIGKILLed with nothing to show for thirty minutes of GPU time.

Stdlib only.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .client import EndpointFatal, ToolsRejected
from .constants import (
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_TOTAL_TOKENS,
    HARNESS_TIME_RESERVE,
    LOG_PREFIX,
    MAX_CONSECUTIVE_TOOL_ERRORS,
    MAX_EVENT_LINE,
    MAX_MALFORMED_RETRIES,
    MAX_ARG_CHARS,
    MAX_PROSE_CHARS,
    MAX_TOOL_CALLS_PER_TURN,
    MIN_TIMEOUT_FOR_RESERVE,
    NO_TOOL_PATIENCE,
)
from .fallback import Malformed, correction_for, parse_action, system_prompt
from .tools import FINISH_TOOL, TOOL_ORDER, TOOLS, run_tool, tool_schemas

#: How many tools-mode turns may emit a parseable fallback block in
#: ``content`` before the harness stops pretending the server does tools.
DRIFT_TURNS_BEFORE_SWITCH = 2


def soft_deadline_seconds(step_timeout: Optional[int]) -> Optional[int]:
    """``harness.time_budget_seconds`` from the step's HARD timeout.

    THE ONE RULE, so the soft deadline and the watchdog's hard one have
    exactly one source (design section 4.1). The backend computes the value it
    puts on the wire; this function is what it computes, and a contract test
    can point at it rather than at a re-typed subtraction.

    ``HARNESS_TIME_RESERVE`` is the commit-plus-push budget. A step whose
    timeout is under twice that gets half its timeout and the caller warns —
    ``timeout - 60`` on a 90s step is 30s, and on a 45s step it is negative.
    """
    if not step_timeout or step_timeout <= 0:
        return None
    if step_timeout < MIN_TIMEOUT_FOR_RESERVE:
        return max(int(step_timeout) // 2, 1)
    return int(step_timeout) - HARNESS_TIME_RESERVE


def resolve_harness_mode(endpoint: Optional[Dict[str, Any]], harness: Optional[Dict[str, Any]]) -> str:
    """``'tools'`` | ``'text'`` — the ONE function that decides (contract 4.3.7).

    ``agent_config.AgentConfig.harness_mode`` and ``HarnessExecutor`` both
    call it, so the loop shape a step runs can never disagree with the loop
    shape the config reports.

    ``auto`` with ``supports_tools is None`` is a REFUSAL, not a guess. The
    backend already refuses to dispatch an unprobed endpoint, so reaching this
    branch means the wire lied — and defaulting to ``text`` there would
    silently route a tool-capable model down the fallback protocol, which is
    exactly the invisible downgrade R1 exists to forbid.
    """
    mode = ((harness or {}).get("mode") or "auto")
    if not isinstance(mode, str):
        raise ValueError(f"harness.mode must be a string; got {type(mode).__name__}")
    mode = mode.strip().lower()
    if mode in ("tools", "text"):
        return mode
    if mode != "auto":
        raise ValueError(
            f"unknown harness.mode {mode!r}; expected 'auto', 'tools' or 'text'"
        )
    capabilities = (endpoint or {}).get("capabilities") or {}
    supports = capabilities.get("supports_tools")
    if supports is True:
        return "tools"
    if supports is False:
        return "text"
    raise ValueError(
        "harness.mode is 'auto' but the endpoint's capability record does not "
        "say whether it supports tool calling (supports_tools is null). The "
        "backend refuses to dispatch an unprobed endpoint, so this means the "
        "wire lied: probe the endpoint, or pin harness.mode to 'tools' or "
        "'text'."
    )


# --------------------------------------------------------------------------
# counters — exactly the fields of ``raw.harness`` (design section 5.1)
# --------------------------------------------------------------------------

@dataclass
class Counters:
    turns: int = 0
    turns_without_usage: int = 0
    tool_calls: Dict[str, int] = field(default_factory=dict)
    tool_errors: int = 0
    malformed_responses: int = 0
    context_elisions: int = 0
    endpoint_http_errors: int = 0
    probe_drift: bool = False

    def count_call(self, name: str) -> None:
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1


@dataclass
class HarnessOutcome:
    """Why the loop stopped, and everything the executor needs to report it."""

    stop_reason: str
    turn: int = 0
    finish_status: Optional[str] = None
    finish_summary: Optional[str] = None
    prose: str = ""
    error: Optional[str] = None
    raw_response: str = ""


# --------------------------------------------------------------------------
# context
# --------------------------------------------------------------------------

class HarnessContext:
    """Everything one harness run needs, and the only mutable state it has."""

    def __init__(
        self,
        *,
        client,
        sandbox,
        transcript,
        accumulator,
        endpoint: Dict[str, Any],
        mode: str,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        max_total_tokens: int = DEFAULT_MAX_TOTAL_TOKENS,
        time_budget_seconds: Optional[float] = None,
        max_tool_calls_per_turn: int = MAX_TOOL_CALLS_PER_TURN,
        max_malformed_retries: int = MAX_MALFORMED_RETRIES,
        streaming: bool = False,
        log=None,
        clock=time.monotonic,
    ):
        self.client = client
        self.sandbox = sandbox
        self.tx = transcript
        self.accumulator = accumulator
        self.endpoint = endpoint or {}
        self.mode = mode
        self.max_iterations = max(int(max_iterations or DEFAULT_MAX_ITERATIONS), 1)
        self.max_total_tokens = int(max_total_tokens or DEFAULT_MAX_TOTAL_TOKENS)
        self.max_tool_calls_per_turn = max(int(max_tool_calls_per_turn or MAX_TOOL_CALLS_PER_TURN), 1)
        self.max_malformed_retries = int(max_malformed_retries or MAX_MALFORMED_RETRIES)
        self.streaming = bool(streaming)
        self.counters = Counters()
        self.cancelled = False
        self.model_version: Optional[str] = None
        self.drift_turns = 0
        self._log_callback = log
        self._clock = clock
        self.started = clock()
        self.time_budget_seconds = (
            float(time_budget_seconds) if time_budget_seconds else None
        )

    # -- logging -----------------------------------------------------------

    def log(self, message: str) -> None:
        """ONE line per event, scrubbed, truncated at ``MAX_EVENT_LINE``.

        The same rule and the same constant the 12.5 wrapper already applies
        to claude's stream-json events, so the UI, the SCRAPE_FAILED grep and
        ``verify_executor`` are unchanged.
        """
        if self._log_callback is None:
            return
        text = self.client.scrub(message)
        text = text.replace("\r", " ").replace("\n", " ")
        if len(text) > MAX_EVENT_LINE:
            text = text[:MAX_EVENT_LINE] + " ..."
        self._log_callback(LOG_PREFIX + text)

    # -- budgets -----------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return self._clock() - self.started

    def past_deadline(self) -> bool:
        if not self.time_budget_seconds:
            return False
        return self.elapsed >= self.time_budget_seconds

    @property
    def tokens_spent(self) -> int:
        return self.accumulator.total_tokens

    # -- one turn ----------------------------------------------------------

    def tool_schemas_or_none(self):
        return tool_schemas() if self.mode == "tools" else None

    def chat(self):
        return self.client.chat(
            self.tx.messages,
            tools=self.tool_schemas_or_none(),
            stream=self.streaming,
        )

    def record_usage(self, response) -> None:
        """EVERY turn, reporting or not (design section 5.1).

        A turn whose server sent no ``usage`` block is COUNTED as such rather
        than skipped, because "we summed 9 of 11 turns" and "we summed 11 of
        11" are different claims and only one of them is true.
        """
        self.counters.turns += 1
        self.accumulator.add(response.usage)
        if not response.reports_usage:
            self.counters.turns_without_usage += 1
        self.counters.endpoint_http_errors = getattr(
            self.client, "http_errors", self.counters.endpoint_http_errors
        )
        self.tx.observe_usage(response.request_chars, response.prompt_tokens)
        served = response.model
        if (
            isinstance(served, str)
            and served
            and served != self.endpoint.get("model")
        ):
            self.model_version = served

    def log_turn(self, turn: int, response) -> None:
        prompt = response.prompt_tokens
        completion = response.completion_tokens
        self.log(
            f"turn {turn}/{self.max_iterations} "
            f"in={prompt if prompt is not None else '?'} "
            f"out={completion if completion is not None else '?'} "
            f"(total in={human_tokens(self.accumulator.input_tokens)} "
            f"out={human_tokens(self.accumulator.output_tokens)}) "
            f"{response.elapsed_seconds:.1f}s"
        )

    def switch_to_text(self, reason: str) -> None:
        """Runtime demotion to the fallback protocol, LOUDLY."""
        if self.mode == "text":
            return
        self.mode = "text"
        self.counters.probe_drift = True
        workdir = str(self.sandbox.workdir)
        self.tx.entries[0].content = system_prompt("text", workdir, self.max_iterations)
        self.log(
            f"WARNING: switching to the no-tools fallback protocol: {reason}. "
            "The endpoint's stored supports_tools will be corrected by the "
            "platform from this step's outcome."
        )

    # -- interpretation ----------------------------------------------------

    def interpret(self, response):
        """``(actions, prose, malformed)``.

        THE BRIDGE FOR A PROBE THAT LIED lives here: after a TOOLS-mode turn
        that returns no ``tool_calls``, the fallback parser is run over
        ``message.content`` BEFORE the turn is treated as prose. A server that
        emits perfectly good calls inside ``content`` therefore still works,
        with ``probe_drift`` recorded — and two such turns switch the harness
        to fallback mode for the remainder rather than paying the tools-schema
        tax on every further request.
        """
        prose = response.content or ""
        if self.mode == "tools":
            if response.tool_calls:
                return list(response.tool_calls), prose, None
            parsed = parse_action(prose)
            if isinstance(parsed, Malformed):
                return [], prose, None  # ordinary prose-only turn
            self.counters.probe_drift = True
            self.drift_turns += 1
            self.log(
                "note: the endpoint advertised tool calling but emitted a "
                "```lazyaf block in content; executing it and recording "
                "probe_drift"
            )
            if self.drift_turns >= DRIFT_TURNS_BEFORE_SWITCH:
                self.switch_to_text(
                    f"{self.drift_turns} turns emitted actions as text instead "
                    "of tool_calls"
                )
            return [parsed], prose, None

        parsed = parse_action(prose)
        if isinstance(parsed, Malformed):
            return [], prose, parsed
        return [parsed], prose, None


# --------------------------------------------------------------------------
# rendering helpers
# --------------------------------------------------------------------------

def human_tokens(value: Optional[int]) -> str:
    if value is None:
        return "?"
    if value < 1000:
        return str(value)
    return f"{value / 1000:.1f}k"


def human_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60:02d}s"


def _rendered_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults-filled arguments for the LOG LINE.

    The fallback parser fills defaults before the loop sees a call and the
    tools path does not, so without this the same action logs differently in
    the two modes — and "the two modes produce the same log lines" is what
    lets an experiment vary only ``harness.mode`` and attribute the
    difference.
    """
    from .tools import validate_args

    spec = TOOLS.get(name)
    if spec is None:
        return args or {}
    clean, reason = validate_args(spec, args)
    return args or {} if reason else clean


def render_args(args: Dict[str, Any]) -> str:
    parts = []
    for key, value in (args or {}).items():
        if value is None:
            continue
        text = value if isinstance(value, str) else repr(value)
        text = " ".join(str(text).split())
        if len(text) > MAX_ARG_CHARS:
            text = text[:MAX_ARG_CHARS] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def run_loop(ctx: HarnessContext) -> HarnessOutcome:
    """Drive the model until exactly one of the ten stop conditions fires."""
    consecutive_textonly = 0
    consecutive_tool_errors = 0
    consecutive_malformed = 0
    turn = 0

    for turn in range(1, ctx.max_iterations + 1):
        # 9 — SIGTERM. Checked first so a cancelled step never opens a new
        # request it cannot finish inside the watchdog's grace period.
        if ctx.cancelled:
            return HarnessOutcome("cancelled", turn, error="cancelled")
        # 4 — the soft deadline.
        if ctx.past_deadline():
            return HarnessOutcome(
                "time_budget",
                turn,
                error=(
                    f"the harness time budget ({int(ctx.time_budget_seconds)}s) "
                    f"was spent after {turn - 1} turns"
                ),
            )
        # 3 — the token budget.
        if ctx.tokens_spent >= ctx.max_total_tokens:
            return HarnessOutcome(
                "token_budget",
                turn,
                error=(
                    f"the token budget ({ctx.max_total_tokens:,}) was spent "
                    f"after {turn - 1} turns ({ctx.tokens_spent:,} tokens)"
                ),
            )

        elided = ctx.tx.fit()  # BEFORE sending. Never after.
        if elided:
            ctx.counters.context_elisions += 1

        try:
            response = ctx.chat()
        except ToolsRejected as exc:
            if ctx.mode == "tools":
                ctx.switch_to_text(
                    f"the endpoint rejected the tools parameter ({exc.reason})"
                )
                continue
            return HarnessOutcome(
                "endpoint", turn, error=ctx.client.scrub(exc.reason)
            )
        except EndpointFatal as exc:
            ctx.counters.endpoint_http_errors = getattr(
                ctx.client, "http_errors", ctx.counters.endpoint_http_errors
            )
            return HarnessOutcome(
                "endpoint", turn, error=ctx.client.scrub(exc.reason)
            )

        ctx.record_usage(response)
        ctx.log_turn(turn, response)

        calls, prose, malformed = ctx.interpret(response)

        # 7 — unparseable responses (fallback mode).
        if malformed is not None:
            ctx.counters.malformed_responses += 1
            consecutive_malformed += 1
            if prose.strip():
                ctx.log(f"  text: {_clip(prose)}")
            if consecutive_malformed > ctx.max_malformed_retries:
                ctx.log(
                    "  last raw response: "
                    + _clip(malformed.raw, 500)
                )
                return HarnessOutcome(
                    "unparseable",
                    turn,
                    error=(
                        f"endpoint {ctx.endpoint.get('name', '?')} (model "
                        f"{ctx.endpoint.get('model', '?')}) produced no "
                        f"parseable action in {consecutive_malformed} "
                        f"consecutive turns; last reason: {malformed.reason}"
                    ),
                    raw_response=malformed.raw[-500:],
                    prose=prose,
                )
            reason_phrase, example = correction_for(malformed)
            ctx.log(f"  unparseable ({malformed.reason}); correcting the model")
            ctx.tx.append_assistant(prose)
            ctx.tx.append_correction(reason_phrase, example)
            continue
        consecutive_malformed = 0

        if prose.strip():
            ctx.log(f"  text: {_clip(prose)}")

        # 5 — the model stopped calling tools.
        if not calls:
            consecutive_textonly += 1
            if consecutive_textonly >= NO_TOOL_PATIENCE:
                return HarnessOutcome(
                    "model_stopped_calling_tools", turn, prose=prose
                )
            ctx.tx.append_assistant(prose)
            ctx.tx.append_nudge()
            continue
        consecutive_textonly = 0

        honored = calls[: ctx.max_tool_calls_per_turn]
        if len(calls) > len(honored):
            ctx.log(
                f"  note: {len(calls)} tool calls in one turn; honoring the "
                f"first {len(honored)}"
            )
        if ctx.mode == "tools":
            ctx.tx.append_assistant(prose, honored)
        else:
            ctx.tx.append_assistant(prose)

        for call in honored:
            args, argument_error = call.arguments()
            ctx.counters.count_call(call.name)

            # 1 — finish. Termination is a FACT, never a parsed phrase.
            if call.name == FINISH_TOOL:
                if argument_error or not isinstance(args, dict):
                    finish_args = {}
                else:
                    finish_args = args
                return HarnessOutcome(
                    "finish",
                    turn,
                    finish_status=str(finish_args.get("status") or "success"),
                    finish_summary=str(finish_args.get("summary") or ""),
                    prose=prose,
                )

            if argument_error:
                from .tools import ToolResult

                result = ToolResult(
                    f"{call.name}: {argument_error}",
                    is_error=True,
                    summary=argument_error,
                )
            else:
                result = run_tool(ctx.sandbox, call.name, args)

            if result.is_error:
                ctx.counters.tool_errors += 1
                consecutive_tool_errors += 1
                ctx.log(f"  tool ERROR {call.name}: {result.summary}")
            else:
                consecutive_tool_errors = 0
                ctx.log(
                    f"  tool {call.name}({render_args(_rendered_args(call.name, args))})"
                    f" -> {result.summary}"
                )

            ctx.tx.append_tool_result(call, result, ctx.mode)

            # 6 — the tool-error loop. Small models retry an identical failing
            # apply_patch indefinitely; this is the thing that notices.
            if consecutive_tool_errors >= MAX_CONSECUTIVE_TOOL_ERRORS:
                return HarnessOutcome(
                    "tool_error_loop",
                    turn,
                    error=(
                        f"{consecutive_tool_errors} consecutive tool errors; "
                        f"last: {result.summary}"
                    ),
                    prose=prose,
                )
            if ctx.cancelled:
                return HarnessOutcome("cancelled", turn, error="cancelled")

    # 2 — the iteration budget.
    return HarnessOutcome(
        "iteration_budget",
        turn or ctx.max_iterations,
        error=(
            f"the iteration budget ({ctx.max_iterations} turns) was spent "
            "without the agent calling finish"
        ),
    )


def _clip(text: str, limit: int = MAX_PROSE_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + " ..."


__all__ = [
    "Counters",
    "HarnessContext",
    "HarnessOutcome",
    "DRIFT_TURNS_BEFORE_SWITCH",
    "human_duration",
    "human_tokens",
    "resolve_harness_mode",
    "run_loop",
    "soft_deadline_seconds",
]
