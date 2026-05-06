"""create node registry foundation

Revision ID: 20260405_000014
Revises: 20260405_000013
Create Date: 2026-04-05 05:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '20260405_000014'
down_revision = '20260405_000013'
branch_labels = None
depends_on = None


NODE_HEALTH_STATUS_ENUM_NAME = 'node_health_status'
AUDIT_ACTION_ENUM_NAME = 'audit_action'
NODE_REGISTRY_TABLE_NAME = 'node_registry'


node_health_status_enum = postgresql.ENUM(
    'unknown',
    'healthy',
    'degraded',
    'unhealthy',
    'disabled',
    name=NODE_HEALTH_STATUS_ENUM_NAME,
    create_type=False,
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typname = '{NODE_HEALTH_STATUS_ENUM_NAME}'
            ) THEN
                CREATE TYPE {NODE_HEALTH_STATUS_ENUM_NAME} AS ENUM (
                    'unknown',
                    'healthy',
                    'degraded',
                    'unhealthy',
                    'disabled'
                );
            END IF;
        END$$;
        """
    )

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_type t
                WHERE t.typname = '{AUDIT_ACTION_ENUM_NAME}'
            ) THEN
                BEGIN
                    ALTER TYPE {AUDIT_ACTION_ENUM_NAME} ADD VALUE IF NOT EXISTS 'node_registry_updated';
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END;
            END IF;
        END$$;
        """
    )

    op.create_table(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('display_name', sa.String(length=128), nullable=False),
        sa.Column('api_base_url', sa.String(length=512), nullable=True),
        sa.Column('subscription_base_url', sa.String(length=512), nullable=True),
        sa.Column('location_code', sa.String(length=32), nullable=True),
        sa.Column('provider_name', sa.String(length=128), nullable=True),
        sa.Column('transport_hint', sa.String(length=64), nullable=True),
        sa.Column(
            'policy_tags',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            'capabilities_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            'is_enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'is_default',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column(
            'priority',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column(
            'weight',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column(
            'sort_order',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column(
            'health_status',
            node_health_status_enum,
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column('last_healthcheck_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_health_error', sa.Text(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('utc', now())"),
        ),
        sa.CheckConstraint('priority >= 0', name='ck_node_registry_priority_non_negative'),
        sa.CheckConstraint('weight >= 0', name='ck_node_registry_weight_non_negative'),
        sa.CheckConstraint('sort_order >= 0', name='ck_node_registry_sort_order_non_negative'),
        sa.CheckConstraint(
            "(api_base_url IS NULL) OR (char_length(trim(api_base_url)) > 0)",
            name='ck_node_registry_api_base_url_not_blank',
        ),
        sa.CheckConstraint(
            "(subscription_base_url IS NULL) OR (char_length(trim(subscription_base_url)) > 0)",
            name='ck_node_registry_subscription_base_url_not_blank',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_node_registry_code'),
    )

    op.create_index('ix_node_registry_code', NODE_REGISTRY_TABLE_NAME, ['code'], unique=False)
    op.create_index('ix_node_registry_location_code', NODE_REGISTRY_TABLE_NAME, ['location_code'], unique=False)
    op.create_index('ix_node_registry_is_enabled', NODE_REGISTRY_TABLE_NAME, ['is_enabled'], unique=False)
    op.create_index('ix_node_registry_is_default', NODE_REGISTRY_TABLE_NAME, ['is_default'], unique=False)
    op.create_index('ix_node_registry_health_status', NODE_REGISTRY_TABLE_NAME, ['health_status'], unique=False)
    op.create_index(
        'ix_node_registry_last_healthcheck_at',
        NODE_REGISTRY_TABLE_NAME,
        ['last_healthcheck_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_node_registry_last_healthcheck_at', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_health_status', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_is_default', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_is_enabled', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_location_code', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_code', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_table(NODE_REGISTRY_TABLE_NAME)

    op.execute(f"DROP TYPE IF EXISTS {NODE_HEALTH_STATUS_ENUM_NAME};")

    # intentionally no-op for audit_action enum value rollback:
    # PostgreSQL does not safely support dropping a single enum value
    # without rebuilding the whole enum type and rewriting dependent rows.
