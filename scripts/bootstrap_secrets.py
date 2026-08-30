#!/usr/bin/env python3
"""Put real, random shared secrets in your .env — once, and then never again.

    python scripts/bootstrap_secrets.py            # generate what is missing
    python scripts/bootstrap_secrets.py --check    # report only, change nothing
    python scripts/bootstrap_secrets.py --env-file /path/to/.env

WHAT IT DOES
    LazyAF's backend refuses to start without ``LAZYAF_STEP_AUTH_SECRET`` and
    ``LAZYAF_RUNNER_AUTH_SECRET``. They are not ceremony: the first signs the
    JWTs step containers use to call /api/steps/*, the second is what a runner
    agent presents to enrol over /ws/runner. There is no built-in default,
    because a default that ships in the source is a published secret.

    This script writes strong random values for exactly the keys that are
    missing, and leaves everything else alone.

WHAT IT WILL NOT DO
    * It never overwrites a value you already set. Re-running is a no-op.
    * It never prints a secret. Run it in a shared terminal; paste its output
      into an issue. If you can see a secret in this output, that is a bug.
    * It never touches keys it does not manage (your API keys, your ports).
    * It never generates for a key you have pointed at a file with
      ``<NAME>_FILE`` — you already said where that secret lives.

    Safe to run repeatedly, and safe to run concurrently: the rewrite takes a
    lock next to the file and lands through an atomic replace, so two
    simultaneous runs cannot lose each other's keys or leave a half-written
    .env behind.

Standard library only, so it runs on a bare Python 3.8+ with the repo freshly
cloned and nothing installed. Exit 0 when the file is in good shape, 1 when
--check found something to fix (or the file could not be written).
"""

import argparse
import errno
import os
import secrets
import sys
import time
from pathlib import Path

#: Files that identify a LazyAF checkout rather than a directory that merely
#: happens to contain this script.
_ROOT_MARKERS = (".env.example", "docker-compose.release.yml", "docker-compose.yml")

_HERE = Path(__file__).resolve().parent


def _default_root(here=_HERE):
    """Where .env belongs.

    In a checkout this file lives at ``<root>/scripts/``, so the root is one
    level up. But it is ALSO published as a standalone release asset, next to
    docker-compose.release.yml and .env.example in whatever directory someone
    downloaded them into - and there, one level up is the wrong place entirely
    (it would write a stranger's .env into their home or Downloads parent).
    Decide by looking for a marker, not by assuming the layout.
    """
    if here.name == "scripts":
        parent = here.parent
        if any((parent / marker).exists() for marker in _ROOT_MARKERS):
            return parent
    return here


REPO_ROOT = _default_root()
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_TEMPLATE = REPO_ROOT / ".env.example"

#: Bytes of entropy per generated secret. token_urlsafe(48) is 64 characters.
SECRET_BYTES = 48

#: (env var, one-line purpose). The purpose is written into .env as a comment
#: above the generated line, so someone reading the file later knows what they
#: are looking at without going to find the docs.
MANAGED_SECRETS = [
    (
        "LAZYAF_STEP_AUTH_SECRET",
        "signs the short-lived JWT a step container uses to call /api/steps/*",
    ),
    (
        "LAZYAF_RUNNER_AUTH_SECRET",
        "shared enrollment secret a runner agent presents at /ws/runner",
    ),
]

#: The constants LazyAF used to ship. Public, therefore not secrets. Present in
#: an inherited .env they are replaced, not kept.
RETIRED_PUBLIC_SECRETS = frozenset(
    {
        "lazyaf-step-auth-secret-key-change-in-production",
        "lazyaf-runner-auth-secret-key-change-in-production",
    }
)

#: Kept in sync, by eye, with backend/app/config.py's _PLACEHOLDER_SECRETS.
#: Duplicated rather than imported on purpose: this script must run before any
#: dependency is installed and without the backend package on sys.path.
PLACEHOLDER_SECRETS = frozenset(
    {
        "change-in-production",
        "change-me",
        "change_me",
        "changeme",
        "generate-me",
        "generateme",
        "none",
        "null",
        "placeholder",
        "replace-me",
        "replaceme",
        "secret",
        "tbd",
        "todo",
        "unset",
        "your-secret-here",
        "<generate>",
    }
)

LOCK_SUFFIX = ".lazyaf-bootstrap.lock"
LOCK_TIMEOUT_SECONDS = 20.0
LOCK_STALE_SECONDS = 120.0

GENERATED = "generated"
KEPT = "kept"
DELEGATED = "delegated"


# ----------------------------------------------------------------- values ---

def is_placeholder(value):
    """True when a value means 'not filled in' rather than a real secret."""
    if value is None:
        return True
    candidate = value.strip()
    if not candidate:
        return True
    if candidate in RETIRED_PUBLIC_SECRETS:
        return True
    lowered = candidate.lower()
    if lowered in PLACEHOLDER_SECRETS:
        return True
    if set(lowered) == {"x"}:
        return True
    return False


def generate_secret():
    """A cryptographically strong URL-safe value (64 chars)."""
    return secrets.token_urlsafe(SECRET_BYTES)


# ------------------------------------------------------------ .env parsing ---

def _strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def split_assignment(line):
    """(key, value) for an ACTIVE assignment line, else None.

    Commented lines are deliberately not assignments: ``# LAZYAF_X=`` is
    documentation, and replacing it in place would be an edit nobody asked
    for. A missing key gets a fresh block appended instead.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    if key.startswith("export "):
        key = key[len("export "):].strip()
    if not key:
        return None
    return key, _strip_quotes(value.strip())


def read_env_values(lines):
    """{KEY: value} from active assignments. Last occurrence wins, as dotenv."""
    values = {}
    for line in lines:
        parsed = split_assignment(line)
        if parsed is not None:
            values[parsed[0]] = parsed[1]
    return values


def detect_newline(raw):
    """Preserve the file's existing line ending instead of imposing one."""
    if "\r\n" in raw:
        return "\r\n"
    if "\n" in raw:
        return "\n"
    return os.linesep


def load_lines(path):
    """(lines, newline) for an existing file, or ([], os.linesep) if absent."""
    if not path.exists():
        return [], os.linesep
    raw = path.read_text(encoding="utf-8", errors="replace")
    return raw.splitlines(), detect_newline(raw)


# -------------------------------------------------------------- the rewrite ---

def plan_and_apply(lines, ambient_env):
    """Return (new_lines, [(key, action)]) for one pass over the managed keys.

    Pure: no I/O. That is what makes the concurrency story testable — the
    caller does read, plan, atomic-write, all inside the lock.
    """
    values = read_env_values(lines)
    new_lines = list(lines)
    actions = []
    appended = []

    for key, purpose in MANAGED_SECRETS:
        file_var = key + "_FILE"
        # A *_FILE pointer is an explicit statement about where the secret
        # lives. Generating an inline value next to it would write a secret
        # that is silently shadowed at resolution time.
        if (values.get(file_var) or ambient_env.get(file_var) or "").strip():
            actions.append((key, DELEGATED))
            continue

        current = values.get(key)
        if not is_placeholder(current):
            actions.append((key, KEPT))
            continue

        value = generate_secret()
        replaced = False
        # Replace an existing empty/placeholder assignment where it stands, so
        # the operator's own ordering and comments survive.
        for index in range(len(new_lines) - 1, -1, -1):
            parsed = split_assignment(new_lines[index])
            if parsed is not None and parsed[0] == key:
                new_lines[index] = "{}={}".format(key, value)
                replaced = True
                break
        if not replaced:
            appended.append((key, purpose, value))
        actions.append((key, GENERATED))

    if appended:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(
            "# --- generated by scripts/bootstrap_secrets.py -----------------------"
        )
        new_lines.append(
            "# Random per-installation values. Keep them; rotating one invalidates"
        )
        new_lines.append(
            "# in-flight step tokens and disconnects runner agents until they match."
        )
        for key, purpose, value in appended:
            new_lines.append("# {}".format(purpose))
            new_lines.append("{}={}".format(key, value))

    return new_lines, actions


# ------------------------------------------------------------------ locking ---

class LockTimeout(RuntimeError):
    pass


class FileLock(object):
    """A cooperative lock beside the target file.

    ``O_CREAT | O_EXCL`` is atomic on every platform this runs on, which is the
    whole mechanism. A lock older than LOCK_STALE_SECONDS is broken rather than
    waited on, because the alternative is a killed process wedging the next
    person's setup forever.
    """

    def __init__(self, target, timeout=LOCK_TIMEOUT_SECONDS):
        self.path = target.parent / (target.name + LOCK_SUFFIX)
        self.timeout = timeout
        self._fd = None

    def __enter__(self):
        deadline = time.time() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
                os.write(self._fd, str(os.getpid()).encode("ascii"))
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if self._break_if_stale():
                    continue
                if time.time() >= deadline:
                    raise LockTimeout(
                        "another bootstrap_secrets run is holding {} (or it "
                        "crashed and left it behind). Delete that file if you "
                        "are sure nothing is running.".format(self.path)
                    )
                time.sleep(0.05)

    def _break_if_stale(self):
        try:
            age = time.time() - os.path.getmtime(str(self.path))
        except OSError:
            return True  # vanished under us: retry the create
        if age <= LOCK_STALE_SECONDS:
            return False
        try:
            os.unlink(str(self.path))
        except OSError:
            return False
        return True

    def __exit__(self, *_exc):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None
        try:
            os.unlink(str(self.path))
        except OSError:
            pass
        return False


def atomic_write(path, lines, newline):
    """Write via a sibling temp file + os.replace.

    A reader either sees the old file or the new one, never a truncated one -
    and the temp file is created 0600 so the secret is never briefly readable
    by everyone on a shared box.
    """
    tmp = path.parent / (path.name + ".tmp-{}".format(os.getpid()))
    body = newline.join(lines) + newline
    fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))
    _restrict_permissions(path)


def _restrict_permissions(path):
    """0600 where it means something; a silent no-op on Windows."""
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def seed_from_template(path, template):
    """Create .env from .env.example when it does not exist yet."""
    if template.exists():
        raw = template.read_text(encoding="utf-8", errors="replace")
        lines = raw.splitlines()
        newline = detect_newline(raw)
        origin = "created from {}".format(template.name)
    else:
        lines = [
            "# LazyAF environment. Created by scripts/bootstrap_secrets.py.",
            "# .env.example was not present, so this file holds only the",
            "# generated shared secrets. See QUICKSTART.md for the rest.",
        ]
        newline = os.linesep
        origin = "created (no .env.example to copy)"
    return lines, newline, origin


# --------------------------------------------------------------------- main ---

def describe(actions, ambient_env):
    """Human-readable result lines. Values are never among them."""
    out = []
    for key, action in actions:
        if action == GENERATED:
            out.append(
                "  generated  {} ({} bytes of entropy, URL-safe)".format(
                    key, SECRET_BYTES
                )
            )
        elif action == KEPT:
            out.append("  kept       {} (already set - not overwritten)".format(key))
        else:
            out.append(
                "  delegated  {} (you set {}_FILE; nothing written)".format(key, key)
            )
        if (ambient_env.get(key) or "").strip():
            out.append(
                "             note: {} is also exported in this shell, which "
                "overrides .env for docker compose".format(key)
            )
    return out


def run(env_file, template, check_only, ambient_env, stream):
    def say(text=""):
        print(text, file=stream)

    created = False
    if not env_file.exists():
        if check_only:
            say("MISSING {}".format(env_file))
            say("  Run: python scripts/bootstrap_secrets.py")
            return 1
        lines, newline, origin = seed_from_template(env_file, template)
        created = True
    else:
        lines, newline = load_lines(env_file)
        origin = None

    new_lines, actions = plan_and_apply(lines, ambient_env)
    missing = [key for key, action in actions if action == GENERATED]

    if check_only:
        say("Checking {}".format(env_file))
        for key, action in actions:
            if action == GENERATED:
                say("  MISSING    {} (unset, retired default, or placeholder)".format(key))
            elif action == KEPT:
                say("  ok         {}".format(key))
            else:
                say("  ok         {} (via {}_FILE)".format(key, key))
        if missing:
            say("")
            say("{} secret(s) need to be set. Fix with:".format(len(missing)))
            say("  python scripts/bootstrap_secrets.py")
            return 1
        say("")
        say("All shared secrets are set.")
        return 0

    if not missing and not created:
        say("{} already has every shared secret. Nothing to do.".format(env_file))
        for line in describe(actions, ambient_env):
            say(line)
        return 0

    try:
        with FileLock(env_file):
            # Re-read INSIDE the lock: a concurrent run may have written since
            # the read above, and its keys must survive ours.
            if env_file.exists():
                lines, newline = load_lines(env_file)
            new_lines, actions = plan_and_apply(lines, ambient_env)
            atomic_write(env_file, new_lines, newline)
    except LockTimeout as exc:
        say("Could not update {}: {}".format(env_file, exc))
        return 1
    except OSError as exc:
        say("Could not write {}: {}".format(env_file, exc))
        return 1

    if origin:
        say("{} {}".format(env_file, origin))
    say("Updated {}".format(env_file))
    for line in describe(actions, ambient_env):
        say(line)
    say("")
    say("No secret value was printed. Do not commit .env - it is gitignored.")
    if created:
        say("Now open it and add your API keys (ANTHROPIC_API_KEY / GEMINI_API_KEY).")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Generate LazyAF's shared auth secrets into .env.",
        epilog=(
            "Idempotent: existing values are never overwritten and no secret "
            "is ever printed."
        ),
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="path to the .env to update (default: the repo's .env)",
    )
    parser.add_argument(
        "--template",
        default=str(DEFAULT_TEMPLATE),
        help="template to seed a missing .env from (default: .env.example)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report which secrets are missing and change nothing (exit 1 if any)",
    )
    args = parser.parse_args(argv)

    return run(
        Path(args.env_file).resolve(),
        Path(args.template).resolve(),
        args.check,
        os.environ,
        sys.stdout,
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
