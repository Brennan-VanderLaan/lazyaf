"""THE definition of "what a leaked credential looks like" for all of LazyAF.

This file used to live at ``.github/scripts/secret_patterns.py``. It moved
because the BACKEND now has to import it (the bug-report redactor) and
``backend/Dockerfile`` copies only ``app/`` into the image - ``.github/`` is
not there at runtime. The direction of the move was forced by that one line
of the Dockerfile. ``.github/scripts/secret_patterns.py`` is now a shim that
loads this module by path, so both CI scanners are unchanged.

HARD RULE FOR WHOEVER EDITS THIS NEXT: **the only import in this module is
``re``.** Three consumers load it outside a backend environment - the bare
``python3`` of a GitHub runner (``scan_repo_secrets.py``), the bare
``python3`` of a dogfood step container (the leak gate in
``.lazyaf/pipelines/test-suite.yaml``), and the backend itself. A pydantic
import here would break two of the three, silently, at the exact moment the
gate matters most.

--------------------------------------------------------------------------
TWO AUDIENCES, TWO PATTERN SETS, ONE FILE
--------------------------------------------------------------------------
A blocking CI gate and a log redactor want different tuning, and pretending
otherwise is how the third copy of this table got written.

* A false positive in the **gate** blocks a build until a human edits an
  allowlist. It must be near-zero or the gate gets switched off.
* A false positive in the **redactor** costs one blacked-out word in a bug
  report. Over-matching is close to free; under-matching publishes a key.

So the table is split by audience rather than duplicated by copy-paste:

    PATTERNS                 the gate set. What find_secrets() uses, which
                             is what scan_repo_secrets.py and
                             scan_image_secrets.py enforce.
    REDACTION_ONLY_PATTERNS  shapes that are correct for a redactor and too
                             loose for a gate.
    REDACT_PATTERNS          PATTERNS + REDACTION_ONLY_PATTERNS. What the
                             Redactor uses.
    BLOCK_PATTERNS           multi-line shapes (PEM). Redactor only; see the
                             comment on that table for why a gate wants the
                             header-only rule and a redactor must not use it.

The split is not a judgement call. It was measured against this repo's
tracked files before it was written down (2026-09-01, 1,300+ tracked files):

    pattern                          hits   verdict
    sk-[A-Za-z0-9_-]{8,}               32   gate-breaking. 28 of the 32 are
                                            `sk-ant-api03-` in prose and
                                            `sk-api-demo` in docs/.
    bearer\\s+<8 or more>               12   gate-breaking. All 12 are test
                                            fixtures ("Bearer step-token").
    <scheme>://user:pass@               3   gate-breaking. All 3 are
                                            documentation examples.
    eyJ..eyJ..sig  (JWT)                0   safe for the gate. Added.
    xox[baprs]-...  (Slack)             0   safe for the gate. Added.
    github_pat_ widened 50 -> 40        0   safe for the gate. Widened.

Rerun that measurement (it is `find_secrets` over `git ls-files`) before
promoting anything from REDACTION_ONLY_PATTERNS into PATTERNS.

--------------------------------------------------------------------------
DESIGN NOTES CARRIED OVER FROM THE ORIGINAL FILE, STILL TRUE
--------------------------------------------------------------------------
* The patterns target LIVE key FORMATS, not the word "key". Grepping for
  ``API_KEY`` would flag every legitimate mention of a variable name in the
  code base and the gate would be turned off within a week. Matching the
  provider's actual token shape keeps the false-positive rate near zero,
  which is the only way a blocking gate survives.

* ``ALLOWLIST`` holds EXACT STRING VALUES, never regexes and never file
  paths. LazyAF's test suite deliberately contains key-shaped strings: they
  are the sentinels the containment tests assert never reach a log, a step
  container's environment, or an API response. Allowlisting by exact value
  means those specific fakes pass while a real key of the same shape sitting
  on the next line still fails the build. An allowlist of file paths would
  have created a hole the size of ``tdd/``.

* Every entry in ``ALLOWLIST`` must be a value that is obviously fake to a
  human reader. If you ever find yourself wanting to add something that
  looks plausibly real, that is the gate working; rotate the key instead.

* **THE ALLOWLIST IS A PROPERTY OF THE SOURCE TREE, NOT OF RUNTIME DATA.**
  ``find_secrets`` honours it; the Redactor does not. "This fake value is
  checked in on purpose" and "this value is safe to publish in a GitHub
  issue" are different claims, and conflating them is exactly how an
  allowlist turns into a leak: someone adds a value to make CI green and
  silently disables runtime redaction for every string that matches it.

--------------------------------------------------------------------------
COMPOSABILITY CONSTRAINT (load-bearing, tested)
--------------------------------------------------------------------------
The Redactor concatenates every ``.pattern`` string in these tables into ONE
alternation so a megabyte-scale bundle member is scanned in a single pass.
That only works if no pattern carries a global inline flag: ``(?i)`` at
position 0 of a sub-expression is a hard error inside an alternation on
Python 3.11+. So every case-insensitive or dot-all rule here is written with
a SCOPED group - ``(?i:...)``, ``(?s:...)`` - and never with ``re.I`` /
``re.S`` compile flags or a leading ``(?i)``. There is a test that asserts
the compile flags are clean and that the combined alternation compiles.
"""

import re

# --- Live key formats: THE GATE SET -----------------------------------------
#
# Each entry is (label, compiled pattern). Labels are printed on failure so
# the operator knows which provider to go rotate. Measured false-positive
# count against the tracked tree: zero.
PATTERNS = [
    # Anthropic. Real keys are `sk-ant-api03-` + ~95 chars of base64url.
    # The generic `sk-ant-` + 12 rule is intentionally wider than the real
    # format so a future key prefix (api04, admin keys, ...) is still caught.
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}")),
    # OpenAI classic (`sk-` + 48) and project keys (`sk-proj-` + long tail).
    # `sk-` alone is too common in ordinary prose, hence the length floor.
    # The FAR wider `sk-` + 8 rule that the model-endpoint scrubber has always
    # used lives in REDACTION_ONLY_PATTERNS: it hits 32 times in this repo.
    ("openai", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    # Google / Gemini. `AIza` + exactly 35 chars is the documented shape.
    ("google", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # GitHub tokens. These would be the worst thing to bake into a published
    # image, since the image is published BY GitHub.
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    # Widened 50 -> 40 to match the bound the packaging test had been using
    # independently. Measured: still zero hits on this tree.
    ("github", re.compile(r"github_pat_[A-Za-z0-9_]{40,}")),
    # AWS access key ids, which travel with a secret and are worth catching
    # even though LazyAF does not use AWS today.
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Slack. Arrived from the packaging test's private copy of this table,
    # which is the only place it had ever existed. Zero hits, so it is safe
    # in the gate and there is no longer a reason for that copy to exist.
    ("slack", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}")),
    # JSON Web Tokens. LazyAF MINTS these - the per-step control-layer token
    # and the per-runner token upgrade are both JWTs signed with
    # LAZYAF_STEP_AUTH_SECRET / LAZYAF_RUNNER_AUTH_SECRET - so a step log or
    # a runner log carrying one is a live credential, not a curiosity. Three
    # base64url segments; the 8-char floor per segment keeps it off prose.
    # Segments are BOUNDED at 4096. `{8,}` followed by a required `\.` is a
    # backtracking trap: on `eyJ` + a long dot-free base64url run the first
    # segment eats to the end of the line and then walks all the way back
    # looking for a dot. Once per `eyJ` occurrence that is O(n); on text
    # containing many of them it is O(n^2). No real JWT segment is 4 KB.
    (
        "jwt",
        re.compile(r"eyJ[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}\.[A-Za-z0-9_\-]{8,4096}"),
    ),
    # Private keys of any flavour. HEADER ONLY, and that is correct HERE: a
    # gate only has to notice. The redactor must not use this rule - see
    # BLOCK_PATTERNS.
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z0-9]{1,12} )?PRIVATE KEY-----")),
]


# --- Shapes the REDACTOR adds, and the gate must not ------------------------
#
# Every entry here was measured to break the gate on this repo's own tracked
# files (counts in the module docstring). They stay because a bug-report
# bundle is a different risk: over-redacting a documentation example costs
# nothing, and `Bearer <token>` in a step log is a live credential.
REDACTION_ONLY_PATTERNS = [
    # The shape `app.services.model_endpoints.secrets.scrub_secrets` has
    # always used. Deliberately much wider than the gate's `sk-` + 32: an
    # OpenAI-compatible server's 401 body echoes back whatever it was given,
    # including short local-vLLM keys that look nothing like an OpenAI key.
    ("openai-wide", re.compile(r"sk-[A-Za-z0-9_\-]{8,}")),
    # `Authorization: Bearer <x>` in any casing.
    #
    # THERE IS NO LENGTH FLOOR, and that is a correction rather than an
    # oversight. The first draft required 8+ characters and immediately broke
    # a standing assertion in tdd/unit/services/test_endpoint_probe.py: a
    # probe error reading `503 from Bearer abc.def` stopped being scrubbed,
    # because the token is seven characters. Anything after the literal word
    # `bearer` IS the credential regardless of how short it is, and the old
    # `\S+` rule this replaces had no floor either. Losing coverage while
    # "tightening" a redactor is the exact failure this suite exists to catch.
    #
    # The character class is still narrower than `\S+` - it excludes the
    # quote, comma and brace that made `\S+` swallow the rest of a JSON line
    # and mangle everything a human then tried to read - but it keeps
    # `+ / = ~` so a base64 or base64url token is matched whole.
    #
    # KNOWN OVER-MATCH, accepted: the English phrase "bearer authentication"
    # redacts to a placeholder. A digit-or-length guard is not worth it,
    # because `Bearer step-token` - no digits, ten characters - is a real
    # credential shape in this codebase (tdd/integration/api/*).
    #
    # The scheme word is CONSUMED, so `Authorization: Bearer abc12345` becomes
    # `Authorization: [REDACTED:bearer:...]` rather than `Bearer [REDACTED...]`.
    # Keeping the word would need a fixed-width lookbehind per whitespace
    # spelling; the header NAME is the diagnostic and it survives either way.
    ("bearer", re.compile(r"(?i:bearer)\s+[A-Za-z0-9._\-+/=~]{1,4096}")),
    # `https://user:password@host` - the shape a git remote takes when a PAT
    # is embedded in the URL, which is exactly how LazyAF's own workspace
    # clone helper would carry one. Only the credential half is matched; the
    # host survives, because the host is the diagnostic.
    #
    # THE SCHEME BOUND `{0,15}` IS NOT COSMETIC. Written as `[a-z0-9+.\-]*`
    # this pattern is quadratic: at EVERY lowercase character it eats the
    # rest of the line and then backtracks the whole way looking for `://`.
    # A one-megabyte single-line log - which is what a JSON body dumped into
    # a step log looks like - took minutes. A redactor that stalls for
    # minutes is a redactor somebody switches off. The longest scheme anyone
    # writes is about eight characters.
    ("git-url-auth", re.compile(r"[a-z][a-z0-9+.\-]{0,15}://[^/\s:@]{1,256}:[^/\s@]{1,256}@")),
    # `Authorization: Basic <base64(user:pass)>`. Guarded harder than
    # `bearer` because "basic authentication" is a phrase people write on
    # purpose: the payload must be 20+ characters AND contain a digit or a
    # base64-only symbol, which "authentication" and "understanding" do not.
    # Both quantifiers bounded, for the reason spelled out on `git-url-auth`:
    # the lookahead's `*` followed by a required class is the same trap.
    (
        "basic-auth",
        re.compile(r"(?i:basic)\s+(?=[A-Za-z0-9+/]{0,512}[0-9+/])[A-Za-z0-9+/]{20,4096}={0,2}"),
    ),
]

#: What the Redactor scans with: the gate set plus the loose shapes.
REDACT_PATTERNS = PATTERNS + REDACTION_ONLY_PATTERNS


# --- Multi-line shapes: REDACTOR ONLY ---------------------------------------
#
# A header-only PEM rule is correct for a gate and CATASTROPHIC in a
# redactor: it would blank the `-----BEGIN ...-----` line and ship the entire
# base64 body of the key. That is not a hypothetical - it is what you get for
# free if you point a redactor at PATTERNS and call it done.
#
# These are matched as BLOCKS and are listed FIRST in the Redactor's combined
# alternation, so at the offset of a `-----BEGIN` the block rule wins over
# the header-only rule in PATTERNS (Python alternation is leftmost-first, and
# at equal offsets the earlier alternative wins).
#
# Both bounds are FINITE on purpose. `.*?` unbounded across a 5 MB bundle
# member with a stray BEGIN and no END is O(n) work per header; bounding it
# keeps a malformed input from turning into a visible stall.
BLOCK_PATTERNS = [
    # The well-formed case: header through footer, inclusive.
    (
        "private-key",
        re.compile(
            r"(?s:-----BEGIN (?:[A-Z0-9]{1,12} )?PRIVATE KEY-----.{0,16384}?"
            r"-----END (?:[A-Z0-9]{1,12} )?PRIVATE KEY-----)"
        ),
    ),
    # The truncated case: a header with a body but no footer, which is what a
    # ring buffer or a log tail produces. Eats base64 and whitespace only, in
    # ONE bounded character class (no nested quantifier, so no backtracking
    # blowup), and stops at the first character that cannot be part of a PEM
    # body. Over-redacts into a following alnum-only line in the worst case,
    # which is the correct direction to be wrong in.
    (
        "private-key-truncated",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]{1,12} )?PRIVATE KEY-----[A-Za-z0-9+/=\s]{0,8192}"
        ),
    ),
]


# --- Exact-value allowlist --------------------------------------------------
#
# SYNTHETIC sentinels owned by the test suite. Each one exists so a test can
# assert that a secret does NOT appear somewhere; they are checked in on
# purpose and are not credentials for anything.
#
#   tdd/integration/services/test_agent_step_container.py  (T2 containment)
#   tdd/unit/execution/test_runner_protocol.py             (frame redaction)
#   tdd/unit/services/test_agent_step_dispatch.py          (dispatch redaction)
#   tdd/unit/services/test_remote_step_dispatch.py         (remote redaction)
#
# Add to this list ONLY when adding a new deliberately-fake sentinel, and
# say in a comment which test owns it.
#
# NOTE: tdd/unit/services/test_redaction.py deliberately adds NOTHING here.
# It builds every key-shaped string at run time by concatenation, so no line
# of its source matches a pattern and the allowlist does not have to grow a
# hole for it. Copy that trick rather than extending this set.
ALLOWLIST = {
    "sk-ant-T2-CONTAINMENT-9f2a11c4",
    "sk-ant-SENTINEL-DO-NOT-LEAK",
    "sk-ant-do-not-leak-me",
    # Placeholder shipped in .env.example so a new user knows the shape.
    "sk-ant-xxxxx",
    # --- Bug-report bundle containment sentinels (12.9 diagnostics) --------
    #
    # Every one of these is asserted ABSENT from a rendered bundle by the test
    # that owns it, so each has to be spelled out in full somewhere in the
    # tree. They are allowlisted for the SOURCE SCAN only; the Redactor does
    # not consult this set, so these still redact at runtime like any other
    # key-shaped string (see the module docstring).
    #
    # tdd/integration/api/test_diagnostics_api.py, the four env/log/response
    # containment cases:
    "sk-ant-do-not-emit-this",
    "sk-ant-api03-REALKEYSHAPEDVALUE0000",
    "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBB",
    # frontend/e2e/logs.spec.ts: the key the Logs tab must never render.
    "sk-ant-api03-LiveLookingKeyMustNeverBeRendered-0123456789",
}

# --- Environment variables that must never carry a value in an image --------
#
# A format-based grep cannot catch a key whose provider we have not modelled,
# so images get a second, shape-independent rule: a variable whose NAME says
# "credential" must not have a non-empty value baked into the image config.
# This is the check that actually enforces "the images must never bake an AI
# key" - the build needs no secrets at all, so any value here is a bug.
#
# The bug-report redactor reuses this for the same reason in the other
# direction: any env var whose NAME says credential contributes its VALUE to
# the exact-match pass, whatever shape that value happens to have. That is
# the only rule in the whole system that catches a provider nobody modelled.
SECRET_ENV_NAME = re.compile(
    r"(API_KEY|_TOKEN|TOKEN_|SECRET|PASSWORD|PASSWD|CREDENTIAL|PRIVATE_KEY)",
    re.IGNORECASE,
)

# Values that are fine to see on a secret-shaped env var name: the empty
# string, and the handful of well-known non-secret defaults.
BENIGN_ENV_VALUES = {
    "",
    "0",
    "1",
    "false",
    "true",
    "none",
    "null",
    "unset",
    "changeme",
}


def find_secrets(text, allowlist=ALLOWLIST, patterns=PATTERNS):
    """Return a list of (label, matched_value) for non-allowlisted matches.

    The signature grew two defaulted arguments and did not change behaviour
    for either CI scanner. They exist so the redactor's own test suite can
    ask the same question with the allowlist switched OFF, which is how the
    bundle gate in tdd/unit/services/test_redaction.py proves that a REAL
    key of a sentinel's shape would still have been caught.
    """
    hits = []
    allowlist = allowlist or ()
    for label, pattern in patterns:
        for match in pattern.findall(text):
            if match in allowlist:
                continue
            hits.append((label, match))
    return hits


def redact_for_ci(value):
    """Render a hit for a public CI log without reprinting the whole secret.

    If a real key ever does trip the gate, the failure output is world
    readable on a public repo. Print enough to locate it, never enough to
    use it.

    **THIS IS NOT THE BUG-REPORT RENDERER AND MUST NOT BE USED AS ONE.** It
    prints ten live characters and the exact length, which is defensible for
    an operator who has to go find the key they must now rotate, and
    indefensible for a file attached to a public GitHub issue. The bundle
    renderer is `redaction.redactor.Redactor`, whose placeholder is fixed
    width and carries no bytes of the value at all.

    Exported as `redact` by the .github/scripts shim, which is the name both
    scanners already call.
    """
    if len(value) <= 12:
        return value
    return f"{value[:10]}...<{len(value) - 10} more chars redacted>"


#: Back-compat alias. The CI scanners import this name.
redact = redact_for_ci
