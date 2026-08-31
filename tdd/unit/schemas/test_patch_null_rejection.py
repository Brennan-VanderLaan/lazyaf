"""PATCH bodies: "omittable" is not "nullable".

QA finding T3: ``PATCH {"<required field>": null}`` 500'd on every patchable
entity — cards (title/status/description/runner_type), features, user stories,
criteria, prompt-templates, agent-files, repos, pipelines, experiments. Every
``*Update`` schema types its fields ``X | None = None`` so a PATCH can carry a
subset, and that type also made an explicit JSON ``null`` valid; the ``None``
then reached a NOT NULL column::

    IntegrityError: NOT NULL constraint failed: cards.title
    -> 500 text/plain, and a dropped keep-alive connection with it

``app/schemas/_patch.py`` restores the distinction. This file pins the three
behaviours per schema and — in ``TestGuardsMatchTheColumns`` — derives what
must be guarded from the SQLAlchemy models, so a new patchable field or a
column that becomes NOT NULL cannot quietly reopen the hole.
"""
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.agent_file import AgentFile  # noqa: E402
from app.models.card import Card  # noqa: E402
from app.models.experiment import Experiment  # noqa: E402
from app.models.pipeline import Pipeline  # noqa: E402
from app.models.repo import Repo  # noqa: E402
from app.models.spec import (  # noqa: E402
    AcceptanceCriterion,
    Feature,
    PromptTemplate,
    UserStory,
)
from app.schemas._patch import not_null  # noqa: E402
from app.schemas.agent_file import AgentFileUpdate  # noqa: E402
from app.schemas.card import CardUpdate  # noqa: E402
from app.schemas.experiment import ExperimentUpdate  # noqa: E402
from app.schemas.pipeline import PipelineUpdate  # noqa: E402
from app.schemas.repo import RepoUpdate  # noqa: E402
from app.schemas.spec import (  # noqa: E402
    CriterionUpdate,
    FeatureUpdate,
    PromptTemplateUpdate,
    UserStoryUpdate,
)

#: Every patchable entity: (label, Update schema, ORM model, a field whose
#: column is NULLABLE — i.e. one an explicit null legitimately clears, or None
#: when the entity has no such field).
PATCHABLE = [
    ("cards", CardUpdate, Card, "feature_id"),
    ("agent-files", AgentFileUpdate, AgentFile, "description"),
    ("repos", RepoUpdate, Repo, "remote_url"),
    ("pipelines", PipelineUpdate, Pipeline, "description"),
    ("features", FeatureUpdate, Feature, None),
    ("user-stories", UserStoryUpdate, UserStory, "priority"),
    ("criteria", CriterionUpdate, AcceptanceCriterion, "notes"),
    ("prompt-templates", PromptTemplateUpdate, PromptTemplate, None),
    ("experiments", ExperimentUpdate, Experiment, "verify"),
]

IDS = [entry[0] for entry in PATCHABLE]

#: The subset that HAS a nullable patchable column. Split out rather than
#: skipped inside the parametrization: a skip in front of an assertion is how
#: coverage rots (R4), and "this entity has no nullable field" is a fact about
#: the schema, not a runtime condition.
CLEARABLE = [(label, schema, field) for label, schema, _, field in PATCHABLE if field]
CLEARABLE_IDS = [entry[0] for entry in CLEARABLE]


def _guarded_fields(schema) -> set:
    """The fields `schema` refuses an explicit null on — measured, not read.

    Was a scan for validators whose function *is* ``_reject_null``. That
    measured the MECHANISM, and 12.8 P3 gave the mechanism a second
    legitimate spelling: ``PipelineUpdate.steps`` left ``not_null(...)``
    because that helper's premise is "this field maps to a NOT NULL column",
    and ``steps`` stopped being written to ``pipelines.steps`` at all — the
    boundary converts it into ``steps_graph``, and P6 drops the column. It
    kept its own ``_steps_is_never_null`` validator, so the wire behaviour is
    identical and only the reason changed.

    Probing the behaviour instead keeps every assertion below true, keeps
    ``steps`` inside the ratchet rather than exempted from it, and means a
    future field guarded some third way is still counted. R4: this is the
    strictly stronger test, not the accommodating one.
    """
    guarded = set()
    for field in schema.model_fields:
        try:
            schema(**{field: None})
        except ValidationError as caught:
            if any(error["loc"] == (field,) for error in caught.errors()):
                guarded.add(field)
    return guarded


def _not_null_columns(model) -> set:
    return {column.name for column in model.__table__.columns if not column.nullable}


def _column_names(model) -> set:
    return {column.name for column in model.__table__.columns}


class TestExplicitNullIsRefused:
    @pytest.mark.parametrize("label, schema, model, nullable_field", PATCHABLE, ids=IDS)
    def test_null_on_a_required_field_is_a_422_naming_the_field(
        self, label, schema, model, nullable_field
    ):
        for field in sorted(_guarded_fields(schema)):
            with pytest.raises(ValidationError) as caught:
                schema(**{field: None})
            errors = caught.value.errors()
            assert [error["loc"] for error in errors] == [(field,)], (
                f"{label}: PATCH {{{field!r}: null}} must be refused against "
                f"that field, got {errors}"
            )
            assert field in errors[0]["msg"]

    @pytest.mark.parametrize("label, schema, model, nullable_field", PATCHABLE, ids=IDS)
    def test_omitting_the_field_still_means_leave_it_alone(
        self, label, schema, model, nullable_field
    ):
        """The whole point of the ``| None = None`` typing. Routers apply
        ``model_dump(exclude_unset=True)``, so an absent field must stay
        absent rather than becoming an explicit null."""
        dumped = schema().model_dump(exclude_unset=True)
        assert dumped == {}, f"{label}: an empty PATCH body dumped {dumped}"

    @pytest.mark.parametrize(
        "label, schema, nullable_field", CLEARABLE, ids=CLEARABLE_IDS
    )
    def test_null_on_a_nullable_field_is_still_how_a_client_clears_it(
        self, label, schema, nullable_field
    ):
        dumped = schema(**{nullable_field: None}).model_dump(exclude_unset=True)
        assert dumped == {nullable_field: None}, (
            f"{label}.{nullable_field} is a nullable column; refusing null "
            f"there would take away the only way to clear it"
        )


class TestGuardsMatchTheColumns:
    """The ratchet: what is guarded is derived from the database, not from a
    list someone has to remember to update."""

    @pytest.mark.parametrize("label, schema, model, nullable_field", PATCHABLE, ids=IDS)
    def test_every_not_null_column_in_the_schema_is_guarded(
        self, label, schema, model, nullable_field
    ):
        patchable = set(schema.model_fields)
        required = _not_null_columns(model) & patchable
        missing = required - _guarded_fields(schema)
        assert not missing, (
            f"{label}: {sorted(missing)} map to NOT NULL columns but accept an "
            f"explicit null, so PATCH with null on them 500s. Add them to the "
            f"schema's not_null(...) list."
        )

    @pytest.mark.parametrize("label, schema, model, nullable_field", PATCHABLE, ids=IDS)
    def test_no_nullable_column_is_over_guarded(
        self, label, schema, model, nullable_field
    ):
        """Guarding a nullable column would remove the only way to clear it —
        a silent feature loss dressed up as a fix.

        Scoped to fields that ARE columns. A field with no column of its name
        is neither NOT NULL nor nullable, so the rationale above simply does
        not reach it: ``PipelineUpdate.steps`` is an authoring dialect the
        boundary converts into ``steps_graph``, and there is nothing to clear
        because the array is not stored. Before this scoping the test read
        "not a NOT NULL column" as "a nullable column", which would have gone
        red at 12.8 P6 — when ``pipelines.steps`` is dropped — on a schema
        that was behaving correctly.
        """
        over = (_guarded_fields(schema) & _column_names(model)) - _not_null_columns(model)
        assert not over, (
            f"{label}: {sorted(over)} are nullable columns; refusing null on "
            f"them takes away the client's way to clear the value"
        )


class TestNotNullHelper:
    def test_refuses_an_empty_field_list(self):
        """A ``not_null()`` with no arguments would be a guard that guards
        nothing — exactly the kind of quiet no-op R4 exists to prevent."""
        with pytest.raises(ValueError):
            not_null()

    def test_message_tells_the_client_what_to_do_instead(self):
        with pytest.raises(ValidationError) as caught:
            CardUpdate(title=None)
        assert "omit the field" in caught.value.errors()[0]["msg"]
