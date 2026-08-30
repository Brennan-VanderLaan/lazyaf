"""
THE wire format for every datetime LazyAF puts on the API (R3).

The problem this module exists to prevent
----------------------------------------
Every model column is ``mapped_column(DateTime, default=datetime.utcnow)`` —
naive UTC — and SQLite hands those values back naive on read, so pydantic
serialised them with no timezone designator::

    "created_at": "2026-08-30T12:06:32.695487"

Per ECMA-262 a date-*time* string with no designator is parsed as **local**
time, so a browser at UTC-4 reads a row written one second ago as four hours
in the future: every "Started" column renders in the future and every live
duration (``Date.now() - startTime``) renders NEGATIVE (`-14399s`).

The fix is here, at the serialization boundary, rather than in the four
frontend formatters that each observed a different symptom of it.

The format
----------
ISO-8601 with an explicit ``+00:00`` offset::

    "created_at": "2026-08-30T12:06:32.695487+00:00"

``+00:00`` and not ``Z`` (pydantic's own default for aware datetimes) because
that single spelling is accepted by every consumer we have: JavaScript
``new Date()``, Python 3.10's ``datetime.fromisoformat`` (which rejects ``Z``
until 3.11), Go, and ``jsonable_encoder`` — which is what renders any endpoint
that returns a raw dict instead of a response_model. One spelling everywhere
is the point; a mix of ``Z`` and ``+00:00`` is the same ambiguity in a nicer
costume.

Naive values are treated as UTC because that is what they are: every writer in
this codebase stores ``datetime.utcnow()``. Aware values are converted, never
truncated.

Usage — annotate the field, do not hand-format the value::

    from app.schemas._datetime import UTCDateTime

    class ThingRead(BaseModel):
        created_at: UTCDateTime
        finished_at: UTCDateTime | None = None

Code that builds a response dict by hand (WebSocket broadcast payloads, mostly)
cannot use the annotation and must call ``utc_isoformat()`` instead of
``value.isoformat()``.
"""
from datetime import datetime, timezone
from typing import Annotated, Optional

from pydantic import PlainSerializer, WithJsonSchema

__all__ = ["UTC_WIRE_EXAMPLE", "to_utc", "utc_isoformat", "UTCDateTime"]

#: What the wire format looks like. Referenced by the tests that pin it.
UTC_WIRE_EXAMPLE = "2026-08-30T12:06:32.695487+00:00"


def to_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime.

    A naive value is *stamped* UTC (that is what the DB holds); an aware
    value is converted.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_isoformat(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime the one way this API serializes datetimes.

    ``None`` passes through so hand-built payloads can call this on optional
    columns without a guard.
    """
    if value is None:
        return None
    return to_utc(value).isoformat()


#: A ``datetime`` field that always leaves as unambiguous UTC.
#:
#: Serialization-only: the in-Python value is untouched, so nothing that
#: compares these against ``datetime.utcnow()`` changes behavior. The
#: WithJsonSchema keeps the OpenAPI response schema at
#: ``{"type": "string", "format": "date-time"}`` instead of degrading it to a
#: bare string, so the generated client contract is unchanged.
UTCDateTime = Annotated[
    datetime,
    PlainSerializer(utc_isoformat, return_type=str, when_used="json-unless-none"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
