"""Diagnostics: what is actually running here, and a bundle a human can read.

THE QUESTION THIS MODULE EXISTS TO ANSWER
=========================================
Not "show me some logs". The expensive question - the one that cost two days -
is **"is the process I am debugging running the code I am reading?"**

The bug that motivated this: `docker compose up --build` does NOT recreate a
container whose config is unchanged, and `backend/app` is bind-mounted. So a
container can sit for 25 hours executing the code it imported at start while
the files underneath it move on. Every symptom points at the application
("branch not found" on a branch that exists) and nothing in the product says
the one thing that matters. `collect_runtime_facts()` says it in one line.

That is why the ordering here is what it is: `runtime` first, everything else
after. A bundle whose first section says "3 app files are newer than this
process" has already paid for the feature.

WHAT IS DELIBERATELY NOT COLLECTED
==================================
This bundle is built to be attached to an issue on a **public** repository.
Everything below is an allowlist, never a denylist, because the failure mode
of a forgotten denylist entry is permanent and indexed:

* container ``Config.Env`` - it holds ``LAZYAF_STEP_AUTH_SECRET``,
  ``LAZYAF_RUNNER_AUTH_SECRET`` and ``ANTHROPIC_API_KEY`` on this very
  service. ``docker inspect`` hands it over cheerfully. It never enters a
  bundle, not redacted, not truncated - absent.
* container ``Cmd`` / ``Entrypoint`` / ``Mounts`` - command lines carry
  arguments and mount sources carry the host username. Mounts are a COUNT.
* the names of containers that are not ours. This host runs
  ``finance_me_captain_db``; publishing that name publishes the existence of
  an unrelated private project. Non-LazyAF containers are a COUNT with no
  names (see ``_is_lazyaf_container``).
* settings VALUES, except a small allowlist of ones that cause bugs and
  cannot be a credential. ``ANTHROPIC_API_KEY: set`` is useful;
  ``ANTHROPIC_API_KEY: sk-ant-...`` is a catastrophe.
* ``LAZYAF_ENDPOINT_*`` variable NAMES. The suffix is operator-chosen and is
  very often a company or product name (``LAZYAF_ENDPOINT_ACME_PROD``). The
  name alone is a disclosure, so these are counted, never listed.
* request/response bodies, git diffs, workspace file contents, ``.env``.

REDACTION IS FAIL-CLOSED, AND IT IS NOT MINE
============================================
Every byte of text leaves this module through the shared redactor
(``app.services.redaction``, Lane A - the same rules
``.github/scripts/secret_patterns.py`` gates CI with, R3). This module does
not own a single credential regex and must never grow one.

If that module cannot be imported, ``build_bundle`` raises
``RedactorUnavailable`` and the router answers 503. It does NOT emit an
unredacted bundle with a warning banner: a warning banner is advice, and the
artifact this produces gets pasted into a public issue by someone who is
already frustrated and debugging something else.

AND REDACTION IS A SAFETY NET, NOT THE DEFENCE
==============================================
Pattern matching catches known credential SHAPES. It cannot catch the source
code of a private repo quoted in an agent step log, a prompt carrying a
confidential decision, a customer's name in a branch, or a host path with a
username in it. The actual defence is that a human downloads this and reads
it before uploading anything, which is why there is no upload path here at
all. ``BUNDLE_README`` says so inside the artifact, so the warning travels
with the file rather than living in a UI the reader never saw.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)


# =============================================================================
# Process identity
# =============================================================================
#
# Captured at IMPORT, which is the closest portable stand-in for "when did
# this interpreter start running the code it is running". Deliberately not
# `/proc/uptime`: inside a container that returns the HOST's uptime. Measured
# on this machine - 39.5 hours reported inside a container that had been up 34
# minutes. Anyone reaching for it will get a plausible number that is wrong,
# which is the worst kind.
#
# Importing diagnostics happens slightly AFTER real process start, so this
# value is a touch late, which biases the staleness probe toward saying
# "fresh". Conservative in the right direction: a false "stale" would send
# someone chasing a rebuild they do not need.
PROCESS_STARTED_AT: float = time.time()

#: Filesystem clock jitter and bind-mount timestamp granularity. A file has to
#: be at least this much newer than the process before we call it stale.
STALE_TOLERANCE_SECONDS = 5.0

#: Root of the application package: `backend/app`. This file is
#: `backend/app/services/diagnostics.py`.
APP_ROOT = Path(__file__).resolve().parent.parent

#: `backend/` - holds `alembic/`, which compose bind-mounts separately and
#: which therefore goes stale independently of `app/`.
BACKEND_ROOT = APP_ROOT.parent


def _utc(ts: float | None) -> datetime | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _human_duration(seconds: float | None) -> str:
    """`25h 4m`. Uptime is read at a glance or not at all."""
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# =============================================================================
# The backend log ring buffer
# =============================================================================
#
# Nothing captured backend logs before this. `main.py` calls
# `logging.basicConfig` and the records go to the container's stdout, where
# they are reachable by `docker logs` and by nothing the product can render.
#
# The FIRST record in this buffer is a load-bearing fact all by itself: a
# first record 25 hours old sitting next to a container that reports 22
# minutes of uptime is the whole staleness bug, visible without any probe.


@dataclass(frozen=True)
class LogRecord:
    """One captured record. Message is pre-rendered; args are mutable and a
    handler that stores the record object would render it later against
    whatever the caller's list had become."""

    timestamp: float
    level: str
    level_no: int
    logger_name: str
    message: str
    exception: str | None = None

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)


#: Per-record message cap. A single log line that is a megabyte of model
#: output would otherwise evict the entire buffer on its own. Truncation is
#: stated in the record, never silent.
MAX_RECORD_CHARS = 4000

#: How many records the buffer holds. 5000 x ~200 bytes is a few MB.
DEFAULT_LOG_CAPACITY = 5000


class LogRingBuffer(logging.Handler):
    """The last N log records, in memory, for the diagnostics surface.

    Never raises out of ``emit``: a logging handler that can throw turns every
    log call in the process into a possible failure. Every branch that could
    fail is caught and dropped, because a missing diagnostic record is a much
    smaller problem than a backend that dies while reporting one.
    """

    def __init__(self, capacity: int = DEFAULT_LOG_CAPACITY) -> None:
        super().__init__()
        self.capacity = capacity
        self._records: deque[LogRecord] = deque(maxlen=capacity)
        #: Records dropped by the ring's own eviction, so "412 shown" can be
        #: reported next to "1,908 dropped" rather than implying 412 is all
        #: that ever happened (R4).
        self.dropped = 0

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial guard
        try:
            self._append(record)
        except Exception:
            # Deliberately silent, and deliberately NOT self.handleError():
            # handleError writes to stderr, and a broken record inside a
            # request handler would then print a traceback per log call.
            pass

    def _append(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception as exc:
            message = f"<un-renderable log record: {type(exc).__name__}>"
        if len(message) > MAX_RECORD_CHARS:
            omitted = len(message) - MAX_RECORD_CHARS
            message = (
                message[:MAX_RECORD_CHARS]
                + f"\n... [record truncated: {omitted:,} more characters]"
            )

        exception = None
        if record.exc_info:
            try:
                exception = logging.Formatter().formatException(record.exc_info)
            except Exception:
                exception = "<exception text unavailable>"

        if len(self._records) == self._records.maxlen:
            self.dropped += 1
        self._records.append(
            LogRecord(
                timestamp=record.created,
                level=record.levelname,
                level_no=record.levelno,
                logger_name=record.name,
                message=message,
                exception=exception,
            )
        )

    # -- readers ---------------------------------------------------------

    def records(self, *, min_level: int = 0, limit: int | None = None) -> list[LogRecord]:
        out = [r for r in self._records if r.level_no >= min_level]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def __len__(self) -> int:
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()
        self.dropped = 0


#: THE buffer. One per process; installed by `install_log_buffer()`.
log_buffer = LogRingBuffer()

_buffer_installed = False


def install_log_buffer(level: int = logging.INFO) -> LogRingBuffer:
    """Attach the ring buffer to the root logger. Idempotent.

    Called at import of ``app.routers.diagnostics`` rather than from
    ``main.py``'s lifespan, for two reasons. It has to be attached before the
    records worth having are emitted - lifespan startup logging is exactly the
    part that answers "when did this process boot and what did it complain
    about on the way up" - and ``main.py`` is a file this change touches for
    router registration only.
    """
    global _buffer_installed
    if _buffer_installed:
        return log_buffer
    log_buffer.setLevel(level)
    logging.getLogger().addHandler(log_buffer)
    _buffer_installed = True
    return log_buffer


# =============================================================================
# Source A: is the running code the code on disk?
# =============================================================================


@dataclass
class StalenessReport:
    stale: bool
    process_started_at: datetime
    process_uptime_seconds: float
    checked_files: int
    stale_files: list[str] = field(default_factory=list)
    stale_file_count: int = 0
    newest_file_mtime: datetime | None = None
    newest_file: str | None = None
    remedy: str | None = None
    #: Honest about what this probe cannot see (R1).
    limitations: list[str] = field(default_factory=list)


#: Report at most this many paths by name; the rest are a count. A rebuild
#: touches every file and a list of 300 names is noise, not a diagnosis.
MAX_STALE_FILES_LISTED = 25


def _iter_python_files(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def collect_staleness(
    *, roots: Sequence[Path] | None = None, process_started_at: float | None = None
) -> StalenessReport:
    """Compare on-disk source mtimes to this process's start.

    A file whose mtime is newer than the process is code that exists on disk
    and is NOT what this interpreter imported. That is the entire 25-hour bug,
    detectable in about thirty lines.

    Walks the tree rather than ``sys.modules`` on purpose: a walk also catches
    a file that was ADDED after start (a new router, a new migration), which
    by definition cannot be in ``sys.modules``, and that is the shape of "I
    added an endpoint and the API still 404s".

    Reports module-relative paths only, never contents, and the paths are the
    public repo's own layout.
    """
    started = PROCESS_STARTED_AT if process_started_at is None else process_started_at
    scan_roots = [APP_ROOT, BACKEND_ROOT / "alembic"] if roots is None else list(roots)

    threshold = started + STALE_TOLERANCE_SECONDS
    checked = 0
    stale: list[tuple[float, str]] = []
    newest_mtime: float | None = None
    newest_name: str | None = None

    for root in scan_roots:
        for path in _iter_python_files(root):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            checked += 1
            try:
                rel = path.relative_to(BACKEND_ROOT).as_posix()
            except ValueError:
                rel = path.name
            if newest_mtime is None or mtime > newest_mtime:
                newest_mtime, newest_name = mtime, rel
            if mtime > threshold:
                stale.append((mtime, rel))

    stale.sort(reverse=True)
    listed = [name for _, name in stale[:MAX_STALE_FILES_LISTED]]

    remedy = None
    if stale:
        # Name the command. The failure mode this detects is one that
        # `up --build` does not fix, so telling someone to "rebuild" is the
        # advice that already failed them.
        remedy = (
            "docker compose up -d --force-recreate backend "
            "(plain `up --build` will NOT recreate a container whose config "
            "is unchanged, which is how the running code drifted from disk)"
        )

    return StalenessReport(
        stale=bool(stale),
        process_started_at=_utc(started),
        process_uptime_seconds=max(0.0, time.time() - started),
        checked_files=checked,
        stale_files=listed,
        stale_file_count=len(stale),
        newest_file_mtime=_utc(newest_mtime),
        newest_file=newest_name,
        remedy=remedy,
        limitations=[
            "Detects Python source newer than this process. It does NOT see a "
            "stale frontend bundle, a stale runner-agent image, or a stale "
            "step image - those show up only as container ages below.",
            "Compares modification times, not content. A file touched but not "
            "changed reads as stale; a change that preserves the mtime does not.",
        ],
    )


# =============================================================================
# Source E: the git HEAD of the checkout
# =============================================================================


@dataclass
class GitReport:
    available: bool
    reason: str | None = None
    commit: str | None = None
    branch: str | None = None
    dirty: bool | None = None
    dirty_file_count: int | None = None


def _find_git_dir(start: Path) -> Path | None:
    for candidate in [start, *start.parents]:
        git = candidate / ".git"
        if git.is_dir():
            return git
        if git.is_file():
            # A worktree/submodule pointer file: `gitdir: <path>`.
            try:
                text = git.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                target = Path(text.split(":", 1)[1].strip())
                if not target.is_absolute():
                    target = (candidate / target).resolve()
                return target if target.is_dir() else None
    return None


def collect_git(start: Path | None = None) -> GitReport:
    """The checkout's HEAD, when the checkout is visible at all.

    Very often it is NOT. ``docker-compose.yml`` mounts ``./backend/app`` and
    ``./backend/alembic`` and nothing else, so a container has no ``.git`` -
    verified on the running stack. On the host (dev, and the future CLI) it is
    right there, so the field is live where it can be and honestly empty where
    it cannot.

    THE THING NOT DONE HERE, deliberately: a build-time ``LAZYAF_GIT_SHA``
    baked into the image. With a bind mount that value is a lie by
    construction - the image was built at commit X while the files being
    executed are at commit Y - and shipping it would fake exactly the green
    this whole module exists to remove.

    SHA, branch, dirty flag and a COUNT. Never a diff, never a filename: the
    working tree is the user's private source.
    """
    root = BACKEND_ROOT if start is None else start
    git_dir = _find_git_dir(root)
    if git_dir is None:
        # NO ABSOLUTE PATH IN THIS MESSAGE. It is emitted precisely when the
        # walk failed, and on a host the path walked is a real filesystem path
        # that routinely carries a username (`/home/<user>/...`,
        # `C:\Users\<user>\...`). Pattern redaction does not model
        # usernames, so the only safe move is not to put one here.
        return GitReport(
            available=False,
            reason=(
                "no .git found at or above the backend application root - the "
                "LazyAF checkout is not visible from this process. Inside the "
                "backend container this is expected: compose bind-mounts "
                "backend/app and backend/alembic, not the repository. Run the "
                "host-side collector, or add `- .:/host-repo:ro` to the backend "
                "service, to fill this in."
            ),
        )

    commit = branch = None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            branch = ref.rsplit("/", 1)[-1]
            ref_file = git_dir / ref
            if ref_file.is_file():
                commit = ref_file.read_text(encoding="utf-8").strip()
            else:
                packed = git_dir / "packed-refs"
                if packed.is_file():
                    for line in packed.read_text(encoding="utf-8").splitlines():
                        if line.endswith(f" {ref}"):
                            commit = line.split(" ", 1)[0].strip()
                            break
        else:
            commit = head  # detached HEAD
            branch = "(detached)"
    except OSError as exc:
        # Type of failure only - `exc` stringifies to the full path (see above).
        return GitReport(
            available=False,
            reason=f"the git directory could not be read ({type(exc).__name__}).",
        )

    dirty = dirty_count = None
    try:
        import subprocess

        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(git_dir.parent),
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            # COUNT only. The filenames are the user's private work in
            # progress and have no diagnostic value the count lacks.
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            dirty_count = len(lines)
            dirty = dirty_count > 0
    except Exception:
        # git absent, or a repo too large to stat in 10s. Not fatal: the SHA
        # is the part that answers "what is running".
        pass

    return GitReport(
        available=True,
        commit=commit,
        branch=branch,
        dirty=dirty,
        dirty_file_count=dirty_count,
    )


# =============================================================================
# Source D: alembic head in the DB vs the migrations on disk
# =============================================================================


@dataclass
class MigrationReport:
    db_revisions: list[str] = field(default_factory=list)
    disk_heads: list[str] = field(default_factory=list)
    disk_revision_count: int = 0
    at_head: bool | None = None
    unapplied_count: int | None = None
    unapplied: list[str] = field(default_factory=list)
    unknown_in_db: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    error: str | None = None


async def collect_migrations(db) -> MigrationReport:
    """What the database is stamped at, versus what the tree defines.

    Earns its place empirically: the tip of ``main`` is *"fix: commit
    migration 0007, which 0009 has been referencing from main"*, and the
    versions directory still reads 0007 -> 0009 with no 0008. A schema that is
    a revision behind produces failures that look nothing like a migration
    problem, and this is two queries.

    Uses alembic's own ``ScriptDirectory`` rather than parsing the files: the
    revision graph is alembic's to interpret, and a hand-rolled parser would
    be a second implementation that drifts (R3).
    """
    report = MigrationReport()
    try:
        import sqlalchemy as sa
        from alembic.config import Config as AlembicConfig
        from alembic.script import ScriptDirectory

        from app.database import _ALEMBIC_INI_PATH

        config = AlembicConfig(str(_ALEMBIC_INI_PATH))
        script = ScriptDirectory.from_config(config)
        report.disk_heads = list(script.get_heads())
        report.disk_revision_count = sum(1 for _ in script.walk_revisions())

        try:
            result = await db.execute(sa.text("SELECT version_num FROM alembic_version"))
            report.db_revisions = sorted(str(r) for r in result.scalars().all())
        except Exception as exc:
            # A test database built by `Base.metadata.create_all` has no
            # alembic_version table at all. Say that; do not imply a problem.
            report.problems.append(
                f"alembic_version could not be read ({type(exc).__name__}). "
                "Expected on a database created directly from the models "
                "(the test harness does this); a real deployment always has it."
            )
            return report

        if not report.db_revisions:
            report.problems.append(
                "alembic_version exists but is EMPTY - an interrupted stamp. "
                "The schema is unversioned."
            )
            return report

        for revision in report.db_revisions:
            try:
                script.get_revision(revision)
            except Exception:
                report.unknown_in_db.append(revision)
        if report.unknown_in_db:
            report.problems.append(
                "The database is stamped at revision(s) this codebase does not "
                f"define: {', '.join(report.unknown_in_db)}. It was migrated by "
                "a different chain."
            )
            return report

        heads = set(report.disk_heads)
        report.at_head = set(report.db_revisions) == heads
        if not report.at_head:
            unapplied = []
            for revision in script.iterate_revisions("heads", report.db_revisions[0]):
                if revision.revision not in report.db_revisions:
                    unapplied.append(revision.revision)
            report.unapplied = list(reversed(unapplied))
            report.unapplied_count = len(unapplied)
            if unapplied:
                report.problems.append(
                    f"{len(unapplied)} migration(s) on disk have not been applied "
                    "to this database. The running code expects a schema the "
                    "database does not have."
                )
        else:
            report.unapplied_count = 0
        if len(report.disk_heads) > 1:
            report.problems.append(
                f"The migration chain has {len(report.disk_heads)} heads "
                f"({', '.join(report.disk_heads)}) - a branch that was never merged."
            )
    except Exception as exc:  # pragma: no cover - defensive
        report.error = f"{type(exc).__name__}: {exc}"
    return report


# =============================================================================
# Source B: the container inventory
# =============================================================================


@dataclass
class ContainerInfo:
    name: str
    compose_project: str | None
    compose_service: str | None
    image: str | None
    image_id: str | None
    state: str | None
    health: str | None
    started_at: datetime | None
    uptime_seconds: float | None
    restart_count: int | None
    mount_count: int
    networks: list[str] = field(default_factory=list)


@dataclass
class ContainerReport:
    available: bool
    reason: str | None = None
    #: RUNNING LazyAF containers, one row each. This is the literal answer to
    #: "what is actually running", so it is the part that gets enumerated.
    containers: list[ContainerInfo] = field(default_factory=list)
    #: Stopped/exited LazyAF containers, grouped by image rather than listed.
    #: A dev host accumulates hundreds of finished step containers with random
    #: docker names; enumerating them buries the six rows that matter under
    #: seven kilobytes of `quizzical_rosalind | exited | python:3.12`.
    exited_summary: dict[str, int] = field(default_factory=dict)
    exited_count: int = 0
    other_containers_on_host: int = 0
    oldest_lazyaf: str | None = None
    newest_lazyaf: str | None = None
    spread_warning: str | None = None
    spread_headline: str | None = None


#: Compose project names that are ours. Prefix match: `lazyaf`, `lazyaf-qa`,
#: `lazyaf-e2e` are all in; `finance_me_captain` is not.
LAZYAF_PROJECT_PREFIX = "lazyaf"

#: A container attached to one of our networks counts as ours even without a
#: compose project label - that is how a step container spawned by the
#: executor presents.
LAZYAF_NETWORK_PREFIX = "lazyaf"


def _is_lazyaf_container(attrs: dict) -> bool:
    """Ours, or somebody else's private project?

    THE SCOPING RULE, and it is a privacy control rather than a tidiness one.
    This machine runs `finance_me_captain_db`; an inventory that dumps every
    container publishes the existence and naming of unrelated work into a
    public issue. Everything not matched here is counted and never named.
    """
    labels = (attrs.get("Config") or {}).get("Labels") or {}
    project = labels.get("com.docker.compose.project") or ""
    if project.startswith(LAZYAF_PROJECT_PREFIX):
        return True
    if any(str(k).startswith("com.lazyaf.") or str(k).startswith("lazyaf.") for k in labels):
        return True
    networks = ((attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys()
    if any(str(n).startswith(LAZYAF_NETWORK_PREFIX) for n in networks):
        return True
    name = (attrs.get("Name") or "").lstrip("/")
    return name.startswith(LAZYAF_PROJECT_PREFIX)


def _parse_docker_time(value: str | None) -> datetime | None:
    """Docker's RFC3339 with nanoseconds, which `fromisoformat` rejects."""
    if not value or value.startswith("0001-01-01"):
        return None
    text = value.replace("Z", "+00:00")
    match = re.match(r"^(.*\.\d{6})\d*(\+\d{2}:\d{2})$", text)
    if match:
        text = match.group(1) + match.group(2)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _container_info(attrs: dict) -> ContainerInfo:
    """Project ONE container down to an allowlist of fields.

    ALLOWLIST, NOT DENYLIST, and this is the single most dangerous function in
    the module. ``docker inspect`` on this very backend returns
    ``Config.Env`` containing ``LAZYAF_STEP_AUTH_SECRET``,
    ``LAZYAF_RUNNER_AUTH_SECRET`` and ``ANTHROPIC_API_KEY``; it returns
    ``Config.Cmd``, and ``Mounts`` whose sources carry the host username. A
    denylist here would leak all of it the first time docker added a field.
    Nothing reaches the bundle except the names below.
    """
    config = attrs.get("Config") or {}
    state = attrs.get("State") or {}
    labels = config.get("Labels") or {}
    started_at = _parse_docker_time(state.get("StartedAt"))
    uptime = None
    if started_at is not None and state.get("Running"):
        uptime = max(0.0, (_now() - started_at).total_seconds())
    image_id = attrs.get("Image") or ""
    return ContainerInfo(
        name=(attrs.get("Name") or "").lstrip("/"),
        compose_project=labels.get("com.docker.compose.project"),
        compose_service=labels.get("com.docker.compose.service"),
        image=config.get("Image"),
        image_id=image_id[:19] if image_id else None,
        state=state.get("Status"),
        health=((state.get("Health") or {}).get("Status")),
        started_at=started_at,
        uptime_seconds=uptime,
        restart_count=attrs.get("RestartCount"),
        mount_count=len(attrs.get("Mounts") or []),
        networks=sorted(((attrs.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
    )


#: Uptime spread beyond which the fleet is worth a warning. Twelve hours
#: between the oldest and newest LazyAF container means somebody recreated
#: one service and not the others - the exact shape of the bug this feature
#: was built for.
UPTIME_SPREAD_WARNING_SECONDS = 12 * 3600


def _collect_containers_sync() -> ContainerReport:
    try:
        from app.services.execution.local_executor import make_docker_client
    except Exception as exc:  # pragma: no cover - import-time only
        return ContainerReport(available=False, reason=f"docker client unavailable: {exc}")

    try:
        client = make_docker_client()
        raw = client.containers.list(all=True)
    except Exception as exc:
        # Docker genuinely down, socket not mounted, or the T1 no-docker guard.
        # All three are the same answer to the reader: this section is empty
        # and here is why. Never a silently absent section (R4).
        return ContainerReport(
            available=False,
            reason=(
                f"could not reach the docker daemon ({type(exc).__name__}: {exc}). "
                "The container inventory is empty for that reason, not because "
                "nothing is running."
            ),
        )

    running: list[ContainerInfo] = []
    exited: list[ContainerInfo] = []
    others = 0
    for container in raw:
        try:
            attrs = container.attrs or {}
        except Exception:
            continue
        if not _is_lazyaf_container(attrs):
            others += 1
            continue
        info = _container_info(attrs)
        (running if info.uptime_seconds is not None else exited).append(info)

    # Oldest first: the container that has been up longest is the one most
    # likely to be running code nobody has looked at since.
    running.sort(key=lambda c: c.uptime_seconds or 0, reverse=True)

    exited_summary: dict[str, int] = {}
    for info in exited:
        key = info.image or "(unknown image)"
        exited_summary[key] = exited_summary.get(key, 0) + 1

    report = ContainerReport(
        available=True,
        containers=running,
        exited_summary=dict(
            sorted(exited_summary.items(), key=lambda kv: kv[1], reverse=True)
        ),
        exited_count=len(exited),
        other_containers_on_host=others,
    )
    if running:
        oldest, newest = running[0], running[-1]
        report.oldest_lazyaf = oldest.name
        report.newest_lazyaf = newest.name
        spread = (oldest.uptime_seconds or 0) - (newest.uptime_seconds or 0)
        if spread >= UPTIME_SPREAD_WARNING_SECONDS:
            report.spread_headline = (
                f"{_human_duration(spread)} between the oldest and newest running "
                "LazyAF container"
            )
            report.spread_warning = (
                f"{_human_duration(spread)} between the oldest LazyAF container "
                f"({oldest.name}, up {_human_duration(oldest.uptime_seconds)}) and "
                f"the newest ({newest.name}, up {_human_duration(newest.uptime_seconds)}). "
                "Services were recreated at different times, so they may not be "
                "running the same generation of the code."
            )
    return report


async def collect_containers() -> ContainerReport:
    """Container inventory, off the event loop.

    The docker SDK probes the daemon on construction and every call is
    blocking, so this runs in a thread - the same reason
    ``make_docker_client``'s own docstring gives.
    """
    return await asyncio.to_thread(_collect_containers_sync)


# =============================================================================
# Source H: settings, as NAMES and whether they are set
# =============================================================================


@dataclass
class SettingEntry:
    name: str
    present: bool
    #: Rendered value, ONLY for fields on `PRINTABLE_SETTINGS`. None means
    #: "deliberately not shown", which is different from "not set".
    value: str | None = None
    note: str | None = None


@dataclass
class SettingsReport:
    settings: list[SettingEntry] = field(default_factory=list)
    endpoint_secret_count: int = 0
    env_names: list[str] = field(default_factory=list)
    error: str | None = None


#: Settings whose VALUE may be printed. An allowlist, and every entry has been
#: read and judged incapable of carrying a credential. Everything absent from
#: this set is reported as name + present/absent, forever, including any field
#: added to `Settings` after this line was written - which is the property that
#: makes an allowlist the only defensible choice here.
PRINTABLE_SETTINGS = frozenset(
    {
        "app_name",
        "cors_origins",
        "default_runner_type",
        "test_mode",
        "db_echo",
        "container_network",
        "container_git_url_template",
        "container_backend_url",
        "workspace_clone_image",
        "step_default_image",
        "step_working_dir",
        "step_home_dir",
    }
)

#: Fields that are a secret by definition. Listed explicitly rather than
#: inferred from the name so that renaming one cannot quietly promote it into
#: the printable set.
SECRET_SETTINGS = frozenset(
    {
        "step_auth_secret",
        "runner_auth_secret",
        "anthropic_api_key",
        "gemini_api_key",
    }
)

#: Environment variable names worth reporting the PRESENCE of. Names only.
ENV_NAMES_OF_INTEREST = (
    "DATABASE_URL",
    "DOCKER_HOST",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "LAZYAF_STEP_AUTH_SECRET",
    "LAZYAF_STEP_AUTH_SECRET_FILE",
    "LAZYAF_RUNNER_AUTH_SECRET",
    "LAZYAF_RUNNER_AUTH_SECRET_FILE",
    "LAZYAF_TEST_MODE",
    "LAZYAF_DB_ECHO",
    "LAZYAF_DEV_EPHEMERAL_SECRETS",
    "CONTAINER_NETWORK",
    "CONTAINER_BACKEND_URL",
    "STEP_DEFAULT_IMAGE",
    "WORKSPACE_CLONE_IMAGE",
    "WEB_CONCURRENCY",
    "LAZYAF_GPU_NODE_RATES",
)


def collect_settings() -> SettingsReport:
    """Configuration as names and presence.

    The rule the owner set, and it is the right one: ``ANTHROPIC_API_KEY:
    set`` is useful, and the key itself is a catastrophe. Two fields get a
    narrower treatment than "hide it", because the useful diagnostic in them
    is not the value:

    * ``database_url`` reports its DRIVER (``sqlite+aiosqlite``). A postgres
      URL carries ``user:password@host``, and the answer to "which database am
      I on" is the driver plus whether it is the file one.
    * ``docker_host`` reports its SCHEME. A TCP daemon URL can carry a host
      and port that are not the operator's to publish.

    ``LAZYAF_ENDPOINT_*`` variables are a COUNT and never a list: the suffix
    is operator-chosen and routinely names a customer or a product.
    """
    report = SettingsReport()
    try:
        from app.config import get_settings

        settings = get_settings()
        for name in sorted(type(settings).model_fields):
            raw = getattr(settings, name, None)
            present = raw is not None and raw != "" and raw != []
            entry = SettingEntry(name=name, present=present)
            if name in SECRET_SETTINGS:
                entry.note = "secret - never rendered"
            elif name == "database_url":
                driver = str(raw or "").split(":", 1)[0]
                entry.value = f"{driver}:..." if driver else None
                entry.note = "driver only (a URL can carry user:password@host)"
            elif name == "docker_host":
                scheme = str(raw or "").split("://", 1)[0]
                entry.value = f"{scheme}://..." if scheme else None
                entry.note = "scheme only"
            elif name == "gpu_node_rates":
                entry.value = f"{len(raw or {})} node rate(s) configured"
                entry.note = "count only"
            elif name == "default_prompt_template":
                entry.note = "prompt text - never rendered"
            elif name in PRINTABLE_SETTINGS:
                entry.value = repr(raw)
            else:
                # A field added to Settings since this file was written. It
                # gets the SAFE treatment automatically, and says so.
                entry.note = "not on the printable allowlist - value withheld"
            report.settings.append(entry)
    except Exception as exc:
        report.error = f"settings could not be read: {type(exc).__name__}: {exc}"

    try:
        from app.services.model_endpoints.secrets import ENDPOINT_SECRET_PREFIX

        prefix = ENDPOINT_SECRET_PREFIX
    except Exception:
        prefix = "LAZYAF_ENDPOINT_"
    report.endpoint_secret_count = sum(
        1 for name in os.environ if name.startswith(prefix)
    )
    report.env_names = [name for name in ENV_NAMES_OF_INTEREST if os.environ.get(name)]
    return report


# =============================================================================
# The runtime facts: everything above, assembled
# =============================================================================


@dataclass
class RuntimeFacts:
    collected_at: datetime
    staleness: StalenessReport
    git: GitReport
    migrations: MigrationReport
    containers: ContainerReport
    settings: SettingsReport
    python_version: str
    platform: str
    in_container: bool
    container_started_at: datetime | None
    log_records_held: int
    log_records_dropped: int
    first_log_record_at: datetime | None
    #: The ROOT logger's effective level. The ring buffer only ever sees what
    #: the root logger lets through, so a root at WARNING makes the log
    #: section silently empty. Reported so "my log tab is blank" has an
    #: answer on the page instead of being a second mystery to debug.
    root_log_level: str
    #: The one-line answers, most alarming first. This is what the UI strip
    #: renders and what the issue body leads with.
    headline_problems: list[str] = field(default_factory=list)
    #: A short headline per problem, in the same order. Separate from the
    #: summaries above because an issue TITLE has to fit on one line and a
    #: summary does not.
    problem_titles: list[str] = field(default_factory=list)


def _in_container() -> bool:
    return Path("/.dockerenv").exists() or bool(os.environ.get("LAZYAF_CONTROL"))


def _container_started_at() -> datetime | None:
    """When PID 1 started, i.e. when the container started.

    NOT `/proc/uptime`, which reports the HOST's uptime from inside a
    container - measured at 39.5 hours inside a container that had been up 34
    minutes. `/proc/1`'s ctime is the honest signal and was verified against
    `docker ps` on the running stack.
    """
    try:
        return _utc(os.stat("/proc/1").st_ctime)
    except OSError:
        return None


async def collect_runtime_facts(db=None, *, include_containers: bool = True) -> RuntimeFacts:
    """THE answer to "what is actually running here".

    Every other source in the bundle describes a symptom. This one describes
    the machine, and it is first in the document for that reason.
    """
    staleness = collect_staleness()
    git = collect_git()
    migrations = await collect_migrations(db) if db is not None else MigrationReport(
        error="no database session supplied"
    )
    containers = (
        await collect_containers() if include_containers else ContainerReport(
            available=False, reason="not requested"
        )
    )
    settings = collect_settings()

    records = log_buffer.records()
    first_at = records[0].when if records else None

    # Each problem is (short title, one-line summary). The title becomes the
    # issue's H1, so it has to be a headline rather than a paragraph - the
    # first version of this spliced the full explanation into the title and
    # produced a 200-character H1 that told you nothing at a glance.
    problems: list[str] = []
    titles: list[str] = []
    if staleness.stale:
        titles.append("Backend is running stale code")
        problems.append(
            f"RUNNING CODE IS NOT THE CODE ON DISK - {staleness.stale_file_count} "
            f"file(s) changed since this process started "
            f"{_human_duration(staleness.process_uptime_seconds)} ago."
        )
    for problem in migrations.problems:
        titles.append("Database schema does not match the migrations on disk")
        problems.append(f"MIGRATIONS: {problem}")
    if containers.spread_headline:
        titles.append("LazyAF containers were recreated at different times")
        problems.append(f"CONTAINER AGES: {containers.spread_headline}.")
    if not containers.available and include_containers:
        titles.append("Docker inventory unavailable")
        problems.append(f"CONTAINER INVENTORY UNAVAILABLE: {containers.reason}")

    return RuntimeFacts(
        collected_at=_now(),
        staleness=staleness,
        git=git,
        migrations=migrations,
        containers=containers,
        settings=settings,
        python_version=sys.version.split()[0],
        platform=platform.platform(),
        in_container=_in_container(),
        container_started_at=_container_started_at(),
        log_records_held=len(log_buffer),
        log_records_dropped=log_buffer.dropped,
        first_log_record_at=first_at,
        root_log_level=logging.getLevelName(logging.getLogger().getEffectiveLevel()),
        headline_problems=problems,
        problem_titles=titles,
    )


# =============================================================================
# Redaction: resolved from Lane A, fail-closed
# =============================================================================


class RedactorUnavailable(RuntimeError):
    """The shared redactor could not be loaded, so no bundle is produced.

    Fail-closed on purpose. The alternative - emit the bundle with a banner
    saying redaction was skipped - hands a frustrated person a file that looks
    exactly like the safe one, at the moment they are least likely to read a
    banner, for attachment to a PUBLIC issue. There is no safe degraded mode
    here, so there is no degraded mode.
    """


@dataclass
class ResolvedRedactor:
    """The redactor plus an honest account of how complete it is.

    ``notes`` is not decoration. The shared redactor has two passes: the
    process's KNOWN secret values (strictly stronger than any pattern - it
    matches the actual bytes) and credential SHAPES. If the value-harvesting
    half is unavailable, shapes still run, and the result is genuinely weaker
    in a way the reader cannot see by looking at it. So it is written into the
    bundle rather than swallowed.
    """

    redactor: Any
    notes: list[str] = field(default_factory=list)


def resolve_redactor(endpoint_refs: Sequence[str] = ()) -> ResolvedRedactor:
    """Build the shared redactor, or raise.

    ``app.services.redaction`` owns every credential pattern - the same rules
    `.github/scripts/secret_patterns.py` gates CI with (R3). This module
    imports them and never grows a regex of its own: a fourth copy of the
    pattern set is precisely the drift the shared module exists to end.

    Two passes matter and they are not equal. ``discover_known_secrets``
    harvests the literal values this process is holding - settings fields,
    ``LAZYAF_ENDPOINT_*`` variables, ``*_FILE`` indirection - and matching a
    secret by its actual bytes is strictly stronger than matching it by shape.
    ``endpoint_refs`` comes from the caller because the discovery module
    deliberately imports no models; this module has the session, so it does
    that query (see ``_endpoint_refs``).

    ONE INSTANCE PER BUNDLE, which the caller enforces by resolving once. The
    salt lives on the instance, so the same secret renders as the same
    placeholder in every member - that is what answers "is the token in the
    runner log the same one the backend minted" - and differently in the next
    bundle, so the digest cannot be used to correlate two published issues.
    """
    try:
        from app.services import redaction
    except Exception as exc:
        raise RedactorUnavailable(
            "app.services.redaction could not be imported "
            f"({type(exc).__name__}: {exc}). No bundle is produced: this module "
            "will not emit un-redacted logs for attachment to a public issue."
        ) from exc

    redactor_cls = getattr(redaction, "Redactor", None)
    if redactor_cls is None:
        raise RedactorUnavailable(
            "app.services.redaction exposes no Redactor class. "
            "Nothing is emitted un-redacted."
        )

    known: list[Any] = []
    notes: list[str] = []
    discover = getattr(redaction, "discover_known_secrets", None)
    if discover is None:
        try:
            from app.services.redaction.discovery import discover_known_secrets as discover
        except Exception:
            discover = None
    if callable(discover):
        try:
            known = list(discover(endpoint_refs=endpoint_refs))
        except Exception as exc:
            # Discovery is best-effort; shapes still run. But a weaker
            # redaction pass than the reader expects is a fact about the
            # artifact, so it travels with it rather than into a log nobody
            # reads (R1).
            notes.append(
                "The known-value redaction pass FAILED "
                f"({type(exc).__name__}: {exc}), so only credential SHAPES were "
                "matched. A secret this process holds whose format is not "
                "modelled by the pattern set would NOT have been removed."
            )
    else:
        notes.append(
            "The known-value redaction pass was unavailable, so only credential "
            "SHAPES were matched. A secret this process holds whose format is "
            "not modelled by the pattern set would NOT have been removed. Read "
            "the log sections with that in mind."
        )

    try:
        return ResolvedRedactor(redactor=redactor_cls(known=known), notes=notes)
    except Exception as exc:
        raise RedactorUnavailable(
            f"the shared Redactor could not be constructed ({type(exc).__name__}: {exc})."
        ) from exc


#: How long the LOG VIEW's redactor is reused. See `redactor_for_view`.
VIEW_REDACTOR_TTL_SECONDS = 300.0

_view_redactor: tuple[float, tuple[str, ...], ResolvedRedactor] | None = None


def redactor_for_view(endpoint_refs: Sequence[str] = ()) -> ResolvedRedactor:
    """A redactor for the LOG VIEW, reused across requests.

    The salt lives on the instance, so a fresh redactor per request would give
    the same secret a different placeholder every time the view polled -
    `[REDACTED:step-auth-secret:9c41a2]` becoming `...:d63acc` three seconds
    later. That flickers on screen and, worse, destroys the one thing the
    placeholder is for: telling the reader that two lines are talking about
    the SAME value. Rebuilding the known-value regex on every 3-second poll is
    also pure waste.

    So the view gets a stable instance for a few minutes, keyed on the
    endpoint refs so a newly added endpoint's secret starts being matched
    without a restart.

    BUNDLES DO NOT USE THIS. Each bundle resolves its own redactor and so gets
    its own salt - which is what stops a digest published in one issue from
    being matched against a digest published in another.
    """
    global _view_redactor
    key = tuple(sorted(endpoint_refs))
    now = time.monotonic()
    if _view_redactor is not None:
        created, cached_key, resolved = _view_redactor
        if cached_key == key and now - created < VIEW_REDACTOR_TTL_SECONDS:
            return resolved
    resolved = resolve_redactor(endpoint_refs=endpoint_refs)
    _view_redactor = (now, key, resolved)
    return resolved


def reset_view_redactor() -> None:
    """Drop the cached view redactor. Tests only."""
    global _view_redactor
    _view_redactor = None


async def _endpoint_refs(db) -> list[str]:
    """Every `ModelEndpoint.auth_secret_ref` on record.

    Feeds the redactor's known-value pass: an endpoint's key is resolved from
    the backend environment at dispatch, so the VALUE is reachable here even
    though the database only ever stores the reference (the M14 doctrine).
    Never fatal - a bundle without this pass is weaker, and says so, but a
    bundle that failed to build helps nobody.
    """
    if db is None:
        return []
    try:
        from sqlalchemy import select

        from app.models.model_endpoint import ModelEndpoint

        result = await db.execute(select(ModelEndpoint.auth_secret_ref))
        return [ref for ref in result.scalars().all() if ref]
    except Exception:
        return []


#: The placeholder shape the shared redactor emits: `[REDACTED:anthropic:9c41a2]`.
#: Used only by the fallback counting path in `redact_text`, for a redactor
#: that offers no receipt API.
REDACTION_PLACEHOLDER_RE = re.compile(r"\[REDACTED:(?P<label>[^:\]]+)(?::[0-9a-f]+)?\]")


def redact_text(redactor, text: str) -> tuple[str, dict[str, int], bool]:
    """Run text through the shared redactor; return (text, counts, failed).

    WHOLE MEMBERS, NEVER LINES. A secret split across a newline - a wrapped
    log line, a chunk boundary in the ring buffer - survives a line-wise
    redactor intact. Every caller passes a complete member.

    Prefers the receipt API so the count comes from the redactor's own
    substitution tally rather than from parsing its output. `failed` is
    propagated rather than smoothed over: the shared redactor fails CLOSED
    (it returns a withheld marker, not the input), and a member that came back
    withheld has to be visible as such or the bundle silently loses a section.
    """
    if text is None:
        return "", {}, False

    receipt_fn = getattr(redactor, "redact_with_receipt", None)
    if callable(receipt_fn):
        result = receipt_fn(text)
        return (
            result.text,
            dict(getattr(result, "counts", {}) or {}),
            bool(getattr(result, "failed", False)),
        )

    for method in ("redact", "scrub", "__call__"):
        fn = getattr(redactor, method, None)
        if callable(fn):
            out = fn(text)
            break
    else:
        raise RedactorUnavailable(
            f"{type(redactor).__name__} exposes no redact()/scrub() method."
        )
    if not isinstance(out, str):
        raise RedactorUnavailable(
            f"the shared redactor returned {type(out).__name__}, not text."
        )
    counts: dict[str, int] = {}
    for match in REDACTION_PLACEHOLDER_RE.finditer(out):
        label = match.group("label")
        counts[label] = counts.get(label, 0) + 1
    if not counts and out != text:
        # Something was removed in a shape this module does not recognise.
        # Report it as unknown rather than as zero: "0 values redacted" on a
        # document where redaction quietly misbehaved is the single most
        # dangerous sentence this feature could print.
        counts = {"unrecognised-placeholder": -1}
    return out, counts, False



# =============================================================================
# The bundle
# =============================================================================


class Risk:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class BundleMember:
    name: str
    title: str
    text: str
    risk: str = Risk.LOW
    note: str | None = None
    truncated: bool = False
    truncation_note: str | None = None
    redaction_counts: dict[str, int] = field(default_factory=dict)
    #: The shared redactor failed on this member and returned a withheld
    #: marker instead of content. Surfaced rather than smoothed over: a
    #: section that silently vanished is a section the reader will assume
    #: was empty.
    redaction_failed: bool = False

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def lines(self) -> int:
        return self.text.count("\n") + 1 if self.text else 0


@dataclass
class Bundle:
    id: str
    created_at: datetime
    expires_at: datetime
    title: str
    what_happened: str | None
    members: list[BundleMember]
    total_bytes: int
    redaction_counts: dict[str, int]
    values_redacted: int | None
    document: str
    #: Ways in which redaction was less complete than it should be. Empty is
    #: the good case; anything here is printed in the document itself.
    redaction_notes: list[str] = field(default_factory=list)


#: Per-member ceiling. Generous enough for a real step log, small enough that
#: one runaway member cannot crowd out the system facts.
DEFAULT_MEMBER_MAX_BYTES = 128 * 1024

#: Hard ceiling on what is handed to the redactor, before redaction.
#:
#: THIS IS A DENIAL-OF-SERVICE BOUND, and it is the one place the otherwise
#: absolute "redact before you truncate" rule is relaxed. Measured on the
#: shared redactor: a single line with no newlines costs ~0.07s at 10K
#: characters, 1.7s at 50K and 37s at 200K - quadratic. An agent step log that
#: dumps one long JSON blob is exactly that shape, so an un-capped member can
#: wedge the request for minutes.
#:
#: Why the relaxation is acceptable HERE and nowhere else: the bytes past the
#: cap are DISCARDED, never emitted, so the only exposure is a value that
#: straddles the cut. What survives is a prefix - and a prefix long enough to
#: still look like a credential is still matched by the shape patterns (which
#: carry length floors of 8-36 and run over the retained text), while a prefix
#: too short to match is also too short to use. The cut is taken at a line
#: boundary where one exists, which removes the straddle entirely for all
#: normal logs.
#:
#: One megabyte, so this only ever fires on pathological input.
MAX_RAW_MEMBER_BYTES = 1024 * 1024

#: Longest single LINE handed to the redactor, in characters.
#:
#: THE ACTUAL BOUND. Measured on the shared redactor, cost is driven by line
#: length rather than total size:
#:
#:     1 MiB, newline every 100 chars       0.29s
#:     1 MiB, newline every 4000 chars      3.30s
#:      64 KiB as ONE line                  4.55s
#:     256 KiB as ONE line                 67.67s
#:
#: So a byte cap alone is the wrong lever - it would let one 256 KiB line
#: through while needlessly cutting a perfectly cheap 1 MiB log. Clamping the
#: line instead leaves ordinary logs at full length and bounds the
#: pathological case to a few seconds.
#:
#: 4000 matches MAX_RECORD_CHARS, and is three orders of magnitude longer than
#: any credential - so a token is only ever cut if it straddles character 4000
#: of a single line, and its surviving prefix is still shape-matched.
MAX_REDACTION_LINE_CHARS = 4000

#: Whole-document ceiling. A GitHub issue body caps at 65,536 characters, so
#: anything past that is an attachment rather than a paste - but the artifact
#: still has to be readable, and a 10 MB markdown file is not.
DEFAULT_BUNDLE_MAX_BYTES = 512 * 1024


def _truncate(text: str, max_bytes: int) -> tuple[str, str | None]:
    """Clamp to `max_bytes`, keeping the HEAD and the TAIL, and say what went.

    Head AND tail because both ends are load-bearing and the middle rarely is:
    the head holds the startup banner that dates the process, the tail holds
    the failure that prompted the report. Dropping the tail to fit would
    remove the actual bug.

    NEVER a silent cut. The marker names how many lines and bytes went, so a
    reader can tell the difference between "the log ends here" and "the log
    was cut here" - which is the difference between a diagnosis and a wrong
    one.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, None

    lines = text.splitlines(keepends=True)
    head_budget = int(max_bytes * 0.6)
    tail_budget = max_bytes - head_budget

    head: list[str] = []
    used = 0
    for line in lines:
        size = len(line.encode("utf-8"))
        if used + size > head_budget:
            break
        head.append(line)
        used += size

    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head):]):
        size = len(line.encode("utf-8"))
        if used + size > tail_budget:
            break
        tail.append(line)
        used += size
    tail.reverse()

    dropped_lines = len(lines) - len(head) - len(tail)
    dropped_bytes = len(encoded) - sum(
        len(line.encode("utf-8")) for line in head + tail
    )
    marker = (
        f"\n... [TRUNCATED: {dropped_lines:,} lines and {dropped_bytes:,} bytes "
        f"removed from the MIDDLE to fit the {max_bytes:,}-byte cap. "
        f"The beginning and the end are intact.] ...\n\n"
    )
    note = (
        f"{dropped_lines:,} lines ({dropped_bytes:,} bytes) removed from the middle"
    )
    return "".join(head) + marker + "".join(tail), note


BUNDLE_README = """\
# LazyAF bug report bundle

Generated by LazyAF's diagnostics collector. **Read this before attaching it
to an issue.**

## What automatic redaction does

Every section below was passed through LazyAF's shared secret redactor - the
same rule set that gates the release CI - which replaces known credential
shapes (`sk-ant-`, `ghp_`, `AKIA`, `AIza`, bearer tokens, JWTs, private key
blocks, URLs with inline `user:password@`) and the specific secret values this
process is holding, with `[REDACTED:...]` placeholders.

## What it does NOT do, and this is the important half

Pattern matching catches known SHAPES. It cannot catch:

* **your source code**, if an agent step log quoted a private repository;
* **your prompts**, which routinely carry decisions that are not public;
* a connection string, hostname, or credential in an unusual format;
* **a customer's or employer's name** in a card title, a branch, or a commit
  message;
* a filesystem path containing your username.

A denylist is necessary and insufficient. The real safeguard is that **you
downloaded this and can read it before anyone else sees it.** There is
deliberately no upload button: the review is the defence, and a one-click
upload would remove it.

If the repository you are filing against is public, everything you paste is
permanent and indexed. Read the sections marked **HIGH RISK** in full.

## What was deliberately left out

Container environment variables (they hold this deployment's auth secrets),
container command lines and mount sources, request and response bodies, git
diffs, workspace file contents, `.env`, settings values that are not on a
reviewed allowlist, and the names of containers on this host that do not
belong to LazyAF.
"""


def _render_runtime_section(facts: RuntimeFacts) -> str:
    """The system facts, as the markdown that leads the document.

    First, and as a table, because this is the section that answers the
    expensive question. Someone skimming an issue should reach "the running
    code is 25 hours old" before they reach any log line.
    """
    lines: list[str] = []
    st = facts.staleness

    if facts.headline_problems:
        lines.append("> [!WARNING]")
        for problem in facts.headline_problems:
            lines.append(f"> - {problem}")
        lines.append("")
    else:
        lines.append("No staleness or migration problems detected.\n")

    lines.append("| | |")
    lines.append("|---|---|")
    lines.append(
        f"| backend process started | {st.process_started_at:%Y-%m-%d %H:%M:%S UTC} "
        f"({_human_duration(st.process_uptime_seconds)} ago) |"
    )
    if facts.container_started_at:
        lines.append(
            f"| container (PID 1) started | {facts.container_started_at:%Y-%m-%d %H:%M:%S UTC} |"
        )
    if st.stale:
        lines.append(
            f"| **code on disk** | **{st.stale_file_count} file(s) newer than this "
            f"process** :warning: |"
        )
    else:
        lines.append(
            f"| code on disk | in sync ({st.checked_files} files checked, "
            f"none newer than the process) |"
        )
    if st.newest_file:
        lines.append(
            f"| newest source file | `{st.newest_file}` "
            f"({st.newest_file_mtime:%Y-%m-%d %H:%M:%S UTC}) |"
        )

    mig = facts.migrations
    if mig.error:
        lines.append(f"| migrations | unavailable: {mig.error} |")
    elif mig.problems:
        lines.append(
            f"| **migrations** | **db {', '.join(mig.db_revisions) or 'unstamped'} / "
            f"disk head {', '.join(mig.disk_heads)}** :warning: |"
        )
    else:
        lines.append(
            f"| migrations | db {', '.join(mig.db_revisions) or 'unstamped'} = "
            f"disk head {', '.join(mig.disk_heads)} "
            f"({mig.disk_revision_count} revisions on disk) |"
        )

    git = facts.git
    if git.available:
        dirty = ""
        if git.dirty_file_count is not None:
            dirty = (
                f", {git.dirty_file_count} uncommitted file(s)"
                if git.dirty
                else ", clean"
            )
        lines.append(f"| checkout | `{git.commit}` on `{git.branch}`{dirty} |")
    else:
        lines.append(f"| checkout | unknown - {git.reason} |")

    lines.append(f"| python | {facts.python_version} on {facts.platform} |")
    lines.append(
        f"| in container | {'yes' if facts.in_container else 'no'} |"
    )
    held = f"{facts.log_records_held:,}"
    if facts.log_records_dropped:
        held += f" (+{facts.log_records_dropped:,} evicted from the ring buffer)"
    lines.append(f"| backend log records held | {held} |")
    lines.append(f"| root log level | {facts.root_log_level} |")
    if facts.first_log_record_at:
        lines.append(
            f"| oldest log record | {facts.first_log_record_at:%Y-%m-%d %H:%M:%S UTC} |"
        )
    lines.append("")

    if st.stale:
        lines.append("### Files newer than the running process\n")
        for name in st.stale_files:
            lines.append(f"- `{name}`")
        if st.stale_file_count > len(st.stale_files):
            lines.append(
                f"- ... and {st.stale_file_count - len(st.stale_files):,} more "
                f"(listing capped at {MAX_STALE_FILES_LISTED})"
            )
        lines.append(f"\n**Remedy:** `{st.remedy}`\n")

    if mig.unapplied:
        lines.append("### Migrations on disk that this database has not applied\n")
        for revision in mig.unapplied:
            lines.append(f"- `{revision}`")
        lines.append("")

    lines.append("### What this probe cannot see\n")
    for limitation in st.limitations:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)


def _render_containers_section(report: ContainerReport) -> str:
    if not report.available:
        return (
            f"Container inventory unavailable: {report.reason}\n\n"
            "This section is empty because the daemon could not be reached, "
            "not because nothing is running.\n"
        )
    if not report.containers:
        lines = ["No LazyAF containers are running.\n"]
    else:
        lines = [
            f"{len(report.containers)} running, oldest first.\n",
            "| container | service | up | state | image | image id |",
            "|---|---|---|---|---|---|",
        ]
        for c in report.containers:
            flag = " :warning: oldest" if c.name == report.oldest_lazyaf else ""
            health = f"{c.state or '?'}"
            if c.health:
                health += f" / {c.health}"
            if c.restart_count:
                health += f" / {c.restart_count} restarts"
            lines.append(
                f"| `{c.name}`{flag} | {c.compose_service or '-'} | "
                f"{_human_duration(c.uptime_seconds)} | {health} | "
                f"`{c.image or '-'}` | `{c.image_id or '-'}` |"
            )
        lines.append("")
    if report.spread_warning:
        lines.append(f"> [!WARNING]\n> {report.spread_warning}\n")
    if report.exited_count:
        # Grouped, not listed. A dev host accumulates hundreds of finished step
        # containers with random docker-assigned names; enumerating them would
        # bury the rows that answer the question under kilobytes of noise.
        lines.append(
            f"_{report.exited_count} stopped LazyAF container(s), by image:_\n"
        )
        for image, count in report.exited_summary.items():
            lines.append(f"- `{image}` x{count}")
        lines.append("")
    if report.other_containers_on_host:
        lines.append(
            f"_{report.other_containers_on_host} other container(s) are running on "
            "this host. They are not part of LazyAF, so their names, images and "
            "configuration are deliberately not collected._\n"
        )
    lines.append(
        "_Environment variables, command lines and mount sources are never "
        "collected: this service's own environment holds "
        "`LAZYAF_STEP_AUTH_SECRET`, `LAZYAF_RUNNER_AUTH_SECRET` and "
        "`ANTHROPIC_API_KEY`._\n"
    )
    return "\n".join(lines)


def _render_settings_section(report: SettingsReport) -> str:
    if report.error:
        return f"Settings unavailable: {report.error}\n"
    lines = [
        "Names and whether they are set. Values appear only for settings on a "
        "reviewed allowlist that cannot carry a credential.\n",
        "| setting | set | value |",
        "|---|---|---|",
    ]
    for entry in report.settings:
        if entry.value is not None:
            value = f"`{entry.value}`"
            if entry.note:
                value += f" _({entry.note})_"
        else:
            value = f"_{entry.note}_" if entry.note else "_withheld_"
        lines.append(
            f"| `{entry.name}` | {'yes' if entry.present else 'no'} | {value} |"
        )
    lines.append("")
    if report.env_names:
        lines.append("Environment variables set (names only, never values):\n")
        for name in report.env_names:
            lines.append(f"- `{name}`")
        lines.append("")
    lines.append(
        f"_{report.endpoint_secret_count} `LAZYAF_ENDPOINT_*` variable(s) are set. "
        "Their names are counted rather than listed: the suffix is chosen by "
        "the operator and frequently names a customer or a product._\n"
    )
    return "\n".join(lines)


def _render_log_section(records: Sequence[LogRecord], *, dropped: int) -> str:
    if not records:
        return (
            "No log records captured. The ring buffer attaches when the "
            "diagnostics router is imported, so records emitted before that "
            "point went only to stdout (`docker logs`).\n"
        )
    lines = []
    if dropped:
        lines.append(
            f"_The ring buffer holds the most recent {len(records):,} records; "
            f"{dropped:,} older record(s) were evicted before this bundle was "
            "built._\n"
        )
    lines.append("```")
    for record in records:
        lines.append(
            f"{record.when:%Y-%m-%d %H:%M:%S} {record.level:<8} "
            f"{record.logger_name} {record.message}"
        )
        if record.exception:
            lines.append(record.exception)
    lines.append("```")
    return "\n".join(lines)


def _render_errors_section(records: Sequence[LogRecord]) -> str:
    """Errors deduped by (logger, first line), with counts and a time span.

    The entry point to a diagnosis, not the diagnosis: forty repeats of one
    error are one problem, and the raw log makes that look like forty.
    """
    errors = [r for r in records if r.level_no >= logging.ERROR]
    if not errors:
        return "No ERROR or CRITICAL records in the buffer.\n"
    groups: dict[tuple[str, str], list[LogRecord]] = {}
    for record in errors:
        key = (record.logger_name, record.message.splitlines()[0][:200])
        groups.setdefault(key, []).append(record)
    ordered = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)
    lines = [f"{len(errors)} error record(s) in {len(groups)} distinct group(s).\n"]
    for (logger_name, message), group in ordered:
        first, last = group[0].when, group[-1].when
        span = (
            f"{first:%H:%M:%S}"
            if first == last
            else f"{first:%H:%M:%S}-{last:%H:%M:%S}"
        )
        lines.append(f"- **x{len(group)}** `{logger_name}` ({span}) - {message}")
    lines.append("")
    latest = ordered[0][1][-1]
    if latest.exception:
        lines.append("Most recent traceback:\n")
        lines.append("```")
        lines.append(latest.exception)
        lines.append("```")
    return "\n".join(lines)


# =============================================================================
# Bundle assembly
# =============================================================================

#: The sources a caller may request. An ALLOWLIST: a log surface added in a
#: future phase is absent from bundles until someone adds it here deliberately,
#: with a test. Bundles must never widen on their own.
SOURCE_SYSTEM = "system"
SOURCE_CONTAINERS = "containers"
SOURCE_SETTINGS = "settings"
SOURCE_BACKEND_LOG = "backend_log"
SOURCE_ERRORS = "errors"
SOURCE_RUN = "run"
SOURCE_JOB = "job"

ALL_SOURCES = (
    SOURCE_SYSTEM,
    SOURCE_CONTAINERS,
    SOURCE_SETTINGS,
    SOURCE_BACKEND_LOG,
    SOURCE_ERRORS,
    SOURCE_RUN,
    SOURCE_JOB,
)

#: What a bundle contains when the caller says nothing. `run` and `job` are
#: OPT-IN: they carry agent step logs, which are the highest-risk content in
#: the product and the one thing redaction genuinely cannot clean.
DEFAULT_SOURCES = (
    SOURCE_SYSTEM,
    SOURCE_CONTAINERS,
    SOURCE_SETTINGS,
    SOURCE_BACKEND_LOG,
    SOURCE_ERRORS,
)

STEP_LOG_RISK_NOTE = (
    "HIGH RISK. Agent step logs contain your prompts, the model's output, tool "
    "calls, and diffs of your source. Automatic redaction removes credential "
    "shapes and nothing else. Read this section in full before publishing it."
)


async def _run_members(db, pipeline_run_id: str) -> list[BundleMember]:
    from sqlalchemy import select

    from app.models.pipeline import PipelineRun, StepRun

    result = await db.execute(select(PipelineRun).where(PipelineRun.id == pipeline_run_id))
    run = result.scalar_one_or_none()
    if run is None:
        return [
            BundleMember(
                name="run.md",
                title=f"Pipeline run {pipeline_run_id}",
                text=f"No pipeline run with id `{pipeline_run_id}` exists.\n",
            )
        ]

    result = await db.execute(
        select(StepRun)
        .where(StepRun.pipeline_run_id == pipeline_run_id)
        .order_by(StepRun.step_index)
    )
    steps = list(result.scalars().all())

    lines = [
        f"- run id: `{run.id}`",
        f"- pipeline id: `{run.pipeline_id}`",
        f"- status: **{run.status}**",
        f"- trigger: {run.trigger_type}",
        f"- steps: {run.steps_completed}/{run.steps_total} completed",
        f"- started: {run.started_at}",
        f"- completed: {run.completed_at}",
        "",
        "| # | step | status | executor | error |",
        "|---|---|---|---|---|",
    ]
    for step in steps:
        error = (step.error or "").splitlines()[0][:120] if step.error else "-"
        lines.append(
            f"| {step.step_index} | {step.step_name} | {step.status} | "
            f"{step.executor or '-'} | {error} |"
        )
    lines.append("")

    members = [
        BundleMember(
            name="run.md",
            title=f"Pipeline run {run.id[:8]} ({run.status})",
            text="\n".join(lines),
            risk=Risk.MEDIUM,
            note="Step names, statuses and the first line of each error.",
        )
    ]

    for step in steps:
        if not (step.logs or step.error):
            continue
        body = step.logs or ""
        if step.error:
            body += f"\n\n--- step error ---\n{step.error}\n"
        members.append(
            BundleMember(
                name=f"run-step-{step.step_index}.log",
                title=f"Step {step.step_index} '{step.step_name}' ({step.status}) output",
                text=body,
                risk=Risk.HIGH,
                note=STEP_LOG_RISK_NOTE,
            )
        )
    return members


async def _job_members(db, job_id: str) -> list[BundleMember]:
    from sqlalchemy import select

    from app.models.job import Job
    from app.models.pipeline import StepRun

    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        return [
            BundleMember(
                name="job.md",
                title=f"Job {job_id}",
                text=f"No job with id `{job_id}` exists.\n",
            )
        ]

    logs = job.logs or ""
    if not logs and job.step_run_id:
        # Same fallback `GET /api/jobs/{id}/logs` uses: a running card job's
        # output lives on the StepRun until the run completes.
        result = await db.execute(
            select(StepRun.logs).where(StepRun.id == job.step_run_id)
        )
        logs = result.scalar_one_or_none() or ""
    if job.error:
        logs += f"\n\n--- job error ---\n{job.error}\n"

    return [
        BundleMember(
            name="job.md",
            title=f"Job {job.id[:8]} ({job.status})",
            text=(
                f"- job id: `{job.id}`\n"
                f"- card id: `{job.card_id}`\n"
                f"- status: **{job.status}**\n"
                f"- step type: {job.step_type}\n"
                f"- runner: {job.runner_type or '-'}\n"
                f"- started: {job.started_at}\n"
                f"- completed: {job.completed_at}\n"
            ),
            risk=Risk.LOW,
        ),
        BundleMember(
            name="job.log",
            title=f"Job {job.id[:8]} output",
            text=logs or "(no output recorded)\n",
            risk=Risk.HIGH,
            note=STEP_LOG_RISK_NOTE,
        ),
    ]


async def build_bundle(
    db,
    *,
    title: str | None = None,
    what_happened: str | None = None,
    sources: Sequence[str] | None = None,
    pipeline_run_id: str | None = None,
    job_id: str | None = None,
    log_limit: int = 500,
    member_max_bytes: int = DEFAULT_MEMBER_MAX_BYTES,
    bundle_max_bytes: int = DEFAULT_BUNDLE_MAX_BYTES,
    redactor: Any = None,
) -> Bundle:
    """Collect, redact, cap, and assemble.

    ORDER OF OPERATIONS, and it is a security property rather than a
    preference: **redact, then truncate.** Truncating first can cut a token in
    half and leave a prefix that no longer matches the pattern that would have
    caught it. Doing it in this order costs a redaction pass over bytes that
    are about to be discarded, and buys the guarantee that nothing reaches the
    truncator un-inspected.

    ``redactor`` is injectable so tests can prove that every member goes
    through it. When it is None the shared redactor is resolved, and if that
    fails ``RedactorUnavailable`` propagates - the caller answers 503 rather
    than emitting anything.
    """
    requested = list(DEFAULT_SOURCES if sources is None else sources)
    unknown = [s for s in requested if s not in ALL_SOURCES]
    if unknown:
        raise ValueError(
            f"unknown diagnostic source(s): {', '.join(unknown)}. "
            f"Known sources: {', '.join(ALL_SOURCES)}."
        )
    redaction_notes: list[str] = []
    if redactor is None:
        redactor = resolve_redactor(endpoint_refs=await _endpoint_refs(db))
    if isinstance(redactor, ResolvedRedactor):
        redaction_notes = list(redactor.notes)
        redactor = redactor.redactor

    members: list[BundleMember] = []
    members.append(
        BundleMember(
            name="README.md",
            title="Read this before attaching the bundle",
            text=BUNDLE_README,
            risk=Risk.LOW,
        )
    )

    facts: RuntimeFacts | None = None
    if SOURCE_SYSTEM in requested:
        facts = await collect_runtime_facts(
            db, include_containers=SOURCE_CONTAINERS in requested
        )
        members.append(
            BundleMember(
                name="system.md",
                title="What is actually running",
                text=_render_runtime_section(facts),
                risk=Risk.LOW,
                note=(
                    "Process uptime, code-vs-disk staleness, migration state and "
                    "the checkout. File paths only, never file contents."
                ),
            )
        )

    if SOURCE_CONTAINERS in requested:
        report = facts.containers if facts else await collect_containers()
        members.append(
            BundleMember(
                name="containers.md",
                title="Containers",
                text=_render_containers_section(report),
                risk=Risk.LOW,
                note="No environment variables, command lines or mount sources.",
            )
        )

    if SOURCE_SETTINGS in requested:
        report = facts.settings if facts else collect_settings()
        members.append(
            BundleMember(
                name="settings.md",
                title="Settings",
                text=_render_settings_section(report),
                risk=Risk.LOW,
                note="Names and presence. Values only from a reviewed allowlist.",
            )
        )

    records = log_buffer.records(limit=log_limit)
    if SOURCE_ERRORS in requested:
        members.append(
            BundleMember(
                name="errors.md",
                title="Recent errors, deduplicated",
                text=_render_errors_section(log_buffer.records()),
                risk=Risk.MEDIUM,
                note="Log messages can carry repo names, branch names and paths.",
            )
        )
    if SOURCE_BACKEND_LOG in requested:
        members.append(
            BundleMember(
                name="backend.log",
                title=f"Backend log (last {len(records):,} records)",
                text=_render_log_section(records, dropped=log_buffer.dropped),
                risk=Risk.MEDIUM,
                note="Log messages can carry repo names, branch names and paths.",
            )
        )

    if SOURCE_RUN in requested:
        if not pipeline_run_id:
            raise ValueError("source 'run' requires pipeline_run_id")
        members.extend(await _run_members(db, pipeline_run_id))
    if SOURCE_JOB in requested:
        if not job_id:
            raise ValueError("source 'job' requires job_id")
        members.extend(await _job_members(db, job_id))

    # --- redact, THEN truncate -------------------------------------------
    def _clamp_raw(member: BundleMember) -> str:
        """Bound the redactor's input. See MAX_REDACTION_LINE_CHARS.

        Clamps LONG LINES, then total bytes. Measured: the shared redactor's
        cost is driven by line length, not by total size - 1 MiB of ordinary
        100-character lines redacts in 0.29s, while a single 256 KiB line
        takes 68s. So the lever that actually bounds the work is the line
        clamp; the byte cap is a second, coarser backstop.
        """
        notes: list[str] = []
        text = member.text

        long_lines = 0
        dropped_chars = 0
        if any(len(line) > MAX_REDACTION_LINE_CHARS for line in text.splitlines()):
            out: list[str] = []
            for line in text.splitlines(keepends=True):
                stripped = line.rstrip("\r\n")
                if len(stripped) > MAX_REDACTION_LINE_CHARS:
                    long_lines += 1
                    dropped_chars += len(stripped) - MAX_REDACTION_LINE_CHARS
                    ending = line[len(stripped):]
                    out.append(
                        stripped[:MAX_REDACTION_LINE_CHARS]
                        + f" ... [LINE TRUNCATED: {len(stripped) - MAX_REDACTION_LINE_CHARS:,}"
                        " more characters, dropped before redaction]"
                        + (ending or "\n")
                    )
                else:
                    out.append(line)
            text = "".join(out)
            notes.append(
                f"{long_lines:,} over-long line(s) clamped to "
                f"{MAX_REDACTION_LINE_CHARS:,} characters before redaction "
                f"({dropped_chars:,} characters dropped)"
            )

        encoded = text.encode("utf-8")
        if len(encoded) > MAX_RAW_MEMBER_BYTES:
            kept = encoded[:MAX_RAW_MEMBER_BYTES].decode("utf-8", errors="ignore")
            newline = kept.rfind("\n")
            if newline > MAX_RAW_MEMBER_BYTES // 2:
                kept = kept[: newline + 1]
            dropped = len(encoded) - len(kept.encode("utf-8"))
            text = kept + (
                f"\n... [TRUNCATED: {dropped:,} bytes removed from the END "
                "before redaction, because this member exceeded the processing "
                "limit. The dropped bytes were discarded, not emitted.] ...\n"
            )
            notes.append(
                f"{dropped:,} bytes dropped BEFORE redaction: this member "
                f"exceeded the {MAX_RAW_MEMBER_BYTES:,}-byte processing limit"
            )

        if notes:
            member.truncation_note = "; ".join(notes)
            member.truncated = True
        return text

    def _redact_all() -> list[tuple[str, dict[str, int], bool]]:
        # Off the event loop: redaction is CPU-bound and, on a large member,
        # slow enough to stall every other request in the process.
        return [redact_text(redactor, _clamp_raw(m)) for m in members]

    results = await asyncio.to_thread(_redact_all)

    totals: dict[str, int] = {}
    for member, (redacted, counts, failed) in zip(members, results):
        member.redaction_counts = counts
        member.redaction_failed = failed
        if failed:
            redaction_notes.append(
                f"Redaction FAILED on `{member.name}`; its content was withheld "
                "rather than emitted un-redacted."
            )
        for label, count in counts.items():
            totals[label] = totals.get(label, 0) + count
        # A member can be cut TWICE - once before redaction by the processing
        # cap, once after by the display cap. Both notes survive; a merged
        # note that reported only the second would understate what is missing.
        pre_note = member.truncation_note
        member.text, post_note = _truncate(redacted, member_max_bytes)
        notes = [n for n in (pre_note, post_note) if n]
        member.truncation_note = "; ".join(notes) if notes else None
        member.truncated = bool(notes)

    bundle_id = str(uuid.uuid4())
    created = _now()

    # The USER'S OWN WORDS go through the redactor too, and this is not
    # belt-and-braces - it is the likeliest leak in the whole bundle. The
    # description field invites pasting the thing that failed, which is
    # routinely a 401 body or a token-bearing command line. Redacting only
    # the collected members left these two strings spliced into the H1 and
    # the "What happened" section AFTER redaction, so a bundle could publish
    # a live key in its own title while reporting values_redacted: 1 and
    # certifying itself clean. The preview renders members, so a human
    # reviewing before upload could not see this page either.
    def _redact_user_text() -> tuple[str | None, str | None, list[dict[str, int]]]:
        out: list[str | None] = []
        tallies: list[dict[str, int]] = []
        for value in (title or _default_title(facts), what_happened):
            if not value:
                out.append(value)
                continue
            redacted, counts, _failed = redact_text(redactor, value)
            out.append(redacted)
            tallies.append(counts)
        return out[0], out[1], tallies

    safe_title, safe_what_happened, user_text_tallies = await asyncio.to_thread(
        _redact_user_text
    )
    for counts in user_text_tallies:
        for label, n in counts.items():
            totals[label] = totals.get(label, 0) + n

    document = _render_document(
        title=safe_title,
        what_happened=safe_what_happened,
        members=members,
        created=created,
        bundle_id=bundle_id,
        totals=totals,
        redaction_notes=redaction_notes,
    )
    document, doc_note = _truncate(document, bundle_max_bytes)
    if doc_note:
        document += (
            f"\n\n> [!NOTE]\n> The assembled document exceeded the "
            f"{bundle_max_bytes:,}-byte cap: {doc_note}.\n"
        )

    unknown_count = any(v < 0 for v in totals.values())
    values_redacted = None if unknown_count else sum(totals.values())

    return Bundle(
        id=bundle_id,
        created_at=created,
        expires_at=created + BUNDLE_TTL,
        title=title or _default_title(facts),
        what_happened=what_happened,
        members=members,
        total_bytes=sum(m.bytes for m in members),
        redaction_counts=totals,
        values_redacted=values_redacted,
        document=document,
        redaction_notes=redaction_notes,
    )


def _default_title(facts: RuntimeFacts | None) -> str:
    """A title that names the most alarming fact, so an issue does not open
    with "bug report".

    Uses the SHORT headline, not the summary sentence. Splicing the full
    explanation in here produced a 200-character H1 that had to be read to the
    end before it said anything - the opposite of what a title is for.
    """
    if facts and facts.problem_titles:
        return facts.problem_titles[0]
    return "LazyAF bug report"


def _render_document(
    *,
    title: str,
    what_happened: str | None,
    members: Sequence[BundleMember],
    created: datetime,
    bundle_id: str,
    totals: dict[str, int],
    redaction_notes: Sequence[str] = (),
) -> str:
    """One markdown document, ordered so the expensive answer comes first.

    Markdown rather than a zip because the owner's stated use is pasting it
    into a GitHub issue. Long sections are wrapped in `<details>` so the
    document collapses to something readable while keeping everything present.
    """
    lines = [f"# {title}", ""]
    if what_happened:
        lines += ["## What happened", "", what_happened.strip(), ""]

    unknown = any(v < 0 for v in totals.values())
    if unknown:
        redaction_line = (
            "redaction ran, but the placeholder format was not recognised so the "
            "count is unavailable"
        )
    else:
        total = sum(totals.values())
        by_label = ", ".join(f"{label} x{count}" for label, count in sorted(totals.items()))
        redaction_line = f"{total} value(s) redacted" + (f" ({by_label})" if by_label else "")

    lines += [
        "## Bundle",
        "",
        f"- generated: {created:%Y-%m-%d %H:%M:%S UTC}",
        f"- bundle id: `{bundle_id}`",
        f"- redaction: {redaction_line}",
        f"- sections: {len(members)}",
        "",
        "| section | size | risk | notes |",
        "|---|---|---|---|",
    ]
    for member in members:
        note = member.note or ""
        if member.truncated:
            note = (note + " " if note else "") + f"**TRUNCATED**: {member.truncation_note}."
        lines.append(
            f"| `{member.name}` | {member.bytes:,} B | {member.risk} | {note} |"
        )
    lines.append("")

    if redaction_notes:
        # Loud, and above the content rather than in a footnote: these are the
        # ways this bundle is less safe than it looks.
        lines.append("> [!WARNING]")
        lines.append("> **Redaction was not complete:**")
        for note in redaction_notes:
            lines.append(f"> - {note}")
        lines.append("")

    lines += [
        "> [!IMPORTANT]",
        "> Automatic redaction removes known credential shapes. It cannot remove "
        "source code, prompts, or customer names. Read before publishing - the "
        "full note is at the end of this document.",
        "",
    ]

    # The README renders LAST. It is reference material, and leading a bug
    # report with three hundred words of caveats buries the diagnosis the
    # reader opened the file for. The short warning above carries the point;
    # the member itself still travels with the artifact.
    ordered = [m for m in members if m.name != "README.md"]
    ordered += [m for m in members if m.name == "README.md"]

    for member in ordered:
        lines.append(f"## {member.title}")
        lines.append("")
        if member.risk == Risk.HIGH and member.note:
            lines.append(f"> [!CAUTION]\n> {member.note}\n")
        long_member = member.text.count("\n") > 40
        if long_member:
            lines.append(f"<details><summary>{member.name} (expand)</summary>\n")
        lines.append(member.text)
        if long_member:
            lines.append("\n</details>")
        lines.append("")

    return "\n".join(lines)


# =============================================================================
# The bundle store
# =============================================================================
#
# In memory, unguessable id, short TTL, and at most a few retained.
#
# THE EXPOSURE, stated rather than discovered later: `GET
# /api/diagnostics/bundle/{id}` is unauthenticated, like every other
# human-facing router here, and both compose files publish 0.0.0.0. This does
# not CREATE the exposure - `GET /api/model-endpoints`, `GET
# /api/jobs/{id}/logs` and the git server are already open - but it
# CONCENTRATES it into one download. uuid4 + a 15 minute TTL + never touching
# disk are mitigations, not a fix, and the fix is authentication on the API.
# Nothing here is written under `lazyaf-data`: a bundle sitting on a volume is
# a copy of the logs with none of the review.

BUNDLE_TTL = timedelta(minutes=15)
MAX_RETAINED_BUNDLES = 3


class BundleStore:
    def __init__(self, ttl: timedelta = BUNDLE_TTL, max_retained: int = MAX_RETAINED_BUNDLES):
        self.ttl = ttl
        self.max_retained = max_retained
        self._bundles: dict[str, Bundle] = {}

    def put(self, bundle: Bundle) -> Bundle:
        self._evict()
        self._bundles[bundle.id] = bundle
        while len(self._bundles) > self.max_retained:
            oldest = min(self._bundles.values(), key=lambda b: b.created_at)
            self._bundles.pop(oldest.id, None)
        return bundle

    def get(self, bundle_id: str) -> Bundle | None:
        self._evict()
        return self._bundles.get(bundle_id)

    def _evict(self) -> None:
        now = _now()
        for bundle_id, bundle in list(self._bundles.items()):
            if bundle.expires_at <= now:
                del self._bundles[bundle_id]

    def clear(self) -> None:
        self._bundles.clear()

    def __len__(self) -> int:
        self._evict()
        return len(self._bundles)


bundle_store = BundleStore()
