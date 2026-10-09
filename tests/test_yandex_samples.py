"""Офлайн-тесты по примерам, снятым с живого аккаунта Яндекса. Все данные в фикстурах вымышленные."""

from __future__ import annotations

import datetime
import os
import types
import zoneinfo
from unittest import mock

import icalendar
import pytest
from caldav.lib import error as caldav_error
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import SecretStr

from yandex_calendar_mcp.client import YandexCalendarClient
from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.errors import CalendarNotFoundError, EventNotFoundError
from yandex_calendar_mcp.freebusy import schedule_response__parse
from yandex_calendar_mcp.ical import events__from_icalendar, free_slots__between_events, vtodo__to_todo_info
from yandex_calendar_mcp.server import domain_errors__as_tool_error

MSK = zoneinfo.ZoneInfo('Europe/Moscow')
BASE = 'https://caldav.yandex.ru/calendars/user%40example.com/'

# Разовое событие с участниками: так его отдаёт Яндекс (PRODID, VTIMEZONE, сложенные строки ATTENDEE,
# CN в кавычках, CATEGORIES с именем календаря, URL на веб-календарь, пробел в конце SUMMARY)
ICS_YANDEX_EVENT = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VTIMEZONE
TZID:Europe/Moscow
BEGIN:STANDARD
DTSTART:20141026T020000
TZOFFSETFROM:+0400
TZOFFSETTO:+0300
TZNAME:MSK
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
SUMMARY:Смотрим проблему с файлами 
DTSTART;TZID=Europe/Moscow:20261009T104500
DTEND;TZID=Europe/Moscow:20261009T110000
DTSTAMP:20261009T105411Z
UID:141zhhuiog91ddbvf2w5lxj0example.com
SEQUENCE:3
ATTENDEE;CN="Иванов Иван";PARTSTAT=NEEDS-ACTION;ROLE=REQ-PARTI
 CIPANT:mailto:i.ivanov@example.com
ATTENDEE;CN="Петрова Анна";PARTSTAT=ACCEPTED;ROLE=OPT-PARTICI
 PANT:mailto:a.petrova@example.com
ATTENDEE;CN="Пользователь";PARTSTAT=ACCEPTED;ROLE=REQ
 -PARTICIPANT:mailto:user@example.com
CATEGORIES:Пользователь
CLASS:PUBLIC
CREATED:20261007T130823Z
DESCRIPTION:Звонок: https://meet.example.com/abc 
LAST-MODIFIED:20261008T135317Z
LOCATION:https://meet.example.com/abc 
ORGANIZER;CN="Иванов Иван":mailto:i.ivanov@example.com
TRANSP:OPAQUE
URL:https://calendar.yandex.ru/event?event_id=123456
X-YANDEX-VISIBILITY:PUBLIC
END:VEVENT
END:VCALENDAR
"""

# Мастер ежедневной серии: Яндекс хранит DTSTART в UTC и переписывает COUNT в UNTIL
ICS_YANDEX_SERIES_MASTER = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VEVENT
SUMMARY:Дейли
DTSTART:20260921T071500Z
DTEND:20260921T073000Z
UID:talk-d09af75example
SEQUENCE:1
RRULE:FREQ=WEEKLY;UNTIL=20271021T081500Z;INTERVAL=1;BYDAY=MO,TU,WE,TH,FR
ORGANIZER;CN="Иванов Иван":mailto:i.ivanov@example.com
CLASS:PRIVATE
URL:https://calendar.yandex.ru/event?event_id=7
END:VEVENT
END:VCALENDAR
"""

# Экземпляр серии после серверного expand: отдельный VEVENT с RECURRENCE-ID и без RRULE
ICS_YANDEX_EXPANDED_INSTANCE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VEVENT
SUMMARY:Дейли
DTSTART;TZID=Europe/Moscow:20261009T101500
DTEND;TZID=Europe/Moscow:20261009T103000
RECURRENCE-ID;TZID=Europe/Moscow:20261009T101500
UID:talk-d09af75example
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

# Отпуск на две недели: событие на весь день с DTEND в следующий день после последнего
ICS_YANDEX_VACATION = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VEVENT
SUMMARY:Отпуск Сотрудник
DTSTART;VALUE=DATE:20261005
DTEND;VALUE=DATE:20261019
UID:B4BE3915-EXAMPLE
SEQUENCE:1
END:VEVENT
END:VCALENDAR
"""

# Задача из коллекции todos-* после complete_todo
ICS_YANDEX_TODO_DONE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Yandex LLC//Yandex Calendar//EN
BEGIN:VTODO
UID:4c338bb2-example
SUMMARY:Купить билеты
DUE;VALUE=DATE:20261009
PRIORITY:5
STATUS:COMPLETED
COMPLETED:20261008T164421Z
PERCENT-COMPLETE:100
END:VTODO
END:VCALENDAR
"""

# Ответ outbox: известный адрес с занятостью, свой адрес, неизвестный адрес без календарных данных
SCHEDULE_RESPONSE_MIXED = """<?xml version="1.0" encoding="utf-8"?>
<C:schedule-response xmlns:C="urn:ietf:params:xml:ns:caldav">
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:colleague@example.com</D:href></C:recipient>
    <C:request-status>2.0;Success</C:request-status>
    <C:calendar-data><![CDATA[BEGIN:VCALENDAR
VERSION:2.0
METHOD:REPLY
BEGIN:VFREEBUSY
DTSTAMP:20261009T095839Z
UID:oltkwwwjnsjxjfqjhsfs
FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261009T063000Z/20261009T064000Z
FREEBUSY;FBTYPE=BUSY:20261009T073000Z/20261009T090000Z,20261009T104500Z/20261009T120000Z
FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261009T150000Z/20261009T153000Z
FREEBUSY;FBTYPE=BUSY:20261009T153000Z/20261009T160000Z
ATTENDEE:mailto:colleague@example.com
DTSTART:20261009T060000Z
DTEND:20261009T170000Z
END:VFREEBUSY
END:VCALENDAR
]]></C:calendar-data>
  </C:response>
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:user@example.com</D:href></C:recipient>
    <C:request-status>2.0;Success</C:request-status>
    <C:calendar-data><![CDATA[BEGIN:VCALENDAR
VERSION:2.0
METHOD:REPLY
BEGIN:VFREEBUSY
UID:rjcujbohupqljsdedykj
FREEBUSY;FBTYPE=BUSY:20261009T153000Z/20261009T160000Z
ATTENDEE:mailto:user@example.com
DTSTART:20261009T060000Z
DTEND:20261009T170000Z
END:VFREEBUSY
END:VCALENDAR
]]></C:calendar-data>
  </C:response>
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:nobody@example.com</D:href></C:recipient>
    <C:request-status>3.8;No authority</C:request-status>
  </C:response>
</C:schedule-response>
"""


class FakeCalendar:
    """Минимальная замена caldav.Calendar: URL, имя и поддерживаемые компоненты."""

    def __init__(self, calendar_id: str, name: str, components: list[str]) -> None:
        self.url = f'{BASE}{calendar_id}/'
        self.name = name
        self.components = components

    def get_supported_components(self) -> list[str]:
        return self.components


class FakeObject:
    def __init__(self, ics: str, calendar_id: str) -> None:
        self.data = ics
        self.url = f'{BASE}{calendar_id}/x.ics'

    def get_icalendar_instance(self) -> icalendar.Calendar:
        return icalendar.Calendar.from_ical(self.data)

    def get_icalendar_component(self) -> icalendar.Component:
        return next(c for c in self.get_icalendar_instance().subcomponents if c.name != 'VTIMEZONE')


# Раскладка календарей живого аккаунта: девять событийных и одна коллекция задач
YANDEX_CALENDARS = {
    'events-18230812': FakeCalendar('events-18230812', 'Личный', ['VEVENT']),
    'events-37309973': FakeCalendar('events-37309973', 'Команда', ['VEVENT']),
    'todos-5413332': FakeCalendar('todos-5413332', 'Не забыть', ['VTODO']),
}


@pytest.fixture
def client() -> YandexCalendarClient:
    settings = Settings(_env_file=None, email='user@example.com', key=SecretStr('x' * 16))  # type: ignore[call-arg]
    with mock.patch('yandex_calendar_mcp.client.DAVClient'):
        instance = YandexCalendarClient(settings)
    instance.__dict__['calendars_by_id'] = dict(YANDEX_CALENDARS)
    instance.__dict__['default_event_calendar_id'] = None
    return instance


def _event(ics: str, calendar_id: str = 'events-18230812'):  # type: ignore[no-untyped-def]
    return events__from_icalendar(icalendar.Calendar.from_ical(ics), calendar_id=calendar_id, url=None, tz=MSK)


def test_yandex_event_with_folded_attendees_and_web_url() -> None:
    """Сложенные ATTENDEE, CN в кавычках, ROLE, пробел в конце SUMMARY, URL как web_url, ссылка на звонок в LOCATION."""
    (event,) = _event(ICS_YANDEX_EVENT)

    assert event.summary == 'Смотрим проблему с файлами'
    assert event.location == 'https://meet.example.com/abc'
    assert event.conference_url is None
    assert event.web_url == 'https://calendar.yandex.ru/event?event_id=123456'
    assert event.organizer == 'i.ivanov@example.com'
    assert event.start == datetime.datetime(2026, 10, 9, 10, 45, tzinfo=MSK)
    assert [(a.name, a.status, a.role) for a in event.attendees] == [
        ('Иванов Иван', 'NEEDS-ACTION', 'REQ-PARTICIPANT'),
        ('Петрова Анна', 'ACCEPTED', 'OPT-PARTICIPANT'),
        ('Пользователь', 'ACCEPTED', 'REQ-PARTICIPANT'),
    ]
    assert event.recurring is False
    assert event.last_modified == datetime.datetime(2026, 10, 8, 16, 53, 17, tzinfo=MSK)


def test_yandex_series_master_in_utc_is_shown_in_local_tz() -> None:
    """Мастер с DTSTART в UTC и RRULE с UNTIL: время приводится к Europe/Moscow, recurring=True."""
    (event,) = _event(ICS_YANDEX_SERIES_MASTER)

    assert event.recurring is True
    assert event.recurrence_id is None
    assert event.start == datetime.datetime(2026, 9, 21, 10, 15, tzinfo=MSK)
    assert event.end == datetime.datetime(2026, 9, 21, 10, 30, tzinfo=MSK)


def test_yandex_expanded_instance_has_recurrence_id() -> None:
    """Экземпляр после expand: тот же uid, RECURRENCE-ID заполнен, RRULE нет, но событие помечено как повторяющееся."""
    (event,) = _event(ICS_YANDEX_EXPANDED_INSTANCE)

    assert event.uid == 'talk-d09af75example'
    assert event.recurring is True
    assert event.recurrence_id == datetime.datetime(2026, 10, 9, 10, 15, tzinfo=MSK)
    assert event.start == datetime.datetime(2026, 10, 9, 10, 15, tzinfo=MSK)


def test_yandex_vacation_is_all_day_with_exclusive_end() -> None:
    """Отпуск 5–18 октября: all_day, start=5.10, end=19.10 как DTEND в RFC 5545."""
    (event,) = _event(ICS_YANDEX_VACATION)

    assert event.all_day is True
    assert event.start == datetime.date(2026, 10, 5)
    assert event.end == datetime.date(2026, 10, 19)


def test_yandex_completed_todo_fields() -> None:
    """Выполненная задача: статус, время выполнения в локальной зоне, процент, приоритет, DUE датой."""
    component = next(c for c in icalendar.Calendar.from_ical(ICS_YANDEX_TODO_DONE).walk('VTODO'))

    todo = vtodo__to_todo_info(component, calendar_id='todos-5413332', tz=MSK)

    assert todo.status == 'COMPLETED'
    assert todo.completed == datetime.datetime(2026, 10, 8, 19, 44, 21, tzinfo=MSK)
    assert todo.percent_complete == 100
    assert todo.priority == 5
    assert todo.due == datetime.date(2026, 10, 9)


def test_free_slots_on_a_real_busy_day() -> None:
    """Пятница с десятью встречами, ночным прогоном и отпуском коллеги: окна 9–10, 11–12:30, 17:30–18:30."""
    day = datetime.date(2026, 10, 9)
    busy = [
        ('00:30', '04:00'),
        ('10:00', '10:15'),
        ('10:15', '10:30'),
        ('10:45', '11:00'),
        ('12:30', '13:00'),
        ('13:00', '14:30'),
        ('14:30', '15:00'),
        ('15:00', '16:00'),
        ('16:00', '17:00'),
        ('17:00', '17:30'),
        ('18:30', '19:00'),
    ]
    ics_parts = [
        f'BEGIN:VEVENT\nUID:e{i}\nSUMMARY:Встреча\nDTSTART;TZID=Europe/Moscow:20261009T{s.replace(":", "")}00\n'
        f'DTEND;TZID=Europe/Moscow:20261009T{e.replace(":", "")}00\nEND:VEVENT'
        for i, (s, e) in enumerate(busy)
    ]
    ics = 'BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:test\n' + '\n'.join(ics_parts) + '\nEND:VCALENDAR\n'
    events = _event(ics) + _event(ICS_YANDEX_VACATION)

    slots = free_slots__between_events(
        events,
        own_email='user@example.com',
        start=datetime.datetime.combine(day, datetime.time(0), tzinfo=MSK),
        end=datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), tzinfo=MSK),
        min_minutes=30,
        work_start=datetime.time(9),
        work_end=datetime.time(19),
        tz=MSK,
    )

    assert [(s.start.strftime('%H:%M'), s.end.strftime('%H:%M')) for s in slots] == [
        ('09:00', '10:00'),
        ('11:00', '12:30'),
        ('17:30', '18:30'),
    ]


def test_declined_and_transparent_events_do_not_block_free_slots() -> None:
    """Отклонённая мной встреча и событие TRANSP:TRANSPARENT не занимают время, отклонение коллеги не в счёт."""
    day = datetime.date(2026, 10, 9)
    ics = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:test
BEGIN:VEVENT
UID:declined
DTSTART;TZID=Europe/Moscow:20261009T100000
DTEND;TZID=Europe/Moscow:20261009T110000
ORGANIZER:mailto:boss@example.com
ATTENDEE;PARTSTAT=DECLINED:mailto:User@Example.com
END:VEVENT
BEGIN:VEVENT
UID:free
DTSTART;TZID=Europe/Moscow:20261009T120000
DTEND;TZID=Europe/Moscow:20261009T130000
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:colleague-declined
DTSTART;TZID=Europe/Moscow:20261009T150000
DTEND;TZID=Europe/Moscow:20261009T160000
ORGANIZER:mailto:user@example.com
ATTENDEE;PARTSTAT=DECLINED:mailto:colleague@example.com
END:VEVENT
END:VCALENDAR
"""

    slots = free_slots__between_events(
        _event(ics),
        own_email='user@example.com',
        start=datetime.datetime.combine(day, datetime.time(9), tzinfo=MSK),
        end=datetime.datetime.combine(day, datetime.time(19), tzinfo=MSK),
        min_minutes=30,
        work_start=datetime.time(9),
        work_end=datetime.time(19),
        tz=MSK,
    )

    assert [(s.start.strftime('%H:%M'), s.end.strftime('%H:%M')) for s in slots] == [
        ('09:00', '15:00'),
        ('16:00', '19:00'),
    ]


def test_schedule_response_with_unknown_address_and_multi_period_line() -> None:
    """Неизвестный адрес даёт 3.8 без интервалов; несколько периодов в одной строке FREEBUSY разбираются все."""
    parsed = {a.email: a for a in schedule_response__parse(SCHEDULE_RESPONSE_MIXED, tz=MSK)}

    assert parsed['nobody@example.com'].request_status == '3.8;No authority'
    assert parsed['nobody@example.com'].busy == []
    colleague = parsed['colleague@example.com']
    assert [(i.start.strftime('%H:%M'), i.end.strftime('%H:%M'), i.kind) for i in colleague.busy] == [
        ('09:30', '09:40', 'BUSY-TENTATIVE'),
        ('10:30', '12:00', 'BUSY'),
        ('13:45', '15:00', 'BUSY'),
        ('18:00', '18:30', 'BUSY-TENTATIVE'),
        ('18:30', '19:00', 'BUSY'),
    ]


def test_availability_check_adds_own_address_once(client: YandexCalendarClient) -> None:
    """Свой адрес попадает в запрос первым и один раз, даже если передан явно в другом регистре."""
    day = datetime.date(2026, 10, 9)
    sent: list[str] = []

    def fake_post(body: str) -> str:
        sent.append(body)
        return SCHEDULE_RESPONSE_MIXED

    with mock.patch.object(client, 'outbox__post', side_effect=fake_post):
        result = client.availability__check(
            attendees=['colleague@example.com', 'USER@example.com', 'nobody@example.com'],
            start=datetime.datetime.combine(day, datetime.time(9), tzinfo=MSK),
            end=datetime.datetime.combine(day, datetime.time(20), tzinfo=MSK),
            min_minutes=30,
            work_start=datetime.time(9),
            work_end=datetime.time(19),
        )

    assert sent[0].count('ATTENDEE:mailto:user@example.com') == 1
    assert 'USER@example.com' not in sent[0]
    assert sent[0].index('mailto:user@example.com') < sent[0].index('mailto:colleague@example.com')
    assert [(s.start.strftime('%H:%M'), s.end.strftime('%H:%M')) for s in result.common_free] == [
        ('09:00', '09:30'),
        ('09:40', '10:30'),
        ('12:00', '13:45'),
        ('15:00', '18:00'),
    ]


def test_events_query_skips_todo_collection(client: YandexCalendarClient) -> None:
    """Запрос событий без calendar_id не трогает todos-*: Яндекс на VEVENT-запрос к ней отдал бы всю коллекцию."""
    with mock.patch.object(client, 'objects__search', return_value=[]) as search:
        client.events__list(start=datetime.date(2026, 10, 9), end=datetime.date(2026, 10, 9))

    assert [call.args[0].url.rsplit('/', 2)[-2] for call in search.call_args_list] == [
        'events-18230812',
        'events-37309973',
    ]


def test_todos_query_only_hits_todo_collection(client: YandexCalendarClient) -> None:
    """Список задач ходит только в todos-* и разбирает выполненную задачу."""
    with mock.patch.object(
        client, 'objects__search', return_value=[FakeObject(ICS_YANDEX_TODO_DONE, 'todos-5413332')]
    ) as search:
        todos = client.todos__list(include_completed=True)

    assert [call.args[0].url.rsplit('/', 2)[-2] for call in search.call_args_list] == ['todos-5413332']
    assert [(t.summary, t.status, t.calendar_id) for t in todos] == [('Купить билеты', 'COMPLETED', 'todos-5413332')]


def test_calendar_for_write_picks_collection_by_component(client: YandexCalendarClient) -> None:
    """Без основного календаря событие уходит в первый VEVENT-календарь, задача в todos-*."""
    assert str(client.calendar__for_write(None, component='VEVENT').url).endswith('events-18230812/')
    assert str(client.calendar__for_write(None, component='VTODO').url).endswith('todos-5413332/')
    assert str(client.calendar__for_write('Команда', component='VEVENT').url).endswith('events-37309973/')

    client.__dict__['calendars_by_id'] = {'events-1': FakeCalendar('events-1', 'x', ['VEVENT'])}
    client.__dict__.pop('components_by_calendar_id', None)
    with pytest.raises(CalendarNotFoundError):
        client.calendar__for_write(None, component='VTODO')


def test_event_without_calendar_id_goes_to_default_calendar(client: YandexCalendarClient) -> None:
    """Событие без calendar_id уходит в основной календарь из schedule inbox, даже если первым идёт чужой слой."""
    client.__dict__['default_event_calendar_id'] = 'events-37309973'

    assert str(client.calendar__for_write(None, component='VEVENT').url).endswith('events-37309973/')
    assert str(client.calendar__for_write(None, component='VTODO').url).endswith('todos-5413332/')

    client.__dict__['default_event_calendar_id'] = 'events-404'
    assert str(client.calendar__for_write(None, component='VEVENT').url).endswith('events-18230812/')


def test_principal_falls_back_to_documented_path(client: YandexCalendarClient) -> None:
    """Автообнаружение principal у Яндекса не работает (нет .well-known): берётся /principals/users/<email>/."""
    client.dav.principal.side_effect = caldav_error.NotFoundError('404')  # type: ignore[attr-defined]

    with mock.patch('yandex_calendar_mcp.client.Principal') as principal_cls:
        client.principal__discover()

    principal_cls.assert_called_once_with(
        client=client.dav, url='https://caldav.yandex.ru/principals/users/user@example.com/'
    )


def test_principal_discovery_does_not_hide_auth_errors(client: YandexCalendarClient) -> None:
    """Неверный пароль приложения должен дойти до пользователя, а не превратиться в фолбэк."""
    client.dav.principal.side_effect = caldav_error.AuthorizationError('401')  # type: ignore[attr-defined]

    with pytest.raises(caldav_error.AuthorizationError):
        client.principal__discover()


@pytest.mark.parametrize(
    ('exc', 'expected_text'),
    [
        (EventNotFoundError("Событие с uid 'x' не найдено"), "Событие с uid 'x' не найдено"),
        (caldav_error.AuthorizationError('401'), 'Яндекс отклонил авторизацию'),
        (caldav_error.ReportError("ReportError at '400 Bad Request'"), 'Ошибка CalDAV'),
    ],
)
def test_expected_errors_reach_model_as_tool_error(exc: Exception, expected_text: str) -> None:
    """Ожидаемые ошибки уходят модели текстом через ToolError, а не трейсбеком."""

    @domain_errors__as_tool_error
    def tool() -> None:
        raise exc

    with pytest.raises(ToolError, match=expected_text):
        tool()


def test_forced_readonly_overrides_environment() -> None:
    """Точка входа -ro: явный mode='readonly' сильнее YANDEX_CALDAV_MODE=write из окружения."""
    env = {'YANDEX_CALDAV_EMAIL': 'user@example.com', 'YANDEX_CALDAV_KEY': 'x', 'YANDEX_CALDAV_MODE': 'write'}
    with mock.patch.dict(os.environ, env):
        assert Settings(_env_file=None).mode == 'write'  # type: ignore[call-arg]
        assert Settings(_env_file=None, mode='readonly').mode == 'readonly'  # type: ignore[call-arg]


def test_fake_calendar_namespace_matches_caldav_shape() -> None:
    """Страховка для фикстуры: у подмены те же атрибуты, что читает клиент у caldav.Calendar."""
    cal = types.SimpleNamespace(url=YANDEX_CALENDARS['todos-5413332'].url, name='Не забыть')
    assert cal.url.endswith('todos-5413332/')


def test_empty_mode_from_plugin_config_means_readonly() -> None:
    """Плагин Claude Code подставляет пустую строку в незаполненное поле режима: сервер должен подняться в readonly."""
    env = {'YANDEX_CALDAV_EMAIL': 'user@example.com', 'YANDEX_CALDAV_KEY': 'x', 'YANDEX_CALDAV_MODE': ''}
    with mock.patch.dict(os.environ, env):
        assert Settings(_env_file=None).mode == 'readonly'  # type: ignore[call-arg]
