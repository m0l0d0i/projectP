# Качество кода, надёжность и compliance

> Code-quality + Reliability/Ops + Compliance (бывшие разделы 2/3/4 из PROJECT_AUDIT.md).
> Чек-боксы: `[ ]` не начато · `[~]` в работе · `[x]` сделано · `[!]` отложено.
>
> См. также: [SECURITY.md](SECURITY.md), [SPRINT_PLAN.md](SPRINT_PLAN.md).

## Качество кода и архитектура

### HIGH (P1)

- [ ] **QC-1.1.** **God-module `app/web/routes.py` (5279 LOC, 48 роутов, 102 хелпера).** Внутри HTTP-роуты, subprocess-pipeline для Marzban deploy (4408–4441), file-IO + rollback, CSV-экспорт, бизнес-хелперы.
  - **Что сделать:** разнести в `app/web/admin/{users,pricing,tariffs,tickets,broadcasts,marzban_page,nodes,routing_profiles,invoices,export_csv}.py` + `app/web/public.py`; вынести fs/restart-логику в `app/services/marzban_apply.py`. Оставить `routes.py` тонким aggregator-ом.
- [ ] **QC-1.2.** **Все 17 репозиториев в одном `__init__.py` (2921 LOC).** Соседний `node_registry.py` уже показывает целевой паттерн.
  - **Что сделать:** один файл на репозиторий (`users.py`, `subscriptions.py`, `tariffs.py`, …); `__init__.py` оставить агрегатором.
- [ ] **QC-3.1.** **Тесты отсутствуют полностью.** Нет ни `tests/`, ни `conftest.py`, ни pytest в зависимостях. `aiosqlite` в requirements есть, но не используется.
  - **Highest-value tests первой очереди:**
    1. Идемпотентность `PaymentService.process_provider_callback` для повторных Platega-callback'ов.
    2. `notifications.check_expiring/check_low_traffic/check_traffic_exhaustion` (флаги `notified_*`).
    3. Round-trip миграций (`alembic upgrade head && downgrade base`) против `docker-compose.test.yml`.
    4. CSRF middleware (хитрая ребиндка `request._receive`).
    5. Антиспам middleware (можно тестить с fakeredis).
    6. `SubscriptionService` цикловая математика (`effective_cycle_total_bytes`, `ResetTrafficQuote`).
    7. `TariffRepository` после rework-миграции `20260410_000019`.
  - **Что сделать:** добавить `pytest` + `pytest-asyncio` + `pytest-cov`, тестировать против контейнерного Postgres (sqlite не подойдёт из-за JSON/ENUM/with_for_update/ON CONFLICT).
- [ ] **QC-5.1.** **102 широких `except`/`with suppress(Exception)`.** Hot-spots: `app/main.py` shutdown (9 подряд), `app/web/routes.py` (≥38), `app/services/cache.py` (7), `app/handlers/admin_panel.py` (6), `app/middlewares/blocked.py` (3 — глотает Redis-сбой и сваливается на Postgres без rate-limit).
  - **Что сделать:** заменить на узкие исключения (`RedisError`, `MarzbanAPIError`, `httpx.HTTPError`, `IntegrityError`); общий `safe_close(coro)` хелпер для shutdown вместо 9 inline-suppress.
- [ ] **QC-7.2.** **N+1 в `app/services/notifications.py:377`** — `for ticket in tickets_to_close: user = await user_repo.get_by_id(ticket.user_id)`. Добавить `.options(selectinload(SupportTicket.user))` в `due_auto_close` или batched `users_by_id(ids)`.
- [x] **QC-9.1.** **Нет lockfile.** Все зависимости с `>=` — рандомные апдейты при rebuild. Перейти на `pip-tools`/`uv` с `requirements.in` + pinned `requirements.txt`. Закрыто 2026-05-07 (commit `7a29e93`).
- [x] **QC-10.1.** **CI/CD отсутствует.** Добавить минимальный workflow: `ruff check` → `mypy` → `pytest` против test-compose → `alembic upgrade head` → `docker build`. Блокировать merge без зелёного pipeline. Закрыто 2026-05-07 (commit `fdb348b`).
- [ ] **QC-13.1.** **`audit_logs` без индекса по `created_at`.** `AuditLogRepository.list_recent` (`__init__.py:2784`) делает `ORDER BY created_at DESC, id DESC LIMIT N OFFSET M` — sequential scan при росте таблицы.
  - **Что сделать:** миграция, добавляющая `Index('ix_audit_logs_created_at_id', created_at.desc(), id.desc())`.
- [x] **QC-config-default-pwd.** **Удалить дефолт `web_admin_password='admin'`** из `app/config.py:122-123`. Сделать поле обязательным; pydantic свалится при отсутствии env. Так же для `web_admin_username`. Закрыто 2026-05-07 (commit `306fd5f`).

### MEDIUM (P2)

- [ ] **QC-1.3.** Разделить `marzban.py` (`client.py` / `models.py` / `xray_apply.py` / `node_sync.py`); разделить `support.py` на `SupportTicketService` + handler-роутеры (≤500 LOC на файл).
- [ ] **QC-2.2.** Бизнес/инфраструктурная логика (subprocess, file IO, backup) вынесена прямо в HTTP-handler `routes.py`. Перенести в сервис; route ≈ 20 строк (parse → service → redirect).
- [ ] **QC-2.3.** Репозитории несут валидацию + аудит (`AppSettingsRepository.update_*` зажимает значения, `__init__.py:451–540`). Перенести правила в сервис/pydantic-модели.
- [x] **QC-4.1.** Нет `pyproject.toml`, `mypy.ini`, `.flake8`, `ruff.toml`, `pre-commit`. Добавить `pyproject.toml` с `[tool.ruff]` (`E,F,I,B,UP,SIM,TCH,RUF`), `[tool.mypy]` (strict_optional, warn_unused_ignores), `pre-commit` хуки (ruff, ruff-format, eof-fixer, trailing-whitespace, gitleaks). Закрыто 2026-05-07 (commit `16797e0`).
- [ ] **QC-5.2.** `# pragma: no cover - runtime safety` на критичных rollback-блоках в `routes.py` — code-smell. Превратить в тестируемые сервисные методы с узкими исключениями.
- [ ] **QC-5.3.** `notifications.py:127` глотает `MarzbanAPIError` per-target → если Marzban лежит часами, scheduler-задание тихо падает. Добавить counter в Prometheus + summary log + circuit-breaker.
- [ ] **QC-6.3.** `notifications.py:138` — последовательный `await marzban.get_user(...)` per target. Параллелить через `asyncio.gather` + semaphore (паттерн уже есть в `broadcast_polling.py:520`).
- [ ] **QC-6.4.** `BlockedUserMiddleware` делает 1 SQL + 2 Redis на каждое событие (`AppSettings.get` для admin_ids). Кэшировать `AppSettings.get` per `anti_spam_settings_cache_ttl_seconds`.
- [ ] **QC-7.1.** В FastAPI-роутах handler открывает несколько независимых сессий (`_notify_ticket_closed_from_web_admin` в `routes.py:200` — две). Сделать `Depends(get_session)` per HTTP request с явным commit/rollback.
- [ ] **QC-7.3.** Sparse eager-loading: 16 `selectinload`/`joinedload` на 27 relationship'ов. Аудит `.subscriptions/.invoices/.deliveries/.tickets` доступов вне репозиториев — добавлять `selectinload`.
- [ ] **QC-8.1.** **101 поле в `Settings`, 158 env-переменных в `.env.example`.** Разнести через `env_nested_delimiter`: `MarzbanSettings`, `PaymentSettings`, `BroadcastSettings`, `AntiSpamSettings`, `WebAdminSettings`, `GeodataSettings`. Доступ как `settings.marzban.*`.
- [ ] **QC-8.3.** `trial_duration_hours` и `trial_duration_days` сосуществуют, читается только первый, делится на 24. Удалить `TRIAL_DURATION_HOURS`.
- [ ] **QC-11.1.** Нет `README`, `CONTRIBUTING`, `CHANGELOG`, `LICENSE`. Минимум: README с архитектурой, "как запустить локально", "как добавить миграцию"; LICENSE — юридическая необходимость.
- [ ] **QC-11.2.** Нет module-level docstrings. Добавить 3–5 строк в каждый сервисный модуль (вход/выход/транзакционные границы).
- [ ] **QC-12.1.** В 16 дней — 4 «rework/repair» миграции; downgrade у `20260410_000017_subscription_url_canonical_repair.py` — no-op. Заморозить дизайн тарифов перед новыми изменениями; пометить data-rewrite миграции как irrecoverable в docstring.
- [ ] **QC-13.3.** **`BroadcastJobStatus.pending = 'scheduled'` (alias на тот же value).** Postgres ENUM не получит лейбл `pending`; `WHERE status = 'pending'` сломается. Проверить всех вызывателей; сделать нормализацию явной (helper `as_db_value`).
- [x] **QC-FSM-storage.** **Aiogram FSM использует дефолтный `MemoryStorage`** (явно `RedisStorage`/`MongoStorage` нигде не настроен). После рестарта FSM-контексты пользователей (середина оплаты, заполнение тикета, админская рассылка) теряются. Закрыто 2026-05-07 (commit `368e4ee`).
  - **Что сделать:** подключить `aiogram.fsm.storage.redis.RedisStorage` поверх существующего Redis (`Dispatcher(storage=...)` в `app/main.py`).

### LOW (P3)

- [ ] **QC-1.3-low.** `app/services/cache.py`, `geodata_updater.py`, `payment_engine.py` крупные, но монолитные внутренне — разнести можно, но не критично.
- [ ] **QC-4.2.** 13 файлов без `from __future__ import annotations`. Добавить через ruff `UP010`/`I001`.
- [ ] **QC-4.3.** Слабая типизация `bot`/`sessionmaker` в `build_scheduler` (`scheduler.py:177`). Аннотировать `aiogram.Bot` и `async_sessionmaker[AsyncSession]`.
- [x] **QC-9.2.** `aiosqlite` в `requirements.txt` не используется — удалить. Закрыто 2026-05-07 (commit `7a29e93`, в составе QC-9.1).
- [ ] **QC-13.4.** Дефолтный `lazy='select'` на всех relationship'ах + sparse eager-loading — точечная оптимизация по горячим путям.
- [x] **QC-dockerignore.** `.dockerignore` содержит только `.env`. Добавить `.git`, `__pycache__`, `*.pyc`, `logs/`, `data/`, `tests/`, `*.md`, `.venv/`, `htmlcov/`, `.pytest_cache/`. Закрыто 2026-05-07 (commit `bee6364`).
- [ ] **QC-git-history.** Только 2 коммита `Initial commit` — нет полезной истории. На будущее: `Conventional Commits`, обязательное тело PR, `git filter-repo` для очистки утечек (см. SEC-C1).

---

## Надёжность / Ops

- [x] **OPS-1 (P1).** Добавить `/healthz` (всегда 200) и `/readyz` (Postgres + Redis + Marzban ping) в `app/web/routes.py`. Сейчас `/readyz` есть только в публичном aiohttp-приложении (`app/webhooks.py:67`), но не в admin FastAPI. Закрыто 2026-05-07 (commit `76db305`, в `app/web/app.py`). Marzban ping в /readyz пока не добавлен — отдельная итерация.
- [ ] **OPS-2 (P1).** Rate-limit на `platega_callback` и Marzban-callback'и (Redis token bucket, у проекта уже есть Redis + `anti_spam.py` шаблон).
- [ ] **OPS-3 (P1).** Circuit breaker над `MarzbanClient` и `PlategaProvider` — `tenacity` + лёгкий wrapper. Предотвращает каскадные отказы.
- [ ] **OPS-4 (P2).** **Outbox pattern** для transactional Telegram-сообщений: после оплаты записывать запись в `outbox` той же транзакцией; воркер доставляет exactly-once. Закрывает класс багов «Я заплатил, ничего не пришло».
- [ ] **OPS-5 (P1).** **Idempotency keys на инвойсах** (`Invoice.idempotency_key UNIQUE`) — `BillingService.create_invoice` обязан принимать ключ. Защита от двойного нажатия.
- [ ] **OPS-6 (P2).** Структурированные JSON-логи + request/update IDs middleware → парность с Sentry.
- [ ] **OPS-7 (P2).** Scheduler job-lag SLO: экспортировать `last_run_timestamp` per job в Prometheus; Alertmanager rule на отставание.
- [ ] **OPS-8 (P3).** Encrypted off-site backups: Postgres dump + Marzban configs + Redis snapshot, AES + S3-compatible upload (`app/services/backup.py` + scheduler job).

---

## Compliance / доверие

- [ ] **CMP-1 (P1).** GDPR-подобный экспорт + удаление профиля. `app/services/privacy.py`: export = JSON User + Subscriptions + Invoices + SupportMessages; erase = anonymize + revoke Marzban + soft-delete tickets. Добавить кнопки в `/profile` и `/admin/users/{id}/erase`.
- [ ] **CMP-2 (P2).** Public status page (`/status`) — поверх node-probe данных (см. FEA-B19) показывать per-node/per-provider uptime 7/30 дней. Снижает support-нагрузку.
- [ ] **CMP-3 (P3).** Prometheus → внутренняя SLA-метрика на admin-дашборде («99.7% за 30 дней»).
- [ ] **CMP-4 (P2).** ToS / Privacy versioning: `User.terms_version_accepted`; на смене версии — re-accept перед следующей оплатой. `rules.py` уже показывает текст — расширить.

---

## Quick wins (≤ 1 час каждый)

1. Заменить дефолтный `web_admin_password='admin'` на `Field(...)` без default → forced env (`config.py:122-123`).
2. Удалить `aiosqlite` из `requirements.txt`.
3. Удалить мёртвый `import subprocess` в `app/web/routes.py:16`.
4. Расширить `.dockerignore`: добавить `.git`, `__pycache__`, `*.pyc`, `logs/`, `data/`, `tests/`, `*.md`, `.venv/`.
5. Заменить `.env.example` на placeholder'ы `CHANGE_ME` + добавить `gitleaks` в pre-commit.
6. Очистить fallback `getattr(settings, 'platega_secret', None)` в `app/webhooks.py:218` — fail-closed.
7. Добавить миграцию с `Index('ix_audit_logs_created_at_id', ...)`.
8. Подключить `RedisStorage` к aiogram FSM в `app/main.py`.
9. Задокументировать в `app/web/app.py` двухсерверную архитектуру (FastAPI admin vs aiohttp public).
10. Добавить `/healthz` и `/readyz` в admin FastAPI (FEA-D40 / OPS-1).

---

## Definition of Done для аудита

- [ ] Все P0 (`SEC-C1`, `SEC-C2`, `SEC-C3`) закрыты, секреты ротированы, история git зачищена.
- [ ] CI зелёный: `ruff check` + `mypy` + `pytest` + `alembic upgrade/downgrade` + `docker build`.
- [ ] Покрытие тестами критичных платёжных и notification-путей ≥ 70%.
- [ ] `app/web/routes.py` ≤ 800 LOC, репозитории — по файлу на класс.
- [ ] `requirements.txt` pinned (lockfile).
- [ ] `README.md` + `LICENSE` присутствуют.
- [ ] Audit-log имеет индекс по `created_at`.
- [ ] FSM на Redis, не MemoryStorage.
- [ ] Web-admin: пароль хэширован, есть rate-limit, нет дефолта `admin/admin`.
- [ ] Outbox + idempotency на инвойсах.
- [ ] Админ может полностью управлять клиентом без SQL-консоли (block/edit/notes/tags/DM/timeline/manual subscription).
- [ ] Админ-страница нод показывает live latency и активные сессии; есть алерт при `node_down`.
- [ ] Возможны спец-тарифы: code-only, segment-only, private-link, time-windowed, с inventory cap.