"""Чистые преобразования iCalendar-компонентов в DTO и расчёты по датам. Без сети и состояния."""

from __future__ import annotations

import copy
import datetime
import typing
import zoneinfo

import icalendar
import recurring_ical_events

from yandex_calendar_mcp.dto import (
    Attendee,
    DayAgenda,
    EventCreateDto,
    EventInfo,
    EventUpdateDto,
    FreeSlot,
    TodoCreateDto,
    TodoInfo,
)
from yandex_calendar_mcp.errors import InvalidEventError

type DateLike = datetime.datetime | datetime.date

PRODID = '-//yandex-calendar-mcp//RU'


def calendar_id__from_url(url: str) -> str:
    return url.rstrip('/').rsplit('/', 1)[-1]


def ical_value__to_timezone(value: typing.Any, tz: zoneinfo.ZoneInfo) -> DateLike | None:
    """Приводит значение DTSTART/DTEND/DUE и подобных к таймзоне пользователя. Даты без времени не трогает."""
    if value is None:
        return None
    raw = getattr(value, 'dt', value)
    if isinstance(raw, datetime.datetime):
        if raw.tzinfo is None:
            # Плавающее время по RFC 5545 трактуем как локальное
            raw = raw.replace(tzinfo=tz)
        return raw.astimezone(tz)
    if isinstance(raw, datetime.date):
        return raw
    return None


def ical_text__get(component: icalendar.Component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def mailto__strip(value: typing.Any) -> str:
    text = str(value)
    return text[7:] if text.lower().startswith('mailto:') else text


def attendees__raw(component: icalendar.Component) -> list[icalendar.vCalAddress]:
    """ATTENDEE всегда списком: icalendar отдаёт одного участника значением, нескольких списком."""
    raw = component.get('ATTENDEE')
    if raw is None:
        return []
    return raw if isinstance(raw, list) else [raw]


def emails__unique(emails: list[str]) -> list[str]:
    """Адреса без повторов в исходном порядке. Регистр в e-mail не различается."""
    seen: set[str] = set()
    result: list[str] = []
    for email in emails:
        if email.casefold() not in seen:
            seen.add(email.casefold())
            result.append(email)
    return result


def attendees__from_vevent(component: icalendar.Component) -> list[Attendee]:
    return [
        Attendee(
            email=mailto__strip(item),
            name=item.params.get('CN'),
            status=item.params.get('PARTSTAT'),
            role=item.params.get('ROLE'),
        )
        for item in attendees__raw(component)
    ]


def vevent__to_event_info(
    component: icalendar.Component,
    *,
    calendar_id: str,
    url: str | None,
    tz: zoneinfo.ZoneInfo,
) -> EventInfo | None:
    """Разбирает один VEVENT. Возвращает None, если нет DTSTART (битый объект)."""
    dtstart = component.get('DTSTART')
    if dtstart is None:
        return None
    start = ical_value__to_timezone(dtstart, tz)
    if start is None:
        return None
    end = ical_value__to_timezone(component.get('DTEND'), tz)
    duration = component.get('DURATION')
    if end is None and duration is not None:
        end = start + duration.dt
    organizer = component.get('ORGANIZER')
    last_modified = ical_value__to_timezone(component.get('LAST-MODIFIED'), tz)
    return EventInfo(
        uid=ical_text__get(component, 'UID') or '',
        calendar_id=calendar_id,
        summary=ical_text__get(component, 'SUMMARY'),
        description=ical_text__get(component, 'DESCRIPTION'),
        location=ical_text__get(component, 'LOCATION'),
        start=start,
        end=end,
        all_day=not isinstance(dtstart.dt, datetime.datetime),
        status=ical_text__get(component, 'STATUS'),
        organizer=mailto__strip(organizer) if organizer is not None else None,
        attendees=attendees__from_vevent(component),
        recurring=component.get('RRULE') is not None or component.get('RECURRENCE-ID') is not None,
        transparent=ical_text__get(component, 'TRANSP') == 'TRANSPARENT',
        recurrence_id=ical_value__to_timezone(component.get('RECURRENCE-ID'), tz),
        conference_url=ical_text__get(component, 'X-TELEMOST-CONFERENCE') or ical_text__get(component, 'CONFERENCE'),
        web_url=ical_text__get(component, 'URL'),
        url=url,
        last_modified=last_modified if isinstance(last_modified, datetime.datetime) else None,
    )


def events__from_icalendar(
    ical: icalendar.Calendar,
    *,
    calendar_id: str,
    url: str | None,
    tz: zoneinfo.ZoneInfo,
) -> list[EventInfo]:
    """Один CalDAV-объект может содержать несколько VEVENT: мастер и переопределения, либо раскрытые экземпляры."""
    events = (
        vevent__to_event_info(component, calendar_id=calendar_id, url=url, tz=tz) for component in ical.walk('VEVENT')
    )
    return [event for event in events if event is not None]


def vtodo__to_todo_info(component: icalendar.Component, *, calendar_id: str, tz: zoneinfo.ZoneInfo) -> TodoInfo:
    completed = ical_value__to_timezone(component.get('COMPLETED'), tz)
    percent = component.get('PERCENT-COMPLETE')
    priority = component.get('PRIORITY')
    return TodoInfo(
        uid=ical_text__get(component, 'UID') or '',
        calendar_id=calendar_id,
        summary=ical_text__get(component, 'SUMMARY'),
        description=ical_text__get(component, 'DESCRIPTION'),
        due=ical_value__to_timezone(component.get('DUE'), tz),
        status=ical_text__get(component, 'STATUS'),
        completed=completed if isinstance(completed, datetime.datetime) else None,
        percent_complete=int(percent) if percent is not None else None,
        priority=int(priority) if priority is not None else None,
    )


def events__filter_by_text(events: list[EventInfo], query: str) -> list[EventInfo]:
    needle = query.casefold()
    return [
        event
        for event in events
        if any(needle in (field or '').casefold() for field in (event.summary, event.description, event.location))
    ]


def date_like__to_datetime(value: DateLike, tz: zoneinfo.ZoneInfo, *, end_of_day: bool = False) -> datetime.datetime:
    """Дата без времени становится началом дня (или началом следующего дня для правой границы)."""
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=tz)
    base = datetime.datetime.combine(value, datetime.time.min, tzinfo=tz)
    return base + datetime.timedelta(days=1) if end_of_day else base


def date_like__to_date(value: DateLike) -> datetime.date:
    return value.date() if isinstance(value, datetime.datetime) else value


def event__blocks_time(event: EventInfo, own_email: str) -> bool:
    """Занимает ли событие время: отменённые, прозрачные и отклонённые мной встречи не занимают."""
    if event.status == 'CANCELLED' or event.transparent:
        return False
    return not any(a.email.casefold() == own_email.casefold() and a.status == 'DECLINED' for a in event.attendees)


def free_slots__between_events(
    events: list[EventInfo],
    *,
    own_email: str,
    start: datetime.datetime,
    end: datetime.datetime,
    min_minutes: int,
    work_start: datetime.time,
    work_end: datetime.time,
    tz: zoneinfo.ZoneInfo,
) -> list[FreeSlot]:
    """Окна между занятыми событиями, обрезанные по рабочим часам каждого дня. События на весь день не мешают."""
    busy: list[tuple[datetime.datetime, datetime.datetime]] = []
    for event in events:
        if event.all_day or not isinstance(event.start, datetime.datetime) or not event__blocks_time(event, own_email):
            continue
        event_end = event.end if isinstance(event.end, datetime.datetime) else event.start + datetime.timedelta(hours=1)
        busy.append((event.start, event_end))
    return free_slots__between_busy(
        busy, start=start, end=end, min_minutes=min_minutes, work_start=work_start, work_end=work_end, tz=tz
    )


def free_slots__between_busy(
    busy: list[tuple[datetime.datetime, datetime.datetime]],
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    min_minutes: int,
    work_start: datetime.time,
    work_end: datetime.time,
    tz: zoneinfo.ZoneInfo,
) -> list[FreeSlot]:
    busy = sorted(busy)
    slots: list[FreeSlot] = []
    day = start.date()
    while day <= end.date():
        window_start = max(start, datetime.datetime.combine(day, work_start, tzinfo=tz))
        window_end = min(end, datetime.datetime.combine(day, work_end, tzinfo=tz))
        cursor = window_start
        for busy_start, busy_end in busy:
            if busy_end <= cursor or busy_start >= window_end:
                continue
            slots.extend(free_slot__build(cursor, busy_start, min_minutes))
            cursor = max(cursor, busy_end)
        slots.extend(free_slot__build(cursor, window_end, min_minutes))
        day += datetime.timedelta(days=1)
    return slots


def free_slot__build(start: datetime.datetime, end: datetime.datetime, min_minutes: int) -> list[FreeSlot]:
    """Пустой список, если окно короче min_minutes."""
    minutes = int((end - start).total_seconds() // 60)
    if minutes < min_minutes:
        return []
    return [FreeSlot(start=start, end=end, minutes=minutes)]


def date_like__localize(value: DateLike, tz: zoneinfo.ZoneInfo) -> DateLike:
    """Наивное дата-время считается локальным для пользователя. Дата остаётся датой."""
    if isinstance(value, datetime.datetime) and value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value


def date_like__for_ical(value: DateLike, tz: zoneinfo.ZoneInfo) -> DateLike:
    """Дата-время уходит на сервер в таймзоне tz (TZID), дата остаётся датой.

    UTC не годится: серия в зоне с переходом на летнее время уехала бы на час после перехода.
    """
    localized = date_like__localize(value, tz)
    if isinstance(localized, datetime.datetime):
        return localized.astimezone(tz)
    return localized


def component__zone(component: icalendar.Component, tz: zoneinfo.ZoneInfo) -> zoneinfo.ZoneInfo:
    """IANA-таймзона DTSTART компонента, иначе таймзона пользователя. Правка не меняет зону события."""
    raw = getattr(component.get('DTSTART'), 'dt', None)
    if isinstance(raw, datetime.datetime) and isinstance(raw.tzinfo, zoneinfo.ZoneInfo) and raw.tzinfo.key != 'UTC':
        return raw.tzinfo
    return tz


def event_end__resolve(start: DateLike, end: DateLike | None, duration_minutes: int | None) -> DateLike:
    """Конец события: явный end, иначе длительность, иначе час (или день для всего дня)."""
    if end is not None:
        if type(end) is not type(start):
            raise InvalidEventError('start и end должны быть одного типа: обе даты или оба дата-время.')
        if end <= start:
            if not isinstance(start, datetime.datetime):
                raise InvalidEventError(
                    'Для события на весь день end не включается: один день 2026-10-10 это end=2026-10-11.'
                )
            raise InvalidEventError('end должен быть позже start.')
        return end
    if isinstance(start, datetime.datetime):
        return start + datetime.timedelta(minutes=duration_minutes or 60)
    return start + datetime.timedelta(days=1)


def attendee__build(email: str) -> icalendar.vCalAddress:
    address = icalendar.vCalAddress(f'mailto:{email}')
    address.params['PARTSTAT'] = 'NEEDS-ACTION'
    address.params['ROLE'] = 'REQ-PARTICIPANT'
    address.params['RSVP'] = 'TRUE'
    return address


def organizer__require_own(component: icalendar.Component, organizer: str) -> None:
    """Участников меняет только организатор: он и рассылает приглашения. Событие без ORGANIZER считается своим."""
    current = component.get('ORGANIZER')
    if current is not None and mailto__strip(current).casefold() != organizer.casefold():
        raise InvalidEventError(f'Организатор встречи {mailto__strip(current)}, менять участников может только он.')


def vevent__add_attendees(component: icalendar.Component, emails: list[str], *, organizer: str) -> None:
    """Добавляет участников, которых ещё нет в событии.

    Без ORGANIZER Яндекс считает объект личным, а не встречей, и молча выбрасывает все ATTENDEE.
    Поэтому организатором ставится свой адрес.
    """
    organizer__require_own(component, organizer)
    if component.get('ORGANIZER') is None:
        component.add('ORGANIZER', icalendar.vCalAddress(f'mailto:{organizer}'))
    present = [mailto__strip(attendee) for attendee in attendees__raw(component)]
    for email in emails__unique([*present, *emails])[len(present) :]:
        component.add('ATTENDEE', attendee__build(email))


def vevent__remove_attendees(component: icalendar.Component, emails: list[str], *, organizer: str) -> None:
    """Убирает участников по e-mail, отсутствующих пропускает. Отмену им рассылает сервер (auto-schedule)."""
    organizer__require_own(component, organizer)
    removed = {email.casefold() for email in emails}
    kept = [attendee for attendee in attendees__raw(component) if mailto__strip(attendee).casefold() not in removed]
    component.pop('ATTENDEE', None)
    for attendee in kept:
        component.add('ATTENDEE', attendee)


def vcalendar__wrap(component: icalendar.Component) -> icalendar.Calendar:
    calendar = icalendar.Calendar()
    calendar.add('PRODID', PRODID)
    calendar.add('VERSION', '2.0')
    calendar.add_component(component)
    calendar.add_missing_timezones()
    return calendar


def vevent__build(dto: EventCreateDto, *, uid: str, tz: zoneinfo.ZoneInfo, organizer: str) -> icalendar.Calendar:
    start_local = date_like__localize(dto.start, tz)
    end_local = date_like__localize(dto.end, tz) if dto.end is not None else None
    start = date_like__for_ical(start_local, tz)
    end = date_like__for_ical(event_end__resolve(start_local, end_local, dto.duration_minutes), tz)
    event = icalendar.Event()
    event.add('UID', uid)
    event.add('DTSTAMP', datetime.datetime.now(datetime.UTC))
    event.add('SUMMARY', dto.summary)
    event.add('DTSTART', start)
    event.add('DTEND', end)
    if dto.description:
        event.add('DESCRIPTION', dto.description)
    if dto.location:
        event.add('LOCATION', dto.location)
    if dto.attendees:
        vevent__add_attendees(event, dto.attendees, organizer=organizer)
    if dto.rrule:
        event.add('RRULE', icalendar.vRecur.from_ical(dto.rrule))
    return vcalendar__wrap(event)


def vevent__apply_update(component: icalendar.Component, dto: EventUpdateDto, tz: zoneinfo.ZoneInfo) -> None:
    """Меняет только переданные поля прямо в компоненте. Даты пишутся в зоне события, DURATION заменяется на DTEND."""
    text_fields = {
        'SUMMARY': dto.summary,
        'DESCRIPTION': dto.description,
        'LOCATION': dto.location,
        'STATUS': dto.status,
    }
    for key, value in text_fields.items():
        if value is not None:
            component[key] = value
    if dto.start is None and dto.end is None:
        return
    zone = component__zone(component, tz)
    current_start = ical_value__to_timezone(component.get('DTSTART'), zone)
    if current_start is None:
        raise InvalidEventError('У события нет DTSTART, обновить даты нельзя.')
    current_end = ical_value__to_timezone(component.get('DTEND'), zone)
    duration = component.get('DURATION')
    if current_end is None and duration is not None:
        current_end = current_start + duration.dt
    start = date_like__for_ical(dto.start, zone) if dto.start is not None else current_start
    if dto.end is not None:
        end = event_end__resolve(start, date_like__for_ical(dto.end, zone), None)
    elif current_end is not None and type(current_end) is type(start):
        # Перенос только начала сохраняет длительность события
        end = start + (current_end - current_start)
    else:
        end = event_end__resolve(start, None, None)
    for key in ('DTSTART', 'DTEND', 'DURATION'):
        component.pop(key, None)
    component.add('DTSTART', start)
    component.add('DTEND', end)


def vtodo__build(dto: TodoCreateDto, *, uid: str, tz: zoneinfo.ZoneInfo) -> icalendar.Calendar:
    todo = icalendar.Todo()
    todo.add('UID', uid)
    todo.add('DTSTAMP', datetime.datetime.now(datetime.UTC))
    todo.add('SUMMARY', dto.summary)
    todo.add('STATUS', 'NEEDS-ACTION')
    if dto.due is not None:
        todo.add('DUE', date_like__for_ical(dto.due, tz))
    if dto.description:
        todo.add('DESCRIPTION', dto.description)
    if dto.priority is not None:
        todo.add('PRIORITY', dto.priority)
    return vcalendar__wrap(todo)


# Свойства, которые описывают серию целиком и не переносятся в переопределение экземпляра
SERIES_ONLY_PROPERTIES = ('RRULE', 'RDATE', 'EXDATE', 'EXRULE', 'DTSTART', 'DTEND', 'DURATION', 'RECURRENCE-ID')


def dtstart__get(component: icalendar.Component) -> DateLike:
    """DTSTART как есть, в таймзоне из TZID."""
    value = component.get('DTSTART')
    if value is None:
        raise InvalidEventError('У события нет DTSTART.')
    return typing.cast(DateLike, value.dt)


def vcalendar__master(calendar: icalendar.Calendar) -> icalendar.Component:
    """Мастер-VEVENT объекта: первый без RECURRENCE-ID."""
    for component in calendar.walk('VEVENT'):
        if component.get('RECURRENCE-ID') is None:
            return component
    raise InvalidEventError('В объекте нет мастер-события, только переопределения экземпляров.')


def recurrence_id__normalize(value: DateLike, master: icalendar.Component, tz: zoneinfo.ZoneInfo) -> DateLike:
    """Приводит recurrence_id к типу и таймзоне DTSTART мастера, как его ждёт сервер в RECURRENCE-ID."""
    master_start = dtstart__get(master)
    if not isinstance(master_start, datetime.datetime):
        if isinstance(value, datetime.datetime):
            raise InvalidEventError('Серия на весь день: recurrence_id должен быть датой без времени.')
        return value
    if not isinstance(value, datetime.datetime):
        raise InvalidEventError('Серия со временем: recurrence_id должен содержать время, возьмите его из list_events.')
    localized = value if value.tzinfo else value.replace(tzinfo=tz)
    if master_start.tzinfo is None:
        # Плавающее время мастера: RECURRENCE-ID тоже без таймзоны, в локальном времени пользователя
        return localized.astimezone(tz).replace(tzinfo=None)
    return localized.astimezone(master_start.tzinfo)


def vcalendar__override_find(
    calendar: icalendar.Calendar, recurrence_id: DateLike, tz: zoneinfo.ZoneInfo
) -> icalendar.Component | None:
    for component in calendar.walk('VEVENT'):
        current = ical_value__to_timezone(component.get('RECURRENCE-ID'), tz)
        if current is not None and current == ical_value__to_timezone(recurrence_id, tz):
            return component
    return None


def series__has_occurrence(calendar: icalendar.Calendar, recurrence_id: DateLike, tz: zoneinfo.ZoneInfo) -> bool:
    """Есть ли у серии экземпляр с таким исходным началом. EXDATE уже исключены.

    Раскрывается только мастер: с переопределениями recurring_ical_events отдал бы перенесённый экземпляр
    с новым DTSTART, и его новое время ошибочно сошло бы за исходное.
    """
    series = icalendar.Calendar()
    for component in calendar.subcomponents:
        if component.name == 'VTIMEZONE':
            series.add_component(component)
    series.add_component(vcalendar__master(calendar))
    if isinstance(recurrence_id, datetime.datetime):
        moment = date_like__to_datetime(recurrence_id, tz).astimezone(tz)
        occurrences = recurring_ical_events.of(series).between(moment, moment + datetime.timedelta(minutes=1))
        return any(ical_value__to_timezone(item.get('DTSTART'), tz) == moment for item in occurrences)
    occurrences = recurring_ical_events.of(series).at(recurrence_id)
    return any(dtstart__get(item) == recurrence_id for item in occurrences)


def vcalendar__occurrence_for_edit(
    calendar: icalendar.Calendar, recurrence_id: DateLike, tz: zoneinfo.ZoneInfo
) -> icalendar.Component:
    """VEVENT одного экземпляра серии для правки. Если переопределения ещё нет, создаёт его из мастера."""
    master = vcalendar__master(calendar)
    if master.get('RRULE') is None and master.get('RDATE') is None:
        raise InvalidEventError('Событие не повторяющееся, recurrence_id не нужен.')
    normalized = recurrence_id__normalize(recurrence_id, master, tz)
    existing = vcalendar__override_find(calendar, normalized, tz)
    if existing is not None:
        return existing
    if not series__has_occurrence(calendar, normalized, tz):
        raise InvalidEventError(f'У серии нет экземпляра с recurrence_id {recurrence_id}. Возьмите его из list_events.')
    master_start = dtstart__get(master)
    master_end = master.get('DTEND')
    duration = master_end.dt - master_start if master_end is not None else master.get('DURATION')
    override = copy.deepcopy(master)
    for key in SERIES_ONLY_PROPERTIES:
        override.pop(key, None)
    override.add('RECURRENCE-ID', normalized)
    override.add('DTSTART', normalized)
    if duration is not None:
        override.add('DTEND', normalized + getattr(duration, 'dt', duration))
    calendar.add_component(override)
    return override


def vcalendar__exclude_occurrence(calendar: icalendar.Calendar, recurrence_id: DateLike, tz: zoneinfo.ZoneInfo) -> None:
    """Удаляет один экземпляр серии: EXDATE в мастере и переопределение, если оно было."""
    master = vcalendar__master(calendar)
    if master.get('RRULE') is None and master.get('RDATE') is None:
        raise InvalidEventError('Событие не повторяющееся, recurrence_id не нужен.')
    normalized = recurrence_id__normalize(recurrence_id, master, tz)
    existing = vcalendar__override_find(calendar, normalized, tz)
    if existing is None and not series__has_occurrence(calendar, normalized, tz):
        raise InvalidEventError(f'У серии нет экземпляра с recurrence_id {recurrence_id}. Возьмите его из list_events.')
    if existing is not None:
        calendar.subcomponents.remove(existing)
    master.add('EXDATE', normalized)


def exdates__get(master: icalendar.Component) -> list[DateLike]:
    raw = master.get('EXDATE')
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [typing.cast(DateLike, value.dt) for item in items for value in item.dts]


def occurrence__shift(value: DateLike, old_start: DateLike, new_start: DateLike) -> DateLike:
    """Сдвигает исходное начало экземпляра так же, как сдвинулся DTSTART мастера, в локальном времени серии."""
    if not isinstance(value, datetime.datetime):
        return value + (date_like__to_date(new_start) - date_like__to_date(old_start))
    if not isinstance(old_start, datetime.datetime) or not isinstance(new_start, datetime.datetime):
        raise InvalidEventError('У серии со временем начало тоже должно быть дата-временем.')
    zone = new_start.tzinfo
    old_local = old_start.astimezone(zone) if zone is not None and old_start.tzinfo is not None else old_start
    local = value.astimezone(zone) if zone is not None and value.tzinfo is not None else value
    delta = new_start.replace(tzinfo=None) - old_local.replace(tzinfo=None)
    return (local.replace(tzinfo=None) + delta).replace(tzinfo=zone)


def series__shift(calendar: icalendar.Calendar, old_start: DateLike, new_start: DateLike) -> None:
    """После переноса мастера сдвигает RECURRENCE-ID переопределений и EXDATE, иначе они осиротеют:
    удалённые экземпляры вернутся, перенесённые задвоятся."""
    master = vcalendar__master(calendar)
    if old_start == new_start or (master.get('RRULE') is None and master.get('RDATE') is None):
        return
    overrides = [c for c in calendar.walk('VEVENT') if c.get('RECURRENCE-ID') is not None]
    exdates = exdates__get(master)
    if not overrides and not exdates:
        return
    if type(old_start) is not type(new_start):
        raise InvalidEventError(
            'Нельзя перевести серию между «весь день» и «со временем», пока у неё есть перенесённые '
            'или удалённые экземпляры.'
        )
    for override in overrides:
        recurrence_id = typing.cast(DateLike, override.get('RECURRENCE-ID').dt)
        override.pop('RECURRENCE-ID')
        override.add('RECURRENCE-ID', occurrence__shift(recurrence_id, old_start, new_start))
    master.pop('EXDATE', None)
    for exdate in exdates:
        master.add('EXDATE', occurrence__shift(exdate, old_start, new_start))


def sequence__increment(component: icalendar.Component) -> None:
    sequence = int(component.get('SEQUENCE', 0))
    component.pop('SEQUENCE', None)
    component.add('SEQUENCE', sequence + 1)


def vcalendar__apply_update(
    calendar: icalendar.Calendar, dto: EventUpdateDto, tz: zoneinfo.ZoneInfo, *, organizer: str
) -> None:
    """Правка объекта: без recurrence_id мастер (вся серия), с ним один экземпляр через переопределение.

    SEQUENCE мастера поднимает caldav при сохранении, SEQUENCE переопределений поднимаем сами,
    иначе участники по RFC 5546 могут не принять обновление экземпляра. Правка серии меняет и переопределения:
    перенос сдвигает их RECURRENCE-ID, а участники у каждого экземпляра свои.
    """
    if dto.recurrence_id is not None:
        override = vcalendar__occurrence_for_edit(calendar, dto.recurrence_id, tz)
        vevent__apply_update(override, dto, tz)
        sequence__increment(override)
        edited = [override]
    else:
        master = vcalendar__master(calendar)
        old_start = dtstart__get(master)
        vevent__apply_update(master, dto, tz)
        series__shift(calendar, old_start, dtstart__get(master))
        overrides = [c for c in calendar.walk('VEVENT') if c.get('RECURRENCE-ID') is not None]
        if dtstart__get(master) != old_start or dto.add_attendees or dto.remove_attendees:
            for override in overrides:
                sequence__increment(override)
        edited = [master, *overrides]
    for component in edited:
        if dto.remove_attendees:
            vevent__remove_attendees(component, dto.remove_attendees, organizer=organizer)
        if dto.add_attendees:
            vevent__add_attendees(component, dto.add_attendees, organizer=organizer)
    calendar.add_missing_timezones()


def vcalendar__set_partstat(calendar: icalendar.Calendar, email: str, partstat: str) -> None:
    """Ставит PARTSTAT участнику email во всех VEVENT объекта, включая переопределения экземпляров."""
    found = False
    for component in calendar.walk('VEVENT'):
        for attendee in attendees__raw(component):
            if mailto__strip(attendee).casefold() == email.casefold():
                attendee.params['PARTSTAT'] = partstat
                attendee.params.pop('RSVP', None)
                found = True
    if not found:
        raise InvalidEventError(f'{email} не участник этого события, отвечать на приглашение нечего.')


def vtodo__mark_completed(component: icalendar.Component) -> bool:
    """False, если задача уже выполнена: повторный вызов не ошибка."""
    if str(component.get('STATUS', '')).upper() == 'COMPLETED' or component.get('COMPLETED') is not None:
        return False
    for key in ('STATUS', 'COMPLETED', 'PERCENT-COMPLETE'):
        component.pop(key, None)
    component.add('STATUS', 'COMPLETED')
    component.add('COMPLETED', datetime.datetime.now(datetime.UTC))
    component.add('PERCENT-COMPLETE', 100)
    return True


def events__by_days(
    events: list[EventInfo], *, date_from: datetime.date, days: int, tz: zoneinfo.ZoneInfo
) -> list[DayAgenda]:
    """Раскладывает события по дням окна. Многодневное и ночное событие попадает в каждый свой день."""
    window = [date_from + datetime.timedelta(days=offset) for offset in range(days)]
    grouped: dict[datetime.date, list[EventInfo]] = {day: [] for day in window}
    for event in events:
        first = date_like__to_date(ical_value__to_timezone(event.start, tz) or event.start)
        last = first
        if isinstance(event.end, datetime.datetime):
            # Конец не включается: событие до 00:00 не занимает следующий день
            last = max(first, (event.end.astimezone(tz) - datetime.timedelta(microseconds=1)).date())
        elif event.end is not None:
            last = max(first, event.end - datetime.timedelta(days=1))
        for day in window:
            if first <= day <= last:
                grouped[day].append(event)
    return [DayAgenda(date=day, events=grouped[day]) for day in window]
