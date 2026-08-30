"""
Shared PATCH-body validator: "omittable" is not the same as "nullable".

Every ``*Update`` schema in this package types its fields ``X | None = None``
so that a PATCH can carry a subset of the entity. Routers then apply
``model_dump(exclude_unset=True)``, which correctly distinguishes *absent*
from *present*. But the type also made an explicit JSON ``null`` a VALID
value, and for a field backed by a NOT NULL column that ``None`` went
straight through to the database::

    PATCH /api/cards/{id}  {"title": null}
      -> IntegrityError: NOT NULL constraint failed: cards.title
      -> 500 text/plain "Internal Server Error"   (and a dropped keep-alive)

This is the wrong answer twice over: it is a client error, not a server error,
and the client is told nothing about which field was at fault.

``not_null()`` restores the distinction without giving up the sentinel:

* field ABSENT  -> validator never runs, field stays unset, PATCH ignores it;
* field ``null`` -> 422 naming the field;
* field a value -> validated normally.

Usage inside an ``*Update`` schema — list exactly the fields whose column is
NOT NULL (a column that really is nullable must keep accepting ``null``,
because that is how a client clears it)::

    class CardUpdate(BaseModel):
        title: str | None = None          # cards.title  is NOT NULL
        feature_id: str | None = None     # cards.feature_id is nullable

        _reject_nulls = not_null("title")
"""
from pydantic import field_validator

__all__ = ["not_null"]


def _reject_null(cls, value, info):
    if value is None:
        raise ValueError(
            f"'{info.field_name}' is required and cannot be null; omit the "
            f"field to leave it unchanged"
        )
    return value


def not_null(*fields: str):
    """Build a validator that 422s on an explicit ``null`` for `fields`.

    ``mode="before"`` so the refusal happens before the field's own type
    coercion, and so the error's ``loc`` names the offending field.
    """
    if not fields:
        raise ValueError("not_null() needs at least one field name")
    return field_validator(*fields, mode="before")(classmethod(_reject_null))
