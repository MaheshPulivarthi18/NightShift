"""repo role and fetch version

Revision ID: 9c530470385a
Revises: 4b2645c47a2c
Create Date: 2026-10-20 22:26:00.723580

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import pgvector.sqlalchemy

# revision identifiers, used by Alembic.
revision: str = '9c530470385a'
down_revision: Union[str, Sequence[str], None] = '4b2645c47a2c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("repos", sa.Column("role", sa.String(length=20), server_default="contributor", nullable=False))
    op.add_column("repos", sa.Column("fetch_version", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("repos", "fetch_version")
    op.drop_column("repos", "role")
