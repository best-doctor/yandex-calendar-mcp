"""Офлайн-тесты разбора schedule-response и расчёта общих окон."""

from __future__ import annotations

import datetime
import zoneinfo

from yandex_calendar_mcp.dto import AttendeeAvailability, BusyInterval
from yandex_calendar_mcp.freebusy import (
    common_free__compute,
    freebusy_request__build,
    schedule_default_calendar__parse,
    schedule_response__parse,
)

MSK = zoneinfo.ZoneInfo('Europe/Moscow')

SCHEDULE_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<C:schedule-response xmlns:C="urn:ietf:params:xml:ns:caldav">
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:a@example.com</D:href></C:recipient>
    <C:request-status>2.0;Success</C:request-status>
    <C:calendar-data><![CDATA[BEGIN:VCALENDAR
VERSION:2.0
METHOD:REPLY
BEGIN:VFREEBUSY
UID:x
FREEBUSY;FBTYPE=BUSY-TENTATIVE:20261009T150000Z/20261009T153000Z
FREEBUSY;FBTYPE=BUSY:20261009T153000Z/20261009T160000Z
DTSTART:20261009T060000Z
DTEND:20261009T170000Z
END:VFREEBUSY
END:VCALENDAR
]]></C:calendar-data>
  </C:response>
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:me@example.com</D:href></C:recipient>
    <C:request-status>2.0;Success</C:request-status>
    <C:calendar-data><![CDATA[BEGIN:VCALENDAR
VERSION:2.0
METHOD:REPLY
BEGIN:VFREEBUSY
UID:y
FREEBUSY;FBTYPE=BUSY:20261009T100000Z/20261009T113000Z
DTSTART:20261009T060000Z
DTEND:20261009T170000Z
END:VFREEBUSY
END:VCALENDAR
]]></C:calendar-data>
  </C:response>
</C:schedule-response>
"""


def test_schedule_response_parse_gives_busy_intervals_in_local_tz() -> None:
    """Каждый участник получает свои интервалы в таймзоне пользователя с типом занятости."""
    parsed = schedule_response__parse(SCHEDULE_RESPONSE, tz=MSK)

    assert [a.email for a in parsed] == ['a@example.com', 'me@example.com']
    assert parsed[0].request_status.startswith('2.0')
    assert [(i.start.hour, i.end.hour, i.kind) for i in parsed[0].busy] == [
        (18, 18, 'BUSY-TENTATIVE'),
        (18, 19, 'BUSY'),
    ]
    assert parsed[0].busy[0].start == datetime.datetime(2026, 10, 9, 18, 0, tzinfo=MSK)


def test_common_free_excludes_everyones_busy() -> None:
    """Общие окна не пересекаются ни с чьей занятостью, включая tentative."""
    parsed = schedule_response__parse(SCHEDULE_RESPONSE, tz=MSK)
    day = datetime.date(2026, 10, 9)
    slots = common_free__compute(
        parsed,
        start=datetime.datetime.combine(day, datetime.time(0), tzinfo=MSK),
        end=datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(0), tzinfo=MSK),
        min_minutes=30,
        work_start=datetime.time(9),
        work_end=datetime.time(19),
        tz=MSK,
    )

    assert [(s.start.strftime('%H:%M'), s.end.strftime('%H:%M')) for s in slots] == [
        ('09:00', '13:00'),
        ('14:30', '18:00'),
    ]


def test_freebusy_request_build_is_itip_request_in_utc() -> None:
    """Запрос в outbox: METHOD:REQUEST, организатор, участники, границы в UTC."""
    body = freebusy_request__build(
        organizer='me@example.com',
        attendees=['me@example.com', 'a@example.com'],
        start=datetime.datetime(2026, 10, 9, 9, 0, tzinfo=MSK),
        end=datetime.datetime(2026, 10, 9, 20, 0, tzinfo=MSK),
    )

    assert 'METHOD:REQUEST' in body
    assert 'ORGANIZER:mailto:me@example.com' in body
    assert 'ATTENDEE:mailto:a@example.com' in body
    assert 'DTSTART:20261009T060000Z' in body
    assert 'DTEND:20261009T170000Z' in body


# Ответ Яндекса на PROPFIND schedule-default-calendar-URL по schedule inbox: href без префикса пространства имён
DEFAULT_CALENDAR_RESPONSE = """<?xml version='1.0' encoding='utf-8'?>
<D:multistatus xmlns:D="DAV:"><D:response><href xmlns="DAV:">/calendars/user%40example.com/inbox/</href>\
<D:propstat><D:prop><C:schedule-default-calendar-URL xmlns:C="urn:ietf:params:xml:ns:caldav">\
<D:href>/calendars/user%40example.com/events-18230812/</D:href></C:schedule-default-calendar-URL></D:prop>\
<status xmlns="DAV:">HTTP/1.1 200 OK</status></D:propstat></D:response></D:multistatus>
"""

DEFAULT_CALENDAR_MISSING = """<?xml version='1.0' encoding='utf-8'?>
<D:multistatus xmlns:D="DAV:"><D:response><D:href>/calendars/user%40example.com/inbox/</D:href>
<D:propstat><D:prop><C:schedule-default-calendar-URL xmlns:C="urn:ietf:params:xml:ns:caldav"/></D:prop>
<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat></D:response></D:multistatus>
"""


def test_schedule_default_calendar_parse() -> None:
    """href основного календаря берётся из ответа inbox; пустое свойство (404 в propstat) даёт None."""
    assert (
        schedule_default_calendar__parse(DEFAULT_CALENDAR_RESPONSE) == '/calendars/user%40example.com/events-18230812/'
    )
    assert schedule_default_calendar__parse(DEFAULT_CALENDAR_MISSING) is None


def test_common_free_ignores_free_periods() -> None:
    """FBTYPE=FREE это свободное время: в общие окна оно не вычитается, BUSY-UNAVAILABLE вычитается."""
    day = datetime.date(2026, 10, 9)
    attendee = AttendeeAvailability(
        email='a@example.com',
        request_status='2.0;Success',
        busy=[
            BusyInterval(
                start=datetime.datetime(2026, 10, 9, 10, tzinfo=MSK),
                end=datetime.datetime(2026, 10, 9, 12, tzinfo=MSK),
                kind='FREE',
            ),
            BusyInterval(
                start=datetime.datetime(2026, 10, 9, 14, tzinfo=MSK),
                end=datetime.datetime(2026, 10, 9, 15, tzinfo=MSK),
                kind='BUSY-UNAVAILABLE',
            ),
        ],
    )

    slots = common_free__compute(
        [attendee],
        start=datetime.datetime.combine(day, datetime.time(9), tzinfo=MSK),
        end=datetime.datetime.combine(day, datetime.time(19), tzinfo=MSK),
        min_minutes=30,
        work_start=datetime.time(9),
        work_end=datetime.time(19),
        tz=MSK,
    )

    assert [(s.start.hour, s.end.hour) for s in slots] == [(9, 14), (15, 19)]
