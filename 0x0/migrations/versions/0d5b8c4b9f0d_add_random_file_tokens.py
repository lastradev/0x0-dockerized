"""add random file tokens

Revision ID: 0d5b8c4b9f0d
Revises: 7e246705da6a
Create Date: 2026-04-15 12:50:00.000000

"""

from alembic import op
import sqlalchemy as sa
import secrets


# revision identifiers, used by Alembic.
revision = '0d5b8c4b9f0d'
down_revision = '7e246705da6a'

ALPHABET = "DEQhd2uFteibPwq0SWBInTpA_jcZL5GKz3YCR14Ulk87Jors9vNHgfaOmMXy6Vx-"
TOKEN_LENGTH = 12


def generate_token():
    return "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LENGTH))


def upgrade():
    op.add_column('file', sa.Column('token', sa.String(length=32), nullable=True))

    bind = op.get_bind()
    file_table = sa.table(
        'file',
        sa.column('id', sa.Integer()),
        sa.column('token', sa.String(length=32)),
    )

    rows = bind.execute(sa.select(file_table.c.id)).fetchall()
    used_tokens = set()

    for row in rows:
        token = generate_token()
        while token in used_tokens:
            token = generate_token()
        used_tokens.add(token)
        bind.execute(
            file_table.update().where(file_table.c.id == row.id).values(token=token)
        )

    with op.batch_alter_table('file') as batch_op:
        batch_op.alter_column('token', existing_type=sa.String(length=32), nullable=False)
        batch_op.create_unique_constraint('uq_file_token', ['token'])


def downgrade():
    with op.batch_alter_table('file') as batch_op:
        batch_op.drop_constraint('uq_file_token', type_='unique')
        batch_op.drop_column('token')
