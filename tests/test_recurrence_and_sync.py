"""Офлайн-тесты: правка и удаление одного экземпляра серии, инкрементальная синхронизация. Без сети."""

from __future__ import annotations

import contextlib
import datetime
import types
import typing
import zoneinfo
from unittest import mock

import icalendar
import pytest
import recurring_ical_events
from caldav.lib import error as caldav_error
from caldav.lib.url import URL
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from yandex_calendar_mcp.client import YandexCalendarClient, caldav_error__is_permanent, http_status__from_error
from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.dto import EventUpdateDto
from yandex_calendar_mcp.errors import InvalidEventError, SyncTokenError
from yandex_calendar_mcp.ical import (
    events__from_icalendar,
    vcalendar__apply_update,
    vcalendar__exclude_occurrence,
    vcalendar__master,
    vcalendar__occurrence_for_edit,
    vcalendar__override_find,
    vcalendar__set_partstat,
    vevent__apply_update,
    vtodo__mark_completed,
)
from yandex_calendar_mcp.server import domain_errors__as_tool_error

MSK = zoneinfo.ZoneInfo('Europe/Moscow')
ME = 'me@yandex.ru'
CALENDAR_URL = 'https://caldav.yandex.ru/calendars/me%40yandex.ru/events-1/'

# Так Яндекс хранит серию: мастер и переопределения в одном объекте, всё в TZID=Europe/Moscow
ICS_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VEVENT
UID:series-1
SUMMARY:Синк
DTSTART;TZID=Europe/Moscow:20260923T140000
DTEND;TZID=Europe/Moscow:20260923T150000
RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=WE
SEQUENCE:6
END:VEVENT
BEGIN:VEVENT
UID:series-1
SUMMARY:Синк
RECURRENCE-ID;TZID=Europe/Moscow:20261007T140000
DTSTART;TZID=Europe/Moscow:20261007T133000
DTEND;TZID=Europe/Moscow:20261007T143000
SEQUENCE:6
END:VEVENT
END:VCALENDAR
"""

ICS_ALLDAY_SERIES = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:daily-1
SUMMARY:Зарядка
DTSTART;VALUE=DATE:20261010
DTEND;VALUE=DATE:20261011
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
END:VCALENDAR
"""

ICS_SINGLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:single-1
SUMMARY:Разовая
DTSTART;TZID=Europe/Moscow:20261008T150000
DTEND;TZID=Europe/Moscow:20261008T160000
END:VEVENT
END:VCALENDAR
"""


def _dt(component: icalendar.Component, key: str) -> typing.Any:
    """Значение даты из свойства; icalendar типизирует __getitem__ союзом из 30 типов."""
    return typing.cast(icalendar.vDDDTypes, component[key]).dt


def _vevents(calendar: icalendar.Calendar) -> list[icalendar.Component]:
    return list(calendar.walk('VEVENT'))


def _client() -> YandexCalendarClient:
    settings = Settings(_env_file=None, email='me@yandex.ru', key=SecretStr('x' * 16))  # type: ignore[call-arg]
    with mock.patch('yandex_calendar_mcp.client.DAVClient'):
        return YandexCalendarClient(settings)


def test_occurrence_edit_creates_override_in_master_timezone() -> None:
    """Экземпляр без переопределения: создаётся VEVENT с RECURRENCE-ID в TZID мастера, мастер не меняется."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)

    override = vcalendar__occurrence_for_edit(calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)
    vevent__apply_update(override, EventUpdateDto(uid='series-1', summary='Перенесли'), MSK)

    assert len(_vevents(calendar)) == 3
    assert _dt(override, 'RECURRENCE-ID') == datetime.datetime(2026, 10, 21, 14, 0, tzinfo=MSK)
    assert override['RECURRENCE-ID'].params['TZID'] == 'Europe/Moscow'
    assert _dt(override, 'DTSTART') == datetime.datetime(2026, 10, 21, 14, 0, tzinfo=MSK)
    assert _dt(override, 'DTEND') == datetime.datetime(2026, 10, 21, 15, 0, tzinfo=MSK)
    assert 'RRULE' not in override
    assert override['SUMMARY'] == 'Перенесли'
    assert vcalendar__master(calendar)['SUMMARY'] == 'Синк'


def test_occurrence_edit_reuses_existing_override_by_utc_recurrence_id() -> None:
    """Уже перенесённый экземпляр находится по исходному началу, даже если оно передано в UTC."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)

    override = vcalendar__occurrence_for_edit(calendar, datetime.datetime(2026, 10, 7, 11, 0, tzinfo=datetime.UTC), MSK)

    assert len(_vevents(calendar)) == 2
    assert _dt(override, 'DTSTART') == datetime.datetime(2026, 10, 7, 13, 30, tzinfo=MSK)


def test_occurrence_edit_moves_only_this_instance() -> None:
    """Перенос начала экземпляра сохраняет его длительность, RECURRENCE-ID остаётся исходным."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)
    recurrence_id = datetime.datetime(2026, 10, 21, 14, 0)

    override = vcalendar__occurrence_for_edit(calendar, recurrence_id, MSK)
    vevent__apply_update(override, EventUpdateDto(uid='series-1', start=datetime.datetime(2026, 10, 21, 16, 0)), MSK)
    events = events__from_icalendar(calendar, calendar_id='events-1', url=None, tz=MSK)
    moved = next(event for event in events if event.recurrence_id == recurrence_id.replace(tzinfo=MSK))

    assert moved.start == datetime.datetime(2026, 10, 21, 16, 0, tzinfo=MSK)
    assert moved.end == datetime.datetime(2026, 10, 21, 17, 0, tzinfo=MSK)


@pytest.mark.parametrize(
    ('ics', 'recurrence_id', 'message'),
    [
        (ICS_SERIES, datetime.datetime(2026, 9, 30, 14, 0), 'нет экземпляра'),
        # Новое время перенесённого экземпляра не равно его исходному началу
        (ICS_SERIES, datetime.datetime(2026, 10, 7, 13, 30), 'нет экземпляра'),
        (ICS_SERIES, datetime.datetime(2026, 10, 21, 15, 0), 'нет экземпляра'),
        (ICS_SERIES, datetime.date(2026, 10, 21), 'должен содержать время'),
        (ICS_ALLDAY_SERIES, datetime.datetime(2026, 10, 11, 0, 0), 'датой без времени'),
        (ICS_ALLDAY_SERIES, datetime.date(2026, 10, 20), 'нет экземпляра'),
        (ICS_SINGLE, datetime.datetime(2026, 10, 8, 15, 0), 'не повторяющееся'),
    ],
)
def test_occurrence_edit_rejects_unknown_or_mistyped_recurrence_id(
    ics: str, recurrence_id: datetime.date, message: str
) -> None:
    """Несуществующий экземпляр, неверный тип recurrence_id или разовое событие дают понятную ошибку."""
    calendar = icalendar.Calendar.from_ical(ics)

    with pytest.raises(InvalidEventError, match=message):
        vcalendar__occurrence_for_edit(calendar, recurrence_id, MSK)


def test_all_day_occurrence_edit() -> None:
    """Серия на весь день: RECURRENCE-ID и DTSTART остаются датами."""
    calendar = icalendar.Calendar.from_ical(ICS_ALLDAY_SERIES)

    override = vcalendar__occurrence_for_edit(calendar, datetime.date(2026, 10, 12), MSK)

    assert _dt(override, 'RECURRENCE-ID') == datetime.date(2026, 10, 12)
    assert _dt(override, 'DTEND') == datetime.date(2026, 10, 13)


def test_exclude_occurrence_adds_exdate_and_drops_override() -> None:
    """Удаление экземпляра: EXDATE в мастере, переопределение этого экземпляра убирается."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)

    vcalendar__exclude_occurrence(calendar, datetime.datetime(2026, 10, 7, 14, 0), MSK)
    vcalendar__exclude_occurrence(calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)

    master = vcalendar__master(calendar)
    exdates = [item.dt for entry in typing.cast(list[icalendar.vDDDLists], master['EXDATE']) for item in entry.dts]
    assert exdates == [
        datetime.datetime(2026, 10, 7, 14, 0, tzinfo=MSK),
        datetime.datetime(2026, 10, 21, 14, 0, tzinfo=MSK),
    ]
    assert len(_vevents(calendar)) == 1
    with pytest.raises(InvalidEventError, match='нет экземпляра'):
        vcalendar__exclude_occurrence(calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)


@pytest.mark.parametrize(
    ('exc', 'status', 'permanent'),
    [
        (caldav_error.ReportError('400 Bad Request\n\n'), 400, True),
        (caldav_error.ResponseError('HTTP/1.1 507 Insufficient Storage'), 507, False),
        (caldav_error.RateLimitError('Rate limited, retry after 30'), None, False),
        (ConnectionError('reset by peer'), None, False),
        (ConnectionError("HTTPSConnectionPool(host='caldav.yandex.ru', port=443): Max retries"), None, False),
        (caldav_error.ReportError('https://caldav.yandex.ru/calendars/me/events-1/404.ics'), None, False),
    ],
)
def test_http_status_is_parsed_from_caldav_error_text(exc: Exception, status: int | None, permanent: bool) -> None:
    """Код ответа берётся из текста ошибки caldav: 4xx не ретраятся, 5xx и 429 ретраятся."""
    assert http_status__from_error(exc) == status
    assert caldav_error__is_permanent(exc) is permanent


def _fake_object(name: str, ics: str | None) -> types.SimpleNamespace:
    # multiget у caldav собирает URL с @, а sync-collection отдаёт %40, как и search
    return types.SimpleNamespace(
        url=URL.objectify(f'{CALENDAR_URL.replace("%40", "@")}{name}'),
        data=ics,
        get_icalendar_instance=lambda: icalendar.Calendar.from_ical(ics or ''),
    )


def _sync_client(cal: mock.Mock) -> YandexCalendarClient:
    client = _client()
    cal.url = URL.objectify(CALENDAR_URL)
    client.__dict__['calendars_by_id'] = {'events-1': cal}
    return client


def test_sync_without_token_returns_baseline_token_only() -> None:
    """Первый вызов: токен через PROPFIND, объекты не выкачиваются."""
    cal = mock.Mock()
    cal.get_property.return_value = 'sync-token:1 100'

    result = _sync_client(cal).sync__changes('events-1', sync_token=None)

    assert result.baseline is True
    assert result.sync_token == 'sync-token:1 100'
    assert result.events == []
    cal.get_objects_by_sync_token.assert_not_called()


def test_sync_with_token_returns_changed_and_deleted() -> None:
    """Объект из дельты без данных в multiget (404 или вовсе не вернулся) считается удалённым."""
    cal = mock.Mock()
    names = ('a.ics', 'gone-404.ics', 'gone-missing.ics')
    changed = [types.SimpleNamespace(url=URL.objectify(f'{CALENDAR_URL}{name}')) for name in names]
    cal.get_objects_by_sync_token.return_value = types.SimpleNamespace(objects=changed, sync_token='sync-token:1 200')
    cal.multiget.return_value = [_fake_object('a.ics', ICS_SERIES), _fake_object('gone-404.ics', None)]

    result = _sync_client(cal).sync__changes('events-1', sync_token='sync-token:1 100')

    assert result.baseline is False
    assert result.sync_token == 'sync-token:1 200'
    assert [event.uid for event in result.events] == ['series-1', 'series-1']
    assert {event.url for event in result.events} == {f'{CALENDAR_URL}a.ics'}
    assert result.deleted_urls == [f'{CALENDAR_URL}gone-404.ics', f'{CALENDAR_URL}gone-missing.ics']
    cal.get_objects_by_sync_token.assert_called_once_with('sync-token:1 100', load_objects=False, disable_fallback=True)


@pytest.mark.parametrize(
    'exc',
    [caldav_error.ReportError('400 Bad Request\n\n'), caldav_error.AuthorizationError(reason='Forbidden')],
)
def test_sync_rejects_unknown_token(exc: Exception) -> None:
    """Токен, отклонённый сервером (400 у Яндекса, 403 по RFC 6578), даёт SyncTokenError без ретраев."""
    cal = mock.Mock()
    cal.get_objects_by_sync_token.side_effect = exc

    with pytest.raises(SyncTokenError):
        _sync_client(cal).sync__changes('events-1', sync_token='sync-token:1 garbage')

    assert cal.get_objects_by_sync_token.call_count == 1


def test_sync_rejects_caldav_fake_token_without_request() -> None:
    """Фейковый токен caldav отклоняется до запроса: по нему caldav молча выкачал бы весь календарь."""
    cal = mock.Mock()

    with pytest.raises(SyncTokenError):
        _sync_client(cal).sync__changes('events-1', sync_token='fake-abc')

    cal.get_objects_by_sync_token.assert_not_called()


def test_series_shift_moves_overrides_and_exdates_with_master() -> None:
    """Перенос серии на час: переопределения и EXDATE едут вместе, удалённый экземпляр не воскресает."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)
    vcalendar__exclude_occurrence(calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)

    vcalendar__apply_update(
        calendar, EventUpdateDto(uid='series-1', start=datetime.datetime(2026, 9, 23, 15, 0)), MSK, organizer=ME
    )

    occurrences = recurring_ical_events.of(calendar).between(datetime.date(2026, 10, 1), datetime.date(2026, 11, 1))
    assert [_dt(item, 'DTSTART') for item in occurrences] == [datetime.datetime(2026, 10, 7, 13, 30, tzinfo=MSK)]
    override = vcalendar__override_find(calendar, datetime.datetime(2026, 10, 7, 15, 0, tzinfo=MSK), MSK)
    assert override is not None


def test_series_shift_refuses_type_change_with_overrides() -> None:
    """Перевод серии с переопределениями в «весь день» отклоняется: RECURRENCE-ID не пересчитать однозначно."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)

    with pytest.raises(InvalidEventError, match='весь день'):
        vcalendar__apply_update(
            calendar, EventUpdateDto(uid='series-1', start=datetime.date(2026, 9, 23)), MSK, organizer=ME
        )


def test_occurrence_update_bumps_override_sequence() -> None:
    """Правка экземпляра поднимает SEQUENCE переопределения, мастер поднимет caldav при сохранении."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)
    dto = EventUpdateDto(uid='series-1', recurrence_id=datetime.datetime(2026, 10, 21, 14, 0), summary='x')

    vcalendar__apply_update(calendar, dto, MSK, organizer=ME)

    override = vcalendar__override_find(calendar, datetime.datetime(2026, 10, 21, 14, 0, tzinfo=MSK), MSK)
    assert override is not None
    assert override['SEQUENCE'] == 7
    assert vcalendar__master(calendar)['SEQUENCE'] == 6


def test_series_attendee_change_reaches_overrides() -> None:
    """Участник серии добавляется и в переопределения: у перенесённого экземпляра свой список участников."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)

    vcalendar__apply_update(
        calendar, EventUpdateDto(uid='series-1', add_attendees=['a@example.com']), MSK, organizer=ME
    )

    assert [[str(c['ATTENDEE'])] for c in _vevents(calendar)] == [['mailto:a@example.com']] * 2
    assert {str(c['ORGANIZER']) for c in _vevents(calendar)} == {f'mailto:{ME}'}

    vcalendar__apply_update(
        calendar, EventUpdateDto(uid='series-1', remove_attendees=['a@example.com']), MSK, organizer=ME
    )

    assert ['ATTENDEE' in c for c in _vevents(calendar)] == [False, False]


def test_occurrence_attendee_change_touches_only_this_instance() -> None:
    """С recurrence_id участник добавляется только в переопределение этого экземпляра, мастер не меняется."""
    calendar = icalendar.Calendar.from_ical(ICS_SERIES)
    occurrence = datetime.datetime(2026, 10, 21, 14, 0)
    dto = EventUpdateDto(uid='series-1', recurrence_id=occurrence, add_attendees=['a@example.com'])

    vcalendar__apply_update(calendar, dto, MSK, organizer=ME)

    override = vcalendar__override_find(calendar, occurrence.replace(tzinfo=MSK), MSK)
    assert override is not None
    assert str(override['ATTENDEE']) == 'mailto:a@example.com'
    assert 'ATTENDEE' not in vcalendar__master(calendar)


ICS_INVITE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:invite-1
DTSTART;TZID=Europe/Moscow:20261008T150000
DTEND;TZID=Europe/Moscow:20261008T160000
RRULE:FREQ=WEEKLY;COUNT=3
ORGANIZER:mailto:boss@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:Me@Yandex.ru
ATTENDEE;PARTSTAT=ACCEPTED:mailto:other@example.com
END:VEVENT
BEGIN:VEVENT
UID:invite-1
RECURRENCE-ID;TZID=Europe/Moscow:20261015T150000
DTSTART;TZID=Europe/Moscow:20261015T170000
DTEND;TZID=Europe/Moscow:20261015T180000
ORGANIZER:mailto:boss@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:me@yandex.ru
END:VEVENT
END:VCALENDAR
"""


def test_set_partstat_updates_own_attendee_in_every_vevent() -> None:
    """Ответ на приглашение меняет PARTSTAT своего адреса в мастере и в переопределениях, чужих не трогает."""
    calendar = icalendar.Calendar.from_ical(ICS_INVITE)

    vcalendar__set_partstat(calendar, 'me@yandex.ru', 'ACCEPTED')

    events = events__from_icalendar(calendar, calendar_id='events-1', url=None, tz=MSK)
    statuses = [[(a.email.lower(), a.status) for a in event.attendees] for event in events]
    assert statuses == [
        [('me@yandex.ru', 'ACCEPTED'), ('other@example.com', 'ACCEPTED')],
        [('me@yandex.ru', 'ACCEPTED')],
    ]
    assert 'RSVP' not in calendar.to_ical().decode()


def test_set_partstat_rejects_non_attendee() -> None:
    """Если пользователя нет среди участников, ответ не отправляется."""
    calendar = icalendar.Calendar.from_ical(ICS_INVITE)

    with pytest.raises(InvalidEventError, match='не участник'):
        vcalendar__set_partstat(calendar, 'stranger@yandex.ru', 'DECLINED')


def test_mark_completed_is_idempotent() -> None:
    """Первый вызов закрывает задачу, повторный ничего не меняет и не падает."""
    todo = icalendar.Todo()
    todo.add('UID', 't')
    todo.add('STATUS', 'NEEDS-ACTION')

    assert vtodo__mark_completed(todo) is True
    assert todo['STATUS'] == 'COMPLETED'
    assert todo['PERCENT-COMPLETE'] == 100
    assert vtodo__mark_completed(todo) is False


def test_network_error_after_retries_reaches_model_as_text() -> None:
    """Сетевая ошибка после исчерпания ретраев уходит модели текстом, а не безымянной ошибкой тула."""

    @domain_errors__as_tool_error
    def failing() -> None:
        raise ConnectionError('connection reset')

    with pytest.raises(ToolError, match='Нет связи с CalDAV-сервером'):
        failing()


class FakeEditableObject:
    """Объект caldav с edit_icalendar_instance поверх готового VCALENDAR."""

    def __init__(self, ics: str) -> None:
        self.calendar = icalendar.Calendar.from_ical(ics)
        self.url = URL.objectify(f'{CALENDAR_URL}series-1.ics')

    @contextlib.contextmanager
    def edit_icalendar_instance(self) -> typing.Iterator[icalendar.Calendar]:
        yield self.calendar


def test_series_shift_with_exdate_is_refused_before_put() -> None:
    """Перенос серии с удалённым экземпляром отклоняется до записи: Яндекс выбросил бы EXDATE."""
    client = _client()
    obj = FakeEditableObject(ICS_SERIES)
    vcalendar__exclude_occurrence(obj.calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)
    cal = types.SimpleNamespace(url=URL.objectify(CALENDAR_URL))
    dto = EventUpdateDto(uid='series-1', start=datetime.datetime(2026, 9, 23, 15, 0))

    with (
        mock.patch.object(client, 'object__find', return_value=(cal, obj)),
        mock.patch.object(client, 'object__save') as save,
        pytest.raises(InvalidEventError, match='удалённые экземпляры'),
    ):
        client.event__update(dto)

    save.assert_not_called()


def test_series_summary_update_with_exdate_is_allowed() -> None:
    """Без переноса времени серия с удалённым экземпляром правится как обычно."""
    client = _client()
    obj = FakeEditableObject(ICS_SERIES)
    vcalendar__exclude_occurrence(obj.calendar, datetime.datetime(2026, 10, 21, 14, 0), MSK)
    cal = types.SimpleNamespace(url=URL.objectify(CALENDAR_URL))

    with (
        mock.patch.object(client, 'object__find', return_value=(cal, obj)),
        mock.patch.object(client, 'object__save') as save,
    ):
        client.event__update(EventUpdateDto(uid='series-1', summary='Новое'))

    save.assert_called_once()
    assert vcalendar__master(obj.calendar)['SUMMARY'] == 'Новое'
