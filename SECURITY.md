# Безопасность

> Аудит безопасности. Приоритеты: `P0` — немедленно, `P1` — текущий спринт, `P2` — 1–2 месяца, `P3` — backlog.
> Чек-боксы: `[ ]` не начато · `[~]` в работе · `[x]` сделано · `[!]` отложено.
>
> См. также: [SPRINT_PLAN.md](SPRINT_PLAN.md), [DECISIONS.md](DECISIONS.md).

## CRITICAL (P0 — сделать в первую очередь)

- [ ] **SEC-C1.** Реальные продовые секреты в `.env.example` (BOT_TOKEN, PLATEGA_SECRET, PLATEGA_MERCHANT_ID, POSTGRES_PASSWORD, MARZBAN_PASSWORD, WEB_ADMIN_PASSWORD, TELEGRAM_WEBHOOK_SECRET) — частично замаскированы, но префикс `8ecc0cb10abf` повторяется в `WEB_ADMIN_PASSWORD` и `TELEGRAM_WEBHOOK_SECRET`. Файл лежит в обоих коммитах (`4a2269f`, `b91e882`).
  - **Файлы:** `.env.example:8,12-13,40,53,71,88`
  - **Что сделать:** ротировать все 6 секретов (BotFather, Platega, Postgres, Marzban, web-admin, webhook), переписать `.env.example` на `CHANGE_ME`, очистить историю git (`git filter-repo`), добавить pre-commit hook `gitleaks`.
- [ ] **SEC-C2.** Bot-контейнер монтирует `/var/lib/marzban/assets` и `/var/lib/marzban/templates` с RW. Любая ошибка в `_atomic_write_text` / geodata пайплайне → отравление публичной страницы подписки и `geoip.dat`/`geosite.dat` для всех клиентов. Дополнительно: `.env.example` указывает `GEODATA_ASSETS_DIR=/opt/marzban/assets`, а compose монтирует `/var/lib/marzban/assets` — geodata тихо пишется в эфемерный путь контейнера.
  - **Файлы:** `docker-compose.yml:91-92`, `app/services/geodata_updater.py`, `app/services/marzban_template_renderer.py:115`, `app/web/routes.py:4405`
  - **Что сделать:** примирить пути (`GEODATA_ASSETS_DIR=/var/lib/marzban/assets`); либо сделать assets-mount RO и обновлять geodata привилегированным sidecar; для template-директории — staging path + privileged hook.
- [ ] **SEC-C3.** Geodata: `_fetch_remote_sha256` молча возвращает `None` при ошибке, и проверка `if remote_sha256 and downloaded_sha256 != remote_sha256` обходится — атакующий, который ломает доступ к `*.sha256sum` URL, может подсунуть произвольный `geoip.dat`/`geosite.dat`, после чего Marzban рестартует.
  - **Файлы:** `app/services/geodata_updater.py:421-435,350-357`
  - **Что сделать:** отсутствие/неразбираемость удалённой контрольной суммы — hard error; либо пиновать релиз (release tag) и зашивать хэш в образ.

## HIGH (P1)

- [x] **SEC-H1.** Web-admin: HTTP Basic + plaintext `secrets.compare_digest` без хэширования, без rate-limit, без lockout; дефолтный логин/пароль `admin/admin` (`config.py:122-123`, `main.py:42-43`). Закрыто 2026-05-07: `app/services/web_admin_auth.py` (Argon2id verify с graceful fallback на plaintext-сравнение для обратной совместимости в dev), per-IP token bucket в `require_web_admin` (10 fails / 5 min → 429 + Retry-After), `validate_password_strength` отбрасывает старт если plaintext < 14 символов или из blacklist (`admin`, `password`, `qwerty`...). Argon2 хэш генерится через `python -c "from app.services.web_admin_auth import hash_password; print(hash_password('YOUR_PASSWORD'))"`. Session cookie + TOTP — отдельная задача.
- [ ] **SEC-H2.** SSRF: geodata URL допускает любой `http(s)`, Marzban httpx-клиент с `follow_redirects=True` без ограничения хоста.
  - **Файлы:** `app/services/geodata_updater.py:535-543`, `app/services/marzban.py:183-187`
  - **Что сделать:** переиспользовать существующий `_validate_public_https_base_url` (отсев приватных/loopback/link-local); `follow_redirects=False` или whitelist хоста.
- [x] **SEC-H3.** Платега-callback: `getattr(settings, 'platega_secret_value', None) or getattr(settings, 'platega_secret', None)` — если первый `None`, во второй ветке `str(SecretStr_obj)` вернёт `'**********'`, и сравнение секретов превращается в `compare_digest('**********', X-Secret)`. Закрыто 2026-05-07 (commit `0d39f99`).
  - **Файлы:** `app/webhooks.py:218-232`, `app/services/payment_polling.py:80`, `app/services/payments/platega.py:124-125`
  - **Что сделать:** убрать fallback, fail-closed (503), использовать только `settings.platega_secret_value`.
- [x] **SEC-H4.** Self-XSS / HTML-injection в админ-странице инвойса: HTML собирался f-строками, `html.escape` руками — паттерн хрупкий, отсутствует CSP. Закрыто инкрементально:
  - 2026-05-07: добавлены security-заголовки (CSP с `'unsafe-inline'` style/script, X-Frame-Options: DENY, X-Content-Type-Options: nosniff, Referrer-Policy: same-origin, HSTS на HTTPS).
  - 2026-05-08 (ч.1): invoice-страницы давно были на Jinja (`admin_invoice_detail.html`, `admin_invoices.html`); удалён dead-code f-string `_invoice_detail_html`/`_invoice_list_html`/`_invoice_history_pretty_rows`/`_invoice_pretty_json` из `app/web/routes.py` (-130 строк). Inline `<style>`/`<script>` из `base.html` и `admin_broadcasts.html` вынесены в `/static/admin.css` + `/static/admin.js` + `/static/admin_broadcasts.js`; 8 inline `onsubmit="return confirm(...)"` → `data-confirm` + delegated listener. CSP `script-src` сужен до `'self' https://cdn.tailwindcss.com` (без `'unsafe-inline'`).
  - 2026-05-08 (ч.2): Tailwind собран статически через standalone CLI (`pytailwindcss`) в `/static/tailwind.css` (42 КБ, tree-shake по `@source`); CDN убран из `base.html`. Marzban preview error-page вынесен в `.preview-error-page` класс в `admin.css`. Полное strict CSP: `script-src 'self'; style-src 'self'`. Единственное исключение — `/admin/marzban-page/preview` рендерит публичный Marzban-template (внешний контент с inline-style/script + Tailwind CDN); этот один роут переопределяет CSP-заголовок на relaxed (`_MARZBAN_PREVIEW_CSP`).
  - Build instructions: `app/static/src/README.md`.
- [x] **SEC-H5.** Admin-роль проверяется в каждом хэндлере вручную (18 повторений), при ошибке загрузки `AppSettings` в `_load_admin_ids` исключение глотается без алерта. Закрыто 2026-05-08: введён `IsAdminFilter` (`app/filters/admin.py`) на основе `aiogram.filters.BaseFilter`, применён router-level (`router.message.filter`, `router.callback_query.filter`) в `admin_panel.py`. Удалены `_is_admin_tg`/`_load_admin_ids` и 18 ручных проверок. На degraded path (ошибка чтения AppSettings) — `logger.exception` вместо silent swallow, fallback на env `admin_ids`. Redis-кэш admin_ids — отдельная задача (low-priority perf).

## MEDIUM (P2)

- [ ] **SEC-M1.** Subprocess исполнение `marzban_restart_command` / `xray_test_command` из настроек — сегодня env-only; запретить редактирование через web UI (allow-list команд).
- [ ] **SEC-M2.** CSRF cookie `secure` вычисляется из `request.url.scheme`; за TLS-терминирующим nginx даёт non-Secure. Доверять `X-Forwarded-Proto` от перечисленных прокси. CSRF-токен не ротируется при смене сессии.
- [ ] **SEC-M3.** Документировать двухсерверную архитектуру (FastAPI admin vs aiohttp public webhooks) комментарием в `app/web/app.py`, чтобы случайно не подсадить публичный роут под IP-allowlist.
- [x] **SEC-M4.** Логирование полного payload Платеги (с amount, transactionId, может включать PII) в `bot.log` — снизить до DEBUG, оставить в WARN только хэш/префикс tx-id и `normalized_status`.
  - **Файл:** `app/webhooks.py:242,262-268`, аналогично `app/services/marzban.py:536`
  - Частично закрыто 2026-05-07: в `app/webhooks.py` добавлен `_sanitize_tx_ids` (первые 8 символов + ellipsis), полные payload'ы Платеги переведены с WARN на DEBUG, на WARN/INFO/EXCEPTION — только sanitized tx-id префиксы + `normalized_status` + `raw_status`.
  - Закрыто 2026-05-08: `app/services/marzban.py:562` (malformed node payload) — на WARN теперь только `node_hint` (id/uuid/name из payload, без хоста/портов/api_url); полный payload — только на DEBUG.
- [ ] **SEC-M5.** Marzban-auth ошибка содержит `response.text` — может утекать в Sentry.
  - **Файл:** `app/services/marzban.py:289-291`
- [ ] **SEC-M6.** `MarzbanClient._request` ретраит POST/PUT на сетевых таймаутах → возможны дубли (mit `UserAlreadyExistsError`, но всё равно опасно). Ретраить только GET, либо использовать idempotency-ключ.
- [ ] **SEC-M7.** `Settings.platega_callback_url` использует nested-quote f-string (`config.py:361`), валидно только Python ≥3.12. Заменить, чтобы не быть upgrade-trap.
- [ ] **SEC-M8.** Порядок middleware: `BlockedUserMiddleware` бьёт DB до антиспама. Заведённый user-banlist в Redis-кэше + ранний rejection до открытия DB-сессии.

## LOW / INFO (P3)

- [x] **SEC-L1.** Удалить `curl` из runtime-образа (используется только в healthcheck) — заменить на python-based check (`Dockerfile:26`). Закрыто 2026-05-09: `curl` удалён из `Dockerfile`, healthcheck в `docker-compose.yml` переписан на `python -c "urllib.request.urlopen(/readyz)"`.
- [ ] **SEC-L2.** Нет lockfile (`requirements.txt` `>=` only) — supply-chain risk. Перейти на `pip-tools` / `uv pip compile` (см. также QC-9.1 в [CODE_QUALITY.md](CODE_QUALITY.md)).
- [x] **SEC-L3.** Удалить мёртвый импорт `import subprocess` в `app/web/routes.py:16`. Закрыто 2026-05-07: top-level `subprocess` использовался только через `asyncio.subprocess.PIPE` (а тот доступен через сам `asyncio`-пакет).
- [ ] **SEC-L4.** Без CSP / `X-Frame-Options` / `X-Content-Type-Options` на admin-страницах.
- [ ] **SEC-L5.** `audit.log` хранит 30 дней админских действий с деталями инвойсов — убедиться в шифровании диска.
- [x] **SEC-L6.** `_prune_backups` использует glob по `path.name`. Закрыто 2026-05-09: добавлен `glob.escape(path.name)` в `app/services/geodata_updater.py:_prune_backups` — defence-in-depth даже если конфиг геодаты позже расширят произвольными путями.
- [ ] **SEC-L7.** `marzban_env_manager` корректно использует allowlist + readonly + atomic write — оставлено как образец, но проверить, что admin не имеет UI-расширения allowlist (model validator уже запрещает overlap).