"""
Curated spec context for an agent step - Phase 12.6.6.

WHAT THIS IS. An agent step used to receive a card title, a card description
and (before 12.5) a 2000-byte head of the repo README. The spec layer (12.2.5)
and the test tie-back (12.2.6) put the actual INTENT in the database -
Feature -> UserStory -> AcceptanceCriterion, and the TestRefs that measure
those criteria - and this module turns the slice of it that belongs to ONE
card into the markdown block the prompt carries.

CURATED, not dumped. The whole point is that a bundle is a small, bounded,
deliberately chosen slice: the card's story, that story's parent feature, that
story's acceptance criteria, and the paths of the tests already registered
against those criteria. Sibling stories' criteria, other features, test source
code, TestRun history and the other cards on the same story are excluded ON
PURPOSE (see `_EXCLUSIONS` below) - they are the noise that makes a model hedge
and re-implement work another agent is doing right now.

DERIVED AT DISPATCH, STORED NOWHERE. The bundle is reproducible from the spec
tables plus the run's repo id. Persisting it would create a second source of
truth for the spec that goes stale the moment a criterion is edited (R3).

BUDGETED, LOUDLY. The prompt is one argv element and Linux caps that at
131072 bytes, so the bundle is capped at SPEC_CONTEXT_MAX_BYTES and every
truncation rule that fires leaves a marker IN the markdown, a name in
`dropped`, and `truncated: true` in the metadata (R1: an agent reading a shrunk
brief must be able to see that it is shrunk, and so must the operator reading
the step log).

NO LINKS -> `None`. Not `{}`, not a bundle with empty markdown, not an empty
`## Spec Context` heading. `None` is the one spelling of "this card has no spec
context", and the prompt it produces is byte-identical to the pre-12.6.6 one.

The constants live in `control_layer.workspace` (next to
PREVIOUS_STEP_LOGS_MAX_BYTES, for the same reason: the PRODUCER has to be able
to re-assert the cap without importing the DB layer). One definition, two
users.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.card import Card
from app.models.spec import AcceptanceCriterion, Feature, UserStory
from app.models.testref import TestRef, TestRefStatus, TestRun
from app.services.control_layer.workspace import (
    SPEC_CONTEXT_MAX_BYTES,
    SPEC_CONTEXT_MAX_STORY_TITLES,
    SPEC_CONTEXT_MAX_TEST_REFS,
    SPEC_CONTEXT_MAX_TOKENS,
    SPEC_CONTEXT_PATH,
    SPEC_CONTEXT_TRUNCATION_MARKER,
    estimate_spec_context_tokens,
)

logger = logging.getLogger(__name__)

#: Deliberate exclusions, written down so a later change has to argue with
#: them rather than discover them. Referenced by the bundle tests.
_EXCLUSIONS = (
    "test source code (paths only - the agent is sitting in the checkout)",
    "sibling stories' narratives and criteria on the story-linked path",
    "other features entirely",
    "TestRun history beyond the single latest status per ref",
    "other cards linked to the same story (parallel agent work - naming it "
    "invites duplicate implementation)",
    "TestRefs from other repos (their file_path does not exist here)",
    "orphan TestRefs and refs with no file_path",
)

#: Rendered when a test ref has never been observed. Never a blank.
_NEVER_RAN = "never"

#: How many test lines survive the first pass of the `test_refs` drop rule.
_TEST_REFS_SOFT_KEEP = 10

#: Appended INSIDE the narrative when rule 3 keeps only its head, so the cut
#: is visible where it happened and not only in the trailing notice.
_NARRATIVE_CUT_MARKER = "\n[...narrative truncated to fit the budget...]"

_PREAMBLE = (
    "## Spec Context\n"
    "\n"
    "This is the curated slice of the product spec for the card you are "
    "working on.\n"
    "It is the intent you are implementing - satisfy the acceptance criteria "
    "below.\n"
    f"The same text is on disk at {SPEC_CONTEXT_PATH}.\n"
)

_FOOTER = "Paths are relative to the repository root (/workspace/repo).\n"


def _short(value: Optional[str]) -> str:
    """The logging idiom already used throughout agent_run.py: first 8 chars."""
    return (value or "")[:8]


def _sort_key(row) -> Tuple[datetime, str]:
    """Deterministic order for rows with no ordering column.

    `AcceptanceCriterion` has neither `order` nor `position`, so
    `(created_at, id)` IS the order - stable across processes, and stable when
    two criteria were written in the same transaction.
    """
    return (getattr(row, "created_at", None) or datetime.min, row.id or "")


def _head_bytes(text: str, limit: int) -> str:
    """Keep the HEAD of `text` within `limit` UTF-8 bytes, on a codepoint
    boundary. Deliberately the opposite of `truncate_previous_step_logs`,
    which keeps the tail: a narrative states its intent in its opening lines,
    a log states its outcome in its last ones.
    """
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def _marker(what: str) -> str:
    return SPEC_CONTEXT_TRUNCATION_MARKER.format(
        tokens=SPEC_CONTEXT_MAX_TOKENS, what=what
    )


class _Bundle:
    """The resolved rows plus the drop state, rendered on demand.

    Assembly and truncation are separated on purpose: every drop rule mutates
    THIS, then `render()` re-measures. A rule that shrinks the wrong thing is
    then a visible diff in one place instead of a string-surgery bug.
    """

    def __init__(self) -> None:
        self.feature_id: Optional[str] = None
        self.feature_title: str = ""
        self.feature_description: str = ""
        self.story_id: Optional[str] = None
        self.story_title: str = ""
        self.story_narrative: str = ""
        self.narrative_truncated: bool = False
        # (id, text, required, notes)
        self.criteria: List[Tuple[str, str, bool, Optional[str]]] = []
        self.dropped_optional_criteria: int = 0
        self.dropped_required_criteria: int = 0
        # (title, id)
        self.story_titles: List[Tuple[str, str]] = []
        self.story_titles_total: int = 0
        # (file_path, criterion_id, lazyaf_test_id, last_status)
        self.test_refs: List[Tuple[str, Optional[str], str, str]] = []
        self.test_refs_total: int = 0
        self.dropped: List[str] = []

    # -- rendering ---------------------------------------------------------

    def render(self) -> str:
        parts: List[str] = [_PREAMBLE]

        if self.feature_id:
            parts.append(
                f"\n### Feature: {self.feature_title}  "
                f"(feature {_short(self.feature_id)})\n"
            )
            if self.feature_description:
                parts.append(f"{self.feature_description.rstrip()}\n")

        if self.story_id:
            parts.append(
                f"\n### Story: {self.story_title}  "
                f"(story {_short(self.story_id)})\n"
            )
            if self.story_narrative:
                narrative = self.story_narrative.rstrip()
                if self.narrative_truncated:
                    narrative += _NARRATIVE_CUT_MARKER
                parts.append(f"{narrative}\n")
            parts.append(self._criteria_block())
        else:
            parts.append(self._story_titles_block())

        parts.append(self._tests_block())
        parts.append(self._notice_block())
        parts.append(f"\n{_FOOTER}")
        return "".join(parts)

    def _criteria_block(self) -> str:
        lines = [f"\n### Acceptance criteria ({len(self.criteria)})\n"]
        if not self.criteria and not (
            self.dropped_optional_criteria or self.dropped_required_criteria
        ):
            lines.append(
                "No acceptance criteria have been written for this story yet.\n"
            )
            return "".join(lines)
        for criterion_id, text, required, notes in self.criteria:
            flag = "required" if required else "optional"
            lines.append(
                f"- [{flag}] (criterion {_short(criterion_id)}) "
                f"{' '.join(text.split())}\n"
            )
            if notes:
                lines.append(f"  note: {' '.join(notes.split())}\n")
        if self.dropped_optional_criteria:
            lines.append(
                f"- [...{self.dropped_optional_criteria} optional criteria "
                "omitted - the spec exceeds the context budget...]\n"
            )
        if self.dropped_required_criteria:
            lines.append(
                f"- [...{self.dropped_required_criteria} further REQUIRED "
                "criteria omitted - the spec exceeds the context budget; read "
                "the story in LazyAF before you finish...]\n"
            )
        return "".join(lines)

    def _story_titles_block(self) -> str:
        """The feature-only path.

        Sibling stories OF THE LINKED FEATURE are in scope by definition - the
        agent needs the shape of the feature so it does not re-implement one -
        but their criteria and narratives are not. Walking every story's
        criteria is exactly the dump this phase exists to avoid.
        """
        lines = [
            "\nThis card is linked to the FEATURE, not to one story, so no "
            "acceptance\ncriteria are attached to it.\n"
        ]
        if self.story_titles:
            lines.append(
                f"\n### Stories in this feature ({self.story_titles_total})\n"
            )
            for title, story_id in self.story_titles:
                lines.append(f"- {title}  (story {_short(story_id)})\n")
            if self.story_titles_total > len(self.story_titles):
                omitted = self.story_titles_total - len(self.story_titles)
                lines.append(f"- [...{omitted} further stories omitted...]\n")
        return "".join(lines)

    def _tests_block(self) -> str:
        if not self.test_refs:
            return ""
        lines = [
            f"\n### Existing tests for these criteria ({len(self.test_refs)})\n",
            "Read these before writing new ones - they already cover the "
            "criteria named.\n",
        ]
        for file_path, criterion_id, test_id, status in self.test_refs:
            lines.append(
                f"- {file_path}  (criterion {_short(criterion_id)}, "
                f'lazyaf_test_id "{test_id}", last run: {status})\n'
            )
        if self.test_refs_total > len(self.test_refs):
            omitted = self.test_refs_total - len(self.test_refs)
            lines.append(f"- [...{omitted} further tests omitted...]\n")
        return "".join(lines)

    def _notice_block(self) -> str:
        """R1: a shrunk brief says so, in the brief."""
        if not self.dropped:
            return ""
        return "\n" + _marker(", ".join(self.dropped))

    # -- metadata ----------------------------------------------------------

    def to_payload(self, card_id: str) -> Dict[str, Any]:
        markdown = self.render()
        return {
            "markdown": markdown,
            "source": {
                "card_id": card_id,
                "feature_id": self.feature_id,
                "user_story_id": self.story_id,
            },
            "criteria_count": len(self.criteria),
            "test_ref_count": len(self.test_refs),
            "estimated_tokens": estimate_spec_context_tokens(markdown),
            "truncated": bool(self.dropped),
            "dropped": list(self.dropped),
        }


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------

def _size(bundle: _Bundle) -> int:
    return len(bundle.render().encode("utf-8"))


def _fit(bundle: _Bundle) -> None:
    """Reduce `bundle` until it fits SPEC_CONTEXT_MAX_BYTES.

    Rules fire IN ORDER, each re-measuring before the next, each recording its
    own name in `dropped`. The order is the argument of the phase: notes are
    supplementary, a feature description is context, a narrative states its
    intent up front, test paths are cheap, optional criteria are optional -
    and REQUIRED CRITERIA ARE THE CONTRACT and go last, never all of them.
    """
    if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
        return

    # 1. criterion notes
    if any(notes for _i, _t, _r, notes in bundle.criteria):
        bundle.criteria = [
            (cid, text, required, None)
            for cid, text, required, _notes in bundle.criteria
        ]
        bundle.dropped.append("criterion_notes")
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return

    # 2. feature description (the TITLE always survives)
    if bundle.feature_description:
        bundle.feature_description = ""
        bundle.dropped.append("feature_description")
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return

    # 3. story narrative - HEAD kept (see _head_bytes)
    if bundle.story_narrative:
        full = bundle.story_narrative
        bundle.story_narrative = ""
        bundle.narrative_truncated = True
        # The rule's own name goes into `dropped` BEFORE the measurement: the
        # notice block is part of the bundle, so measuring without it would
        # hand the narrative an allowance the finished markdown cannot honour.
        bundle.dropped.append("story_narrative")
        # Room left for the narrative itself, once the inline cut marker and
        # the block's own trailing newline are paid for. Measuring the
        # rendered bundle WITHOUT the narrative is what makes this exact
        # rather than a guess that then has to be retried.
        without = _size(bundle)
        overhead = len(_NARRATIVE_CUT_MARKER.encode("utf-8")) + 1
        allowance = SPEC_CONTEXT_MAX_BYTES - without - overhead
        kept = _head_bytes(full, allowance) if allowance > 0 else ""
        bundle.story_narrative = kept
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return
        bundle.story_narrative = ""

    # 4. sibling story titles (feature-only path)
    if bundle.story_titles:
        bundle.story_titles = []
        bundle.dropped.append("story_titles")
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return

    # 5. test refs - soft keep, then all
    if bundle.test_refs:
        if len(bundle.test_refs) > _TEST_REFS_SOFT_KEEP:
            bundle.test_refs = bundle.test_refs[:_TEST_REFS_SOFT_KEEP]
            bundle.dropped.append("test_refs")
            if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
                return
        bundle.test_refs = []
        if "test_refs" not in bundle.dropped:
            bundle.dropped.append("test_refs")
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return

    # 6. optional criteria, replaced by one count line
    optional = [c for c in bundle.criteria if not c[2]]
    if optional:
        bundle.criteria = [c for c in bundle.criteria if c[2]]
        bundle.dropped_optional_criteria = len(optional)
        bundle.dropped.append("optional_criteria")
        if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
            return

    # 7. LAST RESORT: trailing required criteria. Never all of them - a bundle
    #    with no contract in it is worse than no bundle.
    while len(bundle.criteria) > 1 and _size(bundle) > SPEC_CONTEXT_MAX_BYTES:
        bundle.criteria = bundle.criteria[:-1]
        bundle.dropped_required_criteria += 1
        if "required_criteria" not in bundle.dropped:
            bundle.dropped.append("required_criteria")
    if _size(bundle) <= SPEC_CONTEXT_MAX_BYTES:
        return

    # 8. A single pathological criterion can still be over. The cap is a
    #    FACT, not an intention.
    bundle.dropped.append("hard_clamp")


def _clamp(markdown: str) -> str:
    tail = "\n" + _marker("hard clamped at the byte cap")
    room = SPEC_CONTEXT_MAX_BYTES - len(tail.encode("utf-8"))
    return _head_bytes(markdown, room) + tail


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

async def _latest_run_status(
    db: AsyncSession, ref_ids: List[str]
) -> Dict[str, str]:
    """Latest observed TestRun status per ref, in ONE bounded query.

    Both legs ride ix_test_runs_test_ref_id_created_at. Timestamp ties yield
    duplicate rows; the FIRST under the (test_ref_id, id) order wins, which is
    deterministic. A ref with no runs is simply absent and renders as
    `never` - never a blank.
    """
    if not ref_ids:
        return {}
    latest = (
        select(TestRun.test_ref_id, func.max(TestRun.created_at).label("ts"))
        .where(TestRun.test_ref_id.in_(ref_ids))
        .group_by(TestRun.test_ref_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(TestRun.test_ref_id, TestRun.status)
            .join(
                latest,
                and_(
                    TestRun.test_ref_id == latest.c.test_ref_id,
                    TestRun.created_at == latest.c.ts,
                ),
            )
            .order_by(TestRun.test_ref_id, TestRun.id)
        )
    ).all()
    statuses: Dict[str, str] = {}
    for ref_id, status in rows:
        statuses.setdefault(ref_id, status)
    return statuses


async def _load_test_refs(
    db: AsyncSession, criterion_ids: List[str], repo_id: str
) -> Tuple[List[Tuple[str, Optional[str], str, str]], int]:
    """Active, path-carrying TestRefs of these criteria IN THIS REPO.

    REPO SCOPING IS A CORRECTNESS RULE, NOT A FILTER. TestRef identity is the
    pair (repo_id, lazyaf_test_id) and `file_path` is repo-root-relative, so a
    ref from another repo names a path that does not exist in this workspace.
    "Go read this file" must never be a lie - which is also why `orphan` refs
    and refs with a NULL file_path are excluded.
    """
    if not criterion_ids:
        return [], 0

    total = (
        await db.execute(
            select(func.count())
            .select_from(TestRef)
            .where(
                TestRef.criterion_id.in_(criterion_ids),
                TestRef.repo_id == repo_id,
                TestRef.status == TestRefStatus.ACTIVE.value,
                TestRef.file_path.is_not(None),
            )
        )
    ).scalar_one()

    refs = (
        (
            await db.execute(
                select(TestRef)
                .where(
                    TestRef.criterion_id.in_(criterion_ids),
                    TestRef.repo_id == repo_id,
                    TestRef.status == TestRefStatus.ACTIVE.value,
                    TestRef.file_path.is_not(None),
                )
                .order_by(TestRef.file_path, TestRef.lazyaf_test_id)
                .limit(SPEC_CONTEXT_MAX_TEST_REFS)
            )
        )
        .scalars()
        .all()
    )
    statuses = await _latest_run_status(db, [r.id for r in refs])
    rendered = [
        (
            ref.file_path,
            ref.criterion_id,
            ref.lazyaf_test_id,
            statuses.get(ref.id, _NEVER_RAN),
        )
        for ref in refs
    ]
    return rendered, int(total)


async def build_spec_context(
    db: AsyncSession, *, card_id: Optional[str], repo_id: str
) -> Optional[Dict[str, Any]]:
    """Assemble the curated spec bundle for one card, or `None`.

    `None` - never `{}`, never a bundle with empty markdown - is the clean
    no-op: no card id, no card row, no spec links, or a linked row that has
    since been deleted. Every `None` that is not simply "this card has no spec
    links" is preceded by one WARNING naming the id, so a broken link is
    visible without reading a container's stdout (R1).

    `repo_id` is REQUIRED and is the RUN's repo, not the card's: the workspace
    is checked out at the run's repo, and a TestRef path is only meaningful
    there. A card whose repo disagrees gets one WARNING and the run's repo
    wins - the workspace is the authority.

    Returns the wire payload documented in `control_layer.workspace`'s
    `generate_agent_config` (`markdown` + provenance + size/truncation facts),
    already inside SPEC_CONTEXT_MAX_BYTES.
    """
    if not card_id:
        return None

    card = (
        await db.execute(select(Card).where(Card.id == card_id))
    ).scalar_one_or_none()
    if card is None:
        logger.warning(
            "spec context: card %s does not exist; dispatching with no "
            "curated context",
            card_id,
        )
        return None

    if card.repo_id and repo_id and card.repo_id != repo_id:
        logger.warning(
            "spec context: card %s belongs to repo %s but the run is checked "
            "out at repo %s; scoping test refs to the RUN's repo - the "
            "workspace is the authority for what paths exist",
            card_id,
            card.repo_id,
            repo_id,
        )

    if not card.user_story_id and not card.feature_id:
        return None

    bundle = _Bundle()

    if card.user_story_id:
        story = (
            await db.execute(
                select(UserStory)
                .options(
                    selectinload(UserStory.criteria),
                    selectinload(UserStory.feature),
                )
                .where(UserStory.id == card.user_story_id)
            )
        ).scalar_one_or_none()
        if story is None:
            logger.warning(
                "spec context: card %s links user story %s, which does not "
                "exist; dispatching with no curated context",
                card_id,
                card.user_story_id,
            )
            return None

        bundle.story_id = story.id
        bundle.story_title = story.title or ""
        bundle.story_narrative = story.narrative or ""

        # The STORY's parent wins a link conflict: the story is the more
        # precise link. The discrepancy is not hidden, it is logged.
        feature = story.feature
        if (
            card.feature_id
            and story.feature_id
            and card.feature_id != story.feature_id
        ):
            logger.warning(
                "spec context: card %s links feature %s but its story %s "
                "belongs to feature %s; the story's parent wins",
                card_id,
                card.feature_id,
                story.id,
                story.feature_id,
            )
        if feature is not None:
            bundle.feature_id = feature.id
            bundle.feature_title = feature.title or ""
            bundle.feature_description = feature.description or ""

        criteria = sorted(story.criteria, key=_sort_key)
        bundle.criteria = [
            (c.id, c.text or "", bool(c.required), c.notes) for c in criteria
        ]
        bundle.test_refs, bundle.test_refs_total = await _load_test_refs(
            db, [c.id for c in criteria], repo_id
        )
    else:
        feature = (
            await db.execute(
                select(Feature)
                .options(selectinload(Feature.stories))
                .where(Feature.id == card.feature_id)
            )
        ).scalar_one_or_none()
        if feature is None:
            logger.warning(
                "spec context: card %s links feature %s, which does not "
                "exist; dispatching with no curated context",
                card_id,
                card.feature_id,
            )
            return None
        bundle.feature_id = feature.id
        bundle.feature_title = feature.title or ""
        bundle.feature_description = feature.description or ""
        stories = sorted(feature.stories, key=_sort_key)
        bundle.story_titles_total = len(stories)
        bundle.story_titles = [
            (s.title or "", s.id) for s in stories[:SPEC_CONTEXT_MAX_STORY_TITLES]
        ]

    _fit(bundle)
    payload = bundle.to_payload(card_id)
    if len(payload["markdown"].encode("utf-8")) > SPEC_CONTEXT_MAX_BYTES:
        payload["markdown"] = _clamp(payload["markdown"])
        payload["estimated_tokens"] = estimate_spec_context_tokens(
            payload["markdown"]
        )
        payload["truncated"] = True
        if "hard_clamp" not in payload["dropped"]:
            payload["dropped"].append("hard_clamp")
    return payload
