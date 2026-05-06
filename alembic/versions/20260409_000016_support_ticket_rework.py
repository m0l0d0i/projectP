"""rework support tickets statuses and attachment metadata

Revision ID: 20260409_000016
Revises: 20260405_000015
Create Date: 2026-04-09 20:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260409_000016'
down_revision = '20260405_000015'
branch_labels = None
depends_on = None


SUPPORT_TICKET_STATUS_ENUM_NAME = 'support_ticket_status'
SUPPORT_SENDER_TYPE_ENUM_NAME = 'support_sender_type'
SUPPORT_TICKETS_TABLE = 'support_tickets'
SUPPORT_MESSAGES_TABLE = 'support_messages'
OLD_ACTIVE_TICKET_INDEX = 'uq_support_tickets_one_open_per_user'
NEW_ACTIVE_TICKET_INDEX = 'uq_support_tickets_one_active_per_user'


def upgrade() -> None:
    op.execute(f'DROP INDEX IF EXISTS {OLD_ACTIVE_TICKET_INDEX}')

    context = op.get_context()
    with context.autocommit_block():
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM pg_type t
                    JOIN pg_enum e ON e.enumtypid = t.oid
                    WHERE t.typname = '{SUPPORT_TICKET_STATUS_ENUM_NAME}'
                      AND e.enumlabel = 'open'
                ) THEN
                    ALTER TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}
                    RENAME VALUE 'open' TO 'waiting_operator';
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
                    WHERE t.typname = '{SUPPORT_TICKET_STATUS_ENUM_NAME}'
                ) THEN
                    BEGIN
                        ALTER TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}
                        ADD VALUE IF NOT EXISTS 'waiting_user';
                    EXCEPTION
                        WHEN duplicate_object THEN NULL;
                    END;
                END IF;
            END$$;
            """
        )

    op.alter_column(
        SUPPORT_TICKETS_TABLE,
        'status',
        existing_type=sa.Enum(name=SUPPORT_TICKET_STATUS_ENUM_NAME),
        server_default=sa.text("'waiting_operator'"),
        existing_nullable=False,
    )

    op.add_column(
        SUPPORT_TICKETS_TABLE,
        sa.Column('closed_by_admin_tg_id', sa.BigInteger(), nullable=True),
    )
    op.add_column(
        SUPPORT_TICKETS_TABLE,
        sa.Column(
            'last_actor_type',
            sa.Enum(name=SUPPORT_SENDER_TYPE_ENUM_NAME),
            nullable=True,
        ),
    )
    op.add_column(
        SUPPORT_TICKETS_TABLE,
        sa.Column('last_actor_tg_id', sa.BigInteger(), nullable=True),
    )

    op.alter_column(
        SUPPORT_TICKETS_TABLE,
        'close_reason',
        existing_type=sa.String(length=64),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using='close_reason::text',
    )

    op.add_column(
        SUPPORT_MESSAGES_TABLE,
        sa.Column('media_file_unique_id', sa.Text(), nullable=True),
    )
    op.add_column(
        SUPPORT_MESSAGES_TABLE,
        sa.Column('media_file_name', sa.String(length=255), nullable=True),
    )
    op.add_column(
        SUPPORT_MESSAGES_TABLE,
        sa.Column('media_mime_type', sa.String(length=255), nullable=True),
    )
    op.add_column(
        SUPPORT_MESSAGES_TABLE,
        sa.Column('media_size_bytes', sa.BigInteger(), nullable=True),
    )

    op.create_index(
        'ix_support_tickets_closed_by_admin_tg_id',
        SUPPORT_TICKETS_TABLE,
        ['closed_by_admin_tg_id'],
        unique=False,
    )
    op.create_index(
        'ix_support_tickets_closed_at',
        SUPPORT_TICKETS_TABLE,
        ['closed_at'],
        unique=False,
    )
    op.create_index(
        'ix_support_tickets_last_actor_tg_id',
        SUPPORT_TICKETS_TABLE,
        ['last_actor_tg_id'],
        unique=False,
    )
    op.create_index(
        'ix_support_tickets_user_status_id',
        SUPPORT_TICKETS_TABLE,
        ['user_id', 'status', 'id'],
        unique=False,
    )
    op.create_index(
        'ix_support_tickets_closed_at_id',
        SUPPORT_TICKETS_TABLE,
        ['closed_at', 'id'],
        unique=False,
    )
    op.create_index(
        NEW_ACTIVE_TICKET_INDEX,
        SUPPORT_TICKETS_TABLE,
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.create_index(
        'ix_support_messages_sender_type_created_id',
        SUPPORT_MESSAGES_TABLE,
        ['sender_type', 'created_at', 'id'],
        unique=False,
    )

    op.execute(
        f"""
        WITH last_message AS (
            SELECT DISTINCT ON (sm.ticket_id)
                sm.ticket_id,
                sm.sender_type,
                sm.sender_tg_id
            FROM {SUPPORT_MESSAGES_TABLE} sm
            ORDER BY sm.ticket_id, sm.created_at DESC, sm.id DESC
        )
        UPDATE {SUPPORT_TICKETS_TABLE} st
        SET
            last_actor_type = last_message.sender_type,
            last_actor_tg_id = last_message.sender_tg_id
        FROM last_message
        WHERE st.id = last_message.ticket_id
        """
    )

    with context.autocommit_block():
        op.execute(
            f"""
            UPDATE {SUPPORT_TICKETS_TABLE}
            SET status = 'waiting_user'
            WHERE status = 'waiting_operator'
              AND last_actor_type = 'admin'
            """
        )

    op.execute(
        f"""
        UPDATE {SUPPORT_TICKETS_TABLE}
        SET closed_by_admin_tg_id = last_actor_tg_id
        WHERE status = 'closed'
          AND closed_by_admin_tg_id IS NULL
          AND last_actor_type = 'admin'
        """
    )


def downgrade() -> None:
    op.drop_index('ix_support_messages_sender_type_created_id', table_name=SUPPORT_MESSAGES_TABLE)
    op.drop_index(NEW_ACTIVE_TICKET_INDEX, table_name=SUPPORT_TICKETS_TABLE)
    op.drop_index('ix_support_tickets_closed_at_id', table_name=SUPPORT_TICKETS_TABLE)
    op.drop_index('ix_support_tickets_user_status_id', table_name=SUPPORT_TICKETS_TABLE)
    op.drop_index('ix_support_tickets_last_actor_tg_id', table_name=SUPPORT_TICKETS_TABLE)
    op.drop_index('ix_support_tickets_closed_at', table_name=SUPPORT_TICKETS_TABLE)
    op.drop_index('ix_support_tickets_closed_by_admin_tg_id', table_name=SUPPORT_TICKETS_TABLE)

    op.drop_column(SUPPORT_MESSAGES_TABLE, 'media_size_bytes')
    op.drop_column(SUPPORT_MESSAGES_TABLE, 'media_mime_type')
    op.drop_column(SUPPORT_MESSAGES_TABLE, 'media_file_name')
    op.drop_column(SUPPORT_MESSAGES_TABLE, 'media_file_unique_id')

    op.drop_column(SUPPORT_TICKETS_TABLE, 'last_actor_tg_id')
    op.drop_column(SUPPORT_TICKETS_TABLE, 'last_actor_type')
    op.drop_column(SUPPORT_TICKETS_TABLE, 'closed_by_admin_tg_id')

    op.execute(
        f"""
        UPDATE {SUPPORT_TICKETS_TABLE}
        SET close_reason = left(close_reason, 64)
        WHERE close_reason IS NOT NULL
        """
    )
    op.alter_column(
        SUPPORT_TICKETS_TABLE,
        'close_reason',
        existing_type=sa.Text(),
        type_=sa.String(length=64),
        existing_nullable=True,
        postgresql_using='left(close_reason, 64)',
    )

    op.execute(
        f"""
        ALTER TABLE {SUPPORT_TICKETS_TABLE}
        ALTER COLUMN status DROP DEFAULT
        """
    )

    op.execute(
        f"""
        ALTER TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}
        RENAME TO {SUPPORT_TICKET_STATUS_ENUM_NAME}_old
        """
    )
    op.execute(
        f"""
        CREATE TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}
        AS ENUM ('open', 'closed')
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SUPPORT_TICKETS_TABLE}
        ALTER COLUMN status
        TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}
        USING (
            CASE
                WHEN status::text IN ('waiting_operator', 'waiting_user') THEN 'open'
                ELSE status::text
            END
        )::{SUPPORT_TICKET_STATUS_ENUM_NAME}
        """
    )
    op.execute(
        f"""
        ALTER TABLE {SUPPORT_TICKETS_TABLE}
        ALTER COLUMN status SET DEFAULT 'open'
        """
    )
    op.execute(
        f"""
        DROP TYPE {SUPPORT_TICKET_STATUS_ENUM_NAME}_old
        """
    )

    op.create_index(
        OLD_ACTIVE_TICKET_INDEX,
        SUPPORT_TICKETS_TABLE,
        ['user_id'],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )
