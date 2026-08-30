"""
Unit tests for the 12.6.6 CURATED SPEC CONTEXT assembler.

`app.services.spec_context.build_spec_context` is the ASSEMBLER side of the
wire contract in `tdd/unit/control_runtime/spec_context_contract.py`; the
producer/consumer sides are driven in
`tdd/unit/control_runtime/test_spec_context_contract.py`. Every bundle this
module builds is run through `assert_bundle_conforms`, so an assembler that
starts emitting a shape the wrapper cannot read fails HERE, naming the side
that drifted.

Real async session, real rows, no mocks - the queries (repo scoping, the
latest-run join, the eager loads) ARE the thing under test, and a mocked
session would pin nothing about them.

What this file exists to prevent, in one line each:
- a bundle that dumps the whole spec instead of curating a slice,
- a bundle that names a test file that does not exist in this workspace,
- a bundle that shrinks silently under budget pressure,
- a bundle that drops the acceptance criteria - the contract - first,
- a card with no spec links producing anything other than `None`.
"""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (REPO_ROOT / "backend", REPO_ROOT / "runner-common"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.models.card import Card  # noqa: E402
from app.models.repo import Repo  # noqa: E402
from app.models.spec import (  # noqa: E402
    AcceptanceCriterion,
    Feature,
    UserStory,
)
from app.models.testref import (  # noqa: E402
    TestRef,
    TestRefStatus,
    TestRun,
    TestRunStatus,
)
from app.services.control_layer.workspace import (  # noqa: E402
    SPEC_CONTEXT_MAX_BYTES,
    SPEC_CONTEXT_MAX_TOKENS,
    estimate_spec_context_tokens,
)
from app.services.spec_context import build_spec_context  # noqa: E402

from tdd.unit.control_runtime.spec_context_contract import (  # noqa: E402
    DROP_RULES,
    assert_bundle_conforms,
)

SERVICE_LOGGER = "app.services.spec_context"


# ---------------------------------------------------------------------------
# builders - plain rows, no factories, so each test reads as its own fixture
# ---------------------------------------------------------------------------

class SpecWorld:
    """One repo, one feature, one story, and whatever else a test adds."""

    def __init__(self, db):
        self.db = db
        self.base_time = datetime(2026, 8, 30, 12, 0, 0)
        self._tick = 0

    def _next_time(self) -> datetime:
        self._tick += 1
        return self.base_time + timedelta(seconds=self._tick)

    async def repo(self, name="app") -> Repo:
        row = Repo(id=str(uuid4()), name=name, default_branch="main")
        self.db.add(row)
        await self.db.flush()
        return row

    async def feature(self, title="Per-repo API rate limiting", description="") -> Feature:
        row = Feature(
            id=str(uuid4()),
            title=title,
            description=description,
            repo_ids="[]",
            created_at=self._next_time(),
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def story(self, feature, title="Operator sets a budget", narrative="") -> UserStory:
        row = UserStory(
            id=str(uuid4()),
            feature_id=feature.id,
            title=title,
            narrative=narrative,
            created_at=self._next_time(),
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def criterion(
        self, story, text, required=True, notes=None
    ) -> AcceptanceCriterion:
        row = AcceptanceCriterion(
            id=str(uuid4()),
            user_story_id=story.id,
            text=text,
            required=required,
            notes=notes,
            created_at=self._next_time(),
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def card(self, repo, story=None, feature=None) -> Card:
        row = Card(
            id=str(uuid4()),
            repo_id=repo.id,
            title="Implement the budget",
            description="",
            user_story_id=story.id if story is not None else None,
            feature_id=feature.id if feature is not None else None,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def test_ref(
        self,
        repo,
        criterion=None,
        file_path="tests/api/test_rate_limit.py",
        lazyaf_test_id=None,
        status=TestRefStatus.ACTIVE.value,
    ) -> TestRef:
        row = TestRef(
            id=str(uuid4()),
            lazyaf_test_id=lazyaf_test_id or f"t-{uuid4().hex[:6]}",
            repo_id=repo.id,
            file_path=file_path,
            criterion_id=criterion.id if criterion is not None else None,
            status=status,
        )
        self.db.add(row)
        await self.db.flush()
        return row

    async def test_run(self, ref, status, at=None) -> TestRun:
        row = TestRun(
            id=str(uuid4()),
            test_ref_id=ref.id,
            pipeline_run_id=str(uuid4()),
            commit_sha="",
            status=status,
            created_at=at or self._next_time(),
        )
        self.db.add(row)
        await self.db.flush()
        return row


@pytest.fixture
def world(db_session):
    return SpecWorld(db_session)


async def _bundle(db, card, repo, **kwargs):
    """Build a bundle and hold it to the shared wire contract every time."""
    payload = await build_spec_context(
        db, card_id=card.id if card is not None else None, repo_id=repo.id, **kwargs
    )
    assert_bundle_conforms(payload, "ASSEMBLER (spec_context)")
    return payload


# ---------------------------------------------------------------------------
# what is IN the bundle
# ---------------------------------------------------------------------------

class TestBundleContents:
    async def test_card_with_story_link_pulls_narrative(self, db_session, world):
        repo = await world.repo()
        feature = await world.feature()
        story = await world.story(
            feature,
            title="Operator sets a per-repo request budget",
            narrative=(
                "As an operator I want to cap requests per repo per minute\n"
                "so that one misbehaving integration cannot starve the others."
            ),
        )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload is not None
        assert "Operator sets a per-repo request budget" in payload["markdown"]
        assert "cannot starve the others" in payload["markdown"]
        assert payload["source"]["user_story_id"] == story.id
        assert payload["source"]["card_id"] == card.id

    async def test_bundle_includes_all_criteria_in_created_order(
        self, db_session, world
    ):
        repo = await world.repo()
        story = await world.story(await world.feature())
        first = await world.criterion(story, "A repo over its budget gets 429.")
        second = await world.criterion(story, "The 429 body names retry-after.")
        third = await world.criterion(
            story, "Headers are emitted on every response.", required=False
        )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)
        markdown = payload["markdown"]

        assert payload["criteria_count"] == 3
        assert markdown.index(first.text) < markdown.index(second.text)
        assert markdown.index(second.text) < markdown.index(third.text)
        assert "[required]" in markdown and "[optional]" in markdown

    async def test_criterion_ids_are_present_so_an_agent_can_name_them(
        self, db_session, world
    ):
        """The integration check is 'logs show the agent referenced criteria
        by name', and an agent registering a new lazyaf_test_id against a
        criterion needs the id to put in the marker. ~36 bytes each."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert f"criterion {criterion.id[:8]}" in payload["markdown"]

    async def test_criterion_notes_are_included(self, db_session, world):
        repo = await world.repo()
        story = await world.story(await world.feature())
        await world.criterion(
            story,
            "A repo over its budget gets 429.",
            notes="the budget is per minute, not per hour",
        )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert "note: the budget is per minute, not per hour" in payload["markdown"]

    async def test_bundle_includes_parent_feature_description(
        self, db_session, world
    ):
        """Derived via `UserStory.feature_id`, so it works even when
        `Card.feature_id` is null - which is the common case."""
        repo = await world.repo()
        feature = await world.feature(
            title="Per-repo API rate limiting",
            description="Protect the public API from runaway clients.",
        )
        story = await world.story(feature)
        card = await world.card(repo, story=story)
        assert card.feature_id is None

        payload = await _bundle(db_session, card, repo)

        assert "Per-repo API rate limiting" in payload["markdown"]
        assert "Protect the public API from runaway clients." in payload["markdown"]
        assert payload["source"]["feature_id"] == feature.id

    async def test_bundle_includes_related_test_paths(self, db_session, world):
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        await world.test_ref(
            repo,
            criterion=criterion,
            file_path="tests/api/test_rate_limit.py",
            lazyaf_test_id="rl-429",
        )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["test_ref_count"] == 1
        assert "tests/api/test_rate_limit.py" in payload["markdown"]
        assert 'lazyaf_test_id "rl-429"' in payload["markdown"]

    async def test_bundle_includes_last_run_status_per_test(
        self, db_session, world
    ):
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        ref = await world.test_ref(
            repo, criterion=criterion, lazyaf_test_id="rl-429"
        )
        never = await world.test_ref(
            repo,
            criterion=criterion,
            file_path="tests/api/test_headers.py",
            lazyaf_test_id="rl-headers",
        )
        await world.test_run(ref, TestRunStatus.PASSED.value)
        await world.test_run(ref, TestRunStatus.FAILED.value)  # later == latest
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)
        markdown = payload["markdown"]

        assert 'lazyaf_test_id "rl-429", last run: failed' in markdown
        assert 'lazyaf_test_id "rl-headers", last run: never' in markdown
        assert never.id  # the pathless/never case is a real row, not a gap


# ---------------------------------------------------------------------------
# what is deliberately OUT
# ---------------------------------------------------------------------------

class TestBundleExclusions:
    async def test_bundle_omits_unrelated_features(self, db_session, world):
        repo = await world.repo()
        feature = await world.feature()
        story = await world.story(feature)
        await world.criterion(story, "A repo over budget gets 429.")

        other_feature = await world.feature(
            title="ZZZ Unrelated billing engine",
            description="UNRELATED-FEATURE-DESCRIPTION",
        )
        other_story = await world.story(
            other_feature, title="ZZZ Unrelated story", narrative="UNRELATED-NARRATIVE"
        )
        await world.criterion(other_story, "UNRELATED-CRITERION")

        card = await world.card(repo, story=story)
        payload = await _bundle(db_session, card, repo)

        for leak in (
            "ZZZ Unrelated billing engine",
            "UNRELATED-FEATURE-DESCRIPTION",
            "ZZZ Unrelated story",
            "UNRELATED-NARRATIVE",
            "UNRELATED-CRITERION",
        ):
            assert leak not in payload["markdown"], leak

    async def test_bundle_omits_sibling_story_detail_on_the_story_path(
        self, db_session, world
    ):
        """The story link is precise. A sibling story's narrative and criteria
        are the noise that makes a model hedge."""
        repo = await world.repo()
        feature = await world.feature()
        story = await world.story(feature)
        await world.criterion(story, "A repo over budget gets 429.")
        sibling = await world.story(
            feature, title="SIBLING-TITLE", narrative="SIBLING-NARRATIVE"
        )
        await world.criterion(sibling, "SIBLING-CRITERION")

        card = await world.card(repo, story=story)
        payload = await _bundle(db_session, card, repo)

        assert "SIBLING-NARRATIVE" not in payload["markdown"]
        assert "SIBLING-CRITERION" not in payload["markdown"]

    async def test_bundle_omits_test_refs_from_other_repos(
        self, db_session, world
    ):
        """TestRef identity is (repo_id, lazyaf_test_id) and `file_path` is
        repo-root-relative, so a ref from another repo names a path that does
        not exist in this workspace. 'Go read this file' must never be a lie.
        """
        repo = await world.repo(name="app")
        other_repo = await world.repo(name="other")
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        await world.test_ref(
            repo, criterion=criterion, file_path="tests/mine.py", lazyaf_test_id="a"
        )
        await world.test_ref(
            other_repo,
            criterion=criterion,
            file_path="tests/NOT-IN-THIS-WORKSPACE.py",
            lazyaf_test_id="a",
        )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert "tests/mine.py" in payload["markdown"]
        assert "NOT-IN-THIS-WORKSPACE" not in payload["markdown"]
        assert payload["test_ref_count"] == 1

    async def test_bundle_omits_orphan_and_pathless_test_refs(
        self, db_session, world
    ):
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        await world.test_ref(
            repo,
            criterion=criterion,
            file_path="tests/ORPHANED.py",
            status=TestRefStatus.ORPHAN.value,
        )
        await world.test_ref(repo, criterion=criterion, file_path=None)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert "ORPHANED" not in payload["markdown"]
        assert payload["test_ref_count"] == 0

    async def test_bundle_never_carries_test_source_or_run_history(
        self, db_session, world
    ):
        """PLAN's open question, defaulting to paths: full files would spend
        the whole budget on two tests, and the agent is in the checkout."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        ref = await world.test_ref(repo, criterion=criterion, lazyaf_test_id="rl-429")
        await world.test_run(ref, TestRunStatus.PASSED.value)
        await world.test_run(ref, TestRunStatus.FAILED.value)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        # exactly ONE status word per test line, not a history
        assert payload["markdown"].count("last run:") == 1
        assert "def test_" not in payload["markdown"]


# ---------------------------------------------------------------------------
# the None paths
# ---------------------------------------------------------------------------

class TestNoSpecLinks:
    async def test_card_without_links_is_none(self, db_session, world):
        repo = await world.repo()
        card = await world.card(repo)

        assert await _bundle(db_session, card, repo) is None

    async def test_missing_card_is_none_with_a_warning(
        self, db_session, world, caplog
    ):
        repo = await world.repo()
        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            payload = await build_spec_context(
                db_session, card_id="does-not-exist", repo_id=repo.id
            )

        assert payload is None
        assert "does-not-exist" in caplog.text

    async def test_no_card_id_is_none_without_a_query(self, db_session, world):
        repo = await world.repo()
        assert (
            await build_spec_context(db_session, card_id=None, repo_id=repo.id)
            is None
        )

    async def test_dangling_story_link_is_none_with_a_warning(
        self, db_session, world, caplog
    ):
        repo = await world.repo()
        card = await world.card(repo)
        card.user_story_id = "gone-" + uuid4().hex[:8]
        await db_session.flush()

        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            payload = await build_spec_context(
                db_session, card_id=card.id, repo_id=repo.id
            )

        assert payload is None
        assert "user story" in caplog.text

    async def test_story_with_no_criteria_is_still_a_bundle(
        self, db_session, world
    ):
        """Feature + story + narrative is real intent. The empty criteria
        section SAYS it is empty rather than being omitted (R1)."""
        repo = await world.repo()
        story = await world.story(await world.feature(), narrative="Some intent.")
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload is not None
        assert payload["criteria_count"] == 0
        assert "### Acceptance criteria (0)" in payload["markdown"]
        assert "No acceptance criteria have been written" in payload["markdown"]


# ---------------------------------------------------------------------------
# link resolution edge cases
# ---------------------------------------------------------------------------

class TestLinkResolution:
    async def test_feature_only_link_lists_story_titles_not_criteria(
        self, db_session, world
    ):
        repo = await world.repo()
        feature = await world.feature(description="Protect the public API.")
        story = await world.story(feature, title="Operator sets a budget")
        await world.criterion(story, "CRITERION-MUST-NOT-APPEAR")
        card = await world.card(repo, feature=feature)

        payload = await _bundle(db_session, card, repo)
        markdown = payload["markdown"]

        assert "Operator sets a budget" in markdown
        assert f"story {story.id[:8]}" in markdown
        assert "CRITERION-MUST-NOT-APPEAR" not in markdown
        assert payload["criteria_count"] == 0
        assert payload["source"]["user_story_id"] is None
        assert "linked to the FEATURE" in markdown

    async def test_story_link_beats_conflicting_feature_link(
        self, db_session, world, caplog
    ):
        repo = await world.repo()
        story_feature = await world.feature(title="THE STORY PARENT")
        other_feature = await world.feature(title="THE CARD FEATURE")
        story = await world.story(story_feature)
        card = await world.card(repo, story=story, feature=other_feature)

        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            payload = await _bundle(db_session, card, repo)

        assert payload["source"]["feature_id"] == story_feature.id
        assert "THE STORY PARENT" in payload["markdown"]
        assert "THE CARD FEATURE" not in payload["markdown"]
        assert story_feature.id in caplog.text and other_feature.id in caplog.text

    async def test_repo_mismatch_warns_and_the_runs_repo_wins(
        self, db_session, world, caplog
    ):
        """The workspace is checked out at the RUN's repo; a TestRef path is
        only meaningful there."""
        card_repo = await world.repo(name="card-repo")
        run_repo = await world.repo(name="run-repo")
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        await world.test_ref(
            card_repo, criterion=criterion, file_path="tests/CARD-REPO.py"
        )
        await world.test_ref(
            run_repo, criterion=criterion, file_path="tests/RUN-REPO.py"
        )
        card = await world.card(card_repo, story=story)

        with caplog.at_level(logging.WARNING, logger=SERVICE_LOGGER):
            payload = await _bundle(db_session, card, run_repo)

        assert "tests/RUN-REPO.py" in payload["markdown"]
        assert "CARD-REPO" not in payload["markdown"]
        assert run_repo.id in caplog.text


# ---------------------------------------------------------------------------
# the budget
# ---------------------------------------------------------------------------

class TestBudget:
    async def test_estimated_tokens_matches_the_byte_heuristic(
        self, db_session, world
    ):
        repo = await world.repo()
        story = await world.story(await world.feature(), narrative="Some intent.")
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["estimated_tokens"] == estimate_spec_context_tokens(
            payload["markdown"]
        )

    async def test_a_small_bundle_is_not_truncated(self, db_session, world):
        repo = await world.repo()
        story = await world.story(await world.feature(), narrative="Short.")
        await world.criterion(story, "A repo over budget gets 429.", notes="n")
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["truncated"] is False
        assert payload["dropped"] == []
        assert "truncated to fit" not in payload["markdown"]

    async def test_bundle_size_is_capped_in_bytes(self, db_session, world):
        repo = await world.repo()
        feature = await world.feature(description="D" * 30_000)
        story = await world.story(feature, narrative="N" * 30_000)
        for index in range(60):
            await world.criterion(
                story, f"criterion {index} " + "C" * 400, notes="X" * 400
            )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert len(payload["markdown"].encode("utf-8")) <= SPEC_CONTEXT_MAX_BYTES
        assert payload["truncated"] is True

    async def test_cap_is_bytes_not_characters(self, db_session, world):
        """A spec written in a multi-byte script cannot blow the argv cap."""
        repo = await world.repo()
        story = await world.story(await world.feature(), narrative="é" * 20_000)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert len(payload["markdown"].encode("utf-8")) <= SPEC_CONTEXT_MAX_BYTES

    async def test_truncation_is_announced_in_markdown_and_metadata(
        self, db_session, world
    ):
        """R1: an agent reading a shrunk brief must be able to SEE that it is
        shrunk, and so must the operator reading the step log."""
        repo = await world.repo()
        story = await world.story(await world.feature(), narrative="N" * 40_000)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["truncated"] is True
        assert payload["dropped"]
        assert all(name in DROP_RULES for name in payload["dropped"])
        assert f"{SPEC_CONTEXT_MAX_TOKENS}-token budget" in payload["markdown"]

    async def test_truncation_drops_notes_before_criteria(
        self, db_session, world
    ):
        """Drop order, rule 1 before rule 6/7: notes are supplementary, the
        criteria are the contract."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        for index in range(40):
            await world.criterion(
                story, f"CRITERION-{index} " + "C" * 200, notes="N" * 600
            )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["dropped"][0] == "criterion_notes"
        assert "note: " not in payload["markdown"]
        assert "CRITERION-0" in payload["markdown"]

    async def test_optional_criteria_go_before_required_ones(
        self, db_session, world
    ):
        repo = await world.repo()
        story = await world.story(await world.feature())
        for index in range(30):
            await world.criterion(story, f"REQUIRED-{index} " + "R" * 400)
        for index in range(30):
            await world.criterion(
                story, f"OPTIONAL-{index} " + "O" * 400, required=False
            )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert "optional_criteria" in payload["dropped"]
        assert "OPTIONAL-0" not in payload["markdown"]
        assert "REQUIRED-0" in payload["markdown"]
        assert payload["dropped"].index("optional_criteria") < len(
            payload["dropped"]
        )

    async def test_truncation_never_drops_every_required_criterion(
        self, db_session, world
    ):
        """A bundle with no contract in it is worse than no bundle."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        for index in range(40):
            await world.criterion(story, f"REQUIRED-{index} " + "R" * 900)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["criteria_count"] >= 1
        assert "REQUIRED-0" in payload["markdown"]
        assert "further REQUIRED" in payload["markdown"]

    async def test_a_single_pathological_criterion_is_hard_clamped(
        self, db_session, world
    ):
        """Rule 8. The byte cap is a FACT, not an intention."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        await world.criterion(story, "P" * 40_000)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert len(payload["markdown"].encode("utf-8")) <= SPEC_CONTEXT_MAX_BYTES
        assert "hard_clamp" in payload["dropped"]

    async def test_story_narrative_is_head_kept_on_truncation(
        self, db_session, world
    ):
        """Deliberately the opposite of `truncate_previous_step_logs`, which
        keeps the tail: a narrative states its intent in its opening lines, a
        log states its outcome in its last ones."""
        repo = await world.repo()
        narrative = "HEAD-OF-THE-NARRATIVE\n" + ("m" * 40_000) + "\nTAIL-OF-THE-NARRATIVE"
        story = await world.story(await world.feature(), narrative=narrative)
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert "HEAD-OF-THE-NARRATIVE" in payload["markdown"]
        assert "TAIL-OF-THE-NARRATIVE" not in payload["markdown"]
        assert "story_narrative" in payload["dropped"]

    async def test_test_refs_are_bounded_before_the_budget_bites(
        self, db_session, world
    ):
        """SPEC_CONTEXT_MAX_TEST_REFS keeps a story with hundreds of refs from
        spending the whole budget on paths, and the omission is stated."""
        repo = await world.repo()
        story = await world.story(await world.feature())
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        for index in range(40):
            await world.test_ref(
                repo,
                criterion=criterion,
                file_path=f"tests/test_{index:03d}.py",
                lazyaf_test_id=f"t-{index:03d}",
            )
        card = await world.card(repo, story=story)

        payload = await _bundle(db_session, card, repo)

        assert payload["test_ref_count"] <= 25
        assert "further tests omitted" in payload["markdown"]


# ---------------------------------------------------------------------------
# R5
# ---------------------------------------------------------------------------

class TestAsyncSafety:
    async def test_no_lazy_load_after_the_session_is_expired(
        self, db_session, world
    ):
        """R5: every relationship the service touches is selectinload-ed.

        Expiring the whole identity map first means any attribute the service
        reaches for that it did NOT eager-load would have to emit IO from a
        greenlet-less context and raise MissingGreenlet - which is exactly the
        production failure this rule exists to stop.
        """
        repo = await world.repo()
        feature = await world.feature(description="Protect the API.")
        story = await world.story(feature, narrative="Intent.")
        criterion = await world.criterion(story, "A repo over budget gets 429.")
        ref = await world.test_ref(repo, criterion=criterion)
        await world.test_run(ref, TestRunStatus.PASSED.value)
        card = await world.card(repo, story=story)
        # Read the ids BEFORE expiring: after expire_all() even a scalar
        # attribute access outside a greenlet raises, and that would be the
        # TEST reaching into the DB, not the service.
        card_id, repo_id = card.id, repo.id
        await db_session.commit()
        db_session.expire_all()

        payload = await build_spec_context(
            db_session, card_id=card_id, repo_id=repo_id
        )
        assert_bundle_conforms(payload, "ASSEMBLER (spec_context)")

        assert payload is not None
        assert "Protect the API." in payload["markdown"]
        assert "last run: passed" in payload["markdown"]


# ---------------------------------------------------------------------------
# determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    async def test_the_same_rows_produce_byte_identical_markdown(
        self, db_session, world
    ):
        """The preview endpoint, the dispatched bundle and a 12.6.5 replay all
        have to agree; that only works if assembly is a pure function of the
        rows."""
        repo = await world.repo()
        feature = await world.feature(description="Protect the API.")
        story = await world.story(feature, narrative="Intent.")
        for index in range(5):
            await world.criterion(story, f"criterion {index}", notes=f"note {index}")
        criterion = (await world.criterion(story, "last one"))
        await world.test_ref(repo, criterion=criterion, lazyaf_test_id="a")
        await world.test_ref(
            repo,
            criterion=criterion,
            file_path="tests/b.py",
            lazyaf_test_id="b",
        )
        card = await world.card(repo, story=story)

        first = await _bundle(db_session, card, repo)
        second = await _bundle(db_session, card, repo)

        assert first["markdown"] == second["markdown"]
        assert first == second
