"""
Every budget, limit and exit code the harness uses (Milestone 14.2).

NO INLINE LITERALS ANYWHERE ELSE. A budget that is spelled twice is a budget
that drifts, and every number here is either quoted verbatim from
``upcoming/wave8-m14-wiring.md`` section 3.2 or derived from a value that is.

Stdlib only, imports nothing.
"""

# --------------------------------------------------------------------------
# section 3.2 — `constants.py` in full, verbatim from the design
# --------------------------------------------------------------------------

#: Consecutive prose-only turns tolerated before stop condition 5 fires.
NO_TOOL_PATIENCE = 2

#: Consecutive tool ERRORS before stop condition 6. Small models thrash on an
#: identical failing ``apply_patch`` indefinitely; this is the thing that
#: notices.
MAX_CONSECUTIVE_TOOL_ERRORS = 5

#: Consecutive unparseable responses in fallback mode. RESET by any successful
#: parse, so a model that stumbles once every ten turns is not punished.
MAX_MALFORMED_RETRIES = 3

#: Tool calls honoured from one assistant turn. Extras are dropped with a log
#: line rather than silently: a model that asks for 30 reads at once is a
#: model about to blow its own context.
MAX_TOOL_CALLS_PER_TURN = 4

#: Retries per HTTP request on 429 / 5xx / timeout.
MAX_ENDPOINT_RETRIES = 3

#: Full-jitter backoff base, capped by ENDPOINT_RETRY_MAX_SECONDS.
ENDPOINT_RETRY_BASE_SECONDS = 1.5
ENDPOINT_RETRY_MAX_SECONDS = 20.0

#: The commit-plus-push budget the SOFT deadline leaves for the wrapper. The
#: harness stops itself at ``step_timeout - HARNESS_TIME_RESERVE`` so it is
#: still alive to commit, push and write telemetry inside the container
#: watchdog's HARD deadline (which remains the ONE killer — 12.5's rule).
HARNESS_TIME_RESERVE = 60

#: One tool result never exceeds this, head+tail elided.
TOOL_OUTPUT_MAX_BYTES = 8192

#: Default per-``run_shell`` timeout.
TOOL_SHELL_TIMEOUT = 120

#: Longest rendered log line. The SAME value and the SAME rule the 12.5
#: wrapper already applies to claude's stream-json events.
MAX_EVENT_LINE = 2000

#: Messages at the tail of the transcript that elision may never drop.
KEEP_RECENT_TURNS = 6

#: Slack held back from the context window for the model's own overhead
#: (chat template, tool schemas the server re-renders, tokenizer drift).
CONTEXT_RESERVE_FRACTION = 0.15

DEFAULT_MAX_ITERATIONS = 40
DEFAULT_MAX_TOTAL_TOKENS = 400_000

# --------------------------------------------------------------------------
# section 2 / 3.7 — context defaults
# --------------------------------------------------------------------------

#: Used ONLY with a loud log line (section 3.7). Assuming 128k silently is how
#: a step dies at turn 12 with an opaque 400.
DEFAULT_ASSUMED_CONTEXT = 8192

#: Applied at use time when the endpoint declares no ``max_output_tokens``.
DEFAULT_MAX_OUTPUT_TOKENS = 1024

#: Crude first estimate, corrected after turn 1 from the server's own
#: ``usage.prompt_tokens`` (section 3.7).
DEFAULT_CHARS_PER_TOKEN = 4.0

#: Guard rails on the LIVE correction: a server that reports nonsense must not
#: be able to talk the harness into a 100x-wrong budget.
MIN_CHARS_PER_TOKEN = 1.0
MAX_CHARS_PER_TOKEN = 12.0

#: A step whose timeout is under this gets ``time_budget_seconds = timeout//2``
#: and a warning, because ``timeout - 60`` would be zero or negative.
MIN_TIMEOUT_FOR_RESERVE = 2 * HARNESS_TIME_RESERVE

# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

#: Every harness log line carries the 12.5 wrapper's prefix, so the UI, the
#: SCRAPE_FAILED_LOG_MARKER grep and verify_executor are unchanged.
LOG_PREFIX = "[agent] "

#: Tool ARGUMENTS are rendered, elided to this per argument. Tool RESULTS are
#: summarized to size and exit code — dumping them would double the step's log
#: volume and put file contents into ``StepRun.logs`` for no benefit.
MAX_ARG_CHARS = 120

#: The last raw response quoted into the step log when the fallback protocol
#: gives up (section 3.8).
MAX_RAW_RESPONSE_CHARS = 500

#: Model prose emitted per turn (R1: a 20-minute dark step is unacceptable).
MAX_PROSE_CHARS = 600

# --------------------------------------------------------------------------
# exit codes — section 3.5's table, and the ONE place they are spelled
# --------------------------------------------------------------------------

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BUDGET = 3
EXIT_ENDPOINT = 4
EXIT_UNPARSEABLE = 5
EXIT_CONTEXT = 6
EXIT_SIGTERM = 143

#: The stop-reason vocabulary (section 3.2's table). Anything not in here is a
#: bug, and ``STOP_EXIT_CODES`` is exhaustive over it by construction.
STOP_REASONS = (
    "finish",
    "iteration_budget",
    "token_budget",
    "time_budget",
    "model_stopped_calling_tools",
    "tool_error_loop",
    "unparseable",
    "endpoint",
    "cancelled",
    "context_floor",
)

#: stop reason -> exit code. ``finish`` and ``model_stopped_calling_tools``
#: are resolved by section 3.5's change check, so they are not here.
STOP_EXIT_CODES = {
    "iteration_budget": EXIT_BUDGET,
    "token_budget": EXIT_BUDGET,
    "time_budget": EXIT_BUDGET,
    "tool_error_loop": EXIT_FAILED,
    "unparseable": EXIT_UNPARSEABLE,
    "endpoint": EXIT_ENDPOINT,
    "cancelled": EXIT_SIGTERM,
    "context_floor": EXIT_CONTEXT,
}

#: ``UsageManifest.provider`` for every harness step (section 5.1). Constant
#: for this executor: an OpenAI-compatible server is what it is.
HARNESS_PROVIDER = "openai-compatible"

#: The harness NEVER prices. Dollars come from ``gpu_node_id`` +
#: ``usage_pricing`` server-side (section 5.2), and a harness StepUsage row may
#: never carry ``cli-reported``.
HARNESS_COST_SOURCE = "unknown"
