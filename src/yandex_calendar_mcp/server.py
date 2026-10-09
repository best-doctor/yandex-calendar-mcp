"""Presentation-слой: MCP-тулы. Только десериализация аргументов, вызов клиента, возврат DTO."""

from __future__ import annotations

import collections.abc
import contextlib
import dataclasses
import datetime
import functools
import logging
import typing

from caldav.lib import error as caldav_error
from mcp.server import MCPServer
from mcp.server.mcpserver import Context as McpContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from yandex_calendar_mcp import __version__
from yandex_calendar_mcp.client import YandexCalendarClient
from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.dto import (
    AvailabilityResult,
    CalendarInfo,
    ConnectionInfo,
    DateLikeInput,
    DayAgenda,
    EventCreateDto,
    EventInfo,
    EventList,
    EventUpdateDto,
    FreeSlot,
    InviteResponse,
    SyncResult,
    TodoCreateDto,
    TodoList,
    WriteResult,
)
from yandex_calendar_mcp.errors import (
    CalendarNotFoundError,
    EventConflictError,
    EventNotFoundError,
    InvalidEventError,
    SyncTokenError,
)
from yandex_calendar_mcp.ical import date_like__to_datetime

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=True)

CalendarIdParam = typing.Annotated[
    str | None,
    Field(description='id или имя календаря из list_calendars. None = все календари'),
]
DateParam = DateLikeInput | None
RecurrenceIdParam = typing.Annotated[
    DateLikeInput | None,
    Field(description='Экземпляр серии: recurrence_id из list_events. None = объект целиком'),
]


@dataclasses.dataclass(frozen=True)
class AppState:
    settings: Settings
    client: YandexCalendarClient


Context = McpContext[AppState, typing.Any]
DOMAIN_ERRORS = (CalendarNotFoundError, EventNotFoundError, EventConflictError, InvalidEventError, SyncTokenError)


def domain_errors__as_tool_error[**P, R](fn: collections.abc.Callable[P, R]) -> collections.abc.Callable[P, R]:
    """Ожидаемые ошибки логики и CalDAV уходят модели текстом (is_error), а не трейсбеком в лог."""

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except DOMAIN_ERRORS as exc:
            raise ToolError(str(exc)) from exc
        except caldav_error.AuthorizationError as exc:
            raise ToolError('Яндекс отклонил авторизацию: проверьте YANDEX_CALDAV_EMAIL и пароль приложения') from exc
        except caldav_error.DAVError as exc:
            raise ToolError(f'Ошибка CalDAV: {exc}') from exc
        except OSError as exc:
            # Сеть и таймауты, когда ретраи исчерпаны
            raise ToolError(f'Нет связи с CalDAV-сервером: {exc}') from exc

    return wrapper


def day__of(value: datetime.date) -> datetime.date:
    return value.date() if isinstance(value, datetime.datetime) else value


def client__from_context(ctx: Context) -> YandexCalendarClient:
    return ctx.request_context.lifespan_context.client


def server__build(settings: Settings) -> MCPServer:
    """Тулы чтения регистрируются всегда, тулы записи только в режиме write."""

    @contextlib.asynccontextmanager
    async def lifespan(_: MCPServer) -> collections.abc.AsyncIterator[AppState]:
        yield AppState(settings=settings, client=YandexCalendarClient(settings))

    mcp = MCPServer(
        name='yandex-calendar',
        version=__version__,
        instructions=(
            'Яндекс Календарь через CalDAV. Даты принимаются в ISO 8601 (2026-10-08 или 2026-10-08T15:00). '
            'Без таймзоны считаются локальными (см. timezone в ответах). '
            'calendar_id берётся из list_calendars; если не указан, запрос идёт по всем календарям. '
            'Правило для встреч: перед create_event и перед переносом через update_event всегда вызывай '
            'check_availability для всех участников на нужный слот. Если у кого-то есть пересечение '
            '(BUSY или BUSY-TENTATIVE), не ставь встречу молча: покажи пересечения и общие свободные окна '
            'и уточни у пользователя. '
            f'Режим: {settings.mode}.'
        ),
        lifespan=lifespan,
    )
    tools__register_read(mcp)
    if settings.mode == 'write':
        tools__register_write(mcp)
    return mcp


def tools__register_read(mcp: MCPServer) -> None:
    @mcp.tool(annotations=READ_ONLY, description='Проверить подключение и показать аккаунт, сервер, календари.')
    @domain_errors__as_tool_error
    def get_connection_info(ctx: Context) -> ConnectionInfo:
        return client__from_context(ctx).connection_info()

    @mcp.tool(annotations=READ_ONLY, description='Список календарей аккаунта с их id.')
    @domain_errors__as_tool_error
    def list_calendars(ctx: Context) -> list[CalendarInfo]:
        return client__from_context(ctx).calendars__list()

    @mcp.tool(
        annotations=READ_ONLY,
        description=(
            'События за период. Повторяющиеся раскрываются в отдельные экземпляры (expand). '
            'По умолчанию: с сегодня на 7 дней. query — подстрока в названии/описании/месте.'
        ),
    )
    @domain_errors__as_tool_error
    def list_events(
        ctx: Context,
        start: DateParam = None,
        end: DateParam = None,
        calendar_id: CalendarIdParam = None,
        query: typing.Annotated[str | None, Field(description='Текстовый фильтр без учёта регистра')] = None,
        expand: typing.Annotated[bool, Field(description='Раскрывать повторяющиеся события')] = True,
    ) -> EventList:
        client = client__from_context(ctx)
        events, window_start, window_end = client.events__list_default_window(
            start=start, end=end, default_days=7, calendar_id=calendar_id, expand=expand, query=query
        )
        return EventList(
            events=events, count=len(events), window_start=window_start, window_end=window_end, timezone=client.tz.key
        )

    @mcp.tool(
        annotations=READ_ONLY,
        description=(
            'Одно событие по UID: участники со статусами, организатор, ссылка на встречу. '
            'Для повторяющегося события без occurrence возвращается мастер серии (дата первого экземпляра); '
            'с occurrence — экземпляр, начинающийся в этот день.'
        ),
    )
    @domain_errors__as_tool_error
    def get_event(
        ctx: Context,
        uid: str,
        calendar_id: CalendarIdParam = None,
        occurrence: typing.Annotated[
            datetime.date | None, Field(description='Дата экземпляра повторяющегося события, ISO 8601')
        ] = None,
    ) -> EventInfo:
        return client__from_context(ctx).event__get(uid, calendar_id=calendar_id, occurrence=occurrence)

    @mcp.tool(
        annotations=READ_ONLY, description='Сырой iCalendar (VCALENDAR) события по UID. Для отладки и редких полей.'
    )
    @domain_errors__as_tool_error
    def get_event_raw(ctx: Context, uid: str, calendar_id: CalendarIdParam = None) -> str:
        return client__from_context(ctx).event__get_raw(uid, calendar_id=calendar_id)

    @mcp.tool(
        annotations=READ_ONLY,
        description='Повестка по дням: события сгруппированы по дате. По умолчанию сегодня (days=1).',
    )
    @domain_errors__as_tool_error
    def get_agenda(
        ctx: Context,
        date_from: DateParam = None,
        days: typing.Annotated[int, Field(ge=1, le=62)] = 1,
        calendar_id: CalendarIdParam = None,
    ) -> list[DayAgenda]:
        client = client__from_context(ctx)
        first_day = day__of(date_from) if date_from is not None else datetime.datetime.now(client.tz).date()
        return client.agenda__by_days(date_from=first_day, days=days, calendar_id=calendar_id)

    @mcp.tool(
        annotations=READ_ONLY,
        description='Поиск событий по тексту в названии/описании/месте. Окно по умолчанию: -30…+90 дней от сегодня.',
    )
    @domain_errors__as_tool_error
    def search_events(
        ctx: Context,
        query: typing.Annotated[str, Field(min_length=1)],
        start: DateParam = None,
        end: DateParam = None,
        calendar_id: CalendarIdParam = None,
    ) -> EventList:
        client = client__from_context(ctx)
        today = datetime.datetime.now(client.tz).date()
        window_start = start if start is not None else today - datetime.timedelta(days=30)
        # Окно считается от start, иначе start позже today+90 дал бы пустое окно
        window_end = end if end is not None else max(today, day__of(window_start)) + datetime.timedelta(days=90)
        events = client.events__list(start=window_start, end=window_end, calendar_id=calendar_id, query=query)
        return EventList(
            events=events, count=len(events), window_start=window_start, window_end=window_end, timezone=client.tz.key
        )

    @mcp.tool(annotations=READ_ONLY, description='Задачи (VTODO). По умолчанию только незавершённые.')
    @domain_errors__as_tool_error
    def list_todos(ctx: Context, calendar_id: CalendarIdParam = None, include_completed: bool = False) -> TodoList:
        todos = client__from_context(ctx).todos__list(calendar_id=calendar_id, include_completed=include_completed)
        return TodoList(todos=todos, count=len(todos))

    @mcp.tool(
        annotations=READ_ONLY,
        description='Свободные окна в рабочие часы между событиями. Считается локально по событиям календаря.',
    )
    @domain_errors__as_tool_error
    def find_free_slots(
        ctx: Context,
        start: DateParam = None,
        end: DateParam = None,
        min_minutes: typing.Annotated[int, Field(ge=5, le=480)] = 30,
        work_start: typing.Annotated[datetime.time, Field(description='HH:MM')] = datetime.time(9, 0),
        work_end: typing.Annotated[datetime.time, Field(description='HH:MM')] = datetime.time(19, 0),
        calendar_id: CalendarIdParam = None,
    ) -> list[FreeSlot]:
        client = client__from_context(ctx)
        window_start = start if start is not None else datetime.datetime.now(client.tz).date()
        return client.free_slots__find(
            start=window_start,
            end=end if end is not None else day__of(window_start),
            min_minutes=min_minutes,
            work_start=work_start,
            work_end=work_end,
            calendar_id=calendar_id,
        )

    @mcp.tool(
        annotations=READ_ONLY,
        description=(
            'Инкрементальная синхронизация одного календаря (RFC 6578). Первый вызов без sync_token возвращает '
            'только токен. Следующий вызов с ним отдаёт события и задачи, изменённые после, и URL удалённых. '
            'Серии не раскрываются: приходят мастер и переопределения.'
        ),
    )
    @domain_errors__as_tool_error
    def sync_changes(
        ctx: Context,
        calendar_id: typing.Annotated[str, Field(description='id или имя календаря из list_calendars')],
        sync_token: typing.Annotated[str | None, Field(description='Токен из прошлого ответа sync_changes')] = None,
    ) -> SyncResult:
        return client__from_context(ctx).sync__changes(calendar_id, sync_token=sync_token)

    @mcp.tool(
        annotations=READ_ONLY,
        description=(
            'Занятость участников по их календарям (как наложение календарей в веб-интерфейсе) и общие свободные окна. '
            'Свой адрес добавляется автоматически. BUSY-TENTATIVE = приглашение без ответа. '
            'Уведомления участникам не отправляются.'
        ),
    )
    @domain_errors__as_tool_error
    def check_availability(
        ctx: Context,
        attendees: typing.Annotated[list[str], Field(min_length=1, description='E-mail участников')],
        start: DateParam = None,
        end: DateParam = None,
        min_minutes: typing.Annotated[int, Field(ge=5, le=480)] = 30,
        work_start: typing.Annotated[datetime.time, Field(description='HH:MM')] = datetime.time(9, 0),
        work_end: typing.Annotated[datetime.time, Field(description='HH:MM')] = datetime.time(19, 0),
    ) -> AvailabilityResult:
        client = client__from_context(ctx)
        today = datetime.datetime.now(client.tz).date()
        start_dt = date_like__to_datetime(start if start is not None else today, client.tz)
        end_dt = date_like__to_datetime(end if end is not None else start_dt.date(), client.tz, end_of_day=True)
        return client.availability__check(
            attendees=attendees,
            start=start_dt,
            end=end_dt,
            min_minutes=min_minutes,
            work_start=work_start,
            work_end=work_end,
        )


def tools__register_write(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=WRITE,
        description=(
            'Создать событие. Без calendar_id берётся первый календарь с поддержкой VEVENT. '
            'Дата без времени = событие на весь день. Участникам уйдут приглашения. '
            'Перед вызовом проверь слот через check_availability и при пересечениях уточни у пользователя.'
        ),
    )
    @domain_errors__as_tool_error
    def create_event(ctx: Context, event: EventCreateDto) -> WriteResult:
        return client__from_context(ctx).event__create(event)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            'Изменить поля события по uid. Передаются только меняемые поля. '
            'Для повторяющегося события без recurrence_id меняется вся серия, с recurrence_id только этот экземпляр. '
            'При переносе start/end сначала проверь новый слот через check_availability и при пересечениях '
            'уточни у пользователя.'
        ),
    )
    @domain_errors__as_tool_error
    def update_event(ctx: Context, event: EventUpdateDto) -> WriteResult:
        return client__from_context(ctx).event__update(event)

    @mcp.tool(
        annotations=DESTRUCTIVE,
        description=(
            'Удалить событие по uid. Для повторяющегося без recurrence_id удаляется вся серия, '
            'с recurrence_id только этот экземпляр.'
        ),
    )
    @domain_errors__as_tool_error
    def delete_event(
        ctx: Context, uid: str, calendar_id: CalendarIdParam = None, recurrence_id: RecurrenceIdParam = None
    ) -> WriteResult:
        return client__from_context(ctx).event__delete(uid, calendar_id=calendar_id, recurrence_id=recurrence_id)

    @mcp.tool(annotations=WRITE, description='Ответить на приглашение: accept, decline или tentative.')
    @domain_errors__as_tool_error
    def respond_to_invite(
        ctx: Context, uid: str, response: InviteResponse, calendar_id: CalendarIdParam = None
    ) -> WriteResult:
        return client__from_context(ctx).invite__respond(uid, response=response, calendar_id=calendar_id)

    @mcp.tool(
        annotations=WRITE, description='Создать задачу. Без calendar_id берётся первый календарь с поддержкой VTODO.'
    )
    @domain_errors__as_tool_error
    def create_todo(ctx: Context, todo: TodoCreateDto) -> WriteResult:
        return client__from_context(ctx).todo__create(todo)

    @mcp.tool(annotations=DESTRUCTIVE, description='Отметить задачу выполненной по uid.')
    @domain_errors__as_tool_error
    def complete_todo(ctx: Context, uid: str, calendar_id: CalendarIdParam = None) -> WriteResult:
        return client__from_context(ctx).todo__complete(uid, calendar_id=calendar_id)

    @mcp.tool(annotations=DESTRUCTIVE, description='Удалить задачу по uid.')
    @domain_errors__as_tool_error
    def delete_todo(ctx: Context, uid: str, calendar_id: CalendarIdParam = None) -> WriteResult:
        return client__from_context(ctx).todo__delete(uid, calendar_id=calendar_id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    # caldav пишет diff каждого исправленного объекта на уровне WARNING, это шум
    logging.getLogger('caldav').setLevel(logging.ERROR)
    server__build(Settings()).run('stdio')  # type: ignore[call-arg]
