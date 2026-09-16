
# Throwaway collector: dump every lazyaf_test_id-marked test to JSON.
import json
import os

OUT = os.environ["LAZYAF_COLLECT_OUT"]
ROOT = os.environ.get("LAZYAF_COLLECT_ROOT") or ""


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "lazyaf_test_id(id): LazyAF test tie-back identifier"
    )


def _relativize(path):
    # REPO-ROOT-relative with "/" separators (cross-agent contract #3).
    # A relpath that ESCAPES the root (8.3 short names, symlinks, a suite
    # outside the repo) is worse than no root at all - fall back to the
    # invocation dir, then to the raw path, rather than emitting a
    # "../../.." climb that matches nothing the server ever seeds.
    real = os.path.realpath(path)
    for base in (ROOT, os.getcwd()):
        if not base:
            continue
        try:
            rel = os.path.relpath(real, os.path.realpath(base))
        except ValueError:
            continue
        if not rel.startswith(".."):
            return rel.replace(os.sep, "/")
    return path.replace(os.sep, "/")


def pytest_collection_finish(session):
    refs = {}
    for item in session.items:
        marker = item.get_closest_marker("lazyaf_test_id")
        if marker is None or not marker.args:
            continue
        test_id = marker.args[0]
        if not isinstance(test_id, str) or not test_id:
            continue
        path = str(getattr(item, "fspath", "") or "")
        path = _relativize(path) if path else ""
        refs.setdefault(
            test_id, {"lazyaf_test_id": test_id, "file_path": path or None}
        )
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"refs": list(refs.values())}, fh)
