"""LazyAF CLI - Ingest repos and land changes.

This module holds the SINGLE SOURCE OF TRUTH for the distribution version.
`cli/pyproject.toml` declares `dynamic = ["version"]` and reads `__version__`
from here statically, so bumping the release means editing this line only.

DO NOT EDIT THE VERSION BY HAND. release-please owns it now: the comment
markers around the assignment below are what its generic updater looks for, and
it rewrites the version between them inside the release PR, derived from the
conventional-commit log. Bumping it manually only conflicts with the next
release PR. See CONTRIBUTING.md and .github/WORKFLOWS.md.

(The markers bracket the line rather than sitting on it. release-please also
supports an end-of-line marker, but that would put a trailing comment on the
assignment, and tdd/unit/packaging reads this line as text. Keeping the
assignment byte-for-byte plain keeps that contract intact.)
"""

# x-release-please-start-version
__version__ = "0.1.0"
# x-release-please-end

__all__ = ["__version__"]
