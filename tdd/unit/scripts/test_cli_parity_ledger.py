"""Enforcement A of the parity ledger (upcoming/go-cli.md section 11, R2).

`tdd/contracts/cli_parity.json` says where every Python CLI test went before
the P4 deletion commit removes it: either to named Go tests (`go`) or out of
the suite with a reason (`retired`). This file is the Python half of the
machine check, and it lives only while both CLIs are in the tree - it is
deleted in the same commit as the classes it counts, and the tombstone
(`tdd/unit/services/test_python_cli_retired.py`) freezes the enumeration then.

What it proves, with NO imports of the files it reads (they need click, rich
and websockets, which the backend env deliberately does not carry; `ast.parse`
needs nothing):

  * the ledger's keys are EXACTLY the test classes (module-level functions for
    the class-less test_bootstrap_secrets.py) in the files the deletion list
    names - a class added without an entry fails, a stale key fails;
  * every `go` reference resolves to a real `func TestX(t *testing.T)` under
    cli/**/*_test.go, and every `/subtest` to a `t.Run("...")` literal inside
    it - a dangling reference fails naming it;
  * every `retired` reason has a kind from the closed set and at least 40
    characters, and any Go test it names by `package.TestX` exists too;
  * the "survivors" (the corpus half of the contract test, which outlives P4)
    are the classes that do NOT touch the Python client, and the ledgered
    ones are the classes that DO.

The checker is a pure function over three in-memory structures so the
negatives below can prove it catches each failure shape (R4: a test that only
ever sees a green ledger has not shown it can go red).

The Go half, `cli/internal/parity/ledger_test.go`, resolves the same
references with go/parser and is permanent: it keeps a carrier from being
deleted after the Python side is gone.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
LEDGER = REPO_ROOT / "tdd" / "contracts" / "cli_parity.json"
CLI_ROOT = REPO_ROOT / "cli"

# The deletion list, section 13.5. A file here with no ledger entries fails
# (its tests went nowhere); a ledger key naming a file outside it fails (the
# ledger is not a place to park unrelated tests). tdd/unit/packaging/ is the
# whole directory, so its members are globbed rather than listed.
LEDGER_FILES = sorted(
    [
        "tdd/unit/scripts/test_cli_debug.py",
        "tdd/unit/scripts/test_cli_errors.py",
        "tdd/unit/scripts/test_cli_tests_reconcile.py",
        "tdd/unit/scripts/test_bootstrap_secrets.py",
        "tdd/unit/scripts/test_preflight.py",
        "tdd/unit/debug/test_terminal_protocol_contract.py",
    ]
    + sorted(p.relative_to(REPO_ROOT).as_posix() for p in (REPO_ROOT / "tdd" / "unit" / "packaging").glob("test_*.py"))
)

# The closed set of retirement kinds (section 11). Held here and in the Go
# test, NOT in the JSON, so the set cannot be widened by editing the ledger.
RETIREMENT_KINDS = frozenset(
    {
        "rich-specific",
        "click-specific",
        "python-websockets-specific",
        "packaging-wheel",
        "python-interpreter",
        "semantics-changed",
        "superseded-by-contract",
        "moved-to-e2e",
    }
)
MIN_REASON_CHARS = 40

# "<package>.<TestFunc>" or "<package>.<TestFunc>/<subtest>", where package is
# the directory under cli/internal (see go_tests()).
GO_REF = re.compile(r"^([a-z][a-z0-9_]*)\.(Test[A-Za-z0-9_]+)(?:/([^/]+))?$")
# A Go test named inside prose: "debugproto.TestContractConstants". Python
# test ids ("test_x.py::TestY") do not match because "py" is followed by "::".
GO_NAME_IN_PROSE = re.compile(r"\b([a-z][a-z0-9_]*)\.(Test[A-Za-z0-9_]+)\b")
GO_FUNC = re.compile(r"^func (Test[A-Za-z0-9_]+)\(t \*testing\.T\)", re.MULTILINE)
GO_SUBTEST = re.compile(r't\.Run\("((?:[^"\\]|\\.)*)"')


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def load_ledger(path: Path = LEDGER) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def python_tests(rel_path: str, root: Path = REPO_ROOT) -> dict:
    """What pytest would collect from ONE file, by AST, plus who touches the
    Python client.

    Returns {"names": [..], "kind": "classes"|"functions",
             "touches_client": {name: bool}}. A file with `class Test*` is
    keyed by class; only a class-less file is keyed by its module-level
    `def test_*`, so nothing pytest collects can escape the ledger - a file
    mixing both shapes is refused outright.
    """
    source = (root / rel_path).read_text(encoding="utf-8")
    module = ast.parse(source, filename=rel_path)
    client_aliases = set()
    for node in module.body:
        if isinstance(node, ast.ImportFrom) and node.module == "lazyaf":
            for alias in node.names:
                client_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "lazyaf" or alias.name.startswith("lazyaf."):
                    client_aliases.add((alias.asname or alias.name).split(".")[0])

    classes = [n for n in module.body if isinstance(n, ast.ClassDef) and n.name.startswith("Test")]
    functions = [
        n
        for n in module.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")
    ]
    if classes and functions:
        raise AssertionError(
            f"{rel_path} mixes test classes and module-level test functions; the ledger keys one "
            f"shape per file, so split the file or move the functions into a class: "
            f"{[f.name for f in functions]}"
        )
    nodes = classes or functions

    def touches(node: ast.AST) -> bool:
        return any(isinstance(sub, ast.Name) and sub.id in client_aliases for sub in ast.walk(node))

    return {
        "names": [n.name for n in nodes],
        "kind": "classes" if classes else "functions",
        "touches_client": {n.name: touches(n) for n in nodes},
    }


def go_tests(cli_root: Path = CLI_ROOT) -> dict:
    """{package: {TestFunc: {subtest, ...}}} over every *_test.go under cli/.

    `package` is the directory under cli/internal/ (or the path relative to
    cli/ for anything outside it) - NOT the `package` clause, so reconcile's
    external `package reconcile_test` is keyed `reconcile`, which is also how
    gotestsum's junit `classname` reads. Subtests are the `t.Run("...")`
    literals between one `func TestX(` and the next top-level `func`,
    rewritten the way Go's testing package reports them (space -> `_`).
    Table-driven subtests (`t.Run(tc.name, ...)`) are not literals and cannot
    be referenced; the parent test is the reference then.
    """
    found: dict = {}
    for path in sorted(cli_root.rglob("*_test.go")):
        rel = path.relative_to(cli_root)
        parts = rel.parts
        package = parts[-2] if len(parts) >= 3 and parts[0] == "internal" else "/".join(parts[:-1])
        text = path.read_text(encoding="utf-8")
        funcs = list(GO_FUNC.finditer(text))
        boundaries = [m.start() for m in re.finditer(r"^func ", text, re.MULTILINE)] + [len(text)]
        for match in funcs:
            start = match.start()
            end = min(b for b in boundaries if b > start)
            body = text[start:end]
            subtests = {name.replace(" ", "_") for name in GO_SUBTEST.findall(body)}
            found.setdefault(package, {}).setdefault(match.group(1), set()).update(subtests)
    return found


# ---------------------------------------------------------------------------
# The checker
# ---------------------------------------------------------------------------


def resolve_go(ref: str, go: dict) -> str | None:
    """None when `ref` names a real Go test, else the reason it does not."""
    match = GO_REF.match(ref)
    if not match:
        return f"{ref!r} is not <package>.<TestFunc>[/<subtest>]"
    package, func, subtest = match.groups()
    if package not in go:
        return f"{ref}: no *_test.go under cli/internal/{package}"
    if func not in go[package]:
        return f"{ref}: no `func {func}(t *testing.T)` in cli/internal/{package}"
    if subtest is not None and subtest not in go[package][func]:
        return (
            f"{ref}: no t.Run({subtest!r}) literal inside {package}.{func} "
            f"(known: {sorted(go[package][func])})"
        )
    return None


def problems(ledger: dict, python: dict, go: dict) -> list:
    """Every way the ledger can be wrong, as one list of sentences.

    `python` is {rel_path: python_tests(rel_path)} for every file in
    LEDGER_FILES; `go` is go_tests(). An empty list is a ledger the deletion
    commit may act on.
    """
    out: list = []
    entries = ledger.get("entries")
    survivors = ledger.get("survivors", {})
    if not isinstance(entries, dict) or not entries:
        return ["the ledger has no 'entries' object"]

    # Keys are file::Name, over exactly the deletion list.
    keyed: dict = {}
    for key in entries:
        if "::" not in key:
            out.append(f"key {key!r} is not file::Name")
            continue
        rel, name = key.split("::", 1)
        keyed.setdefault(rel, set()).add(name)
    for rel in sorted(keyed):
        if rel not in python:
            out.append(f"{rel} is in the ledger but not in the deletion list (section 13.5)")
    for rel in sorted(survivors):
        if rel not in python:
            out.append(f"survivors names {rel}, which is not a ledger file")

    for rel, collected in sorted(python.items()):
        have = keyed.get(rel, set())
        keep = set(survivors.get(rel, []))
        want = set(collected["names"])
        overlap = have & keep
        for name in sorted(overlap):
            out.append(f"{rel}::{name} is both a ledger entry and a survivor; pick one")
        for name in sorted(want - have - keep):
            out.append(
                f"{rel}::{name} is collected by pytest but has no ledger entry "
                f"(add a 'go' list or a 'retired' reason to tdd/contracts/cli_parity.json)"
            )
        for name in sorted((have | keep) - want):
            out.append(f"{rel}::{name} is in the ledger but no such {collected['kind'][:-2]} exists in the file")
        # Survivors are the classes that do NOT import the Python client;
        # everything ledgered in a file WITH survivors must.
        if keep:
            touches = collected["touches_client"]
            for name in sorted(keep & want):
                if touches.get(name):
                    out.append(
                        f"{rel}::{name} is listed as a survivor but references the Python client; "
                        f"it will not survive the deletion commit"
                    )
            for name in sorted(have & want):
                if not touches.get(name):
                    out.append(
                        f"{rel}::{name} has a ledger entry but never touches the Python client; "
                        f"if it stays after P4 it belongs in survivors"
                    )

    # Values: exactly one of go / retired, optional note, nothing else.
    for key, value in sorted(entries.items()):
        if not isinstance(value, dict):
            out.append(f"{key}: value is not an object")
            continue
        unknown = set(value) - {"go", "retired", "note"}
        if unknown:
            out.append(f"{key}: unknown field(s) {sorted(unknown)}")
        if ("go" in value) == ("retired" in value):
            out.append(f"{key}: needs exactly one of 'go' or 'retired'")
            continue
        if "note" in value and (not isinstance(value["note"], str) or not value["note"].strip()):
            out.append(f"{key}: 'note' must be a non-empty string")
        if "go" in value:
            refs = value["go"]
            if not isinstance(refs, list) or not refs or not all(isinstance(r, str) for r in refs):
                out.append(f"{key}: 'go' must be a non-empty list of strings")
                continue
            if len(set(refs)) != len(refs):
                out.append(f"{key}: duplicate go references")
            for ref in refs:
                why = resolve_go(ref, go)
                if why:
                    out.append(f"{key}: {why}")
        else:
            reason = value["retired"]
            if not isinstance(reason, str):
                out.append(f"{key}: 'retired' must be a string")
                continue
            kind, sep, rest = reason.partition(": ")
            if not sep or kind not in RETIREMENT_KINDS:
                out.append(
                    f"{key}: retired kind {kind!r} is not one of {sorted(RETIREMENT_KINDS)} "
                    f"(the value must read '<kind>: <reason>')"
                )
            if len(rest.strip()) < MIN_REASON_CHARS:
                out.append(f"{key}: the retirement reason is under {MIN_REASON_CHARS} characters")
            if re.search(r"\bTBD\b|\bTODO\b", reason):
                out.append(f"{key}: a retirement reason may not be TBD/TODO")
            for package, func in GO_NAME_IN_PROSE.findall(reason):
                why = resolve_go(f"{package}.{func}", go)
                if why:
                    out.append(f"{key}: the reason names a Go test that does not exist - {why}")
    return out


# ---------------------------------------------------------------------------
# Fixtures over the real tree
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ledger() -> dict:
    assert LEDGER.exists(), f"{LEDGER} is missing; the parity gate (section 11) has nothing to check"
    return load_ledger()


@pytest.fixture(scope="module")
def python() -> dict:
    for rel in LEDGER_FILES:
        assert (REPO_ROOT / rel).exists(), (
            f"{rel} is gone but this test still exists; P4 deletes both together (section 13.5)"
        )
    return {rel: python_tests(rel) for rel in LEDGER_FILES}


@pytest.fixture(scope="module")
def go() -> dict:
    found = go_tests()
    assert found, f"no *_test.go under {CLI_ROOT}; the Go CLI is not in this tree"
    return found


# ---------------------------------------------------------------------------
# The real ledger
# ---------------------------------------------------------------------------


class TestTheLedgerIsComplete:
    def test_the_ledger_has_no_problems(self, ledger, python, go):
        found = problems(ledger, python, go)
        assert not found, "the parity ledger is not fit to gate a deletion:\n  " + "\n  ".join(found)

    def test_every_deletion_list_file_is_represented(self, ledger, python):
        files = {key.split("::", 1)[0] for key in ledger["entries"]}
        assert files == set(LEDGER_FILES), (
            f"missing: {sorted(set(LEDGER_FILES) - files)}; unexpected: {sorted(files - set(LEDGER_FILES))}"
        )

    def test_the_bootstrap_file_is_keyed_per_function(self, python):
        """The one class-less file (section 11); a class added there would
        silently switch the file to class keys and orphan every function."""
        assert python["tdd/unit/scripts/test_bootstrap_secrets.py"]["kind"] == "functions"
        for rel, collected in python.items():
            if rel != "tdd/unit/scripts/test_bootstrap_secrets.py":
                assert collected["kind"] == "classes", rel

    def test_the_go_index_saw_every_package_the_ledger_names(self, ledger, go):
        named = {GO_REF.match(r).group(1) for v in ledger["entries"].values() for r in v.get("go", [])}
        assert named <= set(go), sorted(named - set(go))

    def test_the_share_is_reported(self, ledger, capsys):
        """The count the plan's reviewer reads: how much went to Go and how
        much was retired, printed past pytest's capture so `-q` shows it."""
        entries = ledger["entries"]
        ported = sum("go" in v for v in entries.values())
        retired = sum("retired" in v for v in entries.values())
        kinds: dict = {}
        for value in entries.values():
            if "retired" in value:
                kind = value["retired"].split(": ", 1)[0]
                kinds[kind] = kinds.get(kind, 0) + 1
        with capsys.disabled():
            print(
                f"\ncli_parity.json: {len(entries)} entries - {ported} go, {retired} retired "
                f"({', '.join(f'{k} {n}' for k, n in sorted(kinds.items()))})"
            )
        assert ported + retired == len(entries)
        assert ported > retired, "more Python tests were retired than ported; that is not a port"


# ---------------------------------------------------------------------------
# The checker itself, against ledgers built to be wrong (R4)
# ---------------------------------------------------------------------------


def _minimal_python() -> dict:
    return {
        "x/test_a.py": {"names": ["TestOne", "TestTwo"], "kind": "classes", "touches_client": {}},
    }


def _minimal_go() -> dict:
    return {"cmd": {"TestLand": {"a_failed_pr_does_not_report_success"}}}


def _entries(**overrides) -> dict:
    base = {
        "x/test_a.py::TestOne": {"go": ["cmd.TestLand", "cmd.TestLand/a_failed_pr_does_not_report_success"]},
        "x/test_a.py::TestTwo": {"retired": "rich-specific: " + "no markup parser exists in the binary at all now"},
    }
    base.update(overrides)
    return {"entries": base}


class TestTheCheckerCatchesEachFailureShape:
    def test_a_correct_ledger_has_no_problems(self):
        assert problems(_entries(), _minimal_python(), _minimal_go()) == []

    def test_a_class_without_an_entry_is_named(self):
        python = _minimal_python()
        python["x/test_a.py"]["names"].append("TestThree")
        found = problems(_entries(), python, _minimal_go())
        assert any("x/test_a.py::TestThree is collected by pytest but has no ledger entry" in p for p in found)

    def test_a_stale_key_is_named(self):
        ledger = _entries(**{"x/test_a.py::TestGone": {"go": ["cmd.TestLand"]}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("x/test_a.py::TestGone is in the ledger but no such class exists" in p for p in found)

    def test_a_dangling_go_function_is_named(self):
        ledger = _entries(**{"x/test_a.py::TestOne": {"go": ["cmd.TestNope"]}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("cmd.TestNope: no `func TestNope(t *testing.T)`" in p for p in found)

    def test_a_dangling_subtest_is_named(self):
        ledger = _entries(**{"x/test_a.py::TestOne": {"go": ["cmd.TestLand/no_such_subtest"]}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("no t.Run('no_such_subtest') literal inside cmd.TestLand" in p for p in found)

    def test_an_unknown_package_is_named(self):
        ledger = _entries(**{"x/test_a.py::TestOne": {"go": ["nowhere.TestLand"]}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("no *_test.go under cli/internal/nowhere" in p for p in found)

    def test_a_tbd_is_refused(self):
        ledger = _entries(**{"x/test_a.py::TestTwo": {"retired": "rich-specific: TBD, will decide later when the port is done"}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("may not be TBD" in p for p in found)

    def test_a_kind_outside_the_closed_set_is_refused(self):
        ledger = _entries(**{"x/test_a.py::TestTwo": {"retired": "just-because: " + "x" * 50}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("retired kind 'just-because' is not one of" in p for p in found)

    def test_a_short_reason_is_refused(self):
        ledger = _entries(**{"x/test_a.py::TestTwo": {"retired": "rich-specific: too short"}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("under 40 characters" in p for p in found)

    def test_a_reason_naming_a_missing_go_test_is_refused(self):
        ledger = _entries(
            **{"x/test_a.py::TestTwo": {"retired": "semantics-changed: replaced by cmd.TestImaginary which covers the same"}}
        )
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("names a Go test that does not exist" in p and "cmd.TestImaginary" in p for p in found)

    def test_both_go_and_retired_is_refused(self):
        ledger = _entries(**{"x/test_a.py::TestOne": {"go": ["cmd.TestLand"], "retired": "rich-specific: " + "y" * 50}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("needs exactly one of 'go' or 'retired'" in p for p in found)

    def test_an_empty_go_list_is_refused(self):
        ledger = _entries(**{"x/test_a.py::TestOne": {"go": []}})
        found = problems(ledger, _minimal_python(), _minimal_go())
        assert any("'go' must be a non-empty list" in p for p in found)

    def test_a_survivor_that_touches_the_client_is_refused(self):
        python = _minimal_python()
        python["x/test_a.py"]["names"].append("TestKept")
        python["x/test_a.py"]["touches_client"] = {"TestOne": True, "TestTwo": True, "TestKept": True}
        ledger = _entries()
        ledger["survivors"] = {"x/test_a.py": ["TestKept"]}
        found = problems(ledger, python, _minimal_go())
        assert any("TestKept is listed as a survivor but references the Python client" in p for p in found)

    def test_an_entry_that_never_touches_the_client_beside_survivors_is_flagged(self):
        python = _minimal_python()
        python["x/test_a.py"]["names"].append("TestKept")
        python["x/test_a.py"]["touches_client"] = {"TestOne": True, "TestTwo": False, "TestKept": False}
        ledger = _entries()
        ledger["survivors"] = {"x/test_a.py": ["TestKept"]}
        found = problems(ledger, python, _minimal_go())
        assert any("TestTwo has a ledger entry but never touches the Python client" in p for p in found)


class TestTheCollectorsReadTheRealTree:
    def test_python_collection_matches_a_known_file(self):
        collected = python_tests("tdd/unit/scripts/test_cli_errors.py")
        assert collected["kind"] == "classes"
        assert "TestLand" in collected["names"] and "TestAgainstRealRich" in collected["names"]

    def test_the_contract_file_splits_on_the_client_import(self):
        collected = python_tests("tdd/unit/debug/test_terminal_protocol_contract.py")
        touches = collected["touches_client"]
        assert touches["TestConstantParity"] is True
        assert touches["TestCorpusIsCurrent"] is False

    def test_go_collection_keys_reconcile_by_directory_not_package_clause(self, go):
        """`package reconcile_test` (external) is still `reconcile` here."""
        assert "TestFromCollect" in go["reconcile"]

    def test_go_subtests_are_rewritten_like_the_testing_package(self, go):
        """A t.Run("http becomes ws") is reported as http_becomes_ws by
        `go test -v` and gotestsum alike; the ledger uses that spelling."""
        assert "http_becomes_ws" in go["terminal"]["TestTerminalURL"]

    def test_test_main_is_not_a_test(self, go):
        assert "TestMain" not in go.get("initcmd", {})

    def test_a_synthetic_tree_is_indexed_the_same_way(self, tmp_path):
        pkg = tmp_path / "internal" / "pkgx"
        pkg.mkdir(parents=True)
        (pkg / "x_test.go").write_text(
            'package pkgx\n\nimport "testing"\n\n'
            "func TestMain(m *testing.M) {}\n\n"
            "func TestA(t *testing.T) {\n"
            '\tt.Run("one two", func(t *testing.T) {})\n'
            "\tt.Run(name, func(t *testing.T) {})\n"
            "}\n\n"
            "func helper() {}\n\n"
            "func TestB(t *testing.T) {\n"
            '\tt.Run("three", func(t *testing.T) {})\n'
            "}\n",
            encoding="utf-8",
        )
        assert go_tests(tmp_path) == {"pkgx": {"TestA": {"one_two"}, "TestB": {"three"}}}
