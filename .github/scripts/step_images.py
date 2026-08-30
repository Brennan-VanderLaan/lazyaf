#!/usr/bin/env python3
"""Print the step-image list, read from scripts/build_images.py.

    python3 .github/scripts/step_images.py --refs
        lazyaf-base:dev lazyaf-debug-sidecar:dev lazyaf-agent-base:dev ...

    python3 .github/scripts/step_images.py --pairs
        lazyaf-base:dev=base
        lazyaf-debug-sidecar:dev=debug-sidecar
        ...

WHY THIS EXISTS
    The release workflows have to scan and publish every step image, and the
    authoritative list of step images is the IMAGES table in
    scripts/build_images.py - nowhere else. Hardcoding that list in
    .github/workflows/*.yml would mean every future image silently goes
    unpublished and, worse, unscanned, with a green build to say everything
    is fine. (The debug sidecar landed in that table while this CI was being
    written, which is exactly the drift being designed out.)

    So the workflows ask this script, and this script asks build_images.py.
    An image added to IMAGES is published and leak-scanned automatically.

PUBLISHED NAME
    The `lazyaf-` prefix is stripped, because the GHCR path already carries
    it: ghcr.io/<owner>/lazyaf/base, matching the ghcr.io/<owner>/lazyaf/
    backend | frontend | runner-agent naming that docker-compose.release.yml
    pulls. The local tag is untouched - it stays `lazyaf-base:dev`, which is
    the name the backend resolves and the one build_images.py stamps.

    Importing build_images.py is safe and cheap here: it pulls in the docker
    SDK lazily, inside its functions, so reading the table needs no daemon
    and no dependencies.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILD_IMAGES = REPO_ROOT / "scripts" / "build_images.py"
NAME_PREFIX = "lazyaf-"


def load_images():
    spec = importlib.util.spec_from_file_location("_lazyaf_build_images", BUILD_IMAGES)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {BUILD_IMAGES}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tag = getattr(module, "TAG", "dev")
    entries = []
    for row in module.IMAGES:
        # (subdir, image name, parent subdir, extra_context). Only the first
        # two are read here; unpack defensively so a future column added to
        # the table does not break the release pipeline.
        name = row[1]
        published = name[len(NAME_PREFIX):] if name.startswith(NAME_PREFIX) else name
        entries.append((f"{name}:{tag}", published))
    return entries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--refs", action="store_true", help="space-separated local image refs")
    group.add_argument("--pairs", action="store_true", help="one 'localref=publishedname' per line")
    args = parser.parse_args()

    try:
        entries = load_images()
    except Exception as exc:  # noqa: BLE001 - the message is the whole point
        print(f"could not read the IMAGES table from {BUILD_IMAGES}: {exc}", file=sys.stderr)
        return 2

    if not entries:
        print("the IMAGES table is empty - refusing to publish nothing", file=sys.stderr)
        return 2

    if args.refs:
        print(" ".join(ref for ref, _ in entries))
    else:
        for ref, published in entries:
            print(f"{ref}={published}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
