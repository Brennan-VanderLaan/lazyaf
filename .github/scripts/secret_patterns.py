"""Shared secret-detection rules for LazyAF's release CI.

ONE definition of "what a leaked key looks like", imported by both
`scan_repo_secrets.py` (source tree) and `scan_image_secrets.py` (built
container images). Keeping the rules here means a new provider is added in
one place and both gates tighten at once.

Design notes for whoever edits this next:

* The patterns target LIVE key FORMATS, not the word "key". Grepping for
  `API_KEY` would flag every legitimate mention of a variable name in the
  code base and the gate would be turned off within a week. Matching the
  provider's actual token shape keeps the false-positive rate near zero,
  which is the only way a blocking gate survives.

* `ALLOWLIST` holds EXACT STRING VALUES, never regexes and never file
  paths. LazyAF's test suite deliberately contains key-shaped strings: they
  are the sentinels the containment tests assert never reach a log, a step
  container's environment, or an API response. Allowlisting by exact value
  means those specific fakes pass while a real key of the same shape sitting
  on the next line still fails the build. An allowlist of file paths would
  have created a hole the size of `tdd/`.

* Every entry in `ALLOWLIST` must be a value that is obviously fake to a
  human reader. If you ever find yourself wanting to add something that
  looks plausibly real, that is the gate working; rotate the key instead.
"""

import re

# --- Live key formats -------------------------------------------------------
#
# Each entry is (label, compiled pattern). Labels are printed on failure so
# the operator knows which provider to go rotate.
PATTERNS = [
    # Anthropic. Real keys are `sk-ant-api03-` + ~95 chars of base64url.
    # The generic `sk-ant-` + 12 rule is intentionally wider than the real
    # format so a future key prefix (api04, admin keys, ...) is still caught.
    ("anthropic", re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}")),
    # OpenAI classic (`sk-` + 48) and project keys (`sk-proj-` + long tail).
    # `sk-` alone is too common in ordinary prose, hence the length floor.
    ("openai", re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}")),
    ("openai", re.compile(r"\bsk-[A-Za-z0-9]{32,}")),
    # Google / Gemini. `AIza` + exactly 35 chars is the documented shape.
    ("google", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    # GitHub tokens. These would be the worst thing to bake into a published
    # image, since the image is published BY GitHub.
    ("github", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("github", re.compile(r"github_pat_[A-Za-z0-9_]{50,}")),
    # AWS access key ids, which travel with a secret and are worth catching
    # even though LazyAF does not use AWS today.
    ("aws", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # Private keys of any flavour.
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
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
ALLOWLIST = {
    "sk-ant-T2-CONTAINMENT-9f2a11c4",
    "sk-ant-SENTINEL-DO-NOT-LEAK",
    "sk-ant-do-not-leak-me",
    # Placeholder shipped in .env.example so a new user knows the shape.
    "sk-ant-xxxxx",
}

# --- Environment variables that must never carry a value in an image --------
#
# A format-based grep cannot catch a key whose provider we have not modelled,
# so images get a second, shape-independent rule: a variable whose NAME says
# "credential" must not have a non-empty value baked into the image config.
# This is the check that actually enforces "the images must never bake an AI
# key" — the build needs no secrets at all, so any value here is a bug.
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


def find_secrets(text):
    """Return a list of (label, matched_value) for non-allowlisted matches."""
    hits = []
    for label, pattern in PATTERNS:
        for match in pattern.findall(text):
            if match in ALLOWLIST:
                continue
            hits.append((label, match))
    return hits


def redact(value):
    """Render a hit for a public CI log without reprinting the whole secret.

    If a real key ever does trip this gate, the failure output is world
    readable on a public repo. Print enough to locate it, never enough to
    use it.
    """
    if len(value) <= 12:
        return value
    return f"{value[:10]}...<{len(value) - 10} more chars redacted>"
