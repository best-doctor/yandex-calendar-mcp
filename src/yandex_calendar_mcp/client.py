"""Клиент Яндекс Календаря: обёртка над python-caldav с ретраями."""

from __future__ import annotations

import datetime
import functools
import logging
import re
import typing
import urllib.parse
import uuid

import backoff
from caldav.calendarobjectresource import CalendarObjectResource, Event, Todo
from caldav.collection import (
    Calendar,
    Principal,
    ScheduleInbox,
    ScheduleOutbox,
    SynchronizableCalendarObjectCollection,
)
from caldav.davclient import DAVClient
from caldav.elements import cdav, dav
from caldav.lib import error as caldav_error

from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.dto import (
    AvailabilityResult,
    CalendarInfo,
    ConnectionInfo,
    DayAgenda,
    EventCreateDto,
    EventInfo,
    EventUpdateDto,
    FreeSlot,
    InviteResponse,
    PeopleList,
    SyncResult,
    TodoCreateDto,
    TodoInfo,
    WriteResult,
)
from yandex_calendar_mcp.errors import (
    CalendarNotFoundError,
    EventConflictError,
    EventNotFoundError,
    InvalidEventError,
    SyncTokenError,
)
from yandex_calendar_mcp.freebusy import (
    SCHEDULE_DEFAULT_CALENDAR_PROPFIND,
    common_free__compute,
    freebusy_request__build,
    schedule_default_calendar__parse,
    schedule_response__parse,
)
from yandex_calendar_mcp.ical import (
    DateLike,
    calendar_id__from_url,
    date_like__to_date,
    date_like__to_datetime,
    dtstart__get,
    emails__unique,
    events__by_days,
    events__filter_by_text,
    events__from_icalendar,
    exdates__get,
    free_slots__between_events,
    vcalendar__apply_update,
    vcalendar__exclude_occurrence,
    vcalendar__master,
    vcalendar__set_partstat,
    vevent__build,
    vtodo__build,
    vtodo__mark_completed,
    vtodo__to_todo_info,
)
from yandex_calendar_mcp.people import people_search__build, people_search__parse

log = logging.getLogger(__name__)

PARTSTAT_BY_RESPONSE: dict[InviteResponse, str] = {
    'accept': 'ACCEPTED',
    'decline': 'DECLINED',
    'tentative': 'TENTATIVE',
}
ACTION_BY_RESPONSE: dict[InviteResponse, typing.Literal['accepted', 'declined', 'tentative']] = {
    'accept': 'accepted',
    'decline': 'declined',
    'tentative': 'tentative',
}

# Временные сбои: сеть, таймауты, 5xx, 429. Остальное падает сразу.
RETRYABLE_ERRORS = (
    caldav_error.RateLimitError,
    caldav_error.ResponseError,
    caldav_error.ReportError,
    caldav_error.PropfindError,
    # PUT с фиксированным uid и DELETE идемпотентны; 4xx отсекает giveup
    caldav_error.PutError,
    caldav_error.DeleteError,
    # ConnectionError и TimeoutError — подклассы OSError
    OSError,
)
# Яндекс держит события и задачи в коллекциях events-* и todos-*: запасной вариант, если сервер не отдал компоненты
COMPONENTS_BY_ID_PREFIX = {'events-': ['VEVENT'], 'todos-': ['VTODO']}


# Ошибки caldav не несут код ответа атрибутом, он есть только в тексте: "ReportError at '400 Bad Request'".
# Код ищется вместе с текстом статуса, иначе совпадёт любое число, например port=443 в сетевой ошибке.
HTTP_STATUS_PATTERN = re.compile(r'\b([1-5]\d\d) [A-Z][A-Za-z]')
# calendar-multiget за один запрос; 148 объектов Яндекс отдаёт примерно за 6 секунд
MULTIGET_CHUNK_SIZE = 100

ObjectT = typing.TypeVar('ObjectT', bound=CalendarObjectResource)


def http_status__from_error(exc: Exception) -> int | None:
    status = getattr(exc, 'status', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
    if isinstance(status, int):
        return status
    if not isinstance(exc, caldav_error.DAVError):
        return None
    match = HTTP_STATUS_PATTERN.search(str(exc))
    return int(match.group(1)) if match else None


def caldav_error__is_permanent(exc: Exception) -> bool:
    if isinstance(exc, (caldav_error.AuthorizationError, caldav_error.NotFoundError)):
        return True
    status = http_status__from_error(exc)
    return status is not None and 400 <= status < 500 and status != 429


retry_on_transient_error = backoff.on_exception(
    backoff.expo,
    RETRYABLE_ERRORS,
    max_tries=4,
    max_time=60,
    giveup=caldav_error__is_permanent,
    logger=log,
)


class YandexCalendarClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tz = settings.zoneinfo
        self.dav = DAVClient(
            url=str(settings.url),
            username=settings.email,
            password=settings.key.get_secret_value(),
            timeout=settings.timeout,
        )

    # ---------------------------------------------------------- подключение
    @functools.cached_property
    def principal(self) -> Principal:
        return self.principal__discover()

    @retry_on_transient_error
    def principal__discover(self) -> Principal:
        """Сначала автообнаружение; у Яндекса нет /.well-known/caldav, поэтому фолбэк на документированный путь."""
        try:
            # DAVClient.principal не аннотирован в caldav 3.4
            return typing.cast(Principal, self.dav.principal())  # type: ignore[no-untyped-call]
        except caldav_error.AuthorizationError:
            raise
        except caldav_error.DAVError as exc:
            log.info('principal auto-discovery failed (%s), using explicit path', type(exc).__name__)
            explicit_url = f'{str(self.settings.url).rstrip("/")}/principals/users/{self.settings.email}/'
            return Principal(client=self.dav, url=explicit_url)

    @functools.cached_property
    def calendars_by_id(self) -> dict[str, Calendar]:
        return {calendar_id__from_url(str(cal.url)): cal for cal in self.calendars__fetch()}

    @retry_on_transient_error
    def calendars__fetch(self) -> list[Calendar]:
        # caldav 3.4 типизирует sync-методы как `T | Coroutine[..., T]`, sync-клиент возвращает T
        return typing.cast(list[Calendar], self.principal.get_calendars())

    def calendar__get(self, calendar_id: str) -> Calendar:
        """Ищет по id, затем по отображаемому имени."""
        if calendar_id in self.calendars_by_id:
            return self.calendars_by_id[calendar_id]
        for cal in self.calendars_by_id.values():
            if cal.name == calendar_id:
                return cal
        raise CalendarNotFoundError(f'Календарь {calendar_id!r} не найден. Известные: {sorted(self.calendars_by_id)}')

    @functools.cached_property
    def components_by_calendar_id(self) -> dict[str, list[str]]:
        return {info.id: info.supported_components for info in self.calendars__list()}

    def calendars__for_query(self, calendar_id: str | None, *, component: str | None = None) -> list[Calendar]:
        """Без calendar_id — все календари, поддерживающие компонент.

        Яндекс на запрос VTODO к событийному календарю (и наоборот) отдаёт всю коллекцию целиком,
        а фильтрует уже клиент, поэтому неподходящие календари пропускаем заранее.
        """
        if calendar_id is not None:
            return [self.calendar__get(calendar_id)]
        return [
            cal
            for cal_id, cal in self.calendars_by_id.items()
            if component is None or component in self.components_by_calendar_id[cal_id]
        ]

    def connection_info(self) -> ConnectionInfo:
        return ConnectionInfo(
            login=self.settings.email,
            server=str(self.settings.url),
            principal_url=str(self.principal.url),
            calendars=self.calendars__list(),
            timezone=self.tz.key,
            mode=self.settings.mode,
        )

    # ---------------------------------------------------------------- чтение
    @retry_on_transient_error
    def calendar__components(self, cal: Calendar) -> list[str]:
        return list(typing.cast(list[str], cal.get_supported_components()) or [])

    def calendars__list(self) -> list[CalendarInfo]:
        result: list[CalendarInfo] = []
        for calendar_id, cal in self.calendars_by_id.items():
            try:
                components = self.calendar__components(cal)
            except caldav_error.AuthorizationError:
                raise
            except caldav_error.DAVError as exc:
                # Без этого календарь с временным сбоем выпал бы из всех запросов до рестарта: результат кешируется
                log.warning('supported components failed for %s: %s', calendar_id, exc)
                components = next(
                    (value for prefix, value in COMPONENTS_BY_ID_PREFIX.items() if calendar_id.startswith(prefix)), []
                )
            result.append(
                CalendarInfo(
                    id=calendar_id,
                    name=cal.name or calendar_id,
                    url=str(cal.url),
                    supported_components=components,
                )
            )
        return result

    @retry_on_transient_error
    def objects__search(self, cal: Calendar, **search_args: typing.Any) -> list[CalendarObjectResource]:
        return typing.cast(list[CalendarObjectResource], cal.search(**search_args))

    @retry_on_transient_error
    def object__by_uid(self, cal: Calendar, uid: str) -> Event:
        return typing.cast(Event, cal.get_event_by_uid(uid))

    @retry_on_transient_error
    def object__by_href(self, cal: Calendar, uid: str, comp_class: type[ObjectT]) -> ObjectT | None:
        """GET <календарь>/<uid>.ics. None, если по этому адресу ничего нет или лежит объект с другим UID.

        Яндекс кладёт объект по адресу <uid>.ics, а фильтр по UID в calendar-query игнорирует и отдаёт
        всю коллекцию: на живом календаре это 10 МБ и 15–30 с против 0,1–0,8 с у GET.
        Запрос идёт мимо obj.load(): тот на 5xx молча кладёт в data тело страницы ошибки, и ретрай не срабатывает.
        """
        url = f'{str(cal.url).rstrip("/")}/{urllib.parse.quote(uid, safe="")}.ics'
        try:
            response = self.dav.request(url)
        except caldav_error.AuthorizationError:
            # 403 на одном календаре (чужой слой) не должен обрывать поиск в остальных;
            # неверный пароль всё равно всплывёт из поиска по UID
            return None
        if response.status == 404:
            return None
        if response.status >= 400:
            # Текст в формате "502 Bad Gateway": по нему retry_on_transient_error отличает 5xx от 4xx
            raise caldav_error.ResponseError(f'GET {url}: {response.status} {response.reason}')
        obj = comp_class(client=self.dav, url=url, data=response.raw, parent=cal)
        if obj.id != uid:
            return None
        # ETag уходит в If-Match при сохранении: правку поверх чужой сервер отклонит с 412
        if etag := response.headers.get('ETag'):
            obj.props[dav.GetEtag.tag] = etag
        return obj

    def object__locate(
        self,
        uid: str,
        calendar_id: str | None,
        *,
        component: str,
        comp_class: type[ObjectT],
        by_search: typing.Callable[[Calendar, str], ObjectT],
        not_found: str,
    ) -> tuple[Calendar, ObjectT]:
        """GET по <uid>.ics во всех подходящих календарях; поиск по UID только в явно указанном календаре.

        Поиск нужен для объектов, которые сторонний клиент загрузил под другим именем файла. По всем календарям
        он не идёт: в каждом, где объекта нет, Яндекс отдаёт всю коллекцию, и промах стоит минуты.
        """
        calendars = self.calendars__for_query(calendar_id, component=component)
        for cal in calendars:
            obj = self.object__by_href(cal, uid, comp_class)
            if obj is not None:
                return cal, obj
        if calendar_id is None:
            raise EventNotFoundError(f'{not_found}. Если объект создан сторонним клиентом, укажите calendar_id')
        try:
            return calendars[0], by_search(calendars[0], uid)
        except caldav_error.NotFoundError:
            raise EventNotFoundError(not_found) from None

    def events__list(
        self,
        *,
        start: DateLike,
        end: DateLike,
        calendar_id: str | None = None,
        expand: bool = True,
        query: str | None = None,
    ) -> list[EventInfo]:
        start_dt = date_like__to_datetime(start, self.tz)
        end_dt = date_like__to_datetime(end, self.tz, end_of_day=True)
        events: list[EventInfo] = []
        for cal in self.calendars__for_query(calendar_id, component='VEVENT'):
            calendar_id_current = calendar_id__from_url(str(cal.url))
            for obj in self.objects__search(cal, event=True, start=start_dt, end=end_dt, expand=expand):
                events.extend(
                    events__from_icalendar(
                        obj.get_icalendar_instance(),
                        calendar_id=calendar_id_current,
                        url=str(obj.url) if obj.url else None,
                        tz=self.tz,
                    )
                )
        if query:
            events = events__filter_by_text(events, query)
        events.sort(key=lambda event: date_like__to_datetime(event.start, self.tz))
        return events

    def events__list_default_window(
        self,
        *,
        start: DateLike | None,
        end: DateLike | None,
        default_days: int,
        calendar_id: str | None = None,
        expand: bool = True,
        query: str | None = None,
    ) -> tuple[list[EventInfo], DateLike, DateLike]:
        """Окно по умолчанию: с сегодня на default_days. Возвращает события и фактические границы."""
        start = start if start is not None else datetime.datetime.now(self.tz).date()
        # Правая граница включается, поэтому default_days дней это start + default_days - 1
        end = end if end is not None else date_like__to_date(start) + datetime.timedelta(days=default_days - 1)
        events = self.events__list(start=start, end=end, calendar_id=calendar_id, expand=expand, query=query)
        return events, start, end

    def object__find(self, uid: str, *, calendar_id: str | None) -> tuple[Calendar, CalendarObjectResource]:
        return self.object__locate(
            uid,
            calendar_id,
            component='VEVENT',
            comp_class=Event,
            by_search=self.object__by_uid,
            not_found=f'Событие с uid {uid!r} не найдено',
        )

    def event__get(
        self, uid: str, *, calendar_id: str | None = None, occurrence: datetime.date | None = None
    ) -> EventInfo:
        """Без occurrence возвращает мастер-объект; для серии это первый экземпляр с RRULE.

        С occurrence возвращает экземпляр серии, начинающийся в этот день (с учётом переопределений).
        """
        cal, obj = self.object__find(uid, calendar_id=calendar_id)
        if occurrence is not None:
            return self.event__occurrence(cal, uid, occurrence)
        events = events__from_icalendar(
            obj.get_icalendar_instance(),
            calendar_id=calendar_id__from_url(str(cal.url)),
            url=str(obj.url) if obj.url else None,
            tz=self.tz,
        )
        if not events:
            raise EventNotFoundError(f'Объект {uid!r} не содержит VEVENT')
        return events[0]

    def event__occurrence(self, cal: Calendar, uid: str, occurrence: datetime.date) -> EventInfo:
        """Экземпляр события на дату через серверный expand за один день."""
        calendar_id_current = calendar_id__from_url(str(cal.url))
        for event in self.events__list(start=occurrence, end=occurrence, calendar_id=calendar_id_current, expand=True):
            if event.uid == uid and date_like__to_date(event.start) == occurrence:
                return event
        raise EventNotFoundError(f'У события {uid!r} нет экземпляра на {occurrence.isoformat()}')

    def event__get_raw(self, uid: str, *, calendar_id: str | None = None) -> str:
        _, obj = self.object__find(uid, calendar_id=calendar_id)
        return str(obj.data)

    def agenda__by_days(
        self, *, date_from: datetime.date, days: int, calendar_id: str | None = None
    ) -> list[DayAgenda]:
        end = date_from + datetime.timedelta(days=days - 1)
        events = self.events__list(start=date_from, end=end, calendar_id=calendar_id, expand=True)
        return events__by_days(events, date_from=date_from, days=days, tz=self.tz)

    def todos__list(self, *, calendar_id: str | None = None, include_completed: bool = False) -> list[TodoInfo]:
        todos: list[TodoInfo] = []
        for cal in self.calendars__for_query(calendar_id, component='VTODO'):
            calendar_id_current = calendar_id__from_url(str(cal.url))
            try:
                objects = self.objects__search(cal, todo=True, include_completed=include_completed)
            except caldav_error.AuthorizationError:
                raise
            except caldav_error.DAVError as exc:
                # Часть календарей Яндекса отвечает ошибкой на запрос VTODO: при обходе всех календарей пропускаем,
                # а явно запрошенный календарь должен вернуть ошибку, а не пустой список
                if calendar_id is not None:
                    raise
                log.debug('todo search failed for %s: %s', calendar_id_current, exc)
                continue
            todos.extend(
                vtodo__to_todo_info(obj.get_icalendar_component(), calendar_id=calendar_id_current, tz=self.tz)
                for obj in objects
            )
        return todos

    def free_slots__find(
        self,
        *,
        start: DateLike,
        end: DateLike,
        min_minutes: int,
        work_start: datetime.time,
        work_end: datetime.time,
        calendar_id: str | None = None,
    ) -> list[FreeSlot]:
        start_dt = date_like__to_datetime(start, self.tz)
        end_dt = date_like__to_datetime(end, self.tz, end_of_day=True)
        events = self.events__list(start=start_dt, end=end_dt, calendar_id=calendar_id, expand=True)
        return free_slots__between_events(
            events,
            own_email=self.settings.email,
            start=start_dt,
            end=end_dt,
            min_minutes=min_minutes,
            work_start=work_start,
            work_end=work_end,
            tz=self.tz,
        )

    # ---------------------------------------------------------- синхронизация
    @retry_on_transient_error
    def sync_token__current(self, cal: Calendar) -> str:
        """Токен через PROPFIND. sync-collection без токена на большом календаре Яндекс отбивает 507."""
        token = cal.get_property(dav.SyncToken())
        if not token:
            raise SyncTokenError(f'Календарь {calendar_id__from_url(str(cal.url))} не отдаёт sync-token')
        return str(token)

    @retry_on_transient_error
    def sync__changed_urls(self, cal: Calendar, sync_token: str) -> tuple[list[typing.Any], str]:
        """URL объектов, изменённых или удалённых после sync_token, и новый токен."""
        try:
            collection = typing.cast(
                SynchronizableCalendarObjectCollection,
                cal.get_objects_by_sync_token(sync_token, load_objects=False, disable_fallback=True),
            )
        except caldav_error.DAVError as exc:
            # RFC 6578 отвечает на чужой токен 403 (valid-sync-token), caldav поднимает его как AuthorizationError.
            # Авторизация к этому моменту уже прошла: календарь найден по PROPFIND
            if isinstance(exc, caldav_error.AuthorizationError) or http_status__from_error(exc) in (400, 409, 412):
                raise SyncTokenError(
                    'Сервер не принял sync_token: устарел или выдан для другого календаря. '
                    'Вызовите sync_changes без sync_token, чтобы получить новый.'
                ) from exc
            raise
        return [obj.url for obj in collection.objects], str(collection.sync_token)

    @retry_on_transient_error
    def objects__multiget(self, cal: Calendar, urls: list[typing.Any]) -> list[CalendarObjectResource]:
        return list(typing.cast(typing.Iterable[CalendarObjectResource], cal.multiget(urls)))

    def sync__changes(self, calendar_id: str, *, sync_token: str | None) -> SyncResult:
        """Без токена возвращает базовый токен. С токеном отдаёт изменения после него.

        Удалённым считается объект из дельты, для которого calendar-multiget не вернул данных:
        на удалённый URL Яндекс отвечает 404, а caldav всё равно создаёт объект с data=None.
        URL в ответе берётся из дельты: multiget отдаёт его с @ вместо %40, и он не совпал бы
        ни с deleted_urls, ни с url из list_events.
        """
        cal = self.calendar__get(calendar_id)
        calendar_id_current = calendar_id__from_url(str(cal.url))
        if sync_token is None:
            return SyncResult(calendar_id=calendar_id_current, sync_token=self.sync_token__current(cal), baseline=True)
        if sync_token.startswith('fake-'):
            # Такие токены caldav выдаёт в режиме эмуляции, по ним он молча выкачивает весь календарь
            raise SyncTokenError('Это не токен сервера. Вызовите sync_changes без sync_token, чтобы получить новый.')
        urls, new_token = self.sync__changed_urls(cal, sync_token)
        loaded = [
            obj
            for offset in range(0, len(urls), MULTIGET_CHUNK_SIZE)
            for obj in self.objects__multiget(cal, urls[offset : offset + MULTIGET_CHUNK_SIZE])
            if obj.data
        ]
        delta_url_by_canonical = {url.canonical(): url for url in urls}
        loaded_urls = {obj.url.canonical() for obj in loaded if obj.url is not None}
        events: list[EventInfo] = []
        todos: list[TodoInfo] = []
        for obj in loaded:
            url = delta_url_by_canonical.get(obj.url.canonical(), obj.url) if obj.url is not None else None
            ical = obj.get_icalendar_instance()
            events.extend(events__from_icalendar(ical, calendar_id=calendar_id_current, url=str(url), tz=self.tz))
            todos.extend(
                vtodo__to_todo_info(component, calendar_id=calendar_id_current, tz=self.tz)
                for component in ical.walk('VTODO')
            )
        return SyncResult(
            calendar_id=calendar_id_current,
            sync_token=new_token,
            baseline=False,
            events=events,
            todos=todos,
            deleted_urls=[str(url) for url in urls if url.canonical() not in loaded_urls],
        )

    # ------------------------------------------------------------ занятость
    @retry_on_transient_error
    def outbox__post(self, body: str) -> str:
        outbox = typing.cast(ScheduleOutbox, self.principal.schedule_outbox())
        outbox_url = str(outbox.url)
        response = self.dav.post(outbox_url, body, headers={'Content-Type': 'text/calendar; charset=utf-8'})
        if response.status != 200:
            raise caldav_error.ResponseError(f'schedule outbox answered {response.status}')
        raw = response.raw
        return raw if isinstance(raw, str) else raw.decode('utf-8', 'replace')

    def availability__check(
        self,
        *,
        attendees: list[str],
        start: datetime.datetime,
        end: datetime.datetime,
        min_minutes: int,
        work_start: datetime.time,
        work_end: datetime.time,
    ) -> AvailabilityResult:
        """Занятость участников через scheduling outbox: сервер отвечает VFREEBUSY по каждому адресу.

        Запрос без побочных эффектов: уведомления участникам не уходят. Свой адрес добавляется автоматически.
        """
        emails = emails__unique([self.settings.email, *attendees])
        body = freebusy_request__build(organizer=self.settings.email, attendees=emails, start=start, end=end)
        parsed = schedule_response__parse(self.outbox__post(body), tz=self.tz)
        return AvailabilityResult(
            start=start,
            end=end,
            timezone=self.tz.key,
            attendees=parsed,
            common_free=common_free__compute(
                parsed,
                start=start,
                end=end,
                min_minutes=min_minutes,
                work_start=work_start,
                work_end=work_end,
                tz=self.tz,
            ),
        )

    # ---------------------------------------------------------------- люди
    @retry_on_transient_error
    def people__search(self, query: str, *, limit: int) -> PeopleList:
        """Люди организации по подстроке имени или адреса одним REPORT по /principals/ (RFC 3744).

        Яндекс ищет по справочнику организации и отдаёт адрес в её основном домене.
        """
        url = f'{str(self.settings.url).rstrip("/")}/principals/'
        headers = {'Depth': '0', 'Content-Type': 'application/xml; charset=utf-8'}
        response = self.dav.request(url, 'REPORT', people_search__build(query), headers)
        if response.status != 207:
            # Текст в формате "502 Bad Gateway": по нему retry_on_transient_error отличает 5xx от 4xx
            raise caldav_error.ReportError(f'principal-property-search: {response.status} {response.reason}')
        raw = response.raw
        people = people_search__parse(raw if isinstance(raw, str) else raw.decode('utf-8', 'replace'))
        return PeopleList(people=people[:limit], count=len(people), truncated=len(people) > limit)

    # ---------------------------------------------------------------- запись
    @retry_on_transient_error
    def schedule_default_calendar__fetch(self) -> str | None:
        inbox = typing.cast(ScheduleInbox, self.principal.schedule_inbox())
        response = self.dav.propfind(str(inbox.url), SCHEDULE_DEFAULT_CALENDAR_PROPFIND, depth=0)
        if response.status != 207:
            raise caldav_error.PropfindError(f'schedule inbox answered {response.status}')
        raw = response.raw
        href = schedule_default_calendar__parse(raw if isinstance(raw, str) else raw.decode('utf-8', 'replace'))
        return calendar_id__from_url(href) if href is not None else None

    @functools.cached_property
    def default_event_calendar_id(self) -> str | None:
        """Основной календарь пользователя (schedule-default-calendar-URL). Первый в списке бывает чужим слоем."""
        try:
            return self.schedule_default_calendar__fetch()
        except caldav_error.AuthorizationError:
            raise
        except (caldav_error.DAVError, OSError) as exc:
            log.warning('default calendar lookup failed, using first VEVENT calendar: %s', exc)
            return None

    def calendar__for_write(self, calendar_id: str | None, *, component: str) -> Calendar:
        """Явный календарь, для события основной календарь пользователя, иначе первый с нужным компонентом.

        У Яндекса события и задачи живут в разных коллекциях (events-* только VEVENT, todos-* только VTODO).
        """
        if calendar_id is not None:
            return self.calendar__get(calendar_id)
        default_id = self.default_event_calendar_id if component == 'VEVENT' else None
        if default_id is not None and component in self.components_by_calendar_id.get(default_id, []):
            return self.calendars_by_id[default_id]
        for cal_id, components in self.components_by_calendar_id.items():
            if component in components:
                return self.calendars_by_id[cal_id]
        raise CalendarNotFoundError(f'Нет календаря с поддержкой {component}')

    @retry_on_transient_error
    def object__add(self, cal: Calendar, *, ical: str, todo: bool) -> CalendarObjectResource:
        """PUT с фиксированным uid: повтор после сбоя перезапишет тот же объект, а не создаст дубль."""
        if todo:
            return typing.cast(CalendarObjectResource, cal.add_todo(ical=ical))
        return typing.cast(CalendarObjectResource, cal.add_event(ical=ical))

    def object__save(self, obj: CalendarObjectResource, *, increase_seqno: bool = True) -> None:
        """PUT с If-Match, если у объекта есть ETag. Повтор после временного сбоя идёт уже без условия.

        Первый PUT мог дойти до сервера, а ответ потеряться: повтор с прежним ETag получил бы 412 на собственную
        правку. Без условия повтор записывает то же содержимое и остаётся идемпотентным.
        """
        try:
            self.object__put(obj, increase_seqno=increase_seqno)
        except RETRYABLE_ERRORS as exc:
            if caldav_error__is_permanent(exc):
                raise
            log.info('PUT %s failed (%s), retrying without If-Match', obj.url, type(exc).__name__)
            obj.props.pop(dav.GetEtag.tag, None)
            obj.props.pop(cdav.ScheduleTag.tag, None)
            # SEQUENCE уже поднят первой попыткой
            self.object__put_retrying(obj)

    def object__put(self, obj: CalendarObjectResource, *, increase_seqno: bool) -> None:
        # only_this_recurrence=False: объект уже содержит мастер и все переопределения, caldav не должен их сливать
        try:
            obj.save(increase_seqno=increase_seqno, only_this_recurrence=False)
        except (caldav_error.ETagMismatchError, caldav_error.ScheduleTagMismatchError) as exc:
            raise EventConflictError('Объект изменился на сервере, перечитайте его и повторите') from exc

    @retry_on_transient_error
    def object__put_retrying(self, obj: CalendarObjectResource) -> None:
        self.object__put(obj, increase_seqno=False)

    @retry_on_transient_error
    def object__delete(self, obj: CalendarObjectResource) -> None:
        obj.delete()

    def event__create(self, dto: EventCreateDto) -> WriteResult:
        cal = self.calendar__for_write(dto.calendar_id, component='VEVENT')
        uid = str(uuid.uuid4())
        ical = vevent__build(dto, uid=uid, tz=self.tz, organizer=self.settings.email).to_ical().decode()
        obj = self.object__add(cal, ical=ical, todo=False)
        return WriteResult(action='created', uid=uid, calendar_id=calendar_id__from_url(str(cal.url)), url=str(obj.url))

    def event__update(self, dto: EventUpdateDto) -> WriteResult:
        """Без recurrence_id меняет мастер, то есть всю серию. С ним только один экземпляр через переопределение."""
        cal, obj = self.object__find(dto.uid, calendar_id=dto.calendar_id)
        with obj.edit_icalendar_instance() as calendar:
            master = vcalendar__master(calendar)
            old_start = dtstart__get(master)
            vcalendar__apply_update(calendar, dto, self.tz, organizer=self.settings.email)
            if dto.recurrence_id is None and dtstart__get(master) != old_start and exdates__get(master):
                # Проверено на живом аккаунте: при переносе серии Яндекс выбрасывает все EXDATE, удалённые экземпляры
                # воскресают, а повторный PUT с EXDATE он отбивает 504. Отказываем до записи, объект не меняется
                raise InvalidEventError(
                    'У серии есть удалённые экземпляры: при переносе времени серии Яндекс вернёт их обратно '
                    'и не даст удалить снова. Перенесите серию в веб-интерфейсе Яндекс Календаря.'
                )
        self.object__save(obj)
        return WriteResult(
            action='updated',
            uid=dto.uid,
            calendar_id=calendar_id__from_url(str(cal.url)),
            url=str(obj.url),
            recurrence_id=dto.recurrence_id,
        )

    def event__delete(
        self, uid: str, *, calendar_id: str | None = None, recurrence_id: DateLike | None = None
    ) -> WriteResult:
        """Без recurrence_id удаляет объект целиком, с ним исключает один экземпляр серии через EXDATE."""
        cal, obj = self.object__find(uid, calendar_id=calendar_id)
        if recurrence_id is None:
            self.object__delete(obj)
            url = None
        else:
            with obj.edit_icalendar_instance() as calendar:
                vcalendar__exclude_occurrence(calendar, recurrence_id, self.tz)
            self.object__save(obj)
            url = str(obj.url)
        return WriteResult(
            action='deleted',
            uid=uid,
            calendar_id=calendar_id__from_url(str(cal.url)),
            url=url,
            recurrence_id=recurrence_id,
        )

    def invite__respond(self, uid: str, *, response: InviteResponse, calendar_id: str | None = None) -> WriteResult:
        """PARTSTAT своего адреса во всех VEVENT объекта и PUT. Ответ организатору рассылает сервер (auto-schedule).

        accept_invite из caldav не подходит: он ждёт объект из schedule-inbox, заново ищет principal,
        который на Яндексе не находится автоматически, и меняет только первый VEVENT серии.
        """
        cal, obj = self.object__find(uid, calendar_id=calendar_id)
        with obj.edit_icalendar_instance() as calendar:
            vcalendar__set_partstat(calendar, self.settings.email, PARTSTAT_BY_RESPONSE[response])
        # SEQUENCE меняет только организатор
        self.object__save(obj, increase_seqno=False)
        return WriteResult(
            action=ACTION_BY_RESPONSE[response],
            uid=uid,
            calendar_id=calendar_id__from_url(str(cal.url)),
            url=str(obj.url),
        )

    @retry_on_transient_error
    def todo__by_uid(self, cal: Calendar, uid: str) -> Todo:
        return typing.cast(Todo, cal.get_todo_by_uid(uid))

    def todo__find(self, uid: str, *, calendar_id: str | None) -> tuple[Calendar, Todo]:
        return self.object__locate(
            uid,
            calendar_id,
            component='VTODO',
            comp_class=Todo,
            by_search=self.todo__by_uid,
            not_found=f'Задача с uid {uid!r} не найдена',
        )

    def todo__create(self, dto: TodoCreateDto) -> WriteResult:
        cal = self.calendar__for_write(dto.calendar_id, component='VTODO')
        uid = str(uuid.uuid4())
        ical = vtodo__build(dto, uid=uid, tz=self.tz).to_ical().decode()
        obj = self.object__add(cal, ical=ical, todo=True)
        return WriteResult(action='created', uid=uid, calendar_id=calendar_id__from_url(str(cal.url)), url=str(obj.url))

    def todo__complete(self, uid: str, *, calendar_id: str | None = None) -> WriteResult:
        """Идемпотентно: уже выполненная задача не трогается. Todo.complete из caldav падает на ней голым assert."""
        cal, todo = self.todo__find(uid, calendar_id=calendar_id)
        with todo.edit_icalendar_component() as component:
            changed = vtodo__mark_completed(component)
        if changed:
            self.object__save(todo)
        return WriteResult(
            action='completed', uid=uid, calendar_id=calendar_id__from_url(str(cal.url)), url=str(todo.url)
        )

    def todo__delete(self, uid: str, *, calendar_id: str | None = None) -> WriteResult:
        cal, todo = self.todo__find(uid, calendar_id=calendar_id)
        self.object__delete(todo)
        return WriteResult(action='deleted', uid=uid, calendar_id=calendar_id__from_url(str(cal.url)), url=None)
