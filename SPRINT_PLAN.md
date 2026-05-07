# Sprint Plan — порядок реализации

> Терсе: каждый спринт — список ID и DoD. Детали задач смотрите в:
> [SECURITY.md](SECURITY.md), [CODE_QUALITY.md](CODE_QUALITY.md),
> [ROADMAP.md](ROADMAP.md), [CRM_BACKLOG.md](CRM_BACKLOG.md).
>
> Между спринтами порядок строгий (последующий зависит от предыдущего).
> Внутри спринта — параллельно. Каждый спринт ≈ 1–2 недели на разработчика.

---

## Sprint 0 — Hygiene & Safety (P0/P1) · ~3–5 дней

**ID:** SEC-C1, SEC-C2, SEC-C3, QC-config-default-pwd, SEC-H3, QC-9.1, QC-10.1, QC-4.1, QC-FSM-storage, QC-dockerignore, OPS-1 / FEA-D40

**DoD:** CI зелёный, секреты ротированы, pre-commit gate активен, FSM на Redis, health-эндпоинты доступны.

---

## Sprint 1 — Reliability core (P1) · ~1 неделя

**ID:** OPS-5 / FEA-D44, OPS-4 / FEA-D43, OPS-3 / FEA-D42, OPS-2 / FEA-D41, QC-13.1, QC-7.2, SEC-H1, SEC-H5, SEC-H4

**DoD:** двойной Платега-callback не создаёт дубль-инвойс; «оплатил-ничего-не-пришло» отлажен; Marzban-out не валит весь сервис; admin-страницы с CSP.

---

## Sprint 2 — Notification framework + Upsell (D5, D6) · ~1.5 недели

**ID:** FEA-NOTIF (backend + admin-UI), FEA-A8, FEA-A9

**DoD:** админ может включить/выключить любой push-сценарий и отредактировать текст/кнопки; пользователь видит +50/+100/+200/+500 ГБ варианты; можно добавить устройство к активной подписке без переоформления тарифа.

---

## Sprint 3 — AI Support + RBAC + Affiliate (D3, D4, D7) · ~1.5 недели

**ID:** FEA-C39, FEA-C31, FEA-C32, FEA-A6

**DoD:** support-оператор открывает тикет, нажимает «Сгенерировать», получает черновик от DeepSeek, правит и отправляет; разные роли видят разные разделы админки; админ в один клик меняет реферальный бонус.

---

## Sprint 3.5 — Admin / CRM expansion (D10–D12) · ~2 недели

**ID:** FEA-ADMIN-USER-CRM, FEA-ADMIN-SUB-CRM, FEA-ADMIN-TARIFF-PLUS, FEA-ADMIN-NODE-MONITOR, FEA-ADMIN-CRUD-EXPAND

**DoD:** админ может полностью управлять клиентом без SQL-консоли (заметки/теги/блок/выдача/DM/timeline); видит per-node latency и активные сессии; можно создать спец-тариф под промокод или закрытый сегмент.

---

## Sprint 4 — UX foundation: i18n + Mini App · ~2 недели

**ID:** FEA-B20, FEA-B17, FEA-B18

**DoD:** EN-пользователь видит интерфейс на английском; есть рабочий Mini App с основными карточками.

---

## Sprint 4.5 — CRM Plus core · ~2 недели

**ID:** CRM-P26, CRM-P27, CRM-P28, CRM-P01, CRM-P02, CRM-P09, CRM-P11, CRM-P12, CRM-P14, CRM-P15, CRM-P16, CRM-P17, CRM-P32, CRM-P33, CRM-P34, CRM-P36, CRM-P37

**DoD:** администраторы работают командой (комментарии, задачи, DM-шаблоны); bulk-операции работают; refund/chargeback в AuditLog; web-admin защищён 2FA и маскирует чувствительные данные для `support`; REST API с Swagger доступен; admin-UI корректно отображается на телефоне.

---

## Sprint 5 — Analytics + Compliance + Operator quality · ~1.5 недели

**ID:** FEA-ADMIN-DASHBOARD, FEA-C30, CMP-1, CMP-2, CMP-4, FEA-C33, OPS-6 / FEA-D45, OPS-7 / FEA-D46, QC-1.1, QC-1.2

**DoD:** аналитический дашборд работает; пользователь может экспортировать/удалить свои данные; `routes.py` < 800 LOC; public status page показывает реальный uptime.

---

## Sprint 6 — Tests catch-up · параллельно (с Sprint 1)

**ID:** QC-3.1

> Не отдельный спринт, а постоянная активность с Sprint 1+. Каждая фича закрывается с unit/integration-тестом.
> К концу Sprint 5 цель: критичные пути (платёж idempotency, notifications, RBAC, referral bonus, upsell) ≥ 70% line coverage.

---

## Sprint 7 — Backlog (P2/P3, по приоритету бизнеса)

**ID:** FEA-B19, FEA-B22, FEA-B23, FEA-A11, FEA-A13, FEA-A14, FEA-A15, FEA-A7, FEA-C34, FEA-C37, FEA-C35 (полная реализация после выбора провайдера), FEA-A10, FEA-B21, FEA-B24, FEA-B27, OPS-8, CMP-3

**Также CRM Plus вторая волна:** CRM-P03, P04, P05, P06, P07, P08, P10, P13, P18, P19, P20, P21, P22, P24, P25, P29, P30, P31, P35

---

## Принципы, которым следуем все спринты

1. Каждый спринт начинается с миграции (если она нужна) → код → тесты → CI зелёный → merge.
2. Никаких удалений данных без backup. Все data-rewrite миграции — с явным docstring «downgrade irrecoverable».
3. Каждая новая фича в админке — закрывается RBAC из Sprint 3 (даже если временно `superadmin`-only).
4. Каждое новое pushable-уведомление — обязательно через `NotificationDispatcher` (Sprint 2), не через прямой `bot.send_message`.
5. Каждый новый Marzban-зависимый код — через circuit breaker из Sprint 1.
6. Каждый payment touch-point — обязателен `idempotency_key` из Sprint 1.