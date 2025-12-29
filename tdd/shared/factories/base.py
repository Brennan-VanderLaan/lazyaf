"""
Base factory classes and utilities.

This module provides the foundation for creating test data factories
using factory_boy with async SQLAlchemy support.
"""
from datetime import datetime
from typing import Any
from uuid import uuid4

import factory
from faker import Faker

fake = Faker()


class BaseFactory(factory.Factory):
    """Base factory for all model factories.

    Provides common functionality and patterns for creating test data.
    """

    class Meta:
        abstract = True

    @classmethod
    def _create(cls, model_class, *args, **kwargs):
        """Override create to handle SQLAlchemy models."""
        return model_class(*args, **kwargs)

    @classmethod
    def create_dict(cls, **kwargs) -> dict[str, Any]:
        """Create a dictionary representation suitable for API requests."""
        obj = cls.build(**kwargs)
        return {
            key: getattr(obj, key)
            for key in cls._meta.model.__table__.columns.keys()
            if hasattr(obj, key) and getattr(obj, key) is not None
        }


def generate_uuid() -> str:
    """Generate a UUID string for use as an ID."""
    return str(uuid4())


def generate_timestamp() -> datetime:
    """Generate a current UTC timestamp."""
    return datetime.utcnow()


def generate_branch_name(title: str) -> str:
    """Generate a valid git branch name from a title."""
    # Convert to lowercase and replace spaces with hyphens
    branch = title.lower().replace(" ", "-")
    # Remove non-alphanumeric characters except hyphens
    branch = "".join(c for c in branch if c.isalnum() or c == "-")
    # Truncate to reasonable length
    branch = branch[:50]
    return f"feature/{branch}"
