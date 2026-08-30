"""Tests for scripts/bootstrap_secrets.py - Phase 12.7.

The script exists so that "starting the stack" populates the shared auth
secrets with random values instead of inheriting a constant from the source
tree. That makes three properties load-bearing, and every one of them is a
property about what the script does NOT do:

  * it never overwrites a value that is already there (re-running must be
    safe, because everything from QUICKSTART to preflight tells you to run it);
  * it never prints a secret (people paste this output into issues);
  * two simultaneous runs never lose each other's keys or leave a torn .env.

Driven through the real CLI by subprocess wherever the behaviour is about the
file on disk, because that is how a person and a Makefile both invoke it.
"""
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "bootstrap_secrets.py"

MANAGED = ("LAZYAF_STEP_AUTH_SECRET", "LAZYAF_RUNNER_AUTH_SECRET")
RETIRED_STEP_DEFAULT = "lazyaf-step-auth-secret-key-change-in-production"
RETIRED_RUNNER_DEFAULT = "lazyaf-runner-auth-secret-key-change-in-production"


def run(env_file: Path, *args: str, env: dict | None = None):
    """Invoke the CLI against an isolated .env."""
    child_env = dict(os.environ)
    # The ambient shell must not decide the outcome of a test about a file.
    for name in MANAGED:
        child_env.pop(name, None)
        child_env.pop(name + "_FILE", None)
    child_env.update(env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--env-file", str(env_file), *args],
        capture_output=True,
        text=True,
        env=child_env,
    )


def values_in(env_file: Path) -> dict:
    """Active assignments in a .env, the way dotenv reads them."""
    found = {}
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        found[key.strip()] = value.strip()
    return found


@pytest.fixture
def env_file(tmp_path) -> Path:
    return tmp_path / ".env"


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def test_creates_env_from_the_template_when_missing(env_file, tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(
        "# LazyAF example\n# ANTHROPIC_API_KEY=\n", encoding="utf-8"
    )
    result = run(env_file, "--template", str(template))

    assert result.returncode == 0, result.stdout + result.stderr
    assert env_file.exists()
    # The template's own content survives - this is a seed, not a replacement.
    assert "# LazyAF example" in env_file.read_text(encoding="utf-8")
    for name in MANAGED:
        assert values_in(env_file)[name]


def test_generates_both_secrets_with_real_entropy(env_file, tmp_path):
    run(env_file, "--template", str(tmp_path / "no-such-template"))
    values = values_in(env_file)

    for name in MANAGED:
        assert len(values[name]) >= 43, f"{name} is too short to be generated"
        assert re.fullmatch(r"[A-Za-z0-9_\-]+", values[name]), "expected URL-safe"
    assert values[MANAGED[0]] != values[MANAGED[1]], "one value used twice is one secret"


def test_two_installations_do_not_share_a_secret(tmp_path):
    """Per-installation, not per-release. Otherwise it is a default again."""
    first, second = tmp_path / "a.env", tmp_path / "b.env"
    run(first, "--template", str(tmp_path / "none"))
    run(second, "--template", str(tmp_path / "none"))
    assert values_in(first)[MANAGED[0]] != values_in(second)[MANAGED[0]]


# ---------------------------------------------------------------------------
# Idempotence - the property everything else depends on
# ---------------------------------------------------------------------------

def test_rerunning_changes_nothing(env_file, tmp_path):
    run(env_file, "--template", str(tmp_path / "none"))
    before = env_file.read_text(encoding="utf-8")

    result = run(env_file, "--template", str(tmp_path / "none"))

    assert result.returncode == 0
    assert env_file.read_text(encoding="utf-8") == before, "a re-run rewrote the file"
    assert "kept" in result.stdout


def test_an_existing_value_is_never_overwritten(env_file):
    env_file.write_text(
        "LAZYAF_STEP_AUTH_SECRET=my-own-carefully-chosen-value\n", encoding="utf-8"
    )
    run(env_file)

    values = values_in(env_file)
    assert values["LAZYAF_STEP_AUTH_SECRET"] == "my-own-carefully-chosen-value"
    assert values["LAZYAF_RUNNER_AUTH_SECRET"], "the missing one is still generated"


def test_unrelated_keys_and_comments_survive(env_file):
    original = (
        "# my notes\n"
        # Deliberately NOT key-shaped: .github/scripts/scan_repo_secrets.py
        # fails the build on any `sk-ant-` string that is not an allowlisted
        # sentinel, and this fixture only needs an unrelated line to survive.
        "ANTHROPIC_API_KEY=redacted-fixture-value\n"
        "CLAUDE_RUNNERS=4\n"
        "\n"
        "# LAZYAF_BACKEND_PORT=\n"
    )
    env_file.write_text(original, encoding="utf-8")
    run(env_file)

    text = env_file.read_text(encoding="utf-8")
    assert "# my notes" in text
    assert "ANTHROPIC_API_KEY=redacted-fixture-value" in text
    assert "CLAUDE_RUNNERS=4" in text
    assert "# LAZYAF_BACKEND_PORT=" in text


def test_an_empty_assignment_is_filled_in_place(env_file):
    env_file.write_text(
        "LAZYAF_STEP_AUTH_SECRET=\nCLAUDE_RUNNERS=4\n", encoding="utf-8"
    )
    run(env_file)

    lines = env_file.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("LAZYAF_STEP_AUTH_SECRET=")
    assert len(values_in(env_file)["LAZYAF_STEP_AUTH_SECRET"]) >= 43
    # Exactly one assignment for the key: no shadowing duplicate appended.
    assert sum(1 for line in lines if line.startswith("LAZYAF_STEP_AUTH_SECRET=")) == 1


@pytest.mark.parametrize(
    "stale", [RETIRED_STEP_DEFAULT, "changeme", "TODO", "xxxxxxxx"]
)
def test_a_retired_default_or_placeholder_is_replaced(env_file, stale):
    """The whole point: an inherited .env must not keep the published key."""
    env_file.write_text(f"LAZYAF_STEP_AUTH_SECRET={stale}\n", encoding="utf-8")
    run(env_file)

    value = values_in(env_file)["LAZYAF_STEP_AUTH_SECRET"]
    assert value != stale
    assert len(value) >= 43


def test_a_file_pointer_is_respected_instead_of_generating(env_file):
    """You already said where the secret lives; writing another is confusing."""
    env_file.write_text(
        "LAZYAF_STEP_AUTH_SECRET_FILE=/run/secrets/step\n", encoding="utf-8"
    )
    result = run(env_file)

    assert "LAZYAF_STEP_AUTH_SECRET" not in values_in(env_file)
    assert "delegated" in result.stdout


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------

def test_no_generated_secret_is_ever_printed(env_file, tmp_path):
    result = run(env_file, "--template", str(tmp_path / "none"))
    output = result.stdout + result.stderr
    for value in values_in(env_file).values():
        assert value not in output, "a generated secret reached stdout"


def test_an_existing_secret_is_never_printed_back(env_file):
    sentinel = "SENTINEL-EXISTING-SECRET-DO-NOT-ECHO-0123456789"
    env_file.write_text(f"LAZYAF_STEP_AUTH_SECRET={sentinel}\n", encoding="utf-8")
    result = run(env_file)
    assert sentinel not in result.stdout + result.stderr


def test_check_mode_does_not_print_secrets_either(env_file):
    sentinel = "SENTINEL-CHECK-MODE-DO-NOT-ECHO-0123456789abcd"
    env_file.write_text(
        f"LAZYAF_STEP_AUTH_SECRET={sentinel}\n"
        f"LAZYAF_RUNNER_AUTH_SECRET={sentinel}-r\n",
        encoding="utf-8",
    )
    result = run(env_file, "--check")
    assert result.returncode == 0
    assert sentinel not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------

def test_check_reports_missing_and_changes_nothing(env_file):
    env_file.write_text("CLAUDE_RUNNERS=4\n", encoding="utf-8")
    before = env_file.read_text(encoding="utf-8")

    result = run(env_file, "--check")

    assert result.returncode == 1
    assert env_file.read_text(encoding="utf-8") == before
    for name in MANAGED:
        assert name in result.stdout
    assert "bootstrap_secrets.py" in result.stdout


def test_check_passes_once_bootstrapped(env_file, tmp_path):
    run(env_file, "--template", str(tmp_path / "none"))
    assert run(env_file, "--check").returncode == 0


def test_check_on_a_missing_file_fails_without_creating_it(env_file):
    result = run(env_file, "--check")
    assert result.returncode == 1
    assert not env_file.exists()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_runs_leave_one_consistent_file(tmp_path):
    """Read-modify-write on a shared file is a lost-update bug by default.

    Six processes race on one fresh .env. The invariant is not "whoever wins":
    it is that the file that survives is well-formed and holds BOTH keys, and
    that no temp or lock file is left lying around.
    """
    env_file = tmp_path / ".env"
    template = tmp_path / "none"
    results = []

    def worker():
        results.append(run(env_file, "--template", str(template)))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all(r.returncode == 0 for r in results), [
        r.stdout + r.stderr for r in results if r.returncode != 0
    ]
    values = values_in(env_file)
    for name in MANAGED:
        assert len(values.get(name, "")) >= 43, f"{name} lost in the race"

    leftovers = [p.name for p in tmp_path.iterdir() if p.name != ".env"]
    assert leftovers == [], f"temp/lock files left behind: {leftovers}"


def test_a_concurrent_run_does_not_clobber_a_value_written_between_read_and_write(
    tmp_path,
):
    """The second process must re-read INSIDE the lock.

    Simulated by starting from a file that already has one key: the run has to
    notice the other one and add it without touching the first.
    """
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LAZYAF_STEP_AUTH_SECRET=written-by-the-other-process-9f2a11c4\n",
        encoding="utf-8",
    )
    run(env_file)
    values = values_in(env_file)
    assert values["LAZYAF_STEP_AUTH_SECRET"] == "written-by-the-other-process-9f2a11c4"
    assert len(values["LAZYAF_RUNNER_AUTH_SECRET"]) >= 43


def test_a_stale_lock_does_not_wedge_the_next_run_forever(tmp_path, monkeypatch):
    """A killed process must not lock a stranger out of their own setup."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import bootstrap_secrets as module
    finally:
        sys.path.pop(0)

    env_file = tmp_path / ".env"
    env_file.write_text("CLAUDE_RUNNERS=4\n", encoding="utf-8")
    lock = tmp_path / (".env" + module.LOCK_SUFFIX)
    lock.write_text("99999", encoding="utf-8")
    # Backdate it past the stale threshold.
    old = os.path.getmtime(str(lock)) - module.LOCK_STALE_SECONDS - 10
    os.utime(str(lock), (old, old))

    monkeypatch.setattr(module, "LOCK_TIMEOUT_SECONDS", 2.0)
    import io

    stream = io.StringIO()
    code = module.run(env_file, tmp_path / "none", False, {}, stream)

    assert code == 0, stream.getvalue()
    assert not lock.exists()
    assert len(values_in(env_file)["LAZYAF_STEP_AUTH_SECRET"]) >= 43


# ---------------------------------------------------------------------------
# The script is usable where it has to be usable
# ---------------------------------------------------------------------------

def test_runs_on_a_bare_interpreter_with_no_dependencies():
    """It runs BEFORE `uv sync`, on a fresh clone. Stdlib imports only."""
    source = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", source, re.M))
    allowed = {
        "argparse", "errno", "os", "secrets", "sys", "time", "pathlib",
        "__future__",
    }
    assert imported <= allowed, f"non-stdlib import: {sorted(imported - allowed)}"


# ---------------------------------------------------------------------------
# Where .env goes
# ---------------------------------------------------------------------------

def test_default_env_file_is_the_repo_root_in_a_checkout():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import bootstrap_secrets as module
    finally:
        sys.path.pop(0)
    assert module.DEFAULT_ENV_FILE == REPO_ROOT / ".env"


def test_standalone_download_writes_beside_itself_not_a_level_up(tmp_path):
    """The release publishes this script as a bare asset.

    Downloaded next to docker-compose.release.yml, ``<here>/..`` is somebody's
    Downloads folder or home directory. Writing a .env there would be both
    wrong and startling.
    """
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import bootstrap_secrets as module
    finally:
        sys.path.pop(0)

    loose = tmp_path / "lazyaf-download"
    loose.mkdir()
    assert module._default_root(loose) == loose

    # A directory literally named "scripts" but with no LazyAF above it is
    # still standalone.
    scripts_like = tmp_path / "somewhere" / "scripts"
    scripts_like.mkdir(parents=True)
    assert module._default_root(scripts_like) == scripts_like

    # ...and with a marker above it, it IS a checkout.
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / ".env.example").write_text("", encoding="utf-8")
    assert module._default_root(checkout / "scripts") == checkout
