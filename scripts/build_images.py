#!/usr/bin/env python3
"""
Build the LazyAF step images (Phase 12.3).

Usage:
    python scripts/build_images.py            # build stale/missing images
    python scripts/build_images.py --force    # rebuild everything
    python scripts/build_images.py --check    # exit 1 listing stale/missing,
                                              # build nothing

Tag scheme: lazyaf-base:dev, lazyaf-claude:dev, lazyaf-test-runner:dev.
`:dev` is the moving local tag every reference uses — NO `:latest` anywhere
(grep-able rule). Each build stamps `LABEL lazyaf.content-hash=<sha256[:12]>`
of the image directory tree (child hashes chain the parent's hash so a base
change makes children stale); a build is SKIPPED when the local `:dev` image
already carries the computed hash.

Step images are deliberately NOT compose services and are never auto-built by
the backend: LocalExecutor fails a step loudly with "Image not found:
lazyaf-base:dev" — that message plus `--check` is the rebuild trigger story.

Pure docker SDK (mirrors workspace/population.py's client handling) — no
shelling out, Windows-host friendly.
"""
import argparse
import hashlib
import sys
from pathlib import Path

# Docker build output contains non-cp1252 characters; never let a Windows
# console encoding crash the build script.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGES_ROOT = REPO_ROOT / "images"

TAG = "dev"
HASH_LABEL = "lazyaf.content-hash"

# (directory under images/, image name) in dependency order: base first.
IMAGES = [
    ("base", "lazyaf-base"),
    ("claude", "lazyaf-claude"),
    ("test-runner", "lazyaf-test-runner"),
]


def tree_hash(directory: Path, extra: str = "") -> str:
    """sha256[:12] over the directory tree (sorted relative paths + bytes).

    `extra` folds a parent image's hash into a child's, so children go stale
    when the base changes even if their own directory did not.
    """
    h = hashlib.sha256()
    h.update(extra.encode())
    for path in sorted(directory.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        h.update(str(path.relative_to(directory)).replace("\\", "/").encode())
        h.update(b"\0")
        # Normalize line endings so a CRLF checkout hashes like LF
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()[:12]


def local_hash(client, image_ref: str):
    """Return the content-hash label of the local image, or None if absent."""
    import docker

    try:
        return client.images.get(image_ref).labels.get(HASH_LABEL)
    except docker.errors.ImageNotFound:
        return None


def build_image(client, directory: Path, image_ref: str, content_hash: str) -> None:
    """Build one image, streaming build output; raise on failure."""
    from docker.errors import BuildError

    print(f"[build] {image_ref}  (hash {content_hash})  <- {directory}")
    try:
        _, logs = client.images.build(
            path=str(directory),
            tag=image_ref,
            buildargs={"CONTENT_HASH": content_hash},
            rm=True,
        )
        for chunk in logs:
            line = chunk.get("stream")
            if line and line.strip():
                print(f"    {line.rstrip()}")
    except BuildError as e:
        for chunk in e.build_log:
            line = chunk.get("stream") or chunk.get("error")
            if line and str(line).strip():
                print(f"    {str(line).rstrip()}", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="rebuild even if fresh")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit nonzero listing missing/stale images without building",
    )
    args = parser.parse_args()

    try:
        import docker
    except ImportError:
        print("docker SDK not installed (pip install docker)", file=sys.stderr)
        return 2

    try:
        client = docker.from_env()
        client.ping()
    except Exception as e:
        print(f"Docker daemon not reachable: {e}", file=sys.stderr)
        return 2

    parent_hash = ""
    stale = []
    built = 0
    for subdir, name in IMAGES:
        directory = IMAGES_ROOT / subdir
        image_ref = f"{name}:{TAG}"
        # Children chain the base hash; base itself chains nothing.
        content_hash = tree_hash(directory, extra=parent_hash if subdir != "base" else "")
        if subdir == "base":
            parent_hash = content_hash

        current = local_hash(client, image_ref)
        if current == content_hash and not args.force:
            print(f"[fresh] {image_ref}  (hash {content_hash})")
            continue

        state = "missing" if current is None else f"stale (has {current})"
        if args.check:
            stale.append(f"{image_ref}: {state}, want {content_hash}")
            continue

        print(f"[stale] {image_ref}: {state}")
        build_image(client, directory, image_ref, content_hash)
        built += 1

    if args.check:
        if stale:
            print("Missing/stale step images (run: python scripts/build_images.py):")
            for line in stale:
                print(f"  {line}")
            return 1
        print("All step images fresh.")
        return 0

    print(f"[done] {built} image(s) built.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
