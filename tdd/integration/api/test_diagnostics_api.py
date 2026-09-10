"""Diagnostics API: the system strip, the log view, and the bug-report bundle.

WHAT THESE TESTS ARE DEFENDING
==============================
The bundle this API produces is built to be attached to an issue on a PUBLIC
repository. So the tests are weighted toward containment rather than
formatting: the ones that matter assert that something specific is *absent*
from the output.

Three of them earn their place by encoding a decision that a future change
could plausibly undo without noticing:

* ``test_container_projection_drops_env_and_command`` - ``docker inspect``
  returns ``Config.Env``, and on this very service that holds
  ``LAZYAF_STEP_AUTH_SECRET`` and ``ANTHROPIC_API_KEY``. The projection is an
  allowlist; this test fails the moment somebody makes it a denylist.
* ``test_foreign_containers_are_counted_never_named`` - the host runs
  containers belonging to unrelated private projects. Publishing their names
  in a public issue is a disclosure with no diagnostic value.
* ``test_bundle_refuses_when_redactor_unavailable`` - the collector is
  fail-closed. There is no degraded mode that emits un-redacted logs with a
  warning banner, because a warning banner is advice and this artifact gets
  pasted somewhere permanent.

WHAT THESE TESTS DO NOT COVER, DELIBERATELY
===========================================
Whether the redactor's patterns are correct belongs to the redaction module's
own suite. What is asserted here is the COMPOSITION: that every member goes
through the redactor, that nothing bypasses it, and that the order is
redact-then-truncate. ``test_every_member_is_routed_through_the_redactor``
uses a spy for exactly that reason - it proves the wiring without restating
somebody else's regexes.

T1 (no Docker). The container inventory therefore reports itself as
unavailable, and ``test_container_inventory_unavailable_is_stated`` asserts it
says so rather than rendering an empty list, which is the R4 case: an empty
section with no explanation reads as "nothing is running".
"""
import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.services import diagnostics


# =============================================================================
# Fixtures and helpers
# =============================================================================


@pytest.fixture(autouse=True)
def _clean_diagnostics_state():
    """The log buffer and bundle store are process-wide singletons."""
    diagnostics.log_buffer.clear()
    diagnostics.bundle_store.clear()
    diagnostics.reset_view_redactor()
    yield
    diagnostics.log_buffer.clear()
    diagnostics.bundle_store.clear()
    diagnostics.reset_view_redactor()


class SpyRedactor:
    """Records every string it was asked to redact and marks its output.

    The marker is what lets a test assert that a member reached the redactor
    at all. Asserting "the secret is gone" cannot distinguish redaction from a
    member that was silently dropped - this can.
    """

    def __init__(self, replace: dict[str, str] | None = None):
        self.seen: list[str] = []
        self.replace = replace or {}

    def redact_with_receipt(self, text):
        self.seen.append(text)
        out = text
        counts: dict[str, int] = {}
        for needle, label in self.replace.items():
            if needle in out:
                counts[label] = counts.get(label, 0) + out.count(needle)
                out = out.replace(needle, f"[REDACTED:{label}:aaaaaa]")
        return diagnostics_result(out + "\n<<SPY>>", counts)


def diagnostics_result(text, counts):
    from app.services.redaction import RedactionResult

    return RedactionResult(text=text, counts=counts)


async def _seed_run(db, *, step_logs: str, step_name: str = "agent-step"):
    """A repo -> pipeline -> run -> step chain, so `source=run` has something."""
    from app.models.pipeline import Pipeline, PipelineRun, StepRun
    from app.models.repo import Repo

    repo = Repo(name="diag-repo")
    db.add(repo)
    await db.flush()

    pipeline = Pipeline(repo_id=repo.id, name="diag-pipeline", steps="[]")
    db.add(pipeline)
    await db.flush()

    run = PipelineRun(pipeline_id=pipeline.id, status="failed", steps_total=1)
    db.add(run)
    await db.flush()

    step = StepRun(
        pipeline_run_id=run.id,
        step_index=0,
        step_name=step_name,
        status="failed",
        logs=step_logs,
        error="step exploded",
    )
    db.add(step)
    await db.commit()
    return run, step


# =============================================================================
# Source A: the staleness probe - the field this whole feature exists for
# =============================================================================


def test_staleness_detects_a_file_newer_than_the_process(tmp_path):
    """THE test. A file whose mtime is after process start is code on disk
    that the running process did not import - the 25-hour bug, detected."""
    import os

    root = tmp_path / "app"
    root.mkdir()
    module = root / "routers" / "cards.py"
    module.parent.mkdir(parents=True)
    module.write_text("# changed after the process started\n")

    process_start = datetime.now(tz=timezone.utc).timestamp() - 3600
    os.utime(module, (process_start + 1800, process_start + 1800))

    report = diagnostics.collect_staleness(
        roots=[root], process_started_at=process_start
    )

    assert report.stale is True
    assert report.stale_file_count == 1
    assert any("cards.py" in name for name in report.stale_files)
    # The remedy has to name the command that actually works. `up --build` is
    # the one that already failed the person reading this.
    assert "force-recreate" in report.remedy
    # Wall-clock, so allow a hair of slack rather than asserting an exact
    # boundary that a sub-microsecond difference can fail.
    assert report.process_uptime_seconds == pytest.approx(3600, abs=5)


def test_staleness_is_clean_when_nothing_changed(tmp_path):
    import os

    root = tmp_path / "app"
    root.mkdir()
    module = root / "main.py"
    module.write_text("# old\n")

    process_start = datetime.now(tz=timezone.utc).timestamp()
    os.utime(module, (process_start - 7200, process_start - 7200))

    report = diagnostics.collect_staleness(
        roots=[root], process_started_at=process_start
    )
    assert report.stale is False
    assert report.stale_file_count == 0
    assert report.checked_files == 1
    # R1: the probe states what it cannot see rather than implying it is total.
    assert report.limitations


def test_staleness_reports_paths_never_contents(tmp_path):
    import os

    root = tmp_path / "app"
    root.mkdir()
    module = root / "secretive.py"
    module.write_text("SECRET_IN_SOURCE = 'sk-ant-do-not-emit-this'\n")
    process_start = datetime.now(tz=timezone.utc).timestamp() - 60
    os.utime(module, (process_start + 30, process_start + 30))

    report = diagnostics.collect_staleness(
        roots=[root], process_started_at=process_start
    )
    assert report.stale is True
    blob = repr(vars(report))
    assert "SECRET_IN_SOURCE" not in blob
    assert "sk-ant-do-not-emit-this" not in blob


def test_stale_file_listing_is_capped_but_the_count_is_not(tmp_path):
    """A rebuild touches every file; 300 filenames is noise, not a diagnosis.
    The cap is honest because the full count survives alongside it."""
    import os

    root = tmp_path / "app"
    root.mkdir()
    process_start = datetime.now(tz=timezone.utc).timestamp() - 60
    for i in range(diagnostics.MAX_STALE_FILES_LISTED + 10):
        f = root / f"mod_{i}.py"
        f.write_text("x\n")
        os.utime(f, (process_start + 30, process_start + 30))

    report = diagnostics.collect_staleness(
        roots=[root], process_started_at=process_start
    )
    assert report.stale_file_count == diagnostics.MAX_STALE_FILES_LISTED + 10
    assert len(report.stale_files) == diagnostics.MAX_STALE_FILES_LISTED


# =============================================================================
# Source E: git, and being honest when it is not there
# =============================================================================


def test_git_is_honestly_unavailable_outside_a_checkout(tmp_path):
    """Inside the backend container there is no `.git` - compose mounts
    backend/app and backend/alembic, not the repo. The field says so and names
    the two ways to fill it, rather than inventing a SHA."""
    report = diagnostics.collect_git(start=tmp_path)
    assert report.available is False
    assert report.commit is None
    assert "not visible" in report.reason
    assert "host-repo" in report.reason  # names an actual remedy

    # ...and it must not print the path it walked. This message is emitted
    # exactly when the walk FAILED, which on a host means the path is a real
    # filesystem path - and those carry usernames (`/home/<user>/...`,
    # `C:\\Users\\<user>\\...`). Pattern redaction does not model usernames, so
    # the only safe move is never to put one in the string.
    assert str(tmp_path) not in report.reason
    assert tmp_path.as_posix() not in report.reason


def test_git_reports_a_dirty_count_never_filenames(tmp_path):
    """Working-tree filenames are the user's private work in progress and
    carry no diagnostic value the count lacks."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "PATH": __import__("os").environ["PATH"]}
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True, env=env)
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "c"], cwd=repo, capture_output=True, env=env)
    (repo / "PROJECT-CODENAME-SECRET.txt").write_text("x")

    report = diagnostics.collect_git(start=repo)
    assert report.available is True
    assert report.commit and len(report.commit) == 40
    assert report.branch == "main"
    assert report.dirty is True
    assert report.dirty_file_count == 1
    assert "PROJECT-CODENAME-SECRET" not in repr(vars(report))


# =============================================================================
# Source B: the container projection - the most dangerous code in the module
# =============================================================================


def _fake_container_attrs(**overrides):
    attrs = {
        "Name": "/lazyaf-backend-1",
        "Image": "sha256:ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12",
        "RestartCount": 2,
        "Created": "2026-08-31T10:00:00.000000000Z",
        "Config": {
            "Image": "lazyaf-backend:dev",
            "Labels": {
                "com.docker.compose.project": "lazyaf",
                "com.docker.compose.service": "backend",
            },
            # Everything below must NEVER reach the output.
            "Env": [
                "LAZYAF_STEP_AUTH_SECRET=super-secret-step-value-do-not-leak",
                "ANTHROPIC_API_KEY=sk-ant-api03-REALKEYSHAPEDVALUE0000",
                "LAZYAF_RUNNER_AUTH_SECRET=runner-secret-do-not-leak",
            ],
            "Cmd": ["uvicorn", "app.main:app", "--token", "cmdline-secret"],
            "Entrypoint": ["/entrypoint.sh", "--password", "hunter2"],
        },
        "State": {
            "Status": "running",
            "Running": True,
            "StartedAt": "2026-08-31T10:00:00.000000000Z",
            "Health": {"Status": "healthy"},
        },
        "Mounts": [
            {"Source": "/home/bvanderlaan/projects/lazyaf/backend/app", "Destination": "/app/app"},
            {"Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock"},
        ],
        "NetworkSettings": {"Networks": {"lazyaf-network": {}}},
    }
    attrs.update(overrides)
    return attrs


def test_container_projection_drops_env_and_command():
    """THE containment test for source B.

    `docker inspect` on this service returns the deployment's auth secrets in
    `Config.Env`. The projection is an allowlist so that a field docker adds
    tomorrow is excluded by default; this test fails the moment somebody
    inverts it into a denylist.
    """
    info = diagnostics._container_info(_fake_container_attrs())
    blob = repr(vars(info))

    assert "super-secret-step-value-do-not-leak" not in blob
    assert "sk-ant-api03-REALKEYSHAPEDVALUE0000" not in blob
    assert "runner-secret-do-not-leak" not in blob
    assert "cmdline-secret" not in blob
    assert "hunter2" not in blob
    # Mount SOURCES carry the host username; only the count survives.
    assert "bvanderlaan" not in blob
    assert info.mount_count == 2

    # ...and the genuinely useful fields did survive.
    assert info.name == "lazyaf-backend-1"
    assert info.compose_service == "backend"
    assert info.image == "lazyaf-backend:dev"
    assert info.image_id.startswith("sha256:ab12cd34")
    assert info.health == "healthy"
    assert info.restart_count == 2


def test_container_projection_survives_the_rendered_markdown():
    """The projection is only half of it: the renderer must not reach back
    into the raw attrs for anything."""
    report = diagnostics.ContainerReport(
        available=True,
        containers=[diagnostics._container_info(_fake_container_attrs())],
        other_containers_on_host=0,
    )
    rendered = diagnostics._render_containers_section(report)
    for secret in (
        "super-secret-step-value-do-not-leak",
        "sk-ant-api03-REALKEYSHAPEDVALUE0000",
        "hunter2",
        "bvanderlaan",
    ):
        assert secret not in rendered


def test_docker_time_with_nanoseconds_parses():
    """Docker emits RFC3339 with nanosecond precision, which
    `datetime.fromisoformat` rejects outright. A silent None here would make
    every container's uptime 'unknown' - i.e. would disable the second-most
    valuable signal in the bundle."""
    parsed = diagnostics._parse_docker_time("2026-08-31T10:00:00.123456789Z")
    assert parsed is not None
    assert parsed.year == 2026 and parsed.tzinfo is not None
    assert diagnostics._parse_docker_time("0001-01-01T00:00:00Z") is None
    assert diagnostics._parse_docker_time(None) is None


def test_foreign_containers_are_counted_never_named():
    """This host runs `finance_me_captain_db`, which belongs to an unrelated
    private project. An inventory that names it publishes its existence."""
    ours = _fake_container_attrs()
    theirs = _fake_container_attrs(
        Name="/finance_me_captain_db",
        Config={
            "Image": "postgres:16",
            "Labels": {
                "com.docker.compose.project": "finance_me_captain",
                "com.docker.compose.service": "db",
            },
        },
        NetworkSettings={"Networks": {"finance_me_captain_default": {}}},
    )

    assert diagnostics._is_lazyaf_container(ours) is True
    assert diagnostics._is_lazyaf_container(theirs) is False

    report = diagnostics.ContainerReport(
        available=True,
        containers=[diagnostics._container_info(ours)],
        other_containers_on_host=1,
    )
    rendered = diagnostics._render_containers_section(report)
    assert "finance_me_captain" not in rendered
    assert "postgres" not in rendered
    assert "1 other container(s)" in rendered


def test_exited_step_containers_are_grouped_not_enumerated():
    """Found by reading a real bundle: a dev host had 100+ finished step
    containers with random docker names, and listing them turned the most
    valuable section into 7KB of `quizzical_rosalind | exited | python:3.12`
    with the six rows that mattered pushed off the bottom.

    Running containers are the literal answer to "what is actually running",
    so those are enumerated; stopped ones are a count per image.
    """
    running = diagnostics._container_info(_fake_container_attrs())
    report = diagnostics.ContainerReport(
        available=True,
        containers=[running],
        exited_summary={"python:3.12": 104, "lazyaf-runner-claude": 2},
        exited_count=106,
    )
    rendered = diagnostics._render_containers_section(report)

    assert "lazyaf-backend-1" in rendered
    assert "106 stopped LazyAF container(s)" in rendered
    assert "`python:3.12` x104" in rendered
    assert len(rendered) < 2000, "the summary should not be the size of a listing"


def test_running_containers_are_ordered_oldest_first():
    """The container up longest is the one most likely to be running code
    nobody has looked at since - so it is the first row, and it is flagged."""
    report = diagnostics.ContainerReport(
        available=True,
        containers=[
            diagnostics._container_info(_fake_container_attrs(Name="/lazyaf-old-1")),
        ],
        oldest_lazyaf="lazyaf-old-1",
    )
    rendered = diagnostics._render_containers_section(report)
    assert ":warning: oldest" in rendered


def test_step_containers_on_our_network_count_as_ours():
    """A step container the executor spawned has no compose project label; it
    is identified by the network it was attached to."""
    attrs = _fake_container_attrs(
        Name="/lazyaf-step-abc123",
        Config={"Image": "python:3.12", "Labels": {}},
        NetworkSettings={"Networks": {"lazyaf-network": {}}},
    )
    assert diagnostics._is_lazyaf_container(attrs) is True


# =============================================================================
# Source H: settings as names and presence
# =============================================================================


def test_settings_never_render_secret_values(monkeypatch):
    """The owner's rule: `ANTHROPIC_API_KEY: set` is useful, the key is a
    catastrophe."""
    report = diagnostics.collect_settings()
    assert report.error is None
    by_name = {entry.name: entry for entry in report.settings}

    for name in diagnostics.SECRET_SETTINGS:
        assert by_name[name].value is None, f"{name} rendered a value"
        assert "never rendered" in (by_name[name].note or "")

    # The value of the step auth secret this process is actually holding must
    # not appear anywhere in the report.
    from app.config import get_settings

    secret = get_settings().step_auth_secret
    assert secret
    assert secret not in repr(vars(report))
    assert secret not in diagnostics._render_settings_section(report)


def test_database_url_reports_driver_not_credentials():
    """A postgres URL carries `user:password@host`. The useful diagnostic is
    the driver, so that is all that is rendered."""
    report = diagnostics.collect_settings()
    entry = next(e for e in report.settings if e.name == "database_url")
    assert entry.value.endswith(":...")
    assert "@" not in (entry.value or "")
    assert "password" not in (entry.value or "")


def test_a_new_settings_field_is_withheld_by_default():
    """The allowlist's whole point: a field added to Settings after this code
    was written must NOT start rendering its value automatically."""
    report = diagnostics.collect_settings()
    for entry in report.settings:
        if entry.value is not None:
            assert (
                entry.name in diagnostics.PRINTABLE_SETTINGS
                or entry.name in ("database_url", "docker_host", "gpu_node_rates")
            ), f"{entry.name} rendered a value without being on the allowlist"


def test_endpoint_variable_names_are_counted_never_listed(monkeypatch):
    """`LAZYAF_ENDPOINT_ACME_PROD` names a customer. The count is the
    diagnostic; the name is a disclosure."""
    monkeypatch.setenv("LAZYAF_ENDPOINT_ACME_CORP_PROD", "some-value")
    report = diagnostics.collect_settings()
    assert report.endpoint_secret_count >= 1
    rendered = diagnostics._render_settings_section(report)
    assert "ACME_CORP" not in rendered
    assert "ACME_CORP" not in repr(report.env_names)
    assert "some-value" not in rendered


# =============================================================================
# The log ring buffer
# =============================================================================


def test_log_buffer_captures_and_reports_eviction():
    """`412 records` must never be mistaken for `everything that happened`."""
    buf = diagnostics.LogRingBuffer(capacity=3)
    for i in range(5):
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "message %d", (i,), None
        )
        buf.emit(record)
    assert len(buf) == 3
    assert buf.dropped == 2
    assert buf.records()[-1].message == "message 4"


def test_log_buffer_truncates_a_giant_record_honestly():
    buf = diagnostics.LogRingBuffer(capacity=5)
    record = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "x" * (diagnostics.MAX_RECORD_CHARS + 500), (), None
    )
    buf.emit(record)
    stored = buf.records()[0].message
    assert "record truncated" in stored
    assert "500 more characters" in stored


def test_log_buffer_never_raises_on_a_bad_record():
    """A logging handler that can throw turns every log call in the process
    into a possible failure."""
    buf = diagnostics.LogRingBuffer(capacity=5)

    class Exploding:
        def __str__(self):
            raise RuntimeError("boom")

    record = logging.LogRecord("t", logging.INFO, __file__, 1, "%s", (Exploding(),), None)
    buf.emit(record)  # must not raise
    assert len(buf) == 1
    assert "un-renderable" in buf.records()[0].message


# =============================================================================
# Truncation: honest, and never before redaction
# =============================================================================


def test_truncation_keeps_head_and_tail_and_says_what_was_cut():
    """Both ends are load-bearing: the head dates the process, the tail holds
    the failure. Dropping the tail to fit would remove the actual bug."""
    text = "".join(f"line {i}\n" for i in range(4000))
    out, note = diagnostics._truncate(text, 2000)

    assert len(out.encode("utf-8")) <= 2000 + 400  # marker overhead
    assert "line 0" in out
    assert "line 3999" in out
    assert "TRUNCATED" in out
    assert "removed from the MIDDLE" in out
    assert note and "removed from the middle" in note


def test_truncation_is_a_noop_under_the_cap():
    out, note = diagnostics._truncate("short\n", 1000)
    assert out == "short\n"
    assert note is None


# =============================================================================
# The bundle: composition, containment, and fail-closed
# =============================================================================


@pytest.mark.asyncio
async def test_every_member_is_routed_through_the_redactor(db_session):
    """THE composition test.

    Proves the wiring rather than the regexes: a member that bypassed the
    redactor would still 'contain no secret' in a test that only checked for a
    sentinel, but it would be missing the spy's marker here.
    """
    logging.getLogger("diag.test").info("a log line for the buffer")
    spy = SpyRedactor()

    bundle = await diagnostics.build_bundle(
        db_session,
        redactor=spy,
        title="a title the user typed",
        what_happened="a description the user pasted",
    )

    assert bundle.members
    for member in bundle.members:
        assert "<<SPY>>" in member.text, f"{member.name} bypassed the redactor"

    # AT LEAST one call per member, not exactly one. The user's own title and
    # description go through the redactor too, and they are the likeliest
    # place for a pasted 401 body to carry a live key - an equality here made
    # "we started redacting something else as well" read as a failure, which
    # is precisely backwards for a safety net that is supposed to grow.
    assert len(spy.seen) >= len(bundle.members)

    # And the user's words specifically reached it - the leak this pins is a
    # bundle that redacts every collected member, then splices the raw title
    # into its own H1 and self-certifies as clean.
    assert "<<SPY>>" in bundle.document
    joined = chr(10).join(spy.seen)
    assert "a title the user typed" in joined, "the title bypassed the redactor"
    assert (
        "a description the user pasted" in joined
    ), "what_happened bypassed the redactor"


@pytest.mark.asyncio
async def test_step_logs_are_redacted_and_marked_high_risk(db_session):
    """Agent step logs are the highest-risk content in the product and the one
    thing pattern redaction genuinely cannot clean. They are opt-in, and they
    carry the warning next to the thing it describes."""
    await _seed_run(db_session, step_logs="token=SENTINEL-STEP-SECRET-VALUE\n")
    from app.models.pipeline import PipelineRun
    from sqlalchemy import select

    run_id = (await db_session.execute(select(PipelineRun.id))).scalars().first()
    spy = SpyRedactor(replace={"SENTINEL-STEP-SECRET-VALUE": "known"})

    bundle = await diagnostics.build_bundle(
        db_session,
        sources=["system", "run"],
        pipeline_run_id=run_id,
        redactor=spy,
    )

    step_members = [m for m in bundle.members if m.name.startswith("run-step-")]
    assert step_members, "the step log was not included"
    step = step_members[0]
    assert step.risk == diagnostics.Risk.HIGH
    assert "prompts" in step.note and "diffs of your source" in step.note
    assert "SENTINEL-STEP-SECRET-VALUE" not in step.text
    assert "SENTINEL-STEP-SECRET-VALUE" not in bundle.document
    assert bundle.values_redacted == 1

    # The warning has to reach the DOCUMENT, next to the content it describes.
    # A risk field the renderer ignores is a risk nobody is told about.
    doc = bundle.document
    caution = doc.index("[!CAUTION]")
    assert caution < doc.index(step.title) + len(doc)
    assert "prompts" in doc[caution : caution + 400]


@pytest.mark.asyncio
async def test_step_logs_are_not_in_the_default_bundle(db_session):
    """Opt-in, because they are the risky source. A bundle must never widen on
    its own."""
    await _seed_run(db_session, step_logs="private source code here\n")
    bundle = await diagnostics.build_bundle(db_session, redactor=SpyRedactor())
    assert not any(m.name.startswith("run-step-") for m in bundle.members)
    assert "private source code here" not in bundle.document


@pytest.mark.asyncio
async def test_redaction_runs_before_truncation(db_session):
    """ORDER IS A SECURITY PROPERTY. Truncating first can cut a token in half
    and leave a prefix that no longer matches the pattern that would have
    caught it. This asserts the redactor saw the FULL text, not the clamped
    one."""
    big = "".join(f"line {i} SENTINEL-STEP-SECRET-VALUE\n" for i in range(3000))
    await _seed_run(db_session, step_logs=big)
    from app.models.pipeline import PipelineRun
    from sqlalchemy import select

    run_id = (await db_session.execute(select(PipelineRun.id))).scalars().first()
    spy = SpyRedactor(replace={"SENTINEL-STEP-SECRET-VALUE": "known"})

    bundle = await diagnostics.build_bundle(
        db_session,
        sources=["system", "run"],
        pipeline_run_id=run_id,
        member_max_bytes=4000,
        redactor=spy,
    )
    step = next(m for m in bundle.members if m.name.startswith("run-step-"))
    assert step.truncated is True
    assert "SENTINEL-STEP-SECRET-VALUE" not in step.text
    # The redactor received every byte, including the ones truncation dropped.
    seen_step_text = [t for t in spy.seen if t.startswith("line 0 ")]
    assert seen_step_text and len(seen_step_text[0]) > 4000


@pytest.mark.asyncio
async def test_a_pathological_member_is_bounded_before_redaction(db_session):
    """A DoS bound, and the one place "redact before you truncate" is relaxed.

    The shared redactor is quadratic on a single line with no newlines -
    measured at 0.07s for 10K characters, 1.7s for 50K and 37s for 200K. An
    agent step log that dumps one long JSON blob is exactly that shape, so an
    un-capped member wedges the request for minutes.

    The bytes past the cap are DISCARDED rather than emitted, and the cut is
    reported in the member itself.
    """
    huge = "j" * (diagnostics.MAX_RAW_MEMBER_BYTES + 50_000)  # one enormous line
    await _seed_run(db_session, step_logs=huge)
    from app.models.pipeline import PipelineRun
    from sqlalchemy import select

    run_id = (await db_session.execute(select(PipelineRun.id))).scalars().first()
    spy = SpyRedactor()

    bundle = await diagnostics.build_bundle(
        db_session, sources=["run"], pipeline_run_id=run_id, redactor=spy
    )
    step = next(m for m in bundle.members if m.name.startswith("run-step-"))
    assert step.truncated is True
    # The LINE clamp is what fires - it is the bound that matters, and it
    # fires before the coarser byte cap ever becomes relevant.
    assert "clamped" in step.truncation_note
    assert "before redaction" in step.truncation_note.lower()

    # The redactor never saw more than the cap - which is the property that
    # bounds the request.
    step_inputs = [t for t in spy.seen if t.startswith("jjj")]
    assert step_inputs
    assert len(step_inputs[0].encode("utf-8")) <= diagnostics.MAX_RAW_MEMBER_BYTES + 500
    # No single line reaching the redactor exceeds the clamp - the line, not
    # the total, is what makes it quadratic.
    assert max(len(l) for l in step_inputs[0].splitlines()) < (
        diagnostics.MAX_REDACTION_LINE_CHARS + 200
    )


@pytest.mark.asyncio
async def test_bundle_refuses_when_the_redactor_is_unavailable(db_session, monkeypatch):
    """FAIL-CLOSED. No degraded mode, no warning banner: the person hitting
    this is about to paste the result somewhere permanent."""

    def _boom(*args, **kwargs):
        raise diagnostics.RedactorUnavailable("redaction module is gone")

    monkeypatch.setattr(diagnostics, "resolve_redactor", _boom)
    with pytest.raises(diagnostics.RedactorUnavailable):
        await diagnostics.build_bundle(db_session)


@pytest.mark.asyncio
async def test_a_failed_member_redaction_is_surfaced(db_session):
    """The shared redactor fails closed by withholding content. A section that
    silently vanished would read as a section that was empty."""

    class FailingRedactor:
        def redact_with_receipt(self, text):
            from app.services.redaction import RedactionResult

            return RedactionResult(text="[WITHHELD]", counts={}, failed=True)

    bundle = await diagnostics.build_bundle(db_session, redactor=FailingRedactor())
    assert all(m.redaction_failed for m in bundle.members)
    assert bundle.redaction_notes
    assert "Redaction FAILED" in bundle.document


@pytest.mark.asyncio
async def test_job_source_falls_back_to_the_step_run_logs(db_session):
    """A card job's output lives on the StepRun until the run completes - the
    same fallback `GET /api/jobs/{id}/logs` already implements. A bundle that
    only read `Job.logs` would be empty for exactly the in-flight job someone
    is trying to report."""
    from app.models.card import Card
    from app.models.job import Job

    run, step = await _seed_run(db_session, step_logs="agent output SENTINEL-JOB\n")
    card = Card(repo_id=(await _first_repo_id(db_session)), title="c")
    db_session.add(card)
    await db_session.flush()
    job = Job(card_id=card.id, status="running", logs="", step_run_id=step.id)
    db_session.add(job)
    await db_session.commit()

    spy = SpyRedactor(replace={"SENTINEL-JOB": "known"})
    bundle = await diagnostics.build_bundle(
        db_session, sources=["job"], job_id=job.id, redactor=spy
    )
    log_member = next(m for m in bundle.members if m.name == "job.log")
    assert log_member.risk == diagnostics.Risk.HIGH
    assert "SENTINEL-JOB" not in log_member.text
    assert "REDACTED" in log_member.text


async def _first_repo_id(db):
    from sqlalchemy import select

    from app.models.repo import Repo

    return (await db.execute(select(Repo.id))).scalars().first()


@pytest.mark.asyncio
async def test_a_missing_run_says_so_rather_than_failing(db_session):
    """A stale run id in a bug report is common - the run was pruned, or the
    id was mistyped. Say it does not exist; do not 500 on the person already
    filing a bug."""
    bundle = await diagnostics.build_bundle(
        db_session,
        sources=["run"],
        pipeline_run_id="00000000-0000-0000-0000-000000000000",
        redactor=SpyRedactor(),
    )
    member = next(m for m in bundle.members if m.name == "run.md")
    assert "No pipeline run" in member.text


@pytest.mark.asyncio
async def test_unknown_source_is_refused_by_name(db_session):
    with pytest.raises(ValueError) as exc:
        await diagnostics.build_bundle(
            db_session, sources=["system", "nope"], redactor=SpyRedactor()
        )
    assert "nope" in str(exc.value)
    assert "Known sources" in str(exc.value)


@pytest.mark.asyncio
async def test_run_source_requires_a_run_id(db_session):
    with pytest.raises(ValueError, match="pipeline_run_id"):
        await diagnostics.build_bundle(
            db_session, sources=["run"], redactor=SpyRedactor()
        )


@pytest.mark.asyncio
async def test_bundle_title_is_a_headline_not_a_paragraph(db_session):
    """Found by reading a real bundle: the first version spliced the full
    explanation into the H1 and produced a 200-character title that had to be
    read to the end before it said anything."""
    bundle = await diagnostics.build_bundle(db_session, redactor=SpyRedactor())
    first_line = bundle.document.splitlines()[0]
    assert first_line.startswith("# ")
    assert len(first_line) < 80, f"title is a paragraph: {first_line!r}"
    assert "\n" not in bundle.title


@pytest.mark.asyncio
async def test_readme_renders_last_so_the_diagnosis_comes_first(db_session):
    """The caveats travel with the artifact, but leading a bug report with 300
    words of them buries what the reader opened the file for. A short warning
    sits up top instead."""
    bundle = await diagnostics.build_bundle(db_session, redactor=SpyRedactor())
    doc = bundle.document
    assert doc.index("What is actually running") < doc.index("Read this before")
    # The short pointer is above the content.
    assert doc.index("cannot remove") < doc.index("What is actually running")


@pytest.mark.asyncio
async def test_bundle_document_leads_with_the_system_facts(db_session):
    """A maintainer reading the issue needs 'is this a real bug or a stale
    container' before they need any log line."""
    bundle = await diagnostics.build_bundle(
        db_session, what_happened="clicked Start, got a 500", redactor=SpyRedactor()
    )
    doc = bundle.document
    assert "clicked Start, got a 500" in doc
    assert doc.index("What is actually running") < doc.index("Backend log")
    # The README travels with the artifact, so the warning reaches whoever
    # opens the file rather than living in a UI they never saw.
    assert "cannot" in doc and "source code" in doc
    assert "PUBLIC" in doc or "public" in doc


# =============================================================================
# The API surface
# =============================================================================


@pytest.mark.asyncio
async def test_system_endpoint_answers_what_is_running(client):
    response = await client.get("/api/diagnostics/system")
    assert response.status_code == 200
    body = response.json()

    assert "staleness" in body
    assert isinstance(body["staleness"]["stale"], bool)
    assert body["staleness"]["process_started_at"]
    assert body["staleness"]["checked_files"] > 0
    assert "migrations" in body and "git" in body and "containers" in body
    assert body["python_version"]


@pytest.mark.asyncio
async def test_system_endpoint_never_leaks_the_step_auth_secret(client):
    """The end-to-end containment check on the always-on endpoint."""
    from app.config import get_settings

    secret = get_settings().step_auth_secret
    response = await client.get("/api/diagnostics/system")
    assert secret not in response.text


@pytest.mark.asyncio
async def test_container_inventory_unavailable_is_stated(client):
    """T1 runs with Docker stopped. An empty list with no explanation would
    read as 'nothing is running' - R4's fake green, in a diagnostic."""
    response = await client.get("/api/diagnostics/system")
    containers = response.json()["containers"]
    assert containers["available"] is False
    assert containers["reason"]
    assert "docker" in containers["reason"].lower()
    assert containers["containers"] == []


@pytest.mark.asyncio
async def test_logs_endpoint_redacts_on_the_way_to_the_screen(client):
    """A screenshot of an unredacted token is the same leak with extra steps,
    so the screen gets the same redactor as the bundle."""
    logging.getLogger("diag.test").error(
        "upstream said: sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    )
    response = await client.get("/api/diagnostics/logs?min_level=ERROR")
    assert response.status_code == 200
    body = response.json()
    assert body["redacted"] is True
    assert "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in response.text
    assert any("REDACTED" in r["message"] for r in body["records"])


@pytest.mark.asyncio
async def test_log_view_placeholders_are_stable_across_polls(client):
    """The placeholder's whole job is telling the reader that two lines mean
    the SAME value. A fresh salt per request would renumber it every 3-second
    poll - flicker on screen, and the correlation gone."""
    logging.getLogger("diag.test").error(
        "first sighting sk-ant-api03-BBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    )
    first = await client.get("/api/diagnostics/logs?min_level=ERROR")
    second = await client.get("/api/diagnostics/logs?min_level=ERROR")

    def placeholder(response):
        import re

        match = re.search(r"\[REDACTED:[^\]]+\]", response.text)
        assert match, response.text
        return match.group(0)

    assert placeholder(first) == placeholder(second)


@pytest.mark.asyncio
async def test_each_bundle_gets_its_own_salt(db_session):
    """Two bundles must NOT share a digest for the same secret: an unsalted or
    shared digest of a known-format secret is a confirmation oracle, and these
    documents get published to a public tracker."""
    from app.models.pipeline import PipelineRun
    from sqlalchemy import select

    key = "sk-ant-api03-" + "Y" * 80
    await _seed_run(db_session, step_logs=f"key={key}\n")
    run_id = (await db_session.execute(select(PipelineRun.id))).scalars().first()

    import re

    digests = []
    for _ in range(2):
        bundle = await diagnostics.build_bundle(
            db_session, sources=["run"], pipeline_run_id=run_id
        )
        match = re.search(r"\[REDACTED:anthropic:([0-9a-f]+)\]", bundle.document)
        assert match, bundle.document
        digests.append(match.group(1))
    assert digests[0] != digests[1], "two bundles shared a salt"


@pytest.mark.asyncio
async def test_logs_endpoint_rejects_a_bad_level(client):
    response = await client.get("/api/diagnostics/logs?min_level=LOUD")
    assert response.status_code == 422
    assert "LOUD" in response.json()["detail"]


@pytest.mark.asyncio
async def test_bundle_round_trip_and_download(client):
    logging.getLogger("diag.test").info("something for the log section")
    response = await client.post(
        "/api/diagnostics/bundle",
        json={"what_happened": "branch not found on a branch that exists"},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["id"]
    assert body["members"]
    assert body["total_bytes"] > 0
    # Every member arrives with its FINAL bytes, so the review sheet renders
    # the artifact rather than a rehearsal of it.
    assert all("text" in m for m in body["members"])

    read_back = await client.get(f"/api/diagnostics/bundle/{body['id']}")
    assert read_back.status_code == 200

    download = await client.get(body["download_url"])
    assert download.status_code == 200
    assert "text/markdown" in download.headers["content-type"]
    assert "attachment" in download.headers["content-disposition"]
    assert ".md" in download.headers["content-disposition"]
    assert download.text.startswith("#")
    assert "branch not found on a branch that exists" in download.text


@pytest.mark.asyncio
async def test_bundle_offers_no_upload_control(client):
    """Phase 1 ships no upload path. Not greyed out with a tooltip - absent,
    with a reason, because a disabled button is an invitation to find the flag
    that enables it."""
    response = await client.post("/api/diagnostics/bundle", json={})
    body = response.json()
    assert body["upload_available"] is False
    assert "review" in body["upload_unavailable_reason"]
    assert "token" in body["upload_unavailable_reason"]

    routes = [r for r in _app_routes() if "diagnostics" in r]
    assert not any("github" in r or "upload" in r or "issue" in r for r in routes), (
        f"an upload route exists: {routes}"
    )


def _app_routes():
    from app.main import app

    return [getattr(r, "path", "") for r in app.routes]


@pytest.mark.asyncio
async def test_missing_bundle_explains_the_ttl(client):
    response = await client.get("/api/diagnostics/bundle/does-not-exist")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert "minutes" in detail and "never written to disk" in detail


@pytest.mark.asyncio
async def test_bundle_store_evicts_by_ttl_and_count():
    """In memory, short-lived, few. The endpoint is unauthenticated like the
    rest of this API, so the mitigations are an unguessable id and a bundle
    that stops existing."""
    store = diagnostics.BundleStore(ttl=timedelta(minutes=15), max_retained=2)
    now = datetime.now(tz=timezone.utc)

    def make(bundle_id, created):
        return diagnostics.Bundle(
            id=bundle_id,
            created_at=created,
            expires_at=created + timedelta(minutes=15),
            title="t",
            what_happened=None,
            members=[],
            total_bytes=0,
            redaction_counts={},
            values_redacted=0,
            document="",
        )

    store.put(make("a", now - timedelta(minutes=5)))
    store.put(make("b", now - timedelta(minutes=2)))
    store.put(make("c", now))
    assert store.get("a") is None, "oldest should be evicted past max_retained"
    assert store.get("c") is not None

    store.clear()
    store.put(make("expired", now - timedelta(minutes=30)))
    assert store.get("expired") is None, "TTL should have removed it"


@pytest.mark.asyncio
async def test_bundle_rejects_a_run_source_without_an_id(client):
    response = await client.post(
        "/api/diagnostics/bundle", json={"sources": ["system", "run"]}
    )
    assert response.status_code == 422
    assert "pipeline_run_id" in response.json()["detail"]


@pytest.mark.asyncio
async def test_redactor_unavailable_answers_503(client, monkeypatch):
    """The router's half of fail-closed."""

    def _boom(*args, **kwargs):
        raise diagnostics.RedactorUnavailable("redaction module is gone")

    monkeypatch.setattr(diagnostics, "resolve_redactor", _boom)
    response = await client.post("/api/diagnostics/bundle", json={})
    assert response.status_code == 503
    assert "redaction module is gone" in response.json()["detail"]
    assert "un-redacted" in response.json()["detail"]


# =============================================================================
# The real redactor, end to end
# =============================================================================


@pytest.mark.asyncio
async def test_a_real_secret_shape_is_gone_from_the_downloaded_bytes(client, db_session):
    """The joint test with the redaction module: a live-format key seeded into
    a step log must not survive into the artifact a human would attach.

    Uses the SHARED scanner (`find_secrets`) rather than a hand-written
    assertion, so this test and the CI gate can never disagree about what
    counts as a secret.
    """
    from app.services.redaction import find_secrets

    key = "sk-ant-api03-" + "Z" * 80
    await _seed_run(db_session, step_logs=f"ANTHROPIC_API_KEY={key}\ncalling model\n")
    from app.models.pipeline import PipelineRun
    from sqlalchemy import select

    run_id = (await db_session.execute(select(PipelineRun.id))).scalars().first()

    response = await client.post(
        "/api/diagnostics/bundle",
        json={"sources": ["system", "run"], "pipeline_run_id": run_id},
    )
    assert response.status_code == 201, response.text
    body = response.json()

    download = await client.get(body["download_url"])
    assert key not in download.text
    assert not find_secrets(download.text), "the assembled bundle tripped the CI scanner"
    assert body["values_redacted"] and body["values_redacted"] >= 1
