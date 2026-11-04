"""add hmac links, audit table, and make token nullable

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-10-14 12:30:00.000000

"""

from alembic import op
from alembic import context
import sqlalchemy as sa
import hmac
import os
from flask import current_app


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'


def compute_link(secret, sha256_val, ext_val, original_name_val):
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    msg = "\0".join([sha256_val or "", ext_val or "", original_name_val or ""])
    return hmac.new(secret, msg.encode("utf-8"), "sha256").hexdigest()


def upgrade():
    secret = current_app.config.get("FHOST_LINK_SECRET") or os.environ.get("FHOST_LINK_SECRET")
    if not secret:
        if current_app.config.get("TESTING"):
            secret = "test-secret"
        else:
            raise RuntimeError("FHOST_LINK_SECRET is required to backfill links during migration")

    # 1. Add link column (nullable initially for backfill)
    op.add_column('file', sa.Column('link', sa.String(length=64), nullable=True))

    # 2. Backfill link column: iterates existing rows in Python and derives HMAC-SHA256,
    # treating NULL ext and original_name values as empty strings ("").
    bind = op.get_bind()
    file_table = sa.table(
        'file',
        sa.column('id', sa.Integer()),
        sa.column('sha256', sa.String()),
        sa.column('ext', sa.UnicodeText()),
        sa.column('original_name', sa.UnicodeText()),
        sa.column('link', sa.String(length=64)),
    )
    rows = bind.execute(sa.select(file_table.c.id, file_table.c.sha256, file_table.c.ext, file_table.c.original_name)).fetchall()
    for row in rows:
        link_val = compute_link(secret, row.sha256, row.ext, row.original_name)
        bind.execute(
            file_table.update().where(file_table.c.id == row.id).values(link=link_val)
        )

    # 3. Set link NOT NULL, make token nullable, create indexes
    with op.batch_alter_table('file') as batch_op:
        batch_op.alter_column('link', existing_type=sa.String(length=64), nullable=False)
        batch_op.alter_column('token', existing_type=sa.String(length=32), nullable=True)
        batch_op.create_index('ix_file_sha256', ['sha256'])
        batch_op.create_index('ix_file_link', ['link'])
        batch_op.create_index(
            'uq_file_link',
            ['link'],
            unique=True,
            postgresql_where=sa.text("token IS NULL"),
            sqlite_where=sa.text("token IS NULL")
        )

    # 4. Create file_audit table
    op.create_table(
        'file_audit',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('sha256', sa.String(), nullable=False),
        sa.Column('link', sa.String(length=64), nullable=True),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    )
    op.create_index('ix_file_audit_sha256', 'file_audit', ['sha256'])
    op.create_index('ix_file_audit_link', 'file_audit', ['link'])


def downgrade():
    bind = op.get_bind()
    null_token_count = bind.execute(sa.text("SELECT COUNT(*) FROM file WHERE token IS NULL")).scalar()
    if null_token_count and null_token_count > 0:
        raise RuntimeError("Cannot downgrade: rows with token IS NULL exist. Clean up new rows before downgrading.")

    op.drop_index('ix_file_audit_link', table_name='file_audit')
    op.drop_index('ix_file_audit_sha256', table_name='file_audit')
    op.drop_table('file_audit')

    with op.batch_alter_table('file') as batch_op:
        batch_op.drop_index('uq_file_link')
        batch_op.drop_index('ix_file_link')
        batch_op.drop_index('ix_file_sha256')
        batch_op.alter_column('token', existing_type=sa.String(length=32), nullable=False)
        batch_op.drop_column('link')
