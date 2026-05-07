# Roadmap — идеи фич

> Полный список с эффортом (S/M/L/XL) и точкой приземления в коде.
> Топ-8 высокорычажных идей выделены в конце файла.
> Отменённые пункты см. в [DECISIONS.md](DECISIONS.md). CRM-расширение (раздел G — 36 идей) перенесено в [CRM_BACKLOG.md](CRM_BACKLOG.md).
>
> Чек-боксы: `[ ]` не начато · `[~]` в работе · `[x]` сделано · `[!]` отложено.

## A. Монетизация и рост

- [ ] **FEA-A6 (S, P1, D3+D4).** Простая партнёрская программа с настраиваемым бонусом.
  - Сейчас бонус захардкожен в `app/services/referrals.py:11` (`Decimal('50.00')`); зачисляется обоим (приглашённому и пригласившему); вывода нет.
  - Перенести `REF_BONUS` в `AppSettings` (`referral_inviter_bonus`, `referral_invited_bonus`); миграция Alembic.
  - Редактор в `/admin/referrals/` (две формы); экран «Мои рефералы» в `profile.py`.
  - Архитектурный инвариант (D3): бонус идёт только на `User.balance`, оттуда — только на оплату инвойса. Withdrawal forbidden.
- [ ] **FEA-A7 (S, P3).** Branded referral landing `/r/<code>` с кастомным title/avatar.
- [ ] **FEA-A8 (M, P1, D5).** Улучшение апсейла трафика. Сейчас `PricingService.TOPUPS` (`tariffs.py:127`) — 2 захардкоженных варианта. Кнопка «Докупить трафик» уже встроена в `low_traffic_alert_keyboard` (`keyboards/inline.py:225`).
  - Перенести `TOPUPS` в DB (новая таблица `traffic_topup_options`). CRUD в `/admin/upsells/traffic/`.
  - 3-й вариант пакета (+200/+500 ГБ); авто-бейдж «лучшая цена за ГБ»; promo-код применим к topup invoice.
- [ ] **FEA-A9 (M, P2, D5).** Mid-cycle апсейл устройств — сейчас не поддерживается. Устройства задаются только при покупке тарифа.
  - Опция «Добавить устройство к текущей подписке» в `purchase.py` / `vpn.py` если `used_device_count < MAX_CUSTOM_DEVICES`.
  - Цена пропорционально оставшимся дням (или фиксированная, конфиг в `AppSettings.mid_cycle_device_price_mode`).
  - Новый `InvoicePurpose.device_topup` (миграция); после оплаты `_consume_paid_invoice` инкрементирует `used_device_count` и зовёт `MarzbanClient.update_user_online_limit`.
  - `/admin/upsells/devices/` — настройка цены/лимита/on-off.
  - Bonus: апгрейд single → unlimited mid-cycle (доплата = разница).
- [ ] **FEA-A10 (M, P2).** Premium / dedicated-IP tier через `RoutingProfile.tier` + `node_policy.py`.
- [ ] **FEA-A11 (M, P2).** Family / group plans — `Subscription.parent_subscription_id`, экран «manage members».
- [ ] **FEA-A12 (L, P3).** Corporate plan — scoped `/biz/` admin для самообслуживания компании.
- [ ] **FEA-A13 (S, P2).** Gift subscriptions — авто-генерируемый `PromoCode` на checkout.
- [ ] **FEA-A14 (M, P2).** Loyalty streak / cashback — `LoyaltyTier` table + cashback на renewal.
- [ ] **FEA-A15 (S, P2).** Scheduled flash sales — `valid_from/valid_to` + auto-apply на `PromoCode`.
- [ ] **FEA-A16 (объединено в `FEA-NOTIF`, см. D6).** Trial-to-paid sequence.

## B. UX

- [ ] **FEA-B17 (L, P1).** Telegram Mini App (TWA) dashboard — React/Vue из `app/web/`, traffic graph, devices, one-click renew, per-server ping.
- [ ] **FEA-B18 (S, P1).** Auto-detect platform → одна кнопка «Set up on this device» с deep-link. Половина уже есть в `vpn.py:device_os_screen`.
- [ ] **FEA-B19 (M, P2).** In-bot speedtest — `app/services/node_probe.py`, периодический пинг, Redis-cache, показ в «Choose server».
- [ ] **FEA-B20 (M, P1).** i18n EN+RU. Сейчас отсутствует. `gettext`, `locale/{en,ru}/LC_MESSAGES/`, детект `from_user.language_code`.
- [ ] **FEA-B21 (S, P2).** Onboarding wizard — 4-шаговый inline-гайд.
- [ ] **FEA-B22 (S, P2).** Universal subscription URL с auto-rotation — Marzban уже умеет, нужен один публичный link.
- [ ] **FEA-B23 (S, P2).** Per-node ping в `services_screen` и в WebApp.
- [ ] **FEA-B24 (S, P3).** Dark/light theme toggle для admin/sub страниц.
- [ ] **FEA-B25/B26 (объединены в `FEA-NOTIF`).** Quick-actions в alert'ах + weekly usage report.
- [ ] **FEA-B27 (S, P3).** Branded QR + `/setup/<key>` HTML-страница с табами по платформам.

## B+. Smart push-уведомления (D6)

- [ ] **FEA-NOTIF (M, P1).** Smart push-уведомления + админ-панель регулирования. Объединяет `FEA-A16/B25/B26` и формализует существующую логику в `app/services/notifications.py` (`check_expiring/check_low_traffic/check_traffic_exhaustion/check_monthly_traffic_reset`).
  - **Анализ существующего:** уже работают флаги `Subscription.notified_3d/notified_1d/notified_low_traffic/notified_exhausted/notified_expired`. Триггеры — APScheduler-задания каждые 6/12 часов.
  - **Backend:**
    - Новая таблица `notification_rules`: `code, is_enabled, template_text, template_keyboard_json, cooldown_seconds, segment_filter_json, priority`. Codes: `expiring_3d/expiring_1d/low_traffic_90/exhausted/trial_mid/trial_last_day/trial_post_expire_rescue/weekly_usage`.
    - Сервис `NotificationDispatcher` (`app/services/notification_dispatcher.py`): рендер шаблона + cooldown через Redis-key `notif:{user_id}:{code}` + `_safe_send`.
    - Рефакторить существующие jobs на dispatcher; fallback на вшитый текст если правила нет.
    - Новые jobs: `trial_mid_reminder` (12ч после старта), `trial_last_day_offer` (за 2ч до окончания), `trial_post_expire_rescue` (24ч после), `weekly_usage_report`.
    - Расширить `low_traffic_alert_keyboard`: «+50 ГБ» / «+100 ГБ» / «Продлить 1 мес» / «Не напоминать 24ч».
  - **Admin UI:**
    - `/admin/notifications/` — список правил, переключатель on/off, счётчик отправок 7/30д.
    - Редактор шаблона + drag-n-drop конструктор кнопок (переиспользовать из `BroadcastService`).
    - Тестовая отправка на admin tg_id.
    - Метрики Prometheus: `vpn_bot_notifications_sent_total{code,status}`, `vpn_bot_notifications_blocked_total{code,reason}`.

## C. Operator features

- [ ] **FEA-C28 (XL, P3).** Multi-tenant / reseller — `tenant_id` на User/Subscription/Invoice/Tariff; роутинг по bot-token.
- [ ] **FEA-C30 (M, P2).** Cohort/retention dashboards — D1/D7/D30, MRR, LTV by source. `/admin/analytics/` + Chart.js.
- [ ] **FEA-C31 (M, P1).** CRM-lite для саппорта (D7 prereq): `tags JSONB`, `assignee_admin_id`, `CannedResponse`. Editor в `/admin/tickets/`. Делается **до** FEA-C32.
- [ ] **FEA-C32 (M, P1, D7).** AI-помощник саппорта с pluggable LLM. Default — DeepSeek; абстракция позволит поменять на любой OpenAI-compatible endpoint.
  - Каркас `app/services/support_ai/{base,deepseek,openai_compat}.py` (фабрика провайдеров + Protocol интерфейс).
  - Конфиг через `AppSettings` или новую таблицу `LLMConfig`: provider, api_base_url, model_name, temperature, system_prompt, encrypted api_key.
  - UI `/admin/support-ai/`: список провайдеров, тест соединения, редактор system-prompt, счётчик токенов.
  - На странице тикета — кнопка «Сгенерировать ответ» → правка → отправить / сохранить как canned response.
  - Knowledge-base — переиспользовать `CannedResponse` из `FEA-C31` для few-shot.
  - PII: regex-маскирование tg_id/email/имён перед отправкой в LLM.
- [ ] **FEA-C33 (S, P2).** User segmentation для рассылок: «trial-active», «expired-7d», «high-LTV», «no-purchase-after-trial». `BroadcastJob.filter` уже есть — расширить UI.
- [ ] **FEA-C34 (M, P2).** Fraud detection — same payment card на >N аккаунтах, trial-фермы, скорость redemption. `app/services/fraud.py` + `User.flags JSON`.
- [~] **FEA-C35 (D9, groundwork-only).** Marzban node auto-provisioning. Цель — aeza, не привязываемся жёстко.
  - Каркас `deploy/provision/` (Ansible inventory + role-skeleton `marzban_node/`).
  - `app/services/node_provisioning.py` — заглушка `NotImplementedError`.
  - README в `deploy/provision/README.md` с шагами ручного провижнинга.
  - Полная реализация — после выбора провайдера и закрытия P0/P1.
- [ ] **FEA-C37 (S, P2).** Outgoing webhooks: `subscription.created/paid/expired/refunded` → URL с HMAC.
- [ ] **FEA-C39 (M, P1).** Per-admin RBAC. Сегодня бинарный `require_web_admin`. Роли: `superadmin/support/finance/readonly`. Необходимо для команды >1 человека.

## D. Reliability / Ops

> Эти пункты дублируются в [CODE_QUALITY.md](CODE_QUALITY.md) (раздел Reliability/Ops). Здесь — алиасы для удобства roadmap'а.

- [ ] **FEA-D40** = OPS-1 — `/healthz`, `/readyz` в admin FastAPI.
- [ ] **FEA-D41** = OPS-2 — Rate-limit на callback'и.
- [ ] **FEA-D42** = OPS-3 — Circuit breaker.
- [ ] **FEA-D43** = OPS-4 — Outbox pattern.
- [x] **FEA-D44** = OPS-5 — Invoice idempotency keys.
- [ ] **FEA-D45** = OPS-6 — JSON-логи + request IDs.
- [ ] **FEA-D46** = OPS-7 — Scheduler lag SLO.

## E. Compliance / trust

См. блок Compliance в [CODE_QUALITY.md](CODE_QUALITY.md) (CMP-1…CMP-4).

## F. Полноценная админ-панель / CRM (D10–D12)

> Текущее состояние: 48 admin-роутов в `app/web/routes.py`. CRUD уже есть для тарифов, нод (read+sync), промо, рассылок, тикетов (read+close), invoice (read+approve/cancel), `app_links`, `marzban_page`, AppSettings. Полностью отсутствует CRUD по пользователям (только balance edit) и подпискам, и нет реал-тайм метрик нод.

- [ ] **FEA-ADMIN-USER-CRM (M, P1, D10+D12).** Расширенное управление клиентом из админки.
  - Сейчас `/admin/users/{id}` — read-only кроме баланса.
  - Edit: username/first_name/last_name, `User.admin_notes Text`, `User.tags JSON` (vip/chargeback/support_priority).
  - Block/unblock с reason (поля уже есть, нужна форма).
  - Force-cancel subscription + revoke в Marzban (`MarzbanClient.disable_user`).
  - Reset trial flag (`User.trial_issued_at = NULL`).
  - Manual subscription create — admin-approved invoice через `BillingService`.
  - DM-композер с записью в audit + outbox; история DM на странице пользователя.
  - Communication timeline: тикеты + DM + invoices + audit chronological.
  - LTV badge, CSV export per user (готовится к CMP-1 GDPR).
  - Аудит каждого admin-действия через `AuditLogRepository`.

- [ ] **FEA-ADMIN-SUB-CRM (M, P1, D10).** Административный CRUD по подпискам.
  - Сейчас read-only список через `/admin/users/{id}`.
  - `/admin/subscriptions/` — поиск (`service_id/marzban_username/tg_id`) + фильтры (trial-only/active/expired/exhausted/by-node).
  - `/admin/subscriptions/{id}` — кнопки: extend by N days/months, change tariff, reset traffic, force-disable, re-issue URL, move to another node.
  - Все операции через circuit breaker (Sprint 1) + `AuditLog`.

- [ ] **FEA-ADMIN-TARIFF-PLUS (M, P1, D11).** Спец-тарифы.
  - Сейчас в `TariffPlan` есть `is_public/is_active/is_archived/code/badge_text/description`.
  - Visibility enum: `public/code_only/segment_only/private_link`. Миграция.
  - Code-only: `PromoCode.unlocks_tariff_id` (миграция).
  - Segment-only: `TariffPlan.segment_filter_json` (DSL `{"min_paid_count": 3}`, `{"created_before": "2026-01-01"}`).
  - Time-windowed: `available_from/available_to`.
  - Private-link: `private_token UUID`, deep-link `t.me/<bot>?start=tariff_<token>` биндит в `User.unlocked_tariff_ids JSON`.
  - Highlighting: `accent_color`, `is_recommended`.
  - Inventory cap: `max_active_subscriptions`.
  - UI: расширение `/admin/pricing/` + предпросмотр глазами пользователя.

- [ ] **FEA-ADMIN-NODE-MONITOR (M, P1, D12).** Статистика и health нод.
  - Сейчас `NodeRegistry.health_status` обновляется только при ручном `sync_now`. Реальный probe не делается.
  - APScheduler job `probe_nodes_health` (каждые 60–120 сек): HTTP-ping `/api/system`, `users_count`, `online_users_count`.
  - Новая таблица `node_health_samples(node_id, ts, latency_ms, status, users_total, users_online, error_text)`. TTL 30 дней (cleanup job).
  - Денорм: `NodeRegistry.last_latency_ms/users_online/users_total`.
  - `/admin/nodes/` — latency колонка + users counters.
  - `/admin/nodes/{id}` — графики (Chart.js, downsample на сервере: 1точ/мин для 24h, 1точ/час для 7d).
  - Алерт `node_down` через `NotificationDispatcher` (5 fail-probes подряд).
  - Метрики Prometheus: `vpn_bot_node_latency_seconds{node}`, `vpn_bot_node_users_online{node}`, `vpn_bot_node_health{node}`.

- [ ] **FEA-ADMIN-DASHBOARD (M, P2, D12).** Главный дашборд админа (расширяет `FEA-C30`).
  - Сейчас `/admin/system/` — health snapshot.
  - KPI-плитки: MRR, DAU/WAU/MAU, Trial→Paid conversion, активные подписки, expiring 7d.
  - Графики: новые регистрации/день, оплаты/день, churn по тарифам, traffic usage.
  - Last activity feed (50 последних `AuditLog/Invoice/Ticket`).
  - Top customers по LTV.
  - Sticky алерты: «Marzban deferred», «Платежей нет 30 минут», «N нод down».
  - Все запросы кешируются 60–300 сек в Redis.

- [ ] **FEA-ADMIN-CRUD-EXPAND (S, P2, D10).** Точечные расширения CRUD по существующим страницам.
  - Промокоды: `unlocks_tariff_id`, `valid_from/valid_to`, `min_invoice_amount`, segment-binding.
  - Routing profiles: bulk-toggle, копирование, тест валидации XRAY rule.
  - Broadcasts: «дублировать», предпросмотр с реальной аудиторией, dry-run на admin'а.
  - Invoices: bulk-cancel pending старше 24ч; статистика по провайдеру.
  - Tickets: bulk-close, tags, assignee, attach файлов в reply.
  - Trial settings: «принудительная выдача всем active-юзерам».

---

## Топ-8 идей наибольшего рычага

1. **OPS-4 + OPS-5** — Outbox + idempotency на инвойсах: убирают худший класс саппорт-обращений «оплатил, ничего не пришло» и chargeback'и.
2. **FEA-ADMIN-USER-CRM + FEA-ADMIN-SUB-CRM** — полноценное управление клиентом из админки (D10/D12).
3. **FEA-NOTIF** — Smart push + админ-панель: trial→paid sequence, quick-actions, weekly report. Прямой uplift конверсии и retention.
4. **FEA-A8 + FEA-A9** — Улучшенные апсейлы трафика и устройств с админ-конфигом. Высокая маржа.
5. **FEA-C32 + FEA-C31** — DeepSeek-помощник саппорта + CRM-lite: 40%+ deflection.
6. **FEA-ADMIN-NODE-MONITOR** — реал-тайм статистика нод (latency, online users, throughput).
7. **FEA-ADMIN-TARIFF-PLUS** — спец-тарифы (D11): visibility/code-only/segment-only/private-link/inventory cap.
8. **FEA-C39** — RBAC: разделение ролей `superadmin/support/finance/readonly`.

> CRM Plus (раздел G, см. [CRM_BACKLOG.md](CRM_BACKLOG.md)) добавляет поверх: `CRM-P26` (2FA TOTP), `CRM-P27` (permission audit log), `CRM-P37` (bot admin-команды), `CRM-P33` (Public REST API).