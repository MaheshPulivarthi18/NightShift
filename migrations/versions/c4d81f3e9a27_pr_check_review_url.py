"""pr check review url

Revision ID: c4d81f3e9a27
Revises: 9b1e4c7a2d10
Create Date: 2026-10-31 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4d81f3e9a27'
down_revision: Union[str, Sequence[str], None] = '9b1e4c7a2d10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('pr_checks', sa.Column('review_url', sa.String(length=300), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pr_checks', 'review_url')
