"""Add manager_id column to crm_users for reporting hierarchy.

Revision ID: 0010
Revises: 0009
"""

revision = "0010"
down_revision = "0009"

import sqlalchemy as sa
from alembic import op


def upgrade():
    with op.batch_alter_table("crm_users") as batch_op:
        batch_op.add_column(sa.Column("manager_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_crm_users_manager", "crm_users", ["manager_id"], ["id"])


def downgrade():
    with op.batch_alter_table("crm_users") as batch_op:
        batch_op.drop_constraint("fk_crm_users_manager", type_="foreignkey")
        batch_op.drop_column("manager_id")
