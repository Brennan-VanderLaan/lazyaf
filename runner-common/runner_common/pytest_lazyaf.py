"""
pytest-lazyaf — collect ``lazyaf_test_id``-marked outcomes into a manifest.

Phase 12.2.6 test tie-back, plugin side. Tests declare a stable identifier:

    @pytest.mark.lazyaf_test_id("auth.revoke_key.returns_401")
    def test_revoked_key_returns_401(): ...

When the environment variable ``LAZYAF_TEST_RESULTS_PATH`` is set (the control
runtime injects a per-step path, see images/base/control/run.py), the plugin
records the outcome of every MARKED test and writes the manifest ATOMICALLY
(temp file + os.replace in the destination directory) at session finish:

    {"version": 1, "results": [{"lazyaf_test_id": str,
                                "status": "passed"|"failed"|"skipped",
                                "duration_ms": int|null,
                                "file_path": str|null}]}

When the env var is unset the plugin is a pure no-op apart from registering
the marker (so annotated tests still collect warning-free, including under
``--strict-markers``).

file_path CONVENTION (cross-agent contract #3): REPO-ROOT-relative with "/"
separators — NOT pytest-rootdir-relative. In this repo pytest's rootdir is
``tdd/`` (tdd/pytest.ini, invoked as ``cd backend && pytest ../tdd``), so
``item.location[0]`` would emit ``unit/foo/test_x.py`` while every other
LazyAF surface (CLI seeds, MCP, the TestRef rows) speaks
``tdd/unit/foo/test_x.py``. The repo root is found by WALKING UP from the
test file for a ``.git`` marker (works for any checkout, host or container,
without the runtime having to tell us anything). ``LAZYAF_REPO_ROOT`` is an
explicit override for checkouts with no ``.git`` (exported tarballs); when
neither resolves, the plugin falls back to the rootdir-relative path rather
than emitting nothing.

NOTHING here may fail a test run: the manifest write swallows every
exception (logged to stderr) — a step's tie-back telemetry must never turn
a green suite red (mirror of the control runtime's rule that a malformed
manifest can never stop terminal status reporting).

LOADING DECISION (documented per the 12.2.6 task): NO setuptools entry point.
The plugin is loaded EXPLICITLY with ``-p runner_common.pytest_lazyaf``.
An entry point would auto-activate the plugin in every pytest environment
that happens to have runner-common installed (runner-common's own suite,
agent images, arbitrary user repos) — explicit ``-p`` keeps activation a
per-invocation choice. scripts/run_tier.py passes the flag for the tier
lanes; other suites opt in the same way.

Status mapping (open question #2 in PLAN: skipped is informational):
- setup/teardown error  -> "failed" (an errored test is not a pass)
- setup skip / xfail    -> "skipped"
- call passed / xpass   -> "passed"
- call failed           -> "failed"
Only ``passed``/``failed``/``skipped`` ever appear (contract #1).

Stdlib + pytest only — the plugin must import cleanly in envs that do not
install runner-common's runtime dependencies.
"""
import json
import os
import sys
import tempfile

import pytest

MANIFEST_ENV_VAR = "LAZYAF_TEST_RESULTS_PATH"
REPO_ROOT_ENV_VAR = "LAZYAF_REPO_ROOT"
MANIFEST_VERSION = 1
MARKER_NAME = "lazyaf_test_id"
REPO_ROOT_MARKER = ".git"
_COLLECTOR_NAME = "lazyaf-manifest-collector"


def pytest_configure(config):
    # Always register the marker: unset-env runs must still collect
    # annotated tests without warnings/errors (contract #5).
    config.addinivalue_line(
        "markers",
        f"{MARKER_NAME}(id): map this test's outcome to the LazyAF TestRef "
        "with this stable id (Phase 12.2.6 test tie-back)",
    )
    out_path = os.environ.get(MANIFEST_ENV_VAR)
    if out_path:
        config.pluginmanager.register(
            LazyafManifestCollector(out_path), _COLLECTOR_NAME
        )


class LazyafManifestCollector:
    """Records outcomes of marker-annotated tests; writes the manifest at
    session finish. Registered only when LAZYAF_TEST_RESULTS_PATH is set."""

    def __init__(self, out_path: str):
        self.out_path = out_path
        # nodeid -> result entry (insertion ordered = execution ordered)
        self._results = {}
        # directory -> repo root (or None); the .git walk runs once per dir
        self._root_cache = {}

    # -- file_path (cross-agent contract #3) ---------------------------------

    def _repo_root(self, start_dir):
        """Repo root for a test file's directory, or None if undiscoverable.

        Explicit ``LAZYAF_REPO_ROOT`` wins (checkouts with no ``.git``);
        otherwise walk up for the ``.git`` marker, memoized per directory.
        """
        env_root = os.environ.get(REPO_ROOT_ENV_VAR)
        if env_root:
            return os.path.abspath(env_root)

        if start_dir in self._root_cache:
            return self._root_cache[start_dir]

        root = None
        current = start_dir
        while True:
            if os.path.exists(os.path.join(current, REPO_ROOT_MARKER)):
                root = current
                break
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        self._root_cache[start_dir] = root
        return root

    def _file_path(self, item):
        """REPO-ROOT-relative, "/"-separated path of the test's file.

        Falls back to pytest's own rootdir-relative ``item.location[0]``
        when no repo root can be found — a worse path beats no path.
        """
        fallback = item.location[0].replace(os.sep, "/")
        try:
            absolute = os.path.abspath(str(getattr(item, "path", None) or item.fspath))
            root = self._repo_root(os.path.dirname(absolute))
            if not root:
                return fallback
            relative = os.path.relpath(absolute, root)
        except (OSError, ValueError, AttributeError, TypeError):
            return fallback
        if relative.startswith(os.pardir + os.sep) or relative == os.pardir:
            # Test file lives outside the repo root: rootdir-relative is the
            # honest answer.
            return fallback
        return relative.replace(os.sep, "/")

    @staticmethod
    def _marker_id(item):
        marker = item.get_closest_marker(MARKER_NAME)
        if marker is None or not marker.args:
            return None
        test_id = marker.args[0]
        return test_id if isinstance(test_id, str) and test_id else None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item, call):
        outcome = yield
        report = outcome.get_result()

        test_id = self._marker_id(item)
        if test_id is None:
            return  # unannotated tests are never recorded

        entry = self._results.get(item.nodeid)
        if entry is None:
            entry = {
                "lazyaf_test_id": test_id,
                # A test whose call phase never runs (setup skip) is skipped
                # unless a phase FAILS below.
                "status": "skipped",
                "duration_ms": None,
                # REPO-ROOT-relative, "/"-separated (cross-agent #3) — NOT
                # pytest's rootdir-relative location (rootdir here is tdd/).
                "file_path": self._file_path(item),
            }
            self._results[item.nodeid] = entry

        if report.when == "call":
            entry["duration_ms"] = int(report.duration * 1000)

        if report.failed:
            # Any failed phase (setup error, call failure, teardown error)
            # makes the test failed — and failed is terminal.
            entry["status"] = "failed"
        elif entry["status"] != "failed":
            if report.when == "call" and report.passed:
                entry["status"] = "passed"
            elif report.skipped:
                # setup skip or call-phase xfail; informational per PLAN.
                entry["status"] = "skipped"

    def pytest_sessionfinish(self, session):
        """Write the manifest. NEVER raises out of the hook.

        Telemetry must not be able to fail a test run: an unwritable path, a
        full disk, a read-only mount — every one of those is a stderr line,
        not a red suite (and never a changed exit status). Mirrors the
        control runtime's rule on the consumer side.
        """
        try:
            self._write_manifest()
        except Exception as e:  # noqa: BLE001 - deliberate: telemetry only
            print(
                "[pytest-lazyaf] WARNING: could not write test-results "
                f"manifest to {self.out_path!r}: {e!r}",
                file=sys.stderr,
            )

    def _write_manifest(self):
        if not self._results:
            # No annotated test ran: no manifest. The control runtime only
            # POSTs when the file exists, so unannotated runs cost nothing.
            return
        manifest = {
            "version": MANIFEST_VERSION,
            "results": list(self._results.values()),
        }
        out = os.path.abspath(self.out_path)
        out_dir = os.path.dirname(out) or "."
        os.makedirs(out_dir, exist_ok=True)
        # Atomic write: the control runtime may poll/pick the file up the
        # moment the step command exits — it must never see a partial JSON.
        fd, tmp = tempfile.mkstemp(
            dir=out_dir, prefix=".lazyaf-manifest-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(manifest, fh)
            os.replace(tmp, out)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
