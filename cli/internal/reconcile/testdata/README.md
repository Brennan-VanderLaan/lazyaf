# reconcile test fixtures

`declared_suite.py` and `broken_suite.py` are the two pytest suites
`TestFromCollect` drives REAL `python -m pytest --collect-only` over (the
fixtures `tdd/unit/scripts/test_cli_tests_reconcile.py::TestFromCollect`
wrote into `tmp_path`). The Go test copies each into a fresh temp directory
as `tests/test_declared.py` / `tests/test_broken.py` before collecting, for
two reasons:

- the collector's `file_path` must come back repo-root-relative to the
  SUITE (`tests/test_declared.py`), and a fixture left in place would resolve
  against this checkout's `.git` instead;
- they are deliberately NOT named `test_*.py` here: `tdd/unit/packaging/
  test_wheel_metadata.py::TestPackagedTreeIsClean::test_no_tests_in_the_build_context`
  walks everything under `cli/` for that shape while the Python CLI still
  ships from it (P4 deletes both).
