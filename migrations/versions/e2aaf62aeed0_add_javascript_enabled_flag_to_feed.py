"""add javascript enabled flag to feed

Revision ID: e2aaf62aeed0
Revises: b975c1a56ab3
Create Date: 2023-09-15 12:45:06.443060

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e2aaf62aeed0'
down_revision: Union[str, None] = 'b975c1a56ab3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column('feeds', sa.Column('javascript_enabled', sa.Boolean(), nullable=True))
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('feeds', 'javascript_enabled')
    # ### end Alembic commands ###