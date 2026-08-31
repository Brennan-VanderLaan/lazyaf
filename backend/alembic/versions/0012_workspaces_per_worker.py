"""A pipeline run may own MANY workspaces, one per parallel worker (M13-1).

0002 built `workspaces` with a UNIQUE index on `pipeline_run_id`: exactly
one workspace, one named volume, one checkout per run. That constraint is
the reason the owner's headline hypothesis cannot be measured. K parallel
agent steps in a graph fan-out all mount the SAME working tree, so any
conflict rate a benchmark reports today is a property of this schema, not of
the strategy under test. The target shape is the owner's own framing:
parallel writers get to the goal via git-style commits and merges, working
in parallel WITHOUT touching the same file on disk.

This revision moves the constraint to the correct grain:

- adds `workspaces.worker_key` (String(64), NOT NULL, default 'default')
- drops `ix_workspaces_pipeline_run_id` (the single-column UNIQUE index)
- adds `uq_workspaces_run_worker` UNIQUE (pipeline_run_id, worker_key)

**worker_key is NOT NULL with a sentinel, deliberately.** SQLite and
Postgres both treat NULLs as DISTINCT inside a unique index, so a nullable
lane column would constrain nothing at all for rows that do not name a
lane - i.e. for every legacy row and every future default-lane row. A run
could then accumulate unlimited duplicate "default" workspaces, each with
its own volume and clone, only one of which any query would ever find
again. The sentinel is what keeps the common case protected.

**No volume is renamed and nothing is orphaned.** `generate_volume_name`
emits NO suffix for the default lane, so a backfilled row's name is
`lazyaf-ws-{run_id}` - the exact string already in every legacy row's
`volume_name` column and already on every live docker volume. A run that is
in flight when this migration lands keeps its volume, its lock key, its
use_count and its cleanup path.

**No table rebuild.** The constraint being removed is an INDEX
(`op.create_index(..., unique=True)` in 0002), not a table-level constraint,
so this is plain ALTER/INDEX work even on SQLite.

Guard note (same as 0002/0004/0005/0006/0007/0009/0010/0011): a pre-alembic
database adopted at startup is healed by Base.metadata.create_all - which
builds the CURRENT model schema, worker_key and composite index included -
before it is stamped and upgraded. Every step here is therefore guarded by a
column/index existence check and the revision is re-runnable.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-30

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0012'
down_revision: Union[str, Sequence[str], None] = '0011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: Must equal app.services.workspace.worker_key.DEFAULT_WORKER_KEY. Spelled
#: out rather than imported: a migration is a historical record and must not
#: change meaning when application code is refactored years later.
DEFAULT_WORKER_KEY = 'default'

_OLD_INDEX = 'ix_workspaces_pipeline_run_id'
_NEW_INDEX = 'uq_workspaces_run_worker'


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns('workspaces')}
    if 'worker_key' not in columns:
        op.add_column(
            'workspaces',
            sa.Column(
                'worker_key',
                sa.String(length=64),
                nullable=False,
                server_default=DEFAULT_WORKER_KEY,
            ),
        )

    # Backfill. Every pre-M13-1 row IS the default lane, and its volume is
    # already named as one. Also heals a healed-but-empty-valued column on
    # the adopt path.
    result = bind.execute(
        sa.text(
            "UPDATE workspaces SET worker_key = :key "
            "WHERE worker_key IS NULL OR worker_key = ''"
        ),
        {"key": DEFAULT_WORKER_KEY},
    )
    if result.rowcount:
        logger.info(
            "0012: backfilled %s workspace row(s) to the %r lane; their volume "
            "names are unchanged, so no volume is orphaned",
            result.rowcount,
            DEFAULT_WORKER_KEY,
        )

    inspector = sa.inspect(bind)
    indexes = {ix['name'] for ix in inspector.get_indexes('workspaces')}
    if _OLD_INDEX in indexes:
        # Dropped outright rather than recreated non-unique: the composite
        # index below leads with pipeline_run_id, so it already serves every
        # WHERE pipeline_run_id = ? lookup.
        op.drop_index(_OLD_INDEX, table_name='workspaces')
    if _NEW_INDEX not in indexes:
        op.create_index(
            _NEW_INDEX, 'workspaces', ['pipeline_run_id', 'worker_key'], unique=True
        )


def downgrade() -> None:
    """Downgrade schema.

    DESTRUCTIVE, and it has to be. A non-default lane cannot be represented
    by the old one-workspace-per-run shape, and leaving those rows in place
    would make recreating the single-column UNIQUE index fail outright. They
    are DELETED. Their volumes are then unmatched - correctly labeled and
    correctly prefixed - so the orphan audit's third sweep reaps them within
    one interval rather than leaking them.

    Default-lane rows and their volumes are untouched.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns('workspaces')}
    if 'worker_key' in columns:
        result = bind.execute(
            sa.text("DELETE FROM workspaces WHERE worker_key <> :key"),
            {"key": DEFAULT_WORKER_KEY},
        )
        if result.rowcount:
            logger.warning(
                "0012 downgrade: deleted %s non-default workspace row(s); the "
                "old schema cannot represent a per-worker lane. Their docker "
                "volumes are now unmatched and the orphan audit will remove "
                "them on its next sweep",
                result.rowcount,
            )

    indexes = {ix['name'] for ix in inspector.get_indexes('workspaces')}
    if _NEW_INDEX in indexes:
        op.drop_index(_NEW_INDEX, table_name='workspaces')
    if _OLD_INDEX not in indexes:
        op.create_index(_OLD_INDEX, 'workspaces', ['pipeline_run_id'], unique=True)

    if 'worker_key' in columns:
        # Safe as a plain ALTER: the column is no longer indexed by the time
        # this runs (SQLite refuses DROP COLUMN on an indexed column).
        op.drop_column('workspaces', 'worker_key')
