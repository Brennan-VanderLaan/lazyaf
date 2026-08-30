"""LazyAF CLI - Ingest repos and land changes.

This module holds the SINGLE SOURCE OF TRUTH for the distribution version.
`cli/pyproject.toml` declares `dynamic = ["version"]` and reads `__version__`
from here statically, so bumping the release means editing this line only.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
