"""Endpoints record whether they accept IMAGE and AUDIO content parts (M14.6).

Two nullable Boolean columns on `model_endpoints`, and that is the whole
revision. Everything interesting about it is what it deliberately does NOT do.

**IT BACKFILLS NOTHING. EXISTING ROWS GET NULL, NOT FALSE.**

That is the entire point, and it is the same decision 0011 made for
`supports_tools`. `NULL` means "we have not asked"; `False` means "we asked
and it said no". An `UPDATE ... SET supports_images = 0` here would take every
endpoint registered before this revision - endpoints nobody has re-probed, and
some of which genuinely see - and record a POSITIVE CLAIM that they are
blind. The operator would then read "does not support images" from the UI and
believe a fact this migration invented. That is the invisible downgrade R1
exists to forbid, and it is exactly what a well-meaning "sensible default"
backfill looks like from the inside.

The cost of NULL is honest and small: every pre-existing endpoint shows
`NOT PROBED` until someone presses Probe. The cost of False would be a lie
that never expires.

**No video column.** The OpenAI chat-completions user-content-part vocabulary
is `text` / `image_url` / `input_audio` / `file`; there is no `video_url` and
no `input_video`. LazyAF cannot SEND video to any conforming server, so a
`supports_video` column would be NULL on every row forever - schema rot with
extra steps. Video is a property of the wire format, is declared as a constant
(`UNREPRESENTABLE_MODALITIES` in `app/models/model_endpoint.py`) and is
projected to the UI as a permanently-explained state. If vLLM's
OpenAI-incompatible `video_url` extension is ever wanted it lands as an
explicitly named, `server_kind`-gated vendor field - never as a general
capability.

**No index.** Neither column is a lookup predicate; the UI reads them off rows
it already has, and dispatch reads them off the row it already resolved. An
index here would be two more objects to keep in sync for no query.

Guard note (same as 0002/0004/0005/0006/0007/0009/0010/0011/0012): a
pre-alembic database adopted at startup is healed by
`Base.metadata.create_all` - which builds the CURRENT model schema, these two
columns included - before it is stamped and upgraded. Both steps are therefore
guarded by a column-existence check and the revision is re-runnable.

Numbering note: head on disk when this was written was `0012`
(workspaces_per_worker), so this takes `0013`. `upcoming/wave10-v1-retirement.md`
still labels its two UNWRITTEN migrations `0012` and `0013`; `0012` was
already taken before this revision existed, and that plan's own guidance says
to take the next free numbers. It now needs `0014`/`0015`. Flagged, not
edited.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-31

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0013'
down_revision: Union[str, Sequence[str], None] = '0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

_TABLE = 'model_endpoints'

#: Three-state, nullable, NO server_default. A `server_default='0'` would be a
#: backfill wearing a different hat: every row inserted by a client that does
#: not name the column would silently become False instead of NULL. The model
#: declares no default either, so `ModelEndpoint()` constructs with None.
_MODALITY_COLUMNS = ('supports_images', 'supports_audio')


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    columns = {col['name'] for col in inspector.get_columns(_TABLE)}
    missing = [name for name in _MODALITY_COLUMNS if name not in columns]
    for name in missing:
        op.add_column(_TABLE, sa.Column(name, sa.Boolean(), nullable=True))

    if missing:
        # Say out loud that nothing was backfilled, because "the migration ran
        # and every endpoint now says NOT PROBED" is a support question, and
        # this line is the answer to it.
        count = bind.execute(
            sa.text(f"SELECT COUNT(*) FROM {_TABLE}")  # noqa: S608 - fixed name
        ).scalar_one()
        logger.info(
            "0013: added %s to model_endpoints. %s existing endpoint row(s) "
            "read NULL - 'not probed' - and NOT false. Re-probe each endpoint "
            "to learn whether it accepts image or audio content parts; a "
            "backfill to false would have been this migration inventing a "
            "capability claim it never observed",
            ", ".join(missing),
            count,
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops both columns. The observations are lost, which is correct and
    recoverable: they are re-derivable by pressing Probe, unlike the workspace
    lanes 0012's downgrade has to delete. Plain ALTER even on SQLite - neither
    column is indexed.
    """
    inspector = sa.inspect(op.get_bind())
    columns = {col['name'] for col in inspector.get_columns(_TABLE)}

    present = [name for name in _MODALITY_COLUMNS if name in columns]
    if present:
        with op.batch_alter_table(_TABLE) as batch_op:
            for name in present:
                batch_op.drop_column(name)
