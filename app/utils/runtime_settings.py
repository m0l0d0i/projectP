from __future__ import annotations

from typing import Any


def runtime_row_present(row: Any) -> bool:
    return row is not None


def effective_list(db_value: Any, fallback: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[Any]:
    """Return AppSettings value if it is explicitly set, even when empty.

    AppSettings JSON-list fields are non-null and may legitimately be an empty list.
    Using `db_value or fallback` would incorrectly ignore an explicit empty list and
    silently keep env-based privileges/recipients enabled.
    """
    if db_value is None:
        return list(fallback or [])
    if isinstance(db_value, list):
        return list(db_value)
    if isinstance(db_value, (tuple, set)):
        return list(db_value)
    return [db_value]


def coerce_int_set(values: Any) -> set[int]:
    normalized: set[int] = set()
    for value in effective_list(values, []):
        try:
            normalized.add(int(value))
        except (TypeError, ValueError):
            continue
    return normalized


def effective_list_from_row(row: Any, attr_name: str, fallback: list[Any] | tuple[Any, ...] | set[Any] | None) -> list[Any]:
    """Use env/bootstrap fallback only when AppSettings row does not exist yet."""
    if not runtime_row_present(row):
        return list(fallback or [])
    return effective_list(getattr(row, attr_name, None), [])


def effective_optional_int_from_row(row: Any, attr_name: str, fallback: int | None) -> int | None:
    """Use env/bootstrap fallback only when AppSettings row does not exist yet."""
    if not runtime_row_present(row):
        return int(fallback) if fallback is not None else None

    value = getattr(row, attr_name, None)
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def effective_bool_from_row(row: Any, attr_name: str, fallback: bool) -> bool:
    """Use env/bootstrap fallback only when AppSettings row does not exist yet."""
    if not runtime_row_present(row):
        return bool(fallback)
    return bool(getattr(row, attr_name, False))


def effective_int_from_row(row: Any, attr_name: str, fallback: int, *, minimum: int | None = None) -> int:
    """Use env/bootstrap fallback only when AppSettings row does not exist yet."""
    raw_value = fallback if not runtime_row_present(row) else getattr(row, attr_name, fallback)
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        value = int(fallback)

    if minimum is not None:
        value = max(minimum, value)
    return value
