"""CRM-lite: assignee/tags на support_tickets + canned_responses + seed (FEA-C31 #1)

Revision ID: 20260510_000032
Revises: 20260510_000031
Create Date: 2026-05-10 00:00:04.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '20260510_000032'
down_revision = '20260510_000031'
branch_labels = None
depends_on = None


_NEW_AUDIT_VALUES = (
    'ticket_assigned',
    'ticket_tagged',
    'canned_response_created',
    'canned_response_updated',
    'canned_response_deleted',
)

# Стартовый набор шаблонов ответов саппорта. Покрывает топ-15 типовых
# обращений по категориям onboarding / billing / troubleshooting. Коды
# используются как stable-references из аналитики и FAQ; меняются только
# через миграции, чтобы не сломать external привязки. Контент саппорт
# редактирует через `/admin/canned-responses/` (FEA-C31 #3).
_SEED_CANNED_RESPONSES: list[dict] = [
    # --- onboarding -------------------------------------------------------
    {
        'code': 'how_to_connect',
        'title': '🔑 Как подключиться к VPN',
        'tags': ['onboarding', 'connect'],
        'sort_order': 10,
        'content': (
            'Чтобы подключиться к VPN:\n'
            '1. В разделе «Мой VPN» выберите вашу подписку.\n'
            '2. Нажмите «🔑 Показать ключ / QR».\n'
            '3. Установите рекомендованное приложение для вашей платформы.\n'
            '4. Импортируйте подписку через QR-код или скопируйте ссылку.\n\n'
            'Подробные инструкции по платформам — в разделе «📱 Подключить устройство».'
        ),
    },
    {
        'code': 'ios_setup',
        'title': '📱 Настройка на iOS / iPadOS',
        'tags': ['onboarding', 'ios'],
        'sort_order': 20,
        'content': (
            'Рекомендуем приложение V2RayTun или Streisand из App Store.\n\n'
            '1. Установите приложение.\n'
            '2. В боте откройте «Мой VPN» → «🔑 Показать ключ / QR».\n'
            '3. Отсканируйте QR-код камерой приложения или нажмите «➕» → '
            '«Импорт из буфера обмена» (предварительно скопируйте ссылку подписки).\n'
            '4. Включите подключение тумблером.\n\n'
            'Если приложение не видит QR — попробуйте импорт ссылки.'
        ),
    },
    {
        'code': 'android_setup',
        'title': '🤖 Настройка на Android',
        'tags': ['onboarding', 'android'],
        'sort_order': 30,
        'content': (
            'Рекомендуем v2rayNG или Hiddify из Google Play.\n\n'
            '1. Установите приложение.\n'
            '2. В боте откройте «Мой VPN» → «🔑 Показать ключ / QR».\n'
            '3. В приложении нажмите «➕» → «Сканировать QR-код» или «Импорт из буфера обмена».\n'
            '4. Выберите импортированный профиль и нажмите кнопку «▶» внизу.\n\n'
            'Разрешите создание VPN-подключения, когда система спросит.'
        ),
    },
    {
        'code': 'macos_setup',
        'title': '🖥 Настройка на macOS',
        'tags': ['onboarding', 'macos'],
        'sort_order': 40,
        'content': (
            'Рекомендуем V2RayTun (Mac App Store) или V2Box.\n\n'
            '1. Установите приложение.\n'
            '2. В боте скопируйте ссылку подписки в «Мой VPN» → «🔑 Показать ключ / QR».\n'
            '3. В приложении: «Configurations» → «➕» → «Import from Clipboard».\n'
            '4. Включите подключение в строке меню.\n\n'
            'macOS может попросить разрешение на установку VPN-конфигурации — подтвердите.'
        ),
    },
    {
        'code': 'windows_setup',
        'title': '💻 Настройка на Windows',
        'tags': ['onboarding', 'windows'],
        'sort_order': 50,
        'content': (
            'Рекомендуем Hiddify-Next или Nekoray.\n\n'
            '1. Скачайте и установите приложение.\n'
            '2. В боте скопируйте ссылку подписки.\n'
            '3. В приложении: «Profiles» → «Add from Clipboard» (или «➕» → URL).\n'
            '4. Выберите профиль и включите подключение.\n\n'
            'При первом запуске Windows может запросить разрешение Defender — разрешите.'
        ),
    },

    # --- troubleshooting --------------------------------------------------
    {
        'code': 'not_working',
        'title': '⚠️ VPN не работает',
        'tags': ['troubleshooting'],
        'sort_order': 100,
        'content': (
            'Проверьте по порядку:\n'
            '1. Подписка активна: «Мой VPN» → статус «Активна» и срок не истёк.\n'
            '2. Трафик не закончился: остаток отображается там же.\n'
            '3. В приложении подписка обновлена (нажмите «Update» / «Обновить» на профиле).\n'
            '4. Попробуйте переключиться на другой сервер, если в приложении есть выбор.\n'
            '5. Перезапустите подключение тумблером и проверьте интернет вне VPN.\n\n'
            'Если всё проверено — пришлите название платформы, приложения и скриншот ошибки.'
        ),
    },
    {
        'code': 'speed_slow',
        'title': '🐌 Медленная скорость',
        'tags': ['troubleshooting', 'speed'],
        'sort_order': 110,
        'content': (
            'Скорость зависит от провайдера, времени суток и выбранного сервера.\n\n'
            'Что проверить:\n'
            '1. Обновите подписку в приложении (новые ключи маршрутизации).\n'
            '2. Если в приложении есть выбор сервера — попробуйте другой.\n'
            '3. Проверьте скорость без VPN (speedtest.net) — иногда проблема у провайдера.\n'
            '4. На мобильных — переключитесь между Wi-Fi и мобильным интернетом.\n\n'
            'Если разница большая и без VPN всё нормально — пришлите результаты speedtest с VPN и без.'
        ),
    },
    {
        'code': 'which_country_select',
        'title': '🌍 Какую страну выбрать',
        'tags': ['troubleshooting', 'routing'],
        'sort_order': 120,
        'content': (
            'Сейчас маршрутизация подбирается автоматически — для большинства сервисов '
            'этого достаточно. Если нужен конкретный регион (например, для стримингов) — '
            'напишите, какой сервис и какая страна нужна, мы посмотрим что можно сделать.\n\n'
            'Если в вашем приложении уже есть переключатель серверов — можете попробовать '
            'другие варианты вручную, это никак не нарушит подписку.'
        ),
    },

    # --- billing ----------------------------------------------------------
    {
        'code': 'how_to_renew',
        'title': '⏳ Как продлить подписку',
        'tags': ['billing', 'renewal'],
        'sort_order': 200,
        'content': (
            '1. «Мой VPN» → откройте подписку.\n'
            '2. «⏳ Продлить подписку» → выберите срок и оплатите.\n\n'
            'Если подписка ещё активна, новые дни добавятся к текущему сроку без сброса '
            'трафика. Если истекла — продление активируется с момента оплаты.'
        ),
    },
    {
        'code': 'how_to_topup_balance',
        'title': '💳 Как пополнить баланс',
        'tags': ['billing', 'balance'],
        'sort_order': 210,
        'content': (
            'В главном меню нажмите «💳 Пополнить» → введите сумму → оплатите. После '
            'подтверждения деньги поступят на внутренний баланс и будут автоматически '
            'предложены к списанию при следующей покупке (включая продление и докупку трафика).\n\n'
            'Минимальная сумма пополнения отображается на экране ввода.'
        ),
    },
    {
        'code': 'traffic_exhausted',
        'title': '📦 Закончился трафик',
        'tags': ['billing', 'traffic'],
        'sort_order': 220,
        'content': (
            'Текущий трафик можно дополнить, не дожидаясь следующего цикла:\n\n'
            '«Мой VPN» → подписка → «📦 Докупить трафик». Дополнительные ГБ действуют '
            'до конца текущего расчётного периода и обнулятся при monthly reset.\n\n'
            'Если хотите бóльший лимит на постоянной основе — лучше продлить тариф с '
            'бóльшим месячным объёмом.'
        ),
    },
    {
        'code': 'change_device_count',
        'title': '➕ Добавить устройство',
        'tags': ['billing', 'devices'],
        'sort_order': 230,
        'content': (
            'В карточке подписки есть кнопка «➕ Добавить устройство» — лимит увеличится '
            'на одно устройство до конца текущего срока подписки. Цена считается '
            'пропорционально оставшимся дням.\n\n'
            'При продлении тарифа количество устройств возьмётся из выбранного варианта, '
            'поэтому продление как «5 устройств» с самого начала бывает выгоднее.'
        ),
    },
    {
        'code': 'promo_invalid',
        'title': '🎫 Промокод не работает',
        'tags': ['billing', 'promo'],
        'sort_order': 240,
        'content': (
            'Возможные причины:\n'
            '1. Срок промокода истёк.\n'
            '2. Лимит использований исчерпан.\n'
            '3. Промокод одноразовый и уже применялся на вашем аккаунте.\n'
            '4. Вы вводите код с пробелом или регистром — попробуйте скопировать ровно как в источнике.\n\n'
            'Если проверили всё — пришлите код, посмотрим в админке.'
        ),
    },
    {
        'code': 'refund_policy',
        'title': '↩️ Возврат денег',
        'tags': ['billing', 'refund'],
        'sort_order': 250,
        'content': (
            'Возврат возможен в первые 24 часа после оплаты, если подписка ещё не '
            'использовалась активно (трафик не расходовался). После начала использования '
            'возврат рассматриваем индивидуально — пришлите номер счёта и опишите ситуацию, '
            'постараемся помочь.'
        ),
    },

    # --- closing ----------------------------------------------------------
    {
        'code': 'thank_you_close',
        'title': '✅ Закрытие тикета',
        'tags': ['closing'],
        'sort_order': 900,
        'content': (
            'Если больше вопросов нет — закроем тикет. По желанию можете написать снова в '
            'любой момент, мы откроем новый.\n\nХорошего дня! 🙌'
        ),
    },
]


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == 'postgresql'

    if is_pg:
        for value in _NEW_AUDIT_VALUES:
            op.execute(
                sa.text(
                    f"ALTER TYPE audit_action ADD VALUE IF NOT EXISTS '{value}'"
                ).execution_options(autocommit=True)
            )

    # support_tickets: assignee + tags
    op.add_column(
        'support_tickets',
        sa.Column(
            'assignee_admin_id',
            sa.Integer(),
            nullable=True,
        ),
    )
    op.add_column(
        'support_tickets',
        sa.Column(
            'tags',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.create_foreign_key(
        'fk_support_tickets_assignee_admin_id',
        'support_tickets',
        'web_admin_users',
        ['assignee_admin_id'],
        ['id'],
        ondelete='SET NULL',
    )
    op.create_index(
        'ix_support_tickets_assignee_status',
        'support_tickets',
        ['assignee_admin_id', 'status'],
    )

    # canned_responses
    op.create_table(
        'canned_responses',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column(
            'tags',
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column(
            'is_active',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
        sa.Column(
            'sort_order',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('100'),
        ),
        sa.Column(
            'usage_count',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('0'),
        ),
        sa.Column('created_by_admin_id', sa.Integer(), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text('CURRENT_TIMESTAMP'),
        ),
        sa.CheckConstraint(
            "char_length(trim(code)) > 0",
            name='ck_canned_responses_code_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(trim(title)) > 0",
            name='ck_canned_responses_title_not_blank',
        ),
        sa.CheckConstraint(
            "char_length(content) > 0",
            name='ck_canned_responses_content_not_blank',
        ),
        sa.CheckConstraint(
            'usage_count >= 0',
            name='ck_canned_responses_usage_count_non_negative',
        ),
        sa.UniqueConstraint('code', name='uq_canned_responses_code'),
        sa.ForeignKeyConstraint(
            ['created_by_admin_id'],
            ['web_admin_users.id'],
            ondelete='SET NULL',
            name='fk_canned_responses_created_by_admin_id',
        ),
    )
    op.create_index(
        'ix_canned_responses_active_sort',
        'canned_responses',
        ['is_active', 'sort_order'],
    )

    # Seed
    canned_table = sa.table(
        'canned_responses',
        sa.column('code', sa.String()),
        sa.column('title', sa.String()),
        sa.column('content', sa.Text()),
        sa.column('tags', sa.JSON()),
        sa.column('sort_order', sa.Integer()),
    )
    seed_rows = [
        {
            'code': item['code'],
            'title': item['title'],
            'content': item['content'],
            'tags': item['tags'],
            'sort_order': item['sort_order'],
        }
        for item in _SEED_CANNED_RESPONSES
    ]
    op.bulk_insert(canned_table, seed_rows)


def downgrade() -> None:
    op.drop_index('ix_canned_responses_active_sort', table_name='canned_responses')
    op.drop_table('canned_responses')

    op.drop_index('ix_support_tickets_assignee_status', table_name='support_tickets')
    op.drop_constraint(
        'fk_support_tickets_assignee_admin_id',
        'support_tickets',
        type_='foreignkey',
    )
    op.drop_column('support_tickets', 'tags')
    op.drop_column('support_tickets', 'assignee_admin_id')

    # PG не поддерживает удаление enum value — namespace value останутся.
