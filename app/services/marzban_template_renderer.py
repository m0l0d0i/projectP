from __future__ import annotations

import ipaddress
import os
from html import escape as html_escape
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from dateutil.relativedelta import relativedelta
from jinja2 import BaseLoader, Environment, FileSystemLoader, StrictUndefined, TemplateError, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import MarzbanPageSettings, Subscription
from app.db.repositories import AppLinkRepository, MarzbanPageSettingsRepository
from app.utils.formatters import bytes_to_gb, format_dt


@dataclass(slots=True)
class PlatformCard:
    key: str
    title: str
    emoji: str
    download_url: str | None
    guide_url: str | None

    @property
    def has_any_link(self) -> bool:
        return bool(self.download_url or self.guide_url)

    def as_dict(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'title': self.title,
            'emoji': self.emoji,
            'download_url': self.download_url,
            'guide_url': self.guide_url,
            'has_any_link': self.has_any_link,
        }


@dataclass(slots=True)
class TemplatePathState:
    source_template_path: Path
    deployed_template_path: Path
    source_exists: bool
    source_writable: bool
    deployed_exists: bool
    deployed_writable: bool


@dataclass(slots=True)
class MarzbanTemplateRenderResult:
    source_template: str
    rendered_html: str
    context: dict[str, Any]
    paths: TemplatePathState


class MarzbanTemplateRenderer:
    PLATFORM_META: dict[str, tuple[str, str]] = {
        'ios': ('iOS', '📱'),
        'android': ('Android', '🤖'),
        'windows': ('Windows', '🪟'),
        'macos': ('macOS', '💻'),
        'linux': ('Linux', '🐧'),
        'androidtv': ('Android TV', '📺'),
    }
    PLATFORM_ORDER: tuple[str, ...] = ('ios', 'android', 'windows', 'macos', 'linux', 'androidtv')
    SOURCE_TEMPLATE_FILENAME = 'marzban_subscription_template_source.html'

    ADMIN_STRING_TOKENS: tuple[str, ...] = (
        'brand_name',
        'page_title',
        'hero_title',
        'hero_text',
        'connect_button_text',
        'connect_hint_text',
        'support_text',
        'platforms_title',
        'platforms_subtitle',
    )
    ADMIN_BOOL_TOKENS: tuple[str, ...] = (
        'show_usage_block',
        'show_subscription_copy_button',
        'show_platform_cards',
        'show_primary_connect_button',
        'show_one_click_block',
        'show_hiddify_button',
        'show_v2raytun_button',
        'show_happ_button',
        'show_qr_button',
    )

    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.page_settings = MarzbanPageSettingsRepository(session)
        self.app_links = AppLinkRepository(session)

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[2]

    @classmethod
    def _default_source_template_path(cls) -> Path:
        return cls._project_root() / 'app' / 'templates' / cls.SOURCE_TEMPLATE_FILENAME

    @classmethod
    def _default_deployed_template_path(cls) -> Path:
        return Path('/var/lib/marzban/templates/subscription/index.html')

    @staticmethod
    def _is_path_writable(path: Path) -> bool:
        if path.exists():
            return path.is_file() and os.access(path, os.W_OK)

        parent = path.parent
        return parent.exists() and parent.is_dir() and os.access(parent, os.W_OK)

    def source_template_path(self) -> Path:
        return self._default_source_template_path()

    def deployed_template_path(self) -> Path:
        return self._default_deployed_template_path()

    def template_paths_state(self) -> TemplatePathState:
        source_path = self.source_template_path()
        deployed_path = self.deployed_template_path()
        return TemplatePathState(
            source_template_path=source_path,
            deployed_template_path=deployed_path,
            source_exists=source_path.exists(),
            source_writable=self._is_path_writable(source_path),
            deployed_exists=deployed_path.exists(),
            deployed_writable=self._is_path_writable(deployed_path),
        )

    @staticmethod
    def _bytesformat_filter(value: Any) -> str:
        if value in (None, ''):
            return 'Безлимит'
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            return str(value)
        return bytes_to_gb(numeric)

    @staticmethod
    def _datetime_filter(value: Any) -> str:
        if value in (None, ''):
            return '—'
        if isinstance(value, (int, float)):
            try:
                return format_dt(datetime.fromtimestamp(float(value), tz=timezone.utc))
            except (OverflowError, OSError, ValueError):
                return '—'
        if isinstance(value, datetime):
            return format_dt(value)
        return str(value)

    @staticmethod
    def _build_jinja_env(*, loader: BaseLoader | FileSystemLoader) -> Environment:
        env = Environment(
            loader=loader,
            autoescape=select_autoescape(enabled_extensions=('html', 'xml'), default_for_string=True),
            undefined=StrictUndefined,
            enable_async=False,
        )
        env.filters['bytesformat'] = MarzbanTemplateRenderer._bytesformat_filter
        env.filters['datetime'] = MarzbanTemplateRenderer._datetime_filter
        env.globals['now'] = lambda: datetime.now(timezone.utc)
        return env

    @classmethod
    def _jinja_env_for_string(cls) -> Environment:
        return cls._build_jinja_env(loader=BaseLoader())

    def _jinja_env_for_files(self) -> Environment:
        return self._build_jinja_env(loader=FileSystemLoader(str(self.source_template_path().parent)))

    @staticmethod
    def _normalize_platform_key(os_name: str) -> str:
        normalized = (os_name or '').strip().lower()
        if normalized in {'ios', 'iphone', 'ipad'}:
            return 'ios'
        if normalized == 'android':
            return 'android'
        if normalized in {'windows', 'win'}:
            return 'windows'
        if normalized in {'macos', 'mac', 'mac os', 'osx'}:
            return 'macos'
        if normalized in {'androidtv', 'android_tv', 'android tv'}:
            return 'androidtv'
        return normalized

    @staticmethod
    def _is_public_http_url(value: str | None) -> bool:
        normalized = (value or '').strip()
        if not normalized:
            return False

        parsed = urlparse(normalized)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
            return False

        hostname = (parsed.hostname or '').strip().lower()
        if not hostname or hostname == 'localhost':
            return False

        if parsed.path.startswith('/admin/'):
            return False

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return True

        return not (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )

    @classmethod
    def _normalize_public_url(cls, value: str | None) -> str | None:
        normalized = (value or '').strip()
        if not normalized:
            return None
        return normalized if cls._is_public_http_url(normalized) else None

    @staticmethod
    def _extract_origin(value: str | None) -> str | None:
        normalized = (value or '').strip()
        if not normalized:
            return None
        parsed = urlparse(normalized)
        if not (parsed.scheme and parsed.netloc):
            return None
        return f'{parsed.scheme}://{parsed.netloc}'

    @staticmethod
    def _path_parts(value: str | None) -> list[str]:
        return [part for part in (value or '').split('/') if part]

    def _configured_public_subscription_origin(self) -> str | None:
        configured = self._extract_origin(getattr(self.settings, 'marzban_subscription_base_url', None))
        if configured is None:
            return None
        return configured.rstrip('/')

    def _canonical_subscription_url(self, raw_url: str | None) -> str | None:
        normalized = (raw_url or '').strip()
        if not normalized:
            return None

        parsed = urlparse(normalized)
        path = parsed.path if (parsed.scheme or parsed.netloc) else normalized
        parts = self._path_parts(path)
        if len(parts) < 2 or parts[0] != 'sub' or not parts[1].strip():
            return None

        token = parts[1].strip()
        public_origin = self._configured_public_subscription_origin()
        if public_origin is not None:
            return f'{public_origin}/sub/{token}'

        if parsed.scheme and parsed.netloc:
            candidate = f'{parsed.scheme}://{parsed.netloc}/sub/{token}'
            return candidate if self._is_public_http_url(candidate) else None

        return None

    @staticmethod
    def _subscription_status(subscription: Subscription) -> tuple[str, str]:
        now = datetime.now(timezone.utc)
        expire_date = getattr(subscription, 'expire_date', None)
        if not getattr(subscription, 'is_active', False):
            return 'Истек', 'expired'
        if expire_date is not None and expire_date <= now:
            return 'Истек', 'expired'
        return 'Активен', 'active'

    @staticmethod
    def _traffic_progress(subscription: Subscription) -> dict[str, Any]:
        limit = getattr(subscription, 'effective_cycle_total_bytes', None)
        if callable(limit):
            limit = limit()
        if limit is None:
            limit = getattr(subscription, 'data_limit_bytes', None)

        used = max(0, int(getattr(subscription, 'used_traffic_bytes', 0) or 0))
        extra_traffic_bytes = max(0, int(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0))
        cycle_base_bytes = getattr(subscription, 'effective_cycle_base_bytes', None)
        if callable(cycle_base_bytes):
            cycle_base_bytes = cycle_base_bytes()

        if limit in (None, 0):
            return {
                'is_unlimited': True,
                'percent': 100,
                'used_label': bytes_to_gb(used),
                'limit_label': 'Безлимит',
                'remaining_label': 'Безлимит',
                'base_label': 'Безлимит',
                'extra_label': bytes_to_gb(extra_traffic_bytes) if extra_traffic_bytes > 0 else None,
            }

        limit_int = max(int(limit), 1)
        remaining = max(limit_int - used, 0)
        percent = max(0, min(100, round((used / limit_int) * 100)))
        return {
            'is_unlimited': False,
            'percent': percent,
            'used_label': bytes_to_gb(used),
            'limit_label': bytes_to_gb(limit_int),
            'remaining_label': bytes_to_gb(remaining),
            'base_label': bytes_to_gb(cycle_base_bytes) if cycle_base_bytes not in (None, 0) else bytes_to_gb(limit_int),
            'extra_label': bytes_to_gb(extra_traffic_bytes) if extra_traffic_bytes > 0 else None,
        }

    async def _build_platform_cards(self) -> list[dict[str, Any]]:
        rows = await self.app_links.ensure_defaults()
        links_by_key = {self._normalize_platform_key(row.os_name): row for row in rows}

        cards: list[dict[str, Any]] = []
        for key in self.PLATFORM_ORDER:
            title, emoji = self.PLATFORM_META[key]
            link_row = links_by_key.get(key)
            card = PlatformCard(
                key=key,
                title=title,
                emoji=emoji,
                download_url=self._normalize_public_url(getattr(link_row, 'download_url', None) if link_row else None),
                guide_url=self._normalize_public_url(getattr(link_row, 'guide_url', None) if link_row else None),
            )
            cards.append(card.as_dict())
        return cards

    @staticmethod
    def _build_legacy_user_context(
        *,
        username: str,
        subscription_url: str | None,
        used_traffic_bytes: int,
        data_limit_bytes: int | None,
        expire_date: datetime | None,
        status_code: str,
        data_limit_reset_strategy: str = 'month',
    ) -> SimpleNamespace:
        links = [subscription_url] if subscription_url else []
        return SimpleNamespace(
            username=username,
            status=SimpleNamespace(value=status_code),
            used_traffic=used_traffic_bytes,
            data_limit=data_limit_bytes,
            expire=expire_date.timestamp() if expire_date is not None else None,
            links=links,
            data_limit_reset_strategy=SimpleNamespace(value=data_limit_reset_strategy),
        )

    async def build_subscription_context(
        self,
        *,
        subscription: Subscription,
        page_settings: MarzbanPageSettings | None = None,
        subscription_url: str | None = None,
    ) -> dict[str, Any]:
        settings_row = page_settings or await self.page_settings.ensure()
        status_label, status_code = self._subscription_status(subscription)
        resolved_subscription_url = self._canonical_subscription_url(
            subscription_url or getattr(subscription, 'subscription_url', None)
        )
        traffic = self._traffic_progress(subscription)
        platform_cards = await self._build_platform_cards()

        cycle_start_at = getattr(subscription, 'traffic_cycle_start_at', None)
        cycle_end_at = getattr(subscription, 'traffic_cycle_end_at', None)

        legacy_user = self._build_legacy_user_context(
            username=str(getattr(subscription, 'service_id', '') or getattr(subscription, 'id', '') or 'SVOIVPN'),
            subscription_url=resolved_subscription_url,
            used_traffic_bytes=int(getattr(subscription, 'used_traffic_bytes', 0) or 0),
            data_limit_bytes=getattr(subscription, 'effective_cycle_total_bytes', None)() if callable(getattr(subscription, 'effective_cycle_total_bytes', None)) else getattr(subscription, 'effective_cycle_total_bytes', None),
            expire_date=getattr(subscription, 'expire_date', None),
            status_code=status_code,
            data_limit_reset_strategy='month',
        )

        return {
            'subscription': subscription,
            'user': legacy_user,
            'brand_name': settings_row.brand_name,
            'page_title': settings_row.page_title,
            'hero_title': settings_row.hero_title,
            'hero_text': settings_row.hero_text,
            'connect_button_text': settings_row.connect_button_text,
            'connect_hint_text': settings_row.connect_hint_text,
            'support_text': settings_row.support_text,
            'platforms_title': settings_row.platforms_title,
            'platforms_subtitle': settings_row.platforms_subtitle,
            'show_usage_block': bool(settings_row.show_usage_block),
            'show_subscription_copy_button': bool(settings_row.show_subscription_copy_button),
            'show_platform_cards': bool(settings_row.show_platform_cards),
            'show_primary_connect_button': bool(settings_row.show_primary_connect_button),
            'show_one_click_block': bool(settings_row.show_one_click_block),
            'show_hiddify_button': bool(settings_row.show_hiddify_button),
            'show_v2raytun_button': bool(settings_row.show_v2raytun_button),
            'show_happ_button': bool(settings_row.show_happ_button),
            'show_qr_button': bool(settings_row.show_qr_button),
            'status_label': status_label,
            'status_code': status_code,
            'expire_label': format_dt(getattr(subscription, 'expire_date', None)),
            'cycle_start_label': format_dt(cycle_start_at) if cycle_start_at else '—',
            'cycle_end_label': format_dt(cycle_end_at) if cycle_end_at else '—',
            'subscription_url': resolved_subscription_url,
            'traffic': traffic,
            'platform_cards': platform_cards,
            'has_cycle_extra_traffic': bool(getattr(subscription, 'cycle_extra_traffic_bytes', 0) or 0),
        }

    async def build_preview_context(self) -> dict[str, Any]:
        settings_row = await self.page_settings.ensure()
        platform_cards = await self._build_platform_cards()
        now = datetime.now(timezone.utc)

        preview_subscription = type('PreviewSubscription', (), {})()
        preview_subscription.service_id = 'DEMO001'
        preview_subscription.expire_date = now + relativedelta(days=21)
        preview_subscription.is_active = True
        preview_subscription.used_traffic_bytes = 45 * (1024 ** 3)
        preview_subscription.data_limit_bytes = 250 * (1024 ** 3)
        preview_subscription.monthly_traffic_bytes = 250 * (1024 ** 3)
        preview_subscription.traffic_cycle_base_bytes = 250 * (1024 ** 3)
        preview_subscription.cycle_extra_traffic_bytes = 50 * (1024 ** 3)
        preview_subscription.traffic_cycle_start_at = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        preview_subscription.traffic_cycle_end_at = preview_subscription.traffic_cycle_start_at + relativedelta(months=1)
        preview_subscription.subscription_url = self._canonical_subscription_url('/sub/demo-preview-token')

        traffic = {
            'is_unlimited': False,
            'percent': 15,
            'used_label': bytes_to_gb(preview_subscription.used_traffic_bytes),
            'limit_label': bytes_to_gb(300 * (1024 ** 3)),
            'remaining_label': bytes_to_gb(255 * (1024 ** 3)),
            'base_label': bytes_to_gb(preview_subscription.traffic_cycle_base_bytes),
            'extra_label': bytes_to_gb(preview_subscription.cycle_extra_traffic_bytes),
        }

        legacy_user = self._build_legacy_user_context(
            username='demo-preview-user',
            subscription_url=preview_subscription.subscription_url,
            used_traffic_bytes=preview_subscription.used_traffic_bytes,
            data_limit_bytes=preview_subscription.data_limit_bytes,
            expire_date=preview_subscription.expire_date,
            status_code='active',
            data_limit_reset_strategy='month',
        )

        return {
            'subscription': preview_subscription,
            'user': legacy_user,
            'brand_name': settings_row.brand_name,
            'page_title': settings_row.page_title,
            'hero_title': settings_row.hero_title,
            'hero_text': settings_row.hero_text,
            'connect_button_text': settings_row.connect_button_text,
            'connect_hint_text': settings_row.connect_hint_text,
            'support_text': settings_row.support_text,
            'platforms_title': settings_row.platforms_title,
            'platforms_subtitle': settings_row.platforms_subtitle,
            'show_usage_block': bool(settings_row.show_usage_block),
            'show_subscription_copy_button': bool(settings_row.show_subscription_copy_button),
            'show_platform_cards': bool(settings_row.show_platform_cards),
            'show_primary_connect_button': bool(settings_row.show_primary_connect_button),
            'show_one_click_block': bool(settings_row.show_one_click_block),
            'show_hiddify_button': bool(settings_row.show_hiddify_button),
            'show_v2raytun_button': bool(settings_row.show_v2raytun_button),
            'show_happ_button': bool(settings_row.show_happ_button),
            'show_qr_button': bool(settings_row.show_qr_button),
            'status_label': 'Активен',
            'status_code': 'active',
            'expire_label': format_dt(preview_subscription.expire_date),
            'cycle_start_label': format_dt(preview_subscription.traffic_cycle_start_at),
            'cycle_end_label': format_dt(preview_subscription.traffic_cycle_end_at),
            'subscription_url': preview_subscription.subscription_url,
            'traffic': traffic,
            'platform_cards': platform_cards,
            'has_cycle_extra_traffic': True,
        }


    @staticmethod
    def _jinja_bool_literal(value: bool) -> str:
        return 'true' if bool(value) else 'false'

    @staticmethod
    def _token_placeholder(token_name: str) -> str:
        return f'[[{token_name}]]'

    @staticmethod
    def _escaped_token_value(value: str | None) -> str:
        return html_escape((value or '').strip(), quote=True)

    @classmethod
    def _platform_token_name(cls, platform_key: str, field_name: str) -> str:
        return f'{platform_key}_{field_name}'

    @classmethod
    def _replace_deploy_tokens(cls, template_source: str, token_map: dict[str, str]) -> str:
        compiled = template_source
        for token_name, replacement in token_map.items():
            compiled = compiled.replace(cls._token_placeholder(token_name), replacement)
        return compiled

    def _build_deploy_token_map(
        self,
        *,
        page_settings: MarzbanPageSettings,
        platform_cards: list[dict[str, Any]],
    ) -> dict[str, str]:
        token_map: dict[str, str] = {}

        for token_name in self.ADMIN_STRING_TOKENS:
            raw_value = getattr(page_settings, token_name, None)
            token_map[token_name] = self._escaped_token_value(raw_value)

        for token_name in self.ADMIN_BOOL_TOKENS:
            token_map[token_name] = self._jinja_bool_literal(bool(getattr(page_settings, token_name, False)))

        cards_by_key = {str(card.get('key') or '').strip().lower(): card for card in platform_cards}
        for platform_key in self.PLATFORM_ORDER:
            card = cards_by_key.get(platform_key, {})
            token_map[self._platform_token_name(platform_key, 'download_url')] = self._escaped_token_value(
                str(card.get('download_url') or '')
            )
            token_map[self._platform_token_name(platform_key, 'guide_url')] = self._escaped_token_value(
                str(card.get('guide_url') or '')
            )
            token_map[self._platform_token_name(platform_key, 'title')] = self._escaped_token_value(
                str(card.get('title') or '')
            )
            token_map[self._platform_token_name(platform_key, 'emoji')] = self._escaped_token_value(
                str(card.get('emoji') or '')
            )
        return token_map

    async def render_deploy_template(
        self,
        *,
        template_source: str | None = None,
        page_settings: MarzbanPageSettings | None = None,
    ) -> MarzbanTemplateRenderResult:
        settings_row = page_settings or await self.page_settings.ensure()
        platform_cards = await self._build_platform_cards()
        source_text = template_source if template_source is not None else self.read_source_template()
        token_map = self._build_deploy_token_map(
            page_settings=settings_row,
            platform_cards=platform_cards,
        )
        deployed_template = self._replace_deploy_tokens(source_text, token_map)
        return MarzbanTemplateRenderResult(
            source_template=source_text,
            rendered_html=deployed_template,
            context=token_map,
            paths=self.template_paths_state(),
        )


    async def _prepare_runtime_template_source(
        self,
        *,
        template_source: str | None = None,
        page_settings: MarzbanPageSettings | None = None,
    ) -> tuple[str, dict[str, Any]]:
        deploy_result = await self.render_deploy_template(
            template_source=template_source,
            page_settings=page_settings,
        )
        return deploy_result.rendered_html, deploy_result.context

    def read_source_template(self) -> str:
        path = self.source_template_path()
        return path.read_text(encoding='utf-8')

    def read_deployed_template(self) -> str | None:
        path = self.deployed_template_path()
        if not path.exists():
            return None
        return path.read_text(encoding='utf-8')

    def validate_template_source(self, template_source: str) -> None:
        try:
            self._jinja_env_for_string().from_string(template_source)
        except TemplateError as exc:
            raise ValueError(f'Некорректный шаблон страницы подписки: {exc}') from exc

    def render_template_source(self, template_source: str, context: dict[str, Any]) -> str:
        self.validate_template_source(template_source)
        try:
            return self._jinja_env_for_string().from_string(template_source).render(context)
        except TemplateError as exc:
            raise ValueError(f'Не удалось отрендерить шаблон страницы подписки: {exc}') from exc

    async def render_preview(self, *, template_source: str | None = None) -> MarzbanTemplateRenderResult:
        source_text = template_source if template_source is not None else self.read_source_template()
        prepared_source, _token_map = await self._prepare_runtime_template_source(template_source=source_text)
        context = await self.build_preview_context()
        rendered_html = self.render_template_source(prepared_source, context)
        return MarzbanTemplateRenderResult(
            source_template=source_text,
            rendered_html=rendered_html,
            context=context,
            paths=self.template_paths_state(),
        )

    async def render_subscription_page(
        self,
        *,
        subscription: Subscription,
        page_settings: MarzbanPageSettings | None = None,
        subscription_url: str | None = None,
        template_source: str | None = None,
    ) -> MarzbanTemplateRenderResult:
        context = await self.build_subscription_context(
            subscription=subscription,
            page_settings=page_settings,
            subscription_url=subscription_url,
        )
        source_text = template_source if template_source is not None else self.read_source_template()
        prepared_source, _token_map = await self._prepare_runtime_template_source(
            template_source=source_text,
            page_settings=page_settings,
        )
        rendered_html = self.render_template_source(prepared_source, context)
        return MarzbanTemplateRenderResult(
            source_template=source_text,
            rendered_html=rendered_html,
            context=context,
            paths=self.template_paths_state(),
        )

    def render_source_template_file(self, context: dict[str, Any]) -> str:
        try:
            template = self._jinja_env_for_files().get_template(self.SOURCE_TEMPLATE_FILENAME)
            return template.render(context)
        except TemplateError as exc:
            raise ValueError(f'Не удалось отрендерить source-template файл: {exc}') from exc
