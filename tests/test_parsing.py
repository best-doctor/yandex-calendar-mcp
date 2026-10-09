"""Офлайн-тесты: iCalendar → DTO, фильтр по тексту, расчёт свободных окон. Без сети."""

from __future__ import annotations

import datetime
import types
import typing
import zoneinfo
from unittest import mock

import icalendar
import pydantic
import pytest
import recurring_ical_events
from pydantic import SecretStr

from yandex_calendar_mcp.client import YandexCalendarClient
from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.dto import EventCreateDto, EventInfo, EventUpdateDto, TodoCreateDto
from yandex_calendar_mcp.errors import EventNotFoundError, InvalidEventError
from yandex_calendar_mcp.ical import (
    calendar_id__from_url,
    events__by_days,
    events__from_icalendar,
    vevent__apply_update,
    vevent__build,
    vtodo__build,
)
from yandex_calendar_mcp.server import DateParam, server__build

MSK = zoneinfo.ZoneInfo('Europe/Moscow')

ICS_TIMED = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VEVENT
UID:abc-123
SUMMARY:Планёрка
DESCRIPTION:Обсудить релиз
LOCATION:Переговорка 3
DTSTART;TZID=Europe/Moscow:20261008T150000
DTEND;TZID=Europe/Moscow:20261008T160000
STATUS:CONFIRMED
ORGANIZER;CN=Boss:mailto:boss@example.com
ATTENDEE;CN=Me;PARTSTAT=ACCEPTED;ROLE=REQ-PARTICIPANT:mailto:me@example.com
ATTENDEE;CN=Other;PARTSTAT=NEEDS-ACTION:mailto:other@example.com
RRULE:FREQ=WEEKLY
X-TELEMOST-CONFERENCE:https://telemost.yandex.ru/j/123
LAST-MODIFIED:20261001T100000Z
END:VEVENT
END:VCALENDAR
"""

ICS_DURATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:dur-1
SUMMARY:Длинная
DTSTART;TZID=Europe/Moscow:20261008T150000
DURATION:PT2H
END:VEVENT
END:VCALENDAR
"""

ICS_ALLDAY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:day-1
SUMMARY:Отпуск
DTSTART;VALUE=DATE:20261010
DTEND;VALUE=DATE:20261012
END:VEVENT
END:VCALENDAR
"""


class FakeObject:
    """Минимальная замена caldav.CalendarObjectResource."""

    def __init__(self, ics: str) -> None:
        self.ics = ics
        self.url = 'https://caldav.yandex.ru/calendars/me/events-1/x.ics'
        self.data = ics

    def get_icalendar_instance(self) -> icalendar.Calendar:
        return icalendar.Calendar.from_ical(self.ics)


def _settings(mode: typing.Literal['readonly', 'write'] = 'readonly') -> Settings:
    return Settings(_env_file=None, email='me@yandex.ru', key=SecretStr('x' * 16), mode=mode)  # type: ignore[call-arg]


def _dt(component: icalendar.Component, key: str) -> typing.Any:
    """Значение даты из свойства; icalendar типизирует __getitem__ союзом из 30 типов."""
    return typing.cast(icalendar.vDDDTypes, component[key]).dt


def _fake_calendar() -> types.SimpleNamespace:
    return types.SimpleNamespace(url='https://caldav.yandex.ru/calendars/me/events-1/', name='Мой')


@pytest.fixture
def client() -> YandexCalendarClient:
    with mock.patch('yandex_calendar_mcp.client.DAVClient'):
        return YandexCalendarClient(_settings())


def test_calendar_id_is_last_path_segment() -> None:
    """Из URL календаря берётся последний сегмент пути."""
    assert calendar_id__from_url('https://caldav.yandex.ru/calendars/me%40yandex.ru/events-12345/') == 'events-12345'


def test_timed_event_parsing() -> None:
    """VEVENT со временем: даты в таймзоне, участники, организатор, ссылка на Телемост."""
    (event,) = events__from_icalendar(icalendar.Calendar.from_ical(ICS_TIMED), calendar_id='events-1', url=None, tz=MSK)

    assert event.uid == 'abc-123'
    assert event.summary == 'Планёрка'
    assert event.location == 'Переговорка 3'
    assert event.start == datetime.datetime(2026, 10, 8, 15, 0, tzinfo=MSK)
    assert event.end == datetime.datetime(2026, 10, 8, 16, 0, tzinfo=MSK)
    assert event.all_day is False
    assert event.recurring is True
    assert event.organizer == 'boss@example.com'
    assert [a.email for a in event.attendees] == ['me@example.com', 'other@example.com']
    assert event.attendees[0].status == 'ACCEPTED'
    assert event.attendees[0].name == 'Me'
    assert event.conference_url == 'https://telemost.yandex.ru/j/123'
    assert event.last_modified is not None and event.last_modified.tzinfo is not None


def test_all_day_event_parsing() -> None:
    """VEVENT без времени: all_day, start и end остаются датами."""
    (event,) = events__from_icalendar(
        icalendar.Calendar.from_ical(ICS_ALLDAY), calendar_id='events-1', url=None, tz=MSK
    )

    assert event.all_day is True
    assert event.start == datetime.date(2026, 10, 10)
    assert event.end == datetime.date(2026, 10, 12)
    assert event.attendees == []


def test_free_slots_between_events(client: YandexCalendarClient) -> None:
    """Событие 15:00–16:00 делит рабочий день 9–19 на два окна."""
    day = datetime.date(2026, 10, 8)
    with (
        mock.patch.object(client, 'calendars__for_query', return_value=[_fake_calendar()]),
        mock.patch.object(client, 'objects__search', return_value=[FakeObject(ICS_TIMED)]),
    ):
        slots = client.free_slots__find(
            start=day,
            end=day,
            min_minutes=30,
            work_start=datetime.time(9),
            work_end=datetime.time(19),
        )

    assert [(slot.start.hour, slot.end.hour) for slot in slots] == [(9, 15), (16, 19)]
    assert slots[0].minutes == 360


def test_query_filter_is_case_insensitive(client: YandexCalendarClient) -> None:
    """Текстовый фильтр не зависит от регистра и ищет по подстроке."""
    with (
        mock.patch.object(client, 'calendars__for_query', return_value=[_fake_calendar()]),
        mock.patch.object(client, 'objects__search', return_value=[FakeObject(ICS_TIMED), FakeObject(ICS_ALLDAY)]),
    ):
        found = client.events__list(start=datetime.date(2026, 10, 1), end=datetime.date(2026, 10, 31), query='планёр')

    assert [event.uid for event in found] == ['abc-123']


def test_agenda_groups_events_by_day(client: YandexCalendarClient) -> None:
    """Повестка возвращает запись на каждый день, включая пустые."""
    with (
        mock.patch.object(client, 'calendars__for_query', return_value=[_fake_calendar()]),
        mock.patch.object(client, 'objects__search', return_value=[FakeObject(ICS_TIMED)]),
    ):
        agenda = client.agenda__by_days(date_from=datetime.date(2026, 10, 8), days=2)

    assert [(day.date.day, len(day.events)) for day in agenda] == [(8, 1), (9, 0)]


def test_agenda_puts_multi_day_event_into_every_day() -> None:
    """Событие на весь день 10–11.10 (DTEND 12.10 не включается) видно в повестке, начатой с 11.10."""
    events = events__from_icalendar(icalendar.Calendar.from_ical(ICS_ALLDAY), calendar_id='events-1', url=None, tz=MSK)

    agenda = events__by_days(events, date_from=datetime.date(2026, 10, 11), days=2, tz=MSK)

    assert [(day.date.day, len(day.events)) for day in agenda] == [(11, 1), (12, 0)]


def test_agenda_timed_event_ending_at_midnight_stays_in_one_day() -> None:
    """Событие 23:00–00:00 не попадает в следующий день."""
    event = EventInfo(
        uid='n',
        calendar_id='events-1',
        start=datetime.datetime(2026, 10, 8, 23, 0, tzinfo=MSK),
        end=datetime.datetime(2026, 10, 9, 0, 0, tzinfo=MSK),
    )

    agenda = events__by_days([event], date_from=datetime.date(2026, 10, 8), days=2, tz=MSK)

    assert [(day.date.day, len(day.events)) for day in agenda] == [(8, 1), (9, 0)]


def test_settings_reject_unknown_timezone() -> None:
    """Опечатка в таймзоне даёт ошибку валидации, а не KeyError из zoneinfo."""
    with pytest.raises(pydantic.ValidationError, match='Неизвестная таймзона'):
        Settings(_env_file=None, email='me@yandex.ru', key=SecretStr('x' * 16), tz='Europe/Moskow')  # type: ignore[call-arg]


def test_settings_require_full_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Логин без @ отклоняется."""
    monkeypatch.setenv('YANDEX_CALDAV_EMAIL', 'justlogin')
    monkeypatch.setenv('YANDEX_CALDAV_KEY', 'x')

    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


READ_TOOLS = {
    'get_connection_info',
    'list_calendars',
    'list_events',
    'get_event',
    'get_event_raw',
    'get_agenda',
    'sync_changes',
    'search_events',
    'list_todos',
    'find_free_slots',
    'check_availability',
}
WRITE_TOOLS = {
    'create_event',
    'update_event',
    'delete_event',
    'respond_to_invite',
    'create_todo',
    'complete_todo',
    'delete_todo',
}


async def _tool_names(mode: typing.Literal['readonly', 'write']) -> set[str]:
    with mock.patch('yandex_calendar_mcp.client.DAVClient'):
        server = server__build(_settings(mode=mode))
    tools = await server.list_tools()
    for tool in tools:
        # ctx не должен попадать в схему аргументов даже через обёртку domain_errors__as_tool_error
        assert 'ctx' not in tool.input_schema.get('properties', {}), tool.name
    return {tool.name for tool in tools}


@pytest.mark.asyncio
async def test_readonly_mode_registers_only_read_tools() -> None:
    """В режиме readonly write-тулов нет вообще."""
    assert await _tool_names('readonly') == READ_TOOLS


@pytest.mark.asyncio
async def test_write_mode_registers_read_and_write_tools() -> None:
    """В режиме write доступны оба набора."""
    assert await _tool_names('write') == READ_TOOLS | WRITE_TOOLS


def test_vevent_build_uses_tzid_duration_attendees_and_rrule() -> None:
    """Собранный VEVENT: время с TZID и VTIMEZONE, конец из длительности, участники с RSVP, RRULE."""
    dto = EventCreateDto(
        summary='Созвон',
        start=datetime.datetime(2026, 10, 9, 15, 0),
        duration_minutes=45,
        attendees=['a@example.com'],
        rrule='FREQ=WEEKLY;COUNT=3',
        location='Zoom',
    )

    calendar = vevent__build(dto, uid='u-1', tz=MSK)
    component = next(c for c in calendar.walk('VEVENT'))

    assert component['UID'] == 'u-1'
    assert _dt(component, 'DTSTART') == datetime.datetime(2026, 10, 9, 15, 0, tzinfo=MSK)
    assert component['DTSTART'].params['TZID'] == 'Europe/Moscow'
    assert _dt(component, 'DTEND') == datetime.datetime(2026, 10, 9, 15, 45, tzinfo=MSK)
    assert str(component['ATTENDEE']) == 'mailto:a@example.com'
    assert typing.cast(icalendar.vCalAddress, component['ATTENDEE']).params['RSVP'] == 'TRUE'
    assert typing.cast(icalendar.vRecur, component['RRULE'])['COUNT'] == [3]
    assert 'VTIMEZONE' in {c.name for c in calendar.subcomponents}


def test_vevent_build_series_keeps_local_time_across_dst() -> None:
    """Серия в зоне с переходом времени остаётся в 10:00 после перехода: время пишется с TZID, а не в UTC."""
    berlin = zoneinfo.ZoneInfo('Europe/Berlin')
    dto = EventCreateDto(summary='x', start=datetime.datetime(2026, 10, 19, 10, 0), rrule='FREQ=WEEKLY;COUNT=3')

    calendar = icalendar.Calendar.from_ical(vevent__build(dto, uid='u', tz=berlin).to_ical())
    occurrences = recurring_ical_events.of(calendar).between(datetime.date(2026, 10, 1), datetime.date(2026, 12, 1))
    starts = [_dt(c, 'DTSTART') for c in occurrences]

    assert [start.hour for start in starts] == [10, 10, 10]


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [('RRULE:FREQ=WEEKLY', 'FREQ=WEEKLY'), (' FREQ=DAILY;COUNT=3 ', 'FREQ=DAILY;COUNT=3')],
)
def test_rrule_is_normalized(raw: str, expected: str) -> None:
    """Префикс RRULE: и пробелы убираются."""
    assert EventCreateDto(summary='x', start=datetime.date(2026, 10, 10), rrule=raw).rrule == expected


@pytest.mark.parametrize('raw', ['garbage', 'COUNT=3', 'FREQ=WEEKLY;COUNT=x'])
def test_rrule_without_freq_or_unparsable_is_rejected(raw: str) -> None:
    """Правило без FREQ или с битым значением отклоняется на входе, а не превращается в пустой RRULE."""
    with pytest.raises(pydantic.ValidationError):
        EventCreateDto(summary='x', start=datetime.date(2026, 10, 10), rrule=raw)


def test_all_day_end_equal_to_start_explains_exclusive_end() -> None:
    """Для события на весь день end не включается, ошибка подсказывает, что передать."""
    dto = EventCreateDto(summary='x', start=datetime.date(2026, 10, 10), end=datetime.date(2026, 10, 10))

    with pytest.raises(InvalidEventError, match='end=2026-10-11'):
        vevent__build(dto, uid='u', tz=MSK)


def test_vevent_build_all_day_defaults_to_one_day() -> None:
    """Дата без времени даёт событие на весь день с DTEND на следующий день."""
    calendar = vevent__build(EventCreateDto(summary='Отпуск', start=datetime.date(2026, 10, 10)), uid='u', tz=MSK)
    component = next(c for c in calendar.walk('VEVENT'))

    assert _dt(component, 'DTSTART') == datetime.date(2026, 10, 10)
    assert _dt(component, 'DTEND') == datetime.date(2026, 10, 11)


def test_vevent_build_rejects_end_before_start() -> None:
    """end раньше start — доменная ошибка, а не молчаливое событие."""
    dto = EventCreateDto(summary='x', start=datetime.datetime(2026, 1, 1, 12), end=datetime.datetime(2026, 1, 1, 11))

    with pytest.raises(InvalidEventError):
        vevent__build(dto, uid='u', tz=MSK)


def test_vevent_apply_update_changes_only_given_fields() -> None:
    """Обновление трогает только переданные поля; перенос start без end сохраняет длительность."""
    component = next(c for c in icalendar.Calendar.from_ical(ICS_TIMED).walk('VEVENT'))
    dto = EventUpdateDto(uid='abc-123', summary='Новое имя', start=datetime.datetime(2026, 10, 8, 14, 0))

    vevent__apply_update(component, dto, MSK)

    assert component['SUMMARY'] == 'Новое имя'
    assert component['LOCATION'] == 'Переговорка 3'
    assert _dt(component, 'DTSTART') == datetime.datetime(2026, 10, 8, 14, 0, tzinfo=MSK)
    assert component['DTSTART'].params['TZID'] == 'Europe/Moscow'
    assert _dt(component, 'DTEND') == datetime.datetime(2026, 10, 8, 15, 0, tzinfo=MSK)


def test_vevent_apply_update_replaces_duration_with_dtend() -> None:
    """Событие с DURATION: перенос сохраняет длительность, DURATION уходит, чтобы не было DTEND вместе с ним."""
    component = next(c for c in icalendar.Calendar.from_ical(ICS_DURATION).walk('VEVENT'))

    vevent__apply_update(component, EventUpdateDto(uid='dur-1', start=datetime.datetime(2026, 10, 8, 18, 0)), MSK)

    assert 'DURATION' not in component
    assert _dt(component, 'DTEND') == datetime.datetime(2026, 10, 8, 20, 0, tzinfo=MSK)


def test_vtodo_build() -> None:
    """VTODO с датой, приоритетом и статусом NEEDS-ACTION."""
    calendar = vtodo__build(
        TodoCreateDto(summary='Купить', due=datetime.date(2026, 10, 12), priority=1), uid='t', tz=MSK
    )
    component = next(c for c in calendar.walk('VTODO'))

    assert component['SUMMARY'] == 'Купить'
    assert _dt(component, 'DUE') == datetime.date(2026, 10, 12)
    assert component['PRIORITY'] == 1
    assert component['STATUS'] == 'NEEDS-ACTION'


@pytest.mark.parametrize(
    ('raw', 'expected'),
    [
        ('2026-10-09', datetime.date(2026, 10, 9)),
        ('2026-10-09T15:00', datetime.datetime(2026, 10, 9, 15, 0)),
        ('2026-10-09T00:00', datetime.datetime(2026, 10, 9, 0, 0)),
        ('2026-10-09T00:00:00Z', datetime.datetime(2026, 10, 9, 0, 0, tzinfo=datetime.UTC)),
        (
            '2026-10-09T15:00:00+03:00',
            datetime.datetime(2026, 10, 9, 15, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=3))),
        ),
    ],
)
def test_date_param_keeps_date_only_strings_as_dates(raw: str, expected: datetime.date) -> None:
    """Строка без времени разбирается как дата (весь день), со временем как дата-время."""
    assert pydantic.TypeAdapter(DateParam).validate_python(raw) == expected
    assert type(pydantic.TypeAdapter(DateParam).validate_python(raw)) is type(expected)


ICS_INSTANCE_OCT_15 = ICS_TIMED.replace('20261008', '20261015').replace(
    'RRULE:FREQ=WEEKLY\n', 'RECURRENCE-ID;TZID=Europe/Moscow:20261015T150000\n'
)


def test_get_event_occurrence_returns_instance_for_date(client: YandexCalendarClient) -> None:
    """С occurrence возвращается экземпляр серии на эту дату, а не мастер с датой первого вхождения."""
    master = FakeObject(ICS_TIMED)
    with (
        mock.patch.object(client, 'calendars__for_query', return_value=[_fake_calendar()]),
        mock.patch.object(client, 'object__by_uid', return_value=master),
        mock.patch.object(client, 'objects__search', return_value=[FakeObject(ICS_INSTANCE_OCT_15)]),
    ):
        event = client.event__get('abc-123', occurrence=datetime.date(2026, 10, 15))
        assert event.start == datetime.datetime(2026, 10, 15, 15, 0, tzinfo=MSK)
        assert event.recurrence_id == datetime.datetime(2026, 10, 15, 15, 0, tzinfo=MSK)

        with pytest.raises(EventNotFoundError):
            client.event__get('abc-123', occurrence=datetime.date(2026, 10, 16))
