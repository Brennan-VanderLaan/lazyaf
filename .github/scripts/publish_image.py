#!/usr/bin/env python3
"""Tag one locally-built LazyAF image for GHCR and push it.

    python3 .github/scripts/publish_image.py \
        --local lazyaf-base:dev --repo ghcr.io/brennan-vanderlaan/lazyaf/base
    python3 .github/scripts/publish_image.py ... --dry-run   # print, push nothing

WHY A SCRIPT INSTEAD OF SIX LINES OF BASH IN THE WORKFLOW
    Two jobs (step images and service images) have to derive the SAME tag
    set from the same ref, and images.yml would otherwise carry two copies of
    that logic which would drift the first time somebody changed one. It is
    also the only part of the release pipeline whose behaviour can be
    exercised on a laptop: `--dry-run` prints exactly what would be pushed,
    which is how the tag policy below was checked without a registry.

TAG POLICY
    Every push carries, at minimum, an IMMUTABLE identifier:

      sha-<7>            the exact commit. Never reused, never moved. This is
                         the tag to quote in a bug report.
      content-<12>       step images only, read from the image's own
                         `lazyaf.content-hash` label. Two builds of identical
                         source produce the same value, so this says what is
                         IN the image rather than which commit produced it.

    On a version tag `vX.Y.Z`:

      vX.Y.Z, X.Y.Z      both spellings, because half the world writes the
                         'v' and half does not.
      latest             ONLY for a stable release (no `-rc1`/`-beta`
                         suffix). It is a moving pointer to the newest
                         stable release and nothing else.

    On main:

      main, edge         the tip of the default branch. Explicitly NOT
                         `latest`: someone who typed `latest` asked for a
                         release, not for whatever landed an hour ago.

    ABOUT `latest`, since scripts/build_images.py says "NO :latest anywhere":
    that rule governs LOCAL step image tags, where a moving tag makes a step
    silently run yesterday's image and makes the staleness check meaningless.
    Those images are still `lazyaf-<name>:dev` locally and always will be.
    `latest` here is a REGISTRY tag on published release artifacts, which is
    the one place the convention is worth honouring - it is what a stranger
    types, and it is what docker-compose.release.yml defaults to.
"""

import argparse
import json
import os
import re
import subprocess
import sys

SEMVER_TAG = re.compile(r"^v(\d+\.\d+\.\d+)(-[0-9A-Za-z.-]+)?$")
# Docker's own tag grammar. Anything outside it has to be rewritten or the
# push fails with a confusing error deep inside the daemon.
TAG_SAFE = re.compile(r"[^A-Za-z0-9._-]")

HASH_LABEL = "lazyaf.content-hash"


def run(args, capture=False):
    if capture:
        return subprocess.run(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
        ).stdout.decode("utf-8", "replace")
    subprocess.run(args, check=True)
    return ""


def sanitize(value):
    cleaned = TAG_SAFE.sub("-", value).strip("-.")
    return cleaned[:128] or "untagged"


def compute_tags(ref_type, ref_name, sha, extra_tags):
    """Derive the published tag list. Pure function - see TAG POLICY above."""
    tags = []
    reasons = {}

    if sha:
        short = f"sha-{sha[:7]}"
        tags.append(short)
        reasons[short] = "immutable commit identifier"

    if ref_type == "tag":
        safe = sanitize(ref_name)
        tags.append(safe)
        reasons[safe] = "the git tag this release was cut from"
        match = SEMVER_TAG.match(ref_name)
        if match:
            bare = match.group(1) + (match.group(2) or "")
            tags.append(bare)
            reasons[bare] = "same version without the leading 'v'"
            if not match.group(2):
                tags.append("latest")
                reasons["latest"] = "newest STABLE release (prereleases excluded)"
    elif ref_type == "branch" and ref_name == "main":
        tags.extend(["main", "edge"])
        reasons["main"] = "tip of the default branch"
        reasons["edge"] = "alias for main, for people who expect it"
    elif ref_name:
        safe = sanitize(ref_name)
        tags.append(safe)
        reasons[safe] = f"non-default ref ({ref_type})"

    for extra in extra_tags:
        safe = sanitize(extra)
        tags.append(safe)
        reasons[safe] = "requested via workflow input"

    # Preserve order, drop duplicates.
    seen = set()
    ordered = [t for t in tags if not (t in seen or seen.add(t))]
    return ordered, reasons


def content_hash_tag(local):
    """The step images' provenance tag, read off the image's own label."""
    try:
        raw = run(["docker", "image", "inspect", local], capture=True)
    except subprocess.CalledProcessError:
        return None
    labels = (json.loads(raw)[0].get("Config") or {}).get("Labels") or {}
    value = labels.get(HASH_LABEL)
    # 'dev' is build_images.py's default when CONTENT_HASH was not passed -
    # a real build always overrides it, so seeing it means something built
    # this image by hand and the tag would be a lie.
    if not value or value == "dev":
        return None
    return f"content-{sanitize(value)}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", required=True, help="locally built image ref")
    parser.add_argument("--repo", required=True, help="target repo, e.g. ghcr.io/owner/lazyaf/base")
    parser.add_argument("--extra-tag", action="append", default=[], help="additional tag")
    parser.add_argument("--dry-run", action="store_true", help="print, push nothing")
    args = parser.parse_args()

    # GHCR rejects uppercase path segments, and github.repository_owner is
    # mixed case ("Brennan-VanderLaan"). Normalising here means the workflow
    # does not have to remember to pipe it through `tr`.
    repo = args.repo.lower()
    if repo != args.repo:
        print(f"note: lowercased target repo to {repo} (registries require it)")

    # GITHUB_REF_TYPE ('branch'|'tag'), GITHUB_REF_NAME and GITHUB_SHA are set
    # automatically by the Actions runner - the workflow does not pass them,
    # and does not need to. Locally they are how you drive --dry-run.
    tags, reasons = compute_tags(
        os.environ.get("GITHUB_REF_TYPE", ""),
        os.environ.get("GITHUB_REF_NAME", ""),
        os.environ.get("GITHUB_SHA", ""),
        [t for t in args.extra_tag if t],
    )
    hash_tag = content_hash_tag(args.local)
    if hash_tag:
        tags.append(hash_tag)
        reasons[hash_tag] = f"content hash from the {HASH_LABEL} label"

    if not tags:
        print("refusing to push with no tags: no ref and no --extra-tag", file=sys.stderr)
        return 2

    print(f"{args.local}  ->  {repo}")
    for tag in tags:
        print(f"    :{tag:<24} {reasons.get(tag, '')}")

    if args.dry_run:
        print("\n--dry-run: nothing tagged, nothing pushed.")
        return 0

    for tag in tags:
        target = f"{repo}:{tag}"
        run(["docker", "tag", args.local, target])
        run(["docker", "push", target])

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"**`{repo}`** &larr; `{args.local}`\n\n")
            for tag in tags:
                handle.write(f"- `{tag}` &mdash; {reasons.get(tag, '')}\n")
            handle.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
