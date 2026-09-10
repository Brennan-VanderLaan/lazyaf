"""Wire contract for the diagnostics surface.

Two properties are contractual rather than incidental, and both exist because
the artifact this describes is destined for a PUBLIC issue tracker:

**No field here can carry a secret value.** Settings arrive as a name, a
boolean, and an optional value that the service only fills for an allowlisted
field. Containers arrive without ``Env``, ``Cmd`` or mount sources. There is
deliberately no ``value``-shaped field that a future change could route a
credential through by accident, and ``SettingEntryOut.value`` is documented as
allowlist-only so the next person to touch it knows the rule before they add
a field.

**Redaction counts are nullable, and null does not mean zero.** If the shared
redactor's placeholder format is not recognised, ``values_redacted`` is
``None`` and the UI must render "unknown", never "0". A document that says
"0 values redacted" when redaction silently failed is worse than one that
admits it does not know - it is the single sentence in this feature most
likely to get a credential published.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas._datetime import UTCDateTime


# =============================================================================
# System facts
# =============================================================================


class StalenessOut(BaseModel):
    """Is the running process executing the code that is on disk?

    THE field of this whole feature. `stale=true` with a 25-hour uptime is the
    bug that cost two days, stated in one boolean.
    """

    stale: bool = Field(
        description=(
            "True when source files on disk are newer than this process, i.e. "
            "the container is running code that has since changed underneath it."
        )
    )
    process_started_at: UTCDateTime
    process_uptime_seconds: float
    checked_files: int
    stale_file_count: int
    stale_files: list[str] = Field(
        default_factory=list,
        description="Repo-relative paths, capped. Paths only - never contents.",
    )
    newest_file: str | None = None
    newest_file_mtime: UTCDateTime | None = None
    remedy: str | None = Field(
        default=None,
        description=(
            "The command that actually fixes it. `up --build` does NOT recreate "
            "a container whose config is unchanged, which is how this happens."
        ),
    )
    limitations: list[str] = Field(
        default_factory=list,
        description="What this probe cannot see, stated rather than implied.",
    )


class GitOut(BaseModel):
    """The checkout's HEAD, or an honest account of why it is unknown.

    Frequently unavailable: compose bind-mounts `backend/app` and
    `backend/alembic`, so a container has no `.git`. `available=false` with a
    reason, never a fabricated or build-time-baked SHA.
    """

    available: bool
    reason: str | None = None
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    dirty_file_count: int | None = Field(
        default=None,
        description="Count only. Filenames are the user's private work in progress.",
    )


class MigrationsOut(BaseModel):
    db_revisions: list[str] = Field(default_factory=list)
    disk_heads: list[str] = Field(default_factory=list)
    disk_revision_count: int = 0
    at_head: bool | None = None
    unapplied_count: int | None = None
    unapplied: list[str] = Field(default_factory=list)
    unknown_in_db: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    error: str | None = None


class ContainerOut(BaseModel):
    """One container, projected onto an allowlist.

    Everything `docker inspect` returns that is not named here is dropped:
    `Config.Env` (which holds this deployment's auth secrets), `Cmd`,
    `Entrypoint`, and mount sources (which carry the host username). Mounts
    survive as a count.
    """

    name: str
    compose_project: str | None = None
    compose_service: str | None = None
    image: str | None = None
    image_id: str | None = None
    state: str | None = None
    health: str | None = None
    started_at: UTCDateTime | None = None
    uptime_seconds: float | None = None
    restart_count: int | None = None
    mount_count: int = 0
    networks: list[str] = Field(default_factory=list)


class ContainersOut(BaseModel):
    available: bool
    reason: str | None = Field(
        default=None,
        description="Why the inventory is empty. Never an empty list with no reason.",
    )
    containers: list[ContainerOut] = Field(
        default_factory=list,
        description="RUNNING LazyAF containers, oldest first.",
    )
    exited_summary: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Stopped LazyAF containers grouped by image. Grouped rather than "
            "listed: a dev host accumulates hundreds of finished step "
            "containers, and enumerating them buries the rows that matter."
        ),
    )
    exited_count: int = 0
    other_containers_on_host: int = Field(
        default=0,
        description=(
            "Containers on this host that are not LazyAF's. A COUNT, never "
            "names: publishing them would disclose unrelated private projects."
        ),
    )
    oldest_lazyaf: str | None = None
    newest_lazyaf: str | None = None
    spread_warning: str | None = None
    spread_headline: str | None = None


class SettingEntryOut(BaseModel):
    name: str
    present: bool
    value: str | None = Field(
        default=None,
        description=(
            "Rendered value, ONLY for settings on the service's reviewed "
            "allowlist. None means deliberately withheld - which is not the "
            "same as not set (see `present`)."
        ),
    )
    note: str | None = None


class SettingsOut(BaseModel):
    settings: list[SettingEntryOut] = Field(default_factory=list)
    endpoint_secret_count: int = Field(
        default=0,
        description=(
            "How many LAZYAF_ENDPOINT_* variables are set. Counted, not listed: "
            "the operator-chosen suffix routinely names a customer."
        ),
    )
    env_names: list[str] = Field(default_factory=list)
    error: str | None = None


class SystemOut(BaseModel):
    """The system strip: what is actually running here."""

    collected_at: UTCDateTime
    headline_problems: list[str] = Field(
        default_factory=list,
        description="One line per detected problem, most alarming first. Empty is good news.",
    )
    problem_titles: list[str] = Field(
        default_factory=list,
        description=(
            "A short headline per problem, same order as `headline_problems`. "
            "Use these for a title or a badge; use the summaries for body text."
        ),
    )
    staleness: StalenessOut
    git: GitOut
    migrations: MigrationsOut
    containers: ContainersOut
    settings: SettingsOut
    python_version: str
    platform: str
    in_container: bool
    container_started_at: UTCDateTime | None = None
    log_records_held: int
    log_records_dropped: int
    first_log_record_at: UTCDateTime | None = None
    root_log_level: str = Field(
        default="NOTSET",
        description=(
            "The root logger's effective level. The ring buffer only sees what "
            "the root logger admits, so a root at WARNING is why an INFO-level "
            "log view would be empty. Surfaced so that is never a mystery."
        ),
    )


# =============================================================================
# Logs
# =============================================================================


class LogRecordOut(BaseModel):
    timestamp: UTCDateTime
    level: str
    logger: str
    message: str
    exception: str | None = None


class LogsOut(BaseModel):
    records: list[LogRecordOut] = Field(default_factory=list)
    held: int
    dropped: int = Field(
        default=0,
        description=(
            "Records evicted by the ring buffer before this read. Reported so "
            "'412 records' is never mistaken for 'everything that happened'."
        ),
    )
    capacity: int
    redacted: bool = Field(
        default=True,
        description=(
            "Always true. These records are redacted on the way to the screen "
            "as well as into a bundle - a screenshot of an unredacted token is "
            "the same leak with extra steps."
        ),
    )


# =============================================================================
# Bundles
# =============================================================================

SourceName = Literal[
    "system", "containers", "settings", "backend_log", "errors", "run", "job"
]


class BundleRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    what_happened: str | None = Field(
        default=None,
        max_length=8000,
        description="The reporter's own description. Free text, redacted like everything else.",
    )
    sources: list[SourceName] | None = Field(
        default=None,
        description=(
            "Which sources to include. None means the default set, which "
            "EXCLUDES `run` and `job`: those carry agent step logs, the "
            "highest-risk content in the product, so they are opt-in."
        ),
    )
    pipeline_run_id: str | None = None
    job_id: str | None = None
    log_limit: int = Field(default=500, ge=1, le=5000)


class BundleMemberOut(BaseModel):
    name: str
    title: str
    bytes: int
    lines: int
    risk: Literal["low", "medium", "high"]
    note: str | None = None
    truncated: bool = False
    truncation_note: str | None = Field(
        default=None,
        description="Exactly what was cut. Never a silent truncation.",
    )
    redaction_counts: dict[str, int] = Field(default_factory=dict)
    redaction_failed: bool = Field(
        default=False,
        description=(
            "The redactor failed on this member and its content was WITHHELD "
            "rather than emitted un-redacted. Surfaced so a section that "
            "vanished is never mistaken for a section that was empty."
        ),
    )
    text: str = Field(
        description=(
            "The member's FINAL content - already redacted and already "
            "truncated. The preview renders this and the download serves the "
            "document assembled from these same bytes, so there is no second "
            "code path between what was reviewed and what was sent."
        )
    )


class BundleOut(BaseModel):
    id: str
    created_at: UTCDateTime
    expires_at: UTCDateTime
    title: str
    what_happened: str | None = None
    total_bytes: int
    values_redacted: int | None = Field(
        default=None,
        description=(
            "How many values redaction removed. NULL means the count could not "
            "be determined - render 'unknown', never '0'."
        ),
    )
    redaction_counts: dict[str, int] = Field(default_factory=dict)
    redaction_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Ways this bundle's redaction was less complete than it should be "
            "(e.g. the known-value pass was unavailable, or a member failed). "
            "Empty is the good case. Render these prominently - they describe "
            "how the artifact is less safe than it looks."
        ),
    )
    members: list[BundleMemberOut] = Field(default_factory=list)
    download_url: str
    upload_available: bool = Field(
        default=False,
        description="Always false in this phase - see `upload_unavailable_reason`.",
    )
    upload_unavailable_reason: str = Field(
        description=(
            "Why there is no upload button. Stated as a decision rather than "
            "shipped as a greyed-out control."
        )
    )
