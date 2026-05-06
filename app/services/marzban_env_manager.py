from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from app.config import Settings


_ENV_KEY_RE = re.compile(r'^[A-Z][A-Z0-9_]*$')
_ENV_ASSIGNMENT_RE = re.compile(
    r'^(?P<indent>\s*)(?P<export>export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*?)(?P<newline>\r?\n?)$'
)
_BOOL_RE = re.compile(r'^(?:1|0|true|false|yes|no|on|off)$', re.IGNORECASE)
_PATH_FRAGMENT_RE = re.compile(r'^/[A-Za-z0-9._~\-/%]*$')


class MarzbanEnvManagerError(Exception):
    """Base error for managed Marzban/Xray env operations."""


class MarzbanEnvNotAllowedError(MarzbanEnvManagerError):
    """Raised when a key is not in the managed allowlist."""


class MarzbanEnvReadonlyError(MarzbanEnvManagerError):
    """Raised when a key is allowlisted but readonly."""


class MarzbanEnvValidationError(MarzbanEnvManagerError):
    """Raised when an env key or value is invalid."""


@dataclass(slots=True)
class ManagedEnvItem:
    key: str
    value: str | None
    present: bool
    readonly: bool


@dataclass(slots=True)
class ManagedEnvDiffItem:
    key: str
    old_value: str | None
    new_value: str | None
    changed: bool
    readonly: bool
    present_before: bool
    present_after: bool


@dataclass(slots=True)
class ManagedEnvPreview:
    path: Path
    values_before: dict[str, str | None]
    values_after: dict[str, str | None]
    changed_items: list[ManagedEnvDiffItem]

    @property
    def changed_keys(self) -> list[str]:
        return [item.key for item in self.changed_items if item.changed]

    @property
    def has_changes(self) -> bool:
        return any(item.changed for item in self.changed_items)


@dataclass(slots=True)
class ManagedEnvApplyResult:
    path: Path
    preview: ManagedEnvPreview
    backup_path: Path | None

    @property
    def changed_keys(self) -> list[str]:
        return self.preview.changed_keys

    @property
    def has_changes(self) -> bool:
        return self.preview.has_changes


@dataclass(slots=True)
class TemplatePathState:
    path: Path
    exists: bool
    writable_target: Path
    writable: bool


@dataclass(slots=True)
class _EnvLine:
    raw: str
    key: str | None = None
    value: str | None = None
    indent: str = ''
    export_prefix: str = ''
    newline: str = '\n'

    @property
    def is_assignment(self) -> bool:
        return self.key is not None


class MarzbanEnvManager:
    """
    Safe manager for allowlisted Marzban/Xray environment variables.

    Design goals:
    - no raw arbitrary .env editing from admin UI
    - only explicitly allowlisted keys can be read/updated
    - readonly keys are exposed but cannot be changed
    - writes are atomic (temp file + os.replace)
    - comments / unknown lines / ordering are preserved
    - apply flow can create backup and diff preview for admin UI
    """

    _VALUE_VALIDATORS: dict[str, Callable[[str | None], str | None]]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

        raw_env_file_path = str(getattr(settings, 'marzban_env_file_path', '') or '').strip()
        if not raw_env_file_path:
            raise MarzbanEnvValidationError('marzban_env_file_path не может быть пустым.')

        self.env_file_path = Path(raw_env_file_path).expanduser()

        allowlist = getattr(settings, 'marzban_managed_env_allowlist', [])
        readonly_keys = getattr(settings, 'marzban_managed_env_readonly_keys', [])

        normalized_allowlist: list[str] = []
        seen_allowlist: set[str] = set()
        for raw_key in allowlist:
            normalized_key = self._normalize_key(raw_key)
            if normalized_key in seen_allowlist:
                continue
            seen_allowlist.add(normalized_key)
            normalized_allowlist.append(normalized_key)

        self.allowlist = tuple(normalized_allowlist)
        self.readonly_keys = frozenset(self._normalize_key(key) for key in readonly_keys)

        self._VALUE_VALIDATORS = {
            'XRAY_SUBSCRIPTION_URL_PREFIX': self._validate_url_prefix,
            'XRAY_FALLBACK_DNS': self._validate_non_empty_text,
            'XRAY_GEOIP_PATH': self._validate_abs_path,
            'XRAY_GEOSITE_PATH': self._validate_abs_path,
            'UVICORN_FORWARDED_ALLOW_IPS': self._validate_non_empty_text,
            'DOCS': self._validate_boolish,
            'REDOC': self._validate_boolish,
        }

    def list_items(self) -> list[ManagedEnvItem]:
        values = self._read_current_values()
        return [
            ManagedEnvItem(
                key=key,
                value=values.get(key),
                present=key in values,
                readonly=key in self.readonly_keys,
            )
            for key in self.allowlist
        ]

    def path_state(self) -> TemplatePathState:
        env_file_path = self.env_file_path
        env_exists = env_file_path.exists()
        writable_target = env_file_path if env_exists else env_file_path.parent
        writable = os.access(writable_target, os.W_OK)
        return TemplatePathState(
            path=env_file_path,
            exists=env_exists,
            writable_target=writable_target,
            writable=writable,
        )

    def get_values(self) -> dict[str, str | None]:
        values = self._read_current_values()
        return {key: values.get(key) for key in self.allowlist}

    def get_item(self, key: str) -> ManagedEnvItem:
        normalized_key = self._require_allowed_key(key)
        values = self._read_current_values()
        return ManagedEnvItem(
            key=normalized_key,
            value=values.get(normalized_key),
            present=normalized_key in values,
            readonly=normalized_key in self.readonly_keys,
        )

    def preview_updates(self, updates: Mapping[str, str | None]) -> ManagedEnvPreview:
        normalized_updates = self.validate_updates(updates)
        current = self.get_values()
        values_after = dict(current)
        changed_items: list[ManagedEnvDiffItem] = []

        for key in self.allowlist:
            before = current.get(key)
            after = before
            if key in normalized_updates:
                after = normalized_updates[key]
            values_after[key] = after
            changed_items.append(
                ManagedEnvDiffItem(
                    key=key,
                    old_value=before,
                    new_value=after,
                    changed=before != after,
                    readonly=key in self.readonly_keys,
                    present_before=key in current and before is not None,
                    present_after=after is not None,
                )
            )

        return ManagedEnvPreview(
            path=self.env_file_path,
            values_before=current,
            values_after=values_after,
            changed_items=changed_items,
        )

    def update_values(self, updates: Mapping[str, str | None]) -> dict[str, str | None]:
        result = self.apply_updates(updates)
        return result.preview.values_after

    def apply_updates(
        self,
        updates: Mapping[str, str | None],
        *,
        create_backup: bool = True,
        backup_suffix: str | None = None,
    ) -> ManagedEnvApplyResult:
        preview = self.preview_updates(updates)
        normalized_updates = self.validate_updates(updates)

        lines = self._read_lines()
        seen_keys: set[str] = set()
        changed = False

        for line in lines:
            if not line.is_assignment or line.key is None:
                continue
            if line.key not in normalized_updates:
                continue

            new_value = normalized_updates[line.key]
            if line.value != new_value:
                changed = True
            line.value = new_value
            seen_keys.add(line.key)

        newline = self._detect_newline(lines)

        for key, value in normalized_updates.items():
            if key in seen_keys:
                continue
            changed = True
            lines.append(
                _EnvLine(
                    raw='',
                    key=key,
                    value=value,
                    indent='',
                    export_prefix='',
                    newline=newline,
                )
            )

        backup_path: Path | None = None
        if changed:
            if create_backup:
                backup_path = self._write_backup(backup_suffix=backup_suffix)
            self._atomic_write(lines)

        return ManagedEnvApplyResult(path=self.env_file_path, preview=preview, backup_path=backup_path)

    def remove_value(self, key: str, *, create_backup: bool = True) -> ManagedEnvApplyResult:
        normalized_key = self._require_writable_key(key)
        current = self.get_values()
        if current.get(normalized_key) is None and normalized_key not in current:
            preview = self.preview_updates({normalized_key: None})
            return ManagedEnvApplyResult(path=self.env_file_path, preview=preview, backup_path=None)

        return self.apply_updates({normalized_key: None}, create_backup=create_backup)

    def validate_updates(self, updates: Mapping[str, str | None]) -> dict[str, str | None]:
        normalized: dict[str, str | None] = {}
        for raw_key, raw_value in updates.items():
            key = self._require_writable_key(raw_key)
            normalized[key] = self._normalize_value_for_key(key, raw_value)
        return normalized

    def _read_current_values(self) -> dict[str, str | None]:
        values: dict[str, str | None] = {}
        for line in self._read_lines():
            if not line.is_assignment or line.key is None:
                continue
            if line.key not in self.allowlist:
                continue
            values[line.key] = line.value
        return values

    def _read_lines(self) -> list[_EnvLine]:
        if not self.env_file_path.exists():
            return []

        self._ensure_target_path_is_file()

        with self.env_file_path.open('r', encoding='utf-8', newline='') as file_obj:
            text = file_obj.read()

        raw_lines = text.splitlines(keepends=True)
        if not raw_lines and text == '':
            return []

        parsed_lines: list[_EnvLine] = []
        for raw_line in raw_lines:
            parsed_lines.append(self._parse_line(raw_line))

        if text and not text.endswith(('\n', '\r')):
            last = parsed_lines[-1]
            if last.is_assignment:
                last.newline = ''

        return parsed_lines

    def _parse_line(self, raw_line: str) -> _EnvLine:
        match = _ENV_ASSIGNMENT_RE.match(raw_line)
        if not match:
            return _EnvLine(raw=raw_line)

        raw_key = match.group('key')
        value_part = match.group('value')
        return _EnvLine(
            raw=raw_line,
            key=raw_key,
            value=self._parse_env_value(value_part),
            indent=match.group('indent') or '',
            export_prefix=match.group('export') or '',
            newline=match.group('newline') or '',
        )

    def _serialize_line(self, line: _EnvLine) -> str:
        if not line.is_assignment or line.key is None:
            return line.raw

        serialized_value = self._serialize_env_value(line.value)
        return f'{line.indent}{line.export_prefix}{line.key}={serialized_value}{line.newline}'

    def _atomic_write(self, lines: list[_EnvLine]) -> None:
        target_path = self.env_file_path
        parent_dir = self._ensure_parent_ready()
        self._ensure_target_path_is_file(allow_missing=True)

        rendered = ''.join(self._serialize_line(line) for line in lines)
        existing_mode = self._existing_file_mode(target_path)

        tmp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                delete=False,
                dir=str(parent_dir),
                newline='',
            ) as tmp_file:
                tmp_file.write(rendered)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_name = tmp_file.name

            if existing_mode is not None:
                os.chmod(tmp_name, existing_mode)
            else:
                os.chmod(tmp_name, 0o600)

            os.replace(tmp_name, target_path)
            self._fsync_directory(parent_dir)
        except Exception as exc:
            if tmp_name:
                with suppress_file_not_found():
                    os.unlink(tmp_name)
            raise MarzbanEnvManagerError(
                f'Не удалось атомарно обновить env-файл {target_path}: {exc}'
            ) from exc

    def _write_backup(self, *, backup_suffix: str | None = None) -> Path | None:
        target_path = self.env_file_path
        if not target_path.exists():
            return None

        self._ensure_target_path_is_file()
        parent_dir = self._ensure_parent_ready()

        timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        suffix = f'.{backup_suffix}' if backup_suffix else ''
        backup_path = target_path.with_name(f'{target_path.name}.bak.{timestamp}{suffix}')

        tmp_name: str | None = None
        try:
            with target_path.open('r', encoding='utf-8', newline='') as src:
                content = src.read()

            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                delete=False,
                dir=str(parent_dir),
                newline='',
            ) as tmp_file:
                tmp_file.write(content)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
                tmp_name = tmp_file.name

            existing_mode = self._existing_file_mode(target_path)
            os.chmod(tmp_name, existing_mode if existing_mode is not None else 0o600)
            os.replace(tmp_name, backup_path)
            self._fsync_directory(parent_dir)
            return backup_path
        except Exception as exc:
            if tmp_name:
                with suppress_file_not_found():
                    os.unlink(tmp_name)
            raise MarzbanEnvManagerError(
                f'Не удалось создать backup env-файла {target_path}: {exc}'
            ) from exc

    @staticmethod
    def _detect_newline(lines: list[_EnvLine]) -> str:
        for line in lines:
            if line.newline:
                return line.newline
        return '\n'

    @staticmethod
    def _parse_env_value(raw_value: str) -> str | None:
        value = raw_value.strip()
        if value == '':
            return None

        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            inner = value[1:-1]
            return (
                inner.replace(r'\\', '\\')
                .replace(r'\"', '"')
                .replace(r'\n', '\n')
                .replace(r'\r', '\r')
                .replace(r'\t', '\t')
            )

        if value.startswith("'") and value.endswith("'") and len(value) >= 2:
            return value[1:-1]

        if ' #' in value:
            value = value.split(' #', 1)[0].rstrip()

        return value

    @staticmethod
    def _serialize_env_value(value: str | None) -> str:
        if value is None:
            return ''

        if value == '':
            return '""'

        needs_quotes = any(ch.isspace() for ch in value) or any(ch in value for ch in '#"\'\\')
        if '\n' in value or '\r' in value or needs_quotes:
            escaped = (
                value.replace('\\', r'\\')
                .replace('"', r'\"')
                .replace('\n', r'\n')
                .replace('\r', r'\r')
                .replace('\t', r'\t')
            )
            return f'"{escaped}"'

        return value

    def _require_allowed_key(self, key: str) -> str:
        normalized_key = self._normalize_key(key)
        if normalized_key not in self.allowlist:
            raise MarzbanEnvNotAllowedError(
                f'ENV key "{normalized_key}" не входит в allowlist управляемых ключей.'
            )
        return normalized_key

    def _require_writable_key(self, key: str) -> str:
        normalized_key = self._require_allowed_key(key)
        if normalized_key in self.readonly_keys:
            raise MarzbanEnvReadonlyError(
                f'ENV key "{normalized_key}" доступен только для чтения.'
            )
        return normalized_key

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized = str(key or '').strip().upper()
        if not normalized:
            raise MarzbanEnvValidationError('ENV key не может быть пустым.')
        if not _ENV_KEY_RE.match(normalized):
            raise MarzbanEnvValidationError(
                f'ENV key "{normalized}" имеет некорректный формат. Разрешены только A-Z, 0-9 и _.'
            )
        return normalized

    def _normalize_value_for_key(self, key: str, value: str | None) -> str | None:
        normalized = self._normalize_value(value)
        validator = self._VALUE_VALIDATORS.get(key)
        return validator(normalized) if validator is not None else normalized

    @staticmethod
    def _normalize_value(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            value = str(value)

        normalized = value.strip()
        if normalized == '':
            return None
        if '\x00' in normalized:
            raise MarzbanEnvValidationError('ENV value не может содержать NUL-символ.')
        return normalized

    @staticmethod
    def _validate_non_empty_text(value: str | None) -> str | None:
        if value is None:
            return None
        if not value:
            raise MarzbanEnvValidationError('ENV value не может быть пустой строкой.')
        return value

    @staticmethod
    def _validate_boolish(value: str | None) -> str | None:
        if value is None:
            return None
        if not _BOOL_RE.match(value):
            raise MarzbanEnvValidationError(
                'Разрешены только булевы значения: true/false, yes/no, on/off, 1/0.'
            )
        lowered = value.strip().lower()
        return 'true' if lowered in {'1', 'true', 'yes', 'on'} else 'false'

    @staticmethod
    def _validate_abs_path(value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute():
            raise MarzbanEnvValidationError('Значение должно быть абсолютным путём.')
        return str(path)

    @staticmethod
    def _validate_url_prefix(value: str | None) -> str | None:
        if value is None:
            return None
        if not _PATH_FRAGMENT_RE.match(value):
            raise MarzbanEnvValidationError(
                'XRAY_SUBSCRIPTION_URL_PREFIX должен быть относительным HTTP path, например /sub или /sub/.'
            )
        if '//' in value:
            raise MarzbanEnvValidationError('XRAY_SUBSCRIPTION_URL_PREFIX не должен содержать // внутри пути.')
        return value.rstrip('/') or '/'

    def _ensure_parent_ready(self) -> Path:
        parent_dir = self.env_file_path.parent
        if not str(parent_dir).strip():
            raise MarzbanEnvValidationError('Parent directory для env-файла не определён.')

        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise MarzbanEnvManagerError(
                f'Не удалось создать parent-каталог для env-файла: {parent_dir} ({exc})'
            ) from exc

        if not parent_dir.exists() or not parent_dir.is_dir():
            raise MarzbanEnvManagerError(
                f'Parent path для env-файла не является директорией: {parent_dir}'
            )

        if not os.access(parent_dir, os.R_OK | os.W_OK | os.X_OK):
            raise MarzbanEnvManagerError(
                f'Нет прав на чтение/запись/доступ к каталогу env-файла: {parent_dir}'
            )

        return parent_dir

    def _ensure_target_path_is_file(self, *, allow_missing: bool = False) -> None:
        if self.env_file_path.exists():
            if not self.env_file_path.is_file():
                raise MarzbanEnvManagerError(
                    f'Путь env-файла указывает не на файл: {self.env_file_path}'
                )
            return

        if allow_missing:
            return

    @staticmethod
    def _existing_file_mode(path: Path) -> int | None:
        if not path.exists() or not path.is_file():
            return None
        return path.stat().st_mode & 0o777

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            dir_fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return

        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _setting(self, name: str, default):
        return getattr(self.settings, name, default)


class suppress_file_not_found:
    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return exc_type is FileNotFoundError
