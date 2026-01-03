"""
Conftest for runner-common tests.

This is a lightweight conftest that doesn't require backend dependencies.
"""

import pytest


@pytest.fixture
def anyio_backend():
    """Required for pytest-asyncio compatibility."""
    return "asyncio"
