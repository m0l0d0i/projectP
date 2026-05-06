"""drop legacy landing settings

Revision ID: 20260405_000013
Revises: 20260405_000012
Create Date: 2026-04-05 03:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260405_000013'
down_revision = '20260405_000012'
branch_labels = None
depends_on = None


LEGACY_TABLE_NAME = 'landing_settings'
LEGACY_SINGLETON_CONSTRAINT = 'ck_landing_settings_singleton_id'


def upgrade() -> None:
    op.drop_table(LEGACY_TABLE_NAME)


def downgrade() -> None:
    op.create_table(
        LEGACY_TABLE_NAME,
        sa.Column('id', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('brand_name', sa.String(length=128), nullable=False, server_default=sa.text("'😎 SwoiVPN'")),
        sa.Column(
            'page_title',
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'😎 SwoiVPN — Профиль подписки'"),
        ),
        sa.Column(
            'hero_title',
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'Добро пожаловать в 😎 SwoiVPN'"),
        ),
        sa.Column(
            'hero_text',
            sa.Text(),
            nullable=False,
            server_default=sa.text(
                "'Здесь вы можете быстро подключить VPN, посмотреть статус подписки и открыть инструкции для своей платформы.'"
            ),
        ),
        sa.Column(
            'connect_button_text',
            sa.String(length=128),
            nullable=False,
            server_default=sa.text("'Подключить в 1 клик'"),
        ),
        sa.Column(
            'support_text',
            sa.Text(),
            nullable=True,
            server_default=sa.text(
                "'Если приложение ещё не установлено — сначала откройте инструкцию для своей платформы, затем подключитесь по кнопке ниже.'"
            ),
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.CheckConstraint('id = 1', name=LEGACY_SINGLETON_CONSTRAINT),
        sa.PrimaryKeyConstraint('id'),
    )
