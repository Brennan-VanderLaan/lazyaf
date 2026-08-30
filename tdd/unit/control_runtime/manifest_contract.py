"""THE test-results manifest wire contract (Phase 12.2.6, cross-agent #2).

ONE module, imported by BOTH sides of the wire, so a drift on either side
fails a test that names the side that drifted:

- PRODUCER  = runner_common.pytest_lazyaf (writes the manifest file)
- CONSUMER  = images/base/control/run.py  (validates + POSTs it)
- SERVER    = backend /api/steps/{id}/test-results (parses it)

The pinned shape is EXACTLY::

    {"version": 1,
     "results": [{"lazyaf_test_id": str,      # non-empty
                  "status": "passed"|"failed"|"skipped",
                  "duration_ms": int | None,
                  "file_path": str | None}]}  # REPO-ROOT-relative, "/" sep

No extra top-level keys, no extra result keys, no other statuses.

This module is NOT a test module (it must not match ``python_files``); it is
plain importable code:

    from tdd.unit.control_runtime.manifest_contract import (
        CANONICAL_MANIFEST, assert_manifest_conforms,
    )
"""

MANIFEST_VERSION = 1

#: Exactly the allowed top-level keys.
TOP_LEVEL_KEYS = frozenset({"version", "results"})

#: Exactly the allowed keys of one result entry.
RESULT_KEYS = frozenset({"lazyaf_test_id", "status", "duration_ms", "file_path"})

#: Exactly the allowed statuses (PLAN open question #2: skipped is
#: informational; an errored test maps to "failed" — there is no "error").
STATUSES = frozenset({"passed", "failed", "skipped"})

#: A manifest that every side MUST accept, byte-shape included.
CANONICAL_MANIFEST = {
    "version": 1,
    "results": [
        {
            "lazyaf_test_id": "contract.canonical.passed",
            "status": "passed",
            "duration_ms": 12,
            "file_path": "tdd/unit/control_runtime/test_contract.py",
        },
        {
            "lazyaf_test_id": "contract.canonical.failed",
            "status": "failed",
            "duration_ms": 0,
            "file_path": "tdd/unit/control_runtime/test_contract.py",
        },
        {
            "lazyaf_test_id": "contract.canonical.skipped",
            "status": "skipped",
            "duration_ms": None,
            "file_path": None,
        },
    ],
}


def manifest_violations(manifest) -> list:
    """Return a list of human-readable contract violations (empty == valid)."""
    problems = []
    if not isinstance(manifest, dict):
        return [f"manifest is {type(manifest).__name__}, expected dict"]

    keys = set(manifest)
    if keys != set(TOP_LEVEL_KEYS):
        problems.append(
            f"top-level keys {sorted(keys)} != {sorted(TOP_LEVEL_KEYS)}"
        )
    if manifest.get("version") != MANIFEST_VERSION:
        problems.append(
            f"version {manifest.get('version')!r} != {MANIFEST_VERSION}"
        )

    results = manifest.get("results")
    if not isinstance(results, list):
        problems.append(
            f"results is {type(results).__name__}, expected list"
        )
        return problems

    for i, entry in enumerate(results):
        if not isinstance(entry, dict):
            problems.append(
                f"results[{i}] is {type(entry).__name__}, expected dict"
            )
            continue
        ekeys = set(entry)
        if ekeys != set(RESULT_KEYS):
            problems.append(
                f"results[{i}] keys {sorted(ekeys)} != {sorted(RESULT_KEYS)}"
            )
        test_id = entry.get("lazyaf_test_id")
        if not isinstance(test_id, str) or not test_id:
            problems.append(
                f"results[{i}].lazyaf_test_id {test_id!r} is not a non-empty str"
            )
        status = entry.get("status")
        if status not in STATUSES:
            problems.append(
                f"results[{i}].status {status!r} not in {sorted(STATUSES)}"
            )
        duration = entry.get("duration_ms")
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool)
        ):
            problems.append(
                f"results[{i}].duration_ms {duration!r} is not int|None"
            )
        file_path = entry.get("file_path")
        if file_path is not None and not isinstance(file_path, str):
            problems.append(
                f"results[{i}].file_path {file_path!r} is not str|None"
            )
        elif isinstance(file_path, str) and "\\" in file_path:
            problems.append(
                f"results[{i}].file_path {file_path!r} uses '\\\\' — the "
                "contract is '/'-separated, repo-root-relative"
            )
    return problems


def assert_manifest_conforms(manifest, side: str) -> None:
    """Assert ``manifest`` matches the wire contract.

    ``side`` names WHO produced this value ("PRODUCER (pytest plugin)",
    "CONSUMER (control runtime)", "SERVER (test-results router)") so a
    failure message says which side drifted from the shared contract.
    """
    problems = manifest_violations(manifest)
    assert not problems, (
        f"{side} DRIFTED from the 12.2.6 manifest contract "
        f"(tdd/unit/control_runtime/manifest_contract.py):\n  - "
        + "\n  - ".join(problems)
        + f"\noffending value: {manifest!r}"
    )


#: Values NO side may accept as a valid manifest. Each is (label, value).
INVALID_MANIFESTS = [
    ("bare list", [{"lazyaf_test_id": "a", "status": "passed"}]),
    ("bare string", "not a manifest"),
    ("null", None),
    ("results is a dict", {"version": 1, "results": {"a": "passed"}}),
    ("results is a string", {"version": 1, "results": "passed"}),
    ("results missing", {"version": 1}),
    (
        "entry is not a dict",
        {"version": 1, "results": ["just-a-string"]},
    ),
    (
        "entry missing lazyaf_test_id",
        {"version": 1, "results": [{"status": "passed"}]},
    ),
    (
        "entry id is not a string",
        {
            "version": 1,
            "results": [
                {
                    "lazyaf_test_id": 17,
                    "status": "passed",
                    "duration_ms": 1,
                    "file_path": None,
                }
            ],
        },
    ),
    (
        "unknown status",
        {
            "version": 1,
            "results": [
                {
                    "lazyaf_test_id": "a",
                    "status": "errored",
                    "duration_ms": 1,
                    "file_path": None,
                }
            ],
        },
    ),
]
