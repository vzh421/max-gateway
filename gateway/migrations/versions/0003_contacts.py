"""Контакты телефона: список должников для сопоставления по ФИО и классификация контактов

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-26 09:58:44.404285
"""

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('debtors',
    sa.Column('case_id', sa.String(length=64), nullable=False),
    sa.Column('case_number', sa.String(length=64), nullable=True),
    sa.Column('debtor_name', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('case_id')
    )
    op.create_table('phone_contacts',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('resource_name', sa.String(length=128), nullable=False),
    sa.Column('phone', sa.String(length=11), nullable=False),
    sa.Column('display_name', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('case_ids', sa.JSON(), nullable=True),
    sa.Column('match_reason', sa.Text(), nullable=True),
    sa.Column('decided_by', sa.String(length=16), nullable=False),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("decided_by IN ('auto', 'manager')", name='ck_phone_contacts_decided_by'),
    sa.CheckConstraint("status IN ('debtor', 'review', 'other')", name='ck_phone_contacts_status'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('resource_name', 'phone', name='uq_phone_contacts_res_phone')
    )
    with op.batch_alter_table('phone_contacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_phone_contacts_phone'), ['phone'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('phone_contacts', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_phone_contacts_phone'))

    op.drop_table('phone_contacts')
    op.drop_table('debtors')
