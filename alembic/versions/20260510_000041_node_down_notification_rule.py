"""Seed `node_down` notification rule (FEA-ADMIN-NODE-MONITOR #2)

Revision ID: 20260510_000041
Revises: 20260510_000040
Create Date: 2026-05-10 00:00:41.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000041'
down_revision = '20260510_000040'
branch_labels = None
depends_on = None


TABLE_NAME = 'notification_rules'

_NODE_DOWN_TEMPLATE = (
    '🚨 <b>Нода {{ node_display_name }} ({{ node_code }}) недоступна</b>\n'
    'Подряд {{ consecutive_fails }} fail-probe.\n'
    '{% if error_text %}Ошибка: <code>{{ error_text }}</code>\n{% endif %}'
    'Проверьте /admin/nodes/{{ node_id }}.'
)

_NODE_DOWN_RULE = {
    'code': 'node_down',
    'is_enabled': True,
    'template_text': _NODE_DOWN_TEMPLATE,
    'cooldown_seconds': 21600,  # 6 часов — anti-spam между алертами одного админа
    'priority': 200,
    'description': 'Алерт при ≥5 подряд fail-probe ноды (admin-only)',
}


def upgrade() -> None:
    rules_table = sa.table(
        TABLE_NAME,
        sa.column('code', sa.String()),
        sa.column('is_enabled', sa.Boolean()),
        sa.column('template_text', sa.Text()),
        sa.column('cooldown_seconds', sa.Integer()),
        sa.column('priority', sa.Integer()),
        sa.column('description', sa.String()),
    )

    bind = op.get_bind()
    existing = bind.execute(
        sa.text('SELECT 1 FROM notification_rules WHERE code = :code LIMIT 1').bindparams(
            code=_NODE_DOWN_RULE['code'],
        )
    ).first()
    if existing is None:
        op.bulk_insert(rules_table, [_NODE_DOWN_RULE])


def downgrade() -> None:
    op.execute(
        sa.text('DELETE FROM notification_rules WHERE code = :code').bindparams(
            code=_NODE_DOWN_RULE['code'],
        )
    )
