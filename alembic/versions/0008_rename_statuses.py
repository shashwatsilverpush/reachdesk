"""Rename mail_1_sent → contacted, mail_2_sent → following_up.

Revision ID: 0008
Revises: 0007
"""

revision = "0008"
down_revision = "0007"

from alembic import op


def upgrade():
    op.execute("UPDATE leads SET status = 'contacted' WHERE status = 'mail_1_sent'")
    op.execute("UPDATE leads SET status = 'following_up' WHERE status = 'mail_2_sent'")


def downgrade():
    op.execute("UPDATE leads SET status = 'mail_1_sent' WHERE status = 'contacted'")
    op.execute("UPDATE leads SET status = 'mail_2_sent' WHERE status = 'following_up'")
