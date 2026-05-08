from __future__ import annotations

import hmac
import logging

logger = logging.getLogger(__name__)

MIN_PLAINTEXT_PASSWORD_LENGTH = 14

try:
    from argon2 import PasswordHasher
    from argon2.exceptions import VerifyMismatchError, InvalidHash

    _hasher = PasswordHasher()
    _ARGON2_AVAILABLE = True
except ImportError:  # pragma: no cover - argon2-cffi is in requirements.txt
    _hasher = None
    _ARGON2_AVAILABLE = False
    VerifyMismatchError = Exception  # type: ignore[assignment,misc]
    InvalidHash = Exception  # type: ignore[assignment,misc]


class WebAdminPasswordTooWeak(ValueError):
    """Plaintext web-admin password fails the minimum strength policy."""


def is_argon2_hash(value: str) -> bool:
    return value.startswith('$argon2')


def hash_password(plaintext: str) -> str:
    """Hash a plaintext password with Argon2id. Raises if argon2-cffi is missing."""
    if not _ARGON2_AVAILABLE or _hasher is None:
        raise RuntimeError('argon2-cffi is not installed; cannot hash password')
    return _hasher.hash(plaintext)


def verify_password(stored: str, provided: str) -> bool:
    """Constant-time verification.

    If `stored` looks like an Argon2 hash, verify with argon2-cffi.
    Otherwise compare plaintext (legacy mode) with hmac.compare_digest.
    Returns False on any error — never raises.
    """
    if not stored or not provided:
        return False

    if is_argon2_hash(stored):
        if not _ARGON2_AVAILABLE or _hasher is None:
            logger.error('Web-admin password is an argon2 hash but argon2-cffi is not available')
            return False
        try:
            return _hasher.verify(stored, provided)
        except (VerifyMismatchError, InvalidHash):
            return False
        except Exception:  # pragma: no cover - defensive
            logger.exception('Unexpected error verifying argon2 hash')
            return False

    return hmac.compare_digest(stored, provided)


def validate_password_strength(stored: str) -> None:
    """Reject startup if the configured password is plaintext and too short.

    Argon2 hashes are accepted as-is (the original plaintext was the operator's
    responsibility at hashing time).
    """
    if is_argon2_hash(stored):
        return
    if len(stored) < MIN_PLAINTEXT_PASSWORD_LENGTH:
        raise WebAdminPasswordTooWeak(
            f'WEB_ADMIN_PASSWORD должен быть длиной не менее '
            f'{MIN_PLAINTEXT_PASSWORD_LENGTH} символов в plaintext-режиме '
            f'или быть argon2id-хэшем (`$argon2id$...`). '
            f'Сгенерировать хэш: '
            f'`python -c "from app.services.web_admin_auth import hash_password; '
            f'print(hash_password(\\"YOUR_PASSWORD\\"))"`'
        )
    if stored.lower() in {'admin', 'password', 'qwerty', '12345678'}:
        raise WebAdminPasswordTooWeak('WEB_ADMIN_PASSWORD слишком очевиден')
