from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import secrets
from contextlib import suppress
from pathlib import Path
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import text

from app.utils.formatters import bytes_to_gb, format_dt
from app.web.routes import router as web_admin_router


logger = logging.getLogger(__name__)

_ADMIN_CSRF_COOKIE = 'web_admin_csrf'
_ADMIN_MUTATING_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


def _csrf_input(request: Request) -> str:
    token = request.cookies.get(_ADMIN_CSRF_COOKIE, '')
    if not token:
        return ''
    return f'<input type="hidden" name="csrf_token" value="{token}">'


def _get_flashed_messages(*args, **kwargs) -> list[tuple[str, str]] | list[str]:
    return []


def _admin_cookie_secure(request: Request) -> bool:
    return request.url.scheme == 'https'


def _ensure_admin_csrf_cookie(request: Request, response) -> None:
    token = request.cookies.get(_ADMIN_CSRF_COOKIE)
    if token:
        return

    response.set_cookie(
        key=_ADMIN_CSRF_COOKIE,
        value=secrets.token_urlsafe(32),
        httponly=False,
        samesite='strict',
        secure=_admin_cookie_secure(request),
        path='/',
    )


def _csrf_token_matches(expected: str | None, actual: str | None) -> bool:
    if not expected or not actual:
        return False
    return hmac.compare_digest(expected, actual)


def _extract_forwarded_chain(header_value: str | None) -> list[str]:
    if not header_value:
        return []
    return [ip.strip() for ip in header_value.split(',') if ip.strip()]


def _build_templates(templates_dir: Path) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.globals.update(
        format_dt=format_dt,
        bytes_to_gb=bytes_to_gb,
        csrf_cookie_name=_ADMIN_CSRF_COOKIE,
        csrf_input=_csrf_input,
        get_flashed_messages=_get_flashed_messages,
    )
    return templates


def _get_real_ip(request: Request, settings) -> str:
    """
    Извлекает реальный IP клиента с учетом доверенных прокси.
    Доверяем X-Forwarded-For только от явно доверенных proxy IP/CIDR.
    """
    remote_addr = request.client.host if request.client else '127.0.0.1'

    if not settings.web_admin_trust_forwarded_headers:
        return remote_addr

    try:
        client_ip_obj = ipaddress.ip_address(remote_addr)
        is_trusted_proxy = any(
            client_ip_obj in ipaddress.ip_network(proxy)
            for proxy in settings.web_admin_allowed_proxy_ips
        )
    except ValueError:
        return remote_addr

    if not is_trusted_proxy:
        return remote_addr

    forwarded_chain = _extract_forwarded_chain(request.headers.get(settings.web_admin_forwarded_for_header))
    if not forwarded_chain:
        return remote_addr

    candidate = forwarded_chain[0]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        logger.warning('Ignoring malformed forwarded client IP: %s', candidate)
        return remote_addr

    return candidate


def create_fastapi_app(*, sessionmaker, settings) -> FastAPI:
    """
    Local-only web admin application.
    """
    base_dir = Path(__file__).resolve().parents[1]
    static_dir = base_dir / 'static'
    templates_dir = base_dir / 'templates'

    static_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(
        title=f'{settings.service_name} Web Admin',
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.sessionmaker = sessionmaker
    app.state.settings = settings
    app.state.templates = _build_templates(templates_dir)

    @app.middleware('http')
    async def ip_restriction_middleware(request: Request, call_next):
        path = request.url.path or '/'
        is_admin_surface = path.startswith('/admin')

        if settings.web_admin_local_only:
            real_ip = _get_real_ip(request, settings)

            try:
                ip_obj = ipaddress.ip_address(real_ip)
                is_allowed = any(
                    ip_obj in ipaddress.ip_network(allowed)
                    for allowed in settings.web_admin_allowed_ips
                )
            except ValueError:
                is_allowed = False

            if not is_allowed:
                logger.warning(
                    'Access denied for IP: %s (Remote: %s, Forwarded-For: %s)',
                    real_ip,
                    request.client.host if request.client else 'unknown',
                    request.headers.get(settings.web_admin_forwarded_for_header),
                )
                return JSONResponse(
                    status_code=403,
                    content={'detail': 'Access restricted to authorized local networks only.'},
                )

        if is_admin_surface and request.method in _ADMIN_MUTATING_METHODS:
            cookie_token = request.cookies.get(_ADMIN_CSRF_COOKIE)
            header_token = request.headers.get('X-CSRF-Token')
            form_token = None
            content_type = (request.headers.get('content-type') or '').lower()

            body = b''
            if 'application/x-www-form-urlencoded' in content_type or 'multipart/form-data' in content_type:
                with suppress(Exception):
                    body = await request.body()

                if body and 'application/x-www-form-urlencoded' in content_type:
                    parsed = parse_qs(body.decode('utf-8', errors='ignore'), keep_blank_values=True)
                    form_token = (parsed.get('csrf_token') or [None])[0]

                if body:
                    consumed = False

                    async def _receive() -> dict[str, object]:
                        nonlocal consumed
                        if consumed:
                            return {'type': 'http.request', 'body': b'', 'more_body': False}
                        consumed = True
                        return {'type': 'http.request', 'body': body, 'more_body': False}

                    request._receive = _receive

            if not _csrf_token_matches(cookie_token, header_token or form_token):
                logger.warning('Admin CSRF validation failed for path=%s method=%s', path, request.method)
                return JSONResponse(status_code=403, content={'detail': 'Проверка CSRF не пройдена'})

        response = await call_next(request)
        if is_admin_surface:
            response.headers.setdefault('Cache-Control', 'no-store')
            if request.method in {'GET', 'HEAD'}:
                _ensure_admin_csrf_cookie(request, response)
        return response

    app.state.bot = None
    app.state.anti_spam_service = None
    app.state.cache = None
    app.state.redis = None
    app.state.redis_client = None
    app.state.scheduler = None
    app.state.marzban = None
    app.state.payments = None

    app.state.web_surface = 'admin_only'

    @app.get('/healthz', include_in_schema=False)
    async def healthz() -> dict[str, bool]:
        return {'ok': True}

    @app.get('/readyz', include_in_schema=False)
    async def readyz() -> JSONResponse:
        sessionmaker_local = getattr(app.state, 'sessionmaker', None)
        if sessionmaker_local is not None:
            try:
                async with sessionmaker_local() as session:
                    await session.execute(text('SELECT 1'))
            except Exception as exc:
                logger.error('Admin readiness failed: DB error - %s', exc)
                return JSONResponse({'ok': False, 'error': 'db_unreachable'}, status_code=500)

        if settings.redis_url:
            cache_local = getattr(app.state, 'cache', None)
            redis_client = getattr(cache_local, 'redis', None) if cache_local is not None else None
            if redis_client is None:
                logger.error('Admin readiness failed: Redis configured but cache instance not attached')
                return JSONResponse(
                    {'ok': False, 'error': 'redis_driver_unavailable'}, status_code=500
                )
            try:
                pong = await redis_client.ping()
                if not pong:
                    return JSONResponse(
                        {'ok': False, 'error': 'redis_unreachable'}, status_code=500
                    )
            except Exception as exc:
                logger.error('Admin readiness failed: Redis error - %s', exc)
                return JSONResponse({'ok': False, 'error': 'redis_unreachable'}, status_code=500)

        return JSONResponse(
            {
                'ok': True,
                'db': 'connected' if sessionmaker_local is not None else 'disabled',
                'redis': 'connected' if settings.redis_url else 'disabled',
            }
        )

    app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')
    app.include_router(web_admin_router)
    return app


async def start_fastapi_server(*, sessionmaker, settings) -> tuple[uvicorn.Server, asyncio.Task]:
    app = create_fastapi_app(sessionmaker=sessionmaker, settings=settings)
    forwarded_allow_ips = None
    if settings.web_admin_trust_forwarded_headers:
        forwarded_allow_ips = ','.join(settings.web_admin_allowed_proxy_ips)

    config = uvicorn.Config(
        app=app,
        host=settings.web_admin_host,
        port=settings.web_admin_port,
        loop='asyncio',
        log_level=str(settings.log_level).lower(),
        access_log=False,
        proxy_headers=bool(settings.web_admin_trust_forwarded_headers),
        forwarded_allow_ips=forwarded_allow_ips,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name='fastapi-admin-server')
    await asyncio.sleep(0)
    return server, task


async def stop_fastapi_server(server: uvicorn.Server | None) -> None:
    if server is None:
        return

    server.should_exit = True
    for _ in range(50):
        if not getattr(server, 'started', False):
            break
        await asyncio.sleep(0.1)