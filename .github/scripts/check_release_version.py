#!/usr/bin/env python3
"""Refuse to publish a wheel whose version disagrees with the git tag.

    python3 .github/scripts/check_release_version.py --tag v0.1.0 --dist dist

Exit 0 = they agree, 1 = they do not, 2 = nothing to check.

WHY
    `pip install lazyaf-cli==0.1.0` has to give you the code that release
    v0.1.0 was cut from. Nothing enforces that on its own: the tag lives in
    git, the version lives in cli/pyproject.toml, and the two drift the first
    time someone tags a release without bumping the file. The result is two
    different artifacts claiming the same version - unfixable after the fact,
    because a version on PyPI can never be reused.

    So the release workflow asks this question before it uploads anything,
    and the answer comes from the BUILT WHEEL's filename rather than from
    re-reading pyproject.toml. The filename is what pip will actually see;
    parsing the source again would only prove that the parser agrees with
    itself.

TAG vs PEP 440
    A git tag is written `v1.2.0-rc1`; the wheel is named `1.2.0rc1` because
    PEP 440 normalises separators away. Comparing them literally would
    reject every prerelease, so both sides are reduced to lowercase
    alphanumerics before comparison. `v1.2.0-rc.1` and `1.2.0rc1` therefore
    match, which is the intent - what this check is for is catching
    `v0.2.0` against a wheel that still says `0.1.0`.
"""

import argparse
import re
import sys
from pathlib import Path

# `lazyaf_cli-0.1.0-py3-none-any.whl` -> ('lazyaf_cli', '0.1.0')
WHEEL_NAME = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)-")


def normalize(value):
    return re.sub(r"[^0-9a-z]", "", value.lower())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="git tag, e.g. v0.1.0")
    parser.add_argument("--dist", default="dist", help="directory holding the built wheel")
    args = parser.parse_args()

    wheels = sorted(Path(args.dist).glob("*.whl"))
    if not wheels:
        print(f"no wheel found in {args.dist}/ - nothing to verify", file=sys.stderr)
        return 2

    tag = args.tag.lstrip("vV")
    failures = []
    for wheel in wheels:
        match = WHEEL_NAME.match(wheel.name)
        if not match:
            failures.append(f"cannot parse a version out of {wheel.name}")
            continue
        version = match.group("version")
        if normalize(version) != normalize(tag):
            failures.append(
                f"{wheel.name} declares version {version}, but this release is "
                f"tagged {args.tag}. The CLI version has ONE source - "
                f"`__version__` in cli/lazyaf/__init__.py, which "
                f"cli/pyproject.toml reads via [tool.setuptools.dynamic]. Bump "
                f"it there to match the tag (or move the tag), then run the "
                f"release again. A published version can never be reused, so "
                f"this has to be right BEFORE the upload, not after."
            )
        else:
            print(f"OK  {wheel.name} matches tag {args.tag}")

    if failures:
        print("\nRELEASE VERSION MISMATCH\n", file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
