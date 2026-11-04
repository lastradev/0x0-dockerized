"""preserve original filenames per upload

Revision ID: a1b2c3d4e5f6
Revises: 0d5b8c4b9f0d
Create Date: 2026-04-23 23:40:00.000000

"""

from alembic import op
from alembic import context
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '0d5b8c4b9f0d'


def upgrade():
    dialect_name = context.get_context().dialect.name

    op.create_table(
        'file_new',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(), nullable=True),
        sa.Column('token', sa.String(length=32), nullable=False),
        sa.Column('ext', sa.UnicodeText(), nullable=True),
        sa.Column('mime', sa.UnicodeText(), nullable=True),
        sa.Column('addr', sa.UnicodeText(), nullable=True),
        sa.Column('removed', sa.Boolean(), nullable=True),
        sa.Column('nsfw_score', sa.Float(), nullable=True),
        sa.Column('original_name', sa.UnicodeText(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token', name='uq_file_new_token'),
    )

    op.execute(
        """
        INSERT INTO file_new (id, sha256, token, ext, mime, addr, removed, nsfw_score, original_name)
        SELECT id, sha256, token, ext, mime, addr, removed, nsfw_score, NULL
        FROM file
        """
    )
    if dialect_name == 'postgresql':
        op.execute(
            """
            SELECT setval(
                pg_get_serial_sequence('file_new', 'id'),
                COALESCE((SELECT MAX(id) FROM file_new), 1),
                (SELECT COUNT(*) > 0 FROM file_new)
            )
            """
        )

    op.drop_table('file')
    op.rename_table('file_new', 'file')


def downgrade():
    op.create_table(
        'file_old',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(), nullable=True),
        sa.Column('token', sa.String(length=32), nullable=False),
        sa.Column('ext', sa.UnicodeText(), nullable=True),
        sa.Column('mime', sa.UnicodeText(), nullable=True),
        sa.Column('addr', sa.UnicodeText(), nullable=True),
        sa.Column('removed', sa.Boolean(), nullable=True),
        sa.Column('nsfw_score', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('sha256'),
        sa.UniqueConstraint('token', name='uq_file_token'),
    )

    op.execute(
        """
        INSERT INTO file_old (id, sha256, token, ext, mime, addr, removed, nsfw_score)
        SELECT DISTINCT ON (sha256) id, sha256, token, ext, mime, addr, removed, nsfw_score
        FROM file
        ORDER BY sha256, id
        """
    )

    op.drop_table('file')
    op.rename_table('file_old', 'file')
