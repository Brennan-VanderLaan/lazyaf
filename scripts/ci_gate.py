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
import time
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


#: A junitxml older than this is refused as stale. T1 - the longest tier -
#: measures 11 to 13 minutes, so this is roughly 2.4x the worst observed run
#: plus the gate's own invocation. Deliberately NOT an hour: the stale reports
#: that actually fooled people were 40 to 90 minutes old, and a threshold that
#: admits those admits the bug.
DEFAULT_MAX_AGE_SECONDS = 1800


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", nargs="+", type=Path, help="junitxml file(s) for this tier")
    parser.add_argument("--tier", required=True, help="tier name, e.g. T1")
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--floors", type=Path, default=DEFAULT_FLOORS)
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=DEFAULT_MAX_AGE_SECONDS,
        help=(
            "refuse a junitxml older than this (default "
            f"{DEFAULT_MAX_AGE_SECONDS}s). 0 disables the check."
        ),
    )
    args = parser.parse_args(argv)

    # A junitxml is written at the END of a pytest session, so a run that never
    # STARTS - a bad plugin, an import error, a stray `--help`, a killed
    # process - writes nothing and leaves the PREVIOUS run's file in place.
    # This gate then reads hours-old results and calls them today's.
    #
    # That is not hypothetical: `run_tier.py T1 -- --help` printed
    # "CI GATE [T1]: OK - executed=4836" having executed nothing, and three
    # separate agents were fooled by the same shape while it went unnoticed.
    # A gate that cannot tell "the suite passed" from "the suite never ran" is
    # not a gate, and every green number that rests on it is unearned.
    if args.max_age_seconds > 0:
        now = time.time()
        for path in args.xml:
            if not path.exists():
                continue  # the read below reports a missing file properly
            age = now - path.stat().st_mtime
            if age > args.max_age_seconds:
                print(
                    f"CI GATE [{args.tier}]: FAIL - {path} is {age / 60:.0f} "
                    f"minutes old, older than the {args.max_age_seconds}s "
                    "limit. It is almost certainly a PREVIOUS run's report: "
                    "pytest writes this file when a session ENDS, so a run "
                    "that failed to start leaves the old one behind. Re-run "
                    "the tier. (Pass --max-age-seconds 0 only if you really "
                    "mean to gate on an archived report.)",
                    file=sys.stderr,
                )
                return 1

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
    # Defense in depth. run_tier.py returns before invoking the gate when
    # pytest is red, so in the pipeline a failure never reaches here - but a
    # human or an agent invoking ci_gate directly on a junitxml used to read
    # "GATE OK" while the summary line said failed=3, which is exactly the
    # fake-green this tool exists to prevent. The gate now refuses red input
    # itself, so no caller can mistake it for a pass.
    if counts["failed"] or counts["errors"]:
        failed = True
        print(
            f"CI GATE [{args.tier}]: FAIL - {counts['failed']} failed / "
            f"{counts['errors']} error(s) in the junitxml. A gate can never "
            f"report OK on a red suite, however it was invoked.",
            file=sys.stderr,
        )
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
