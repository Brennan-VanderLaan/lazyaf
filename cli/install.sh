#!/usr/bin/env bash
# lazyaf installer (upcoming/go-cli.md §6).
#
#   curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash
#
# Installs ONE statically linked binary into ~/.local/bin after verifying its
# sha256 against the release's checksums.txt, then tells you the one line to
# add if that directory is not on your PATH. It never edits an rc file and
# never runs `setx`. Re-running upgrades (or repairs) in place.
#
#   LAZYAF_VERSION=v0.3.0 ...   pin a version (default: the latest non-prerelease)
#   LAZYAF_INSTALL_DIR=DIR ...  install somewhere other than ~/.local/bin
#   --install-dir DIR           same as LAZYAF_INSTALL_DIR
#   --from-dir DIR              local mode: read a build_cli.sh dist directory
#                               instead of the network (tests, CI smoke)
#   --no-completions            skip shell completion
#   --version vX.Y.Z            same as LAZYAF_VERSION
#
# This file is committed at cli/install.sh AND attached to every release, so
# the script and the asset names it expects are versioned together (a script
# fetched from main could name assets an older release does not have).
#
# Portability: bash 3.2 (macOS /bin/bash) - no arrays, no ${var,,}, no
# mapfile, no associative anything. Runs under Git Bash on Windows (uname
# says MINGW*/MSYS*), where it installs lazyaf.exe into %USERPROFILE%\.local\bin.
# Every path expansion is double-quoted: the owner's $HOME contains a space
# and the TG test runs once under such a HOME.
#
# Needs: bash, curl (network mode only), and sha256sum or shasum. A missing
# tool is a refusal naming it and the manual path. Verification is never
# skipped; there is no --insecure.
set -euo pipefail

REPO="Brennan-VanderLaan/lazyaf"
RELEASES="https://github.com/${REPO}/releases"
MODULE_MAIN="github.com/Brennan-VanderLaan/lazyaf/cli/cmd/lazyaf"
# The six prebuilt targets (§1.1). Anything else gets the go-install fallback,
# which only resolves because go.mod lives at the repo root (§2.1).
SUPPORTED="linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64"

# --- output ---------------------------------------------------------------
# Progress on stdout; refusals and warnings on stderr, like the binary (§5).
say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
# die WHAT REMEDY: every failure names what was attempted and one remedy (R1).
die() {
    printf 'install.sh: %s\n' "$1" >&2
    if [ "${2:-}" != "" ]; then
        printf '  remedy: %s\n' "$2" >&2
    fi
    exit 1
}

usage() {
    cat <<'USAGE'
lazyaf installer - installs one verified static binary into ~/.local/bin.

  curl -fsSL https://github.com/Brennan-VanderLaan/lazyaf/releases/latest/download/install.sh | bash

  LAZYAF_VERSION=v0.3.0        pin a version (default: latest non-prerelease)
  LAZYAF_INSTALL_DIR=DIR       install somewhere other than ~/.local/bin
  --install-dir DIR            same as LAZYAF_INSTALL_DIR
  --from-dir DIR               local mode: read a build_cli.sh dist directory
  --no-completions             skip shell completion
  --version vX.Y.Z             same as LAZYAF_VERSION
  -h, --help                   this text
USAGE
}

# --- arguments ------------------------------------------------------------
from_dir=""
install_dir="${LAZYAF_INSTALL_DIR:-}"
want_version="${LAZYAF_VERSION:-}"
completions=1
while [ $# -gt 0 ]; do
    case "$1" in
        --from-dir) [ $# -ge 2 ] || die "--from-dir needs a directory" "bash install.sh --from-dir cli/dist"; from_dir="$2"; shift 2 ;;
        --from-dir=*) from_dir="${1#--from-dir=}"; shift ;;
        --install-dir) [ $# -ge 2 ] || die "--install-dir needs a directory" "bash install.sh --install-dir \"\$HOME/.local/bin\""; install_dir="$2"; shift 2 ;;
        --install-dir=*) install_dir="${1#--install-dir=}"; shift ;;
        --version) [ $# -ge 2 ] || die "--version needs a tag" "bash install.sh --version v0.3.0"; want_version="$2"; shift 2 ;;
        --version=*) want_version="${1#--version=}"; shift ;;
        --no-completions) completions=0; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument '$1'" "accepted: --from-dir DIR, --install-dir DIR, --version vX.Y.Z, --no-completions, --help" ;;
    esac
done

# --- tools ----------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

hash_file() {
    if have sha256sum; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

if ! have sha256sum && ! have shasum; then
    die "neither sha256sum nor shasum is on PATH, so the download cannot be verified (and verification is never skipped)" \
        "install coreutils (Linux: your package manager; macOS ships shasum with perl) or download lazyaf and checksums.txt from ${RELEASES} and compare the digest yourself"
fi
if [ -z "$from_dir" ] && ! have curl; then
    die "curl is not on PATH; nothing was downloaded" \
        "install curl, or download the binary and checksums.txt from ${RELEASES} into a folder and run: bash install.sh --from-dir THAT_FOLDER"
fi

# --- 2. platform ----------------------------------------------------------
# LAZYAF_FORCE_PLATFORM=os/arch is a test hook for the refusal path; the
# HOST detection below (host_os) still drives the Windows-specific steps.
raw_os="$(uname -s)"
raw_arch="$(uname -m)"
case "$raw_os" in
    Linux) host_os="linux" ;;
    Darwin) host_os="darwin" ;;
    MINGW*|MSYS*|CYGWIN*) host_os="windows" ;;
    *) host_os="$raw_os" ;;
esac
case "$raw_arch" in
    x86_64|amd64) host_arch="amd64" ;;
    aarch64|arm64) host_arch="arm64" ;;
    *) host_arch="$raw_arch" ;;
esac
os="$host_os"
arch="$host_arch"
if [ -n "${LAZYAF_FORCE_PLATFORM:-}" ]; then
    os="${LAZYAF_FORCE_PLATFORM%/*}"
    arch="${LAZYAF_FORCE_PLATFORM#*/}"
fi

supported=0
# shellcheck disable=SC2086  # SUPPORTED is a space-separated list on purpose
for pair in $SUPPORTED; do
    if [ "$pair" = "${os}/${arch}" ]; then supported=1; fi
done
if [ "$supported" != 1 ]; then
    if [ -n "$want_version" ]; then
        go_ref="v${want_version#v}"
    else
        go_ref="latest"
    fi
    die "no prebuilt lazyaf for ${os}/${arch}; supported: ${SUPPORTED}" \
        "build from source: go install ${MODULE_MAIN}@${go_ref}"
fi

ext=""
if [ "$os" = "windows" ]; then ext=".exe"; fi

# --- temp dir, cleaned on every exit ---------------------------------------
tmp="$(mktemp -d)"
tmpfile=""
cleanup() {
    rm -rf "$tmp"
    # The half-written target must never survive (§6 step 5): readers see
    # the old binary or the new one, never a partial file.
    if [ -n "$tmpfile" ]; then rm -f "$tmpfile"; fi
}
trap cleanup EXIT

# --- 3. version -----------------------------------------------------------
# The version is read out of the FIRST file name in checksums.txt
# (lazyaf_<ver>_<os>_<arch>[.exe]): no GitHub API call, no token, no rate
# limit, and releases/latest never resolves to a prerelease.
version_from_checksums() {
    local first
    first="$(awk 'NR==1 {print $NF}' "$1")"
    first="${first#\*}"
    case "$first" in
        lazyaf_*) ;;
        *) return 1 ;;
    esac
    first="${first#lazyaf_}"
    printf '%s\n' "${first%%_*}"
}

download() {
    # download URL DEST
    local rc=0
    curl -fsSL --retry 3 -o "$2" "$1" || rc=$?
    if [ "$rc" -eq 22 ]; then
        die "HTTP error fetching ${1}: that version has no asset for ${os}/${arch} (or the tag does not exist)" \
            "see the releases page: ${RELEASES}"
    elif [ "$rc" -ne 0 ]; then
        die "curl exited ${rc} fetching ${1}" \
            "check the network/proxy and re-run, or download the file by hand from ${RELEASES} and use --from-dir"
    fi
    say "downloaded $1"
}

checksums="$tmp/checksums.txt"
if [ -n "$from_dir" ]; then
    [ -d "$from_dir" ] || die "--from-dir ${from_dir} is not a directory" "bash scripts/build_cli.sh writes one to cli/dist"
    [ -f "$from_dir/checksums.txt" ] || die "${from_dir}/checksums.txt not found" "bash scripts/build_cli.sh writes it; a hand-made dist needs the sha256sum-format file too"
    cp "$from_dir/checksums.txt" "$checksums"
    version="$(version_from_checksums "$checksums")" || die "${from_dir}/checksums.txt does not start with a lazyaf_<ver>_<os>_<arch> entry" "rebuild the dist with bash scripts/build_cli.sh"
    label="from ${from_dir}"
    source_desc="$from_dir"
elif [ -n "$want_version" ]; then
    version="${want_version#v}"
    download "${RELEASES}/download/v${version}/checksums.txt" "$checksums"
    label="pinned"
    source_desc="${RELEASES}/download/v${version}"
else
    download "${RELEASES}/latest/download/checksums.txt" "$checksums"
    version="$(version_from_checksums "$checksums")" || die "the latest release's checksums.txt does not start with a lazyaf_<ver>_<os>_<arch> entry" "pin a version with LAZYAF_VERSION=vX.Y.Z or see ${RELEASES}"
    label="latest"
    source_desc="${RELEASES}/download/v${version}"
fi
# A dev dist is "dev", not "vdev": only a release version wears the v.
pretty="v${version}"
if [ "$version" = "dev" ]; then pretty="dev"; fi
say "installing lazyaf ${pretty} (${label})"

asset="lazyaf_${version}_${os}_${arch}${ext}"

# --- 4. fetch and verify -----------------------------------------------------
if [ -n "$from_dir" ]; then
    [ -f "$from_dir/$asset" ] || die "${from_dir}/${asset} not found (the dist has no build for ${os}/${arch})" "bash scripts/build_cli.sh builds all six"
    cp "$from_dir/$asset" "$tmp/$asset"
else
    download "${source_desc}/${asset}" "$tmp/$asset"
fi

# Equivalent to `sha256sum -c --ignore-missing` / `shasum -a 256 -c`, done by
# hand so both tools behave identically and a mismatch can print expected vs
# actual. The name column may carry sha256sum's binary-mode `*` prefix.
expected="$(awk -v n="$asset" '{name=$NF; sub(/^\*/, "", name); if (name == n) {print $1; exit}}' "$checksums")"
[ -n "$expected" ] || die "checksums.txt from ${source_desc} has no entry for ${asset}" "the release is incomplete; see ${RELEASES}"
actual="$(hash_file "$tmp/$asset")"
if [ "$expected" != "$actual" ]; then
    printf 'install.sh: sha256 mismatch for %s\n  expected %s\n  actual   %s\n  source   %s/%s\n' \
        "$asset" "$expected" "$actual" "$source_desc" "$asset" >&2
    die "nothing was installed" "re-run to download again; if it repeats, the asset on the release is not the one checksums.txt describes - report it at https://github.com/${REPO}/issues"
fi
say "verified sha256 ${actual:0:12}"

# --- 5. install atomically --------------------------------------------------
if [ -z "$install_dir" ]; then
    install_dir="$HOME/.local/bin"
fi
mkdir -p "$install_dir" || die "could not create ${install_dir}" "pick a writable directory with --install-dir DIR"
# Canonical form, so PATH comparison and the shadow check compare like with
# like (Git Bash: /c/Users/... whichever spelling the caller used).
install_dir="$(cd "$install_dir" && pwd -P)"
target="$install_dir/lazyaf${ext}"

# A previous Windows upgrade may have left the old running binary behind.
rm -f "$target.old" 2>/dev/null || true

# --- 6. upgrade / reinstall report ----------------------------------------------
# A dev dist names its files lazyaf_dev_* while the binary says dev+<sha>
# (§4.3), so "dev" matches any dev+ build; a release version matches exactly.
version_matches() {
    if [ "$2" = "dev" ]; then
        case "$1" in dev|dev+*) return 0 ;; esac
        return 1
    fi
    [ "$1" = "$2" ]
}

if [ -e "$target" ]; then
    old="$("$target" --version --short 2>/dev/null || true)"
    if [ -n "$old" ] && version_matches "$old" "$version"; then
        say "${version} is already installed; reinstalling"
    elif [ -n "$old" ]; then
        say "lazyaf ${old} -> ${version}"
    else
        say "replacing ${target}, which could not report its version"
    fi
fi

tmpfile="$install_dir/lazyaf.tmp.$$"
cp "$tmp/$asset" "$tmpfile"
chmod +x "$tmpfile"
if ! mv -f "$tmpfile" "$target" 2>/dev/null; then
    # Windows cannot replace a RUNNING lazyaf.exe, but it can rename one:
    # move the old aside, move the new in, delete the .old on the next run.
    moved=0
    if [ "$host_os" = "windows" ] && [ -e "$target" ]; then
        if mv -f "$target" "$target.old" 2>/dev/null && mv -f "$tmpfile" "$target" 2>/dev/null; then
            moved=1
        fi
    fi
    if [ "$moved" != 1 ]; then
        die "could not replace ${target}" \
            "close every running lazyaf (or a shell whose completion is calling it) and re-run; on Windows, also check nothing has the file open"
    fi
fi
tmpfile=""

# --- 7. post-install proof: the bytes run HERE --------------------------------
reported="$("$target" --version --short 2>&1)" || die "installed binary ${target} failed to run: ${reported}" "the checksum matched, so this is a platform problem - is ${os}/${arch} really this machine? report it at https://github.com/${REPO}/issues"
if ! version_matches "$reported" "$version"; then
    die "installed binary reports ${reported}, expected ${version}" \
        "the file at ${source_desc}/${asset} was not built for tag v${version}; re-run to repair, or pin another version with LAZYAF_VERSION"
fi
say "installed ${target} (lazyaf ${reported})"

# Another lazyaf earlier on PATH would keep winning; say so and name it.
found="$(command -v lazyaf 2>/dev/null || true)"
if [ -n "$found" ] && [ -d "$(dirname "$found")" ]; then
    found_dir="$(cd "$(dirname "$found")" && pwd -P)"
    if [ "$found_dir" != "$install_dir" ]; then
        warn "your shell will keep finding ${found} (earlier on PATH than ${install_dir}); remove it or reorder PATH to use the one just installed"
    fi
fi

# --- 8. PATH: print the one line, never edit an rc file ---------------------------
on_path=0
case ":$PATH:" in
    *":$install_dir:"*) on_path=1 ;;
esac
if [ "$on_path" != 1 ]; then
    # The textual check misses a differently spelled entry (~ vs $HOME, a
    # Windows short name, a symlink); compare canonical directories before
    # telling someone to add a line they already have.
    old_ifs="$IFS"
    IFS=':'
    set -f
    # shellcheck disable=SC2086  # split on ':' only (IFS above), globbing off (set -f)
    for entry in $PATH; do
        if [ -n "$entry" ] && [ -d "$entry" ] && [ "$(cd "$entry" 2>/dev/null && pwd -P)" = "$install_dir" ]; then
            on_path=1
            break
        fi
    done
    set +f
    IFS="$old_ifs"
fi

if [ "$on_path" = 1 ]; then
    say "${install_dir} is on your PATH"
else
    # Is the target the default? Compared in canonical form (install_dir
    # already is) so the pretty $HOME spelling is used exactly when it is
    # true, and a custom directory is always named literally.
    default_dir="$(cd "$HOME/.local/bin" 2>/dev/null && pwd -P || true)"
    if [ -n "$default_dir" ] && [ "$install_dir" = "$default_dir" ]; then
        is_default=1
        dir_expr="\$HOME/.local/bin"
    else
        is_default=0
        dir_expr="$install_dir"
    fi
    shell_name="$(basename "${SHELL:-bash}")"
    say "${install_dir} is not on your PATH. Add it (one line, then open a new shell):"
    case "$shell_name" in
        zsh)  say "  echo 'export PATH=\"${dir_expr}:\$PATH\"' >> ~/.zshrc" ;;
        fish) say "  fish_add_path ${dir_expr}" ;;
        *)    say "  echo 'export PATH=\"${dir_expr}:\$PATH\"' >> ~/.bashrc" ;;
    esac
    if [ "$host_os" = "windows" ]; then
        # The Windows-side line must name the SAME directory as the bashrc
        # line: a custom --install-dir / LAZYAF_INSTALL_DIR is spelled the
        # way Windows spells it (cygpath -w), and only the default target
        # gets the %USERPROFILE% shorthand. Naming a directory that holds
        # nothing is the wrong remedy (R1).
        if [ "$is_default" = 1 ]; then
            win_settings="%USERPROFILE%\\.local\\bin"
            win_ps="\$env:USERPROFILE\\.local\\bin"
        else
            if have cygpath; then
                win_settings="$(cygpath -w "$install_dir")"
            else
                win_settings="$install_dir"
            fi
            win_ps="$win_settings"
        fi
        say "  Windows (so PowerShell and cmd find it too): add ${win_settings} to your user PATH via Settings, or in PowerShell:"
        say "    [Environment]::SetEnvironmentVariable('Path', \"${win_ps};\" + [Environment]::GetEnvironmentVariable('Path','User'), 'User')"
        say "  Do NOT use 'setx PATH' - it truncates PATH at 1024 characters."
    fi
fi

# --- 9. completion (§7) -------------------------------------------------------
if [ "$completions" = 1 ]; then
    loader=""
    for candidate in \
        "${BASH_COMPLETION_USER_DIR:-}/bash_completion" \
        /usr/share/bash-completion/bash_completion \
        /usr/local/share/bash-completion/bash_completion \
        /opt/homebrew/etc/profile.d/bash_completion.sh \
        /usr/local/etc/profile.d/bash_completion.sh; do
        if [ -n "$candidate" ] && [ -r "$candidate" ]; then loader="$candidate"; break; fi
    done
    if [ -n "$loader" ]; then
        # bash-completion 2.x autoloads this file by command name; no rc edit.
        comp_dir="${BASH_COMPLETION_USER_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion}/completions"
        if mkdir -p "$comp_dir" 2>/dev/null && "$target" completion bash > "$comp_dir/lazyaf.tmp.$$" 2>/dev/null \
            && mv -f "$comp_dir/lazyaf.tmp.$$" "$comp_dir/lazyaf"; then
            say "bash completion written to ${comp_dir}/lazyaf (loaded by ${loader})"
        else
            rm -f "$comp_dir/lazyaf.tmp.$$" 2>/dev/null || true
            warn "could not write bash completion to ${comp_dir}/lazyaf; add this to ~/.bashrc instead:  source <(lazyaf completion bash)"
        fi
    else
        # Git Bash ships no bash-completion loader, so a file there would
        # never be read; the source line works in any bash.
        say "bash completion: add to ~/.bashrc:  source <(lazyaf completion bash)"
    fi
    say "zsh completion:   lazyaf completion zsh > \"\${fpath[1]}/_lazyaf\"   (then: rm -f ~/.zcompdump; compinit)"
    say "fish completion:  lazyaf completion fish > ~/.config/fish/completions/lazyaf.fish"
    say "PowerShell:       add to \$PROFILE:  lazyaf completion powershell | Out-String | Invoke-Expression"
fi

say "done: lazyaf ${pretty} at ${target}"
