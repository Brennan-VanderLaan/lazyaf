"""Diagnostics API: the system strip, the log view, and the bug-report bundle.

WHAT THIS SURFACE IS FOR
========================
The owner spent two days on bugs the product could not describe. The most
expensive one was a backend container running 25-hour-old code while the
bind-mounted files on disk had moved on - a state with no symptom that points
at it and no way to see it from inside the product. ``GET
/api/diagnostics/system`` answers that in one field, and everything else here
is built around getting that answer into a form a human can attach to an
issue.

THERE IS NO UPLOAD ENDPOINT, AND THAT IS THE DESIGN
===================================================
Creating a GitHub issue needs a token with write scope, held by a process
where every human-facing router is unauthenticated and both compose files
publish 0.0.0.0 with the docker socket mounted. Milestone 14 refused to store
model API keys for a weaker version of that reason; a token that can publish
to a public repo under the owner's name is strictly worse than one that can
spend money at a model provider.

It is also unnecessary. The bundle is a markdown document. It downloads, it
pastes into an issue, and it needs no credential at all - so the phase that
needs no token ships first and is independently complete. The follow-on that
needs none either is a prefilled `issues/new?title=...&body=...` link, where
GitHub's own composer opens unsubmitted and a human presses Submit.

That last part is not merely convenient. Automatic redaction cannot remove
source code quoted in an agent step log, a prompt, or a customer's name - so
the actual defence is a human reading the bundle before it goes anywhere. A
one-click upload would delete that defence. Its absence here is what keeps
the review structural rather than advisory.

FAIL-CLOSED ON REDACTION
========================
Every endpoint that returns log text resolves the shared redactor first and
answers **503** if it cannot. Never a degraded mode, never a banner: the
person hitting this is already frustrated, already debugging something else,
and about to paste the result somewhere permanent.

AUTHENTICATION, STATED HONESTLY
===============================
There is none, here or anywhere else in this API. This router does not create
that exposure - `GET /api/model-endpoints`, `GET /api/jobs/{id}/logs` and the
git server are already reachable by anyone who can reach the port - but it
does CONCENTRATE it: one request now returns a curated summary of the whole
system. The mitigations are an unguessable bundle id, a 15-minute TTL, and
nothing ever touching disk. They are mitigations, not a fix, and the fix is
authentication on the API.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.diagnostics import (
    BundleMemberOut,
    BundleOut,
    BundleRequest,
    ContainerOut,
    ContainersOut,
    GitOut,
    LogRecordOut,
    LogsOut,
    MigrationsOut,
    SettingEntryOut,
    SettingsOut,
    StalenessOut,
    SystemOut,
)
from app.services import diagnostics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

# Attach the backend log ring buffer at IMPORT of this router, which happens
# when main.py registers it.
#
# Not in main.py's lifespan, for two reasons. The records worth having start
# before lifespan finishes - startup logging is precisely the part that
# answers "when did this process boot and what did it complain about on the
# way up" - and a handler attached later would miss them. And main.py is a
# file this change touches for router registration only, so the install lives
# with the thing that needs it. Idempotent.
diagnostics.install_log_buffer()


UPLOAD_UNAVAILABLE_REASON = (
    "Uploading to GitHub is not implemented, on purpose. It would need a "
    "write-scoped token held by a process whose every endpoint is "
    "unauthenticated and published on 0.0.0.0 - and it would remove the review "
    "step that automatic redaction depends on, since redaction cannot remove "
    "source code, prompts, or customer names from an agent step log. Download "
    "the bundle, read it, and paste or attach it yourself."
)


async def _require_redactor(db, *, for_view: bool = False) -> diagnostics.ResolvedRedactor:
    """Resolve the shared redactor or refuse the request.

    503 rather than 500: the dependency is absent, not broken, and the request
    is worth retrying once it is present. The reason travels in the detail so
    an operator is not left guessing which module failed to import.

    The salt lives on the redactor instance, so instance lifetime decides
    which texts share a placeholder for the same secret - and the two callers
    want opposite things:

    * a BUNDLE gets a fresh instance, so its digests are its own and a value
      published in one issue cannot be matched against the same value
      published in another;
    * the LOG VIEW reuses one for a few minutes, so a placeholder does not
      change identity between two 3-second polls - which would both flicker
      and destroy the only thing the digest is there to say.
    """
    try:
        refs = await diagnostics._endpoint_refs(db)
        # The VIEW reuses one instance so placeholders stay stable across
        # polls; a BUNDLE always gets a fresh one so its salt is its own.
        if for_view:
            return diagnostics.redactor_for_view(endpoint_refs=refs)
        return diagnostics.resolve_redactor(endpoint_refs=refs)
    except diagnostics.RedactorUnavailable as exc:
        logger.error("diagnostics refused: redactor unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                f"{exc} Diagnostics are unavailable until the shared redactor "
                "can be loaded; no un-redacted output is served in the meantime."
            ),
        ) from exc


# =============================================================================
# The system strip
# =============================================================================


def _staleness_out(report) -> StalenessOut:
    return StalenessOut(
        stale=report.stale,
        process_started_at=report.process_started_at,
        process_uptime_seconds=report.process_uptime_seconds,
        checked_files=report.checked_files,
        stale_file_count=report.stale_file_count,
        stale_files=report.stale_files,
        newest_file=report.newest_file,
        newest_file_mtime=report.newest_file_mtime,
        remedy=report.remedy,
        limitations=report.limitations,
    )


def _containers_out(report) -> ContainersOut:
    return ContainersOut(
        available=report.available,
        reason=report.reason,
        containers=[ContainerOut(**vars(c)) for c in report.containers],
        exited_summary=report.exited_summary,
        exited_count=report.exited_count,
        other_containers_on_host=report.other_containers_on_host,
        oldest_lazyaf=report.oldest_lazyaf,
        newest_lazyaf=report.newest_lazyaf,
        spread_warning=report.spread_warning,
        spread_headline=report.spread_headline,
    )


def _settings_out(report) -> SettingsOut:
    return SettingsOut(
        settings=[SettingEntryOut(**vars(s)) for s in report.settings],
        endpoint_secret_count=report.endpoint_secret_count,
        env_names=report.env_names,
        error=report.error,
    )


@router.get("/system", response_model=SystemOut)
async def get_system(
    include_containers: bool = Query(
        True,
        description="Inspect the docker daemon. Off skips it entirely (and says so).",
    ),
    db: AsyncSession = Depends(get_db),
):
    """What is actually running here.

    Cheap enough to poll and the first thing the Logs view renders, because
    when the answer is "your process is 25 hours old and three files have
    changed" nothing else on the page matters.
    """
    facts = await diagnostics.collect_runtime_facts(
        db, include_containers=include_containers
    )
    return SystemOut(
        collected_at=facts.collected_at,
        headline_problems=facts.headline_problems,
        problem_titles=facts.problem_titles,
        staleness=_staleness_out(facts.staleness),
        git=GitOut(**vars(facts.git)),
        migrations=MigrationsOut(**vars(facts.migrations)),
        containers=_containers_out(facts.containers),
        settings=_settings_out(facts.settings),
        python_version=facts.python_version,
        platform=facts.platform,
        in_container=facts.in_container,
        container_started_at=facts.container_started_at,
        log_records_held=facts.log_records_held,
        log_records_dropped=facts.log_records_dropped,
        first_log_record_at=facts.first_log_record_at,
        root_log_level=facts.root_log_level,
    )


# =============================================================================
# The log view
# =============================================================================


@router.get("/logs", response_model=LogsOut)
async def get_logs(
    limit: int = Query(500, ge=1, le=5000),
    min_level: str = Query(
        "INFO", description="INFO | WARNING | ERROR | CRITICAL - the floor, inclusive."
    ),
    db: AsyncSession = Depends(get_db),
):
    """The backend log ring buffer, redacted.

    Redacted on the way to the SCREEN, not only into bundles. The owner will
    screenshot this view, and a screenshot of an unredacted token is the same
    leak with extra steps - so it is the same redactor and the same
    fail-closed rule.
    """
    redactor = await _require_redactor(db, for_view=True)
    level_no = logging.getLevelName(min_level.upper())
    if not isinstance(level_no, int):
        raise HTTPException(
            status_code=422,
            detail=f"min_level must be one of INFO, WARNING, ERROR, CRITICAL - got {min_level!r}",
        )

    records = diagnostics.log_buffer.records(min_level=level_no, limit=limit)
    out: list[LogRecordOut] = []
    for record in records:
        message, _, _ = diagnostics.redact_text(redactor.redactor, record.message)
        exception = None
        if record.exception:
            exception, _, _ = diagnostics.redact_text(
                redactor.redactor, record.exception
            )
        out.append(
            LogRecordOut(
                timestamp=record.when,
                level=record.level,
                logger=record.logger_name,
                message=message,
                exception=exception,
            )
        )
    return LogsOut(
        records=out,
        held=len(diagnostics.log_buffer),
        dropped=diagnostics.log_buffer.dropped,
        capacity=diagnostics.log_buffer.capacity,
    )


# =============================================================================
# The bundle
# =============================================================================


def _bundle_out(bundle) -> BundleOut:
    return BundleOut(
        id=bundle.id,
        created_at=bundle.created_at,
        expires_at=bundle.expires_at,
        title=bundle.title,
        what_happened=bundle.what_happened,
        total_bytes=bundle.total_bytes,
        values_redacted=bundle.values_redacted,
        redaction_counts=bundle.redaction_counts,
        redaction_notes=bundle.redaction_notes,
        members=[
            BundleMemberOut(
                name=m.name,
                title=m.title,
                bytes=m.bytes,
                lines=m.lines,
                risk=m.risk,
                note=m.note,
                truncated=m.truncated,
                truncation_note=m.truncation_note,
                redaction_counts=m.redaction_counts,
                redaction_failed=m.redaction_failed,
                text=m.text,
            )
            for m in bundle.members
        ],
        download_url=f"/api/diagnostics/bundle/{bundle.id}/download",
        upload_available=False,
        upload_unavailable_reason=UPLOAD_UNAVAILABLE_REASON,
    )


@router.post("/bundle", response_model=BundleOut, status_code=201)
async def create_bundle(request: BundleRequest, db: AsyncSession = Depends(get_db)):
    """Build a bug-report bundle and return it for review.

    The response carries each member's FINAL bytes - already redacted, already
    truncated - so the review sheet renders the artifact itself rather than a
    rehearsal of it. `/download` then serves the document assembled from those
    same members. There is no "we will redact it on the way out" step for a
    bug to hide in.
    """
    redactor = await _require_redactor(db)
    try:
        bundle = await diagnostics.build_bundle(
            db,
            title=request.title,
            what_happened=request.what_happened,
            sources=request.sources,
            pipeline_run_id=request.pipeline_run_id,
            job_id=request.job_id,
            log_limit=request.log_limit,
            redactor=redactor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    diagnostics.bundle_store.put(bundle)
    return _bundle_out(bundle)


@router.get("/bundle/{bundle_id}", response_model=BundleOut)
async def get_bundle(bundle_id: str):
    """Re-read a built bundle. 404 once its 15-minute TTL has passed."""
    bundle = diagnostics.bundle_store.get(bundle_id)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No bundle {bundle_id}. Bundles are held in memory for "
                f"{int(diagnostics.BUNDLE_TTL.total_seconds() // 60)} minutes and "
                "never written to disk; build a new one."
            ),
        )
    return _bundle_out(bundle)


@router.get("/bundle/{bundle_id}/download", response_class=PlainTextResponse)
async def download_bundle(bundle_id: str):
    """The bundle as one markdown document.

    Markdown rather than a zip because the stated use is pasting it into a
    GitHub issue, and because a single reviewable document is what makes the
    human-review step actually happen. Served as an attachment so a browser
    saves it instead of rendering it.
    """
    bundle = diagnostics.bundle_store.get(bundle_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail=f"No bundle {bundle_id}.")
    filename = f"lazyaf-bugreport-{bundle.id[:8]}.md"
    return PlainTextResponse(
        content=bundle.document,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
