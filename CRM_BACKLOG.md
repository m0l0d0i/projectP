# CRM Plus — backlog

> 36 идей для превращения admin-панели в полноценную CRM-платформу.
> Email-интеграция (CRM-P23) исключена — email не будет присутствовать в боте.
> Подключайте этот документ только когда работаете именно над CRM-задачами.
>
> Топ-волну (Sprint 4.5) см. в [SPRINT_PLAN.md](SPRINT_PLAN.md).

---

## G1. Коммуникации и командная работа

- [ ] **CRM-P01 (S, P2).** DM-шаблоны: библиотека именованных шаблонов сообщений (`dm_templates` table) для единичных и массовых рассылок из `/admin/users/{id}/dm` и broadcast-страницы.
- [ ] **CRM-P02 (S, P2).** Внутренние комментарии: `crm_comments` (user_id / ticket_id, admin_author, text, created_at) — видны только команде, не попадают в Telegram-диалог с клиентом.
- [ ] **CRM-P03 (S, P3).** @mentions в комментариях: `@login` → уведомление упомянутому админу в Telegram. Требует `admin_users` из RBAC (Sprint 3).
- [ ] **CRM-P04 (S, P3).** Watchers: подписка сотрудника на события конкретного пользователя/тикета (`crm_watchers` table) → digest-уведомление.
- [ ] **CRM-P05 (M, P3).** Глобальный activity feed команды: Redis Pub/Sub + SSE или polling; последние N событий по всем клиентам на дашборде.

## G2. Жизненный цикл и автоматизация

- [ ] **CRM-P06 (M, P2).** Lifecycle stages: `User.lifecycle_stage` (`lead/trial/active/churned/win_back`); история переходов в `AuditLog`; фильтр в списке.
- [ ] **CRM-P07 (M, P2).** Health score: формула на основе активности, трафика, поддержки, чарджбэков. Денорм-поле `User.health_score INT` обновляется APScheduler-задачей.
- [ ] **CRM-P08 (L, P3).** Workflow automation builder: if-this-then-that правила (напр. `health_score < 30 → создать задачу support`). Конфиг в `crm_workflows`, интерпретируется scheduler'ом.
- [ ] **CRM-P09 (S, P2).** Tasks & follow-ups: `crm_tasks (id, user_id, assignee_admin_tg_id, title, due_at, done_at)`; `/admin/tasks/`; reminder через бот.
- [ ] **CRM-P10 (M, P3).** NPS/CSAT опросы: inline-опрос в боте через `notification_rules` (code `nps_survey`); результаты в `crm_survey_responses`; агрегат на дашборде.

## G3. Bulk-операции и saved views

- [ ] **CRM-P11 (M, P2).** Bulk actions: массовые операции (бан/разбан, смена тарифа, отправка DM, добавление тега) на `/admin/users/` через HTMX-форму с checkbox-выбором + CSRF.
- [ ] **CRM-P12 (S, P2).** Saved filters / smart lists: сохранение фильтров как сегментов (`saved_filters` table); переиспользование в broadcast'ах.
- [ ] **CRM-P13 (M, P3).** CSV import: загрузка пользователей/подписок из CSV для миграции; preview 10 строк, dry-run validation, реальный import.
- [ ] **CRM-P14 (S, P2).** Recently viewed + Pinned: 10 последних карточек (localStorage) и ≤5 закреплённых (`admin_preferences`).

## G4. Финансовые операции

- [ ] **CRM-P15 (S, P2).** Refund workflow: кнопка «Вернуть средства» на Invoice → обязательный комментарий → `Transaction(type=refund)` + `AuditLog`. Баланс корректируется явно (Platega не поддерживает refund API).
- [ ] **CRM-P16 (S, P2).** Chargeback tracking: `Invoice.chargeback_status (none/open/won/lost)` + `chargeback_notes TEXT`; фильтр и счётчик на дашборде.
- [ ] **CRM-P17 (M, P2).** Финансовые периодические отчёты: APScheduler генерирует CSV (выручка, возвраты, новые клиенты, churn) за день/неделю/месяц → `report_exports` + отправка в `support_chat_id`.
- [ ] **CRM-P18 (S, P2).** Promo-code performance report: `/admin/promo-codes/{id}/stats` — конверсия, уникальные пользователи, выручка по промокоду.

## G5. Self-service портал

- [ ] **CRM-P19 (M, P3).** Help Center / FAQ: `faq_articles (slug, title, body_md, is_published, sort_order)`; в боте `/help` или инлайн-кнопка; рендер Markdown → HTML; редактор в `/admin/faq/`.
- [ ] **CRM-P20 (M, P2).** AI первая линия: LLM-бот отвечает на вопрос до создания тикета; при низкой уверенности — передаёт живому агенту. Опирается на `FEA-C32` + FAQ-корпус.
- [ ] **CRM-P21 (S, P3).** Suggested ticket merge: при создании тикета LLM/TF-IDF ищет похожие открытые → предлагает объединить; `Ticket.merged_into_id`.

## G6. Интеграции (без email)

- [ ] **CRM-P22 (S, P2).** Slack/Discord webhooks: при ключевых событиях (`node_down`, `new_chargeback`, `churned_user`, `new_payment`) POST на настраиваемый URL с HMAC; конфиг в `/admin/integrations/`.
- [ ] **CRM-P24 (S, P3).** Auto-post в Telegram-канал: при событии (новый тариф, акция, ТО) публикация в `channel_id`; шаблон в `notification_rules`.
- [ ] **CRM-P25 (M, P3).** Inbound webhooks: приём событий от внешних систем с верификацией HMAC; сохранение в `inbound_webhook_log`; триггер `crm_workflows`.

## G7. Безопасность администраторов

- [ ] **CRM-P26 (S, P1).** 2FA TOTP для web-admin: TOTP поверх Basic Auth (`pyotp`); QR-код при первом входе; backup-коды; расширяет SEC-H1. `admin_users.totp_secret` зашифрован Fernet.
- [ ] **CRM-P27 (S, P1).** Permission audit log: каждое admin-действие в `admin_permission_log (admin_tg_id, role, action, entity, entity_id, ip, user_agent, ts)`; список в `/admin/audit/`.
- [ ] **CRM-P28 (S, P2).** Sensitive data masking: роль `support` видит tg_id/токены/ключи в маскированном виде (`123***789`); Jinja2-фильтр + middleware role-check. Расширяет RBAC (Sprint 3).
- [ ] **CRM-P29 (M, P3).** Session management: `admin_sessions (token_hash, admin_tg_id, created_at, last_seen_at, ip, user_agent)`; `/admin/sessions/` со списком и кнопкой «Завершить все».

## G8. Reporting & BI

- [ ] **CRM-P30 (M, P2).** Saved reports + scheduled exports: сохранить параметры report-запроса → авто-генерация CSV по cron-расписанию → доставка в Telegram / webhook из `CRM-P22`.
- [ ] **CRM-P31 (L, P3).** Cohort heatmap: retention по cohort (регистрация → оплаты) в виде тепловой карты (HTML-таблица с CSS-градиентом); обновляется ежедневно.
- [ ] **CRM-P32 (S, P2).** Drill-down из KPI-плиток: клик на число → модал/переход на преднастроенный фильтр в users/invoices/subscriptions.

## G9. API и расширяемость

- [ ] **CRM-P33 (L, P2).** Public REST API + JWT/API-key auth: `/api/v1/` в FastAPI; `Bearer` JWT или статический API-key (`api_keys` table); read-доступ к users/subscriptions/invoices/nodes — с RBAC.
- [ ] **CRM-P34 (S, P2).** Swagger/OpenAPI документация: авто-генерация из FastAPI-аннотаций (`/api/docs`); только для залогиненных.
- [ ] **CRM-P35 (M, P3).** Webhook subscriptions UI: внешние системы регистрируют endpoint + список событий через `/admin/webhooks/outbound/`; доставка через `app/services/webhooks_out.py` (`FEA-C37`).

## G10. Mobile-friendly admin

- [ ] **CRM-P36 (M, P2).** PWA/responsive admin: адаптивная вёрстка (Bootstrap 5 / Tailwind); `manifest.json` + service worker для offline-shell; viewport meta.
- [ ] **CRM-P37 (S, P2).** Telegram bot admin-команды: `/stats` (DAU/MRR/активные подписки), `/user <tg_id>`, `/block <tg_id>` (с подтверждением), `/node` — для admin_ids, в `app/handlers/admin_commands.py`.