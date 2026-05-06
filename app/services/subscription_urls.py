from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


_CANONICAL_PREFIX = 'sub'
_LEGACY_PREFIXES = {'subscription', 'profile'}
_ALLOWED_SCHEMES = {'http', 'https'}


class SubscriptionUrlError(ValueError):
    """Raised when a subscription URL or token cannot be canonicalized safely."""


def _normalized_str(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _path_parts(path: str | None) -> list[str]:
    return [part for part in (path or '').split('/') if part]


def _looks_like_absolute_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def normalize_public_subscription_origin(value: str | None) -> str | None:
    normalized = _normalized_str(value)
    if normalized is None:
        return None

    parsed = urlparse(normalized)
    if not (parsed.scheme and parsed.netloc):
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    return f'{parsed.scheme.lower()}://{parsed.netloc}'.rstrip('/')


def canonical_subscription_path(token: str | None) -> str | None:
    normalized_token = _normalized_str(token)
    if normalized_token is None:
        return None

    cleaned_token = normalized_token.strip('/')
    if not cleaned_token or '/' in cleaned_token:
        return None
    return f'/{_CANONICAL_PREFIX}/{cleaned_token}'


def is_canonical_subscription_path(value: str | None) -> bool:
    normalized = _normalized_str(value)
    if normalized is None:
        return False

    parsed = urlparse(normalized)
    candidate_path = parsed.path if (parsed.scheme or parsed.netloc) else normalized
    parts = _path_parts(candidate_path)
    return len(parts) >= 2 and parts[0] == _CANONICAL_PREFIX and bool(parts[1].strip())


def extract_subscription_token(value: str | None, *, allow_bare_token: bool = False) -> str | None:
    normalized = _normalized_str(value)
    if normalized is None:
        return None

    parsed = urlparse(normalized)
    candidate_path = parsed.path if (parsed.scheme or parsed.netloc) else normalized
    parts = _path_parts(candidate_path)

    if len(parts) >= 2 and parts[0] == _CANONICAL_PREFIX:
        token = parts[1].strip()
        return token or None

    if parts and parts[0] in _LEGACY_PREFIXES:
        return None

    if allow_bare_token and not _looks_like_absolute_url(normalized):
        bare_token = normalized.strip().strip('/')
        if bare_token and '/' not in bare_token:
            return bare_token

    return None


def build_canonical_subscription_url(token: str | None, *, public_origin: str | None = None) -> str | None:
    path = canonical_subscription_path(token)
    if path is None:
        return None

    origin = normalize_public_subscription_origin(public_origin)
    if origin is None:
        return path
    return f'{origin}{path}'


def canonicalize_subscription_url(
    value: str | None,
    *,
    public_origin: str | None = None,
    allow_bare_token: bool = False,
) -> str | None:
    token = extract_subscription_token(value, allow_bare_token=allow_bare_token)
    if token is None:
        return None
    return build_canonical_subscription_url(token, public_origin=public_origin)


def configured_public_subscription_origin(settings: Any) -> str | None:
    return normalize_public_subscription_origin(getattr(settings, 'marzban_subscription_base_url', None))


def canonicalize_subscription_url_from_settings(
    value: str | None,
    settings: Any,
    *,
    allow_bare_token: bool = False,
) -> str | None:
    return canonicalize_subscription_url(
        value,
        public_origin=configured_public_subscription_origin(settings),
        allow_bare_token=allow_bare_token,
    )


def require_canonical_subscription_url(
    value: str | None,
    *,
    public_origin: str | None = None,
    allow_bare_token: bool = False,
) -> str:
    canonical = canonicalize_subscription_url(
        value,
        public_origin=public_origin,
        allow_bare_token=allow_bare_token,
    )
    if canonical is None:
        raise SubscriptionUrlError('Subscription URL must use canonical /sub/<token> format.')
    return canonical
