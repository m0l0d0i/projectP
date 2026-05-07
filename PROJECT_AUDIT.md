# SwoiVPN — Аудит проекта (индекс)

> Документ создан на ветке `claude/analyze-project-features-HGco2`.
> Содержание разнесено по тематическим файлам — этот документ является только индексом и хранит инвентаризацию + журнал изменений.

## Куда смотреть

| Файл | Что внутри |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Продуктовые решения D1–D12 + список отменённых пунктов |
| [SECURITY.md](SECURITY.md) | SEC-* — критические/high/medium/low security findings |
| [CODE_QUALITY.md](CODE_QUALITY.md) | QC-*, OPS-*, CMP-* + Quick wins + DoD аудита |
| [ROADMAP.md](ROADMAP.md) | FEA-* roadmap (разделы A–F) + Топ-8 идей |
| [SPRINT_PLAN.md](SPRINT_PLAN.md) | Sprint 0–7 + 3.5/4.5 (только ID, детали — в исходных задачах) |
| [CRM_BACKLOG.md](CRM_BACKLOG.md) | CRM-P01–P37 (без P23 email) — расширенный CRM backlog |

> **Легенда статусов:** `[ ]` не начато · `[~]` в работе · `[x]` сделано · `[!]` отложено / неактуально
>
> **Приоритеты:** `P0` немедленно (продакшн риск) · `P1` текущий спринт · `P2` 1–2 месяца · `P3` backlog

---

## Краткая инвентаризация

| Параметр | Значение |
|---|---|
| Стек | Python 3.12 · aiogram 3 · FastAPI · aiohttp · SQLAlchemy 2 (async) + asyncpg · Postgres 16 · Redis 7 · APScheduler · Alembic |
| Интеграции | Marzban (Xray) · Platega · Sentry · Prometheus |
| Файлов Python | 93 (`~21k LOC`) |
| Крупнейшие файлы | `app/web/routes.py` 5279 · `app/db/repositories/__init__.py` 2921 · `app/services/marzban.py` 1282 · `app/db/models.py` 1266 · `app/handlers/admin_panel.py` 1236 · `app/services/subscriptions.py` 1181 · `app/handlers/support.py` 1137 · `app/services/payment_engine.py` 1097 |
| Миграций | 21 за 16 дней (включая 4 «rework/repair») |
| Тесты | **отсутствуют полностью** |
| Документация | нет `README`, `LICENSE`, `CONTRIBUTING`, `CHANGELOG` |
| CI/CD | нет (`.github/workflows`, `.gitlab-ci.yml` отсутствуют) |
| Линтеры/типизация | нет `pyproject.toml`, `mypy.ini`, `ruff.toml`, `pre-commit` |
| Git история | 2 коммита `Initial commit` — нормальной истории нет |

---

## Журнал изменений

| Дата | Кто | Что | Ссылка / коммит |
|---|---|---|---|
| 2026-05-06 | initial audit | Создан `PROJECT_AUDIT.md` (security + code-quality + roadmap) | commit `2cd0220` |
| 2026-05-06 | product review | Решения D1–D9: только Platega, нет авто-продления, нет multi-tier affiliate, упрощённый бонус, апсейлы improve, smart push с админ-UI, AI-саппорт через DeepSeek+pluggable LLM, нет A/B, провижниг нод — только подготовка | commit `2cd0220` |
| 2026-05-06 | admin/CRM expansion | Решения D10–D12: расширенный CRUD по всем сущностям, спец-тарифы, полная CRM по клиентам и live-статистика нод. Добавлен раздел F и Sprint 3.5 | commit `2cd0220` |
| 2026-05-07 | CRM Plus expansion | Добавлен раздел G «CRM Plus» (36 идей в 10 категориях), Sprint 4.5 | commit `89bcff2` |
| 2026-05-07 | split into multiple files | Разбит на DECISIONS/SECURITY/CODE_QUALITY/ROADMAP/SPRINT_PLAN/CRM_BACKLOG; убраны дублирования (file-map, code snippets, описания отменённых задач); Sprint Plan свёрнут до ID | commit `0de968b` |
| 2026-05-07 | Sprint 0 code tasks | Закрыты 8 локальных задач: QC-dockerignore (`bee6364`), QC-config-default-pwd (`306fd5f`), SEC-H3 (`0d39f99`), QC-FSM-storage (`368e4ee`), OPS-1/FEA-D40 (`76db305`), QC-9.1+QC-9.2 (`7a29e93`), QC-4.1 (`16797e0`), QC-10.1 (`fdb348b`). Остаются на стороне пользователя: P0 секреты (SEC-C1/C2/C3), git filter-repo, ротация продовых секретов | (этот коммит) |
| 2026-05-07 | Sprint 0 закоммичен в main | Sprint 0 переоформлен в 8 атомарных коммитов: `6512a30` (audit split), `d2304ac` (QC-dockerignore), `66253ad` (QC-config-default-pwd), `4f57888` (SEC-H3), `6d926de` (QC-FSM-storage), `d579565` (OPS-1/FEA-D40), `74d55c0` (QC-9.1+9.2), `2161fd5` (QC-4.1). Также добавлен `.gitignore` (`fbbf7b0`). | (эти коммиты) |
| 2026-05-07 | Sprint 1 start: OPS-5 | OPS-5 / FEA-D44 — invoice idempotency keys. Колонка `Invoice.idempotency_key` + partial unique `WHERE idempotency_key IS NOT NULL`, ключ = SHA-256 от (tg_id, purpose, code, units, extras, bucket60s), `DuplicateInvoiceError` → понятное сообщение в 3 хендлерах `purchase.py`. | commit `9cfba1a` |

> При закрытии пункта: ставьте `[x]` в соответствующем файле, добавляйте строку сюда с коммитом/PR.