"""Офлайн-тесты разбора schedule-response и расчёта общих окон."""

from __future__ import annotations

import datetime
import zoneinfo

from yandex_calendar_mcp.freebusy import common_free__compute, freebusy_request__build, schedule_response__parse

MSK = zoneinfo.ZoneInfo('Europe/Moscow')

SCHEDULE_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<C:schedule-response xmlns:C="urn:ietf:params:xml:ns:caldav">
  <C:response>
    <C:recipient><D:href xmlns:D="DAV:">mailto:a@luchi.ru</D:href></C:recipient>
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
    <C:recipient><D:href xmlns:D="DAV:">mailto:me@luchi.ru</D:href></C:recipient>
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

    assert [a.email for a in parsed] == ['a@luchi.ru', 'me@luchi.ru']
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
        organizer='me@luchi.ru',
        attendees=['me@luchi.ru', 'a@luchi.ru'],
        start=datetime.datetime(2026, 10, 9, 9, 0, tzinfo=MSK),
        end=datetime.datetime(2026, 10, 9, 20, 0, tzinfo=MSK),
    )

    assert 'METHOD:REQUEST' in body
    assert 'ORGANIZER:mailto:me@luchi.ru' in body
    assert 'ATTENDEE:mailto:a@luchi.ru' in body
    assert 'DTSTART:20261009T060000Z' in body
    assert 'DTEND:20261009T170000Z' in body
