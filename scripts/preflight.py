#!/usr/bin/env python3
"""Check that this machine is ready to run LazyAF, before you start it.

    python scripts/preflight.py              # full check
    python scripts/preflight.py --offline    # skip every registry lookup
    python scripts/preflight.py --dev        # check the build-from-source stack

Every check prints one line and, when something is wrong, the exact command
that fixes it. Nothing here changes your machine: no pulls, no writes, no
containers. Standard library only, so it runs on a bare Python 3.9+ with the
repo freshly cloned and nothing installed.

SECRET HYGIENE: this script reads .env to see WHETHER a key is set and whether
its shape is plausible. It never prints a value, never logs one, and never
sends one anywhere. If you can see a key in this output, that is a bug.

Exit code 0 when the stack should start, 1 when something must be fixed
first. Warnings do not fail the run.
"""

import argparse
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_COMPOSE = "docker-compose.release.yml"
DEV_COMPOSE = "docker-compose.yml"

DEFAULT_IMAGE_PREFIX = "ghcr.io/brennan-vanderlaan/lazyaf"
DEFAULT_VERSION = "latest"

# Services published by the release workflow, in the order they matter.
SERVICE_IMAGES = ["backend", "frontend", "runner-agent"]

# Fallback only. The real list is scripts/build_images.py's IMAGES table,
# which _step_image_names() imports so the two can never drift.
FALLBACK_STEP_IMAGES = [
    "lazyaf-base",
    "lazyaf-debug-sidecar",
    "lazyaf-agent-base",
    "lazyaf-claude",
    "lazyaf-gemini",
    "lazyaf-test-runner",
]
STEP_TAG = "dev"

# The GHCR path already ends in /lazyaf, so a step image published there drops
# this prefix: lazyaf-base:dev is pulled as <prefix>/base. check_step_images()
# strips it to build the remote reference.
STEP_NAME_PREFIX = "lazyaf-"

# Free space we want available to docker. Images plus a couple of workspaces.
DISK_WARN_GB = 15
DISK_FAIL_GB = 5

OK, WARN, FAIL = "ok", "warn", "fail"
_MARK = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}

_results = []


def report(status, title, *details):
    """Print one check result plus indented, actionable detail lines."""
    _results.append(status)
    print("{} {}".format(_MARK[status], title))
    for line in details:
        for physical in str(line).splitlines():
            print("       " + physical)


def run(args, timeout=20):
    """Run a command, returning (returncode, stdout+stderr). Never raises."""
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        return proc.returncode, (proc.stdout or "").strip()
    except FileNotFoundError:
        return 127, "command not found: {}".format(args[0])
    except subprocess.TimeoutExpired:
        return 124, "timed out after {}s: {}".format(timeout, " ".join(args))
    except OSError as exc:  # permissions, exec format, ...
        return 126, "could not run {}: {}".format(args[0], exc)


# ---------------------------------------------------------------- docker ---

def check_docker():
    """Docker CLI present, daemon reachable, compose v2 available."""
    if shutil.which("docker") is None:
        report(
            FAIL,
            "Docker CLI not found",
            "LazyAF runs entirely in containers, so this is required.",
            "Install Docker Desktop (Windows/macOS) or Docker Engine (Linux):",
            "  https://docs.docker.com/get-docker/",
        )
        return False

    code, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    if code != 0:
        report(
            FAIL,
            "Docker daemon is not responding",
            "The CLI is installed but cannot reach the engine.",
            "Start Docker Desktop, or on Linux:  sudo systemctl start docker",
            "Docker said:",
            "  " + out.splitlines()[0] if out else "  (no output)",
        )
        return False
    report(OK, "Docker engine {} is running".format(out.splitlines()[-1]))

    code, out = run(["docker", "compose", "version", "--short"])
    if code != 0:
        report(
            FAIL,
            "`docker compose` (v2) is not available",
            "The old `docker-compose` script will not do: these compose files",
            "use v2 features (profiles, service_healthy conditions).",
            "Upgrade Docker Desktop, or install the compose plugin:",
            "  https://docs.docker.com/compose/install/",
        )
        return False
    report(OK, "docker compose v{} available".format(out.splitlines()[-1].lstrip("v")))
    return True


def check_disk():
    """Enough free space for the images and a workspace or two."""
    try:
        free_gb = shutil.disk_usage(str(REPO_ROOT)).free / (1024 ** 3)
    except OSError as exc:
        report(WARN, "Could not read free disk space", str(exc))
        return

    where = "on the drive holding this repo"
    note = (
        "On Windows and macOS, Docker Desktop stores images in its own VM disk, "
        "which may live elsewhere - check Docker Desktop > Settings > Resources "
        "if this passes but pulls still fail with 'no space left on device'."
    )
    if free_gb < DISK_FAIL_GB:
        report(
            FAIL,
            "Only {:.1f} GB free {}".format(free_gb, where),
            "The LazyAF images need roughly {} GB. Free some space first.".format(DISK_WARN_GB),
            note,
        )
    elif free_gb < DISK_WARN_GB:
        report(
            WARN,
            "{:.1f} GB free {}".format(free_gb, where),
            "That is enough to start, but {} GB is more comfortable once step".format(DISK_WARN_GB),
            "images and workspaces accumulate.",
            note,
        )
    else:
        report(OK, "{:.1f} GB free {}".format(free_gb, where))


# ------------------------------------------------------------------ env ----

def parse_env_file(path):
    """Return {KEY: value} from a .env file. Tolerant, and never printed."""
    values = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report(FAIL, "Could not read {}".format(path.name), str(exc))
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def looks_like_placeholder(value):
    """True for the obvious 'I did not fill this in' shapes."""
    lowered = value.lower()
    if not value:
        return True
    if re.search(r"x{4,}", lowered):
        return True
    return lowered in {
        "changeme",
        "your-key-here",
        "your_key_here",
        "todo",
        "none",
        "<your-key>",
    }


def describe_key(name, value, expected_prefix, provider):
    """Verdict for one API key. Reports SHAPE ONLY - never the value."""
    if not value:
        return WARN, "{} is not set - {} agents will not run".format(name, provider)
    if looks_like_placeholder(value):
        return FAIL, "{} still holds a placeholder, not a real key".format(name)
    if expected_prefix and not value.startswith(expected_prefix):
        return WARN, (
            "{} is set but does not start with '{}' - double-check you pasted "
            "the right value".format(name, expected_prefix)
        )
    if len(value) < 20:
        return WARN, "{} is set but looks too short to be a real key".format(name)
    return OK, "{} is set and has the expected shape".format(name)


def check_env():
    """.env exists, is ignored by git, and holds usable keys."""
    env_path = REPO_ROOT / ".env"
    example = REPO_ROOT / ".env.example"

    if not env_path.exists():
        report(
            FAIL,
            ".env not found",
            "One command creates it AND generates the shared auth secrets the",
            "backend refuses to start without:",
            "  python scripts/bootstrap_secrets.py",
            "Then open .env and paste in your API keys.",
            "(The stack starts without API keys, but no AI agent will run.)"
            if example.exists()
            else "",
        )
        return

    values = parse_env_file(env_path)
    report(OK, ".env found ({} variables set)".format(len(values)))

    # A committed .env is the one mistake that cannot be undone quietly.
    code, out = run(["git", "-C", str(REPO_ROOT), "check-ignore", "-q", ".env"], timeout=10)
    if code == 0:
        report(OK, ".env is gitignored - it will not be committed")
    elif code == 1:
        report(
            FAIL,
            ".env is NOT ignored by git",
            "Committing it would publish your API keys. Add '.env' to .gitignore",
            "before you run any git command in this repo.",
        )
    # code 127/128: not a git checkout, or no git. Not worth a line either way.

    anth = describe_key("ANTHROPIC_API_KEY", values.get("ANTHROPIC_API_KEY", ""), "sk-ant-", "Claude")
    gem = describe_key("GEMINI_API_KEY", values.get("GEMINI_API_KEY", ""), "", "Gemini")

    for status, message in (anth, gem):
        report(status, message)

    if anth[0] != OK and gem[0] != OK:
        report(
            WARN,
            "No usable AI provider key",
            "Repos, cards, pipelines, shell/docker steps and the git server all",
            "work without one. Agent steps and the playground will not.",
            "  ANTHROPIC_API_KEY -> https://console.anthropic.com/",
            "  GEMINI_API_KEY    -> https://aistudio.google.com/apikey",
        )

    check_shared_secrets(values)

    return values


# --------------------------------------------------------------- secrets ----

# The constants LazyAF shipped before 12.7. They are public - git history,
# image layers, every fork - so a .env still holding one is NOT configured.
RETIRED_PUBLIC_SECRETS = frozenset([
    "lazyaf-step-auth-secret-key-change-in-production",
    "lazyaf-runner-auth-secret-key-change-in-production",
])

# Kept in sync, by eye, with backend/app/config.py and
# scripts/bootstrap_secrets.py. Duplicated because preflight must run on a
# bare interpreter with nothing installed and no package on sys.path.
PLACEHOLDER_SECRETS = frozenset([
    "change-in-production", "change-me", "change_me", "changeme",
    "generate-me", "generateme", "none", "null", "placeholder",
    "replace-me", "replaceme", "secret", "tbd", "todo", "unset",
    "your-secret-here", "<generate>",
])

SHARED_SECRETS = [
    ("LAZYAF_STEP_AUTH_SECRET", "signs the JWTs step containers use for /api/steps/*"),
    ("LAZYAF_RUNNER_AUTH_SECRET", "enrols runner agents at /ws/runner"),
]

MIN_SECRET_LENGTH = 32


def secret_is_placeholder(value):
    """True when a value means 'not configured'. Shape only, never printed."""
    candidate = (value or "").strip()
    if not candidate:
        return True
    if candidate in RETIRED_PUBLIC_SECRETS:
        return True
    lowered = candidate.lower()
    if lowered in PLACEHOLDER_SECRETS:
        return True
    if set(lowered) == set("x"):
        return True
    return False


def check_shared_secrets(values):
    """The two secrets the backend now REFUSES to start without.

    This was a warning until 12.7, when the public defaults were removed. It is
    a FAIL now because it is no longer advice: the stack does not come up.
    """
    missing = []
    retired = []
    short = []

    for name, purpose in SHARED_SECRETS:
        file_var = name + "_FILE"
        # The _FILE form wins at resolution time, so it satisfies the check.
        # Whether the PATH resolves inside the container is not knowable from
        # here; the backend says so clearly at startup if it does not.
        pointer = (values.get(file_var) or os.environ.get(file_var) or "").strip()
        if pointer:
            report(OK, "{} supplied by {} ({})".format(name, file_var, pointer))
            continue

        value = (values.get(name) or os.environ.get(name) or "").strip()
        if value in RETIRED_PUBLIC_SECRETS:
            retired.append(name)
        elif secret_is_placeholder(value):
            missing.append(name)
        elif len(value) < MIN_SECRET_LENGTH:
            short.append(name)
        else:
            report(OK, "{} is set ({})".format(name, purpose))

    if retired:
        report(
            FAIL,
            "Retired PUBLIC default still in .env for: {}".format(", ".join(retired)),
            "That value shipped in LazyAF's source, so it is published - anyone",
            "can use it to mint credentials this backend would trust. The backend",
            "treats it as unset and will not start.",
            "Replace it with a generated value:",
            "  python scripts/bootstrap_secrets.py",
            "(it replaces the retired default and leaves everything else alone)",
        )
    if missing:
        report(
            FAIL,
            "Not set: {}".format(", ".join(missing)),
            "The backend REFUSES TO START without these. There is deliberately no",
            "default: a default compiled into the source is a published secret.",
            "Generate them - it is one command, it never overwrites anything you",
            "already set, and it prints no values:",
            "  python scripts/bootstrap_secrets.py",
            "Delivering secrets by mounted file instead? Set the _FILE form",
            "(LAZYAF_STEP_AUTH_SECRET_FILE=...) - it takes precedence.",
        )
    if short:
        report(
            WARN,
            "Under {} characters: {}".format(MIN_SECRET_LENGTH, ", ".join(short)),
            "Set, so the stack will start - but these mint credentials the",
            "backend trusts, and a short one is guessable. Prefer a generated",
            "value:  python scripts/bootstrap_secrets.py",
            "(it will NOT replace what you have; clear the line first if you",
            "want it regenerated)",
        )


# ---------------------------------------------------------------- ports ----

def port_owner_hint(port):
    """A best-effort hint about who holds a port. Never fails the check."""
    code, out = run(
        ["docker", "ps", "--filter", "publish={}".format(port),
         "--format", "{{.Names}} ({{.Image}})"],
        timeout=15,
    )
    if code == 0 and out:
        return out.splitlines()[0]
    return None


def port_in_use(port):
    """True when something already holds this port on localhost.

    Two probes, because neither alone is reliable. A CONNECT proves someone is
    listening and is the one that catches Docker's published ports on Windows,
    where a fresh bind to 127.0.0.1 can succeed over the engine's existing
    0.0.0.0 bind and report a false 'free'. A BIND catches the rest: a socket
    that is bound but not accepting, or bound on another interface.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    except OSError:
        pass
    finally:
        probe.close()

    binder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Deliberately no SO_REUSEADDR: it would let us bind a port someone
        # else already owns and report a false 'free'.
        binder.bind(("", port))
        return False
    except OSError:
        return True
    finally:
        binder.close()


def resolve_port(values, var, default, what):
    """Read a port from .env, reporting a bad value instead of crashing."""
    raw = (values.get(var) or "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError:
        report(
            FAIL,
            "{} is not a number".format(var),
            "Set it to a port number in .env, or remove it to use {}.".format(default),
        )
        return None
    if not 1 <= port <= 65535:
        report(
            FAIL,
            "{} is not a valid port ({})".format(var, port),
            "Ports run 1-65535. Remove it from .env to use {}.".format(default),
        )
        return None
    return port


def check_ports(values):
    """The published host ports are free (or already ours)."""
    ports = [
        (resolve_port(values, "LAZYAF_BACKEND_PORT", 8000, "backend API"),
         "backend API", "LAZYAF_BACKEND_PORT"),
        (resolve_port(values, "LAZYAF_FRONTEND_PORT", 5173, "web UI"),
         "web UI", "LAZYAF_FRONTEND_PORT"),
    ]
    for port, what, var in ports:
        if port is None:
            continue  # resolve_port already reported why
        if not port_in_use(port):
            report(OK, "Port {} is free (for the {})".format(port, what))
            continue
        owner = port_owner_hint(port)
        if owner:
            report(
                WARN,
                "Port {} is already used by a container: {}".format(port, owner),
                "If that is a LazyAF stack you already started, nothing to do.",
                "Otherwise stop it, or set {} in .env to a free port.".format(var),
            )
        else:
            report(
                FAIL,
                "Port {} is in use (needed for the {})".format(port, what),
                "Stop whatever is listening, or set {} in .env to a free port.".format(var),
            )


# --------------------------------------------------------------- images ----

def _step_image_names():
    """Image names from build_images.py's IMAGES table - the source of truth."""
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "_lazyaf_build_images", REPO_ROOT / "scripts" / "build_images.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return [name for _subdir, name, _parent, _extras in module.IMAGES]
    except Exception:
        # A stranger may have only the compose file and this script.
        return list(FALLBACK_STEP_IMAGES)


def image_present_locally(reference):
    code, _ = run(["docker", "image", "inspect", reference], timeout=20)
    return code == 0


def image_present_remotely(reference):
    """None when we could not tell (offline, private, old CLI)."""
    code, out = run(["docker", "manifest", "inspect", reference], timeout=45)
    if code == 0:
        return True
    lowered = out.lower()
    if "not found" in lowered or "manifest unknown" in lowered or "denied" in lowered:
        return False
    return None


def check_service_images(values, offline, dev_mode):
    """The three service images the release compose pulls."""
    if dev_mode:
        report(
            OK,
            "Service images: skipped (--dev builds them from source)",
            "docker compose build   then   docker compose up -d",
        )
        return

    prefix = values.get("LAZYAF_IMAGE_PREFIX") or DEFAULT_IMAGE_PREFIX
    version = values.get("LAZYAF_VERSION") or DEFAULT_VERSION
    missing = []
    unknown = []
    for service in SERVICE_IMAGES:
        reference = "{}/{}:{}".format(prefix.rstrip("/"), service, version)
        if image_present_locally(reference):
            report(OK, "Image present locally: {}".format(reference))
            continue
        if offline:
            unknown.append(reference)
            continue
        remote = image_present_remotely(reference)
        if remote is True:
            report(OK, "Image available to pull: {}".format(reference))
        elif remote is False:
            missing.append(reference)
        else:
            unknown.append(reference)

    if missing:
        report(
            FAIL,
            "{} release image(s) do not exist at that name/tag".format(len(missing)),
            *(["  " + m for m in missing]
              + [
                  "Check LAZYAF_VERSION in .env against the published tags:",
                  "  https://github.com/Brennan-VanderLaan/lazyaf/pkgs/container/lazyaf%2Fbackend",
                  "If no release has been published yet, build from source instead:",
                  "  docker compose build && docker compose up -d",
              ])
        )
    if unknown and offline:
        report(
            WARN,
            "{} image(s) not present locally, registry check skipped (--offline)".format(len(unknown)),
            *(["  " + u for u in unknown]
              + ["Re-run without --offline, or just:",
                 "  docker compose -f {} pull".format(RELEASE_COMPOSE)])
        )
    elif unknown:
        report(
            WARN,
            "Could not verify {} image(s) from here".format(len(unknown)),
            *(["  " + u for u in unknown]
              + [
                  "This is normal behind a proxy or on an older docker CLI.",
                  "The pull itself is the real answer:",
                  "  docker compose -f {} pull".format(RELEASE_COMPOSE),
              ])
        )


def check_step_images(values, offline, dev_mode):
    """The lazyaf-*:dev images agent and control steps run in.

    These are referenced by the LOCAL tag `lazyaf-<name>:dev` from inside the
    backend (pipeline_executor maps claude-code -> lazyaf-claude:dev), so a
    pulled copy has to be retagged to that name. Nothing pulls them for you:
    a missing one fails a step loudly rather than pulling behind your back.
    """
    names = _step_image_names()
    missing = [n for n in names if not image_present_locally("{}:{}".format(n, STEP_TAG))]

    if not missing:
        report(OK, "All {} step images present as :{}".format(len(names), STEP_TAG))
        return

    if dev_mode:
        report(
            WARN,
            "{} of {} step images missing".format(len(missing), len(names)),
            *(["  {}:{}".format(m, STEP_TAG) for m in missing]
              + ["Build them:", "  python scripts/build_images.py"])
        )
        return

    prefix = (values.get("LAZYAF_IMAGE_PREFIX") or DEFAULT_IMAGE_PREFIX).rstrip("/")
    version = values.get("LAZYAF_VERSION") or DEFAULT_VERSION
    commands = []
    for name in missing:
        # The GHCR path already ends in /lazyaf, so the published repository
        # drops the local "lazyaf-" prefix (see .github/scripts/step_images.py,
        # which is what actually pushes them): lazyaf-base:dev is published as
        # <prefix>/base. Building the remote ref from the LOCAL name yields
        # <prefix>/lazyaf-base, and every pull 404s. The local tag is what the
        # backend looks for, so only the remote half is stripped.
        remote = name[len(STEP_NAME_PREFIX):] if name.startswith(STEP_NAME_PREFIX) else name
        commands.append("  docker pull {p}/{r}:{v}".format(p=prefix, r=remote, v=version))
        commands.append("  docker tag {p}/{r}:{v} {n}:{t}".format(p=prefix, r=remote, n=name, v=version, t=STEP_TAG))
    report(
        WARN,
        "{} of {} step images missing".format(len(missing), len(names)),
        "Pipeline steps and AI agent cards need these. The backend does NOT",
        "pull them for you - a missing one fails the step with a clear message.",
        "Pull and retag them to the local :{} name the backend looks for:".format(STEP_TAG),
        *commands
    )
    if offline:
        report(WARN, "(--offline: no registry lookup was attempted for the step images)")


# ----------------------------------------------------------------- main ----

def check_compose_file(dev_mode):
    wanted = DEV_COMPOSE if dev_mode else RELEASE_COMPOSE
    path = REPO_ROOT / wanted
    if path.exists():
        report(OK, "{} found".format(wanted))
        return True
    report(
        FAIL,
        "{} not found in {}".format(wanted, REPO_ROOT),
        "Run this script from a LazyAF checkout:",
        "  git clone https://github.com/Brennan-VanderLaan/lazyaf.git",
        "  cd lazyaf && python scripts/preflight.py",
    )
    return False


def main():
    parser = argparse.ArgumentParser(
        description="Check this machine is ready to run LazyAF.",
        epilog="Nothing is modified. Exit 0 = good to go, 1 = fix something first.",
    )
    parser.add_argument(
        "--offline", action="store_true",
        help="skip every registry lookup (no network calls)",
    )
    parser.add_argument(
        "--dev", action="store_true",
        help="check the build-from-source stack (docker-compose.yml) instead",
    )
    args = parser.parse_args()

    print("LazyAF preflight")
    print("  repo: {}".format(REPO_ROOT))
    print("  mode: {}".format("dev (build from source)" if args.dev else "release (pull images)"))
    print()

    check_compose_file(args.dev)

    if check_docker():
        check_disk()
        values = check_env() or {}
        check_ports(values)
        check_service_images(values, args.offline, args.dev)
        check_step_images(values, args.offline, args.dev)
    else:
        # Every remaining check needs a working docker; stop rather than
        # print a wall of consequential failures.
        check_env()

    fails = _results.count(FAIL)
    warns = _results.count(WARN)
    print()
    if fails:
        print("NOT READY: {} problem(s) to fix, {} warning(s).".format(fails, warns))
        print("Each [FAIL] above says what to do.")
        return 1
    if warns:
        print("READY, with {} warning(s) - read them, then:".format(warns))
    else:
        print("READY. Start the stack with:")
    if args.dev:
        print("  docker compose up -d --build")
    else:
        print("  docker compose -f {} pull".format(RELEASE_COMPOSE))
        print("  docker compose -f {} up -d".format(RELEASE_COMPOSE))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - a preflight must never traceback
        print("\npreflight hit an unexpected problem and stopped:")
        print("  {}: {}".format(type(exc).__name__, exc))
        print("This is a bug in scripts/preflight.py, not a problem with your setup.")
        print("Please report it: https://github.com/Brennan-VanderLaan/lazyaf/issues")
        sys.exit(1)
