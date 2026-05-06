"""create marzban page settings and migrate legacy landing settings

Revision ID: 20260405_000012
Revises: 20260405_000011
Create Date: 2026-04-05 02:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260405_000012'
down_revision = '20260405_000011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'marzban_page_settings',
        sa.Column('id', sa.Integer(), nullable=False, server_default=sa.text('1')),
        sa.Column('brand_name', sa.String(length=128), nullable=False, server_default=sa.text("'😎 SwoiVPN'")),
        sa.Column('page_title', sa.String(length=255), nullable=False, server_default=sa.text("'😎 SwoiVPN — Подписка'")),
        sa.Column('hero_title', sa.String(length=255), nullable=False, server_default=sa.text("'Добро пожаловать в 😎 SwoiVPN'")),
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
            'connect_hint_text',
            sa.Text(),
            nullable=True,
            server_default=sa.text(
                "'Если приложение уже установлено, просто откройте ссылку подписки. Если нет — сначала выберите свою платформу ниже.'"
            ),
        ),
        sa.Column(
            'support_text',
            sa.Text(),
            nullable=True,
            server_default=sa.text(
                "'Если приложение ещё не установлено — сначала откройте инструкцию для своей платформы, затем подключитесь по кнопке ниже.'"
            ),
        ),
        sa.Column(
            'platforms_title',
            sa.String(length=255),
            nullable=False,
            server_default=sa.text("'Платформы подключения'"),
        ),
        sa.Column(
            'platforms_subtitle',
            sa.Text(),
            nullable=True,
            server_default=sa.text("'Выберите свою платформу, чтобы открыть приложение и инструкцию.'"),
        ),
        sa.Column('show_usage_block', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('show_subscription_copy_button', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('show_platform_cards', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text("timezone('utc', now())")),
        sa.CheckConstraint('id = 1', name='ck_marzban_page_settings_singleton_id'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.execute(
        """
        INSERT INTO marzban_page_settings (
            id,
            brand_name,
            page_title,
            hero_title,
            hero_text,
            connect_button_text,
            connect_hint_text,
            support_text,
            platforms_title,
            platforms_subtitle,
            show_usage_block,
            show_subscription_copy_button,
            show_platform_cards
        )
        SELECT
            1,
            ls.brand_name,
            COALESCE(NULLIF(ls.page_title, ''), '😎 SwoiVPN — Подписка'),
            ls.hero_title,
            ls.hero_text,
            ls.connect_button_text,
            'Если приложение уже установлено, просто откройте ссылку подписки. Если нет — сначала выберите свою платформу ниже.',
            ls.support_text,
            'Платформы подключения',
            'Выберите свою платформу, чтобы открыть приложение и инструкцию.',
            true,
            true,
            true
        FROM landing_settings ls
        WHERE ls.id = 1
        ON CONFLICT (id) DO NOTHING
        """
    )

    op.execute(
        """
        INSERT INTO marzban_page_settings (
            id,
            brand_name,
            page_title,
            hero_title,
            hero_text,
            connect_button_text,
            connect_hint_text,
            support_text,
            platforms_title,
            platforms_subtitle,
            show_usage_block,
            show_subscription_copy_button,
            show_platform_cards
        )
        VALUES (
            1,
            '😎 SwoiVPN',
            '😎 SwoiVPN — Подписка',
            'Добро пожаловать в 😎 SwoiVPN',
            'Здесь вы можете быстро подключить VPN, посмотреть статус подписки и открыть инструкции для своей платформы.',
            'Подключить в 1 клик',
            'Если приложение уже установлено, просто откройте ссылку подписки. Если нет — сначала выберите свою платформу ниже.',
            'Если приложение ещё не установлено — сначала откройте инструкцию для своей платформы, затем подключитесь по кнопке ниже.',
            'Платформы подключения',
            'Выберите свою платформу, чтобы открыть приложение и инструкцию.',
            true,
            true,
            true
        )
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_table('marzban_page_settings')
