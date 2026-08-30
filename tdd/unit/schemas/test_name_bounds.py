"""
Every user-supplied name/title field is bounded and non-blank.

Before app/schemas/_strings.py, every one of these was a bare `str`:

  * POST /api/repos/{id}/pipelines {"name": "Q" * 60000} -> 201, and the
    pipeline card measured scrollWidth 66642px inside a 436px container,
    which pushes Edit and Run outside the clipped element;
  * POST /api/repos {"name": "   "} -> 201, an invisible sidebar row.

These tests pin the bound (`Name`, `Sentence`, `Body`) on the INPUT schemas,
and — just as importantly — pin that the READ schemas stay unbounded so rows
written before the bound still serialize instead of 500ing a list endpoint.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.schemas._strings import BODY_MAX, NAME_MAX, SENTENCE_MAX
from app.schemas.agent_file import AgentFileCreate, AgentFileRead, AgentFileUpdate
from app.schemas.card import CardCreate, CardRead, CardUpdate
from app.schemas.experiment import ExperimentUpdate
from app.schemas.lazyaf_yaml import PipelineStepYaml, PipelineYaml
from app.schemas.pipeline import PipelineCreate, PipelineRead, PipelineUpdate
from app.schemas.repo import RepoCreate, RepoRead, RepoUpdate
from app.schemas.spec import (
    CriterionCreate,
    CriterionUpdate,
    FeatureCreate,
    FeatureUpdate,
    PromptTemplateCreate,
    PromptTemplateUpdate,
    UserStoryCreate,
    UserStoryUpdate,
)

# (schema, name-ish field, other required fields) for every entity whose
# short display name a user can set.
NAME_FIELDS = [
    pytest.param(RepoCreate, "name", {}, id="RepoCreate.name"),
    pytest.param(RepoUpdate, "name", {}, id="RepoUpdate.name"),
    pytest.param(CardCreate, "title", {}, id="CardCreate.title"),
    pytest.param(CardUpdate, "title", {}, id="CardUpdate.title"),
    pytest.param(PipelineCreate, "name", {}, id="PipelineCreate.name"),
    pytest.param(PipelineUpdate, "name", {}, id="PipelineUpdate.name"),
    pytest.param(
        AgentFileCreate, "name", {"content": "x"}, id="AgentFileCreate.name"
    ),
    pytest.param(AgentFileUpdate, "name", {}, id="AgentFileUpdate.name"),
    pytest.param(FeatureCreate, "title", {}, id="FeatureCreate.title"),
    pytest.param(FeatureUpdate, "title", {}, id="FeatureUpdate.title"),
    pytest.param(UserStoryCreate, "title", {}, id="UserStoryCreate.title"),
    pytest.param(UserStoryUpdate, "title", {}, id="UserStoryUpdate.title"),
    pytest.param(PromptTemplateCreate, "name", {}, id="PromptTemplateCreate.name"),
    pytest.param(PromptTemplateUpdate, "name", {}, id="PromptTemplateUpdate.name"),
    pytest.param(ExperimentUpdate, "name", {}, id="ExperimentUpdate.name"),
    pytest.param(CriterionCreate, "text", {}, id="CriterionCreate.text"),
    pytest.param(CriterionUpdate, "text", {}, id="CriterionUpdate.text"),
    pytest.param(
        PipelineYaml, "name", {}, id="PipelineYaml.name"
    ),
    pytest.param(
        PipelineStepYaml, "name", {}, id="PipelineStepYaml.name"
    ),
]

# The longest name/title anywhere in the repo's tests, fixtures and shipped
# .lazyaf/pipelines/*.yaml. The bound must clear it with room to spare, or a
# working demo turns into a wall of 422s.
LONGEST_REAL_NAME = "Remote lane: script step via the loopback runner agent"


def _make(schema, field, extra, value):
    return schema(**{field: value, **extra})


class TestNameIsLengthBounded:
    def test_the_bound_clears_the_longest_real_name(self):
        """Sanity-check the bound against real fixture data, not a guess."""
        assert len(LONGEST_REAL_NAME) < NAME_MAX

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_realistic_name_accepted(self, schema, field, extra):
        model = _make(schema, field, extra, LONGEST_REAL_NAME)
        assert getattr(model, field) == LONGEST_REAL_NAME

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_at_the_bound_accepted(self, schema, field, extra):
        limit = SENTENCE_MAX if field == "text" else NAME_MAX
        model = _make(schema, field, extra, "A" * limit)
        assert len(getattr(model, field)) == limit

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_over_length_rejected(self, schema, field, extra):
        limit = SENTENCE_MAX if field == "text" else NAME_MAX
        with pytest.raises(ValidationError) as exc:
            _make(schema, field, extra, "A" * (limit + 1))
        assert field in str(exc.value)

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_the_measured_60000_character_name_is_rejected(
        self, schema, field, extra
    ):
        """The exact value that produced a 66642px card."""
        with pytest.raises(ValidationError):
            _make(schema, field, extra, "Q" * 60_000)


class TestNameCannotBeBlank:
    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_empty_rejected(self, schema, field, extra):
        with pytest.raises(ValidationError) as exc:
            _make(schema, field, extra, "")
        assert field in str(exc.value)

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    @pytest.mark.parametrize("blank", ["   ", "\t\n ", "\n", " " * 3])
    def test_whitespace_only_rejected(self, schema, field, extra, blank):
        with pytest.raises(ValidationError):
            _make(schema, field, extra, blank)

    @pytest.mark.parametrize("schema,field,extra", NAME_FIELDS)
    def test_surrounding_whitespace_is_stripped(self, schema, field, extra):
        model = _make(schema, field, extra, "  My Pipeline\t\n")
        assert getattr(model, field) == "My Pipeline"


class TestFreeTextBodies:
    """Descriptions are bounded far more loosely, and may be empty."""

    @pytest.mark.parametrize(
        "schema,field,extra",
        [
            pytest.param(CardCreate, "description", {"title": "t"}, id="card"),
            pytest.param(PipelineCreate, "description", {"name": "n"}, id="pipeline"),
            pytest.param(FeatureCreate, "description", {"title": "t"}, id="feature"),
            pytest.param(UserStoryCreate, "narrative", {"title": "t"}, id="story"),
        ],
    )
    def test_empty_and_long_prose_accepted(self, schema, field, extra):
        assert getattr(schema(**{field: "", **extra}), field) == ""
        prose = "P" * BODY_MAX
        assert getattr(schema(**{field: prose, **extra}), field) == prose

    @pytest.mark.parametrize(
        "schema,field,extra",
        [
            pytest.param(CardCreate, "description", {"title": "t"}, id="card"),
            pytest.param(PipelineCreate, "description", {"name": "n"}, id="pipeline"),
            pytest.param(FeatureCreate, "description", {"title": "t"}, id="feature"),
            pytest.param(UserStoryCreate, "narrative", {"title": "t"}, id="story"),
        ],
    )
    def test_a_one_megabyte_blob_is_rejected(self, schema, field, extra):
        with pytest.raises(ValidationError):
            schema(**{field: "B" * 1_000_000, **extra})

    def test_file_bodies_stay_unbounded(self):
        """`content` is a whole agent/prompt file, not a description."""
        big = "x" * (BODY_MAX + 10_000)
        assert AgentFileCreate(name="a", content=big).content == big
        assert PromptTemplateCreate(name="p", content=big).content == big


class TestReadSchemasStayUnbounded:
    """A row written before the bound must still serialize.

    Bounding a *Read schema would turn yesterday's 60000-character pipeline
    name from an ugly card into a 500 on the list endpoint - strictly worse.
    """

    def _now(self):
        return datetime.now(timezone.utc)

    def test_repo_read_accepts_a_legacy_over_long_name(self):
        model = RepoRead(
            id="r1",
            name="Q" * 60_000,
            is_ingested=True,
            internal_git_url="git://x",
            created_at=self._now(),
        )
        assert len(model.name) == 60_000

    def test_pipeline_read_accepts_a_legacy_over_long_name(self):
        model = PipelineRead(
            id="p1",
            repo_id="r1",
            name="Q" * 60_000,
            is_template=False,
            created_at=self._now(),
            updated_at=self._now(),
        )
        assert len(model.name) == 60_000

    def test_card_read_accepts_a_legacy_blank_title(self):
        model = CardRead(
            id="c1",
            repo_id="r1",
            title="",
            status="todo",
            created_at=self._now(),
            updated_at=self._now(),
        )
        assert model.title == ""

    def test_agent_file_read_accepts_a_legacy_over_long_name(self):
        model = AgentFileRead(
            id="a1",
            name="Q" * 60_000,
            content="x",
            created_at=self._now(),
            updated_at=self._now(),
        )
        assert len(model.name) == 60_000
