"""Specification layer: features, user stories, criteria, templates (Phase 12.2.5).

Adds:
- features / user_stories / acceptance_criteria: the shallow spec hierarchy
  (app/models/spec.py)
- prompt_templates: named, reusable agent prompt bodies
- cards.feature_id / cards.user_story_id: nullable links from execution
  cards back into the spec layer (app/models/card.py)

Guard note: a pre-alembic database adopted at startup is healed by
Base.metadata.create_all (which builds the CURRENT model schema) before it
is stamped and upgraded — see _adopt_unversioned in app/database.py. The
objects this revision creates can therefore already exist when it runs, so
each is created only if absent. The migration-parity integration test
(tdd/integration/test_migrations.py) pins the resulting schema either way.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0003'
down_revision: Union[str, Sequence[str], None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table('features'):
        op.create_table('features',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('repo_ids', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
        )

    if not inspector.has_table('user_stories'):
        op.create_table('user_stories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('feature_id', sa.String(length=36), nullable=False),
        sa.Column('title', sa.String(length=255), nullable=False),
        sa.Column('narrative', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['feature_id'], ['features.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_user_stories_feature_id'), 'user_stories', ['feature_id'], unique=False)

    if not inspector.has_table('acceptance_criteria'):
        op.create_table('acceptance_criteria',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_story_id', sa.String(length=36), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('required', sa.Boolean(), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_story_id'], ['user_stories.id'], ),
        sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_acceptance_criteria_user_story_id'), 'acceptance_criteria', ['user_story_id'], unique=False)

    if not inspector.has_table('prompt_templates'):
        op.create_table('prompt_templates',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
        )

    cards_columns = {col['name'] for col in sa.inspect(op.get_bind()).get_columns('cards')}
    if 'feature_id' not in cards_columns:
        # Raw ADD COLUMN with an inline (unnamed) REFERENCES clause — SQLite
        # supports this natively for nullable columns, and it is identical to
        # how Base.metadata.create_all renders these FKs. (op.add_column with
        # an attached ForeignKey would try a separate ADD CONSTRAINT, which
        # the SQLite dialect refuses.)
        op.execute('ALTER TABLE cards ADD COLUMN feature_id VARCHAR(36) REFERENCES features (id)')
        op.execute('ALTER TABLE cards ADD COLUMN user_story_id VARCHAR(36) REFERENCES user_stories (id)')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('cards', 'user_story_id')
    op.drop_column('cards', 'feature_id')
    op.drop_table('prompt_templates')
    op.drop_index(op.f('ix_acceptance_criteria_user_story_id'), table_name='acceptance_criteria')
    op.drop_table('acceptance_criteria')
    op.drop_index(op.f('ix_user_stories_feature_id'), table_name='user_stories')
    op.drop_table('user_stories')
    op.drop_table('features')
