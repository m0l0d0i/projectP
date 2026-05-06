from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

from app.config import Settings

logger = logging.getLogger(__name__)

_DEFAULT_GEOIP_URL = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat'
_DEFAULT_GEOSITE_URL = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat'
_SHA256_RE = re.compile(r'(?P<sha>[A-Fa-f0-9]{64})')


class GeodataUpdateError(Exception):
    """Base exception for geodata update failures."""


@dataclass(slots=True)
class GeodataFileStatus:
    name: str
    path: str
    exists: bool
    size_bytes: int | None
    sha256: str | None
    updated_at: datetime | None
    source_url: str
    sha256_url: str | None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        return payload


@dataclass(slots=True)
class GeodataUpdateResult:
    name: str
    path: str
    source_url: str
    sha256_url: str | None
    changed: bool
    downloaded: bool
    skipped_reason: str | None
    local_sha256: str | None
    remote_sha256: str | None
    size_bytes: int | None
    backup_path: str | None
    updated_at: datetime | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload['ok'] = self.ok
        payload['updated_at'] = self.updated_at.isoformat() if self.updated_at else None
        return payload


@dataclass(slots=True)
class GeodataUpdateSummary:
    geoip: GeodataUpdateResult
    geosite: GeodataUpdateResult
    started_at: datetime
    finished_at: datetime

    @property
    def ok(self) -> bool:
        return self.geoip.ok and self.geosite.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            'ok': self.ok,
            'started_at': self.started_at.isoformat(),
            'finished_at': self.finished_at.isoformat(),
            'geoip': self.geoip.to_dict(),
            'geosite': self.geosite.to_dict(),
        }


class GeodataUpdater:
    """
    Safe updater for Xray/Marzban geodata assets.

    Reads canonical bootstrap settings from Settings:
    - geodata_update_enabled
    - geodata_request_timeout_seconds
    - geodata_retained_backups
    - geodata_assets_dir
    - geodata_geoip_filename
    - geodata_geosite_filename
    - geodata_geoip_url
    - geodata_geosite_url
    - geodata_geoip_sha256_url
    - geodata_geosite_sha256_url

    Backward-compatible aliases are still accepted during staged rollout:
    - geodata_enabled
    - geodata_backup_keep_count
    - geodata_geoip_path
    - geodata_geosite_path
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.settings = settings
        self._session = session
        self._owns_session = session is None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(
            self._setting('geodata_update_enabled', None)
            if getattr(self.settings, 'geodata_update_enabled', None) is not None
            else self._setting('geodata_enabled', True)
        )

    @property
    def timeout_seconds(self) -> float:
        return max(1.0, float(self._setting('geodata_request_timeout_seconds', 30.0)))

    @property
    def backup_keep_count(self) -> int:
        retained = self._setting('geodata_retained_backups', None)
        if retained is None:
            retained = self._setting('geodata_backup_keep_count', 3)
        return max(1, int(retained))

    @property
    def geoip_url(self) -> str:
        return self._validated_url(
            self._setting('geodata_geoip_url', _DEFAULT_GEOIP_URL),
            field_name='geodata_geoip_url',
        )

    @property
    def geosite_url(self) -> str:
        return self._validated_url(
            self._setting('geodata_geosite_url', _DEFAULT_GEOSITE_URL),
            field_name='geodata_geosite_url',
        )

    @property
    def geoip_sha256_url(self) -> str | None:
        raw = self._setting('geodata_geoip_sha256_url', f'{self.geoip_url}.sha256sum')
        return self._validated_optional_url(raw, field_name='geodata_geoip_sha256_url')

    @property
    def geosite_sha256_url(self) -> str | None:
        raw = self._setting('geodata_geosite_sha256_url', f'{self.geosite_url}.sha256sum')
        return self._validated_optional_url(raw, field_name='geodata_geosite_sha256_url')

    @property
    def geoip_path(self) -> Path:
        legacy = getattr(self.settings, 'geodata_geoip_path', None)
        if legacy:
            return Path(str(legacy)).expanduser()

        assets_dir = Path(str(self._setting('geodata_assets_dir', '/var/lib/marzban/assets'))).expanduser()
        filename = str(self._setting('geodata_geoip_filename', 'geoip.dat')).strip() or 'geoip.dat'
        return assets_dir / filename

    @property
    def geosite_path(self) -> Path:
        legacy = getattr(self.settings, 'geodata_geosite_path', None)
        if legacy:
            return Path(str(legacy)).expanduser()

        assets_dir = Path(str(self._setting('geodata_assets_dir', '/var/lib/marzban/assets'))).expanduser()
        filename = str(self._setting('geodata_geosite_filename', 'geosite.dat')).strip() or 'geosite.dat'
        return assets_dir / filename

    async def close(self) -> None:
        if self._session is not None and self._owns_session:
            await self._session.close()
        self._session = None

    async def get_status(self) -> dict[str, GeodataFileStatus]:
        def _build() -> dict[str, GeodataFileStatus]:
            return {
                'geoip': self._build_status(
                    name='geoip',
                    path=self.geoip_path,
                    source_url=self.geoip_url,
                    sha256_url=self.geoip_sha256_url,
                ),
                'geosite': self._build_status(
                    name='geosite',
                    path=self.geosite_path,
                    source_url=self.geosite_url,
                    sha256_url=self.geosite_sha256_url,
                ),
            }
        return await asyncio.to_thread(_build)

    async def update_all(self, *, force: bool = False) -> GeodataUpdateSummary:
        started_at = datetime.now(timezone.utc)
        async with self._lock:
            geoip_result = await self._update_one(
                name='geoip',
                path=self.geoip_path,
                source_url=self.geoip_url,
                sha256_url=self.geoip_sha256_url,
                force=force,
            )
            geosite_result = await self._update_one(
                name='geosite',
                path=self.geosite_path,
                source_url=self.geosite_url,
                sha256_url=self.geosite_sha256_url,
                force=force,
            )
        finished_at = datetime.now(timezone.utc)
        return GeodataUpdateSummary(
            geoip=geoip_result,
            geosite=geosite_result,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def update_geoip(self, *, force: bool = False) -> GeodataUpdateResult:
        async with self._lock:
            return await self._update_one(
                name='geoip',
                path=self.geoip_path,
                source_url=self.geoip_url,
                sha256_url=self.geoip_sha256_url,
                force=force,
            )

    async def update_geosite(self, *, force: bool = False) -> GeodataUpdateResult:
        async with self._lock:
            return await self._update_one(
                name='geosite',
                path=self.geosite_path,
                source_url=self.geosite_url,
                sha256_url=self.geosite_sha256_url,
                force=force,
            )

    def _build_status(
        self,
        *,
        name: str,
        path: Path,
        source_url: str,
        sha256_url: str | None,
    ) -> GeodataFileStatus:
        exists = path.exists()
        stat = path.stat() if exists else None
        updated_at = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc) if stat else None
        return GeodataFileStatus(
            name=name,
            path=str(path),
            exists=exists,
            size_bytes=stat.st_size if stat else None,
            sha256=self._hash_file(path) if exists else None,
            updated_at=updated_at,
            source_url=source_url,
            sha256_url=sha256_url,
        )

    async def _update_one(
        self,
        *,
        name: str,
        path: Path,
        source_url: str,
        sha256_url: str | None,
        force: bool,
    ) -> GeodataUpdateResult:
        remote_sha256: str | None = None

        # All filesystem inspections are sync; bundle them once per call to
        # avoid repeated thread-pool hops while keeping the event loop free.
        def _stat_snapshot() -> tuple[bool, str | None, int | None, datetime | None]:
            exists = path.exists() and path.is_file()
            if not exists:
                return False, None, None, None
            stat_result = path.stat()
            return (
                True,
                self._hash_file(path),
                stat_result.st_size,
                datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
            )

        if not self.enabled:
            exists, local_sha, size_bytes, updated_at = await asyncio.to_thread(_stat_snapshot)
            return GeodataUpdateResult(
                name=name,
                path=str(path),
                source_url=source_url,
                sha256_url=sha256_url,
                changed=False,
                downloaded=False,
                skipped_reason='geodata updater disabled',
                local_sha256=local_sha,
                remote_sha256=None,
                size_bytes=size_bytes,
                backup_path=None,
                updated_at=updated_at,
                error=None,
            )

        try:
            await asyncio.to_thread(self._ensure_path_access, path)

            session = await self._get_session()
            remote_sha256 = await self._fetch_remote_sha256(session, sha256_url)
            exists, local_sha256, size_bytes, updated_at = await asyncio.to_thread(_stat_snapshot)

            if not force and remote_sha256 and local_sha256 == remote_sha256:
                logger.info('Geodata asset %s is already up to date at %s', name, path)
                return GeodataUpdateResult(
                    name=name,
                    path=str(path),
                    source_url=source_url,
                    sha256_url=sha256_url,
                    changed=False,
                    downloaded=False,
                    skipped_reason='already up to date',
                    local_sha256=local_sha256,
                    remote_sha256=remote_sha256,
                    size_bytes=size_bytes,
                    backup_path=None,
                    updated_at=updated_at,
                    error=None,
                )

            content = await self._download_bytes(session, source_url)
            downloaded_sha256 = hashlib.sha256(content).hexdigest()
            if remote_sha256 and downloaded_sha256 != remote_sha256:
                raise GeodataUpdateError(
                    f'{name}: downloaded sha256 mismatch (expected {remote_sha256}, got {downloaded_sha256})'
                )

            def _persist() -> tuple[Path | None, int, datetime]:
                backup = self._backup_file(path) if path.exists() else None
                self._atomic_write(path, content)
                self._prune_backups(path)
                stat_result = path.stat()
                return (
                    backup,
                    stat_result.st_size,
                    datetime.fromtimestamp(stat_result.st_mtime, tz=timezone.utc),
                )

            backup_path, new_size, new_updated_at = await asyncio.to_thread(_persist)
            logger.info(
                'Geodata asset %s updated successfully: path=%s size=%s sha256=%s',
                name,
                path,
                new_size,
                downloaded_sha256,
            )
            return GeodataUpdateResult(
                name=name,
                path=str(path),
                source_url=source_url,
                sha256_url=sha256_url,
                changed=(downloaded_sha256 != local_sha256),
                downloaded=True,
                skipped_reason=None,
                local_sha256=downloaded_sha256,
                remote_sha256=remote_sha256,
                size_bytes=new_size,
                backup_path=str(backup_path) if backup_path else None,
                updated_at=new_updated_at,
                error=None,
            )
        except Exception as exc:
            logger.exception('Failed to update geodata asset %s', name)
            try:
                exists, local_sha, size_bytes, updated_at = await asyncio.to_thread(_stat_snapshot)
            except Exception:
                exists, local_sha, size_bytes, updated_at = False, None, None, None
            return GeodataUpdateResult(
                name=name,
                path=str(path),
                source_url=source_url,
                sha256_url=sha256_url,
                changed=False,
                downloaded=False,
                skipped_reason=None,
                local_sha256=local_sha,
                remote_sha256=remote_sha256,
                size_bytes=size_bytes,
                backup_path=None,
                updated_at=updated_at,
                error=str(exc),
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
            self._owns_session = True
        return self._session

    async def _fetch_remote_sha256(self, session: aiohttp.ClientSession, sha256_url: str | None) -> str | None:
        if not sha256_url:
            return None

        try:
            content = await self._download_text(session, sha256_url)
        except Exception as exc:
            logger.warning('Failed to fetch geodata sha256 from %s: %s', sha256_url, exc)
            return None

        match = _SHA256_RE.search(content)
        if not match:
            logger.warning('Failed to parse sha256 from %s', sha256_url)
            return None
        return match.group('sha').lower()

    async def _download_text(self, session: aiohttp.ClientSession, url: str) -> str:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.text()

    async def _download_bytes(self, session: aiohttp.ClientSession, url: str) -> bytes:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()

    def _backup_file(self, path: Path) -> Path:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        backup_path = path.with_name(f'{path.name}.{timestamp}.bak')
        shutil.copy2(path, backup_path)
        return backup_path

    def _prune_backups(self, path: Path) -> None:
        backups = sorted(
            path.parent.glob(f'{path.name}.*.bak'),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        for obsolete in backups[self.backup_keep_count:]:
            try:
                obsolete.unlink(missing_ok=True)
            except Exception:
                logger.warning('Failed to remove obsolete geodata backup: %s', obsolete, exc_info=True)

    def _atomic_write(self, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        dir_fd = None
        try:
            with os.fdopen(fd, 'wb') as tmp_file:
                tmp_file.write(content)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())
            os.replace(tmp_name, path)

            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                dir_fd = None
            if dir_fd is not None:
                os.fsync(dir_fd)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass

    def _ensure_path_access(self, path: Path) -> None:
        parent = path.parent

        if parent.exists() and not parent.is_dir():
            raise GeodataUpdateError(f'Geodata assets parent path is not a directory: {parent}')

        writable_probe_parent = parent if parent.exists() else self._first_existing_parent(parent)
        if writable_probe_parent is None:
            raise GeodataUpdateError(f'No existing parent directory found for geodata path: {parent}')

        if not os.access(writable_probe_parent, os.W_OK | os.X_OK):
            raise GeodataUpdateError(
                f'Geodata directory is not writable from this process: target={parent} probe={writable_probe_parent}'
            )

        if path.exists() and not os.access(path, os.R_OK | os.W_OK):
            raise GeodataUpdateError(f'Geodata file is not readable/writable: {path}')

    @staticmethod
    def _first_existing_parent(path: Path) -> Path | None:
        current = path
        while True:
            if current.exists():
                return current
            if current.parent == current:
                return None
            current = current.parent

    @staticmethod
    def _hash_file(path: Path) -> str | None:
        if not path.exists() or not path.is_file():
            return None

        digest = hashlib.sha256()
        with path.open('rb') as file_obj:
            for chunk in iter(lambda: file_obj.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validated_url(value: str, *, field_name: str) -> str:
        normalized = (value or '').strip()
        if not normalized:
            raise GeodataUpdateError(f'{field_name} is empty')

        parsed = urlparse(normalized)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            raise GeodataUpdateError(f'{field_name} must be an absolute http(s) URL')
        return normalized

    @classmethod
    def _validated_optional_url(cls, value: str | None, *, field_name: str) -> str | None:
        normalized = (value or '').strip() if value is not None else ''
        if not normalized:
            return None
        return cls._validated_url(normalized, field_name=field_name)

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self.settings, name, default)