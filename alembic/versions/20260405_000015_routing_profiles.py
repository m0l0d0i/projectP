"""create routing profiles foundation

Revision ID: 20260405_000015
Revises: 20260405_000014
Create Date: 2026-04-05 05:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260405_000015'
down_revision = '20260405_000014'
branch_labels = None
depends_on = None


AUDIT_ACTION_ENUM_NAME = 'audit_action'
ROUTING_PROFILES_TABLE_NAME = 'routing_profiles'


def upgrade() -> None:
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
                    ALTER TYPE {AUDIT_ACTION_ENUM_NAME} ADD VALUE IF NOT EXISTS 'routing_profile_updated';
                EXCEPTION
                    WHEN duplicate_object THEN NULL;
                END;
            END IF;
        END$$;
        """
    )

    op.create_table(
        ROUTING_PROFILES_TABLE_NAME,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
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
            'sort_order',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column(
            'match_tags',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            'config_json',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
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
        sa.CheckConstraint('sort_order >= 0', name='ck_routing_profiles_sort_order_non_negative'),
        sa.CheckConstraint(
            "(description IS NULL) OR (char_length(trim(description)) > 0)",
            name='ck_routing_profiles_description_not_blank',
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('code', name='uq_routing_profiles_code'),
    )

    op.create_index('ix_routing_profiles_code', ROUTING_PROFILES_TABLE_NAME, ['code'], unique=False)
    op.create_index('ix_routing_profiles_is_enabled', ROUTING_PROFILES_TABLE_NAME, ['is_enabled'], unique=False)
    op.create_index('ix_routing_profiles_is_default', ROUTING_PROFILES_TABLE_NAME, ['is_default'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_routing_profiles_is_default', table_name=ROUTING_PROFILES_TABLE_NAME)
    op.drop_index('ix_routing_profiles_is_enabled', table_name=ROUTING_PROFILES_TABLE_NAME)
    op.drop_index('ix_routing_profiles_code', table_name=ROUTING_PROFILES_TABLE_NAME)
    op.drop_table(ROUTING_PROFILES_TABLE_NAME)

    # intentionally no-op for audit_action enum value rollback:
    # PostgreSQL does not safely support dropping a single enum value
    # without rebuilding the whole enum type and rewriting dependent rows.
