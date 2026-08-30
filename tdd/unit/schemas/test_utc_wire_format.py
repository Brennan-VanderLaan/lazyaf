"""The UTC wire format for datetimes, and the ratchet that keeps it.

QA finding T1 (BLOCKER): every model column is
``mapped_column(DateTime, default=datetime.utcnow)`` and SQLite returns those
values naive, so the API emitted::

    "created_at": "2026-08-30T12:06:32.695487"

Per ECMA-262 a date-*time* string with no designator is parsed as LOCAL time.
On a demo laptop at UTC-4 a row written one second ago read as four hours in
the future, which is why every live duration in the UI rendered negative
(``-14399s``) and every "Started" column showed a time that had not happened.

``app/schemas/_datetime.py`` fixes it at the serialization boundary. This file
pins the format and — in ``TestEveryDatetimeFieldIsPinned`` — asserts that no
schema in the package can go back to a bare ``datetime`` without failing.
"""
import importlib
import pkgutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel, PlainSerializer

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

import app.schemas  # noqa: E402
from app.schemas._datetime import UTCDateTime, to_utc, utc_isoformat  # noqa: E402


class TestToUtc:
    def test_naive_is_stamped_utc_not_shifted(self):
        """A naive value IS UTC (that is what the DB holds) - label it, do not
        convert it. Converting would move the instant."""
        naive = datetime(2026, 8, 30, 12, 6, 32, 695487)
        stamped = to_utc(naive)
        assert stamped.tzinfo is timezone.utc
        assert stamped.replace(tzinfo=None) == naive

    def test_aware_is_converted_to_utc(self):
        eastern = timezone(timedelta(hours=-4))
        aware = datetime(2026, 8, 30, 8, 6, 32, tzinfo=eastern)
        converted = to_utc(aware)
        assert converted.tzinfo is timezone.utc
        assert converted.hour == 12
        assert converted == aware  # same instant, different spelling


class TestUtcIsoformat:
    def test_uses_the_plus_zero_offset_spelling(self):
        """`+00:00`, not `Z`.

        Both are valid ISO-8601 and both parse in a browser, but only
        `+00:00` also parses with Python 3.10's `datetime.fromisoformat`
        (which rejects `Z` until 3.11) - and this project's own test suite,
        CLI and QA harness run on 3.10. One spelling everywhere is the point.
        """
        rendered = utc_isoformat(datetime(2026, 8, 30, 12, 6, 32, 695487))
        assert rendered == "2026-08-30T12:06:32.695487+00:00"
        assert not rendered.endswith("Z")

    def test_round_trips_through_python_310_fromisoformat(self):
        rendered = utc_isoformat(datetime(2026, 8, 30, 12, 6, 32, 695487))
        parsed = datetime.fromisoformat(rendered)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timedelta(0)

    def test_none_passes_through(self):
        """Hand-built payloads call this on nullable columns without a guard."""
        assert utc_isoformat(None) is None


class _Sample(BaseModel):
    created_at: UTCDateTime
    finished_at: UTCDateTime | None = None


class TestUTCDateTimeAnnotation:
    def test_json_mode_carries_the_offset(self):
        dumped = _Sample(created_at=datetime(2026, 8, 30, 12, 6, 32)).model_dump(
            mode="json"
        )
        assert dumped["created_at"] == "2026-08-30T12:06:32+00:00"
        assert dumped["finished_at"] is None

    def test_a_fresh_row_never_reads_as_being_in_the_future(self):
        """The exact arithmetic the UI does, from a browser at UTC-4.

        This is the assertion that the `-14399s` durations were failing.
        """
        wire = _Sample(created_at=datetime.utcnow()).model_dump(mode="json")[
            "created_at"
        ]
        as_a_browser_parses_it = datetime.fromisoformat(wire)
        elapsed = (
            datetime.now(timezone.utc) - as_a_browser_parses_it
        ).total_seconds()
        assert elapsed >= 0, f"a row created now reads {elapsed:.0f}s in the future"

    def test_python_mode_is_untouched(self):
        """Serialization-only. Anything comparing these against
        ``datetime.utcnow()`` in-process must keep seeing a datetime."""
        value = _Sample(created_at=datetime(2026, 8, 30, 12, 6, 32)).model_dump()
        assert value["created_at"] == datetime(2026, 8, 30, 12, 6, 32)

    def test_openapi_contract_is_unchanged(self):
        """The generated client still sees a date-time string, not a bare one."""
        schema = _Sample.model_json_schema(mode="serialization")
        assert schema["properties"]["created_at"] == {
            "title": "Created At",
            "type": "string",
            "format": "date-time",
        }


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------

def _schema_modules():
    """Every module under app.schemas, imported."""
    package = app.schemas
    names = [
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.ispkg
    ]
    return [importlib.import_module(f"app.schemas.{name}") for name in names]


def _models():
    """(module name, model class) for every BaseModel defined in app.schemas."""
    seen = set()
    for module in _schema_modules():
        for attribute in vars(module).values():
            if (
                isinstance(attribute, type)
                and issubclass(attribute, BaseModel)
                and attribute is not BaseModel
                and attribute.__module__ == module.__name__
                and attribute not in seen
            ):
                seen.add(attribute)
                yield module.__name__, attribute


def _flatten(annotation):
    """The annotation and every type/marker nested inside it.

    ``UTCDateTime`` is ``Annotated[datetime, PlainSerializer, ...]``; wrapped
    in ``| None`` the markers move inside the union, so a shallow look at
    ``FieldInfo.metadata`` is not enough.
    """
    yield annotation
    for arg in get_args(annotation):
        yield from _flatten(arg)


def _fields_with_datetimes():
    for module_name, model in _models():
        for field_name, field in model.model_fields.items():
            parts = list(_flatten(field.annotation)) + list(field.metadata)
            if any(part is datetime for part in parts):
                yield module_name, model, field_name, parts


class TestEveryDatetimeFieldIsPinned:
    """No schema in the package may emit a naive datetime — including ones
    that do not exist yet. Adding ``created_at: datetime`` fails here."""

    def test_the_scan_actually_found_fields(self):
        """Guard against a silently empty sweep (R4: no fake green)."""
        found = list(_fields_with_datetimes())
        assert len(found) >= 40, (
            f"only {len(found)} datetime fields discovered across app.schemas; "
            f"the introspection is broken, not the schemas"
        )

    def test_every_datetime_field_serializes_as_utc(self):
        offenders = []
        for module_name, model, field_name, parts in _fields_with_datetimes():
            pinned = any(
                isinstance(part, PlainSerializer) and part.func is utc_isoformat
                for part in parts
            )
            if not pinned:
                offenders.append(f"{module_name}.{model.__name__}.{field_name}")
        assert not offenders, (
            "these fields are annotated `datetime` instead of `UTCDateTime`, so "
            "they go on the wire with no timezone designator and every browser "
            "reads them as local time: " + ", ".join(sorted(offenders))
        )


class TestRepresentativeReadSchemas:
    """Spot-checks on the schemas the demo screens actually render, so a
    regression in one of them fails with its own name rather than only inside
    the sweep above."""

    @pytest.mark.parametrize(
        "import_path, field",
        [
            ("app.schemas.repo:RepoRead", "created_at"),
            ("app.schemas.card:CardRead", "created_at"),
            ("app.schemas.job:JobRead", "created_at"),
            ("app.schemas.pipeline:PipelineRead", "created_at"),
            ("app.schemas.pipeline:PipelineRunRead", "started_at"),
            ("app.schemas.agent_file:AgentFileRead", "created_at"),
            ("app.schemas.spec:FeatureRead", "created_at"),
            ("app.schemas.spec:PromptTemplateRead", "updated_at"),
            ("app.schemas.experiment:ExperimentRead", "launched_at"),
            ("app.schemas.runner:RunnerRead", "last_heartbeat"),
            ("app.schemas.testref:TestRefRead", "created_at"),
            ("app.schemas.usage:StepUsageRead", "created_at"),
            ("app.schemas.playground:PlaygroundStatus", "started_at"),
            ("app.schemas.playground:PlaygroundLogEvent", "timestamp"),
            ("app.schemas.debug:DebugSessionInfo", "expires_at"),
        ],
    )
    def test_field_is_pinned(self, import_path, field):
        module_name, class_name = import_path.split(":")
        model = getattr(importlib.import_module(module_name), class_name)
        parts = list(_flatten(model.model_fields[field].annotation)) + list(
            model.model_fields[field].metadata
        )
        assert any(
            isinstance(part, PlainSerializer) and part.func is utc_isoformat
            for part in parts
        ), f"{import_path}.{field} is not a UTCDateTime"
