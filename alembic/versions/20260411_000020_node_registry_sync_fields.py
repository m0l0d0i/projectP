"""add node registry sync fields

Revision ID: 20260411_000020
Revises: 20260410_000019
Create Date: 2026-04-11 00:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '20260411_000020'
down_revision = '20260410_000019'
branch_labels = None
depends_on = None


NODE_REGISTRY_TABLE_NAME = 'node_registry'
NODE_SOURCE_STATUS_ENUM_NAME = 'node_source_status'
NODE_SYNC_STATE_ENUM_NAME = 'node_sync_state'


node_source_status_enum = postgresql.ENUM(
    'unknown',
    'active',
    'disabled',
    name=NODE_SOURCE_STATUS_ENUM_NAME,
    create_type=False,
)

node_sync_state_enum = postgresql.ENUM(
    'never_synced',
    'synced',
    'missing',
    'error',
    name=NODE_SYNC_STATE_ENUM_NAME,
    create_type=False,
)


def _create_enum_if_missing(enum_name: str, values: list[str]) -> None:
    rendered_values = ', '.join(f"'{value}'" for value in values)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typname = '{enum_name}'
            ) THEN
                CREATE TYPE {enum_name} AS ENUM ({rendered_values});
            END IF;
        END$$;
        """
    )


def upgrade() -> None:
    _create_enum_if_missing(NODE_SOURCE_STATUS_ENUM_NAME, ['unknown', 'active', 'disabled'])
    _create_enum_if_missing(NODE_SYNC_STATE_ENUM_NAME, ['never_synced', 'synced', 'missing', 'error'])

    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('source_node_id', sa.String(length=128), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column(
            'source_status',
            node_source_status_enum,
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column(
            'sync_state',
            node_sync_state_enum,
            nullable=False,
            server_default=sa.text("'never_synced'"),
        ),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column(
            'source_payload_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('last_sync_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        NODE_REGISTRY_TABLE_NAME,
        sa.Column('sync_error', sa.Text(), nullable=True),
    )

    op.create_unique_constraint(
        'uq_node_registry_source_node_id',
        NODE_REGISTRY_TABLE_NAME,
        ['source_node_id'],
    )
    op.create_check_constraint(
        'ck_node_registry_source_node_id_not_blank',
        NODE_REGISTRY_TABLE_NAME,
        "(source_node_id IS NULL) OR (char_length(trim(source_node_id)) > 0)",
    )

    op.create_index(
        'ix_node_registry_source_status',
        NODE_REGISTRY_TABLE_NAME,
        ['source_status'],
        unique=False,
    )
    op.create_index(
        'ix_node_registry_sync_state',
        NODE_REGISTRY_TABLE_NAME,
        ['sync_state'],
        unique=False,
    )
    op.create_index(
        'ix_node_registry_last_sync_at',
        NODE_REGISTRY_TABLE_NAME,
        ['last_sync_at'],
        unique=False,
    )

    op.execute(
        f"""
        UPDATE {NODE_REGISTRY_TABLE_NAME}
        SET
            source_status = 'unknown',
            sync_state = 'never_synced',
            source_payload_json = COALESCE(source_payload_json, '{{}}'::json),
            sync_error = NULL,
            last_sync_at = NULL
        WHERE source_status IS NULL
           OR sync_state IS NULL
           OR source_payload_json IS NULL;
        """
    )

    op.alter_column(
        NODE_REGISTRY_TABLE_NAME,
        'source_status',
        server_default=None,
    )
    op.alter_column(
        NODE_REGISTRY_TABLE_NAME,
        'sync_state',
        server_default=None,
    )
    op.alter_column(
        NODE_REGISTRY_TABLE_NAME,
        'source_payload_json',
        server_default=None,
    )


def downgrade() -> None:
    op.drop_index('ix_node_registry_last_sync_at', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_sync_state', table_name=NODE_REGISTRY_TABLE_NAME)
    op.drop_index('ix_node_registry_source_status', table_name=NODE_REGISTRY_TABLE_NAME)

    op.drop_constraint('ck_node_registry_source_node_id_not_blank', NODE_REGISTRY_TABLE_NAME, type_='check')
    op.drop_constraint('uq_node_registry_source_node_id', NODE_REGISTRY_TABLE_NAME, type_='unique')

    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'sync_error')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'last_sync_at')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'source_payload_json')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'sync_state')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'source_status')
    op.drop_column(NODE_REGISTRY_TABLE_NAME, 'source_node_id')

    op.execute(f"DROP TYPE IF EXISTS {NODE_SYNC_STATE_ENUM_NAME};")
    op.execute(f"DROP TYPE IF EXISTS {NODE_SOURCE_STATUS_ENUM_NAME};")
