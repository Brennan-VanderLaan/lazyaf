#!/usr/bin/env python3
"""CI gate for the tiered dogfood suite (standing rule R4: no fake green).

Parses pytest junitxml output for one tier and fails (exit 1) when:
  (a) any skipped test's reason does not match an allowlisted prefix in
      tdd/skip_baseline.json — a new skip must be baselined in the same
      commit that introduces it, and un-skipping shrinks the baseline; or
  (b) the tier's executed count (passed + failed) is below its committed
      floor in tdd/tier_floors.json — catches whole files silently
      self-skipping (the failure_01 decay mode) even when reasons match.

Stdlib only: runs on the bare python3 inside a Linux runner container,
before any dependency sync has to have succeeded.

Usage:
    python3 scripts/ci_gate.py --tier T1 junit-t1.xml [more.xml ...]
"""
import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = REPO_ROOT / "tdd" / "skip_baseline.json"
DEFAULT_FLOORS = REPO_ROOT / "tdd" / "tier_floors.json"

# pytest writes module-level skips (importorskip / skipif at collection) with
# this generic message; the real reason lives in the element body as
# "('path', lineno, 'Skipped: <reason>')".
_COLLECTION_MSG = "collection skipped"
_BODY_REASON_RE = re.compile(r"Skipped: (.*)", re.DOTALL)


def extract_skip_reason(skipped_el) -> str:
    """Return the human-authored skip reason for a <skipped> element."""
    message = skipped_el.get("message") or ""
    if message and message != _COLLECTION_MSG:
        return message
    body = skipped_el.text or ""
    match = _BODY_REASON_RE.search(body)
    if match:
        # Trim the tuple-repr tail pytest leaves after the reason.
        return match.group(1).rstrip("\"')\n ")
    return message or body.strip()


def tally(xml_paths: list[Path]):
    """Aggregate testcase outcomes across one tier's junitxml files."""
    counts = {"passed": 0, "failed": 0, "errors": 0, "xfailed": 0}
    skips = []  # (test_id, reason)
    for path in xml_paths:
        if not path.is_file():
            raise FileNotFoundError(f"junitxml not found: {path}")
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
            if case.find("error") is not None:
                counts["errors"] += 1
            elif case.find("failure") is not None:
                counts["failed"] += 1
            elif (skipped := case.find("skipped")) is not None:
                if skipped.get("type") == "pytest.xfail":
                    counts["xfailed"] += 1
                else:
                    skips.append((test_id, extract_skip_reason(skipped)))
            else:
                counts["passed"] += 1
    return counts, skips


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", nargs="+", type=Path, help="junitxml file(s) for this tier")
    parser.add_argument("--tier", required=True, help="tier name, e.g. T1")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    floors = json.loads(args.floors.read_text(encoding="utf-8"))
    prefixes = [entry["reason_prefix"] for entry in baseline]

    if args.tier not in floors:
        print(f"CI GATE [{args.tier}]: FAIL - tier '{args.tier}' has no floor in {args.floors}", file=sys.stderr)
        return 1
    floor = floors[args.tier]["floor"]

    try:
        counts, skips = tally(args.xml)
    except (FileNotFoundError, ET.ParseError) as exc:
        print(f"CI GATE [{args.tier}]: FAIL - cannot read results: {exc}", file=sys.stderr)
        return 1

    violations = [
        (test_id, reason)
        for test_id, reason in skips
        if not any(reason.startswith(prefix) for prefix in prefixes)
    ]
    executed = counts["passed"] + counts["failed"]

    summary = (
        f"executed={executed} (floor={floor}) passed={counts['passed']} "
        f"failed={counts['failed']} errors={counts['errors']} "
        f"skipped={len(skips)} xfailed={counts['xfailed']}"
    )

    failed = False
    if violations:
        failed = True
        print(f"CI GATE [{args.tier}]: FAIL - {len(violations)} skip(s) not in tdd/skip_baseline.json:", file=sys.stderr)
        for test_id, reason in violations:
            print(f"  {test_id}: {reason!r}", file=sys.stderr)
        print("  Either fix the skip or baseline it (with a note) in the same commit.", file=sys.stderr)
    if executed < floor:
        failed = True
        print(
            f"CI GATE [{args.tier}]: FAIL - executed count {executed} below committed floor {floor} "
            f"(tdd/tier_floors.json). Tests are silently not running.",
            file=sys.stderr,
        )

    if failed:
        print(f"CI GATE [{args.tier}]: {summary}", file=sys.stderr)
        return 1
    print(f"CI GATE [{args.tier}]: OK - {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
