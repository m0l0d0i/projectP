"""repair legacy subscription urls into canonical /sub/<token> form

Revision ID: 20260410_000017
Revises: 20260409_000016
Create Date: 2026-04-10 21:10:00.000000
"""

from __future__ import annotations

from urllib.parse import urlparse

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260410_000017'
down_revision = '20260409_000016'
branch_labels = None
depends_on = None


SUBSCRIPTIONS_TABLE = 'subscriptions'
SUBSCRIPTION_URL_COLUMN = 'subscription_url'
_CANONICAL_PREFIX = 'sub'
_LEGACY_PREFIXES = {'subscription', 'profile'}


def _normalized_str(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _path_parts(path: str | None) -> list[str]:
    return [part for part in (path or '').split('/') if part]


def _extract_repair_token(value: str | None) -> str | None:
    """
    Repair helper used only inside this migration.

    Accepted input forms:
    - /sub/<token>
    - https://host/sub/<token>
    - /subscription/<token>
    - https://host/subscription/<token>
    - /profile/<token>
    - https://host/profile/<token>

    Bare tokens are intentionally rejected here to avoid accidental data corruption.
    """
    normalized = _normalized_str(value)
    if normalized is None:
        return None

    parsed = urlparse(normalized)
    candidate_path = parsed.path if (parsed.scheme or parsed.netloc) else normalized
    parts = _path_parts(candidate_path)
    if len(parts) < 2:
        return None

    prefix = parts[0].strip().lower()
    token = parts[1].strip()
    if not token or '/' in token:
        return None

    if prefix == _CANONICAL_PREFIX:
        return token
    if prefix in _LEGACY_PREFIXES:
        return token
    return None


def _canonical_subscription_path(token: str | None) -> str | None:
    normalized = _normalized_str(token)
    if normalized is None:
        return None
    cleaned = normalized.strip('/')
    if not cleaned or '/' in cleaned:
        return None
    return f'/{_CANONICAL_PREFIX}/{cleaned}'


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"""
            SELECT id, {SUBSCRIPTION_URL_COLUMN}
            FROM {SUBSCRIPTIONS_TABLE}
            WHERE {SUBSCRIPTION_URL_COLUMN} IS NOT NULL
            ORDER BY id ASC
            """
        )
    ).mappings().all()

    invalid_rows: list[tuple[int, str]] = []
    updates: list[tuple[int, str | None]] = []

    for row in rows:
        subscription_id = int(row['id'])
        raw_value = row[SUBSCRIPTION_URL_COLUMN]
        normalized_value = _normalized_str(raw_value)

        if normalized_value is None:
            updates.append((subscription_id, None))
            continue

        token = _extract_repair_token(normalized_value)
        if token is None:
            invalid_rows.append((subscription_id, normalized_value))
            continue

        canonical_path = _canonical_subscription_path(token)
        if canonical_path is None:
            invalid_rows.append((subscription_id, normalized_value))
            continue

        if normalized_value != canonical_path:
            updates.append((subscription_id, canonical_path))

    if invalid_rows:
        preview = ', '.join(f"id={row_id}: {value!r}" for row_id, value in invalid_rows[:5])
        suffix = '' if len(invalid_rows) <= 5 else f' (+{len(invalid_rows) - 5} more)'
        raise RuntimeError(
            'Cannot canonicalize some subscriptions.subscription_url values automatically. '
            f'Examples: {preview}{suffix}'
        )

    for subscription_id, canonical_path in updates:
        bind.execute(
            sa.text(
                f"""
                UPDATE {SUBSCRIPTIONS_TABLE}
                SET {SUBSCRIPTION_URL_COLUMN} = :subscription_url
                WHERE id = :subscription_id
                """
            ),
            {
                'subscription_id': subscription_id,
                'subscription_url': canonical_path,
            },
        )


def downgrade() -> None:
    # Irreversible data repair: canonical /sub/<token> values are intentionally kept.
    return None
