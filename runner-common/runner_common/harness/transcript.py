"""
The message list, its token estimate, and its elision policy
(Milestone 14.2, design section 3.7).

BUDGET AND ELIDE. NEVER SUMMARIZE. REFUSE ONLY WHEN THE FLOOR CANNOT BE MET.

Summarization is rejected on three counts and the rejection is the design, not
an omission: it costs an extra generation on the slowest, scarcest resource in
the system (the local GPU); it burns output tokens against the very budget it
is trying to protect; and a small model summarizing its own transcript is
precisely the model least able to do it faithfully. An honest elision marker
is cheaper and does not fabricate.

The token estimate starts crude (``len(text) // 4``) and is CORRECTED after
every response from the server's own ``usage.prompt_tokens``. That correction
is what turns a heuristic into a feedback loop after turn 1, and it costs
nothing.

Stdlib only.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .constants import (
    CONTEXT_RESERVE_FRACTION,
    DEFAULT_ASSUMED_CONTEXT,
    DEFAULT_CHARS_PER_TOKEN,
    DEFAULT_MAX_OUTPUT_TOKENS,
    KEEP_RECENT_TURNS,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    TOOL_OUTPUT_MAX_BYTES,
)

#: Kinds that elision may NEVER drop.
KIND_SYSTEM = "system"
KIND_TASK = "task"
KIND_MARKER = "marker"


@dataclass
class Entry:
    """One transcript message plus the bookkeeping elision needs."""

    role: str
    content: str = ""
    kind: str = "assistant"
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    def as_message(self) -> Dict[str, Any]:
        message: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
            # An assistant turn that carries tool_calls has no text on most
            # servers; sending "" rather than omitting content keeps the shape
            # uniform and is accepted everywhere we have run.
        if self.tool_call_id:
            message["tool_call_id"] = self.tool_call_id
        if self.name and self.role == "tool":
            message["name"] = self.name
        return message

    @property
    def chars(self) -> int:
        total = len(self.content or "")
        for call in self.tool_calls or []:
            function = (call or {}).get("function") or {}
            total += len(str(function.get("name") or ""))
            total += len(str(function.get("arguments") or ""))
        return total


@dataclass
class ElisionRecord:
    messages: int = 0
    calls: int = 0
    results: int = 0
    events: int = 0

    def marker_text(self) -> str:
        return (
            f"[{self.messages} earlier messages elided to fit the context "
            f"window: {self.calls} tool calls, {self.results} results. "
            "Re-read files if you need them.]"
        )


class ContextFloorUnmeetable(Exception):
    """Stop condition 10: the prompt cannot fit BEFORE any token is spent.

    Raised at construction, at turn 0, which is the whole point: discovering
    this at turn 12 means the operator paid for eleven turns to learn it.
    """

    def __init__(self, estimated: int, window: int, reserve: int, endpoint_name: str):
        self.estimated = estimated
        self.window = window
        self.reserve = reserve
        self.endpoint_name = endpoint_name
        super().__init__(
            f"prompt (est {estimated:,} tokens) exceeds endpoint "
            f"{endpoint_name}'s context window ({window:,}); use a larger "
            "model, trim the spec context, or set context_window on the "
            "endpoint"
        )


class Transcript:
    """The message list the harness sends, and the only thing that shrinks it."""

    def __init__(
        self,
        system: str,
        task: str,
        *,
        context_window: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
        endpoint_name: str = "endpoint",
        log=None,
    ):
        self.endpoint_name = endpoint_name
        self.context_window = int(context_window or DEFAULT_ASSUMED_CONTEXT)
        self.context_assumed = context_window is None
        self.max_output_tokens = int(max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN
        self.chars_per_token_observed = False
        self._log = log or (lambda message: None)
        self.elided = ElisionRecord()
        self.entries: List[Entry] = [
            Entry(role="system", content=system, kind=KIND_SYSTEM),
            Entry(role="user", content=task, kind=KIND_TASK),
        ]

    # -- budgets -----------------------------------------------------------

    @property
    def working_budget(self) -> int:
        """Context window minus the reply we asked for minus stated slack."""
        return int(
            self.context_window
            - self.max_output_tokens
            - int(CONTEXT_RESERVE_FRACTION * self.context_window)
        )

    def estimate_chars(self, chars: int) -> int:
        ratio = self.chars_per_token or DEFAULT_CHARS_PER_TOKEN
        return int(chars / ratio) + 1 if chars else 0

    def estimate(self) -> int:
        return self.estimate_chars(sum(entry.chars for entry in self.entries))

    def check_floor(self) -> None:
        """Turn-0 refusal (stop condition 10). Call BEFORE the first request."""
        floor = self.estimate_chars(
            sum(
                entry.chars
                for entry in self.entries
                if entry.kind in (KIND_SYSTEM, KIND_TASK)
            )
        )
        if floor > self.working_budget:
            raise ContextFloorUnmeetable(
                floor,
                self.context_window,
                self.working_budget,
                self.endpoint_name,
            )

    def observe_usage(self, request_chars: int, prompt_tokens: Optional[int]) -> None:
        """Correct the chars-per-token ratio from the server's own count.

        Clamped: a server that reports nonsense must not be able to talk the
        harness into a 100x-wrong budget, and a clamp that fires is still a
        better estimate than the constant it replaced.
        """
        if not prompt_tokens or prompt_tokens <= 0 or request_chars <= 0:
            return
        ratio = request_chars / float(prompt_tokens)
        ratio = max(MIN_CHARS_PER_TOKEN, min(MAX_CHARS_PER_TOKEN, ratio))
        self.chars_per_token = ratio
        self.chars_per_token_observed = True

    # -- appending ---------------------------------------------------------

    @property
    def messages(self) -> List[Dict[str, Any]]:
        return [entry.as_message() for entry in self.entries]

    def append_assistant(self, content: str, tool_calls=None) -> None:
        calls = None
        if tool_calls:
            calls = [
                {
                    "id": call.id or f"call_{index}",
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_raw or "{}",
                    },
                }
                for index, call in enumerate(tool_calls)
            ]
        self.entries.append(
            Entry(
                role="assistant",
                content=content or "",
                kind="assistant",
                tool_calls=calls,
            )
        )

    def tool_result_char_cap(self) -> int:
        """A single tool result never takes more than half the working budget.

        A tool result is ALWAYS truncatable; the transcript is not.
        """
        budget_chars = int(max(self.working_budget, 1) * self.chars_per_token)
        return max(min(TOOL_OUTPUT_MAX_BYTES, budget_chars // 2), 256)

    def append_tool_result(self, call, result, mode: str) -> None:
        """Feed one result back in whichever shape this mode can carry.

        The two modes produce the SAME observable transcript shape and the
        same log lines, which is what lets an experiment vary only
        ``harness.mode`` and attribute the difference to the loop shape.
        """
        from .tools import cap_output

        body = cap_output(result.text, self.tool_result_char_cap())
        if mode == "tools" and call.id is not None:
            self.entries.append(
                Entry(
                    role="tool",
                    content=body,
                    kind="tool_result",
                    tool_call_id=call.id,
                    name=call.name,
                )
            )
            return
        header = (
            f"TOOL ERROR {call.name}: " if result.is_error
            else f"TOOL RESULT {call.name} (ok)\n"
        )
        self.entries.append(
            Entry(role="user", content=header + body, kind="tool_result")
        )

    def append_correction(self, reason: str, example: str) -> None:
        self.entries.append(
            Entry(
                role="user",
                content=(
                    f"Your last reply could not be used: {reason}. {example}"
                ),
                kind="correction",
            )
        )

    def append_nudge(self) -> None:
        self.entries.append(
            Entry(
                role="user",
                content=(
                    "You did not take an action. Describing an edit does not "
                    "make it. Take the next action now, or call finish with a "
                    "status and a summary."
                ),
                kind="nudge",
            )
        )

    # -- elision -----------------------------------------------------------

    def fit(self) -> Optional[Dict[str, int]]:
        """Drop from the MIDDLE until the estimate fits. Runs before EVERY
        request — never after, because after is when the server has already
        rejected the turn.

        Returns the before/after estimate when it elided, else None.
        """
        budget = self.working_budget
        before = self.estimate()
        if before <= budget:
            return None

        dropped_messages = 0
        dropped_calls = 0
        dropped_results = 0

        while self.estimate() > budget:
            block = self._next_droppable_block()
            if not block:
                break
            for index in reversed(block):
                entry = self.entries.pop(index)
                dropped_messages += 1
                dropped_calls += len(entry.tool_calls or [])
                if entry.kind == "tool_result":
                    dropped_results += 1

        if not dropped_messages:
            # Nothing left to drop. The caller still sends: the server's own
            # 400 is a truer answer than our estimate, and refusing here would
            # kill a step our estimate merely mis-measured.
            return None

        self.elided.messages += dropped_messages
        self.elided.calls += dropped_calls
        self.elided.results += dropped_results
        self.elided.events += 1
        self._install_marker()

        after = self.estimate()
        self._log(
            f"context: elided {dropped_messages} earlier messages "
            f"(est {before:,} -> {after:,} tokens)"
        )
        return {"dropped": dropped_messages, "before": before, "after": after}

    def _marker_index(self) -> Optional[int]:
        for index, entry in enumerate(self.entries):
            if entry.kind == KIND_MARKER:
                return index
        return None

    def _install_marker(self) -> None:
        text = self.elided.marker_text()
        index = self._marker_index()
        if index is not None:
            self.entries[index].content = text
            return
        self.entries.insert(2, Entry(role="user", content=text, kind=KIND_MARKER))

    def _next_droppable_block(self) -> List[int]:
        """Indices of the oldest droppable block, oldest first.

        A BLOCK, not a message: in tools mode an assistant message carrying
        ``tool_calls`` MUST be followed by a ``tool`` message per call, or the
        server rejects the request. Dropping whole blocks is what keeps the
        elided transcript legal as well as short.
        """
        protected_head = 2
        marker = self._marker_index()
        if marker is not None:
            protected_head = max(protected_head, marker + 1)
        protected_tail = max(len(self.entries) - KEEP_RECENT_TURNS, protected_head)
        if protected_tail <= protected_head:
            return []

        start = protected_head
        end = start + 1
        while end < protected_tail and self.entries[end].role != "assistant":
            end += 1
        # `start` itself may be a non-assistant leftover; either way the span
        # [start, end) is one contiguous, self-consistent group.
        return list(range(start, end))

    # -- debug transcript --------------------------------------------------

    def as_jsonl(self) -> str:
        return "\n".join(json.dumps(entry.as_message()) for entry in self.entries)
