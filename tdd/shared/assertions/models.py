"""
Custom assertion helpers for model testing.

These helpers provide cleaner assertions for SQLAlchemy model
and Pydantic schema validation.
"""
from datetime import datetime
from typing import Any

from pydantic import BaseModel


def assert_model_fields(model: Any, expected: dict[str, Any]) -> None:
    """Assert model instance has expected field values."""
    for field, value in expected.items():
        actual = getattr(model, field, None)
        assert actual == value, (
            f"Expected {field}={value!r}, got {field}={actual!r}"
        )


def assert_model_has_id(model: Any) -> None:
    """Assert model instance has a non-empty ID."""
    assert hasattr(model, "id"), "Model should have 'id' attribute"
    assert model.id is not None, "Model ID should not be None"
    assert isinstance(model.id, str), f"Expected str ID, got {type(model.id)}"
    assert len(model.id) == 36, f"Expected UUID length 36, got {len(model.id)}"


def assert_model_has_timestamps(model: Any) -> None:
    """Assert model has valid created_at (and optionally updated_at) timestamps."""
    assert hasattr(model, "created_at"), "Model should have 'created_at'"
    assert isinstance(model.created_at, datetime), (
        f"Expected datetime, got {type(model.created_at)}"
    )

    if hasattr(model, "updated_at"):
        assert isinstance(model.updated_at, datetime), (
            f"Expected datetime, got {type(model.updated_at)}"
        )


def assert_schema_valid(schema_class: type[BaseModel], data: dict[str, Any]) -> BaseModel:
    """Assert data validates against Pydantic schema.

    Returns the validated schema instance.
    """
    try:
        return schema_class(**data)
    except Exception as e:
        raise AssertionError(f"Schema validation failed: {e}")


def assert_schema_invalid(schema_class: type[BaseModel], data: dict[str, Any]) -> None:
    """Assert data fails validation against Pydantic schema."""
    try:
        schema_class(**data)
        raise AssertionError(
            f"Expected validation to fail for {schema_class.__name__} with data: {data}"
        )
    except Exception:
        pass  # Expected behavior


def assert_enum_value(model: Any, field: str, expected_enum: Any) -> None:
    """Assert model field has expected enum value."""
    actual = getattr(model, field)
    if hasattr(expected_enum, "value"):
        # Handle str Enums that store value in DB
        assert actual == expected_enum.value or actual == expected_enum, (
            f"Expected {field}={expected_enum}, got {actual}"
        )
    else:
        assert actual == expected_enum, (
            f"Expected {field}={expected_enum}, got {actual}"
        )
