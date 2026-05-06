"""add landing settings singleton table

Revision ID: 20260402_000008
Revises: 20260401_000007
Create Date: 2026-04-02 20:40:00
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = '20260402_000008'
down_revision = '20260401_000007'
branch_labels = None
depends_on = None

DEFAULT_BRAND_NAME = '😎 SwoiVPN'
DEFAULT_PAGE_TITLE = '😎 SwoiVPN — Профиль подписки'
DEFAULT_HERO_TITLE = 'Добро пожаловать в 😎 SwoiVPN'
DEFAULT_HERO_TEXT = (
    'Здесь вы можете быстро подключить VPN, посмотреть статус подписки и открыть инструкции для своей платформы.'
)
DEFAULT_CONNECT_BUTTON_TEXT = 'Подключить в 1 клик'
DEFAULT_SUPPORT_TEXT = (
    'Если приложение ещё не установлено — сначала откройте инструкцию для своей платформы, затем подключитесь по кнопке ниже.'
)


def upgrade() -> None:
    op.create_table(
        'landing_settings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('brand_name', sa.String(length=128), nullable=False, server_default=DEFAULT_BRAND_NAME),
        sa.Column('page_title', sa.String(length=255), nullable=False, server_default=DEFAULT_PAGE_TITLE),
        sa.Column('hero_title', sa.String(length=255), nullable=False, server_default=DEFAULT_HERO_TITLE),
        sa.Column(
            'hero_text',
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_HERO_TEXT,
        ),
        sa.Column(
            'connect_button_text',
            sa.String(length=128),
            nullable=False,
            server_default=DEFAULT_CONNECT_BUTTON_TEXT,
        ),
        sa.Column(
            'support_text',
            sa.Text(),
            nullable=True,
            server_default=DEFAULT_SUPPORT_TEXT,
        ),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.CheckConstraint('id = 1', name='ck_landing_settings_singleton_id'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO landing_settings (
                id,
                brand_name,
                page_title,
                hero_title,
                hero_text,
                connect_button_text,
                support_text,
                created_at,
                updated_at
            )
            VALUES (
                1,
                :brand_name,
                :page_title,
                :hero_title,
                :hero_text,
                :connect_button_text,
                :support_text,
                now(),
                now()
            )
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(
            brand_name=DEFAULT_BRAND_NAME,
            page_title=DEFAULT_PAGE_TITLE,
            hero_title=DEFAULT_HERO_TITLE,
            hero_text=DEFAULT_HERO_TEXT,
            connect_button_text=DEFAULT_CONNECT_BUTTON_TEXT,
            support_text=DEFAULT_SUPPORT_TEXT,
        )
    )


def downgrade() -> None:
    op.drop_table('landing_settings')