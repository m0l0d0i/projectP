from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import AuditAction, AuditActorType, BroadcastJob, BroadcastJobStatus
from app.db.repositories import AuditLogRepository, BroadcastJobRepository
from app.services.broadcast_polling import process_scheduled_broadcasts


class BroadcastValidationError(ValueError):
    pass


@dataclass(slots=True)
class BroadcastButtonSpec:
    text: str
    url: str | None = None
    callback_data: str | None = None

    def to_row_item(self) -> dict[str, str]:
        payload = {'text': self.text}
        if self.url:
            payload['url'] = self.url
        if self.callback_data:
            payload['callback_data'] = self.callback_data
        return payload


@dataclass(slots=True)
class BroadcastPayload:
    text: str | None
    photo_file_id: str | None
    photo_file_unique_id: str | None
    media_type: str | None
    keyboard: list[list[BroadcastButtonSpec]]
    payload_json: dict[str, Any]

    @property
    def has_text(self) -> bool:
        return bool((self.text or '').strip())

    @property
    def has_photo(self) -> bool:
        return bool((self.photo_file_id or '').strip())

    @property
    def is_empty(self) -> bool:
        return not self.has_text and not self.has_photo

    @property
    def keyboard_json(self) -> list[list[dict[str, str]]] | None:
        if not self.keyboard:
            return None
        return [[button.to_row_item() for button in row] for row in self.keyboard]

    def to_storage_payload(self) -> dict[str, Any]:
        return {
            'version': 1,
            'content': {
                'text': self.text,
                'photo_file_id': self.photo_file_id,
                'photo_file_unique_id': self.photo_file_unique_id,
                'media_type': self.media_type,
            },
            'keyboard': self.keyboard_json,
        }


class BroadcastService:
    MAX_TEXT_LENGTH = 4096
    MAX_CAPTION_LENGTH = 1024
    MAX_KEYBOARD_ROWS = 8
    MAX_BUTTONS_PER_ROW = 8
    MAX_TOTAL_BUTTONS = 32
    MAX_BUTTON_TEXT_LENGTH = 64
    MAX_CALLBACK_DATA_LENGTH = 64
    MAX_RUN_AT_YEARS_AHEAD = 2

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.jobs = BroadcastJobRepository(session)
        self.audit = AuditLogRepository(session)

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @classmethod
    def _normalize_broadcast_text(cls, value: str | None, *, for_caption: bool = False) -> str | None:
        normalized = cls._normalize_optional_text(value)
        if normalized is None:
            return None
        limit = cls.MAX_CAPTION_LENGTH if for_caption else cls.MAX_TEXT_LENGTH
        if len(normalized) > limit:
            raise BroadcastValidationError(
                'Текст рассылки слишком длинный.' if not for_caption else 'Подпись к фото слишком длинная.'
            )
        return normalized

    @staticmethod
    def _normalize_photo_file_id(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @staticmethod
    def _normalize_photo_file_unique_id(value: str | None) -> str | None:
        normalized = (value or '').strip()
        return normalized or None

    @classmethod
    def _normalize_run_at(cls, value: datetime | None) -> datetime:
        run_at = value or cls.utcnow()
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=timezone.utc)
        else:
            run_at = run_at.astimezone(timezone.utc)

        lower_bound = cls.utcnow().replace(microsecond=0)
        if run_at < lower_bound.replace(year=lower_bound.year - 1):
            raise BroadcastValidationError('Дата запуска рассылки выглядит некорректной.')

        future_limit_year = lower_bound.year + cls.MAX_RUN_AT_YEARS_AHEAD
        if run_at.year > future_limit_year:
            raise BroadcastValidationError('Дата запуска рассылки слишком далеко в будущем.')
        return run_at

    @classmethod
    def _normalize_media_type(
        cls,
        *,
        photo_file_id: str | None,
        media_type: str | None,
    ) -> str | None:
        normalized = (media_type or '').strip().lower() or None
        if normalized is None:
            return 'photo' if photo_file_id else None
        if normalized != 'photo':
            raise BroadcastValidationError('Сейчас рассылки поддерживают только один тип медиа: photo.')
        if not photo_file_id:
            raise BroadcastValidationError('Тип media=photo указан без photo_file_id.')
        return normalized

    @classmethod
    def _normalize_button(cls, raw: dict[str, Any], *, row_index: int, button_index: int) -> BroadcastButtonSpec:
        if not isinstance(raw, dict):
            raise BroadcastValidationError(
                f'Кнопка #{button_index + 1} в ряду #{row_index + 1} должна быть объектом JSON.'
            )
        text = (raw.get('text') or '').strip()
        url = (raw.get('url') or '').strip() or None
        callback_data = (raw.get('callback_data') or '').strip() or None

        if not text:
            raise BroadcastValidationError(
                f'У кнопки #{button_index + 1} в ряду #{row_index + 1} отсутствует text.'
            )
        if len(text) > cls.MAX_BUTTON_TEXT_LENGTH:
            raise BroadcastValidationError(
                f'Текст кнопки #{button_index + 1} в ряду #{row_index + 1} слишком длинный.'
            )
        if bool(url) == bool(callback_data):
            raise BroadcastValidationError(
                f'Кнопка #{button_index + 1} в ряду #{row_index + 1} должна содержать ровно одно поле: url или callback_data.'
            )
        if callback_data and len(callback_data) > cls.MAX_CALLBACK_DATA_LENGTH:
            raise BroadcastValidationError(
                f'callback_data у кнопки #{button_index + 1} в ряду #{row_index + 1} слишком длинный.'
            )
        if url and not (url.startswith('http://') or url.startswith('https://') or url.startswith('tg://')):
            raise BroadcastValidationError(
                f'URL у кнопки #{button_index + 1} в ряду #{row_index + 1} должен начинаться с http://, https:// или tg://.'
            )

        return BroadcastButtonSpec(text=text, url=url, callback_data=callback_data)

    @classmethod
    def _normalize_keyboard_from_rows(
        cls,
        rows: list[list[dict[str, Any]]] | None,
    ) -> list[list[BroadcastButtonSpec]]:
        if rows in (None, []):
            return []
        if not isinstance(rows, list):
            raise BroadcastValidationError('Клавиатура должна быть списком рядов.')
        if len(rows) > cls.MAX_KEYBOARD_ROWS:
            raise BroadcastValidationError('Слишком много рядов кнопок в клавиатуре.')

        normalized_rows: list[list[BroadcastButtonSpec]] = []
        total_buttons = 0
        for row_index, row in enumerate(rows):
            if not isinstance(row, list):
                raise BroadcastValidationError(f'Ряд #{row_index + 1} клавиатуры должен быть массивом.')
            if not row:
                raise BroadcastValidationError(f'Ряд #{row_index + 1} клавиатуры не может быть пустым.')
            if len(row) > cls.MAX_BUTTONS_PER_ROW:
                raise BroadcastValidationError(
                    f'В ряду #{row_index + 1} слишком много кнопок.'
                )

            normalized_row = [
                cls._normalize_button(button, row_index=row_index, button_index=button_index)
                for button_index, button in enumerate(row)
            ]
            total_buttons += len(normalized_row)
            normalized_rows.append(normalized_row)

        if total_buttons > cls.MAX_TOTAL_BUTTONS:
            raise BroadcastValidationError('Слишком много кнопок в одной рассылке.')
        return normalized_rows

    @classmethod
    def parse_keyboard_json(cls, raw: str | None) -> list[list[BroadcastButtonSpec]]:
        normalized = (raw or '').strip()
        if not normalized:
            return []
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError as exc:
            raise BroadcastValidationError('Клавиатура должна быть корректным JSON.') from exc
        return cls._normalize_keyboard_from_rows(payload)

    @classmethod
    def build_payload(
        cls,
        *,
        text: str | None,
        photo_file_id: str | None = None,
        photo_file_unique_id: str | None = None,
        media_type: str | None = None,
        keyboard_rows: list[list[dict[str, Any]]] | None = None,
        keyboard_json_raw: str | None = None,
        payload_json: dict[str, Any] | None = None,
    ) -> BroadcastPayload:
        normalized_photo_file_id = cls._normalize_photo_file_id(photo_file_id)
        normalized_media_type = cls._normalize_media_type(
            photo_file_id=normalized_photo_file_id,
            media_type=media_type,
        )
        normalized_text = cls._normalize_broadcast_text(
            text,
            for_caption=bool(normalized_photo_file_id),
        )
        normalized_keyboard = cls._normalize_keyboard_from_rows(keyboard_rows)
        if keyboard_json_raw is not None:
            normalized_keyboard = cls.parse_keyboard_json(keyboard_json_raw)

        payload = BroadcastPayload(
            text=normalized_text,
            photo_file_id=normalized_photo_file_id,
            photo_file_unique_id=cls._normalize_photo_file_unique_id(photo_file_unique_id),
            media_type=normalized_media_type,
            keyboard=normalized_keyboard,
            payload_json=payload_json or {},
        )
        if payload.is_empty:
            raise BroadcastValidationError('У рассылки должен быть текст или photo_file_id.')
        payload.payload_json = payload.to_storage_payload()
        return payload

    @staticmethod
    def preview_text(payload: BroadcastPayload, *, run_at: datetime | None = None, status: str | None = None) -> str:
        lines = ['📣 Превью рассылки']
        if status:
            lines.append(f'Статус: {status}')
        if run_at is not None:
            run_value = run_at.astimezone(timezone.utc) if run_at.tzinfo else run_at.replace(tzinfo=timezone.utc)
            lines.append(f'Запуск: {run_value.strftime("%Y-%m-%d %H:%M UTC")}')
        if payload.photo_file_id:
            lines.append('Медиа: фото')
        if payload.text:
            lines.append('Текст:')
            lines.append(payload.text[:1500])
        else:
            lines.append('Текст: —')
        if payload.keyboard:
            lines.append(f'Кнопки: {sum(len(row) for row in payload.keyboard)}')
        return '\n'.join(lines)

    @staticmethod
    def payload_from_job(job: BroadcastJob) -> BroadcastPayload:
        keyboard_rows = getattr(job, 'keyboard_json', None) or []
        normalized_keyboard = BroadcastService._normalize_keyboard_from_rows(keyboard_rows)
        stored_payload = getattr(job, 'payload_json', None) or {}
        if not isinstance(stored_payload, dict):
            stored_payload = {}
        return BroadcastPayload(
            text=(getattr(job, 'text', None) or '').strip() or None,
            photo_file_id=(getattr(job, 'photo_file_id', None) or '').strip() or None,
            photo_file_unique_id=(getattr(job, 'photo_file_unique_id', None) or '').strip() or None,
            media_type=(getattr(job, 'media_type', None) or '').strip() or None,
            keyboard=normalized_keyboard,
            payload_json=stored_payload,
        )

    @staticmethod
    def build_inline_keyboard(payload: BroadcastPayload) -> InlineKeyboardMarkup | None:
        if not payload.keyboard:
            return None
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button.text,
                        url=button.url,
                        callback_data=button.callback_data,
                    )
                    for button in row
                ]
                for row in payload.keyboard
            ]
        )

    async def create_job(
        self,
        *,
        created_by_tg_id: int,
        text: str | None,
        run_at: datetime | None,
        photo_file_id: str | None = None,
        photo_file_unique_id: str | None = None,
        media_type: str | None = None,
        keyboard_rows: list[list[dict[str, Any]]] | None = None,
        keyboard_json_raw: str | None = None,
        status: BroadcastJobStatus = BroadcastJobStatus.scheduled,
        actor_tg_id: int | None = None,
        actor_type: AuditActorType = AuditActorType.admin,
        audience_segment: str | None = None,
    ) -> BroadcastJob:
        from app.services.segments import normalize_segment, BroadcastSegment
        payload = self.build_payload(
            text=text,
            photo_file_id=photo_file_id,
            photo_file_unique_id=photo_file_unique_id,
            media_type=media_type,
            keyboard_rows=keyboard_rows,
            keyboard_json_raw=keyboard_json_raw,
        )
        normalized_run_at = self._normalize_run_at(run_at)
        normalized_segment = normalize_segment(audience_segment)
        # NULL в БД = all (legacy-compat); сохраняем NULL когда сегмент = all.
        segment_for_db = None if normalized_segment == BroadcastSegment.all.value else normalized_segment

        job = await self.jobs.create(
            created_by_tg_id=int(created_by_tg_id),
            text=payload.text,
            run_at=normalized_run_at,
            photo_file_id=payload.photo_file_id,
            photo_file_unique_id=payload.photo_file_unique_id,
            media_type=payload.media_type,
            keyboard_json=payload.keyboard_json,
            payload_json=payload.payload_json,
            status=status,
            audience_segment=segment_for_db,
        )
        await self.audit.create(
            action=AuditAction.broadcast_created,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'status': getattr(job.status, 'value', str(job.status)),
                'run_at': normalized_run_at.isoformat(),
                'text_preview': (payload.text or '')[:500],
                'has_photo': bool(payload.photo_file_id),
                'button_count': sum(len(row) for row in payload.keyboard),
                'audience_segment': normalized_segment,
            },
        )
        await self.session.flush()
        return job

    async def update_job(
        self,
        *,
        job_id: int,
        text: str | None,
        run_at: datetime | None = None,
        photo_file_id: str | None = None,
        photo_file_unique_id: str | None = None,
        media_type: str | None = None,
        keyboard_rows: list[list[dict[str, Any]]] | None = None,
        keyboard_json_raw: str | None = None,
        actor_tg_id: int | None = None,
        actor_type: AuditActorType = AuditActorType.admin,
        audience_segment: str | None = None,
    ) -> BroadcastJob:
        from app.services.segments import BroadcastSegment, normalize_segment
        job = await self.jobs.get_by_id_for_update(job_id)
        if job is None:
            raise BroadcastValidationError('Рассылка не найдена.')
        if not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
            raise BroadcastValidationError('Редактировать можно только draft и scheduled рассылки.')

        if audience_segment is not None:
            normalized_segment = normalize_segment(audience_segment)
            job.audience_segment = (
                None if normalized_segment == BroadcastSegment.all.value else normalized_segment
            )

        payload = self.build_payload(
            text=text,
            photo_file_id=photo_file_id,
            photo_file_unique_id=photo_file_unique_id,
            media_type=media_type,
            keyboard_rows=keyboard_rows,
            keyboard_json_raw=keyboard_json_raw,
        )
        if hasattr(self.jobs, 'update_content'):
            job = await self.jobs.update_content(
                job,
                text=payload.text,
                photo_file_id=payload.photo_file_id,
                photo_file_unique_id=payload.photo_file_unique_id,
                media_type=payload.media_type,
                keyboard_json=payload.keyboard_json,
                payload_json=payload.payload_json,
            )
        else:
            await self.jobs.update_text(job, payload.text or '')
            job.photo_file_id = payload.photo_file_id
            job.photo_file_unique_id = payload.photo_file_unique_id
            job.media_type = payload.media_type
            job.keyboard_json = payload.keyboard_json
            job.payload_json = payload.payload_json

        if run_at is not None:
            await self.jobs.update_run_at(job, self._normalize_run_at(run_at))

        await self.audit.create(
            action=AuditAction.admin_action,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'operation': 'update',
                'status': getattr(job.status, 'value', str(job.status)),
                'run_at': getattr(job, 'run_at', None).isoformat() if getattr(job, 'run_at', None) else None,
                'text_preview': (payload.text or '')[:500],
                'has_photo': bool(payload.photo_file_id),
                'button_count': sum(len(row) for row in payload.keyboard),
            },
        )
        await self.session.flush()
        return job

    async def delete_job(
        self,
        *,
        job_id: int,
        actor_tg_id: int | None = None,
        actor_type: AuditActorType = AuditActorType.admin,
    ) -> None:
        job = await self.jobs.get_by_id_for_update(job_id)
        if job is None:
            raise BroadcastValidationError('Рассылка не найдена.')
        if not getattr(job, 'is_editable', job.status in {BroadcastJobStatus.draft, BroadcastJobStatus.scheduled}):
            raise BroadcastValidationError('Удалять можно только draft и scheduled рассылки.')

        await self.audit.create(
            action=AuditAction.admin_action,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'operation': 'delete',
                'status': getattr(job.status, 'value', str(job.status)),
            },
        )
        await self.jobs.delete(job)
        await self.session.flush()

    async def request_cancel(
        self,
        *,
        job_id: int,
        actor_tg_id: int | None = None,
        actor_type: AuditActorType = AuditActorType.admin,
        reason: str | None = None,
    ) -> BroadcastJob:
        job = await self.jobs.get_by_id_for_update(job_id)
        if job is None:
            raise BroadcastValidationError('Рассылка не найдена.')
        if job.status not in {BroadcastJobStatus.scheduled, BroadcastJobStatus.running}:
            raise BroadcastValidationError('Отменить можно только scheduled или running рассылку.')

        if hasattr(self.jobs, 'request_cancel'):
            await self.jobs.request_cancel(job, cancelled_by_tg_id=actor_tg_id)
        else:
            await self.jobs.cancel(job, error=reason)

        await self.audit.create(
            action=AuditAction.admin_action,
            actor_type=actor_type,
            actor_tg_id=actor_tg_id,
            entity_type='broadcast_job',
            entity_id=str(job.id),
            details={
                'operation': 'request_cancel',
                'reason': self._normalize_optional_text(reason),
                'status': getattr(job.status, 'value', str(job.status)),
            },
        )
        await self.session.flush()
        return job

    async def clone_job(
        self,
        *,
        job_id: int,
        created_by_tg_id: int,
        run_at: datetime | None = None,
        status: BroadcastJobStatus = BroadcastJobStatus.draft,
        actor_tg_id: int | None = None,
        actor_type: AuditActorType = AuditActorType.admin,
    ) -> BroadcastJob:
        source = await self.jobs.get_by_id(job_id)
        if source is None:
            raise BroadcastValidationError('Исходная рассылка не найдена.')
        payload = self.payload_from_job(source)
        effective_run_at = run_at or source.run_at
        return await self.create_job(
            created_by_tg_id=created_by_tg_id,
            text=payload.text,
            run_at=effective_run_at,
            photo_file_id=payload.photo_file_id,
            photo_file_unique_id=payload.photo_file_unique_id,
            media_type=payload.media_type,
            keyboard_rows=payload.keyboard_json,
            status=status,
            actor_tg_id=actor_tg_id,
            actor_type=actor_type,
        )

    async def send_test(
        self,
        bot,
        *,
        target_tg_id: int,
        text: str | None,
        photo_file_id: str | None = None,
        photo_file_unique_id: str | None = None,
        media_type: str | None = None,
        keyboard_rows: list[list[dict[str, Any]]] | None = None,
        keyboard_json_raw: str | None = None,
    ) -> None:
        payload = self.build_payload(
            text=text,
            photo_file_id=photo_file_id,
            photo_file_unique_id=photo_file_unique_id,
            media_type=media_type,
            keyboard_rows=keyboard_rows,
            keyboard_json_raw=keyboard_json_raw,
        )
        markup = self.build_inline_keyboard(payload)
        if payload.photo_file_id:
            await bot.send_photo(
                target_tg_id,
                payload.photo_file_id,
                caption=payload.text,
                reply_markup=markup,
            )
            return
        await bot.send_message(target_tg_id, payload.text or '', reply_markup=markup)


async def process_broadcast_jobs(bot, sessionmaker: async_sessionmaker, settings: Settings) -> None:
    await process_scheduled_broadcasts(bot, sessionmaker, settings)
