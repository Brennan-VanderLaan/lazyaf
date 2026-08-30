#!/usr/bin/env python3
"""Fail if the LazyAF source tree contains a live-format credential.

    python3 .github/scripts/scan_repo_secrets.py            # working tree
    python3 .github/scripts/scan_repo_secrets.py --history 300

Exit 0 = clean, exit 1 = a finding, exit 2 = the scanner could not run.

WHY THIS EXISTS
    github.com/Brennan-VanderLaan/lazyaf is public. Everything tracked in it
    is readable by strangers, permanently, including by anyone who cloned it
    before a mistake was noticed. This gate runs before every publish so a
    key can never ride along with a release.

WHAT IT CHECKS

  1. `.env` is still ignored. The single most likely way a key gets
     committed here is someone loosening .gitignore. Checked with `git
     check-ignore`, so it holds no matter how the rule is expressed.

  2. No `.env` file is tracked. Belt and braces for check 1, and it also
     catches an `--force`-added file that .gitignore would otherwise have
     covered. `.env.example` is expected and allowed - it is the template a
     new user copies - and its contents are scanned like any other file.

  3. No tracked file contains a live-format key. See
     .github/scripts/secret_patterns.py for the formats and for the
     exact-value allowlist that lets the test suite's deliberately fake
     sentinels through.

  4. With --history N, the patches of the last N commits are scanned too.
     This is NOT on by default: the working tree is what gets published, and
     a full history scan needs an unshallow clone, so it is a deliberate
     on-demand audit (the secret-scan workflow exposes it as a
     workflow_dispatch input) rather than a tax on every PR.

A NOTE ON SCOPE
    This is a leak gate, not a security scanner. It answers exactly one
    question - "is there a credential in here" - and it answers it with a
    near-zero false-positive rate so that it can stay blocking. It does not
    replace GitHub's own push protection; turn that on as well.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_patterns import find_secrets, redact  # noqa: E402

# Tracked filenames that may start with `.env`. Everything else beginning
# with `.env` is treated as a real environment file and fails.
ENV_FILE_ALLOWED = frozenset(
    {".env.example", ".env.sample", ".env.template", ".env.dist"}
)

# Paths that must be ignored by git, checked with `git check-ignore`.
MUST_BE_IGNORED = (".env", ".env.local")

# Files above this size are skipped. Nothing legitimate in this repo is
# bigger, and the only tracked files that approach it are documentation.
MAX_FILE_BYTES = 4 * 1024 * 1024


def git(*args):
    result = subprocess.run(
        ["git", *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
    )
    return result.stdout.decode("utf-8", "replace")


def check_gitignore(findings):
    for path in MUST_BE_IGNORED:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode != 0:
            findings.append(
                f"[gitignore] `{path}` is NOT ignored by git. Restore the "
                f"`.env` rules in .gitignore before merging - without them a "
                f"routine `git add -A` commits your API keys."
            )


def check_tracked_env_files(tracked, findings):
    for path in tracked:
        base = path.rsplit("/", 1)[-1]
        if (base == ".env" or base.startswith(".env.")) and base not in ENV_FILE_ALLOWED:
            findings.append(
                f"[dotenv] `{path}` is tracked by git. Environment files hold "
                f"credentials and must never be committed; only `.env.example` "
                f"style placeholders belong in the repo."
            )


def check_tracked_contents(tracked, findings):
    scanned = 0
    for path in tracked:
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, "rb") as handle:
                blob = handle.read()
        except (OSError, ValueError):
            # Submodule entries, symlinks to nowhere, files removed in the
            # working tree: nothing to scan, and not this gate's business.
            continue
        if b"\0" in blob[:8192]:
            continue
        scanned += 1
        text = blob.decode("utf-8", "replace")
        for line_no, line in enumerate(text.splitlines(), 1):
            for label, hit in find_secrets(line):
                findings.append(
                    f"[content] {path}:{line_no} matches {label} key format: {redact(hit)}"
                )
    return scanned


def check_history(depth, findings):
    try:
        patch = git("log", f"-{depth}", "-p", "--no-color", "--no-textconv")
    except subprocess.CalledProcessError:
        findings.append(
            "[history] could not read git history. A shallow clone cannot be "
            "history-scanned; the workflow must check out with fetch-depth: 0."
        )
        return
    commit = "(unknown)"
    for line in patch.splitlines():
        if line.startswith("commit "):
            commit = line.split()[1][:12]
        elif line.startswith("+"):
            for label, hit in find_secrets(line):
                findings.append(
                    f"[history] commit {commit} added a {label}-format key: {redact(hit)}. "
                    f"Removing it from HEAD is NOT enough - rotate the key, "
                    f"then decide about rewriting history."
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--history",
        type=int,
        default=0,
        metavar="N",
        help="also scan the patches of the last N commits (needs fetch-depth: 0)",
    )
    args = parser.parse_args()

    try:
        tracked = [p for p in git("ls-files", "-z").split("\0") if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("not a git checkout, or git is unavailable", file=sys.stderr)
        return 2

    findings = []
    check_gitignore(findings)
    check_tracked_env_files(tracked, findings)
    scanned = check_tracked_contents(tracked, findings)
    if args.history:
        check_history(args.history, findings)

    print(f"scanned {scanned} of {len(tracked)} tracked files", end="")
    print(f" + the last {args.history} commits" if args.history else "")

    if findings:
        print("\nSECRET SCAN FAILED.\n", file=sys.stderr)
        for line in findings:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nIf a finding is a deliberately fake test sentinel, add its EXACT "
            "value to ALLOWLIST in .github/scripts/secret_patterns.py with a "
            "comment naming the test that owns it. If it is a real credential, "
            "ROTATE IT NOW - it is already public - and only then clean the tree.",
            file=sys.stderr,
        )
        return 1

    print("OK: no credential-shaped strings outside the allowlisted test sentinels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
