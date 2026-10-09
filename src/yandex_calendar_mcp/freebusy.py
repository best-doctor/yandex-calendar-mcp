"""Scheduling (RFC 6638): запрос занятости через outbox, календарь по умолчанию из inbox. Без сети."""

from __future__ import annotations

import datetime
import uuid
import zoneinfo
from xml.etree import ElementTree

import icalendar

from yandex_calendar_mcp.dto import AttendeeAvailability, BusyInterval, FreeSlot
from yandex_calendar_mcp.ical import PRODID, free_slots__between_busy

NS = {'C': 'urn:ietf:params:xml:ns:caldav', 'D': 'DAV:'}
SCHEDULE_DEFAULT_CALENDAR_PROPFIND = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    '<D:prop><C:schedule-default-calendar-URL/></D:prop></D:propfind>'
)


def freebusy_request__build(
    *, organizer: str, attendees: list[str], start: datetime.datetime, end: datetime.datetime
) -> str:
    calendar = icalendar.Calendar()
    calendar.add('PRODID', PRODID)
    calendar.add('VERSION', '2.0')
    calendar.add('METHOD', 'REQUEST')
    request = icalendar.FreeBusy()
    request.add('UID', str(uuid.uuid4()))
    request.add('DTSTAMP', datetime.datetime.now(datetime.UTC))
    request.add('DTSTART', start.astimezone(datetime.UTC))
    request.add('DTEND', end.astimezone(datetime.UTC))
    request.add('ORGANIZER', icalendar.vCalAddress(f'mailto:{organizer}'))
    for email in attendees:
        request.add('ATTENDEE', icalendar.vCalAddress(f'mailto:{email}'))
    calendar.add_component(request)
    return str(calendar.to_ical().decode())


def schedule_response__parse(xml: str, *, tz: zoneinfo.ZoneInfo) -> list[AttendeeAvailability]:
    """Разбирает C:schedule-response: по одному C:response на участника с VFREEBUSY внутри calendar-data."""
    root = ElementTree.fromstring(xml)
    result: list[AttendeeAvailability] = []
    for response in root.findall('C:response', NS):
        href = response.findtext('C:recipient/D:href', default='', namespaces=NS)
        status = response.findtext('C:request-status', default='', namespaces=NS)
        data = response.findtext('C:calendar-data', default='', namespaces=NS)
        email = href[7:] if href.lower().startswith('mailto:') else href
        result.append(AttendeeAvailability(email=email, request_status=status, busy=vfreebusy__intervals(data, tz=tz)))
    return result


def vfreebusy__intervals(ical_text: str, *, tz: zoneinfo.ZoneInfo) -> list[BusyInterval]:
    if not ical_text.strip():
        return []
    calendar = icalendar.Calendar.from_ical(ical_text)
    intervals: list[BusyInterval] = []
    for component in calendar.walk('VFREEBUSY'):
        raw = component.get('FREEBUSY')
        if raw is None:
            continue
        for prop in raw if isinstance(raw, list) else [raw]:
            kind = str(prop.params.get('FBTYPE', 'BUSY'))
            # Одна строка FREEBUSY это vPeriod (.dt = (start, end)), несколько через запятую — vDDDLists (.dts)
            periods = prop.dts if hasattr(prop, 'dts') else [prop]
            for period in periods:
                period_start, period_end = period.dt
                intervals.append(
                    BusyInterval(start=period_start.astimezone(tz), end=period_end.astimezone(tz), kind=kind)
                )
    intervals.sort(key=lambda interval: interval.start)
    return intervals


def common_free__compute(
    attendees: list[AttendeeAvailability],
    *,
    start: datetime.datetime,
    end: datetime.datetime,
    min_minutes: int,
    work_start: datetime.time,
    work_end: datetime.time,
    tz: zoneinfo.ZoneInfo,
) -> list[FreeSlot]:
    # FBTYPE=FREE по RFC 5545 это свободное время, занятость только BUSY, BUSY-TENTATIVE и BUSY-UNAVAILABLE
    busy = [
        (interval.start, interval.end)
        for attendee in attendees
        for interval in attendee.busy
        if interval.kind.startswith('BUSY')
    ]
    return free_slots__between_busy(
        busy, start=start, end=end, min_minutes=min_minutes, work_start=work_start, work_end=work_end, tz=tz
    )


def schedule_default_calendar__parse(xml: str) -> str | None:
    """href календаря по умолчанию из PROPFIND по schedule inbox, None если сервер его не отдал."""
    root = ElementTree.fromstring(xml)
    href = root.findtext('.//C:schedule-default-calendar-URL/D:href', default='', namespaces=NS)
    return href.strip() or None
