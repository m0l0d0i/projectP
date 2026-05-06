from __future__ import annotations

import ipaddress
import json
from functools import lru_cache
from typing import Any
from urllib.parse import urljoin, urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Runtime/env settings.

    Часть значений ниже управляется через БД (AppSettings).
    Env-поля сохраняются как bootstrap/fallback.

    Правило для Marzban URL:
    - marzban_api_base_url: внутренний URL панели/API, используемый backend-сервисами
    - marzban_subscription_base_url: публичный origin, на котором доступен только /sub/<token>

    Legacy public flow (DEMO_SUBSCRIPTION_URL) полностью удален.
    """

    model_config = SettingsConfigDict(
        env_file='.env',
        env_file_encoding='utf-8',
        case_sensitive=False,
        extra='ignore',
    )

    bot_token: str
    database_url: str

    admin_ids: list[int] = Field(default_factory=list)
    support_ids: list[int] = Field(default_factory=list)
    startup_notify_ids: list[int] = Field(default_factory=list)
    support_chat_id: int | None = None
    telegram_proxy_url: str | None = None

    ui_timezone: str = 'Europe/Moscow'
    db_timezone: str = 'UTC'

    anti_spam_enabled: bool = True
    anti_spam_message_limit: int = 8
    anti_spam_message_window_seconds: int = 12
    anti_spam_callback_limit: int = 12
    anti_spam_callback_window_seconds: int = 8
    anti_spam_block_seconds: int = 10
    anti_spam_min_interval_seconds: float = 1.0
    anti_spam_settings_cache_ttl_seconds: int = 30

    payment_provider: str = 'mock'
    payment_success_delay_seconds: int = 2

    platega_base_url: str = 'https://app.platega.io'
    platega_merchant_id: str | None = None
    platega_secret: SecretStr | None = None
    platega_payment_method: int = 2
    platega_currency: str = 'RUB'
    platega_return_url: str | None = None
    platega_failed_url: str | None = None
    platega_timeout_seconds: float = 20.0
    platega_callback_path: str = '/callbacks/platega/payment-status'
    platega_callback_max_body_bytes: int = 64 * 1024
    platega_request_retries: int = 3

    marzban_enabled: bool = False
    marzban_api_base_url: str | None = None
    marzban_subscription_base_url: str | None = None
    marzban_username: str | None = None
    marzban_password: SecretStr | None = None
    marzban_vless_inbounds: list[str] = Field(default_factory=list)
    marzban_status_on_create: str = 'active'
    marzban_online_limit_field: str | None = 'onlinelimit'
    marzban_request_timeout_seconds: float = 20.0
    marzban_request_retries: int = 2
    marzban_token_ttl_seconds: int = 300

    marzban_env_file_path: str = '/opt/marzban/.env'
    marzban_managed_env_allowlist: list[str] = Field(
        default_factory=lambda: [
            'XRAY_SUBSCRIPTION_URL_PREFIX',
            'XRAY_FALLBACK_DNS',
            'XRAY_GEOIP_PATH',
            'XRAY_GEOSITE_PATH',
            'UVICORN_FORWARDED_ALLOW_IPS',
            'DOCS',
            'REDOC',
        ]
    )
    marzban_managed_env_readonly_keys: list[str] = Field(
        default_factory=lambda: [
            'SQLALCHEMY_DATABASE_URL',
            'POSTGRES_PASSWORD',
            'JWT_SECRET_KEY',
            'SUDO_USERNAME',
            'SUDO_PASSWORD',
        ]
    )

    geodata_update_enabled: bool = True
    geodata_auto_update_on_startup: bool = False
    geodata_assets_dir: str = '/opt/marzban/assets'
    geodata_geoip_filename: str = 'geoip.dat'
    geodata_geosite_filename: str = 'geosite.dat'
    geodata_geoip_url: str = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat'
    geodata_geosite_url: str = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat'
    geodata_geoip_sha256_url: str | None = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat.sha256sum'
    geodata_geosite_sha256_url: str | None = 'https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat.sha256sum'
    geodata_request_timeout_seconds: float = 60.0
    geodata_check_interval_hours: int = 24
    geodata_retained_backups: int = 2

    app_host: str = '0.0.0.0'
    app_port: int = 8080

    web_admin_host: str = '127.0.0.1'
    web_admin_port: int = 8001
    web_admin_username: str = 'admin'
    web_admin_password: SecretStr = SecretStr('admin')
    web_admin_local_only: bool = True
    web_admin_allowed_ips: list[str] = Field(default_factory=lambda: ['127.0.0.1', '::1'])
    web_admin_allowed_proxy_ips: list[str] = Field(default_factory=list)
    web_admin_trust_forwarded_headers: bool = False
    web_admin_forwarded_for_header: str = 'X-Forwarded-For'

    log_level: str = 'INFO'
    log_dir: str = 'logs'

    webhook_enabled: bool = True
    webhook_base_url: str | None = None
    telegram_webhook_path: str = '/webhook/telegram'
    telegram_webhook_secret: str | None = None

    sentry_dsn: str | None = None
    sentry_environment: str = 'production'
    metrics_enabled: bool = True

    trial_duration_hours: int = 24
    trial_duration_days: int = 1
    trial_traffic_gb: int = 5
    trial_device_count: int = 1

    show_subscription_copy_button: bool = True
    show_subscription_page_button: bool = True

    service_name: str = 'SwoiVPN'
    bot_username: str = 'swoi_vpn_bot'

    support_ticket_auto_close_hours: int = 48
    support_max_media_bytes: int = 20 * 1024 * 1024
    support_allowed_media_types: list[str] = Field(
        default_factory=lambda: ['photo', 'video', 'document', 'audio', 'voice', 'video_note', 'animation', 'sticker']
    )

    redis_url: str | None = None
    redis_prefix: str = 'vpn_bot'
    user_cache_ttl_seconds: int = 60

    scheduler_enabled: bool = True
    scheduler_leader_lock_ttl_seconds: int = 90

    broadcast_batch_size: int = 200
    broadcast_send_delay_seconds: float = 0.05
    broadcast_retry_attempts: int = 3
    broadcast_retry_base_delay_seconds: float = 1.0
    broadcast_notify_policy: str = 'always'

    rules_service_url: str | None = None
    rules_of_use_url: str | None = None
    rules_privacy_url: str | None = None

    @staticmethod
    def _parse_listish(value: Any) -> list[Any]:
        if value is None or value == '':
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, (tuple, set)):
            return list(value)
        if isinstance(value, (int, float)):
            return [value]
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith('[') and raw.endswith(']'):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    return [item.strip().strip('"').strip("'") for item in raw[1:-1].split(',') if item.strip()]
                else:
                    if isinstance(parsed, list):
                        return parsed
                    return [parsed]
            return [item.strip() for item in raw.split(',') if item.strip()]
        raise TypeError(f'Unsupported list-like value: {type(value)!r}')

    @staticmethod
    def _secret_value(value: SecretStr | str | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, SecretStr):
            return value.get_secret_value()
        return value

    @staticmethod
    def _normalize_base_url(value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        return cleaned.rstrip('/')

    @staticmethod
    def _validate_ip_or_network(value: str, *, field_name: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f'{field_name} не может содержать пустые значения')

        try:
            if '/' in normalized:
                ipaddress.ip_network(normalized, strict=False)
            else:
                ipaddress.ip_address(normalized)
        except ValueError as exc:
            raise ValueError(f'{field_name} содержит некорректный IP/CIDR: {normalized}') from exc

        return normalized

    @staticmethod
    def _validate_public_https_base_url(value: str | None, *, field_name: str) -> str | None:
        normalized = Settings._normalize_base_url(value)
        if normalized is None:
            return None

        parts = urlparse(normalized)
        if parts.scheme != 'https' or not parts.netloc:
            raise ValueError(f'{field_name} должен быть полным https URL')

        hostname = (parts.hostname or '').strip().lower()
        if not hostname:
            raise ValueError(f'{field_name} должен содержать hostname')

        if hostname == 'localhost':
            raise ValueError(f'{field_name} не может указывать на localhost')

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return normalized

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f'{field_name} не может указывать на private/loopback IP')

        return normalized

    @staticmethod
    def _validate_public_https_url(value: str | None, *, field_name: str) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None

        parts = urlparse(normalized)
        if parts.scheme != 'https' or not parts.netloc:
            raise ValueError(f'{field_name} должен быть полным https URL')

        hostname = (parts.hostname or '').strip().lower()
        if not hostname:
            raise ValueError(f'{field_name} должен содержать hostname')

        if hostname == 'localhost':
            raise ValueError(f'{field_name} не может указывать на localhost')

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return normalized

        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f'{field_name} не может указывать на private/loopback IP')

        return normalized

    @staticmethod
    def _normalize_env_key(value: Any) -> str:
        normalized = str(value or '').strip().upper()
        if not normalized:
            raise ValueError('Ключ env не может быть пустым')
        return normalized

    @property
    def platega_secret_value(self) -> str | None:
        return self._secret_value(self.platega_secret)

    @property
    def marzban_password_value(self) -> str | None:
        return self._secret_value(self.marzban_password)

    @property
    def web_admin_password_value(self) -> str:
        value = self._secret_value(self.web_admin_password)
        if value is None:
            raise ValueError('WEB_ADMIN_PASSWORD обязателен')
        return value

    @property
    def marzban_base_url(self) -> str | None:
        """
        Deprecated: Backward-compatible alias.
        Use marzban_api_base_url instead.
        """
        return self.marzban_api_base_url

    @property
    def effective_marzban_managed_env_allowlist(self) -> list[str]:
        readonly = set(self.marzban_managed_env_readonly_keys)
        return [key for key in self.marzban_managed_env_allowlist if key not in readonly]

    @property
    def web_admin_loopback_hosts(self) -> set[str]:
        return {'127.0.0.1', 'localhost', '::1'}

    @property
    def is_mock_payment_provider(self) -> bool:
        return self.payment_provider == 'mock'

    @property
    def is_platega_payment_provider(self) -> bool:
        return self.payment_provider == 'platega'

    @property
    def platega_configured(self) -> bool:
        return bool(self.platega_base_url and self.platega_merchant_id and self.platega_secret_value)

    @property
    def platega_callback_url(self) -> str | None:
        if not self.webhook_base_url:
            return None
        return urljoin(f'{self.webhook_base_url.rstrip('/')}/', self.platega_callback_path.lstrip('/'))

    @field_validator('admin_ids', 'support_ids', 'startup_notify_ids', mode='before')
    @classmethod
    def parse_int_lists(cls, value: Any) -> list[int]:
        return [int(item) for item in cls._parse_listish(value)]

    @field_validator('marzban_vless_inbounds', mode='before')
    @classmethod
    def parse_vless_inbounds(cls, value: Any) -> list[str]:
        return [str(item) for item in cls._parse_listish(value)]

    @field_validator('support_allowed_media_types', mode='before')
    @classmethod
    def parse_media_types(cls, value: Any) -> list[str]:
        return [str(item).lower() for item in cls._parse_listish(value)]

    @field_validator(
        'web_admin_allowed_ips',
        'web_admin_allowed_proxy_ips',
        'marzban_managed_env_allowlist',
        'marzban_managed_env_readonly_keys',
        mode='before',
    )
    @classmethod
    def parse_string_lists(cls, value: Any) -> list[str]:
        return [str(item).strip() for item in cls._parse_listish(value) if str(item).strip()]

    @field_validator('web_admin_allowed_ips', 'web_admin_allowed_proxy_ips')
    @classmethod
    def validate_ip_lists(cls, value: list[str], info) -> list[str]:
        return [cls._validate_ip_or_network(item, field_name=info.field_name.upper()) for item in value]

    @field_validator('marzban_managed_env_allowlist', 'marzban_managed_env_readonly_keys')
    @classmethod
    def validate_env_key_lists(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            env_key = cls._normalize_env_key(item)
            if env_key in seen:
                continue
            seen.add(env_key)
            normalized.append(env_key)
        return normalized

    @field_validator('support_chat_id', mode='before')
    @classmethod
    def parse_support_chat_id(cls, value: Any) -> int | None:
        if value in (None, ''):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            raw = value.strip()
            if raw.startswith('[') and raw.endswith(']'):
                raw = raw[1:-1].strip()
            return int(raw)
        raise TypeError('SUPPORT_CHAT_ID должен быть целым числом')

    @field_validator('payment_provider')
    @classmethod
    def validate_payment_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {'mock', 'platega'}:
            raise ValueError('PAYMENT_PROVIDER должен быть mock или platega')
        return normalized

    @field_validator('bot_token')
    @classmethod
    def validate_bot_token(cls, value: str) -> str:
        value = value.strip()
        if ':' not in value:
            raise ValueError('BOT_TOKEN должен быть в формате <id>:<token>')
        return value

    @field_validator('web_admin_username')
    @classmethod
    def validate_web_admin_username(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('WEB_ADMIN_USERNAME не может быть пустым')
        return value

    @field_validator('web_admin_password')
    @classmethod
    def validate_web_admin_password(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw:
            raise ValueError('WEB_ADMIN_PASSWORD не может быть пустым')
        return value

    @field_validator('marzban_env_file_path')
    @classmethod
    def validate_marzban_env_file_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('MARZBAN_ENV_FILE_PATH не может быть пустым')
        return normalized

    @field_validator('web_admin_forwarded_for_header')
    @classmethod
    def validate_forwarded_for_header(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError('WEB_ADMIN_FORWARDED_FOR_HEADER не может быть пустым')
        return normalized

    @field_validator('platega_callback_path', 'telegram_webhook_path')
    @classmethod
    def normalize_callback_path(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('Путь webhook/callback не может быть пустым')
        return value if value.startswith('/') else f'/{value}'

    @field_validator('platega_base_url', 'marzban_api_base_url', 'webhook_base_url', mode='before')
    @classmethod
    def normalize_base_urls(cls, value: Any) -> str | None:
        if value in (None, ''):
            return None
        if not isinstance(value, str):
            raise TypeError('URL должен быть строкой')
        return cls._normalize_base_url(value)

    @field_validator('marzban_subscription_base_url', mode='before')
    @classmethod
    def normalize_public_subscription_base_url(cls, value: Any) -> str | None:
        if value in (None, ''):
            return None
        if not isinstance(value, str):
            raise TypeError('URL должен быть строкой')
        return cls._normalize_base_url(value)

    @field_validator('marzban_subscription_base_url')
    @classmethod
    def validate_public_subscription_base_url(cls, value: str | None) -> str | None:
        return cls._validate_public_https_base_url(value, field_name='MARZBAN_SUBSCRIPTION_BASE_URL')

    @field_validator('platega_return_url', 'platega_failed_url')
    @classmethod
    def validate_platega_public_urls(cls, value: str | None, info) -> str | None:
        return cls._validate_public_https_url(value, field_name=info.field_name.upper())

    @field_validator('app_port', 'web_admin_port')
    @classmethod
    def validate_app_port(cls, value: int, info) -> int:
        if not (1 <= int(value) <= 65535):
            raise ValueError(f'{info.field_name.upper()} должен быть в диапазоне 1..65535')
        return int(value)

    @field_validator('platega_payment_method')
    @classmethod
    def validate_platega_payment_method(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError('PLATEGA_PAYMENT_METHOD должен быть >= 1')
        return int(value)

    @field_validator('platega_timeout_seconds')
    @classmethod
    def validate_platega_timeout_seconds(cls, value: float) -> float:
        if float(value) <= 0:
            raise ValueError('PLATEGA_TIMEOUT_SECONDS должен быть > 0')
        return float(value)

    @field_validator('platega_callback_max_body_bytes')
    @classmethod
    def validate_platega_callback_max_body_bytes(cls, value: int) -> int:
        if int(value) < 1024:
            raise ValueError('PLATEGA_CALLBACK_MAX_BODY_BYTES должен быть >= 1024')
        return int(value)

    @field_validator('platega_request_retries')
    @classmethod
    def validate_platega_request_retries(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError('PLATEGA_REQUEST_RETRIES должен быть >= 1')
        return int(value)

    @field_validator('platega_currency')
    @classmethod
    def validate_platega_currency(cls, value: str) -> str:
        normalized = str(value or '').strip().upper()
        if not normalized:
            raise ValueError('PLATEGA_CURRENCY не может быть пустым')
        if len(normalized) != 3:
            raise ValueError('PLATEGA_CURRENCY должен быть 3-буквенным кодом валюты')
        return normalized

    @field_validator('webhook_base_url')
    @classmethod
    def validate_webhook_base_url(cls, value: str | None, info):
        if info.data.get('webhook_enabled'):
            if not value:
                raise ValueError('WEBHOOK_BASE_URL обязателен при WEBHOOK_ENABLED=true')
            if not value.startswith('https://'):
                raise ValueError('WEBHOOK_BASE_URL должен начинаться с https://')
        return value

    @field_validator('telegram_webhook_secret')
    @classmethod
    def validate_webhook_secret(cls, value: str | None, info):
        if info.data.get('webhook_enabled') and not value:
            raise ValueError('TELEGRAM_WEBHOOK_SECRET обязателен при WEBHOOK_ENABLED=true')
        return value

    @field_validator('platega_merchant_id', 'platega_secret', mode='after')
    @classmethod
    def validate_platega_fields(cls, value, info):
        if info.data.get('payment_provider') == 'platega' and not cls._secret_value(value):
            raise ValueError(f'{info.field_name.upper()} обязателен для PAYMENT_PROVIDER=platega')
        return value

    @field_validator('marzban_api_base_url', 'marzban_username', 'marzban_password', mode='after')
    @classmethod
    def validate_marzban_fields(cls, value, info):
        if info.data.get('marzban_enabled') and not cls._secret_value(value):
            raise ValueError(f'{info.field_name.upper()} обязателен для MARZBAN_ENABLED=true')
        return value

    @field_validator('user_cache_ttl_seconds', 'anti_spam_settings_cache_ttl_seconds')
    @classmethod
    def validate_cache_ttl(cls, value: int, info) -> int:
        if int(value) < 1:
            raise ValueError(f'{info.field_name.upper()} должен быть >= 1')
        return int(value)

    @field_validator('anti_spam_min_interval_seconds')
    @classmethod
    def validate_min_interval(cls, value: float) -> float:
        if float(value) < 0:
            raise ValueError('ANTI_SPAM_MIN_INTERVAL_SECONDS должен быть >= 0')
        return float(value)

    @field_validator('redis_prefix')
    @classmethod
    def validate_redis_prefix(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('REDIS_PREFIX не может быть пустым')
        return value

    @field_validator('scheduler_leader_lock_ttl_seconds')
    @classmethod
    def validate_scheduler_ttl(cls, value: int) -> int:
        if int(value) < 10:
            raise ValueError('SCHEDULER_LEADER_LOCK_TTL_SECONDS должен быть >= 10')
        return int(value)

    @field_validator('broadcast_batch_size')
    @classmethod
    def validate_broadcast_batch_size(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError('BROADCAST_BATCH_SIZE должен быть >= 1')
        return int(value)

    @field_validator('broadcast_send_delay_seconds', 'broadcast_retry_base_delay_seconds')
    @classmethod
    def validate_broadcast_delays(cls, value: float, info):
        if float(value) < 0:
            raise ValueError(f'{info.field_name.upper()} должен быть >= 0')
        return float(value)

    @field_validator('broadcast_retry_attempts')
    @classmethod
    def validate_broadcast_retry_attempts(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError('BROADCAST_RETRY_ATTEMPTS должен быть >= 1')
        return int(value)

    @field_validator('broadcast_notify_policy')
    @classmethod
    def validate_broadcast_notify_policy(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {'always', 'failures', 'never'}:
            raise ValueError('BROADCAST_NOTIFY_POLICY должен быть one of: always, failures, never')
        return normalized

    @field_validator('trial_duration_hours', 'trial_duration_days', 'trial_device_count')
    @classmethod
    def validate_positive_trial_values(cls, value: int, info) -> int:
        if int(value) < 1:
            raise ValueError(f'{info.field_name.upper()} должен быть >= 1')
        return int(value)

    @field_validator('trial_traffic_gb')
    @classmethod
    def validate_trial_traffic_gb(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError('TRIAL_TRAFFIC_GB должен быть >= 0')
        return int(value)

    @model_validator(mode='after')
    def validate_admin_local_only_policy(self) -> 'Settings':
        host_normalized = (self.web_admin_host or '').strip().lower()
        if self.web_admin_local_only and host_normalized not in self.web_admin_loopback_hosts:
            raise ValueError('WEB_ADMIN_HOST должен быть loopback адресом при WEB_ADMIN_LOCAL_ONLY=true')

        if self.web_admin_local_only and not self.web_admin_allowed_ips:
            raise ValueError('WEB_ADMIN_ALLOWED_IPS не может быть пустым при WEB_ADMIN_LOCAL_ONLY=true')

        if self.web_admin_trust_forwarded_headers and not self.web_admin_allowed_proxy_ips:
            raise ValueError(
                'WEB_ADMIN_ALLOWED_PROXY_IPS не может быть пустым при WEB_ADMIN_TRUST_FORWARDED_HEADERS=true'
            )

        if self.marzban_enabled and not self.marzban_subscription_base_url:
            raise ValueError('MARZBAN_SUBSCRIPTION_BASE_URL обязателен для MARZBAN_ENABLED=true')

        if self.db_timezone.upper() != 'UTC':
            raise ValueError('DB_TIMEZONE должен быть UTC')

        readonly = set(self.marzban_managed_env_readonly_keys)
        allowlist = set(self.marzban_managed_env_allowlist)

        if not allowlist:
            raise ValueError('MARZBAN_MANAGED_ENV_ALLOWLIST не может быть пустым')

        overlapping = allowlist.intersection(readonly)
        if overlapping:
            overlap_preview = ', '.join(sorted(overlapping))
            raise ValueError(
                f'MARZBAN_MANAGED_ENV_ALLOWLIST не должен содержать readonly-ключи: {overlap_preview}'
            )

        if self.geodata_geoip_filename == self.geodata_geosite_filename:
            raise ValueError('GEODATA_GEOIP_FILENAME и GEODATA_GEOSITE_FILENAME должны отличаться')

        if self.is_platega_payment_provider:
            if not self.platega_base_url:
                raise ValueError('PLATEGA_BASE_URL обязателен для PAYMENT_PROVIDER=platega')
            if not self.platega_base_url.startswith('https://'):
                raise ValueError('PLATEGA_BASE_URL должен начинаться с https://')
            if self.webhook_enabled and not self.platega_callback_url:
                raise ValueError('PLATEGA callback URL не может быть вычислен без WEBHOOK_BASE_URL')

        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
