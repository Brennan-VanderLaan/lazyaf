"""Attack the redactor. Do not admire it.

WHY THIS FILE IS SHAPED LIKE THIS. The bug-report bundle is attached to
issues on a PUBLIC repository. A credential that survives redaction is
published permanently and indexed. So the tests are written as smuggling
attempts - split it across lines, base64 it, url-encode it, truncate it,
double it up - and every attempt that SUCCEEDS is pinned here as a stated
limit rather than quietly left out. A denylist that silently stops matching
is worse than no denylist, because it is trusted.

THE ANTI-ROT MECHANISM IS `test_every_pattern_label_has_a_sample`. It is
parametrised over `REDACT_PATTERNS + BLOCK_PATTERNS` itself, so adding a
pattern without adding a sample to `LIVE_SHAPES` FAILS. Coverage cannot drift
behind the rules.

NOT ONE KEY-SHAPED LITERAL APPEARS IN THIS FILE'S SOURCE. Every sample is
assembled at run time by concatenation, split immediately after the
discriminating prefix, so no single line matches a pattern and
`.github/scripts/scan_repo_secrets.py` needs no new ALLOWLIST entry. That
matters: every allowlist entry is a hole, and a test file full of them is a
big one. Copy the trick rather than growing the list.
"""
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
backend_path = REPO_ROOT / "backend"
sys.path.insert(0, str(backend_path))

from app.services.redaction.discovery import (  # noqa: E402
    MIN_DISCOVERED_LENGTH,
    discover_known_secrets,
)
from app.services.redaction.patterns import (  # noqa: E402
    ALLOWLIST,
    BLOCK_PATTERNS,
    PATTERNS,
    REDACT_PATTERNS,
    REDACTION_ONLY_PATTERNS,
    find_secrets,
    redact_for_ci,
)
from app.services.redaction.redactor import (  # noqa: E402
    MIN_LENGTH_FOR_TRUNCATION,
    TRUNCATION_PREFIX_LENGTH,
    WITHHELD,
    KnownSecret,
    Redactor,
    marker_render,
)

# ---------------------------------------------------------------------------
# Sample credentials. Fake, and assembled at run time - see the module
# docstring for why no key-shaped literal may appear in this source.
# ---------------------------------------------------------------------------

_A = "A" * 96
_a = "a" * 40


def _cat(*parts: str) -> str:
    return "".join(parts)


LIVE_SHAPES = {
    "anthropic": [_cat("sk-", "ant-", "api03-", _A)],
    "openai": [_cat("sk-", "proj-", _A[:48]), _cat("sk-", "T3" + _A[:46])],
    "openai-wide": [_cat("sk-", "localvllm123")],
    "google": [_cat("AIza", "Sy" + _a[:33])],
    "github": [
        _cat("ghp_", _A[:36]),
        _cat("gho_", _a[:36]),
        _cat("github_", "pat_", _A[:22] + "_" + _a[:40]),
    ],
    "aws": [_cat("AKIA", "IOSFODNN7EXAMPLE")],
    "slack": [_cat("xox", "b-", "2401" + _a[:24])],
    "jwt": [
        _cat(
            "eyJ",
            "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            ".",
            "eyJzdWIiOiJzdGVwLTQyIiwiaWF0IjoxNTE2MjM5MDIyfQ",
            ".",
            _a[:32],
        )
    ],
    "bearer": [
        "Bearer " + _cat("st", "ep-token-9f2a11c4"),
        # Base64/base64url tokens: the class must keep `+ / = ~`.
        "bearer " + _cat("YWJjZ", "GVmZ2hpamts+/=="),
        # SEVEN CHARACTERS. A length floor here silently un-scrubbed a
        # standing assertion in test_endpoint_probe.py; see the pattern's
        # comment. Pinned so nobody "tightens" it back.
        "Bearer " + _cat("ab", "c.def"),
    ],
    "basic-auth": ["Basic " + _cat("bGF6", "eWFmOnRvazNuLXNlY3JldC0xMjM0")],
    "git-url-auth": ["https://" + _cat("lazyaf:", "ghp_" + _A[:36]) + "@github.com/x/y.git"],
    "private-key": [
        _cat(
            "-----BEGIN ", "RSA PRIVATE KEY-----\n",
            "MIIEowIBAAKCAQEAx7Vn9dQe0mJZ8kKqTn2wPl4sYbGh3fRcVu1oNtLpQwXyZmDa\n",
            "9cRtBvKsElHjWnOpUqIyXcTgAdFbMeNvZrLkPjHoSuWqYtRbCxDmGnJfAeVkPqLz\n",
            "-----END ", "RSA PRIVATE KEY-----",
        )
    ],
    "private-key-truncated": [
        _cat(
            "-----BEGIN ", "OPENSSH PRIVATE KEY-----\n",
            "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAABlwAAAAdz\n",
            "c2gtcnNhAAAAAwEAAQAAAYEAyVn8dQe0mJZ8kKqTn2wPl4sYbGh3fRcVu1oNtLpQ\n",
        )
    ],
}

#: The part of a sample that must not survive. For most shapes that is the
#: whole string; for the ones that carry a readable scheme or host it is the
#: credential half, because the host IS the diagnostic.
SECRET_CORE = {
    "Bearer " + _cat("st", "ep-token-9f2a11c4"): _cat("st", "ep-token-9f2a11c4"),
    "bearer " + _cat("YWJjZ", "GVmZ2hpamts+/=="): _cat("YWJjZ", "GVmZ2hpamts+/=="),
    "Bearer " + _cat("ab", "c.def"): _cat("ab", "c.def"),
    "bearer": _cat("st", "ep-token-9f2a11c4"),
    "basic-auth": _cat("bGF6", "eWFmOnRvazNuLXNlY3JldC0xMjM0"),
    "git-url-auth": _cat("lazyaf:", "ghp_" + _A[:36]),
    "private-key": "MIIEowIBAAKCAQEAx7Vn9dQe0mJZ8kKqTn2wPl4sYbGh3fRcVu1oNtLpQwXyZmDa",
    "private-key-truncated": "c2gtcnNhAAAAAwEAAQAAAYEAyVn8dQe0mJZ8kKqTn2wPl4sYbGh3fRcVu1oNtLpQ",
}


def core_of(label: str, sample: str) -> str:
    """Per-sample override first, then per-label, then the whole sample."""
    if sample in SECRET_CORE:
        return SECRET_CORE[sample]
    return SECRET_CORE.get(label, sample)


#: A plausible platform secret: the shape `token_urlsafe(48)` produces.
KNOWN = _cat("Xq7", "2mNbVpL", "9sTfR4wYzA1cJhKdEuG", "6nQ8vB3xMrS0tWyZiOaP")
KNOWN_LABEL = "step-auth-secret"

PLACEHOLDER_RE = re.compile(r"\[REDACTED:[A-Za-z0-9._-]+:[0-9a-f]{6}\]")


@pytest.fixture
def red():
    return Redactor([KnownSecret(KNOWN, KNOWN_LABEL)])


def assert_gone(needle: str, haystack: str, *, floor: int = 8) -> None:
    """No run of `floor`+ characters of `needle` survives in `haystack`.

    Stronger than `needle not in haystack`, which a redactor that removed
    only the first character would pass.
    """
    assert needle not in haystack
    for start in range(0, max(1, len(needle) - floor + 1)):
        chunk = needle[start : start + floor]
        assert chunk not in haystack, f"{chunk!r} survived at offset {start}"


# ===========================================================================
# 1. The anti-rot mechanism: coverage is derived from the rules themselves.
# ===========================================================================


class TestPatternCoverageCannotDrift:
    @pytest.mark.parametrize(
        "label", sorted({label for label, _ in list(REDACT_PATTERNS) + list(BLOCK_PATTERNS)})
    )
    def test_every_pattern_label_has_a_sample(self, label):
        """Adding a pattern without a sample FAILS here.

        This is the whole anti-rot property. Everything else in this file is
        coverage; this is the mechanism that keeps coverage honest.
        """
        assert LIVE_SHAPES.get(label), (
            f"pattern label {label!r} has no sample in LIVE_SHAPES. Add a FAKE "
            f"credential of that shape, assembled by concatenation so no line "
            f"of this file matches a pattern, and the rest of this suite will "
            f"exercise it automatically."
        )

    @pytest.mark.parametrize(
        "label,sample",
        [(label, s) for label, samples in LIVE_SHAPES.items() for s in samples],
    )
    def test_every_sample_is_actually_matched_by_its_pattern(self, label, sample):
        """A sample that no longer matches is a pattern that silently rotted."""
        rules = [p for lbl, p in list(BLOCK_PATTERNS) + list(REDACT_PATTERNS) if lbl == label]
        assert rules, label
        assert any(p.search(sample) for p in rules), (
            f"no {label!r} pattern matches its own sample any more"
        )

    @pytest.mark.parametrize(
        "label,sample",
        [(label, s) for label, samples in LIVE_SHAPES.items() for s in samples],
    )
    def test_every_sample_is_redacted(self, label, sample, red):
        out = red.redact(f"2026-09-01 15:04:07 ERROR upstream said: {sample} <- rejected")
        assert_gone(core_of(label, sample), out)
        assert PLACEHOLDER_RE.search(out)


class TestPatternsAreComposable:
    def test_no_pattern_carries_a_global_compile_flag(self):
        """Load-bearing: patterns are concatenated into one alternation, and
        `(?i)` inside an alternation is a hard error on Python 3.11+. Scoped
        `(?i:...)` groups only."""
        for label, pattern in list(PATTERNS) + list(REDACTION_ONLY_PATTERNS) + list(BLOCK_PATTERNS):
            bad = pattern.flags & (re.I | re.S | re.M | re.X)
            assert not bad, f"{label!r} carries compile flags {bad!r}; use (?i:...) / (?s:...)"

    def test_no_pattern_uses_a_plain_capturing_group(self):
        """`Redactor` reads `match.lastgroup` to label a hit, and
        `find_secrets` calls `findall`, which returns TUPLES the moment a
        pattern grows a group. Both break silently."""
        for label, pattern in list(PATTERNS) + list(REDACTION_ONLY_PATTERNS) + list(BLOCK_PATTERNS):
            assert pattern.groups == 0, (
                f"{label!r} has {pattern.groups} capturing group(s). Use (?:...)"
            )


# ===========================================================================
# 2. Pass 1: exact values. Smuggling attempts.
# ===========================================================================


class TestKnownValueSmuggling:
    def test_bare(self, red):
        assert_gone(KNOWN, red.redact(f"LAZYAF_STEP_AUTH_SECRET={KNOWN}"))

    def test_inside_json(self, red):
        import json

        assert_gone(KNOWN, red.redact(json.dumps({"env": {"SECRET": KNOWN}, "ok": False})))

    def test_url_encoded(self, red):
        import urllib.parse

        for safe in ("", "/"):
            encoded = urllib.parse.quote(KNOWN, safe=safe)
            out = red.redact(f"GET /git/x.git?token={encoded} HTTP/1.1")
            assert_gone(encoded, out)

    @pytest.mark.parametrize("offset", [0, 1, 2])
    def test_base64_at_every_byte_alignment(self, red, offset):
        """The reason `_b64_cores` generates three alignments.

        `Authorization: Basic <b64("user:" + token)>` starts the secret at an
        arbitrary offset mod 3, and each alignment produces a completely
        different character sequence. Matching only `b64(value)` misses two
        thirds of the real cases.
        """
        import base64

        blob = base64.b64encode(b"u" * offset + KNOWN.encode() + b"tail").decode()
        out = red.redact(f"Authorization: Basic {blob}")
        # The alignment-specific core of the secret must be gone.
        core = base64.b64encode(b"\x00" * ((3 - offset % 3) % 3) + KNOWN.encode()).decode()
        core = core[((3 - offset % 3) % 3) * 4 // 3 + 1 : -4]
        assert core not in out, "a base64 alignment of the secret survived"

    @pytest.mark.parametrize("alphabet", ["standard", "urlsafe"])
    def test_base64_standalone_leaves_no_tail(self, red, alphabet):
        """Trimming the core's last two characters to survive an unknown
        suffix must not leave those two characters behind when the secret IS
        the whole payload. Two base64 characters is a byte and a half of key.
        """
        import base64

        enc = base64.urlsafe_b64encode if alphabet == "urlsafe" else base64.b64encode
        blob = enc(KNOWN.encode()).decode()
        assert_gone(blob, red.redact(f"payload={blob}"), floor=8)

    def test_hex_encoded(self, red):
        import binascii

        hexed = binascii.hexlify(KNOWN.encode()).decode()
        assert_gone(hexed, red.redact(f"dump: {hexed}"))
        assert_gone(hexed.upper(), red.redact(f"dump: {hexed.upper()}"))

    def test_split_across_a_line_wrap(self, red):
        """A token broken by a hard wrap in a log renderer.

        This test found a real defect. The whitespace-tolerant form used to
        run AFTER the literal forms, and a wrapped value does not match its
        own full literal - but it does match its 12-character truncation
        prefix. So the prefix fired first, the tolerant match was destroyed,
        and thirty-seven characters of a live secret were left sitting
        immediately after a placeholder. Tolerant now runs first.
        """
        wrapped = KNOWN[:30] + "\n" + KNOWN[30:]
        out = red.redact(f"token: {wrapped}\nnext line")
        assert_gone(KNOWN[:30], out)
        assert_gone(KNOWN[30:], out)
        assert "next line" in out

    def test_split_by_a_zero_width_space(self, red):
        """`\\s` does NOT cover U+200B - it is category Cf, not Zs - so a
        redactor relying on `\\s` walks straight past this."""
        out = red.redact("token: " + KNOWN[:20] + "​" + KNOWN[20:])
        assert_gone(KNOWN[:20], out)

    def test_truncated_to_a_prefix(self, red):
        """Something logged `value[:16]` and thought that was safe."""
        assert len(KNOWN) >= MIN_LENGTH_FOR_TRUNCATION
        out = red.redact(f"using key {KNOWN[:16]}... for this run")
        assert_gone(KNOWN[:16], out, floor=TRUNCATION_PREFIX_LENGTH)

    def test_a_truncation_renders_as_the_same_placeholder_as_the_whole_value(self, red):
        """The digest is over the VALUE'S IDENTITY, not the matched bytes, so
        `[REDACTED:step-auth-secret:9c41a2]` reads the same whether the log
        printed all of the secret or the first twelve characters of it."""
        whole = PLACEHOLDER_RE.search(red.redact(KNOWN)).group(0)
        part = PLACEHOLDER_RE.search(red.redact(KNOWN[:16])).group(0)
        assert whole == part

    def test_doubled_up(self, red):
        out = red.redact(f"{KNOWN}{KNOWN}")
        assert_gone(KNOWN, out)
        assert out.count("[REDACTED:") == 2

    def test_a_short_secret_that_prefixes_a_long_one_leaves_no_tail(self):
        """Longest-first ordering. Without it the short value matches first
        and the long one's tail sits in the output NEXT TO a placeholder -
        worse than no redaction, because it looks handled."""
        short = KNOWN[:20]
        red = Redactor([KnownSecret(short, "short"), KnownSecret(KNOWN, "long")])
        out = red.redact(f"value={KNOWN}")
        assert_gone(KNOWN, out)
        assert out.count("[REDACTED:") == 1

    def test_the_label_names_the_source_not_the_shape(self, red):
        out = red.redact(KNOWN)
        assert KNOWN_LABEL in out, "a placeholder should say WHICH secret it hid"

    def test_receipt_counts_and_correlates(self, red):
        result = red.redact_with_receipt(f"a={KNOWN} b={KNOWN} c=nothing")
        assert result.counts == {KNOWN_LABEL: 2}
        assert result.total == 2
        assert len(result.placeholders) == 1, (
            "two occurrences of ONE secret must correlate to one placeholder"
        )


class TestStatedLimits:
    """Attacks that get through. Pinned here so they cannot be claimed away.

    Each of these is also written down in `redactor.py`'s docstring. If one
    of these tests starts failing, that is an IMPROVEMENT - update the
    docstring and invert the assertion.
    """

    def test_LIMIT_an_encoding_of_a_value_we_do_not_hold_is_not_caught(self, red):
        """Pass 2 is shape matching on raw text. A base64'd `ghp_...` is not
        `ghp_`-shaped any more, and we have no literal to compare against."""
        import base64

        unknown = LIVE_SHAPES["github"][0]
        blob = base64.b64encode(unknown.encode()).decode()
        assert blob in red.redact(f"payload={blob}")

    def test_LIMIT_a_truncation_shorter_than_the_prefix_window_is_not_caught(self, red):
        """Named producer: `patterns.redact_for_ci` renders `value[:10]`, so
        its own output survives this redactor. Deliberate for CI; worth
        knowing."""
        ten = KNOWN[:10]
        assert ten in red.redact(f"leaked {ten}")
        assert redact_for_ci(KNOWN).startswith(ten)

    def test_LIMIT_homoglyph_mangling_evades_the_shape_pass(self, red):
        """Precisely stated, because it sounds worse than it is: a token
        rendered with a Cyrillic lookalike IS NO LONGER THE TOKEN, so this
        does not publish a usable credential. The real exposure is the
        reverse - if the log itself mangled the value before we saw it, pass
        1 cannot recognise it either, and nothing recovers from that."""
        mangled = LIVE_SHAPES["anthropic"][0].replace("a", "а", 1)
        assert "а" in red.redact(mangled)

    def test_LIMIT_a_secret_chunked_into_a_list_is_not_reassembled(self, red):
        chunks = [KNOWN[i : i + 8] for i in range(0, len(KNOWN), 8)]
        out = red.redact(str(chunks))
        assert chunks[0] in out

    def test_LIMIT_a_short_value_inside_a_larger_base64_blob_at_an_offset(self):
        """Found by a red-team pass, not by design, which is why the number is
        written down: a value under ~16 characters embedded at a NON-ZERO byte
        alignment produces a base64 core shorter than `MIN_B64_CORE`, and
        lowering that floor would start redacting ordinary 12-character
        alphanumeric runs. Nothing the platform mints is this short."""
        import base64

        short = _cat("s3c", "r3t-9x")
        red = Redactor([KnownSecret(short, "endpoint")])
        offset_blob = base64.b64encode(b"u" + short.encode() + b"x").decode()
        assert offset_blob in red.redact(offset_blob)

    @pytest.mark.parametrize("alphabet", ["standard", "urlsafe"])
    def test_a_short_values_own_base64_IS_caught(self, alphabet):
        """NOT a limit - a hole that the limit above was hiding.

        `MIN_B64_CORE` was originally applied to the standalone encoding too.
        `b64("s3cr3t-9x")` is twelve characters, so the variant was never
        generated and a nine-character endpoint key base64'd on its own went
        through untouched. The floor now guards only the TRIMMED cores, which
        are substrings and can collide; the standalone forms are 1:1 with the
        value and need no floor.
        """
        import base64

        short = _cat("s3c", "r3t-9x")
        red = Redactor([KnownSecret(short, "endpoint")])
        enc = base64.urlsafe_b64encode if alphabet == "urlsafe" else base64.b64encode
        for blob in (enc(short.encode()).decode(), enc(short.encode()).decode().rstrip("=")):
            assert blob not in red.redact(f"payload={blob}")


class TestShortValuesAreStillCoveredWhereTheyCanBe:
    """The other half of the red-team finding: the wrap and zero-width splits
    used to need 16 characters, so a 9-character LAN vLLM key survived being
    broken across a line. The tolerant floor is now the discovery floor."""

    SHORT = _cat("s3c", "r3t-9x")

    @pytest.mark.parametrize(
        "separator",
        ["\n", "\r\n", "\t", "\u200b", "\u00ad", " "],
        ids=["lf", "crlf", "tab", "zwsp", "soft-hyphen", "space"],
    )
    def test_a_nine_character_key_survives_no_split(self, separator):
        red = Redactor([KnownSecret(self.SHORT, "endpoint")])
        out = red.redact("key: " + self.SHORT[:4] + separator + self.SHORT[4:])
        assert self.SHORT[:4] not in out and self.SHORT[4:] not in out

    def test_the_tolerant_floor_stays_above_the_engine_floor(self):
        """At four characters a tolerant match starts finding `a b c d` in
        ordinary prose, so the two floors are deliberately different."""
        from app.services.redaction.redactor import (
            DEFAULT_MIN_KNOWN_LENGTH,
            MIN_LENGTH_FOR_WHITESPACE_TOLERANCE,
        )

        assert MIN_LENGTH_FOR_WHITESPACE_TOLERANCE > DEFAULT_MIN_KNOWN_LENGTH
        assert MIN_LENGTH_FOR_WHITESPACE_TOLERANCE == MIN_DISCOVERED_LENGTH


# ===========================================================================
# 3. The placeholder contract.
# ===========================================================================


class TestPlaceholderIsStableAndIrreversible:
    def test_two_occurrences_correlate(self, red):
        out = red.redact(f"backend minted {KNOWN}\nrunner presented {KNOWN}")
        found = PLACEHOLDER_RE.findall(out)
        assert len(found) == 2 and found[0] == found[1]

    def test_two_different_secrets_do_not_collide(self):
        other = KNOWN[::-1]
        red = Redactor([KnownSecret(KNOWN, "step"), KnownSecret(other, "runner")])
        out = red.redact(f"{KNOWN} vs {other}")
        found = PLACEHOLDER_RE.findall(out)
        assert len(found) == 2 and found[0] != found[1]

    def test_the_placeholder_carries_no_bytes_of_the_secret(self, red):
        placeholder = PLACEHOLDER_RE.search(red.redact(KNOWN)).group(0)
        digest = placeholder.rsplit(":", 1)[1].rstrip("]")
        for size in (3, 4, 5, 6):
            for start in range(len(KNOWN) - size + 1):
                assert KNOWN[start : start + size] not in digest

    def test_the_placeholder_is_fixed_width_regardless_of_secret_length(self):
        """No length, no prefix - unlike `redact_for_ci`, which prints both
        and is therefore never used on bundle content."""
        widths = set()
        for length in (12, 40, 400):
            value = ("z9" * 400)[:length]
            red = Redactor([KnownSecret(value, "x")])
            widths.add(len(PLACEHOLDER_RE.search(red.redact(value)).group(0)))
        assert len(widths) == 1

    def test_the_digest_is_not_a_confirmation_oracle_across_bundles(self):
        """Six hex digits is 24 bits - plenty to CONFIRM a guessed key if the
        digest were unsalted. A per-instance random salt makes the digest
        meaningless to anyone who does not hold it, and nobody does."""
        first = PLACEHOLDER_RE.search(Redactor([KnownSecret(KNOWN, "x")]).redact(KNOWN)).group(0)
        second = PLACEHOLDER_RE.search(Redactor([KnownSecret(KNOWN, "x")]).redact(KNOWN)).group(0)
        assert first != second

    def test_there_is_no_way_to_pin_the_salt(self):
        """Somebody would fix it in a test, and then somebody would make the
        fixed value the default."""
        import inspect

        assert "salt" not in inspect.signature(Redactor.__init__).parameters

    def test_repr_does_not_print_the_known_values(self, red):
        """A traceback that reprs a Redactor must not dump the alternation,
        which is every known secret, escaped but entirely readable."""
        assert_gone(KNOWN, repr(red))

    def test_describe_returns_the_label_and_never_the_value(self, red):
        placeholder = PLACEHOLDER_RE.search(red.redact(KNOWN)).group(0)
        assert red.describe(placeholder) == KNOWN_LABEL
        assert red.describe("not a placeholder") is None

    @pytest.mark.parametrize(
        "hostile",
        [
            "env:FOO]\nsk-" + "ant-" + _A[:40],
            "env:" + _cat("gh", "p_") + _A[:36],
            "]" * 40,
            "\x00\x1b[31m",
            "",
        ],
    )
    def test_a_hostile_label_cannot_inject_into_the_placeholder(self, hostile):
        """Env var NAMES become labels, and env var names are attacker
        supplied in any shared-host deployment.

        Sanitising punctuation is NOT enough, and this test is why: a name
        carrying a credential SHAPE survives sanitisation intact (every
        character of `sk-ant-AAAA...` is legal in a label), and then pass 2
        runs over the substituted text and matches the shape INSIDE the
        placeholder - producing a nested `[REDACTED:...[REDACTED:...]...]`
        that breaks `describe`, breaks the receipt's correlation set, and
        breaks idempotence.
        """
        red = Redactor([KnownSecret(KNOWN, hostile)])
        out = red.redact(KNOWN)
        assert PLACEHOLDER_RE.fullmatch(out), out
        assert out.count("[") == 1 and out.count("]") == 1
        assert red.redact(out) == out

    def test_placeholders_are_inert_and_redaction_is_idempotent(self, red):
        """If any rule matched inside `[REDACTED:...]`, repeated application
        would compound - and the bundle builder redacts on the way in and the
        viewer redacts again on the way to the screen."""
        text = f"{KNOWN} and {LIVE_SHAPES['anthropic'][0]} and {LIVE_SHAPES['aws'][0]}"
        once = red.redact(text)
        assert red.redact(once) == once


# ===========================================================================
# 4. PEM blocks. The defect you get for free by reusing the gate's rule.
# ===========================================================================


class TestPrivateKeyBlocks:
    def test_the_body_is_removed_not_just_the_header(self, red):
        """A header-only rule is right for a GATE and lethal in a REDACTOR:
        it blanks `-----BEGIN...` and ships the entire base64 body."""
        sample = LIVE_SHAPES["private-key"][0]
        out = red.redact(f"config load failed:\n{sample}\n...continuing")
        for line in sample.splitlines():
            if "-----" not in line:
                assert line not in out
        assert "MIIEowIBAAKCAQEA" not in out

    def test_a_truncated_block_is_still_removed(self, red):
        """A ring buffer or a log tail cuts the footer off. Without the
        truncated-block rule the whole body survives."""
        sample = LIVE_SHAPES["private-key-truncated"][0]
        out = red.redact(sample + "\n2026-09-01 ERROR next log line")
        assert "c2gtcnNhAAAAAwEAAQAAAYEA" not in out
        assert "next log line" in out, "over-redaction must stop at non-base64 text"

    def test_the_gate_still_uses_the_header_only_rule(self):
        """Deliberate asymmetry: a gate only has to NOTICE, and the block
        rules would be needless cost on a 1 GB image scan."""
        gate_pem = [p for lbl, p in PATTERNS if lbl == "private-key"]
        assert gate_pem and "END" not in gate_pem[0].pattern


# ===========================================================================
# 5. It must never raise, and it must fail closed.
# ===========================================================================


class TestTotality:
    # Parametrised over FACTORIES, not values. pytest builds a test id from
    # the parameter's repr, so passing `"x" * 200_000` directly produced a
    # 200,000-character test id - four setup ERRORS and a suite that took
    # fifteen minutes to render its own report. The values here are the point;
    # their reprs are not.
    WEIRD = [
        ("none", lambda: None),
        ("int", lambda: 1234),
        ("bytes", lambda: b"raw bytes with no secret"),
        ("invalid-utf8", lambda: bytearray(b"\xff\xfe\x00 not utf-8")),
        ("memoryview", lambda: memoryview(b"abc")),
        ("control-chars", lambda: "\x00\x01\x02\x07\x1b[31mansi\x1b[0m"),
        ("lone-surrogate", lambda: "\ud800 lone surrogate"),
        ("huge-line", lambda: "x" * 200_000),
        ("many-newlines", lambda: "\n" * 50_000),
        ("list", lambda: [1, 2, 3]),
        ("dict", lambda: {"a": 1}),
        ("bare-object", object),
        ("nan", lambda: float("nan")),
        ("nested-none", lambda: [None, {"b": None}]),
    ]

    @pytest.mark.parametrize("make", [f for _, f in WEIRD], ids=[n for n, _ in WEIRD])
    def test_never_raises_on_weird_input(self, red, make):
        out = red.redact(make())
        assert isinstance(out, str)

    def test_an_object_whose_str_raises_is_still_handled(self, red):
        class Explodes:
            def __str__(self):
                raise RuntimeError("nope")

        assert red.redact(Explodes()) == WITHHELD

    def test_it_fails_CLOSED_not_open(self, red, monkeypatch):
        """The direction of failure is the whole point. Returning the input on
        error is how a redactor publishes a key while reporting success."""
        def boom(*_args, **_kwargs):
            raise MemoryError("simulated")

        monkeypatch.setattr(red, "_redact", boom)
        out = red.redact(f"secret is {KNOWN}")
        assert KNOWN not in out
        assert out.startswith("[REDACTION FAILED")
        assert red.redact_with_receipt("x").failed is True

    def test_bytes_round_trip_byte_exactly(self, red):
        """`redact_bytes` is for bundle members written straight into a zip:
        every byte that was not part of a match comes back unchanged, invalid
        UTF-8 included."""
        raw = b"prefix \xff\xfe\x80 " + KNOWN.encode() + b" \xc3\x28 suffix"
        out = red.redact_bytes(raw)
        assert isinstance(out, bytes)
        assert KNOWN.encode() not in out
        assert b"\xff\xfe\x80" in out and b"\xc3\x28" in out

    def test_a_huge_single_line_with_no_newline_is_fine(self, red):
        blob = ("noise" * 200_000) + KNOWN
        assert_gone(KNOWN, red.redact(blob))

    def test_stream_redacts_across_a_chunk_boundary(self, red):
        chunks = ["prefix " + KNOWN[:15], KNOWN[15:] + " suffix"]
        out = "".join(red.redact_stream(chunks, overlap=4096))
        assert_gone(KNOWN, out)
        assert "prefix" in out and "suffix" in out


# ===========================================================================
# 6. Performance. A bundle member is megabytes.
# ===========================================================================


class TestPerformance:
    def test_a_multi_megabyte_member_is_not_quadratic(self):
        """Two passes over the text, not one pass per rule. The budget is
        deliberately loose - this is a "did somebody make it quadratic"
        alarm, not a benchmark."""
        secrets = [KnownSecret(("k%02d" % i) + KNOWN[4:], f"s{i}") for i in range(8)]
        red = Redactor(secrets)
        line = "2026-09-01 15:04:07 INFO app.routers.cards branch lookup ok id=%d\n"
        body = "".join(line % i for i in range(48_000))
        assert len(body) > 3_000_000
        start = time.perf_counter()
        out = red.redact(body)
        elapsed = time.perf_counter() - start
        assert len(out) == len(body)
        assert elapsed < 15.0, f"{len(body)} chars took {elapsed:.1f}s"

    @pytest.mark.parametrize(
        "name,make",
        [
            # THE ONE THAT ACTUALLY BIT. `git-url-auth` was written
            # `[a-z][a-z0-9+.\-]*://`, which at every lowercase character
            # eats the rest of the line and backtracks the whole way looking
            # for `://`. A one-megabyte single-line log - a JSON body dumped
            # into a step log - took MINUTES. The scheme is now bounded.
            ("lowercase-1mb-single-line", lambda: "noise" * 200_000),
            # `eyJ` + a long dot-free run: the JWT segments are `{8,4096}`
            # rather than `{8,}` for the same reason.
            ("many-jwt-prefixes", lambda: ("eyJ" + "a" * 200) * 2_000),
            # The basic-auth lookahead's `*` followed by a required class.
            ("many-basic-prefixes", lambda: ("Basic " + "a" * 200) * 2_000),
            # Unterminated PEM headers: both block rules are length-bounded.
            (
                "unterminated-pem-headers",
                lambda: (_cat("-----BEGIN ", "RSA PRIVATE KEY-----") + "\n") * 400
                + "sk-" + "a" * 60_000,
            ),
        ],
    )
    def test_adversarial_input_does_not_backtrack_catastrophically(self, name, make):
        """Every quantifier that is followed by a required token is BOUNDED.

        A redactor that stalls for minutes on one pathological log line is a
        redactor somebody switches off, which is the same outcome as no
        redactor at all. The budget is loose because this is an alarm for
        `O(n^2)`, not a benchmark - the real numbers are ~0.2s each.
        """
        red = Redactor([])
        blob = make()
        start = time.perf_counter()
        red.redact(blob)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, f"{name}: {len(blob)} chars took {elapsed:.1f}s"


# ===========================================================================
# 7. The composition gate: redact, then re-scan with CI's own definition.
# ===========================================================================


class TestBundleCompositionGate:
    """The only test that borrows CI's definition of "a secret".

    Everything else asserts against this file's expectations. This one takes
    the finished, redacted text and runs `find_secrets` over it - the same
    function `scan_repo_secrets.py` enforces - so the redactor and the gate
    can never disagree about what a credential is.
    """

    def test_no_finding_survives_redaction(self, red):
        members = {
            "backend.log": "\n".join(
                f"2026-09-01 ERROR upstream rejected {s}"
                for samples in LIVE_SHAPES.values()
                for s in samples
            ),
            "step-2.log": f"[runner] env LAZYAF_STEP_AUTH_SECRET={KNOWN}\n"
            + "\n".join(sorted(ALLOWLIST)),
        }
        for name, body in members.items():
            out = red.redact(body)
            # allowlist=frozenset() is the STRONG form: it proves a REAL key
            # of a sentinel's shape would have been removed too, rather than
            # passing because the value was checked in on purpose.
            leftovers = find_secrets(out, allowlist=frozenset(), patterns=REDACT_PATTERNS)
            assert leftovers == [], f"{name}: {leftovers}"

    def test_the_allowlist_does_not_disable_runtime_redaction(self, red):
        """A test sentinel in a live log is still redacted. The allowlist is a
        property of the SOURCE TREE - "this fake is checked in on purpose" -
        not a claim that the value is safe to publish. Conflating the two is
        how an allowlist becomes a leak."""
        for sentinel in ALLOWLIST:
            assert sentinel not in red.redact(f"agent used {sentinel}")


# ===========================================================================
# 8. One source of truth (R3), and the ratchet that keeps it that way.
# ===========================================================================

#: Files permitted to define credential-shaped regex literals.
#:
#: `runner-common/runner_common/harness/client.py` is the ONE exception and it
#: is architecturally forced, not an oversight: runner-common is a published
#: wheel that runs inside a step container and imports nothing from
#: `backend/app`, so it cannot share a module with the canonical set. Its own
#: docstring says so. It IS drifted - it knows `sk-` and `Bearer` and not
#: GitHub, AWS, Google, JWT or PEM - and closing that gap means vendoring
#: `patterns.py` into the wheel, which is a build-order change and a separate
#: piece of work. Recorded here so the exception stays visible.
RATCHET_EXEMPT = {
    "backend/app/services/redaction/patterns.py",
    ".github/scripts/secret_patterns.py",
    "tdd/unit/services/test_redaction.py",
    "runner-common/runner_common/harness/client.py",
}

_PRUNED_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "site-packages", ".tox",
}

_SHAPE_TOKENS = ("sk-ant-", "sk-proj-", "gh[pousr]", "github_pat_", "AKIA", "AIza", "xox[", "eyJ")
_REGEX_TOKENS = ("[A-Za-z0-9", "[0-9A-Za-z", "[A-Za-z", "{8,}", "{10,}", "{12,}", "{20,}", "{32,}", "{36,}", "{40,}", "{50,}")


class TestSingleSourceOfTruth:
    def test_no_fourth_copy_of_the_pattern_table_can_be_committed(self):
        """The ratchet. Three copies existed before this module; a tree-wide
        scan for credential-shaped regex literals is what stops a fourth."""
        offenders = []
        # os.walk with in-place pruning, not rglob: rglob enumerates
        # node_modules and every .venv before the filter runs, which on
        # Windows turned this one test into minutes of directory walking.
        for root, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = Path(root) / filename
                rel = path.relative_to(REPO_ROOT).as_posix()
                if rel in RATCHET_EXEMPT:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if any(t in line for t in _SHAPE_TOKENS) and any(
                        t in line for t in _REGEX_TOKENS
                    ):
                        offenders.append(f"{rel}:{n}: {line.strip()[:90]}")
        assert not offenders, (
            "credential-shaped regex literal(s) outside the canonical table:\n  "
            + "\n  ".join(offenders)
            + "\n\nImport from app.services.redaction.patterns instead. If a file "
            "genuinely cannot (a published wheel that must not depend on the "
            "backend), add it to RATCHET_EXEMPT WITH the reason."
        )

    def test_the_packaging_test_no_longer_keeps_its_own_copy(self):
        """The drift this ratchet was written for: `test_wheel_metadata.py`
        had a private table whose bounds disagreed with the canonical one in
        both directions."""
        source = (REPO_ROOT / "tdd/unit/packaging/test_wheel_metadata.py").read_text(
            encoding="utf-8"
        )
        assert "redaction.patterns" in source or "secret_patterns" in source, (
            "test_wheel_metadata.py must import the shared patterns, not restate them"
        )

    def test_the_model_endpoint_scrubber_no_longer_keeps_its_own_copy(self):
        """It matched `sk-` and `Bearer` and nothing else, and it writes into
        a database column an unauthenticated endpoint serves."""
        source = (
            REPO_ROOT / "backend/app/services/model_endpoints/secrets.py"
        ).read_text(encoding="utf-8")
        # Definitions, not mentions - the docstring explains what was removed
        # and naming it there is the point.
        assert not re.search(r"^_(?:SK|BEARER)_RE\s*=", source, re.MULTILINE)
        assert "from app.services.redaction" in source


class TestTheShimKeepsTheScannersWorking:
    def _shim(self):
        path = REPO_ROOT / ".github/scripts/secret_patterns.py"
        spec = importlib.util.spec_from_file_location("shim_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_it_exports_every_name_both_scanners_import(self):
        shim = self._shim()
        for name in ("PATTERNS", "ALLOWLIST", "SECRET_ENV_NAME", "BENIGN_ENV_VALUES",
                     "find_secrets", "redact"):
            assert hasattr(shim, name), name

    def test_it_is_the_same_object_not_a_copy(self):
        """If this ever becomes a copy, R3 is gone and nobody notices."""
        shim = self._shim()
        assert [lbl for lbl, _ in shim.PATTERNS] == [lbl for lbl, _ in PATTERNS]
        assert shim.ALLOWLIST == ALLOWLIST
        assert shim.redact(KNOWN) == redact_for_ci(KNOWN)

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
    def test_the_real_scanner_still_runs_and_passes(self):
        """End-to-end proof that moving the table did not break the gate."""
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / ".github/scripts/scan_repo_secrets.py")],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "scanned" in proc.stdout


# ===========================================================================
# 9. Discovery: where pass 1's values come from.
# ===========================================================================


class TestDiscovery:
    def test_named_settings_fields_are_harvested(self):
        settings = type("S", (), {
            "step_auth_secret": KNOWN,
            "runner_auth_secret": KNOWN[::-1],
            "anthropic_api_key": None,
            "gemini_api_key": "",
        })()
        found = discover_known_secrets(settings=settings, env={})
        labels = {k.label: k.value for k in found}
        assert labels["step-auth-secret"] == KNOWN
        assert labels["runner-auth-secret"] == KNOWN[::-1]
        assert "anthropic-api-key" not in labels

    def test_an_unmodelled_provider_is_caught_by_NAME(self):
        """The only rule in the system that catches a provider nobody
        modelled: `MISTRAL_API_KEY` has no entry in PATTERNS and no field in
        Settings, and it is redacted anyway because we hold its value."""
        value = "mistral-" + "q" * 40
        found = discover_known_secrets(settings=object(), env={"MISTRAL_API_KEY": value})
        assert any(k.value == value and k.label == "env:MISTRAL_API_KEY" for k in found)
        assert_gone(value, Redactor(found).redact(f"401 from upstream: {value}"))

    def test_benign_values_on_secret_shaped_names_are_ignored(self):
        found = discover_known_secrets(
            settings=object(), env={"DEBUG_TOKEN": "1", "X_SECRET": "true", "Y_SECRET": ""}
        )
        assert found == []

    def test_the_FILE_indirection_is_followed(self, tmp_path):
        """A redactor that only knows the inline form is blind on exactly the
        deployments that took secret handling most seriously."""
        secret_file = tmp_path / "key"
        secret_file.write_text(KNOWN + "\n", encoding="utf-8")
        found = discover_known_secrets(
            settings=object(),
            env={"SOME_API_KEY": "inline-ignored", "SOME_API_KEY_FILE": str(secret_file)},
        )
        assert [k.value for k in found] == [KNOWN]

    def test_an_unreadable_FILE_contributes_nothing_and_does_not_raise(self, tmp_path):
        found = discover_known_secrets(
            settings=object(), env={"A_SECRET_FILE": str(tmp_path / "missing")}
        )
        assert found == []

    def test_endpoint_variables_are_harvested_even_with_no_row(self, monkeypatch):
        monkeypatch.setenv("LAZYAF_ENDPOINT_LOCAL_4090", KNOWN)
        found = discover_known_secrets(settings=object(), env=dict(os.environ))
        assert any(k.value == KNOWN for k in found)

    def test_the_floor_is_higher_than_the_engines(self):
        """A harvested value is a GUESS about what is secret. A four-character
        guess turns prose into confetti in a document a human must read."""
        assert MIN_DISCOVERED_LENGTH == 8
        found = discover_known_secrets(settings=object(), env={"A_SECRET": "abc"})
        assert found == []

    def test_it_never_raises_when_settings_cannot_be_built(self, monkeypatch):
        """The moment you most need a bug report is the moment the process is
        misconfigured, and `get_settings()` raises when a shared secret is
        unset."""
        import app.services.redaction.discovery as discovery

        monkeypatch.setattr(discovery, "_settings_or_none", lambda: None)
        monkeypatch.setenv("LAZYAF_STEP_AUTH_SECRET", KNOWN)
        found = discovery.discover_known_secrets(env=dict(os.environ))
        assert any(k.value == KNOWN for k in found)

    def test_duplicate_values_from_two_sources_collapse(self):
        found = discover_known_secrets(
            settings=object(), env={"A_SECRET": KNOWN, "B_TOKEN": KNOWN}
        )
        assert len(found) == 1


# ===========================================================================
# 10. The live leak this refactor closes.
# ===========================================================================


class TestModelEndpointScrubberDelegates:
    """`scrub_secrets` writes `ModelEndpoint.last_error` and `probe_detail`
    INTO THE DATABASE, and `GET /api/model-endpoints` serves that column
    unauthenticated. Its private copy of the pattern table knew `sk-` and
    `Bearer` and nothing else, so a 401 body echoing back any other provider's
    key was persisted verbatim. These are the shapes that were leaking."""

    @pytest.mark.parametrize("label", ["github", "aws", "google", "jwt", "private-key"])
    def test_shapes_that_used_to_survive_are_now_removed(self, label):
        from app.services.model_endpoints.secrets import scrub_secrets

        sample = LIVE_SHAPES[label][0]
        out = scrub_secrets(f"401 Unauthorized: bad key {sample}")
        assert_gone(core_of(label, sample), out)
        assert "***" in out

    def test_the_marker_stays_flat_not_a_placeholder(self):
        """The UI and existing assertions read `***`. An endpoint error is not
        a public-issue attachment, so it has no use for the digest."""
        from app.services.model_endpoints.secrets import scrub_secrets

        out = scrub_secrets("token sk-abcdefgh12345 rejected")
        assert out == "token *** rejected"
        assert "[REDACTED:" not in out

    def test_the_existing_contract_is_unchanged(self):
        from app.services.model_endpoints.secrets import scrub_secrets

        assert scrub_secrets(None) == ""
        assert scrub_secrets(1234) == "1234"
        assert scrub_secrets("the model is qwen", ["en"]) == "the model is qwen"
        assert "hunter2hunter2" not in scrub_secrets(
            "key is hunter2hunter2 here", ("hunter2hunter2",)
        )

    def test_marker_render_ignores_both_arguments(self):
        assert marker_render("anything", "at all") == "***"
