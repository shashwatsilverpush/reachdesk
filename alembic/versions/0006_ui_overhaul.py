"""UI overhaul: auth fields, sequences, activities

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- crm_users: add auth fields ---
    with op.batch_alter_table("crm_users") as batch_op:
        batch_op.add_column(sa.Column("password_hash", sa.String(255)))
        batch_op.add_column(sa.Column("is_admin", sa.Boolean, nullable=False, server_default="0"))

    # --- leads: migrate 'contacted' → 'mail_1_sent' ---
    op.execute("UPDATE leads SET status = 'mail_1_sent' WHERE status = 'contacted'")

    # --- sequences ---
    op.create_table(
        "sequences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("tenant_id", sa.String(64), index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("crm_users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "sequence_steps",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sequence_id", sa.Integer, sa.ForeignKey("sequences.id"), nullable=False),
        sa.Column("step_number", sa.Integer, nullable=False),
        sa.Column("delay_days", sa.Integer, nullable=False, server_default="0"),
        sa.Column("subject_template", sa.String(500), nullable=False),
        sa.Column("body_template", sa.String(10000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "lead_sequences",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lead_id", sa.Integer, sa.ForeignKey("leads.id"), nullable=False, unique=True),
        sa.Column("sequence_id", sa.Integer, sa.ForeignKey("sequences.id"), nullable=False),
        sa.Column("current_step", sa.Integer, nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("paused", sa.Boolean, nullable=False, server_default="0"),
    )

    op.create_table(
        "sequence_emails",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lead_sequence_id", sa.Integer, sa.ForeignKey("lead_sequences.id"), nullable=False),
        sa.Column("step_id", sa.Integer, sa.ForeignKey("sequence_steps.id"), nullable=False),
        sa.Column("subject", sa.String(500), nullable=False),
        sa.Column("body", sa.String(10000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "lead_activities",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("lead_id", sa.Integer, sa.ForeignKey("leads.id"), nullable=False, index=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("crm_users.id")),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("detail", sa.String(1000)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("lead_activities")
    op.drop_table("sequence_emails")
    op.drop_table("lead_sequences")
    op.drop_table("sequence_steps")
    op.drop_table("sequences")
    with op.batch_alter_table("crm_users") as batch_op:
        batch_op.drop_column("is_admin")
        batch_op.drop_column("password_hash")
    op.execute("UPDATE leads SET status = 'contacted' WHERE status = 'mail_1_sent'")
