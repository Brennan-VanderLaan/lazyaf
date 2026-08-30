"""
Unit tests for prompt-version freezing (Phase 12.6.5).

`PromptTemplate.content` is the EDITABLE DRAFT (owned by routers/spec.py).
`PromptVersion` is the IMMUTABLE RECORD OF WHAT RAN. That is one source of
truth for each of two different data, not two for one — and the tests below
are what keep the second one immutable.

The failure this exists to prevent: a leaderboard that groups by
(template, version, model) while the template body is silently mutable
merges two different prompts into one row, and nothing in the data says it
happened.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models.experiment import ExperimentRun, PromptVersion
from app.models.spec import PromptTemplate
from app.services import experiment_service as svc

from tdd.unit.services.experiment_rows import (  # noqa: E402
    clean_pump_state,  # noqa: F401  (autouse fixture)
    fake_dispatch,  # noqa: F401  (fixture)
    make_card,
    make_experiment,
    make_repo,
)


async def make_template(db, content="Do the thing.", name=None) -> PromptTemplate:
    template = PromptTemplate(
        id=str(uuid4()),
        name=name or f"tpl-{uuid4().hex[:6]}",
        description="",
        content=content,
    )
    db.add(template)
    await db.commit()
    return template


def matrix_with_templates(*template_ids, repeat=1) -> str:
    import json

    return json.dumps(
        {
            "models": [{"agent": "mock", "model": "m0", "label": "m0"}],
            "prompts": [
                {"prompt_template_id": tid, "label": f"p{i}"}
                for i, tid in enumerate(template_ids)
            ],
            "repeat": repeat,
        }
    )


class TestGetOrCreate:
    async def test_first_freeze_is_version_one(self, db_session):
        template = await make_template(db_session)
        versions = await svc.freeze_prompt_versions(db_session, [template.id])

        assert versions[template.id].version == 1
        assert versions[template.id].body == "Do the thing."

    async def test_unchanged_template_reuses_the_same_version(self, db_session):
        template = await make_template(db_session)
        first = await svc.freeze_prompt_versions(db_session, [template.id])
        second = await svc.freeze_prompt_versions(db_session, [template.id])

        assert first[template.id].id == second[template.id].id
        assert second[template.id].version == 1

    async def test_edited_template_yields_version_two(self, db_session):
        template = await make_template(db_session, content="v1 body")
        await svc.freeze_prompt_versions(db_session, [template.id])

        template.content = "v2 body — now with more instructions"
        await db_session.commit()
        second = await svc.freeze_prompt_versions(db_session, [template.id])

        assert second[template.id].version == 2
        assert second[template.id].body == "v2 body — now with more instructions"

    async def test_reverting_a_template_reuses_the_original_version(self, db_session):
        """Identity is the CONTENT HASH, so a revert is the same prompt, not a
        third one — otherwise a leaderboard would show two rows for one body."""
        template = await make_template(db_session, content="original")
        first = await svc.freeze_prompt_versions(db_session, [template.id])
        template.content = "changed"
        await db_session.commit()
        await svc.freeze_prompt_versions(db_session, [template.id])
        template.content = "original"
        await db_session.commit()
        third = await svc.freeze_prompt_versions(db_session, [template.id])

        assert third[template.id].id == first[template.id].id
        assert third[template.id].version == 1

    async def test_versions_are_numbered_per_template(self, db_session):
        a = await make_template(db_session, content="a")
        b = await make_template(db_session, content="b")
        versions = await svc.freeze_prompt_versions(db_session, [a.id, b.id])
        assert versions[a.id].version == 1
        assert versions[b.id].version == 1

    async def test_content_hash_is_sha256_of_the_body(self, db_session):
        import hashlib

        template = await make_template(db_session, content="hash me")
        versions = await svc.freeze_prompt_versions(db_session, [template.id])
        assert versions[template.id].content_hash == (
            hashlib.sha256(b"hash me").hexdigest()
        )

    async def test_empty_body_is_still_a_version(self, db_session):
        """An empty template is a real (bad) prompt, not a missing one."""
        template = await make_template(db_session, content="")
        versions = await svc.freeze_prompt_versions(db_session, [template.id])
        assert versions[template.id].body == ""
        assert versions[template.id].version == 1

    async def test_duplicate_ids_are_resolved_once(self, db_session):
        template = await make_template(db_session)
        versions = await svc.freeze_prompt_versions(
            db_session, [template.id, template.id, template.id]
        )
        rows = list(
            (
                await db_session.execute(
                    select(PromptVersion).where(
                        PromptVersion.template_id == template.id
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1
        assert versions[template.id].id == rows[0].id

    async def test_no_template_ids_is_a_noop(self, db_session):
        assert await svc.freeze_prompt_versions(db_session, []) == {}
        assert await svc.freeze_prompt_versions(db_session, [None, ""]) == {}

    async def test_unknown_template_id_is_refused_by_name(self, db_session):
        with pytest.raises(LookupError) as exc:
            await svc.freeze_prompt_versions(db_session, ["nope-1234"])
        assert "nope-1234" in str(exc.value)

    async def test_concurrent_insert_is_absorbed_not_500ed(self, db_session):
        """The (template_id, content_hash) unique index turns the race into an
        IntegrityError, which the rollback/re-select idiom costs a retry."""
        template = await make_template(db_session, content="racy")
        digest = svc.content_hash("racy")
        # Land the row another session would have inserted between our SELECT
        # and our flush.
        db_session.add(
            PromptVersion(
                id=str(uuid4()), template_id=template.id, version=1,
                body="racy", content_hash=digest,
            )
        )
        await db_session.commit()

        versions = await svc.freeze_prompt_versions(db_session, [template.id])
        assert versions[template.id].content_hash == digest
        rows = list(
            (
                await db_session.execute(
                    select(PromptVersion).where(
                        PromptVersion.template_id == template.id
                    )
                )
            ).scalars()
        )
        assert len(rows) == 1


class TestFreezeAtLaunch:
    async def test_every_version_is_frozen_before_any_cell_dispatches(
        self, db_session, monkeypatch
    ):
        """A template edited between cell 3 and cell 4 must not split one
        variant across two prompt bodies."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        template = await make_template(db_session, content="frozen body")
        experiment = await make_experiment(
            db_session, repo, card, concurrency=1,
            matrix=matrix_with_templates(template.id, repeat=4),
        )

        seen: list[int] = []

        async def _record(db, exp, cell):
            # By the time ANY cell dispatches, every cell already carries its
            # frozen version.
            rows = list(
                (
                    await db.execute(
                        select(ExperimentRun.prompt_version).where(
                            ExperimentRun.experiment_id == exp.id
                        )
                    )
                ).scalars()
            )
            seen.extend(rows)
            from tdd.unit.services.experiment_rows import make_run

            return await make_run(db, repo, trigger_ref=cell.id)

        monkeypatch.setattr(svc, "start_cell_run", _record)
        await svc.launch(db_session, experiment)

        assert seen and all(v == 1 for v in seen)

    async def test_cells_carry_the_version_id_and_number(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        template = await make_template(db_session, content="body")
        experiment = await make_experiment(
            db_session, repo, card, matrix=matrix_with_templates(template.id)
        )
        await svc.launch(db_session, experiment)

        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        version = await db_session.get(PromptVersion, cell.prompt_version_id)
        assert cell.prompt_template_id == template.id
        assert cell.prompt_version == 1
        assert version.body == "body"

    async def test_platform_default_prompt_cells_carry_no_version(
        self, db_session, fake_dispatch
    ):
        """prompt_template_id: null is a real CONTROL variant, not a gap."""
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(db_session, repo, card, models=1)
        await svc.launch(db_session, experiment)

        cell = (
            await db_session.execute(
                select(ExperimentRun).where(
                    ExperimentRun.experiment_id == experiment.id
                )
            )
        ).scalar_one()
        assert cell.prompt_template_id is None
        assert cell.prompt_version_id is None
        assert cell.prompt_version is None

    async def test_a_relaunch_after_an_edit_records_the_new_version(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        template = await make_template(db_session, content="v1")
        first = await make_experiment(
            db_session, repo, card, matrix=matrix_with_templates(template.id)
        )
        await svc.launch(db_session, first)

        template.content = "v2"
        await db_session.commit()
        second = await make_experiment(
            db_session, repo, card, matrix=matrix_with_templates(template.id)
        )
        await svc.launch(db_session, second)

        versions = {
            e.experiment_id: e.prompt_version
            for e in (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id.in_([first.id, second.id])
                    )
                )
            ).scalars()
        }
        assert versions[first.id] == 1
        assert versions[second.id] == 2

    async def test_launch_refuses_an_unknown_template_before_creating_cells(
        self, db_session, fake_dispatch
    ):
        repo = await make_repo(db_session)
        card = await make_card(db_session, repo)
        experiment = await make_experiment(
            db_session, repo, card, matrix=matrix_with_templates("ghost-9")
        )

        with pytest.raises(LookupError):
            await svc.launch(db_session, experiment)

        cells = list(
            (
                await db_session.execute(
                    select(ExperimentRun).where(
                        ExperimentRun.experiment_id == experiment.id
                    )
                )
            ).scalars()
        )
        assert cells == []
