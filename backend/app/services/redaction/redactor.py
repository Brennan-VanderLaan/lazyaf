"""The redactor. Two passes, and the first one is the one that works.

WHAT THIS IS FOR. A bug-report bundle is downloaded, read by a human, and
attached to an issue on github.com/Brennan-VanderLaan/lazyaf, which is
PUBLIC and indexed. A credential that survives this module is published to
the internet permanently. Everything below is tuned to that one fact:
over-redaction costs a blacked-out word, under-redaction costs a rotation.

--------------------------------------------------------------------------
PASS 1 - EXACT VALUES. Strictly stronger than any regex.
--------------------------------------------------------------------------
The backend process HOLDS `LAZYAF_STEP_AUTH_SECRET`. Matching it by shape is
strictly worse than matching it by value: a shape can be wrong, a value
cannot. So the known values run FIRST and unconditionally, and a secret is
gone before any pattern gets a chance to miss it.

This pass is also the only thing in the system that catches a provider
nobody has modelled. `discovery.py` harvests every environment variable
whose NAME says credential - reusing `SECRET_ENV_NAME`, which already exists
for exactly this job on the image scanner - so a key for a provider that has
no entry in PATTERNS is still redacted, because we know its literal value.

--------------------------------------------------------------------------
PASS 2 - SHAPES, for values we do not hold.
--------------------------------------------------------------------------
`REDACT_PATTERNS` from `patterns.py`, plus the multi-line `BLOCK_PATTERNS`.
This catches a key that is in a log because an upstream server echoed it
back, or because the operator pasted one into a prompt.

--------------------------------------------------------------------------
THE PLACEHOLDER: [REDACTED:<label>:<6 hex>]
--------------------------------------------------------------------------
Fixed width. No prefix, no suffix, no length, no bytes of the value, ever.
(`patterns.redact_for_ci` prints ten live characters and the exact length.
That is right for a CI failure an operator has to act on and wrong for a
public issue attachment. Two audiences, two renderers, one pattern set.)

The six hex digits are `HMAC-SHA256(salt, identity)[:6]`, where `salt` is 32
random bytes generated per Redactor instance and NEVER written anywhere.

WHY A DIGEST AT ALL, rather than a bare `***`. "Is the token the runner
presented the same one the backend minted?" is asked constantly while
debugging this platform, and it is the question that separates a routing bug
from a key bug. Deletion answers "there was a secret here" and stops. A
stable placeholder answers "these two log lines refer to the same object"
while carrying zero bits of the object.

WHY SALTED, AND WHY PER-BUNDLE. An unsalted digest of a KNOWN-FORMAT secret
is a confirmation oracle: hold a candidate key, compute sha256(candidate)
truncated, compare against the public issue. Six hex digits is only 24 bits,
but confirming a guess needs no more than that. A per-instance random salt
makes the digest meaningless to everyone, because nobody holds the salt - it
is discarded with the object.

WHAT THAT COSTS, STATED: cross-bundle correlation is gone. You cannot ask
"is this the same key as in last week's report". That is the right trade on
a public repo - within-bundle correlation is the question that actually gets
asked during a debugging session, and cross-bundle correlation is precisely
the capability that would let a stranger link two of the owner's issues
together.

There is deliberately no `salt=` constructor argument. Somebody would fix it
in a test, and then somebody would make the fixed value the default.

FOR KNOWN VALUES THE DIGEST IS OVER THE VALUE'S IDENTITY, NOT THE MATCHED
BYTES. A log that printed the whole secret and a log that printed only its
first twelve characters produce the SAME placeholder, which is what makes
the truncation variant (below) useful rather than confusing. For shape
matches there is no identity to appeal to, so the digest is over the match.

--------------------------------------------------------------------------
ENCODING. How far this goes, and exactly where it stops.
--------------------------------------------------------------------------
A secret that appears base64'd or url-encoded in a log is still a leak. For
KNOWN values - where we hold the literal - the redactor generates and
matches these transforms:

  * literal
  * percent-encoded, both `safe=''` and the default `safe='/'`
  * JSON string-escaped (matters only for values containing " \\ or control
    characters, but it is two lines)
  * base64, standard AND urlsafe alphabets, at ALL THREE byte alignments -
    a secret embedded in a larger base64 blob (`Authorization: Basic
    <b64(user:token)>`) starts at an arbitrary offset mod 3, so matching
    only `b64(value)` misses two thirds of the real cases
  * hex, lower and upper
  * a TRUNCATION variant: the first 12 characters, for values of 32+
    characters. Something printed `value[:16]` and thought that was safe.
  * a WHITESPACE-TOLERANT variant for values of 16+ characters, which
    matches the value with arbitrary whitespace or zero-width characters
    between any two characters. This is what catches a token broken across a
    line wrap. (`\\s` alone would not: U+200B and its family are Unicode
    category Cf, not Zs, so a zero-width space walks straight past it.)

THE TOLERANT FORM RUNS BEFORE THE LITERAL FORMS, and the order is
load-bearing rather than incidental - see `_redact`. A wrapped value does
not match its own full literal but DOES match its truncation prefix, so
running literals first replaced twelve characters and left the remaining
thirty-seven characters of a live secret immediately after a placeholder.

STATED LIMITS - these are NOT caught, and no amount of pattern work fixes
them:

  1. Any encoding of a value we do NOT hold. Pass 2 is shape matching on the
     raw text; a base64'd `ghp_...` is not `ghp_`-shaped any more.
  1b. SHORT known values, at two specific thresholds, both found by a
     red-team pass rather than by design and both stated with their numbers:
     a value under 8 characters gets no whitespace-tolerant form (below that
     length the tolerant match starts finding things in prose), and a value
     under about 16 characters embedded at a NON-ZERO byte alignment inside
     a larger base64 blob produces a core shorter than `MIN_B64_CORE` and is
     not caught. Its own standalone base64 still is. Nothing the platform
     itself mints is affected - `token_urlsafe(48)` is 64 characters and
     `MIN_RECOMMENDED_SECRET_LENGTH` is 32 - but a LAN vLLM key can be short,
     which is exactly why the number is written down instead of rounded off.
  2. A truncation shorter than 12 characters. Named producer:
     `patterns.redact_for_ci` renders `value[:10]`, so its own output would
     survive this redactor. That is intentional for CI and worth knowing.
  3. Compression, encryption, or chunking - a gzipped log body, or a secret
     split into a JSON array of substrings.
  4. A secret split across two DIFFERENT files in one bundle.
  5. Homoglyph / Unicode-lookalike mangling. Worth being precise about,
     because it sounds worse than it is: a token rendered with a Cyrillic
     `a` is no longer the token, so evading pass 2 with homoglyphs does not
     publish a usable credential. The real exposure is the reverse - if the
     LOG ITSELF mangled the value before we saw it, pass 1 cannot recognise
     it either, and we cannot recover from that. NFKC-normalising for
     detection was rejected: normalisation changes string length, so the
     match offsets no longer address the original text.
  6. THE BIG ONE, which is not an encoding problem at all: content that is
     confidential but not credential-shaped. Source code of a private repo
     in an agent step log. A prompt carrying a product decision. A customer
     name in a branch. A host path with a username. A denylist is necessary
     and insufficient, and the compensation is that the bundle is DOWNLOADED
     AND READ BY A HUMAN before anything is uploaded. Redaction is the
     safety net under that review, not a substitute for it.

--------------------------------------------------------------------------
IT MUST NEVER RAISE, AND IT FAILS CLOSED
--------------------------------------------------------------------------
A redactor that throws is a redactor somebody wraps in `except: pass` and
thereby bypasses. Every public entry point catches everything.

FAIL CLOSED, NOT OPEN. On an internal error the return value is a withheld
marker, never the input. Returning the input on failure is how a redactor
publishes a key while reporting success.

--------------------------------------------------------------------------
PERFORMANCE
--------------------------------------------------------------------------
A bundle member can be megabytes. Measured at ~0.3 s/MB, and nothing here is
quadratic - which took two corrections to be true, both found by tests rather
than by reading:

  * KNOWN VALUES are matched with ordered `str.replace`, not with a regex
    alternation. 64 variants as one alternation cost 1.65 s/MB; the same 64
    as `str.replace` cost 0.003 s/MB, because CPython's substring search has
    a Bloom prefilter and `re` has no Aho-Corasick. See `_build_known`.
  * EVERY SHAPE PATTERN is compiled into ONE alternation with the block rules
    first, so pass 2 is a single scan whatever the rule count.
  * EVERY QUANTIFIER FOLLOWED BY A REQUIRED TOKEN IS BOUNDED. `git-url-auth`
    was written `[a-z][a-z0-9+.\\-]*://`, which at every lowercase character
    ate the rest of the line and backtracked the whole way looking for
    `://`: one megabyte on a single line took MINUTES. `patterns.py` names
    each bound and why. A redactor that stalls is a redactor somebody
    switches off, which is the same outcome as no redactor at all.
  * PLACEHOLDERS ARE INERT - no rule matches `[REDACTED:...]`, and labels are
    shape-scrubbed at construction so an attacker-named environment variable
    cannot smuggle one in - so redaction is idempotent and applying it twice
    (once into the bundle, once on the way to the screen) cannot compound.
"""

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .patterns import BLOCK_PATTERNS, REDACT_PATTERNS

# --------------------------------------------------------------------------
# tuning constants - each one is a decision, so each one is named
# --------------------------------------------------------------------------

#: Shortest known value worth substring-matching. Four is what
#: `model_endpoints.secrets.scrub_secrets` has always used and it is kept as
#: the DEFAULT so delegating that function changes nothing. The bug-report
#: collector (`discovery.py`) raises its own floor to 8 instead, because a
#: harvested value is a guess about what is secret and a 4-character guess
#: turns ordinary prose into confetti. The floor belongs at the point of
#: discovery, not in the engine.
DEFAULT_MIN_KNOWN_LENGTH = 4

#: Below this, encoding variants are not generated: base64 of a 6-character
#: value is 8 characters of base64, which collides with real text.
MIN_LENGTH_FOR_ENCODINGS = 8

#: Below this, no whitespace-tolerant variant. Eight characters appearing in
#: order separated only by whitespace does not happen by accident; four
#: (`DEFAULT_MIN_KNOWN_LENGTH`) plausibly does - `a b c d` is a real thing to
#: find in a log - so the tolerant floor sits deliberately above the engine's.
#:
#: Started at 16 and was lowered after a red-team pass found that a 9-character
#: endpoint key - which a LAN vLLM legitimately has - survived being split by a
#: line wrap or a zero-width space. Anything at or above the platform's own
#: discovery floor is now covered.
MIN_LENGTH_FOR_WHITESPACE_TOLERANCE = 8

#: A value must be at least this long before its prefix is matched, and the
#: prefix is this many characters. 32 is `config.MIN_RECOMMENDED_SECRET_LENGTH`,
#: so the prefix is always under half the value.
MIN_LENGTH_FOR_TRUNCATION = 32
TRUNCATION_PREFIX_LENGTH = 12

#: Shortest TRIMMED base64 core worth matching, in base64 characters. A
#: trimmed core is a SUBSTRING of an encoding - that is what makes it able to
#: match a secret sitting at an unknown offset inside a bigger blob - and a
#: short substring of base64 collides with ordinary alphanumeric text.
#:
#: This floor deliberately does NOT apply to the standalone `b64(value)` and
#: `hex(value)` forms, which are 1:1 with the whole value. Applying it to
#: those was a real hole: a nine-character endpoint key base64s to twelve
#: characters, so it was never generated and the encoding went through
#: untouched. See `_b64_cores`.
#:
#: STATED LIMIT (1b in the module docstring): a value under about sixteen
#: characters embedded at a NON-ZERO byte alignment inside a larger base64
#: blob still produces a core too short to reach this floor.
MIN_B64_CORE = 16

#: Characters allowed between two characters of a whitespace-tolerant match:
#: real whitespace plus the soft hyphen and the zero-width family, which
#: ``\s`` does NOT cover - U+200B and friends are Unicode category Cf, not
#: Zs, so ``\s`` walks straight past a token broken with a zero-width space.
_INTERSTITIAL = r"[\s\u00ad\u200b-\u200f\u2060\ufeff]*"

#: The same set as a character MEMBERSHIP test, for the stripped-copy
#: pass in the redactor. A regex class cannot be iterated, so this is a
#: second spelling of one idea - they sit adjacent so an edit to either
#: is visibly an edit to both.
_INTERSTITIAL_CHARS = frozenset(
    # ASCII whitespace, NBSP, soft hyphen, word joiner, BOM ...
    [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0xAD, 0x2060, 0xFEFF]
    # ... plus the zero-width / bidi family U+200B-U+200F, which `\s` does
    # NOT cover: they are category Cf, not Zs.
    + list(range(0x200B, 0x2010))
)
_INTERSTITIAL_CHARS = frozenset(chr(c) for c in _INTERSTITIAL_CHARS)

#: What a failure returns. Not the input - see the module docstring.
WITHHELD = "[REDACTION FAILED - CONTENT WITHHELD]"


def _withheld(exc: BaseException) -> str:
    return f"[REDACTION FAILED - CONTENT WITHHELD: {type(exc).__name__}]"

#: Digest length in hex characters. Correlation needs a handful; more digits
#: would not add safety and would make the placeholder harder to read.
DIGEST_CHARS = 6

_LABEL_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _clean_label(label: str) -> str:
    """Labels reach the output, and one of them is an env var NAME.

    A variable called ``FOO]\\nsk-ant-...`` would otherwise inject a bracket
    and a newline straight into the placeholder. Sanitised and length-capped.
    """
    return _LABEL_SAFE.sub("-", str(label))[:48] or "secret"


def _neutralise_label(label: str) -> str:
    """Strip credential SHAPES out of a label, not just punctuation.

    Found by the injection test, and it is a genuinely nasty one. Sanitising
    punctuation alone is not enough: an environment variable named
    ``FOO]\\nsk-ant-<40 chars>`` sanitises to ``FOO--sk-ant-<40 chars>``,
    every character of which is legal in a label - and then PASS 2 runs over
    the substituted text, matches the shape INSIDE the placeholder, and emits
    ``[REDACTED:env-FOO--[REDACTED:openai-wide:4c3b54]:a773cc]``. Nested
    placeholders break the parser in `describe`, break the receipt's
    correlation set, and break idempotence.

    Run once per known value at construction, so it costs nothing per byte.
    """
    return _SHAPE_RE.sub("-", label) or "secret"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class RedactionResult:
    """Redacted text plus the receipt the review sheet renders.

    `counts` is per label, `total` is the number of substitutions, and
    `placeholders` is the set of placeholder strings emitted - which is how
    a caller answers "the same secret appears in backend.log and in
    step-2.log" without ever holding the secret.
    """

    text: str
    counts: Dict[str, int] = field(default_factory=dict)
    placeholders: set = field(default_factory=set)
    failed: bool = False

    @property
    def total(self) -> int:
        return sum(self.counts.values())


@dataclass(frozen=True)
class KnownSecret:
    """One literal value the process holds, and what to call it.

    `label` names the SOURCE (``step-auth-secret``, ``env:ANTHROPIC_API_KEY``)
    rather than the shape, because "the value in the runner log is your step
    auth secret" is the diagnostic. It reveals nothing the bundle does not
    already publish deliberately: the settings inventory lists which secrets
    are set, by name, with no values.
    """

    value: str
    label: str = "secret"


# --------------------------------------------------------------------------
# variant generation for pass 1
# --------------------------------------------------------------------------


def _b64_cores(raw: bytes, alphabet: str) -> List[str]:
    """Every base64 substring a value can produce at any byte alignment.

    A secret base64'd on its own is `b64(value)`. A secret base64'd INSIDE a
    larger payload - `Basic <b64("user:" + token)>` - starts at an offset
    that is 0, 1 or 2 mod 3, and each alignment produces a completely
    different character sequence. Encoding with 0, 1 and 2 bytes of padding
    in front and stripping the characters that the padding contaminates
    yields the three cores, one of which is guaranteed to be present.
    """
    cores = []
    encode = base64.urlsafe_b64encode if alphabet == "urlsafe" else base64.b64encode
    for pad in (0, 1, 2):
        encoded = encode(b"\x00" * pad + raw).decode("ascii").rstrip("=")
        # Each 3 input bytes -> 4 output chars. `pad` leading bytes
        # contaminate ceil(pad*4/3) leading characters, and the trailing
        # characters are contaminated by whatever the real payload puts
        # AFTER the secret, so both ends are trimmed.
        head = (pad * 4 + 2) // 3
        core = encoded[head:-2] if len(encoded) > head + 2 else ""
        # MIN_B64_CORE applies ONLY to the trimmed cores. A trimmed core is an
        # arbitrary SUBSTRING of an encoding, so a short one can collide with
        # ordinary alphanumeric text and redact it.
        if len(core) >= MIN_B64_CORE:
            cores.append(core)
        if pad == 0:
            # The standalone case, where the secret is the WHOLE payload and
            # nothing contaminates the tail. Two reasons this is separate:
            #
            # 1. Without it the trimmed core matches and leaves the final two
            #    base64 characters - roughly the last byte and a half of the
            #    secret - sitting in the output next to a placeholder.
            # 2. NO CORE FLOOR APPLIES. These forms are 1:1 with the whole
            #    value, not substrings of it, so they cannot collide the way a
            #    trimmed core can. Gating them on MIN_B64_CORE - which the
            #    first version did - meant `b64("s3cr3t-9x")` was 12 characters
            #    and therefore never generated, so a nine-character endpoint
            #    key base64'd on its own went straight through. Caught by a
            #    red-team pass; the length gate that matters is the caller's
            #    MIN_LENGTH_FOR_ENCODINGS on the VALUE.
            padded = encode(raw).decode("ascii")
            cores.append(padded)
            cores.append(padded.rstrip("="))
    return cores


def _literal_variants(value: str) -> List[str]:
    """Every literal spelling of `value` worth searching for."""
    variants = {value}
    if len(value) < MIN_LENGTH_FOR_ENCODINGS:
        return [value]

    # URL / percent encoding, both common `safe` settings.
    try:
        variants.add(urllib.parse.quote(value, safe=""))
        variants.add(urllib.parse.quote(value))
    except Exception:  # noqa: BLE001 - a surrogate in the value
        pass

    # JSON string escaping. Usually identical to the value; matters when the
    # secret contains a quote, a backslash or a control character.
    try:
        variants.add(json.dumps(value)[1:-1])
    except Exception:  # noqa: BLE001
        pass

    try:
        raw = value.encode("utf-8", "surrogatepass")
    except Exception:  # noqa: BLE001
        raw = None
    if raw:
        for alphabet in ("standard", "urlsafe"):
            variants.update(_b64_cores(raw, alphabet))
        # Hex of the whole value, both cases. No core floor: like the
        # standalone base64 forms these are 1:1 with the value rather than
        # substrings of an encoding, so they cannot collide with prose.
        hexed = binascii.hexlify(raw).decode("ascii")
        variants.add(hexed)
        variants.add(hexed.upper())

    if len(value) >= MIN_LENGTH_FOR_TRUNCATION:
        variants.add(value[:TRUNCATION_PREFIX_LENGTH])

    return [v for v in variants if v]


def _whitespace_tolerant_pattern(value: str) -> str:
    """`value` with whitespace and zero-width characters allowed between
    every pair of characters, which is what a line wrap inserts."""
    return _INTERSTITIAL.join(re.escape(ch) for ch in value)


# --------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------


class Redactor:
    """Redact known values and credential shapes out of arbitrary text.

    Reuse ONE instance for a whole bundle: the salt lives on the instance, so
    the same secret renders as the same placeholder across every member, and
    a different bundle renders it differently.
    """

    def __init__(
        self,
        known: Iterable[Any] = (),
        *,
        min_known_length: int = DEFAULT_MIN_KNOWN_LENGTH,
        whitespace_tolerant: bool = True,
        encodings: bool = True,
        render: Optional[Callable[[str, str], str]] = None,
    ) -> None:
        """
        `known` accepts `KnownSecret`s or bare strings (bare strings are
        labelled ``secret``), so the delegating `scrub_secrets` can pass the
        tuple of endpoint values it has always passed.

        `render(label, digest) -> str` overrides the placeholder. The only
        production override is `scrub_secrets`, which must keep emitting
        `***` because that is the marker its callers' assertions and the
        model-endpoint UI already read.
        """
        self._salt = os.urandom(32)
        self._render = render or self._default_render
        # digest -> LABEL only. The values are deliberately not retained on
        # the instance: a traceback that reprs a Redactor must not be able to
        # print them, and neither must anything that pickles one.
        self._identity: Dict[str, str] = {}
        self._known_groups: Dict[str, str] = {}
        self._literals: List[Tuple[str, str]] = []
        self._loose_re: Optional["re.Pattern"] = None
        self._build_known(
            known,
            min_known_length=min_known_length,
            whitespace_tolerant=whitespace_tolerant,
            encodings=encodings,
        )
        self._shape_re = _SHAPE_RE

    # -- construction ------------------------------------------------------

    def _build_known(
        self,
        known: Iterable[Any],
        *,
        min_known_length: int,
        whitespace_tolerant: bool,
        encodings: bool,
    ) -> None:
        """Split pass 1 in two, because ONE regex for it is 550x slower.

        MEASURED, on 1 MB of ordinary log text with 8 known secrets and their
        64 encoding variants (2026-09-01, CPython 3.10):

            64 variants as one `re` alternation      1.650 s
            64 variants as ordered `str.replace`     0.003 s

        `re` has no Aho-Corasick; a BRANCH node is tried alternative by
        alternative at every offset, so cost grows with the variant count.
        `str.replace` is CPython's two-way search with a Bloom prefilter and
        is effectively free when the needle is absent - which it is, for
        every variant, on every line that does not contain a secret. At 2.5
        s/MB the regex version would have made a 10 MB bundle a 25-second
        stall; at 0.003 s/MB the literal pass stops being a cost at all.

        So: LITERALS go through ordered `str.replace`, and only the handful
        of whitespace-tolerant forms - which cannot be literals - stay in a
        regex. The tolerant regex is cheap (0.2 s/MB for 8 of them) because
        there are as many of those as there are secrets, not as there are
        spellings.

        ORDER IS LOAD-BEARING: longest spelling first. Without it a short
        secret that happens to be a prefix of a long one is replaced first
        and leaves the long one's tail sitting in the output next to a
        placeholder - worse than no redaction, because it looks handled.
        """
        literal_alts: List[Tuple[int, str, str]] = []
        loose_alts: List[Tuple[str, str]] = []

        for entry in known or ():
            if isinstance(entry, KnownSecret):
                value, label = entry.value, entry.label
            else:
                value, label = entry, "secret"
            if not isinstance(value, str) or len(value) < max(1, min_known_length):
                continue
            label = _neutralise_label(_clean_label(label))
            # The digest is over the value's IDENTITY: every spelling of one
            # secret - encoded, truncated, wrapped - renders identically.
            identity = self._digest("known", value)
            self._identity[identity] = label
            for spelling in _literal_variants(value) if encodings else [value]:
                literal_alts.append((len(spelling), spelling, identity))
            if whitespace_tolerant and len(value) >= MIN_LENGTH_FOR_WHITESPACE_TOLERANCE:
                loose_alts.append((_whitespace_tolerant_pattern(value), identity))

        literal_alts.sort(key=lambda item: -item[0])
        seen: set = set()
        for _, spelling, identity in literal_alts:
            if spelling in seen:
                continue
            seen.add(spelling)
            self._literals.append((spelling, identity))

        if loose_alts:
            # Longest first here too: at equal offsets Python alternation
            # takes the earlier branch, so a short secret that prefixes a
            # long one must not be listed first.
            loose_alts.sort(key=lambda item: -len(item[0]))
            parts = []
            for offset, (pattern, identity) in enumerate(loose_alts):
                name = f"w{offset}"
                self._known_groups[name] = identity
                parts.append(f"(?P<{name}>{pattern})")
            self._loose_re = re.compile("|".join(parts))

    # -- placeholders ------------------------------------------------------

    def _digest(self, kind: str, value: str) -> str:
        return hmac.new(
            self._salt,
            f"{kind}\x00{value}".encode("utf-8", "surrogatepass"),
            hashlib.sha256,
        ).hexdigest()[:DIGEST_CHARS]

    @staticmethod
    def _default_render(label: str, digest: str) -> str:
        return f"[REDACTED:{label}:{digest}]"

    # -- public API --------------------------------------------------------

    def redact(self, text: Any) -> str:
        """Redacted text. Never raises; fails closed."""
        return self.redact_with_receipt(text).text

    def redact_with_receipt(self, text: Any) -> RedactionResult:
        """Redacted text plus per-label counts and the placeholders emitted."""
        try:
            return self._redact(text)
        except Exception as exc:  # noqa: BLE001 - fail closed, always
            return RedactionResult(text=_withheld(exc), counts={}, failed=True)

    def __repr__(self) -> str:
        """Deliberately opaque. A default repr would print `self._literals`,
        which is every known secret in plain text, into whatever logged the
        traceback."""
        return f"<Redactor known={len(self._identity)} shapes={len(_SHAPE_GROUPS)}>"

    def redact_bytes(self, blob: Any) -> bytes:
        """Byte-exact round trip for content that is not valid UTF-8.

        Decodes with `surrogateescape` and re-encodes the same way, so every
        byte that was not part of a match comes back unchanged - which
        matters for a bundle member written straight into a zip. `redact()`
        uses `errors="replace"` instead, because a `str` carrying lone
        surrogates explodes later in whatever tries to encode it.
        """
        try:
            if isinstance(blob, (bytes, bytearray, memoryview)):
                raw = bytes(blob)
            else:
                raw = str(blob).encode("utf-8", "surrogateescape")
            out = self._redact(raw.decode("utf-8", "surrogateescape")).text
            return out.encode("utf-8", "surrogateescape")
        except Exception as exc:  # noqa: BLE001
            return _withheld(exc).encode("utf-8", "replace")

    def redact_stream(self, chunks: Iterable[Any], *, overlap: int = 4096):
        """Redact an iterable of chunks without holding all of it in memory.

        Carries `overlap` characters between chunks so a secret straddling a
        chunk boundary is still matched.

        STATED LIMIT: this is for TAILS - the live log view - not for bundle
        members. A PEM block longer than `overlap` spans a boundary this
        cannot see across, so the truncated-block rule fires on the first
        half and the second half is emitted as base64. Bundle members are
        buffered whole and go through `redact()`; the design says so and this
        is why.
        """
        carry = ""
        for chunk in chunks:
            try:
                piece = chunk if isinstance(chunk, str) else _coerce(chunk)
            except Exception:  # noqa: BLE001
                yield WITHHELD
                continue
            buffered = carry + piece
            if len(buffered) <= overlap:
                carry = buffered
                continue
            head, carry = buffered[:-overlap], buffered[-overlap:]
            yield self.redact(head)
        if carry:
            yield self.redact(carry)

    def describe(self, placeholder: str) -> Optional[str]:
        """The LABEL behind a placeholder this instance emitted, or None.

        For the operator reading a bundle: "what is [REDACTED:x:9c41a2]".
        Returns the label only. The value is never returned by anything.
        """
        match = re.fullmatch(r"\[REDACTED:([A-Za-z0-9._-]+):([0-9a-f]+)\]", placeholder or "")
        if not match:
            return None
        return self._identity.get(match.group(2)) or match.group(1)

    # -- the actual work ---------------------------------------------------

    def _redact(self, text: Any) -> RedactionResult:
        body = _coerce(text)
        counts: Dict[str, int] = {}
        placeholders: set = set()

        # Pass 1a: the WHITESPACE-TOLERANT forms go FIRST, and the ordering is
        # load-bearing rather than incidental. Found by the line-wrap test:
        # a value broken across a wrap does not match its own full literal,
        # but it DOES match its 12-character truncation prefix - so running
        # literals first replaced the first twelve characters, destroyed the
        # tolerant match, and left the remaining thirty-seven characters of a
        # live secret sitting immediately after a placeholder. The tolerant
        # form subsumes the exact literal (zero interstitials is a legal
        # match), so nothing is lost by trying it first.
        if self._loose_re is not None:
            body = self._loose_re.sub(
                lambda m: self._sub_known(m, counts, placeholders), body
            )

        # Pass 1b: literal spellings - encodings, truncations, and any value
        # too short for a tolerant form - longest first. `needle in haystack`
        # short-circuits the common case (absent) at C speed.
        for spelling, identity in self._literals:
            if spelling not in body:
                continue
            label = self._identity.get(identity, "secret")
            out = self._render(label, identity)
            counts[label] = counts.get(label, 0) + body.count(spelling)
            placeholders.add(out)
            body = body.replace(spelling, out)
        body = self._shape_re.sub(
            lambda m: self._sub_shape(m, counts, placeholders), body
        )
        # Pass 2b: the same shapes, across INSERTED characters. Pass 1 already
        # tolerates them (`_whitespace_tolerant_pattern`); pass 2 did not, and
        # that gap is exactly where it hurts - third-party keys the process
        # does not hold, echoed by an upstream 401 or soft-wrapped by a
        # terminal. A single zero-width space inside `ghp_...` walked straight
        # through, and unlike a homoglyph SUBSTITUTION that mangles the token,
        # an INSERTION is losslessly reversible: delete the inserted characters
        # from the published document and you have the exact credential back.
        body = self._shape_pass_across_interstitials(body, counts, placeholders)
        return RedactionResult(text=body, counts=counts, placeholders=placeholders)

    def _shape_pass_across_interstitials(self, body, counts, placeholders) -> str:
        """Shape-match a copy with interstitials removed, redact in the original.

        Matching on a stripped copy and mapping the spans back is what keeps
        the untouched text byte-identical - a redactor that also silently
        deleted whitespace would corrupt every log it cleaned.

        Over-redaction is the safe direction here: stripping can abut two
        unrelated tokens into something credential-shaped, which costs a
        placeholder where none was needed. Under-redaction costs a published
        credential.
        """
        if not body:
            return body
        stripped_chars: list[str] = []
        origin: list[int] = []
        for index, ch in enumerate(body):
            if ch in _INTERSTITIAL_CHARS:
                continue
            stripped_chars.append(ch)
            origin.append(index)
        if len(stripped_chars) == len(body):
            return body  # nothing was inserted; pass 2 already saw everything
        stripped = "".join(stripped_chars)

        spans: list[tuple[int, int, str]] = []
        for match in self._shape_re.finditer(stripped):
            start, end = match.start(), match.end()
            if start >= len(origin) or end == 0:
                continue
            label = _SHAPE_GROUPS.get(match.lastgroup or "", "secret")
            spans.append((origin[start], origin[end - 1] + 1, label))
        if not spans:
            return body

        out: list[str] = []
        cursor = 0
        for start, end, label in spans:
            if start < cursor:
                continue  # overlapping match already covered
            out.append(body[cursor:start])
            counts[label] = counts.get(label, 0) + 1
            rendered = self._render(label, self._digest("shape", body[start:end]))
            placeholders.add(rendered)
            out.append(rendered)
            cursor = end
        out.append(body[cursor:])
        return "".join(out)

    def _sub_known(self, match, counts, placeholders) -> str:
        identity = self._known_groups.get(match.lastgroup or "", "")
        label = self._identity.get(identity, "secret")
        counts[label] = counts.get(label, 0) + 1
        out = self._render(label, identity)
        placeholders.add(out)
        return out

    def _sub_shape(self, match, counts, placeholders) -> str:
        label = _SHAPE_GROUPS.get(match.lastgroup or "", "secret")
        counts[label] = counts.get(label, 0) + 1
        out = self._render(label, self._digest("shape", match.group(0)))
        placeholders.add(out)
        return out


def _coerce(text: Any) -> str:
    """Anything at all -> str, including a `__str__` that raises."""
    if text is None:
        return ""
    if isinstance(text, str):
        return text
    if isinstance(text, (bytes, bytearray, memoryview)):
        return bytes(text).decode("utf-8", "replace")
    try:
        return str(text)
    except Exception:  # noqa: BLE001 - a __str__ that raises is still a leak
        return WITHHELD


# --------------------------------------------------------------------------
# the shape alternation, built once at import
# --------------------------------------------------------------------------
#
# BLOCK_PATTERNS come FIRST. Python alternation is leftmost-first, so at the
# offset of a `-----BEGIN` the block rule wins over the header-only rule in
# PATTERNS - which is the difference between redacting a private key and
# redacting the word "BEGIN" and shipping the key underneath it.

def _build_shape_re() -> Tuple["re.Pattern", Dict[str, str]]:
    groups: Dict[str, str] = {}
    parts: List[str] = []
    for index, (label, pattern) in enumerate(list(BLOCK_PATTERNS) + list(REDACT_PATTERNS)):
        significant = pattern.flags & (re.I | re.S | re.M | re.X)
        if significant:
            raise ValueError(
                f"redaction pattern {label!r} carries compile flags "
                f"{significant!r}. Patterns must use scoped inline groups - "
                f"(?i:...) / (?s:...) - because they are concatenated into one "
                f"alternation and a global flag is a syntax error there. See "
                f"the composability note in patterns.py."
            )
        name = f"s{index}"
        groups[name] = _clean_label(label)
        parts.append(f"(?P<{name}>{pattern.pattern})")
    return re.compile("|".join(parts)), groups


_SHAPE_RE, _SHAPE_GROUPS = _build_shape_re()


# --------------------------------------------------------------------------
# the marker renderer, for callers that predate the placeholder
# --------------------------------------------------------------------------

#: `model_endpoints.secrets.scrub_secrets` writes into `ModelEndpoint.last_error`
#: and `probe_detail`, which the UI renders and existing assertions read. Those
#: keep the flat `***` marker; only the bug-report bundle gets placeholders.
MARKER = "***"


def marker_render(label: str, digest: str) -> str:
    return MARKER
