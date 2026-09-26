"""Перенаправление в бот: кандидаты дел в токене, отметка об исчерпанном лимите

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26 09:35:22.155927
"""

from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('link_tokens', schema=None) as batch_op:
        batch_op.add_column(sa.Column('candidate_case_ids', sa.JSON(), nullable=True))
        batch_op.create_index(batch_op.f('ix_link_tokens_personal_chat_id'), ['personal_chat_id'], unique=False)

    with op.batch_alter_table('personal_chats', schema=None) as batch_op:
        batch_op.add_column(sa.Column('limit_notified_at', sa.DateTime(timezone=True), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('personal_chats', schema=None) as batch_op:
        batch_op.drop_column('limit_notified_at')

    with op.batch_alter_table('link_tokens', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_link_tokens_personal_chat_id'))
        batch_op.drop_column('candidate_case_ids')

