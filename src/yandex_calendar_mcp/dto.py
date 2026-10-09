from __future__ import annotations

import datetime
import re
import typing

import icalendar
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator

DATE_ONLY_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}')


def date_like__parse(value: typing.Any) -> typing.Any:
    """Строка ровно YYYY-MM-DD становится датой (весь день), любая другая строка дата-временем.

    Без этого pydantic превращает 2026-10-09T00:00 и даже 2026-10-09T00:00Z в дату: полночь теряет время и таймзону.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if DATE_ONLY_PATTERN.fullmatch(text):
        return datetime.date.fromisoformat(text)
    return datetime.datetime.fromisoformat(text)


DateLikeInput = typing.Annotated[
    datetime.datetime | datetime.date,
    BeforeValidator(date_like__parse),
    Field(description='ISO 8601: дата (2026-10-08) = весь день, или дата-время (2026-10-08T15:00)'),
]


class BaseDto(BaseModel):
    model_config = ConfigDict(frozen=True)


class CalendarInfo(BaseDto):
    id: str = Field(description='Короткий id (последний сегмент URL), используется в других тулах')
    name: str
    url: str
    supported_components: list[str] = Field(default_factory=list)


class Attendee(BaseDto):
    email: str
    name: str | None = None
    status: str | None = Field(default=None, description='PARTSTAT: ACCEPTED/DECLINED/TENTATIVE/NEEDS-ACTION')
    role: str | None = None


class EventInfo(BaseDto):
    uid: str
    calendar_id: str
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: datetime.datetime | datetime.date
    end: datetime.datetime | datetime.date | None = None
    all_day: bool = False
    status: str | None = Field(default=None, description='CONFIRMED/TENTATIVE/CANCELLED')
    organizer: str | None = None
    attendees: list[Attendee] = Field(default_factory=list)
    recurring: bool = False
    recurrence_id: datetime.datetime | datetime.date | None = Field(
        default=None,
        description='Заполнено для экземпляра повторяющегося события',
    )
    conference_url: str | None = Field(default=None, description='Ссылка на звонок из X-TELEMOST-CONFERENCE/CONFERENCE')
    web_url: str | None = Field(default=None, description='Ссылка на событие в веб-календаре (свойство URL)')
    url: str | None = Field(default=None, description='CalDAV URL объекта')
    last_modified: datetime.datetime | None = None


class TodoInfo(BaseDto):
    uid: str
    calendar_id: str
    summary: str | None = None
    description: str | None = None
    due: datetime.datetime | datetime.date | None = None
    status: str | None = None
    completed: datetime.datetime | None = None
    percent_complete: int | None = None
    priority: int | None = None


class DayAgenda(BaseDto):
    date: datetime.date
    events: list[EventInfo]


class FreeSlot(BaseDto):
    start: datetime.datetime
    end: datetime.datetime
    minutes: int


class BusyInterval(BaseDto):
    start: datetime.datetime
    end: datetime.datetime
    kind: str = Field(description='BUSY, BUSY-TENTATIVE (приглашение без ответа) или BUSY-UNAVAILABLE')


class AttendeeAvailability(BaseDto):
    email: str
    request_status: str = Field(description='iTIP request-status, 2.x = успех')
    busy: list[BusyInterval]


class AvailabilityResult(BaseDto):
    start: datetime.datetime
    end: datetime.datetime
    timezone: str
    attendees: list[AttendeeAvailability]
    common_free: list[FreeSlot] = Field(description='Окна, свободные у всех участников')


class EventList(BaseDto):
    events: list[EventInfo]
    count: int
    window_start: datetime.datetime | datetime.date
    window_end: datetime.datetime | datetime.date
    timezone: str


class TodoList(BaseDto):
    todos: list[TodoInfo]
    count: int


class ConnectionInfo(BaseDto):
    login: str
    server: str
    principal_url: str
    calendars: list[CalendarInfo]
    timezone: str
    mode: typing.Literal['readonly', 'write']


InviteResponse = typing.Literal['accept', 'decline', 'tentative']
EventStatus = typing.Literal['CONFIRMED', 'TENTATIVE', 'CANCELLED']


def rrule__normalize(value: str | None) -> str | None:
    """Убирает префикс RRULE: и проверяет, что правило разбирается и содержит FREQ."""
    if value is None:
        return None
    text = value.strip()
    if text.upper().startswith('RRULE:'):
        text = text[len('RRULE:') :]
    try:
        recur = icalendar.vRecur.from_ical(text)
    except ValueError as exc:
        raise ValueError(f'Некорректный RRULE {value!r}: {exc}') from exc
    if 'FREQ' not in recur:
        raise ValueError(f'RRULE {value!r} должен содержать FREQ, например FREQ=WEEKLY;COUNT=5')
    return text


class EventCreateDto(BaseDto):
    summary: str = Field(min_length=1)
    start: DateLikeInput = Field(description='Дата = событие на весь день')
    end: DateLikeInput | None = Field(
        default=None,
        description='Если нет, см. duration_minutes. Для события на весь день end не включается: '
        'один день 2026-10-10 это end=2026-10-11',
    )
    duration_minutes: int | None = Field(default=None, ge=1, description='Альтернатива end. По умолчанию 60 минут')
    description: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list, description='E-mail участников')
    rrule: str | None = Field(default=None, description='Правило повтора RFC 5545, например FREQ=WEEKLY;COUNT=5')
    calendar_id: str | None = Field(default=None, description='Из list_calendars. None = первый календарь')

    _rrule__validate = field_validator('rrule')(rrule__normalize)


class EventUpdateDto(BaseDto):
    uid: str
    calendar_id: str | None = None
    recurrence_id: DateLikeInput | None = Field(
        default=None,
        description='Экземпляр серии: recurrence_id из list_events. None = мастер-событие, то есть вся серия',
    )
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: DateLikeInput | None = None
    end: DateLikeInput | None = Field(default=None, description='Для события на весь день end не включается')
    status: EventStatus | None = None


class TodoCreateDto(BaseDto):
    summary: str = Field(min_length=1)
    due: DateLikeInput | None = None
    description: str | None = None
    priority: int | None = Field(default=None, ge=1, le=9, description='1 — самый высокий')
    calendar_id: str | None = None


class WriteResult(BaseDto):
    action: typing.Literal['created', 'updated', 'deleted', 'completed', 'accepted', 'declined', 'tentative']
    uid: str
    calendar_id: str
    url: str | None = None
    recurrence_id: datetime.datetime | datetime.date | None = Field(
        default=None, description='Заполнено, если изменён или удалён один экземпляр серии'
    )


class SyncResult(BaseDto):
    calendar_id: str
    sync_token: str = Field(description='Передать в следующий вызов, чтобы получить изменения после этого момента')
    baseline: bool = Field(description='True: токен без изменений, первый вызов без sync_token')
    events: list[EventInfo] = Field(
        default_factory=list, description='Изменённые и новые события: мастер и переопределения, без раскрытия серий'
    )
    todos: list[TodoInfo] = Field(default_factory=list)
    deleted_urls: list[str] = Field(
        default_factory=list, description='CalDAV URL удалённых объектов, сопоставляются с EventInfo.url'
    )
