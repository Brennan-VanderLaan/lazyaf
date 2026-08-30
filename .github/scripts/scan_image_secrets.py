#!/usr/bin/env python3
"""Fail if a built LazyAF image carries a credential.

    python3 .github/scripts/scan_image_secrets.py IMAGE [IMAGE ...]

Exit 0 = clean, exit 1 = something in the image looks like a secret, exit 2 =
the scanner itself could not run (docker missing, image absent).

WHY THIS EXISTS
    LazyAF's images are published to a public registry. None of them need a
    credential in order to BUILD - the release workflows deliberately run the
    docker builds with no secrets in scope at all - so anything credential
    shaped inside a built image is a bug, not a configuration choice. This
    script is the mechanical proof of that claim, run on every PR and again
    immediately before anything is pushed to GHCR.

FOUR CHECKS, in increasing order of cost:

  1. Image config env. A variable whose NAME says credential
     (ANTHROPIC_API_KEY, *_TOKEN, *_SECRET, ...) must not carry a value.
     This is the shape-INDEPENDENT check: it catches a leaked key from a
     provider whose token format nobody has modelled yet, which is precisely
     the case a regex gate would sail past. `docker build --build-arg
     ANTHROPIC_API_KEY=...` plus an `ENV` line is the realistic way this
     would happen, and it is caught here.

  2. Image config labels + build history. `docker history` records every
     build argument value that was interpolated into a RUN line, which is
     the classic "I passed the key as a --build-arg and it is in the image
     metadata forever" leak. Scanned with the format patterns.

  3. Filenames. Any `.env` (or `.env.local`, `.env.production`, ...)
     ANYWHERE in the filesystem fails. `.env.example` / `.env.sample` /
     `.env.template` are allowed: they are placeholder documentation, and
     check 4 reads their contents anyway.

  4. File contents. Every text file under a path the LazyAF build actually
     creates is matched against the live-key format patterns.

     The content scan deliberately SKIPS third-party dependency trees
     (node_modules, site-packages, /usr/lib, apt state). Not because a
     secret could not hide there, but because those trees are full of
     documentation ABOUT credentials. Measured, not assumed: with
     --include-vendor, lazyaf-claude:dev reports three findings, and all
     three are npm's own docs printing the literal string
     "-----BEGIN ... PRIVATE KEY-----" in a config example
     (usr/lib/node_modules/npm/docs/..., man7/config.7,
     @npmcli/config/lib/definitions/definitions.js). A gate that cries wolf
     is a gate somebody disables, and this one has to stay blocking.

     Checks 1-3 still cover those trees COMPLETELY: a `.env` in
     node_modules fails, and a key injected via env or build arg is caught
     wherever it landed. Pass --include-vendor for a deliberate deep audit.

WHAT IT PRINTS ON FAILURE
     Enough to find the leak, never the whole secret - this output lands in
     a public CI log. See secret_patterns.redact.
"""

import argparse
import json
import os
import subprocess
import sys
import tarfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_patterns import (  # noqa: E402
    BENIGN_ENV_VALUES,
    SECRET_ENV_NAME,
    find_secrets,
    redact,
)

# Content-scan budget. Files larger than this are skipped: an API key lives
# in a config file or a shell profile, not in a 40 MB bundle, and reading
# every large blob in a 1 GB image would make this gate too slow to run on
# every PR. The frontend's built JS bundle is comfortably under this, and it
# is the one large file we genuinely want read.
MAX_FILE_BYTES = 8 * 1024 * 1024

# Path prefixes (container-absolute, no leading slash - that is how tar
# members are named) excluded from the CONTENT scan only. See module docstring.
VENDOR_PREFIXES = (
    "usr/lib/",
    "usr/share/doc/",
    "usr/share/man/",
    "usr/share/locale/",
    "usr/share/zoneinfo/",
    "usr/src/",
    "var/lib/apt/",
    "var/lib/dpkg/",
    "var/cache/",
    "proc/",
    "sys/",
    "dev/",
)

# Any path component equal to one of these excludes the file from the
# CONTENT scan (again: never from the .env filename check).
VENDOR_COMPONENTS = frozenset(
    {"node_modules", "site-packages", "dist-packages", "__pycache__", ".git"}
)

# Filenames that are allowed to exist despite starting with `.env`, because
# they are placeholders by convention. Their CONTENTS are still scanned.
ENV_FILE_ALLOWED = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist"}
)


def docker(*args, binary=False):
    """Run a docker command, returning stdout. Raises on failure."""
    result = subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def check_config(image, findings):
    """Checks 1 and 2: env vars, labels and build history."""
    raw = docker("image", "inspect", image)
    config = json.loads(raw)[0].get("Config") or {}

    for entry in config.get("Env") or []:
        name, _, value = entry.partition("=")
        if SECRET_ENV_NAME.search(name) and value.strip().lower() not in BENIGN_ENV_VALUES:
            findings.append(
                f"[env] {image}: ENV {name} is baked with a non-empty value "
                f"({redact(value)}). Images must ship with credential "
                f"variables UNSET; they are supplied at run time."
            )
        for label, hit in find_secrets(entry):
            findings.append(f"[env] {image}: ENV {name} matches {label} format: {redact(hit)}")

    for key, value in (config.get("Labels") or {}).items():
        for label, hit in find_secrets(f"{key}={value}"):
            findings.append(f"[label] {image}: LABEL {key} matches {label} format: {redact(hit)}")

    history = docker("history", "--no-trunc", "--format", "{{.CreatedBy}}", image)
    for line_no, line in enumerate(history.splitlines(), 1):
        for label, hit in find_secrets(line):
            findings.append(
                f"[history] {image}: build step #{line_no} matches {label} "
                f"format: {redact(hit)}. A --build-arg value is recorded in "
                f"image history forever - rotate the key and use a secret "
                f"mount or run-time env instead."
            )


def is_vendor(name):
    parts = name.split("/")
    if any(part in VENDOR_COMPONENTS for part in parts):
        return True
    return name.startswith(VENDOR_PREFIXES)


def check_filesystem(image, findings, include_vendor):
    """Checks 3 and 4: stream the flattened filesystem out of the image."""
    container = docker("create", "--entrypoint", "/nonexistent-scan-entrypoint", image).strip()
    if not container:
        raise RuntimeError(f"docker create returned no id for {image}")
    scanned = 0
    try:
        proc = subprocess.Popen(
            ["docker", "export", container],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        # Streaming mode ("r|"): members must be consumed in order and the
        # whole export is never buffered to disk.
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                name = member.name.lstrip("./")
                base = name.rsplit("/", 1)[-1]

                # Check 3 - filename, applied to the WHOLE image.
                if (base == ".env" or base.startswith(".env.")) and base not in ENV_FILE_ALLOWED:
                    findings.append(
                        f"[dotenv] {image}: contains /{name}. A .env file must "
                        f"never be baked into a published image; check the "
                        f"build context and its .dockerignore."
                    )

                # Check 4 - contents.
                if not member.isfile() or member.size == 0 or member.size > MAX_FILE_BYTES:
                    continue
                if not include_vendor and is_vendor(name):
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    continue
                blob = handle.read()
                if b"\0" in blob[:8192]:  # binary; regexes would be noise
                    continue
                scanned += 1
                for label, hit in find_secrets(blob.decode("utf-8", "replace")):
                    findings.append(
                        f"[content] {image}: /{name} matches {label} format: {redact(hit)}"
                    )
        proc.stdout.close()
        if proc.wait() != 0:
            raise RuntimeError(f"docker export failed for {image}")
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return scanned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("images", nargs="+", help="image refs to scan")
    parser.add_argument(
        "--include-vendor",
        action="store_true",
        help="also content-scan node_modules/site-packages/usr-lib (slow, noisy)",
    )
    args = parser.parse_args()

    findings = []
    for image in args.images:
        print(f"::group::secret scan {image}", flush=True)
        try:
            check_config(image, findings)
            scanned = check_filesystem(image, findings, args.include_vendor)
        except subprocess.CalledProcessError as exc:
            print(exc.stderr.decode("utf-8", "replace"), file=sys.stderr)
            print(f"scanner could not inspect {image}", file=sys.stderr)
            return 2
        print(f"  content-scanned {scanned} text files", flush=True)
        print("::endgroup::", flush=True)

    if findings:
        print("\nSECRET SCAN FAILED - do not publish these images.\n", file=sys.stderr)
        for line in findings:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nIf a finding is a deliberately fake test sentinel, add its EXACT "
            "value to ALLOWLIST in .github/scripts/secret_patterns.py with a "
            "comment naming the test that owns it. If it is real: rotate it "
            "first, then fix the build.",
            file=sys.stderr,
        )
        return 1

    print(f"\nOK: {len(args.images)} image(s) carry no credentials.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
