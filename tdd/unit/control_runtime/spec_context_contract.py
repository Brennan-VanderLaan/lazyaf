"""THE curated-spec-context wire contract (Phase 12.6.6, cross-agent #1b).

ONE module, imported by BOTH sides of the wire, so a drift on either side
fails a test that names the side that drifted:

- ASSEMBLER = backend/app/services/spec_context.build_spec_context
              (derives the bundle from Feature/UserStory/AcceptanceCriterion/
              TestRef at dispatch time)
- PRODUCER  = backend/app/services/control_layer/workspace.generate_agent_config
              (emits it verbatim as the top-level ``spec_context`` key of
              /workspace/.control/agent.<step_execution_id>.json)
- CONSUMER  = runner_common.agent_config.load_agent_config +
              runner_common.agent_wrapper._write_spec_context
              (loads it, materialises <control dir>/spec_context.md, logs the
              size and truncation facts, deletes the file on the way out)

The pinned shape::

    None                                  # the ONE spelling of "no bundle"
    | {"markdown": str,                   # non-empty; already inside `prompt`
       "source": {"card_id": str|None,
                  "feature_id": str|None,
                  "user_story_id": str|None},
       "criteria_count": int,             # criteria PRESENT in markdown
       "test_ref_count": int,             # test lines PRESENT in markdown
       "estimated_tokens": int,           # ceil(bytes/4) - an ESTIMATE
       "truncated": bool,
       "dropped": [str]}                  # ordered drop-rule names

WHY A DICT AND NOT A BARE STRING. The wrapper has to be able to LOG what it
received - a silently-shrunk brief is exactly the dark behaviour R1 forbids -
and 12.6.5's with/without-curation experiment needs the size and truncation
facts per run without re-deriving them from prose.

WHY ``None`` AND NOT ``{}`` OR ``""``. A card with no spec links must produce
a prompt byte-identical to the pre-12.6.6 one and zero bytes of wire payload.
One spelling, checked once, at every layer.

TRUTHFULNESS RULES (the ones that are not shape):
- ``truncated`` is true IF AND ONLY IF ``dropped`` is non-empty,
- every name in ``dropped`` comes from DROP_RULES, in DROP_RULES order,
- a truncated bundle carries the marker text in its markdown,
- ``markdown`` is at most SPEC_CONTEXT_MAX_BYTES **bytes** (not characters).

This module is NOT a test module (it must not match ``python_files``); it is
plain importable code::

    from tdd.unit.control_runtime.spec_context_contract import (
        CANONICAL_BUNDLE, assert_bundle_conforms,
    )
"""
import math

#: Exactly the allowed top-level keys of a bundle.
TOP_LEVEL_KEYS = frozenset(
    {
        "markdown",
        "source",
        "criteria_count",
        "test_ref_count",
        "estimated_tokens",
        "truncated",
        "dropped",
    }
)

#: Every key is required. Unlike the usage manifest - whose nullable fields
#: encode "the CLI reported nothing", a recorded fact - every field here is
#: something the assembler KNOWS by construction. A missing one is a bug.
REQUIRED_KEYS = frozenset(TOP_LEVEL_KEYS)

#: Exactly the allowed keys of ``source``. Provenance only: what the bundle
#: was derived FROM, so a 12.6.5 variant that underperforms can be traced to
#: the rows it read.
SOURCE_KEYS = frozenset({"card_id", "feature_id", "user_story_id"})

#: The ordered truncation vocabulary. Order IS the contract: notes are
#: supplementary, a feature description is context, a narrative states its
#: intent up front, sibling titles are orientation, test paths are cheap,
#: optional criteria are optional - and REQUIRED CRITERIA ARE THE CONTRACT,
#: so they go last and never all of them.
DROP_RULES = (
    "criterion_notes",
    "feature_description",
    "story_narrative",
    "story_titles",
    "test_refs",
    "optional_criteria",
    "required_criteria",
    "hard_clamp",
)

#: Keys that must NEVER appear on the 12.6.6 wire. The first three are the
#: "just persist it" temptation (the bundle is derived at dispatch and stored
#: nowhere - a stored copy is a second source of truth for the spec that goes
#: stale on the next criterion edit); the rest are payloads this channel is
#: not: test source, other repos' rows, secrets.
FORBIDDEN_KEYS = frozenset(
    {"id", "spec_context_id", "created_at", "test_source", "repo", "secrets"}
)

#: Bytes-per-token of the documented estimator. Mirrors
#: ``control_layer.workspace.SPEC_CONTEXT_BYTES_PER_TOKEN``; the contract test
#: pins the two together so this file can be read on its own.
BYTES_PER_TOKEN = 4

#: The byte cap: 4000 tokens x 4 bytes/token. Mirrors
#: ``control_layer.workspace.SPEC_CONTEXT_MAX_BYTES``, pinned by the contract
#: test. It is the PRODUCT, not a round 16 KiB - the budget is stated in
#: tokens and the bytes are derived, never the other way round.
MAX_BYTES = 16000

#: A bundle every side MUST accept: a story-linked card with criteria and one
#: registered test, nothing truncated.
CANONICAL_BUNDLE = {
    "markdown": (
        "## Spec Context\n"
        "\n"
        "### Feature: Per-repo API rate limiting  (feature 4f2a1c9e)\n"
        "Protect the public API from runaway clients.\n"
        "\n"
        "### Story: Operator sets a per-repo request budget  (story 91bc77d0)\n"
        "As an operator I want to cap requests per repo per minute.\n"
        "\n"
        "### Acceptance criteria (1)\n"
        "- [required] (criterion a11b3f42) A repo over its budget gets 429.\n"
        "\n"
        "### Existing tests for these criteria (1)\n"
        '- tests/api/test_rate_limit.py  (criterion a11b3f42, lazyaf_test_id '
        '"rl-429", last run: passed)\n'
        "\n"
        "Paths are relative to the repository root (/workspace/repo).\n"
    ),
    "source": {
        "card_id": "c1d2e3f4-0000-0000-0000-000000000001",
        "feature_id": "4f2a1c9e-0000-0000-0000-000000000002",
        "user_story_id": "91bc77d0-0000-0000-0000-000000000003",
    },
    "criteria_count": 1,
    "test_ref_count": 1,
    "estimated_tokens": 137,
    "truncated": False,
    "dropped": [],
}

#: A truncated bundle every side MUST accept - the R1 case: the drop is
#: visible in the markdown AND named in the metadata.
TRUNCATED_BUNDLE = {
    "markdown": (
        "## Spec Context\n"
        "\n"
        "### Feature: Per-repo API rate limiting  (feature 4f2a1c9e)\n"
        "\n"
        "### Acceptance criteria (1)\n"
        "- [required] (criterion a11b3f42) A repo over its budget gets 429.\n"
        "\n"
        "> [spec context truncated to fit the 4000-token budget: "
        "criterion_notes, feature_description]\n"
        "\n"
        "Paths are relative to the repository root (/workspace/repo).\n"
    ),
    "source": {
        "card_id": "c1d2e3f4-0000-0000-0000-000000000001",
        "feature_id": "4f2a1c9e-0000-0000-0000-000000000002",
        "user_story_id": "91bc77d0-0000-0000-0000-000000000003",
    },
    "criteria_count": 1,
    "test_ref_count": 0,
    "estimated_tokens": 83,
    "truncated": True,
    "dropped": ["criterion_notes", "feature_description"],
}


def estimated_tokens_for(markdown: str) -> int:
    """The documented estimator, restated here so the contract is readable
    without importing the backend."""
    if not markdown:
        return 0
    return math.ceil(len(markdown.encode("utf-8")) / BYTES_PER_TOKEN)


def bundle_violations(bundle) -> list:
    """Human-readable contract violations (empty == valid).

    ``None`` is VALID - it is the contracted spelling of "no bundle".
    """
    if bundle is None:
        return []
    if not isinstance(bundle, dict):
        return [
            f"bundle is {type(bundle).__name__}; the only non-dict a bundle "
            "may be is None"
        ]

    problems = []
    keys = set(bundle)

    unknown = keys - set(TOP_LEVEL_KEYS)
    if unknown:
        problems.append(f"unknown top-level keys {sorted(unknown)}")

    forbidden = keys & set(FORBIDDEN_KEYS)
    if forbidden:
        problems.append(
            f"keys {sorted(forbidden)} are NOT on the 12.6.6 wire (the bundle "
            "is derived at dispatch and stored nowhere)"
        )

    missing = set(REQUIRED_KEYS) - keys
    if missing:
        problems.append(f"missing required keys {sorted(missing)}")

    markdown = bundle.get("markdown")
    if not isinstance(markdown, str) or not markdown:
        problems.append(
            f"markdown {markdown!r} is not a non-empty string - an empty "
            "bundle is spelled None, never {} and never ''"
        )
        markdown = ""

    size = len(markdown.encode("utf-8"))
    if size > MAX_BYTES:
        problems.append(
            f"markdown is {size} bytes, over the {MAX_BYTES}-byte budget "
            "(the prompt is ONE argv element; the kernel caps that)"
        )

    source = bundle.get("source")
    if not isinstance(source, dict):
        problems.append(f"source {source!r} is not an object")
    else:
        unknown_source = set(source) - set(SOURCE_KEYS)
        if unknown_source:
            problems.append(f"unknown source keys {sorted(unknown_source)}")
            missing_source = set(SOURCE_KEYS) - set(source)
        else:
            missing_source = set(SOURCE_KEYS) - set(source)
        if missing_source:
            problems.append(f"missing source keys {sorted(missing_source)}")
        for key, value in source.items():
            if value is not None and not isinstance(value, str):
                problems.append(f"source.{key} {value!r} is not str|None")

    for key in ("criteria_count", "test_ref_count", "estimated_tokens"):
        value = bundle.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{key} {value!r} is not an int")
        elif value < 0:
            problems.append(f"{key} {value!r} is negative")

    tokens = bundle.get("estimated_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool) and markdown:
        expected = estimated_tokens_for(markdown)
        if tokens != expected:
            problems.append(
                f"estimated_tokens {tokens} != ceil(bytes/{BYTES_PER_TOKEN}) "
                f"= {expected}"
            )

    truncated = bundle.get("truncated")
    if not isinstance(truncated, bool):
        problems.append(f"truncated {truncated!r} is not a bool")

    dropped = bundle.get("dropped")
    if not isinstance(dropped, list):
        problems.append(f"dropped {dropped!r} is not a list")
    else:
        unknown_rules = [d for d in dropped if d not in DROP_RULES]
        if unknown_rules:
            problems.append(
                f"dropped names rules that are not in the vocabulary: "
                f"{unknown_rules} (allowed: {list(DROP_RULES)})"
            )
        ranks = [DROP_RULES.index(d) for d in dropped if d in DROP_RULES]
        if ranks != sorted(ranks):
            problems.append(
                f"dropped {dropped} is out of DROP_RULES order - the order IS "
                "the contract"
            )
        if len(set(dropped)) != len(dropped):
            problems.append(f"dropped {dropped} repeats a rule")
        if isinstance(truncated, bool) and bool(dropped) != truncated:
            problems.append(
                f"truncated={truncated} but dropped={dropped} - the flag and "
                "the list must agree, or the operator cannot trust either"
            )
        if dropped and markdown and "truncated to fit" not in markdown:
            problems.append(
                "the bundle was truncated but says so nowhere IN the "
                "markdown - an agent reading a shrunk brief must be able to "
                "SEE that it is shrunk (R1)"
            )

    return problems


def assert_bundle_conforms(bundle, side: str) -> None:
    """Assert ``bundle`` matches the wire contract.

    ``side`` names WHO produced this value ("ASSEMBLER (spec_context)",
    "PRODUCER (agent config)", "CONSUMER (agent wrapper)") so a failure says
    which side drifted from the shared contract.
    """
    problems = bundle_violations(bundle)
    assert not problems, (
        f"{side} DRIFTED from the 12.6.6 spec-context contract "
        f"(tdd/unit/control_runtime/spec_context_contract.py):\n  - "
        + "\n  - ".join(problems)
        + f"\noffending value: {bundle!r}"
    )


#: Values NO side may accept as a bundle. Each is (label, value).
INVALID_BUNDLES = [
    ("empty dict", {}),
    ("bare string", "## Spec Context"),
    ("bare list", [CANONICAL_BUNDLE]),
    ("markdown missing", {k: v for k, v in CANONICAL_BUNDLE.items() if k != "markdown"}),
    ("markdown empty", {**CANONICAL_BUNDLE, "markdown": ""}),
    ("markdown is a dict", {**CANONICAL_BUNDLE, "markdown": {"text": "x"}}),
    ("source missing", {k: v for k, v in CANONICAL_BUNDLE.items() if k != "source"}),
    ("source is a string", {**CANONICAL_BUNDLE, "source": "card-1"}),
    (
        "source carries an extra key",
        {**CANONICAL_BUNDLE, "source": {**CANONICAL_BUNDLE["source"], "repo_id": "r1"}},
    ),
    ("counts are strings", {**CANONICAL_BUNDLE, "criteria_count": "1"}),
    ("negative count", {**CANONICAL_BUNDLE, "test_ref_count": -1}),
    ("estimated_tokens disagrees with the bytes", {**CANONICAL_BUNDLE, "estimated_tokens": 3}),
    ("truncated is not a bool", {**CANONICAL_BUNDLE, "truncated": "yes"}),
    ("dropped is not a list", {**CANONICAL_BUNDLE, "dropped": "criterion_notes"}),
    ("dropped names an unknown rule", {**TRUNCATED_BUNDLE, "dropped": ["everything"]}),
    (
        "dropped is out of order",
        {**TRUNCATED_BUNDLE, "dropped": ["feature_description", "criterion_notes"]},
    ),
    ("truncated true but nothing dropped", {**CANONICAL_BUNDLE, "truncated": True}),
    (
        "dropped non-empty but truncated false",
        {**CANONICAL_BUNDLE, "dropped": ["criterion_notes"]},
    ),
    (
        "truncated silently - no marker in the markdown",
        {
            **CANONICAL_BUNDLE,
            "truncated": True,
            "dropped": ["criterion_notes"],
        },
    ),
    ("carries a persisted id", {**CANONICAL_BUNDLE, "id": "sc-1"}),
    ("carries test source", {**CANONICAL_BUNDLE, "test_source": "def test_x(): ..."}),
]
