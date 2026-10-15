"""add user full name

Revision ID: 4d0f3d0a9c1
Revises: ad4701a70fb5
Create Date: 2026-10-15 17:40:00

"""
from alembic import op
import sqlalchemy as sa


revision = "4d0f3d0a9c1"
down_revision = "ad4701a70fb5"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("full_name", sa.String(length=160), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("full_name")
