"""
Specification layer routers (Phase 12.2.5).

CRUD for Feature / UserStory / AcceptanceCriterion / PromptTemplate, plus the
idempotent Milestone-12 seed endpoint. Hierarchy is intentionally shallow.

NOTE (integrator): register with `app.include_router(spec.router)` in main.py.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Card, Repo
from app.models.spec import (
    AcceptanceCriterion,
    Feature,
    FeatureStatus,
    PromptTemplate,
    StoryStatus,
    UserStory,
)
from app.schemas.spec import (
    CriterionCreate,
    CriterionRead,
    CriterionUpdate,
    FeatureCreate,
    FeatureRead,
    FeatureUpdate,
    PromptTemplateCreate,
    PromptTemplateRead,
    PromptTemplateUpdate,
    UserStoryCreate,
    UserStoryRead,
    UserStoryUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["spec"])


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

async def _validate_repo_ids(db: AsyncSession, repo_ids: list[str]) -> None:
    """Raise 400 if any of the given repo IDs does not exist."""
    if not repo_ids:
        return
    result = await db.execute(select(Repo.id).where(Repo.id.in_(repo_ids)))
    existing = {row[0] for row in result.all()}
    missing = set(repo_ids) - existing
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown repo IDs: {', '.join(sorted(missing))}",
        )


async def _get_feature_or_404(db: AsyncSession, feature_id: str) -> Feature:
    result = await db.execute(select(Feature).where(Feature.id == feature_id))
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")
    return feature


async def _get_story_or_404(db: AsyncSession, story_id: str) -> UserStory:
    result = await db.execute(select(UserStory).where(UserStory.id == story_id))
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(status_code=404, detail="User story not found")
    return story


async def _get_prompt_template_or_404(
    db: AsyncSession, template_id: str
) -> PromptTemplate:
    result = await db.execute(
        select(PromptTemplate).where(PromptTemplate.id == template_id)
    )
    template = result.scalar_one_or_none()
    if not template:
        raise HTTPException(status_code=404, detail="Prompt template not found")
    return template


def _name_taken(name: str) -> HTTPException:
    """THE answer to a duplicate prompt-template name (R3: one spelling).

    Raised from the pre-check and from the race absorber below, so the loser
    of a concurrent create is told exactly what a sequential duplicate is told.
    """
    return HTTPException(
        status_code=409,
        detail=f"Prompt template named '{name}' already exists",
    )


async def _ensure_template_name_unique(db: AsyncSession, name: str) -> None:
    """Raise 409 if a prompt template with this name already exists."""
    result = await db.execute(
        select(PromptTemplate).where(PromptTemplate.name == name)
    )
    if result.scalar_one_or_none():
        raise _name_taken(name)


async def _insert_template_absorbing_race(
    db: AsyncSession, payload: PromptTemplateCreate
) -> PromptTemplate:
    """Create a PromptTemplate, absorbing the check-then-insert race on `name`.

    ``_ensure_template_name_unique`` is a SELECT, and ``prompt_templates.name``
    is UNIQUE: between that SELECT and the INSERT a concurrent request can take
    the name, and the constraint then fires at COMMIT. Measured by the QA pass
    at 20 simultaneous creates of one name: 4-11 bare 500s per trial and ZERO
    rows written - the winner's insert died along with the losers'.

    Absorbed with the rollback/re-select idiom this codebase already uses in
    usage and test-results ingestion (``app/services/usage_ingestion.py``,
    ``app/services/test_ingestion.py``): the race costs a re-read, never the
    record. Bounded at two passes - the second re-select answers 409 in the
    normal case, and inserts in the rare one where the winner was deleted in
    between; a third collision is answered, not retried forever.
    """
    for attempt in (1, 2):
        await _ensure_template_name_unique(db, payload.name)
        db_template = PromptTemplate(
            name=payload.name,
            description=payload.description,
            content=payload.content,
        )
        db.add(db_template)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            logger.info(
                "Concurrent prompt-template insert for name %r (pass %d) — "
                "re-resolving",
                payload.name,
                attempt,
            )
            continue
        await db.refresh(db_template)
        return db_template

    await _ensure_template_name_unique(db, payload.name)
    raise _name_taken(payload.name)


async def _create_feature(
    db: AsyncSession, feature: FeatureCreate, *, commit: bool = True
) -> Feature:
    """Create a Feature (validates repo_ids). Shared with cards.py's
    promote-to-feature endpoint; pass commit=False to leave the row
    flushed-but-uncommitted so the caller can commit atomically."""
    await _validate_repo_ids(db, feature.repo_ids)
    db_feature = Feature(
        title=feature.title,
        description=feature.description,
        status=feature.status.value,
        repo_ids=json.dumps(feature.repo_ids),
    )
    db.add(db_feature)
    if commit:
        await db.commit()
        await db.refresh(db_feature)
    else:
        await db.flush()
    return db_feature


async def _get_criterion_or_404(db: AsyncSession, criterion_id: str) -> AcceptanceCriterion:
    result = await db.execute(
        select(AcceptanceCriterion).where(AcceptanceCriterion.id == criterion_id)
    )
    criterion = result.scalar_one_or_none()
    if not criterion:
        raise HTTPException(status_code=404, detail="Criterion not found")
    return criterion


async def _story_done_blocked_by_required_criteria(
    db: AsyncSession, story: UserStory
) -> bool:
    """Required-criterion-blocks-done rule — ACTIVATED (Phase 12.2.6).

    A story cannot transition to 'done' while any REQUIRED acceptance
    criterion is not CURRENTLY green. Non-required criteria never block, and
    a criterion with no TestRun at all blocks (nothing measures it).

    "Currently green" is deliberately latest-only, not "ever passed": a
    criterion that passed last month and has been failing since is red, and a
    historical pass must never unblock it (that is exactly the fake-green the
    gate exists to catch). Per criterion, the ONE run that decides is:

    - the newest run on the criterion's relevant branch — the default branch
      of the repo its TestRef belongs to. Runs with no branch recorded
      (manual runs, triggers without branch context) count as relevant: they
      make no claim to be scratch work.
    - if the criterion has no relevant run at all (every run came from a
      named non-default branch), the newest run overall decides, so a repo
      that only ever runs CI on feature branches still gets a verdict rather
      than a permanent block.

    Ordering is (created_at, id) so the verdict is deterministic when two
    runs share a timestamp.
    """
    from app.models import Repo, TestRef, TestRun

    required_ids = list(
        (
            await db.execute(
                select(AcceptanceCriterion.id).where(
                    AcceptanceCriterion.user_story_id == story.id,
                    AcceptanceCriterion.required == True,  # noqa: E712
                )
            )
        ).scalars()
    )
    if not required_ids:
        return False

    rows = (
        await db.execute(
            select(
                TestRef.criterion_id,
                TestRun.status,
                TestRun.branch,
                Repo.default_branch,
            )
            .join(TestRun, TestRun.test_ref_id == TestRef.id)
            .join(Repo, Repo.id == TestRef.repo_id, isouter=True)
            .where(TestRef.criterion_id.in_(required_ids))
            .order_by(TestRun.created_at.desc(), TestRun.id.desc())
        )
    ).all()

    # Newest-first walk: the first row seen per criterion IS the latest one.
    latest_relevant: dict[str, str] = {}
    latest_any: dict[str, str] = {}
    for criterion_id, status, branch, default_branch in rows:
        latest_any.setdefault(criterion_id, status)
        if branch is None or branch == default_branch:
            latest_relevant.setdefault(criterion_id, status)

    blocking_ids = [
        criterion_id
        for criterion_id in required_ids
        if latest_relevant.get(criterion_id, latest_any.get(criterion_id)) != "passed"
    ]
    if blocking_ids:
        logger.info(
            "Story %s blocked from 'done': %d required criteria are not "
            "currently green (%s)",
            story.id,
            len(blocking_ids),
            ", ".join(blocking_ids),
        )
        return True
    return False


async def _create_story(db: AsyncSession, feature_id: str, story: UserStoryCreate) -> UserStory:
    await _get_feature_or_404(db, feature_id)
    db_story = UserStory(
        feature_id=feature_id,
        title=story.title,
        narrative=story.narrative,
        status=story.status.value,
        priority=story.priority,
    )
    db.add(db_story)
    await db.commit()
    await db.refresh(db_story)
    return db_story


async def _create_criterion(
    db: AsyncSession, user_story_id: str, criterion: CriterionCreate
) -> AcceptanceCriterion:
    await _get_story_or_404(db, user_story_id)
    db_criterion = AcceptanceCriterion(
        user_story_id=user_story_id,
        text=criterion.text,
        required=criterion.required,
        notes=criterion.notes,
    )
    db.add(db_criterion)
    await db.commit()
    await db.refresh(db_criterion)
    return db_criterion


# -----------------------------------------------------------------------------
# Milestone 12 seed (idempotent, called explicitly — never at startup)
# -----------------------------------------------------------------------------

MILESTONE12_FEATURE_TITLE = "LazyAF Milestone 12"

MILESTONE12_FEATURE_DESCRIPTION = (
    "The platform's own north-star milestone: finish the runner architecture "
    "arc and the spec/eval layer, with LazyAF gating LazyAF the whole way. "
    "Seeded by Phase 12.2.5 so the platform's first spec-tracked feature is "
    "itself. The story set has since grown past Milestone 12's original "
    "scope — US-4 records Milestone 14's self-hosted model endpoints — "
    "because this feature row IS the platform's north star, not a numbered "
    "sprint. Every criterion below carries notes naming the evidence for it, "
    "or saying plainly that there is none."
)

# Each criterion is a dict so the seed can carry EVIDENCE, not just a claim:
#
#   text        the criterion itself
#   required    required criteria block their story from 'done' until a
#               linked TestRef is currently green (_story_done_blocked_by_
#               required_criteria). A required criterion with no TestRef
#               blocks forever — which is the honest state for a criterion
#               nothing measures, and is why `notes` must say so out loud.
#   notes       WHY we believe it, in one of three shapes, and never a fourth:
#                 "TIED BACK:"    a lazyaf_test_id in MILESTONE12_TEST_REF_
#                                 SEEDS points here, so dogfood runs produce
#                                 TestRuns joined to this criterion.
#                 "COVERED, NOT TIED BACK:" a real test proves it, but carries
#                                 no lazyaf_test_id marker, so nothing joins
#                                 its result to this row. Names the test.
#                 "NOT MET:"      the criterion is false today. Says why.
#   supersedes  exact criterion texts THIS SEED previously wrote and has since
#               retired. Reconciliation rewrites those rows in place rather
#               than orphaning them, so an already-seeded database (the Specs
#               UI the owner actually looks at) converges on the current text
#               instead of keeping a stale claim forever. Only ever list text
#               the platform itself authored — never a user's edit.
#
# Audited against the tree on 2026-08-30. Every test named below was
# confirmed to exist at that time by the tie-back test in
# tdd/integration/api/test_spec_api.py and by direct inspection.

MILESTONE12_STORIES: list[dict] = [
    {
        "title": "US-1 Commits land, AI workflows run",
        "priority": 1,
        "narrative": (
            "Given a repo ingested into LazyAF with a pipeline bound to a push "
            "trigger, when I push to the internal remote, the pipeline runs my "
            "steps (tests, builds, agent steps) in isolated containers, live "
            "status/logs stream to the UI, and the outcome gates the branch."
        ),
        "criteria": [
            {
                "text": "A push to the internal remote triggers the bound pipeline.",
                "required": True,
                "notes": (
                    "TIED BACK: us1.push-triggered-run-executes-steps "
                    "(tdd/integration/api/test_pipeline_sync_on_push.py)."
                ),
            },
            {
                "text": "Steps run in isolated containers with live status and logs streaming to the UI.",
                "required": True,
                "notes": (
                    "TIED BACK by two refs: us1.steps-run-in-isolated-containers "
                    "(tdd/integration/services/test_pipeline_local_execution.py, "
                    "real Docker, T2) and us1.step-status-streams-to-ui "
                    "(tdd/e2e/test_graph_pipeline.py, T3)."
                ),
            },
            {
                "text": "The pipeline outcome gates the branch.",
                "required": True,
                "notes": (
                    "TIED BACK: us1.pipeline-outcome-gates-branch "
                    "(tdd/integration/services/test_pipeline_local_execution.py)."
                ),
            },
            {
                "text": "LazyAF runs LazyAF's own tdd suite this way with the execution tiers actually executing (a green run that skipped the Docker tier is a failure).",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. The mechanism is real and tested: "
                    "scripts/ci_gate.py refuses a run whose executed count is "
                    "below the per-tier floor in tdd/tier_floors.json, refuses "
                    "un-baselined skips, and refuses any run carrying failures "
                    "or errors — so a tier that silently stopped executing "
                    "fails instead of reporting green. Proven by "
                    "tdd/unit/scripts/test_ci_gate.py (TestGateFails::"
                    "test_executed_below_floor_fails, "
                    "TestGateFails::test_out_of_baseline_skip_fails, "
                    "TestGateRefusesRedInput::test_failures_fail_the_gate). "
                    "Those tests carry no lazyaf_test_id marker, so no TestRef "
                    "joins them to this criterion and a dogfood run produces no "
                    "evidence against this row. Adding the marker is the fix."
                ),
            },
        ],
    },
    {
        "title": "US-2 Card dev loop",
        "priority": 2,
        "narrative": (
            "Given a card describing a feature, when I start it, an agent "
            "implements it on a branch; completion triggers the gating "
            "pipeline; on pass the card reaches review with a diff; approve "
            "merges to target."
        ),
        "criteria": [
            {
                "text": "Starting a card causes an agent to implement it on a branch.",
                "required": True,
                "notes": (
                    "TIED BACK: us2.start-card-begins-implementation "
                    "(tdd/e2e/test_card_execute.py)."
                ),
            },
            {
                "text": "Card completion triggers the gating pipeline.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. This is link 4 of the chain "
                    "asserted by tdd/e2e/test_us2_card_loop.py::"
                    "TestUS2CardLoop::test_full_loop, which runs in T3 on every "
                    "push. It carries no lazyaf_test_id marker, so no TestRef "
                    "joins it to this criterion."
                ),
            },
            {
                "text": "On pipeline pass the card reaches review with a diff.",
                "required": True,
                "notes": (
                    "TIED BACK by two refs, both in tdd/e2e/test_card_execute.py: "
                    "us2.card-completion-reaches-review (the card reaches "
                    "in_review) and us2.review-shows-diff (the diff is there)."
                ),
            },
            {
                "text": "Approving the review merges the branch to target.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. Link 5 of "
                    "tdd/e2e/test_us2_card_loop.py::TestUS2CardLoop::"
                    "test_full_loop — POST /api/cards/{id}/approve merges into "
                    "the default branch and the card reaches 'done'. No "
                    "lazyaf_test_id marker, so no TestRef joins it here."
                ),
            },
            {
                "text": "A zero-cost mock-agent e2e inside the dogfood suite covers this loop continuously from 12.5 on.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK, and true: tdd/e2e/"
                    "test_us2_card_loop.py is deliberately NOT marked slow so "
                    "it runs in T3 on every push, and it drives the mock agent "
                    "rather than a paid one, so the loop is covered at zero API "
                    "cost. Recorded in tdd/tier_floors.json's T3 note. No "
                    "lazyaf_test_id marker on it."
                ),
            },
        ],
    },
    {
        "title": "US-3 Compare bench",
        "priority": 3,
        "narrative": (
            "Given a workflow/card and a set of (model x prompt) variants, "
            "when I launch a comparison, each variant runs in isolation and I "
            "get a side-by-side of outcomes (pass-rates per criterion, diffs, "
            "cost/time)."
        ),
        "criteria": [
            {
                "text": "Launching a comparison runs each (model x prompt) variant in isolation.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. Phase 12.6.5 landed this: "
                    "tdd/integration/api/test_experiments_api.py::TestLaunch::"
                    "test_launch_creates_a_pipeline_run_per_cell (one "
                    "PipelineRun per matrix cell) and ::test_cells_get_their_"
                    "own_branch (the isolation), over the fan-out proven by "
                    "tdd/unit/services/test_experiment_matrix_expansion.py::"
                    "TestExpansion. None carry a lazyaf_test_id marker."
                ),
            },
            {
                "text": "Results render side-by-side: pass-rates per criterion, diffs, cost/time.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. Pass-rate and cost/time "
                    "aggregation is proven by tdd/unit/services/"
                    "test_experiment_metrics.py (TestLeaderboardPassRates, "
                    "TestMedians, TestObservedSpend — including the rule that a "
                    "zero denominator reports null with a reason rather than "
                    "0%), and the cross-variant join by tdd/integration/api/"
                    "test_experiment_stamping.py::"
                    "test_feature_leaderboard_spans_experiments_and_labels_the_"
                    "baseline. No lazyaf_test_id marker on any of them. The "
                    "RENDERING half is frontend and is not covered by any tier."
                ),
            },
            {
                "text": "The comparison's inputs are first-class, queryable rows: prompt variants stored as named templates, and acceptance criteria retrievable for scoring.",
                "required": True,
                "supersedes": [
                    # Retired 2026-08-30. This named a file that has never
                    # existed. Phase 12.6.5's coverage landed under other
                    # filenames (see the two criteria above); the exit gate the
                    # PLAN named was not the exit gate that was written, and a
                    # criterion pointing at a nonexistent file verifies nothing
                    # while looking specific.
                    "tdd/e2e/test_experiment_matrix.py exercises the matrix (Phase 12.6.5's exit gate).",
                ],
                "notes": (
                    "TIED BACK by two refs, both in tdd/integration/api/"
                    "test_spec_api.py: us3.prompt-variants-storable and "
                    "us3.criteria-queryable-for-scoring. Scope is deliberately "
                    "narrow — these two tests prove the comparison's INPUTS are "
                    "real rows, which is exactly what they assert. They were "
                    "previously tied to the two criteria above, which they do "
                    "not verify; re-pointed here on 2026-08-30 so the tie-back "
                    "is true rather than merely present."
                ),
            },
        ],
    },
    {
        "title": "US-4 Agents run on self-hosted models",
        "priority": 4,
        "narrative": (
            "Given an OpenAI-compatible model endpoint I host myself (ollama "
            "or vLLM, on my own GPU), when I register it in LazyAF and probe "
            "it, agent steps can name that endpoint and run against it — no "
            "third-party API key and no per-token vendor bill. Added "
            "2026-08-30: Milestone 14 shipped this and the spec did not "
            "mention it."
        ),
        "criteria": [
            {
                "text": "Registering an OpenAI-compatible endpoint probes it in one call and records what it can actually do: model listed, tool-calling, streaming, usage reporting, and context window.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. tdd/integration/api/"
                    "test_model_endpoints_api.py::TestRegistration::"
                    "test_no_auth_endpoint_registers_and_probes_in_one_call and "
                    "class TestProbe; the probe's judgements over real HTTP in "
                    "tdd/unit/services/test_endpoint_probe.py::TestRunProbe, "
                    "including vLLM's max_model_len and ollama's /api/show "
                    "context discovery. No lazyaf_test_id marker."
                ),
            },
            {
                "text": "An agent step naming an endpoint is dispatched to it, and an unprobed, disabled or repeatedly-failing endpoint is refused with a message naming the fix rather than dispatched blindly.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. tdd/unit/services/"
                    "test_harness_step_dispatch.py::TestResolverRefusals "
                    "(test_an_unprobed_endpoint_is_refused_with_the_fix_in_the_"
                    "message, test_a_disabled_endpoint_is_refused, "
                    "test_three_consecutive_failures_is_refused) and "
                    "::TestResolverPrecedence for which key names the endpoint. "
                    "No lazyaf_test_id marker."
                ),
            },
            {
                "text": "The openai-harness agent loop runs in a real container against a real OpenAI-compatible server: it lands files in the workspace, streams its log lines through the steps API, and SUMS token usage across turns rather than keeping the last turn.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. tdd/integration/services/"
                    "test_harness_step_container.py::TestHarnessStepRoundTrip::"
                    "test_tools_mode_lands_files_logs_and_summed_usage — real "
                    "Docker, real named volume, the real steps API over HTTP, "
                    "and tdd/shared/mock_openai as the endpoint, so it needs no "
                    "GPU and runs in T2 on every push. The summing is checked "
                    "against a mock that reports GROWING per-turn usage, so "
                    "'summed' and 'kept the last response' are different "
                    "numbers. Confirmed green in tdd/tier_floors.json's T2 note "
                    "(2026-08-30, executed=77 passed=77). No lazyaf_test_id "
                    "marker."
                ),
            },
            {
                "text": "A model with no tool-calling support still does useful work through the text-fence fallback protocol.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. tdd/integration/services/"
                    "test_harness_step_container.py::TestHarnessStepRoundTrip::"
                    "test_forced_text_mode_runs_the_fallback_protocol, green in "
                    "T2 per tdd/tier_floors.json. The tools-less endpoint is "
                    "registered as degraded-but-usable rather than refused "
                    "(tdd/integration/api/test_model_endpoints_api.py::"
                    "TestProbe::test_a_tools_less_model_is_degraded_but_"
                    "registered). No lazyaf_test_id marker."
                ),
            },
            {
                "text": "An endpoint's API key reaches the harness inside the container while appearing nowhere in that container's docker inspect output.",
                "required": True,
                "notes": (
                    "COVERED, NOT TIED BACK. tdd/integration/services/"
                    "test_harness_step_container.py::"
                    "TestEndpointSecretContainment::"
                    "test_the_endpoint_key_is_in_no_container_inspect, green in "
                    "T2. The key travels through Phase 12.5's existing "
                    "secret_environment file channel — M14 added a secret to "
                    "that channel rather than a new channel. Reinforced at the "
                    "API edge by tdd/integration/api/test_model_endpoints_api.py"
                    "::TestSecretRefs::test_the_sentinel_never_appears_on_any_"
                    "surface. No lazyaf_test_id marker."
                ),
            },
            {
                "text": "A card can be assigned to the self-hosted harness and run the US-2 loop end to end on my own GPU.",
                "required": True,
                "notes": (
                    "NOT MET as of 2026-08-30, and stated here rather than left "
                    "as a silent gap. Two verified blockers. (1) A card cannot "
                    "name the harness at all: app.models.card.RunnerType has "
                    "members any / claude-code / gemini / mock and no "
                    "openai-harness, so the create-card endpoint refuses it. "
                    "(2) The e2e that would prove it, tdd/e2e/"
                    "test_harness_card_loop.py, does not exist — recorded as an "
                    "open gap in tdd/tier_floors.json's T3 note, which also "
                    "names its remaining addressing blocker (T3's backend is a "
                    "host process while step containers are Docker siblings, "
                    "and no single base_url reaches both). What DOES work "
                    "today, and is proven rather than asserted: the harness "
                    "runs as a pipeline AGENT STEP, per the T2 container tests "
                    "named in the criteria above. It is the CARD entry point "
                    "that is missing. This criterion is required and "
                    "unverified, so it correctly blocks US-4 from 'done'."
                ),
            },
        ],
    },
]


# Starter-set annotation map (Phase 12.2.6): lazyaf_test_id -> the seeded
# criterion it verifies, addressed as (story_index, criterion_index) into
# MILESTONE12_STORIES. These ids are the @pytest.mark.lazyaf_test_id markers
# carried by LazyAF's own tdd suite; seed-milestone12 upserts an active
# TestRef for each, linked to the matching criterion, so a dogfood run of the
# suite immediately produces TestRuns joined to the north-star criteria.
# Two ids may verify the same criterion (a criterion can have many TestRefs).
MILESTONE12_TEST_REF_SEEDS: list[dict] = [
    # US-1 Commits land, AI workflows run
    {
        "lazyaf_test_id": "us1.push-triggered-run-executes-steps",
        "file_path": "tdd/integration/api/test_pipeline_sync_on_push.py",
        "story": 0, "criterion": 0,
    },
    {
        "lazyaf_test_id": "us1.steps-run-in-isolated-containers",
        "file_path": "tdd/integration/services/test_pipeline_local_execution.py",
        "story": 0, "criterion": 1,
    },
    {
        "lazyaf_test_id": "us1.step-status-streams-to-ui",
        "file_path": "tdd/e2e/test_graph_pipeline.py",
        "story": 0, "criterion": 1,
    },
    {
        "lazyaf_test_id": "us1.pipeline-outcome-gates-branch",
        "file_path": "tdd/integration/services/test_pipeline_local_execution.py",
        "story": 0, "criterion": 2,
    },
    # US-2 Card dev loop
    {
        "lazyaf_test_id": "us2.start-card-begins-implementation",
        "file_path": "tdd/e2e/test_card_execute.py",
        "story": 1, "criterion": 0,
    },
    {
        "lazyaf_test_id": "us2.card-completion-reaches-review",
        "file_path": "tdd/e2e/test_card_execute.py",
        "story": 1, "criterion": 2,
    },
    {
        "lazyaf_test_id": "us2.review-shows-diff",
        "file_path": "tdd/e2e/test_card_execute.py",
        "story": 1, "criterion": 2,
    },
    # US-3 Compare bench.
    # Re-pointed 2026-08-30 from criteria 0 and 1 to criterion 2. These two
    # tests assert that a prompt template can be created and that the seeded
    # criteria come back through the API — they say nothing about variants
    # running in isolation or about a side-by-side leaderboard, which is what
    # criteria 0 and 1 claim. A tie-back weaker than its criterion reports
    # green for a claim it never tested, which is the precise failure this
    # layer exists to prevent, so criterion 2 now states what they DO prove.
    {
        "lazyaf_test_id": "us3.prompt-variants-storable",
        "file_path": "tdd/integration/api/test_spec_api.py",
        "story": 2, "criterion": 2,
    },
    {
        "lazyaf_test_id": "us3.criteria-queryable-for-scoring",
        "file_path": "tdd/integration/api/test_spec_api.py",
        "story": 2, "criterion": 2,
    },
    # US-4 Agents run on self-hosted models: NO ENTRIES, deliberately.
    # Milestone 14's coverage is real and named in each criterion's notes, but
    # none of those tests carries a @pytest.mark.lazyaf_test_id marker, and
    # seeding a TestRef for a marker that does not exist would create a row
    # that looks like verification, receives no TestRun ever, and quietly
    # verifies nothing. Adding the markers to those files is the fix; inventing
    # refs here is not.
]


async def _upsert_milestone12_test_refs(
    db: AsyncSession, feature: Feature, repo_id: str | None
) -> None:
    """Upsert active TestRefs for the starter-set annotations (idempotent).

    Looks up each seeded criterion by (story title, criterion text) under the
    given feature, then upserts a TestRef per MILESTONE12_TEST_REF_SEEDS
    entry: existing refs (by unique lazyaf_test_id) are re-pointed and
    flipped active; missing ones are created under repo_id. TestRef.repo_id
    is NOT NULL, so without a repo_id (the dogfood caller passes LazyAF's own
    ingested repo) ref seeding is skipped with a log line — the feature/
    story/criterion seed itself never depends on a repo. Flushes only; the
    caller commits.
    """
    if repo_id is None:
        logger.info(
            "seed-milestone12 called without repo_id; skipping starter-set "
            "TestRef upsert (pass repo_id to link LazyAF's own suite)"
        )
        return

    from app.models import TestRef

    result = await db.execute(
        select(UserStory).where(UserStory.feature_id == feature.id)
    )
    stories_by_title = {s.title: s for s in result.scalars().all()}

    # (story title, criterion text) -> criterion id
    criterion_ids: dict[tuple[str, str], str] = {}
    for story in stories_by_title.values():
        result = await db.execute(
            select(AcceptanceCriterion).where(
                AcceptanceCriterion.user_story_id == story.id
            )
        )
        for criterion in result.scalars().all():
            criterion_ids[(story.title, criterion.text)] = criterion.id

    for seed in MILESTONE12_TEST_REF_SEEDS:
        story_def = MILESTONE12_STORIES[seed["story"]]
        key = (
            story_def["title"],
            story_def["criteria"][seed["criterion"]]["text"],
        )
        criterion_id = criterion_ids.get(key)
        if criterion_id is None:  # seeded data was hand-edited; skip, don't die
            logger.warning(
                "Milestone12 TestRef seed: criterion not found for %s",
                seed["lazyaf_test_id"],
            )
            continue

        # Identity is the PAIR (repo_id, lazyaf_test_id) — contract #1 in
        # models/testref.py, and the unique index this table actually carries.
        # Matching on the marker alone was wrong three ways, all measured:
        # seeding a second repo re-pointed the FIRST repo's rows and left the
        # second with none; a marker already registered under another repo was
        # hijacked (flipped active, relinked to this repo's criterion); and
        # once two repos held the marker, scalar_one_or_none() raised
        # MultipleResultsFound and the endpoint answered 500.
        result = await db.execute(
            select(TestRef).where(
                TestRef.repo_id == repo_id,
                TestRef.lazyaf_test_id == seed["lazyaf_test_id"],
            )
        )
        ref = result.scalar_one_or_none()
        if ref:
            ref.criterion_id = criterion_id
            ref.file_path = seed["file_path"]
            ref.status = "active"
        else:
            db.add(
                TestRef(
                    lazyaf_test_id=seed["lazyaf_test_id"],
                    repo_id=repo_id,
                    file_path=seed["file_path"],
                    criterion_id=criterion_id,
                    status="active",
                )
            )
    await db.flush()


async def _reconcile_milestone12_content(db: AsyncSession, feature: Feature) -> None:
    """Bring an ALREADY-SEEDED feature up to the current constants.

    Seeding used to be create-or-skip, which meant editing MILESTONE12_STORIES
    changed nothing for anyone who had already run the seed — and the Specs UI
    is exactly a view of an already-seeded database. A stale claim would
    therefore outlive its own correction forever. That is the drift this whole
    layer exists to catch, so the seed reconciles instead of skipping.

    What it touches, and nothing else:

    - the feature description, and each seeded story's narrative and priority
      (platform-authored text);
    - each seeded criterion's text, `required` flag and notes, resolved by
      current text first and then by any `supersedes` text this seed is known
      to have written before;
    - criteria and stories that are missing entirely, which are created.

    What it never does is DELETE. A criterion the constants no longer mention
    may be a user's own addition, and TestRefs point at criterion ids — so an
    unrecognised row is left exactly as it is. Retiring platform-authored text
    is done by listing it in `supersedes`, which rewrites that row in place and
    keeps every TestRef attached to it.

    Flushes only; the caller commits.
    """
    if feature.description != MILESTONE12_FEATURE_DESCRIPTION:
        feature.description = MILESTONE12_FEATURE_DESCRIPTION

    stories_by_title = {
        s.title: s
        for s in (
            await db.execute(
                select(UserStory).where(UserStory.feature_id == feature.id)
            )
        ).scalars()
    }

    for story_def in MILESTONE12_STORIES:
        story = stories_by_title.get(story_def["title"])
        if story is None:
            story = UserStory(
                feature_id=feature.id,
                title=story_def["title"],
                narrative=story_def["narrative"],
                status=StoryStatus.ACCEPTED.value,
                priority=story_def["priority"],
            )
            db.add(story)
            await db.flush()
            logger.info("seed-milestone12: added story %r", story_def["title"])
        else:
            story.narrative = story_def["narrative"]
            story.priority = story_def["priority"]

        existing = list(
            (
                await db.execute(
                    select(AcceptanceCriterion).where(
                        AcceptanceCriterion.user_story_id == story.id
                    )
                )
            ).scalars()
        )
        by_text = {c.text: c for c in existing}

        # Resolve in two passes so one database row can never be claimed by
        # two criteria. Exact current text wins first; only then may a
        # `supersedes` entry claim what is left. Done in one pass, a criterion
        # whose text another criterion lists as retired could capture the same
        # row twice and silently drop one of the two.
        matched: dict[int, AcceptanceCriterion] = {}
        claimed: set[str] = set()

        for index, criterion_def in enumerate(story_def["criteria"]):
            row = by_text.get(criterion_def["text"])
            if row is not None and row.id not in claimed:
                matched[index] = row
                claimed.add(row.id)

        for index, criterion_def in enumerate(story_def["criteria"]):
            if index in matched:
                continue
            for old_text in criterion_def.get("supersedes", ()):
                row = by_text.get(old_text)
                if row is not None and row.id not in claimed:
                    logger.info(
                        "seed-milestone12: rewriting retired criterion %r as %r",
                        old_text,
                        criterion_def["text"],
                    )
                    matched[index] = row
                    claimed.add(row.id)
                    break

        for index, criterion_def in enumerate(story_def["criteria"]):
            row = matched.get(index)
            if row is None:
                db.add(
                    AcceptanceCriterion(
                        user_story_id=story.id,
                        text=criterion_def["text"],
                        required=criterion_def["required"],
                        notes=criterion_def["notes"],
                    )
                )
                continue
            row.text = criterion_def["text"]
            row.required = criterion_def["required"]
            row.notes = criterion_def["notes"]

    await db.flush()


async def seed_milestone12(
    db: AsyncSession, repo_id: str | None = None
) -> tuple[Feature, bool]:
    """Idempotently seed the north-star feature with its user stories and
    acceptance criteria, plus (when repo_id is given) active TestRefs for the
    starter-set suite annotations (MILESTONE12_TEST_REF_SEEDS) under that repo.

    Idempotent by feature title: exactly one feature named
    MILESTONE12_FEATURE_TITLE ever exists, and the returned `created` flag says
    whether this call was the one that made it. When it already exists the row
    is RECONCILED against the current constants rather than skipped (see
    _reconcile_milestone12_content) and the TestRef upsert still runs, so
    re-seeding both repairs orphaned starter-set refs and converges stale
    seeded text on what the constants now say.

    Safe to re-run: a second call against an up-to-date database writes no new
    rows and changes no values.
    """
    if repo_id is not None:
        await _validate_repo_ids(db, [repo_id])
    result = await db.execute(
        select(Feature).where(Feature.title == MILESTONE12_FEATURE_TITLE)
    )
    existing = result.scalar_one_or_none()
    if existing:
        await _reconcile_milestone12_content(db, existing)
        await _upsert_milestone12_test_refs(db, existing, repo_id)
        await db.commit()
        await db.refresh(existing)
        return existing, False

    feature = Feature(
        title=MILESTONE12_FEATURE_TITLE,
        description=MILESTONE12_FEATURE_DESCRIPTION,
        status=FeatureStatus.ACTIVE.value,
        repo_ids="[]",
    )
    db.add(feature)
    await db.flush()

    for story_def in MILESTONE12_STORIES:
        story = UserStory(
            feature_id=feature.id,
            title=story_def["title"],
            narrative=story_def["narrative"],
            status=StoryStatus.ACCEPTED.value,
            priority=story_def["priority"],
        )
        db.add(story)
        await db.flush()
        for criterion_def in story_def["criteria"]:
            db.add(
                AcceptanceCriterion(
                    user_story_id=story.id,
                    text=criterion_def["text"],
                    required=criterion_def["required"],
                    notes=criterion_def["notes"],
                )
            )

    await db.flush()
    await _upsert_milestone12_test_refs(db, feature, repo_id)
    await db.commit()
    await db.refresh(feature)
    return feature, True


class SeedMilestone12Request(BaseModel):
    """Optional seed context: the repo the starter-set TestRefs belong to
    (in dogfood, LazyAF's own ingested repo). Without it the feature/story/
    criterion seed still runs; only the TestRef upsert is skipped."""

    repo_id: str | None = None


@router.post("/api/features/seed-milestone12")
async def seed_milestone12_endpoint(
    req: SeedMilestone12Request | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Seed the Milestone 12 feature (idempotent by feature title)."""
    repo_id = req.repo_id if req else None
    feature, created = await seed_milestone12(db, repo_id=repo_id)
    return {
        "feature": FeatureRead.model_validate(feature),
        "created": created,
    }


# -----------------------------------------------------------------------------
# Features
# -----------------------------------------------------------------------------

@router.get("/api/features", response_model=list[FeatureRead])
async def list_features(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Feature))
    return result.scalars().all()


@router.post("/api/features", response_model=FeatureRead, status_code=201)
async def create_feature(feature: FeatureCreate, db: AsyncSession = Depends(get_db)):
    return await _create_feature(db, feature)


@router.get("/api/features/{feature_id}", response_model=FeatureRead)
async def get_feature(feature_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_feature_or_404(db, feature_id)


@router.patch("/api/features/{feature_id}", response_model=FeatureRead)
async def update_feature(
    feature_id: str, req: FeatureUpdate, db: AsyncSession = Depends(get_db)
):
    feature = await _get_feature_or_404(db, feature_id)
    update_data = req.model_dump(exclude_unset=True)
    if "repo_ids" in update_data and update_data["repo_ids"] is not None:
        await _validate_repo_ids(db, update_data["repo_ids"])
        update_data["repo_ids"] = json.dumps(update_data["repo_ids"])
    if "status" in update_data and update_data["status"] is not None:
        update_data["status"] = update_data["status"].value
    for key, value in update_data.items():
        setattr(feature, key, value)
    await db.commit()
    await db.refresh(feature)
    return feature


@router.delete("/api/features/{feature_id}", status_code=204)
async def delete_feature(feature_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a feature; cascades to its stories and criteria and unlinks
    any cards pointing at the feature or its stories (SQLite does not
    enforce FKs here, so the unlink is explicit)."""
    result = await db.execute(
        select(Feature)
        .where(Feature.id == feature_id)
        .options(selectinload(Feature.stories).selectinload(UserStory.criteria))
    )
    feature = result.scalar_one_or_none()
    if not feature:
        raise HTTPException(status_code=404, detail="Feature not found")

    story_ids = [story.id for story in feature.stories]
    await db.execute(
        update(Card).where(Card.feature_id == feature_id).values(feature_id=None)
    )
    if story_ids:
        await db.execute(
            update(Card)
            .where(Card.user_story_id.in_(story_ids))
            .values(user_story_id=None)
        )

    await db.delete(feature)  # ORM cascade removes stories + criteria
    await db.commit()


@router.get("/api/features/{feature_id}/stories", response_model=list[UserStoryRead])
async def list_feature_stories(feature_id: str, db: AsyncSession = Depends(get_db)):
    await _get_feature_or_404(db, feature_id)
    result = await db.execute(
        select(UserStory).where(UserStory.feature_id == feature_id)
    )
    return result.scalars().all()


@router.post(
    "/api/features/{feature_id}/stories",
    response_model=UserStoryRead,
    status_code=201,
)
async def create_feature_story(
    feature_id: str, story: UserStoryCreate, db: AsyncSession = Depends(get_db)
):
    """Create a story under a feature (feature_id comes from the path)."""
    return await _create_story(db, feature_id, story)


# -----------------------------------------------------------------------------
# User stories
# -----------------------------------------------------------------------------

@router.get("/api/user-stories", response_model=list[UserStoryRead])
async def list_user_stories(
    feature_id: str | None = Query(None, description="Filter by feature"),
    db: AsyncSession = Depends(get_db),
):
    query = select(UserStory)
    if feature_id:
        query = query.where(UserStory.feature_id == feature_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/api/user-stories", response_model=UserStoryRead, status_code=201)
async def create_user_story(story: UserStoryCreate, db: AsyncSession = Depends(get_db)):
    if not story.feature_id:
        raise HTTPException(
            status_code=400, detail="feature_id is required to create a user story"
        )
    return await _create_story(db, story.feature_id, story)


@router.get("/api/user-stories/{story_id}", response_model=UserStoryRead)
async def get_user_story(story_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_story_or_404(db, story_id)


@router.patch("/api/user-stories/{story_id}", response_model=UserStoryRead)
async def update_user_story(
    story_id: str, req: UserStoryUpdate, db: AsyncSession = Depends(get_db)
):
    story = await _get_story_or_404(db, story_id)
    update_data = req.model_dump(exclude_unset=True)
    if "status" in update_data and update_data["status"] is not None:
        new_status = update_data["status"].value
        if new_status == StoryStatus.DONE.value:
            if await _story_done_blocked_by_required_criteria(db, story):
                raise HTTPException(
                    status_code=409,
                    detail="Story has required acceptance criteria without passing test runs",
                )
        update_data["status"] = new_status
    for key, value in update_data.items():
        setattr(story, key, value)
    await db.commit()
    await db.refresh(story)
    return story


@router.delete("/api/user-stories/{story_id}", status_code=204)
async def delete_user_story(story_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(UserStory)
        .where(UserStory.id == story_id)
        .options(selectinload(UserStory.criteria))
    )
    story = result.scalar_one_or_none()
    if not story:
        raise HTTPException(status_code=404, detail="User story not found")

    await db.execute(
        update(Card).where(Card.user_story_id == story_id).values(user_story_id=None)
    )
    await db.delete(story)  # ORM cascade removes criteria
    await db.commit()


@router.get(
    "/api/user-stories/{story_id}/criteria", response_model=list[CriterionRead]
)
async def list_story_criteria(story_id: str, db: AsyncSession = Depends(get_db)):
    await _get_story_or_404(db, story_id)
    result = await db.execute(
        select(AcceptanceCriterion).where(
            AcceptanceCriterion.user_story_id == story_id
        )
    )
    return result.scalars().all()


@router.post(
    "/api/user-stories/{story_id}/criteria",
    response_model=CriterionRead,
    status_code=201,
)
async def create_story_criterion(
    story_id: str, criterion: CriterionCreate, db: AsyncSession = Depends(get_db)
):
    """Create a criterion under a story (user_story_id comes from the path)."""
    return await _create_criterion(db, story_id, criterion)


# -----------------------------------------------------------------------------
# Acceptance criteria
# -----------------------------------------------------------------------------

@router.get("/api/criteria", response_model=list[CriterionRead])
async def list_criteria(
    user_story_id: str | None = Query(None, description="Filter by user story"),
    db: AsyncSession = Depends(get_db),
):
    query = select(AcceptanceCriterion)
    if user_story_id:
        query = query.where(AcceptanceCriterion.user_story_id == user_story_id)
    result = await db.execute(query)
    return result.scalars().all()


@router.post("/api/criteria", response_model=CriterionRead, status_code=201)
async def create_criterion(
    criterion: CriterionCreate, db: AsyncSession = Depends(get_db)
):
    if not criterion.user_story_id:
        raise HTTPException(
            status_code=400, detail="user_story_id is required to create a criterion"
        )
    return await _create_criterion(db, criterion.user_story_id, criterion)


@router.get("/api/criteria/{criterion_id}", response_model=CriterionRead)
async def get_criterion(criterion_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_criterion_or_404(db, criterion_id)


@router.patch("/api/criteria/{criterion_id}", response_model=CriterionRead)
async def update_criterion(
    criterion_id: str, req: CriterionUpdate, db: AsyncSession = Depends(get_db)
):
    criterion = await _get_criterion_or_404(db, criterion_id)
    for key, value in req.model_dump(exclude_unset=True).items():
        setattr(criterion, key, value)
    await db.commit()
    await db.refresh(criterion)
    return criterion


@router.delete("/api/criteria/{criterion_id}", status_code=204)
async def delete_criterion(criterion_id: str, db: AsyncSession = Depends(get_db)):
    criterion = await _get_criterion_or_404(db, criterion_id)
    await db.delete(criterion)
    await db.commit()


# -----------------------------------------------------------------------------
# Prompt templates
# -----------------------------------------------------------------------------

@router.get("/api/prompt-templates", response_model=list[PromptTemplateRead])
async def list_prompt_templates(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(PromptTemplate))
    return result.scalars().all()


@router.post(
    "/api/prompt-templates", response_model=PromptTemplateRead, status_code=201
)
async def create_prompt_template(
    template: PromptTemplateCreate, db: AsyncSession = Depends(get_db)
):
    return await _insert_template_absorbing_race(db, template)


@router.get("/api/prompt-templates/{template_id}", response_model=PromptTemplateRead)
async def get_prompt_template(template_id: str, db: AsyncSession = Depends(get_db)):
    return await _get_prompt_template_or_404(db, template_id)


@router.patch("/api/prompt-templates/{template_id}", response_model=PromptTemplateRead)
async def update_prompt_template(
    template_id: str, req: PromptTemplateUpdate, db: AsyncSession = Depends(get_db)
):
    template = await _get_prompt_template_or_404(db, template_id)

    update_data = req.model_dump(exclude_unset=True)
    new_name = update_data.get("name")
    renaming = bool(new_name) and new_name != template.name
    if renaming:
        await _ensure_template_name_unique(db, new_name)
    for key, value in update_data.items():
        setattr(template, key, value)
    try:
        await db.commit()
    except IntegrityError:
        # Same check-then-write race as the create path, one row later: a
        # concurrent request took `new_name` between the SELECT above and this
        # UPDATE. Answer what a sequential duplicate would get instead of
        # letting the constraint escape as a 500.
        await db.rollback()
        if not renaming:
            raise
        logger.info(
            "Concurrent prompt-template rename to %r — re-resolving", new_name
        )
        raise _name_taken(new_name) from None
    await db.refresh(template)
    return template


@router.delete("/api/prompt-templates/{template_id}", status_code=204)
async def delete_prompt_template(
    template_id: str, db: AsyncSession = Depends(get_db)
):
    template = await _get_prompt_template_or_404(db, template_id)
    await db.delete(template)
    await db.commit()
