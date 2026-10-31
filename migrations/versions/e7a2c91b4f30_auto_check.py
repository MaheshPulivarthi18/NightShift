"""auto check incoming prs

Revision ID: e7a2c91b4f30
Revises: c4d81f3e9a27
Create Date: 2026-10-31 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e7a2c91b4f30'
down_revision: Union[str, Sequence[str], None] = 'c4d81f3e9a27'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('repos', sa.Column('auto_check', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('pr_checks', sa.Column('auto', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('pr_checks', sa.Column('dismissed', sa.Boolean(), server_default='false', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pr_checks', 'dismissed')
    op.drop_column('pr_checks', 'auto')
    op.drop_column('repos', 'auto_check')
