#!/usr/bin/env python3
"""Write tdd/contracts/debug_terminal.v1.json from the server's debug-terminal codec.

    python scripts/gen_debug_terminal_corpus.py            # (re)write the corpus
    python scripts/gen_debug_terminal_corpus.py --check    # exit 1 with a diff if stale

THIS SCRIPT IS THE ONLY WRITER OF THAT FILE. A hand edit to the JSON is a bug:
T1 (`tdd/unit/debug/test_terminal_protocol_contract.py::TestCorpusIsCurrent`)
regenerates the corpus in memory and byte-compares it with the committed
file, so an edited file goes red on the next run with "run the generator".

WHY IT EXISTS (R3, go-cli plan section 3)
    The debug-terminal wire codec is defined ONCE, in
    `backend/app/services/execution/debug_terminal.py`. Every client is a
    copy. The Python CLI's copy was pinned by a pytest that imported both
    modules; the Go binary cannot be imported by pytest, so the pin became
    data: the server EMITS the contract (`export_contract()`), this script
    writes it down, T1 proves the file is fresh, and the Go suite is tested
    against the file. Only the server can write the corpus. Go can only read
    it, which is what stops a second codec becoming a second source of truth.

    `.v1` in the filename is PROTOCOL_VERSION. A v2 is a NEW file both
    consumers opt into; this one is never rewritten to mean something else.

HOW THE CODEC IS LOADED
    By file path, not through `app.services.execution`. The plan describes
    the `sys.path` trick the contract test uses, but that import runs the
    package's `__init__`, which pulls in the ORM and `get_settings()`, which
    refuses to run without the shared secrets - fine under pytest (conftest
    sets test values), wrong for a generator a developer runs from a bare
    shell. The codec module itself imports only the standard library, so
    loading it by path needs nothing installed and no environment. Stated
    deviation, and the reason.
"""
from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEC = REPO_ROOT / "backend" / "app" / "services" / "execution" / "debug_terminal.py"
CORPUS = REPO_ROOT / "tdd" / "contracts" / "debug_terminal.v1.json"

#: Registered under this name so the module's `@dataclass` (which looks
#: itself up in `sys.modules` because of `from __future__ import annotations`)
#: resolves; a bare `module_from_spec` + `exec_module` fails on that lookup.
MODULE_NAME = "lazyaf_debug_terminal_codec"


def load_server_codec():
    """The server codec module, loaded by path (see the module docstring)."""
    if MODULE_NAME in sys.modules:
        return sys.modules[MODULE_NAME]
    spec = importlib.util.spec_from_file_location(MODULE_NAME, CODEC)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load the server codec from {CODEC}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def render_corpus(server) -> str:
    """The exact text the corpus file holds for this codec module.

    `sort_keys=True` so the file is stable across Python versions and dict
    construction order; `indent=2` so a review diff shows the one line that
    moved; a trailing newline so the file is a POSIX text file. Frame
    `fields` are lists, which `sort_keys` does not touch - that is the point
    of them being lists (see `export_contract`).
    """
    return json.dumps(server.export_contract(), sort_keys=True, indent=2) + "\n"


def corpus_path_for(server) -> Path:
    """The file for THIS protocol version; a version bump names a new file."""
    return CORPUS.with_name(f"debug_terminal.v{server.PROTOCOL_VERSION}.json")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="write nothing; exit 1 with a unified diff if the committed corpus is stale",
    )
    args = parser.parse_args(argv)

    server = load_server_codec()
    fresh = render_corpus(server)
    target = corpus_path_for(server)
    current = target.read_text(encoding="utf-8") if target.exists() else None

    if args.check:
        if current == fresh:
            print("corpus current")
            return 0
        rel = target.relative_to(REPO_ROOT).as_posix()
        if current is None:
            print(f"corpus missing: {rel}", file=sys.stderr)
        else:
            print(f"corpus stale: {rel}", file=sys.stderr)
            sys.stderr.writelines(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    fresh.splitlines(keepends=True),
                    fromfile=f"committed/{rel}",
                    tofile="fresh export",
                )
            )
        print(
            f"\nrun: python {Path(__file__).relative_to(REPO_ROOT).as_posix()} "
            "and commit the result",
            file=sys.stderr,
        )
        return 1

    if current == fresh:
        print(f"corpus current: {target.relative_to(REPO_ROOT).as_posix()}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the bytes are exactly `fresh` on every platform; the
    # currency test compares bytes, and a CRLF from a Windows text-mode
    # write would make a freshly generated file "stale".
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(fresh)
    verb = "updated" if current is not None else "wrote"
    print(f"{verb} {target.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
