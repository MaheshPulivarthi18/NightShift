"""pr check finding marks

Revision ID: f3b8d27a6c41
Revises: e7a2c91b4f30
Create Date: 2026-11-01 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'f3b8d27a6c41'
down_revision: Union[str, Sequence[str], None] = 'e7a2c91b4f30'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('pr_checks', sa.Column('marks', postgresql.JSONB(astext_type=sa.Text()),
                                         server_default=sa.text("'{}'"), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('pr_checks', 'marks')
