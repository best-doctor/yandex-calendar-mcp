from __future__ import annotations

import datetime
import typing

from pydantic import BaseModel, ConfigDict, Field


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


# Во входных DTO date стоит раньше datetime: строка 2026-10-08 должна стать датой (весь день)
class EventCreateDto(BaseDto):
    summary: str = Field(min_length=1)
    start: datetime.date | datetime.datetime = Field(description='Дата = событие на весь день')
    end: datetime.date | datetime.datetime | None = Field(default=None, description='Если нет, см. duration_minutes')
    duration_minutes: int | None = Field(default=None, ge=1, description='Альтернатива end. По умолчанию 60 минут')
    description: str | None = None
    location: str | None = None
    attendees: list[str] = Field(default_factory=list, description='E-mail участников')
    rrule: str | None = Field(default=None, description='Правило повтора RFC 5545, например FREQ=WEEKLY;COUNT=5')
    calendar_id: str | None = Field(default=None, description='Из list_calendars. None = первый календарь')


class EventUpdateDto(BaseDto):
    uid: str
    calendar_id: str | None = None
    recurrence_id: datetime.date | datetime.datetime | None = Field(
        default=None,
        description='Экземпляр серии: recurrence_id из list_events. None = мастер-событие, то есть вся серия',
    )
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    start: datetime.date | datetime.datetime | None = None
    end: datetime.date | datetime.datetime | None = None
    status: EventStatus | None = None


class TodoCreateDto(BaseDto):
    summary: str = Field(min_length=1)
    due: datetime.date | datetime.datetime | None = None
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
