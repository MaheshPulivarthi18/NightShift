"""run heartbeat

Revision ID: 9b1e4c7a2d10
Revises: 70aeab7f28f0
Create Date: 2026-10-28 23:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '9b1e4c7a2d10'
down_revision: Union[str, Sequence[str], None] = '70aeab7f28f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('runs', sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('runs', 'heartbeat_at')
