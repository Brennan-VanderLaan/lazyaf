"""
Curated spec context - the read-only inspection surface (Phase 12.6.6).

`GET /api/cards/{card_id}/spec-context` answers, for one card, exactly what
`pipeline_executor._build_step_spec_context` would ship to an agent for it.

WHY THIS EXISTS, both reasons load-bearing:

- **R1.** A curated brief whose only observable form is a container's stdout,
  after a paid run has already burned, is dark. This is the
  look-before-you-spend surface: the operator can see what the agent will be
  told, before telling it.
- **The 12.6.5 exit gate.** "One experiment comparing with and without
  curation" needs a human to be able to see *what* was curated when a variant
  underperforms. A number on a leaderboard with no way back to the text is not
  a result anyone can act on.

NOT A SECOND ASSEMBLER (R3). This module contains no bundle-building logic
whatsoever: it calls `app.services.spec_context.build_spec_context`, the same
function dispatch calls, with the same arguments, and adds only the budget
constants for display. `test_preview_matches_what_dispatch_would_send` pins
that byte-for-byte, which is what stops this drifting into a second, prettier,
subtly different answer.

"THIS CARD HAS NO SPEC CONTEXT" IS A SUCCESSFUL ANSWER. An unlinked card
returns 200 with `markdown: null`, never 404 - 404 is reserved for a card that
does not exist. The two are genuinely different questions and collapsing them
would make "did the link get dropped?" unanswerable from the API.

NOTE (integrator): register with `app.include_router(spec_context.router)` in
`main.py`, next to `app.include_router(spec.router)`. This lane does not own
main.py; until that line lands the endpoint is unreachable and
`tdd/integration/api/test_spec_context_endpoint.py` mounts the router itself.

No UI surface ships in this phase, so R8 does not apply - stated, not silently
skipped. This is an operator/API surface; putting it in the card panel is a
follow-on with its own Playwright spec.
"""
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Card
from app.services.control_layer.workspace import (
    SPEC_CONTEXT_MAX_BYTES,
    SPEC_CONTEXT_MAX_TOKENS,
    SPEC_CONTEXT_PATH,
)
from app.services.spec_context import build_spec_context

router = APIRouter(tags=["spec-context"])


class SpecContextSource(BaseModel):
    """Provenance of the bundle: which spec rows it was assembled from."""

    card_id: Optional[str] = None
    feature_id: Optional[str] = None
    user_story_id: Optional[str] = None


class SpecContextRead(BaseModel):
    """What an agent step for this card would be told, plus the budget.

    `markdown` is `null` - and every count zero - when the card has no spec
    links. That is the same `None` dispatch sends, rendered for HTTP.
    """

    card_id: str
    markdown: Optional[str] = None
    source: SpecContextSource
    criteria_count: int = 0
    test_ref_count: int = 0
    estimated_tokens: int = 0
    truncated: bool = False
    dropped: list[str] = []
    #: The cap the assembler truncates against. Echoed so a reader can see how
    #: close a bundle is to it without knowing the constant.
    budget_tokens: int = SPEC_CONTEXT_MAX_TOKENS
    budget_bytes: int = SPEC_CONTEXT_MAX_BYTES
    #: Where the wrapper materialises `markdown` inside the container.
    container_path: str = SPEC_CONTEXT_PATH


@router.get("/api/cards/{card_id}/spec-context", response_model=SpecContextRead)
async def get_card_spec_context(
    card_id: str, db: AsyncSession = Depends(get_db)
) -> SpecContextRead:
    """The curated spec bundle an agent step for this card would receive.

    Read-only and side-effect free: the bundle is derived at dispatch and
    stored nowhere, so this recomputes it rather than reading a cache that
    could disagree with what the next run would actually send.

    `repo_id` comes from the card. Dispatch scopes test refs to the RUN's repo
    (the workspace is the authority for which paths exist); for a card whose
    repo differs from a run's, this preview is the card's own repo's answer -
    which is the only repo a card has outside a run.
    """
    card = (
        await db.execute(select(Card).where(Card.id == card_id))
    ).scalar_one_or_none()
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")

    bundle: Optional[dict[str, Any]] = await build_spec_context(
        db, card_id=card_id, repo_id=card.repo_id
    )

    if bundle is None:
        # 200, not 404: "no spec context" is a real, correct answer about a
        # real card, and it is the answer dispatch would act on.
        return SpecContextRead(
            card_id=card_id,
            markdown=None,
            source=SpecContextSource(card_id=card_id),
        )

    source = bundle.get("source") or {}
    return SpecContextRead(
        card_id=card_id,
        markdown=bundle["markdown"],
        source=SpecContextSource(
            card_id=source.get("card_id") or card_id,
            feature_id=source.get("feature_id"),
            user_story_id=source.get("user_story_id"),
        ),
        criteria_count=bundle.get("criteria_count", 0),
        test_ref_count=bundle.get("test_ref_count", 0),
        estimated_tokens=bundle.get("estimated_tokens", 0),
        truncated=bool(bundle.get("truncated", False)),
        dropped=list(bundle.get("dropped") or []),
    )
