#!/usr/bin/env bash
# THE ONE build definition for the lazyaf binary (upcoming/go-cli.md §2.5).
#
# release.yml, pr-build.yml, the TG tier preflight (scripts/run_tier.py) and
# developers all call this script, so the ldflags, the target matrix and the
# checksum format live in one file (R3). Bash; runs under Git Bash on the
# owner's box and on the ubuntu/macos/windows release runners.
#
#   bash scripts/build_cli.sh              # six targets -> cli/dist/ + checksums.txt
#   bash scripts/build_cli.sh --host-only  # this machine only -> cli/bin/lazyaf[.exe]
#
# Version: ${GITHUB_REF_NAME#v} when GITHUB_REF_TYPE=tag, else "dev". A tag
# build stamps Commit explicitly; a dev build does not, so
# runtime/debug.ReadBuildInfo carries the sha and `--version` says
# `dev+<sha>[.dirty]` (§4.2) - "dev must say dev".
#
# -buildvcs stays ON (Go's default) for BOTH kinds of build. §2.5 said a tag
# build passes -buildvcs=false "so the bytes are identical wherever the tag
# is built"; that is a stated deviation, for a verified reason: with
# -trimpath, go1.26.8 records NO `build -ldflags=` line in the binary (Go
# issue 52372 - a -X value could carry a secret), so the VCS-stamped
# `mod github.com/Brennan-VanderLaan/lazyaf v<tag>` line is the only proof
# .github/scripts/check_binary_version.py can read without executing the
# binary (§4.4). vcs.revision/vcs.time are properties of the commit, so the
# bytes are still identical at any clean checkout of the tag; a dirty tag
# checkout stamps `+dirty` and is refused by the check, which is stronger.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODULE="github.com/Brennan-VanderLaan/lazyaf"
PKG="./cli/cmd/lazyaf"
VERSION_PKG="${MODULE}/cli/internal/version"

host_only=0
for arg in "$@"; do
    case "$arg" in
        --host-only) host_only=1 ;;
        -h|--help)
            sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "build_cli.sh: unknown argument '$arg' (only --host-only is accepted)" >&2
            exit 2
            ;;
    esac
done

if ! command -v go >/dev/null 2>&1; then
    echo "build_cli.sh: 'go' is not on PATH. Install Go (>= 1.21 bootstraps the go.mod toolchain automatically) from https://go.dev/dl/ and re-run." >&2
    exit 1
fi

# --- version and commit -----------------------------------------------------
if [ "${GITHUB_REF_TYPE:-}" = "tag" ] && [ -n "${GITHUB_REF_NAME:-}" ]; then
    V="${GITHUB_REF_NAME#v}"
else
    V="dev"
fi

if [ -n "${GITHUB_SHA:-}" ]; then
    SHA="$GITHUB_SHA"
elif git rev-parse HEAD >/dev/null 2>&1; then
    SHA="$(git rev-parse HEAD)"
else
    SHA=""
fi

# A dev build does NOT pass Commit: version.go reads the VCS stamp instead,
# which also carries the dirty bit that -X cannot. A tag build passes it.
ldflags="-s -w -X ${VERSION_PKG}.Version=${V}"
if [ "$V" != "dev" ]; then
    ldflags="${ldflags} -X ${VERSION_PKG}.Commit=${SHA}"
fi

build() {
    local goos="$1" goarch="$2" out="$3"
    echo "building ${goos}/${goarch} -> ${out}"
    CGO_ENABLED=0 GOOS="$goos" GOARCH="$goarch" \
        go build -trimpath -ldflags "$ldflags" -o "$out" "$PKG"
}

ext_for() {
    case "$1" in
        windows) echo ".exe" ;;
        *) echo "" ;;
    esac
}

# --- host only --------------------------------------------------------------
if [ "$host_only" = 1 ]; then
    goos="$(go env GOOS)"
    goarch="$(go env GOARCH)"
    mkdir -p cli/bin
    out="cli/bin/lazyaf$(ext_for "$goos")"
    build "$goos" "$goarch" "$out"
    echo "built ${out} (version ${V})"
    exit 0
fi

# --- the six targets --------------------------------------------------------
rm -rf cli/dist
mkdir -p cli/dist
targets="linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64"
for target in $targets; do
    goos="${target%/*}"
    goarch="${target#*/}"
    build "$goos" "$goarch" "cli/dist/lazyaf_${V}_${goos}_${goarch}$(ext_for "$goos")"
done

# sha256sum format, version-free file name: install.sh reads the version out
# of the first file name in it (§6 step 3) and verifies with `sha256sum -c`.
# Git Bash's sha256sum marks binary mode with `*name`; Linux writes `  name`.
# Normalised to the two-space form so the file is byte-identical in shape
# wherever it was written (`-c` accepts either).
(
    cd cli/dist
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum lazyaf_* | sed 's/ \*/  /' > checksums.txt
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 lazyaf_* | sed 's/ \*/  /' > checksums.txt
    else
        echo "build_cli.sh: neither sha256sum nor shasum is on PATH; cannot write checksums.txt" >&2
        exit 1
    fi
)
echo "wrote cli/dist/checksums.txt:"
cat cli/dist/checksums.txt
