"""Populate role column with defined roles based on is_admin flag.

Revision ID: 0009
Revises: 0008
"""

revision = "0009"
down_revision = "0008"

from alembic import op


def upgrade():
    op.execute("UPDATE crm_users SET role = 'admin' WHERE is_admin = 1")
    op.execute("UPDATE crm_users SET role = 'member' WHERE is_admin = 0 OR is_admin IS NULL")
    op.execute("UPDATE crm_users SET role = 'member' WHERE role IS NULL")


def downgrade():
    op.execute("UPDATE crm_users SET role = NULL")
