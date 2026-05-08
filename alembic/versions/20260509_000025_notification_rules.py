"""add notification_rules with default seed (FEA-NOTIF backend)

Revision ID: 20260509_000025
Revises: 20260507_000024
Create Date: 2026-05-09 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260509_000025'
down_revision = '20260507_000024'
branch_labels = None
depends_on = None


TABLE_NAME = 'notification_rules'


# Default rules — текст и cooldown совпадают с тем, что было захардкожено
# в app/services/notifications.py до FEA-NOTIF. Для кодов, которые ещё
# не имеют активного job (trial_*/weekly_usage), задаём is_enabled=false,
# чтобы admin-UI мог включить их когда соответствующий cron появится.
_DEFAULT_RULES: list[dict] = [
    {
        'code': 'expiring_3d',
        'is_enabled': True,
        'template_text': '⚠️ Ваша подписка истекает через 3 дня!',
        'cooldown_seconds': 0,
        'priority': 100,
        'description': 'За 3 дня до окончания подписки',
    },
    {
        'code': 'expiring_1d',
        'is_enabled': True,
        'template_text': '🔥 Ваша подписка истекает уже завтра!',
        'cooldown_seconds': 0,
        'priority': 110,
        'description': 'За 1 день до окончания подписки',
    },
    {
        'code': 'expired',
        'is_enabled': True,
        'template_text': '❌ Ваша подписка истекла. Доступ к VPN приостановлен.\nПожалуйста, продлите тариф.',
        'cooldown_seconds': 0,
        'priority': 120,
        'description': 'Сразу после истечения подписки',
    },
    {
        'code': 'low_traffic_90',
        'is_enabled': True,
        'template_text': '⚠️ Осталось меньше 10% трафика. Вы можете докупить трафик или продлить тариф досрочно.',
        'cooldown_seconds': 0,
        'priority': 90,
        'description': 'Достигнуто 90% использования трафика',
    },
    {
        'code': 'traffic_exhausted',
        'is_enabled': True,
        'template_text': '📉 <b>Ваш трафик почти исчерпан!</b>\nVPN скоро перестанет работать. Вы можете докупить трафик или досрочно продлить тариф.',
        'cooldown_seconds': 0,
        'priority': 130,
        'description': 'Трафик почти полностью исчерпан (≥99%)',
    },
    {
        'code': 'trial_mid',
        'is_enabled': False,
        'template_text': '🎁 Половина пробного периода уже позади! Попробуйте оформить полноценный тариф.',
        'cooldown_seconds': 86400,
        'priority': 50,
        'description': 'Через 12ч после старта триала (нет активного job)',
    },
    {
        'code': 'trial_last_day',
        'is_enabled': False,
        'template_text': '⏳ Триал заканчивается через 2 часа. Не теряйте доступ — оформите тариф.',
        'cooldown_seconds': 0,
        'priority': 60,
        'description': 'За 2 часа до окончания триала (нет активного job)',
    },
    {
        'code': 'trial_post_expire_rescue',
        'is_enabled': False,
        'template_text': '👋 Триал закончился вчера. Возвращайтесь — у нас есть специальное предложение для вас.',
        'cooldown_seconds': 0,
        'priority': 70,
        'description': 'Через 24ч после окончания триала (нет активного job)',
    },
    {
        'code': 'weekly_usage',
        'is_enabled': False,
        'template_text': '📊 Еженедельный отчёт по использованию VPN.',
        'cooldown_seconds': 518400,
        'priority': 30,
        'description': 'Еженедельный отчёт об использовании (нет активного job)',
    },
]


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column(
            'is_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column('template_text', sa.Text(), nullable=False),
        sa.Column('template_keyboard_json', sa.JSON(), nullable=True),
        sa.Column(
            'cooldown_seconds',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column('segment_filter_json', sa.JSON(), nullable=True),
        sa.Column(
            'priority',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.CheckConstraint(
            'cooldown_seconds >= 0',
            name='ck_notification_rules_cooldown_non_negative',
        ),
        sa.UniqueConstraint('code', name='uq_notification_rules_code'),
    )

    op.create_index(
        'ix_notification_rules_enabled_priority',
        TABLE_NAME,
        ['is_enabled', 'priority'],
    )

    rules_table = sa.table(
        TABLE_NAME,
        sa.column('code', sa.String()),
        sa.column('is_enabled', sa.Boolean()),
        sa.column('template_text', sa.Text()),
        sa.column('cooldown_seconds', sa.Integer()),
        sa.column('priority', sa.Integer()),
        sa.column('description', sa.String()),
    )
    op.bulk_insert(rules_table, _DEFAULT_RULES)


def downgrade() -> None:
    op.drop_index('ix_notification_rules_enabled_priority', table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
