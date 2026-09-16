#!/usr/bin/env python3
"""Refuse to publish a lazyaf binary whose version disagrees with the git tag.

    python3 .github/scripts/check_binary_version.py --tag v0.3.0 --dist cli/dist

Exit 0 = every binary in dist/ carries the tag's version, 1 = at least one
does not (or is missing, or cannot be read), 2 = dist/ holds no binaries.

WHY
    `lazyaf --version` has to print the version of the release it came from.
    The tag lives in git; the binary learns its version through
    `-ldflags -X ...version.Version=<v>` from scripts/build_cli.sh
    (upcoming/go-cli.md §4); nothing in the tree carries a bumpable version.
    So the release workflow asks this question before it uploads anything,
    and it asks it of ALL SIX cross-compiled binaries - `go version -m` prints
    a binary's recorded build settings for any GOOS/GOARCH without executing
    it. Executing the host binary's `--version --short` (release.yml does that
    too) proves the bytes run; this proves every sibling was built the same
    way. Exact string compare: the PEP 440 normalisation the wheel checker
    needed (check_release_version.py) was a wheel problem.

WHAT `go version -m` RECORDS - VERIFIED ON go1.26.8, NOT ASSUMED
    The plan (§4.4) says to parse the `build -ldflags=` line. That line is
    only recorded when a binary was built WITHOUT -trimpath: with -trimpath,
    cmd/go deliberately omits -ldflags from the build info (a -X value could
    carry a secret; Go issue 52372), and scripts/build_cli.sh passes
    -trimpath by design (§2.5). Verified on this box:

        go build -trimpath -ldflags "-X ...Version=9.9.9"   -> no -ldflags line
        go build           -ldflags "-X ...Version=9.9.9"   -> build -ldflags="... Version=9.9.9 ..."

    So there are two proofs, and this checker accepts either, in this order:

      1. the `build -ldflags=` line names `internal/version.Version=<v>`
         (present when the build did not trim paths);
      2. the `mod <module> <version>` line reads exactly `v<tag>` - Go 1.24+
         stamps the main module's version from the VCS checkout: the tag
         itself when HEAD is tagged, a pseudo-version otherwise, `+dirty`
         appended when the tree is modified. Also verified on go1.26.8, in a
         throwaway repo tagged v0.9.9: `mod github.com/... v0.9.9`. This
         proof needs -buildvcs ON (the default) at build time.

    scripts/build_cli.sh therefore keeps -buildvcs ON for tag builds (a
    stated §2.5 deviation, recorded in its header): with -trimpath, proof 2
    is the only executable-free proof there is. A binary that offers
    neither proof (built with -trimpath AND -buildvcs=false - the flags §2.5
    originally prescribed, which is why the P3 verifiers saw every tag
    build refused) is refused with a message naming both missing lines and
    the remedy, rather than waved through. A refusal here is a build-flag
    problem, never a reason to skip the check.

    Two properties of proof 2 that a release engineer must know, both
    verified on go1.26.8 in a scratch clone (tdd/unit/scripts/
    test_check_binary_version.py::TestAgainstARealTagBuild re-proves them
    on every T1 run by building a real tagged clone through build_cli.sh):

      * Go derives the version from the tag only for a v0.x / v1.x tag on
        this /v-less module path; a v2+ tag stamps a pseudo-version and is
        refused here - correct, since such a tag would also break
        `go install ...@vN` (§2.1).
      * `vcs.modified` is `git status --porcelain` being non-empty, and
        UNTRACKED files count. cli/dist/ MUST stay in .gitignore: without
        that, the first binary build_cli.sh writes dirties the tree and the
        remaining five are stamped `+dirty` and refused. The real-build test
        copies the repo's own .gitignore for exactly this reason.

WHAT ELSE IS PINNED, CHEAPLY, WHILE THE TRANSCRIPT IS OPEN
    * the file name `lazyaf_<ver>_<os>_<arch>[.exe]` carries the tag's
      version too (install.sh reads the version out of that name, §6);
    * GOOS/GOARCH recorded in the binary match the name (a mislabelled
      asset would install and then fail to exec);
    * all six targets of §1.1 are present - a matrix that silently shrank is
      the fake-green shape R4 exists for. Fewer is exit 1 naming the gap.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

# §1.1 / §6: the six release targets, and the exact asset names.
TARGETS = (
    ("linux", "amd64"),
    ("linux", "arm64"),
    ("darwin", "amd64"),
    ("darwin", "arm64"),
    ("windows", "amd64"),
    ("windows", "arm64"),
)

# lazyaf_0.3.0_linux_amd64  /  lazyaf_0.3.0_windows_arm64.exe
ASSET_NAME = re.compile(
    r"^lazyaf_(?P<version>[^_]+)_(?P<goos>[a-z0-9]+)_(?P<goarch>[a-z0-9]+)(?P<ext>\.exe)?$"
)

# `build\t-ldflags="-s -w -X github.com/.../cli/internal/version.Version=0.3.0 -X ..."`
LDFLAGS_LINE = re.compile(r"^\s*build\s+-ldflags=(?P<value>.*)$")
LDFLAGS_VERSION = re.compile(r"internal/version\.Version=(?P<version>[^\s\"']+)")

# `mod\tgithub.com/Brennan-VanderLaan/lazyaf\tv0.3.0\t` (a trailing sum column
# may be present or empty).
MOD_LINE = re.compile(r"^\s*mod\s+(?P<path>\S+)\s+(?P<version>\S+)")
SETTING_LINE = re.compile(r"^\s*build\s+(?P<key>[A-Za-z0-9_.-]+)=(?P<value>.*)$")


def asset_name(version, goos, goarch):
    return f"lazyaf_{version}_{goos}_{goarch}{'.exe' if goos == 'windows' else ''}"


def go_version_m(binary, go="go"):
    """Run `go version -m <binary>` and return its stdout.

    Raises RuntimeError with the tool's own words when it fails: a binary Go
    cannot read is not "unknown version", it is a broken artifact.
    """
    proc = subprocess.run(
        [go, "version", "-m", str(binary)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"`{go} version -m {binary}` exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    return proc.stdout


def parse_transcript(text):
    """Pull the facts this check needs out of one `go version -m` transcript.

    Returns a dict with keys: ldflags_version (str|None), mod_version
    (str|None), settings (dict of `build KEY=value` lines).
    """
    facts = {"ldflags_version": None, "mod_version": None, "settings": {}}
    for line in text.splitlines():
        m = LDFLAGS_LINE.match(line)
        if m:
            v = LDFLAGS_VERSION.search(m.group("value"))
            if v:
                facts["ldflags_version"] = v.group("version")
            continue
        m = MOD_LINE.match(line)
        if m and facts["mod_version"] is None:
            facts["mod_version"] = m.group("version")
            continue
        m = SETTING_LINE.match(line)
        if m:
            facts["settings"][m.group("key")] = m.group("value")
    return facts


def check_binary(path, expected, read):
    """Return (ok, message) for one binary against the expected version."""
    name = path.name
    m = ASSET_NAME.match(name)
    if not m:
        return False, f"{name}: not a release asset name (expected lazyaf_<ver>_<os>_<arch>[.exe])"
    if m.group("version") != expected:
        return False, (
            f"{name}: the file name says version {m.group('version')}, but this "
            f"release is tagged v{expected}. build_cli.sh names assets from "
            f"GITHUB_REF_NAME; this dist was not built at the tag."
        )
    try:
        facts = parse_transcript(read(path))
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal, never a traceback
        return False, f"{name}: could not read build info: {exc}"

    goos = facts["settings"].get("GOOS")
    goarch = facts["settings"].get("GOARCH")
    if (goos, goarch) != (m.group("goos"), m.group("goarch")):
        return False, (
            f"{name}: the binary records GOOS={goos} GOARCH={goarch}, but its "
            f"name promises {m.group('goos')}/{m.group('goarch')}"
        )

    recorded = facts["ldflags_version"]
    if recorded is not None:
        if recorded == expected:
            return True, f"OK  {name}: -ldflags records version {recorded} (tag v{expected})"
        return False, (
            f"{name}: -ldflags records version.Version={recorded}, but this "
            f"release is tagged v{expected}. The version has ONE source - the "
            f"tag, stamped by scripts/build_cli.sh from GITHUB_REF_NAME - so "
            f"either this dist was built at the wrong ref or build_cli.sh's "
            f"-X flag drifted. Rebuild at the tag; never edit a version file."
        )

    mod_version = facts["mod_version"]
    if mod_version == f"v{expected}":
        return True, (
            f"OK  {name}: VCS stamp records module version {mod_version} "
            f"(tag v{expected}; no -ldflags line because of -trimpath)"
        )
    if mod_version and mod_version.startswith(f"v{expected}+"):
        return False, (
            f"{name}: built from a DIRTY checkout of v{expected} (module version "
            f"{mod_version}). A release binary must be built from the exact "
            f"tagged tree; clean the checkout and rebuild. (Untracked files "
            f"count as dirty: cli/dist/ and cli/bin/ must stay in .gitignore.)"
        )
    if mod_version and mod_version != "(devel)":
        # The VCS stamp IS a proof - of a different version. This is the
        # mismatch shape, not the missing-proof shape: the file was named
        # after GITHUB_REF_NAME but built from a commit that is not the tag.
        return False, (
            f"{name}: VCS stamp records module version {mod_version}, but this "
            f"release is tagged v{expected}. Go stamps the main module's version "
            f"from the tag at HEAD, so this dist was built from a commit that is "
            f"not v{expected} (a pseudo-version means HEAD carried no v0/v1 tag "
            f"at all). The version has ONE source - the tag - so rebuild at the "
            f"tag; never edit a version file."
        )
    return False, (
        f"{name}: no proof of version. The transcript has no `build -ldflags=` "
        f"line (go omits it under -trimpath) and its `mod` line reads "
        f"{mod_version or '<absent>'}, not v{expected}. A binary can only prove "
        f"its version without being executed through one of those two lines: "
        f"build with -buildvcs on at the tagged commit (the mod line then reads "
        f"v{expected} for a v0/v1 tag), or build without -trimpath (the -ldflags "
        f"line is then recorded). Check the flags in scripts/build_cli.sh - it "
        f"must not pass -buildvcs=false on a tag."
    )


def check_dist(dist, tag, read=go_version_m):
    """Check every lazyaf_* binary in dist. Returns (exit_code, lines)."""
    dist = Path(dist)
    expected = tag[1:] if tag[:1] in ("v", "V") else tag
    binaries = sorted(p for p in dist.glob("lazyaf_*") if p.is_file())
    if not binaries:
        return 2, [f"no lazyaf_* binary found in {dist} - nothing to verify (run bash scripts/build_cli.sh)"]

    lines, failures = [], []
    for binary in binaries:
        ok, message = check_binary(binary, expected, read)
        (lines if ok else failures).append(message)

    present = {p.name for p in binaries}
    for goos, goarch in TARGETS:
        want = asset_name(expected, goos, goarch)
        if want not in present:
            failures.append(
                f"missing {want}: every release ships all six targets "
                f"({', '.join(f'{o}/{a}' for o, a in TARGETS)}); build_cli.sh "
                f"builds them in one job, so a missing one means that build "
                f"did not finish - do not publish a partial matrix."
            )

    if failures:
        lines.append("")
        lines.append("RELEASE VERSION MISMATCH")
        lines.extend(f"  {f}" for f in failures)
        return 1, lines
    return 0, lines


def main(argv=None, read=go_version_m):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="git tag, e.g. v0.3.0")
    parser.add_argument("--dist", default="cli/dist", help="directory holding the built binaries")
    parser.add_argument("--go", default="go", help="the go tool to run `version -m` with (default: go)")
    args = parser.parse_args(argv)

    if read is go_version_m and args.go != "go":
        go = args.go
        read = lambda path: go_version_m(path, go)  # noqa: E731 - a one-line partial

    code, lines = check_dist(args.dist, args.tag, read)
    stream = sys.stdout if code == 0 else sys.stderr
    for line in lines:
        print(line, file=stream)
    return code


if __name__ == "__main__":
    sys.exit(main())
