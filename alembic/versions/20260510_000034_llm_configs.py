"""llm_configs + audit_action support_ai (FEA-C32 #1)

Revision ID: 20260510_000034
Revises: 20260510_000033
Create Date: 2026-05-10 00:00:06.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000034'
down_revision = '20260510_000033'
branch_labels = None
depends_on = None


_PROVIDER_ENUM_NAME = 'llm_provider_kind'
_PROVIDER_VALUES = ('deepseek', 'openai_compat')

_NEW_AUDIT_VALUES = (
    'llm_config_created',
    'llm_config_updated',
    'llm_config_deleted',
    'llm_config_test_run',
    'support_ai_generated',
)


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.execute(
            sa.text(
                "DO $$ BEGIN "
                f"IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{_PROVIDER_ENUM_NAME}') THEN "
                f"CREATE TYPE {_PROVIDER_ENUM_NAME} AS ENUM "
                f"({', '.join(repr(v) for v in _PROVIDER_VALUES)}); "
                "END IF; END $$;"
            )
        )
        for value in _NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )

    provider_type = sa.Enum(
        *_PROVIDER_VALUES, name=_PROVIDER_ENUM_NAME, create_type=False
    )

    op.create_table(
        'llm_configs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('title', sa.String(length=64), nullable=False),
        sa.Column('provider', provider_type, nullable=False),
        sa.Column('api_base_url', sa.String(length=512), nullable=False),
        sa.Column('model_name', sa.String(length=128), nullable=False),
        sa.Column('system_prompt', sa.Text(), nullable=False),
        sa.Column(
            'temperature',
            sa.Numeric(3, 2),
            nullable=False,
            server_default=sa.text('0.30'),
        ),
        sa.Column(
            'max_tokens',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('1024'),
        ),
        sa.Column('api_key_encrypted', sa.Text(), nullable=False),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column(
            'usage_total_calls',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column(
            'usage_total_input_tokens',
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column(
            'usage_total_output_tokens',
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column(
            'last_test_status',
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'never'"),
        ),
        sa.Column('last_test_error', sa.Text(), nullable=True),
        sa.Column('last_test_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
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
            "char_length(trim(title)) > 0",
            name='ck_llm_configs_title_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(trim(api_base_url)) > 0",
            name='ck_llm_configs_api_base_url_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(trim(model_name)) > 0",
            name='ck_llm_configs_model_name_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(trim(system_prompt)) > 0",
            name='ck_llm_configs_system_prompt_not_blank',
        ),
        sa.CheckConstraint(
            'temperature >= 0 AND temperature <= 2',
            name='ck_llm_configs_temperature_range',
        ),
        sa.CheckConstraint(
            'max_tokens > 0',
            name='ck_llm_configs_max_tokens_positive',
        ),
        sa.CheckConstraint(
            'usage_total_calls >= 0',
            name='ck_llm_configs_usage_total_calls_non_negative',
        ),
        sa.CheckConstraint(
            'usage_total_input_tokens >= 0',
            name='ck_llm_configs_usage_total_input_non_negative',
        ),
        sa.CheckConstraint(
            'usage_total_output_tokens >= 0',
            name='ck_llm_configs_usage_total_output_non_negative',
        ),
        sa.ForeignKeyConstraint(
            ['created_by_admin_id'],
            ['web_admin_users.id'],
            ondelete='SET NULL',
            name='fk_llm_configs_created_by_admin_id',
        ),
    )
    # Не больше одного активного конфига одновременно — упрощает резолв
    # «текущего LLM» в support-AI пайплайне (нет неоднозначности).
    if is_pg:
        op.execute(
            sa.text(
                'CREATE UNIQUE INDEX uq_llm_configs_single_active '
                'ON llm_configs ((1)) WHERE is_active = true'
            )
        )
    else:
        op.create_index(
            'ix_llm_configs_is_active',
            'llm_configs',
            ['is_active'],
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        op.execute(sa.text('DROP INDEX IF EXISTS uq_llm_configs_single_active'))
    else:
        op.drop_index('ix_llm_configs_is_active', table_name='llm_configs')
    op.drop_table('llm_configs')

    if is_pg:
        op.execute(sa.text(f'DROP TYPE IF EXISTS {_PROVIDER_ENUM_NAME}'))
    # PG не поддерживает удаление enum value — оставляем audit_action как есть.
