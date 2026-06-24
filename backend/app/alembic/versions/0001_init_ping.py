"""init ping

Revision ID: 0001init
Revises:
Create Date: 2026-06-24 00:00:00.000000

"""
import sqlalchemy as sa
import sqlmodel.sql.sqltypes
from alembic import op

# revision identifiers, used by Alembic.
revision = "0001init"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ping",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("ping")
