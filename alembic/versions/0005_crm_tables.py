"""CRM tables: crm_users, leads, mail_templates

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "crm_users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(64), index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("role", sa.String(128)),
        sa.Column("active", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "leads",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(64), index=True),
        sa.Column("assigned_to", sa.Integer, sa.ForeignKey("crm_users.id"), index=True),
        sa.Column("contact_id", sa.Integer, sa.ForeignKey("contacts.id")),
        sa.Column("first_name", sa.String(255), nullable=False),
        sa.Column("last_name", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("domain", sa.String(255), index=True),
        sa.Column("company", sa.String(255)),
        sa.Column("linkedin_url", sa.String(512)),
        sa.Column("role_title", sa.String(255)),
        sa.Column("status", sa.String(16), nullable=False, server_default="new", index=True),
        sa.Column("notes", sa.String(2000)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "mail_templates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(64), index=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("crm_users.id")),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.String(10000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("mail_templates")
    op.drop_table("leads")
    op.drop_table("crm_users")
