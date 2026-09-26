"""Начальная схема

Revision ID: 0001
Revises: 
Create Date: 2026-09-26 09:14:53.628524
"""

from alembic import op
import sqlalchemy as sa


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('app_state',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('bot_bindings',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('bot_user_id', sa.BigInteger(), nullable=False),
    sa.Column('bot_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('case_id', sa.String(length=64), nullable=False),
    sa.Column('token', sa.String(length=128), nullable=True),
    sa.Column('pd_consent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('bound_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('bot_user_id', 'case_id', name='uq_bot_bindings_user_case')
    )
    op.create_table('debtor_phones',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('case_id', sa.String(length=64), nullable=False),
    sa.Column('phone', sa.String(length=11), nullable=False),
    sa.Column('source', sa.String(length=64), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('case_id', 'phone', name='uq_debtor_phones_case_phone')
    )
    with op.batch_alter_table('debtor_phones', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_debtor_phones_phone'), ['phone'], unique=False)

    op.create_table('link_tokens',
    sa.Column('token', sa.String(length=128), nullable=False),
    sa.Column('case_id', sa.String(length=64), nullable=True),
    sa.Column('personal_chat_id', sa.BigInteger(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('used_by_bot_user_id', sa.BigInteger(), nullable=True),
    sa.PrimaryKeyConstraint('token')
    )
    op.create_table('message_log',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('channel', sa.String(length=16), nullable=False),
    sa.Column('direction', sa.String(length=3), nullable=False),
    sa.Column('chat_id', sa.BigInteger(), nullable=False),
    sa.Column('external_message_id', sa.BigInteger(), nullable=True),
    sa.Column('sender_id', sa.BigInteger(), nullable=True),
    sa.Column('text', sa.Text(), nullable=True),
    sa.Column('attachments', sa.JSON(), nullable=True),
    sa.Column('case_id', sa.String(length=64), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=True),
    sa.Column('status', sa.String(length=48), nullable=True),
    sa.Column('dry_run', sa.Boolean(), nullable=False),
    sa.Column('raw', sa.JSON(), nullable=True),
    sa.CheckConstraint("channel IN ('personal', 'bot')", name='ck_message_log_channel'),
    sa.CheckConstraint("direction IN ('in', 'out')", name='ck_message_log_direction'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('channel', 'direction', 'chat_id', 'external_message_id', name='uq_message_log_event')
    )
    with op.batch_alter_table('message_log', schema=None) as batch_op:
        batch_op.create_index('ix_message_log_chat', ['channel', 'chat_id', 'created_at'], unique=False)
        batch_op.create_index('ix_message_log_out_recent', ['channel', 'direction', 'dry_run', 'created_at'], unique=False)

    op.create_table('personal_chats',
    sa.Column('chat_id', sa.BigInteger(), autoincrement=False, nullable=False),
    sa.Column('max_user_id', sa.BigInteger(), nullable=True),
    sa.Column('phone', sa.String(length=11), nullable=True),
    sa.Column('case_id', sa.String(length=64), nullable=True),
    sa.Column('first_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_incoming_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_auto_reply_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('chat_id')
    )
    with op.batch_alter_table('personal_chats', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_personal_chats_max_user_id'), ['max_user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_personal_chats_phone'), ['phone'], unique=False)

    op.create_table('whitelist',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('phone', sa.String(length=11), nullable=True),
    sa.Column('max_user_id', sa.BigInteger(), nullable=True),
    sa.Column('source', sa.String(length=16), nullable=False),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('address_book', 'manual')", name='ck_whitelist_source'),
    sa.CheckConstraint('phone IS NOT NULL OR max_user_id IS NOT NULL', name='ck_whitelist_has_key'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('max_user_id', name='uq_whitelist_user'),
    sa.UniqueConstraint('phone', name='uq_whitelist_phone')
    )


def downgrade() -> None:
    op.drop_table('whitelist')
    with op.batch_alter_table('personal_chats', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_personal_chats_phone'))
        batch_op.drop_index(batch_op.f('ix_personal_chats_max_user_id'))

    op.drop_table('personal_chats')
    with op.batch_alter_table('message_log', schema=None) as batch_op:
        batch_op.drop_index('ix_message_log_out_recent')
        batch_op.drop_index('ix_message_log_chat')

    op.drop_table('message_log')
    op.drop_table('link_tokens')
    with op.batch_alter_table('debtor_phones', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_debtor_phones_phone'))

    op.drop_table('debtor_phones')
    op.drop_table('bot_bindings')
    op.drop_table('app_state')
