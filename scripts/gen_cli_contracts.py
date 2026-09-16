#!/usr/bin/env python3
"""Render the two Go tables the CLI needs from the Python sources that own them.

    python scripts/gen_cli_contracts.py            # (re)write both files
    python scripts/gen_cli_contracts.py --check    # exit 1 with a diff if stale

Outputs (each with a `Code generated ... DO NOT EDIT.` header naming this script):

    cli/internal/doctor/step_images_gen.go
        var StepImages = []string{"lazyaf-base:dev", ...}
        from the IMAGES table in scripts/build_images.py - the single source
        of the step-image list, the one .github/scripts/step_images.py and
        the release workflows already read. `lazyaf doctor` checks that every
        one of these is present locally, exactly as scripts/preflight.py did
        from its own hand-copied FALLBACK_STEP_IMAGES. That copy dies; this
        one cannot drift.

    cli/internal/envfile/placeholders_gen.go
        var PlaceholderSecrets  = []string{...}   from _PLACEHOLDER_SECRETS
        var RetiredPublicSecrets = []string{...}  from RETIRED_PUBLIC_SECRETS
        both in backend/app/config.py, which is what the backend itself
        consults when it refuses a placeholder secret at startup.
        scripts/bootstrap_secrets.py and scripts/preflight.py carry copies
        "kept in sync by eye"; `lazyaf init` and `lazyaf doctor` get theirs
        from here instead, so a placeholder the backend refuses is one the
        CLI refuses, always.

THIS SCRIPT IS THE ONLY WRITER OF THOSE TWO FILES. T1
(`tdd/unit/scripts/test_cli_contracts_current.py`) renders both in-process
and byte-compares with the committed files, so a table change without a
regenerate goes red naming this command. Same idiom as
`scripts/gen_debug_terminal_corpus.py`: one generator, its outputs, a
currency test. R3: the Python tables stay the only sources.

HOW THE SOURCES ARE LOADED
    By file path with importlib, the way .github/scripts/step_images.py loads
    build_images.py: no package on sys.path, no environment. build_images.py
    imports the docker SDK lazily inside its functions, so reading IMAGES is
    free. backend/app/config.py imports pydantic at module level; on an
    interpreter without it this script REFUSES (naming the interpreter to
    use) rather than parsing the source by hand, so the values rendered are
    always the values the backend runs with.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_IMAGES = REPO_ROOT / "scripts" / "build_images.py"
CONFIG = REPO_ROOT / "backend" / "app" / "config.py"
SELF = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()

STEP_IMAGES_GO = REPO_ROOT / "cli" / "internal" / "doctor" / "step_images_gen.go"
PLACEHOLDERS_GO = REPO_ROOT / "cli" / "internal" / "envfile" / "placeholders_gen.go"


def _load_by_path(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


def load_step_images() -> list[str]:
    """`<name>:<tag>` for every IMAGES row, in the table's dependency order."""
    module = _load_by_path("lazyaf_build_images", BUILD_IMAGES)
    tag = getattr(module, "TAG", "dev")
    # (subdir, image name, parent subdir, extra_context) - only the name is
    # read; unpacked by index so a future column does not break this.
    refs = [f"{row[1]}:{tag}" for row in module.IMAGES]
    if not refs:
        raise RuntimeError(f"the IMAGES table in {BUILD_IMAGES} is empty")
    return refs


def load_placeholders() -> tuple[list[str], list[str]]:
    """(_PLACEHOLDER_SECRETS, RETIRED_PUBLIC_SECRETS) from config.py, sorted.

    Sorted because both are frozensets; a stable order is what makes the
    rendered file diffable and the currency check byte-exact.
    """
    try:
        module = _load_by_path("lazyaf_backend_config", CONFIG)
    except ModuleNotFoundError as exc:
        raise SystemExit(
            f"cannot import {CONFIG.relative_to(REPO_ROOT).as_posix()}: {exc}.\n"
            "It needs the backend's dependencies (pydantic). Run this script "
            "with the backend interpreter instead:\n"
            f"    uv run --directory backend python ../{SELF} {' '.join(sys.argv[1:])}"
        ) from exc
    placeholders = sorted(module._PLACEHOLDER_SECRETS)
    retired = sorted(module.RETIRED_PUBLIC_SECRETS)
    if not placeholders or not retired:
        raise RuntimeError(f"a placeholder table in {CONFIG} is empty")
    return placeholders, retired


def _label(path: Path) -> str:
    """A path as printed: repo-relative when it is under the repo, else as is."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _go_string(value: str) -> str:
    """A Go interpreted string literal. The tables hold plain ASCII today;
    escaping is done anyway so a future value with a quote or backslash
    renders as a compiling literal rather than a syntax error."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _go_slice(name: str, doc: str, values: list[str]) -> str:
    lines = [f"// {line}" if line else "//" for line in doc.rstrip("\n").split("\n")]
    lines.append(f"var {name} = []string{{")
    lines.extend(f"\t{_go_string(value)}," for value in values)
    lines.append("}")
    return "\n".join(lines) + "\n"


def _header(source: str) -> str:
    # The first line matches Go's `^// Code generated .* DO NOT EDIT\.$`
    # convention, so gofmt-aware tools and reviewers treat the file as
    # output, not source.
    return (
        f"// Code generated by {SELF} from {source}; DO NOT EDIT.\n"
        "//\n"
        f"// {source} is the single source of this table (R3). Edit it there,\n"
        f"// then run `python {SELF}`; T1 fails until the two agree.\n"
    )


def render_step_images(refs: list[str]) -> str:
    return (
        _header("scripts/build_images.py")
        + "\n"
        + "package doctor\n"
        + "\n"
        + _go_slice(
            "StepImages",
            "StepImages is every step image the local executor resolves, as\n"
            "`<name>:<tag>`, in the IMAGES table's dependency order (a parent always\n"
            "precedes its children). `lazyaf doctor` reports each one missing locally\n"
            "with the pull-and-retag remedy; nothing here is ever pulled by doctor.",
            refs,
        )
    )


def render_placeholders(placeholders: list[str], retired: list[str]) -> str:
    return (
        _header("backend/app/config.py")
        + "\n"
        + "package envfile\n"
        + "\n"
        + _go_slice(
            "PlaceholderSecrets",
            'PlaceholderSecrets are the "I copied the template and did not fill it in"\n'
            "shapes the backend refuses at startup (config.py's _PLACEHOLDER_SECRETS),\n"
            "sorted. Compared case-insensitively against a trimmed value, exactly as\n"
            "the backend does; `lazyaf init` replaces one, `lazyaf doctor` fails on one.",
            placeholders,
        )
        + "\n"
        + _go_slice(
            "RetiredPublicSecrets",
            "RetiredPublicSecrets are the constants LazyAF used to ship as defaults.\n"
            "They are public (git history, image layers, every fork), so a .env still\n"
            "holding one is treated as unset - exactly as the backend treats it\n"
            "(config.py's RETIRED_PUBLIC_SECRETS). Compared exactly, not lowercased.",
            retired,
        )
    )


def render_all() -> dict[Path, str]:
    """Every output path -> the exact text it must hold."""
    placeholders, retired = load_placeholders()
    return {
        STEP_IMAGES_GO: render_step_images(load_step_images()),
        PLACEHOLDERS_GO: render_placeholders(placeholders, retired),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 with a unified diff if a committed file is stale",
    )
    args = parser.parse_args(argv)

    stale = []
    for path, fresh in render_all().items():
        rel = _label(path)
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == fresh:
            if not args.check:
                print(f"current: {rel}")
            continue
        stale.append(rel)
        if args.check:
            if current is None:
                print(f"missing: {rel}", file=sys.stderr)
            else:
                print(f"stale: {rel}", file=sys.stderr)
                sys.stderr.writelines(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        fresh.splitlines(keepends=True),
                        fromfile=f"committed/{rel}",
                        tofile="fresh render",
                    )
                )
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" so the bytes are exactly `fresh` on every platform (a
        # Windows text-mode write would turn LF into CRLF and the currency
        # test compares bytes).
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(fresh)
        print(f"{'updated' if current is not None else 'wrote'} {rel}")

    if args.check:
        if stale:
            print(f"\nrun: python {SELF} and commit the result", file=sys.stderr)
            return 1
        print("cli contracts current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
