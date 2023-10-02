"""user url and display name

Revision ID: ef1345425b0c
Revises: 475cc826bde8
Create Date: 2023-09-28 17:49:20.538475

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ef1345425b0c'
down_revision: Union[str, None] = '475cc826bde8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.add_column(sa.Column('user_url', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('display_name', sa.String(), nullable=True))

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table('entries', schema=None) as batch_op:
        batch_op.drop_column('display_name')
        batch_op.drop_column('user_url')

    # ### end Alembic commands ###