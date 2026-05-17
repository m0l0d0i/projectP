"""NodeHealthSample + NodeRegistry probe denorm fields (FEA-ADMIN-NODE-MONITOR #1)

Revision ID: 20260510_000040
Revises: 20260510_000039
Create Date: 2026-05-10 00:00:40.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000040'
down_revision = '20260510_000039'
branch_labels = None
depends_on = None


NODE_REGISTRY_TABLE_NAME = 'node_registry'
NODE_HEALTH_SAMPLES_TABLE_NAME = 'node_health_samples'
PROBE_STATUS_ENUM_NAME = 'node_health_probe_status'
PROBE_STATUS_VALUES = ('ok', 'degraded', 'down', 'error')

NEW_AUDIT_VALUES = (
    'node_health_alert',
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{PROBE_STATUS_ENUM_NAME}') THEN "
                f"CREATE TYPE {PROBE_STATUS_ENUM_NAME} AS ENUM "
                f"({', '.join(repr(v) for v in PROBE_STATUS_VALUES)}); "
                "END IF; END $$;"
            )
        )
        for value in NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )

    probe_status_type = sa.Enum(
        *PROBE_STATUS_VALUES,
        name=PROBE_STATUS_ENUM_NAME,
        create_type=False,
    )

    op.create_table(
        NODE_HEALTH_SAMPLES_TABLE_NAME,
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column(
            'node_id',
            sa.Integer(),
            sa.ForeignKey(f'{NODE_REGISTRY_TABLE_NAME}.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column(
            'ts',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
        sa.Column('status', probe_status_type, nullable=False),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('users_total', sa.Integer(), nullable=True),
        sa.Column('users_online', sa.Integer(), nullable=True),
        sa.Column('error_text', sa.Text(), nullable=True),
        sa.CheckConstraint(
            'latency_ms IS NULL OR latency_ms >= 0',
            name='ck_node_health_samples_latency_non_negative',
        ),
        sa.CheckConstraint(
            'users_total IS NULL OR users_total >= 0',
            name='ck_node_health_samples_users_total_non_negative',
        ),
        sa.CheckConstraint(
            'users_online IS NULL OR users_online >= 0',
            name='ck_node_health_samples_users_online_non_negative',
        ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_index(
        'ix_node_health_samples_node_ts',
        NODE_HEALTH_SAMPLES_TABLE_NAME,
        ['node_id', sa.text('ts DESC')],
        unique=False,
    )
    op.create_index(
        'ix_node_health_samples_ts',
        NODE_HEALTH_SAMPLES_TABLE_NAME,
        ['ts'],
        unique=False,
    )

    # denorm probe fields on node_registry
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('last_latency_ms', sa.Integer(), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('last_users_online', sa.Integer(), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('last_users_total', sa.Integer(), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('last_probe_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column(
            'consecutive_fail_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
    )
    op.create_check_constraint(
        'ck_node_registry_consecutive_fail_count_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        'consecutive_fail_count >= 0',
    )
    op.create_check_constraint(
        'ck_node_registry_last_latency_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        'last_latency_ms IS NULL OR last_latency_ms >= 0',
    )
    op.create_check_constraint(
        'ck_node_registry_last_users_online_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        'last_users_online IS NULL OR last_users_online >= 0',
    )
    op.create_check_constraint(
        'ck_node_registry_last_users_total_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        'last_users_total IS NULL OR last_users_total >= 0',
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_node_registry_last_users_total_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        type_='check',
    )
    op.drop_constraint(
        'ck_node_registry_last_users_online_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        type_='check',
    )
    op.drop_constraint(
        'ck_node_registry_last_latency_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        type_='check',
    )
    op.drop_constraint(
        'ck_node_registry_consecutive_fail_count_non_negative',
        NODE_REGISTRY_TABLE_NAME,
        type_='check',
    )
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'consecutive_fail_count')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'last_probe_at')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'last_users_total')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'last_users_online')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'last_latency_ms')

    op.drop_index('ix_node_health_samples_ts', table_name=NODE_HEALTH_SAMPLES_TABLE_NAME)
    op.drop_index('ix_node_health_samples_node_ts', table_name=NODE_HEALTH_SAMPLES_TABLE_NAME)
    op.drop_table(NODE_HEALTH_SAMPLES_TABLE_NAME)

    bind = op.get_bind()
    if bind.dialect.name == 'postgresql':
        op.execute(sa.text(f'DROP TYPE IF EXISTS {PROBE_STATUS_ENUM_NAME}'))
    # PG не удаляет enum value — namespace останется.
