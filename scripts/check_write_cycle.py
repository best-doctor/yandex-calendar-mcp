"""Живой тест записи: создать → изменить → прочитать → удалить для события, серии и задачи.

Для серии дополнительно проверяются правка и удаление одного экземпляра, перенос всей серии
и sync-token до и после удаления.
Тестовые объекты ставятся на 03:00 завтрашнего дня. Участники только на example.com, чтобы приглашения
не ушли живым людям.
Удаление выполняется в finally, чтобы не оставить мусор при падении посередине.
"""

from __future__ import annotations

import datetime
import logging
import sys
import time
import typing

from yandex_calendar_mcp.client import YandexCalendarClient
from yandex_calendar_mcp.config import Settings
from yandex_calendar_mcp.dto import EventCreateDto, EventInfo, EventUpdateDto, SyncResult, TodoCreateDto
from yandex_calendar_mcp.errors import EventNotFoundError, InvalidEventError
from yandex_calendar_mcp.ical import calendar_id__from_url

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
logging.getLogger('caldav').setLevel(logging.ERROR)

MARKER = 'MCP write-test'
# Домен example.com зарезервирован (RFC 2606): приглашения на эти адреса никому не доставляются
GUESTS = ['mcp-guest-1@example.com', 'mcp-guest-2@example.com']
# Дельта sync-collection у Яндекса отстаёт: создание видно через 3–15 с, удаление через 40–90 с
SYNC_WAIT_SECONDS = 180


def sync__wait_for(
    client: YandexCalendarClient, calendar_id: str, sync_token: str, *, matches: typing.Callable[[SyncResult], bool]
) -> SyncResult:
    started = time.monotonic()
    while True:
        result = client.sync__changes(calendar_id, sync_token=sync_token)
        waited = time.monotonic() - started
        if matches(result):
            print(f'[sync] change visible after {waited:.0f}s')
            return result
        if waited > SYNC_WAIT_SECONDS:
            raise AssertionError(f'change not visible in sync delta after {waited:.0f}s')
        time.sleep(3)


def check_event(client: YandexCalendarClient) -> None:
    tomorrow = datetime.datetime.now(client.tz).date() + datetime.timedelta(days=1)
    start = datetime.datetime.combine(tomorrow, datetime.time(3, 0), tzinfo=client.tz)
    created = client.event__create(
        EventCreateDto(summary=f'{MARKER} событие', start=start, duration_minutes=30, location='nowhere')
    )
    print(f'[event] created uid={created.uid} cal={created.calendar_id}')
    try:
        fetched = client.event__get(created.uid, calendar_id=created.calendar_id)
        print(f'[event] read back: {fetched.summary!r} {fetched.start} - {fetched.end} loc={fetched.location!r}')
        if fetched.start != start:
            raise AssertionError(f'start mismatch: {fetched.start} != {start}')

        new_start = start + datetime.timedelta(hours=1)
        update_dto = EventUpdateDto(
            uid=created.uid, calendar_id=created.calendar_id, summary=f'{MARKER} изменено', start=new_start
        )
        updated = client.event__update(update_dto)
        print(f'[event] updated: {updated.action}')
        fetched = client.event__get(created.uid, calendar_id=created.calendar_id)
        print(f'[event] read back: {fetched.summary!r} {fetched.start} - {fetched.end} loc={fetched.location!r}')
        if fetched.summary != f'{MARKER} изменено' or fetched.start != new_start:
            raise AssertionError('update not applied')
        if fetched.end != new_start + datetime.timedelta(minutes=30):
            raise AssertionError(f'end should keep 30 min duration, got {fetched.end}')
    finally:
        deleted = client.event__delete(created.uid, calendar_id=created.calendar_id)
        print(f'[event] deleted: {deleted.action}')
    try:
        client.event__get(created.uid, calendar_id=created.calendar_id)
    except EventNotFoundError:
        print('[event] confirmed gone')
    else:
        raise AssertionError('event still exists after delete')


def check_attendees(client: YandexCalendarClient) -> None:
    """Участники при создании, добавление и удаление. Без ORGANIZER Яндекс молча выбрасывал ATTENDEE."""
    tomorrow = datetime.datetime.now(client.tz).date() + datetime.timedelta(days=1)
    start = datetime.datetime.combine(tomorrow, datetime.time(3, 0), tzinfo=client.tz)
    created = client.event__create(
        EventCreateDto(summary=f'{MARKER} участники', start=start, duration_minutes=15, attendees=GUESTS[:1])
    )
    print(f'[attendees] created uid={created.uid} cal={created.calendar_id}')
    try:
        if created.calendar_id != client.default_event_calendar_id:
            raise AssertionError(f'event went to {created.calendar_id}, not to {client.default_event_calendar_id}')
        fetched = client.event__get(created.uid, calendar_id=created.calendar_id)
        print(f'[attendees] read back: organizer={fetched.organizer} {[a.email for a in fetched.attendees]}')
        if fetched.organizer != client.settings.email or [a.email for a in fetched.attendees] != GUESTS[:1]:
            raise AssertionError('attendee dropped on create')

        client.event__update(
            EventUpdateDto(
                uid=created.uid, calendar_id=created.calendar_id, add_attendees=GUESTS[1:], remove_attendees=GUESTS[:1]
            )
        )
        fetched = client.event__get(created.uid, calendar_id=created.calendar_id)
        print(f'[attendees] after add/remove: {[a.email for a in fetched.attendees]}')
        if [a.email for a in fetched.attendees] != GUESTS[1:]:
            raise AssertionError('add_attendees/remove_attendees not applied')
    finally:
        client.event__delete(created.uid, calendar_id=created.calendar_id)
        print('[attendees] deleted')


def series__instances(
    client: YandexCalendarClient, uid: str, calendar_id: str, start: datetime.date, end: datetime.date
) -> list[EventInfo]:
    return [event for event in client.events__list(start=start, end=end, calendar_id=calendar_id) if event.uid == uid]


def check_series_and_sync(client: YandexCalendarClient) -> None:
    """Серия из трёх ежедневных экземпляров: второй переносится на час, третий удаляется."""
    calendar_id = calendar_id__from_url(str(client.calendar__for_write(None, component='VEVENT').url))
    baseline = client.sync__changes(calendar_id, sync_token=None)
    print(f'[series] baseline sync token: {baseline.sync_token}')

    tomorrow = datetime.datetime.now(client.tz).date() + datetime.timedelta(days=1)
    starts = [
        datetime.datetime.combine(tomorrow + datetime.timedelta(days=offset), datetime.time(3, 0), tzinfo=client.tz)
        for offset in range(3)
    ]
    created = client.event__create(
        EventCreateDto(
            summary=f'{MARKER} серия',
            start=starts[0],
            duration_minutes=30,
            rrule='FREQ=DAILY;COUNT=3',
            calendar_id=calendar_id,
        )
    )
    print(f'[series] created uid={created.uid}')
    try:
        moved_start = starts[1] + datetime.timedelta(hours=1)
        client.event__update(
            EventUpdateDto(
                uid=created.uid,
                calendar_id=calendar_id,
                recurrence_id=starts[1],
                summary=f'{MARKER} экземпляр',
                start=moved_start,
            )
        )
        # Сдвиг всей серии на 30 минут: перенесённый экземпляр остаётся на своём времени
        series_start = starts[0] + datetime.timedelta(minutes=30)
        client.event__update(EventUpdateDto(uid=created.uid, calendar_id=calendar_id, start=series_start))
        shifted = series__instances(client, created.uid, calendar_id, tomorrow, starts[2].date())
        print(f'[series] after shift: {[(e.summary, e.start.isoformat()) for e in shifted]}')
        third_start = starts[2] + datetime.timedelta(minutes=30)
        if [e.start for e in shifted] != [series_start, moved_start, third_start]:
            raise AssertionError('series shift should move regular instances and keep the moved one')

        client.event__delete(created.uid, calendar_id=calendar_id, recurrence_id=third_start)
        instances = series__instances(client, created.uid, calendar_id, tomorrow, starts[2].date())
        print(f'[series] instances: {[(e.summary, e.start.isoformat()) for e in instances]}')
        if [(e.summary, e.start) for e in instances] != [
            (f'{MARKER} серия', series_start),
            (f'{MARKER} экземпляр', moved_start),
        ]:
            raise AssertionError('expected shifted first instance, moved second, no third')
        if instances[1].end != moved_start + datetime.timedelta(minutes=30):
            raise AssertionError(f'moved instance should keep 30 min, got {instances[1].end}')

        # С удалённым экземпляром перенос серии отклоняется до записи: Яндекс воскресил бы его
        try:
            client.event__update(EventUpdateDto(uid=created.uid, calendar_id=calendar_id, start=starts[0]))
        except InvalidEventError as exc:
            print(f'[series] shift with deleted instance refused: {exc}')
        else:
            raise AssertionError('series shift with EXDATE should be refused')

        changes = sync__wait_for(
            client,
            calendar_id,
            baseline.sync_token,
            matches=lambda result: any(e.uid == created.uid for e in result.events),
        )
        series = [e for e in changes.events if e.uid == created.uid]
        print(f'[sync] after create: {[(e.summary, e.recurrence_id) for e in series]}')
        series_url = series[0].url
    finally:
        client.event__delete(created.uid, calendar_id=calendar_id)
        print('[series] deleted')
    after_delete = sync__wait_for(
        client, calendar_id, changes.sync_token, matches=lambda result: series_url in result.deleted_urls
    )
    print(f'[sync] after delete: deleted={after_delete.deleted_urls}')


def check_todo(client: YandexCalendarClient) -> None:
    due = datetime.datetime.now(client.tz).date() + datetime.timedelta(days=1)
    created = client.todo__create(TodoCreateDto(summary=f'{MARKER} задача', due=due, priority=5))
    print(f'[todo] created uid={created.uid} cal={created.calendar_id}')
    try:
        todos = [t for t in client.todos__list(calendar_id=created.calendar_id) if t.uid == created.uid]
        print(f'[todo] read back pending: {[(t.summary, t.due, t.status) for t in todos]}')
        if not todos:
            raise AssertionError('todo not visible in pending list')

        completed = client.todo__complete(created.uid, calendar_id=created.calendar_id)
        print(f'[todo] completed: {completed.action}')
        client.todo__complete(created.uid, calendar_id=created.calendar_id)
        print('[todo] repeated complete is a no-op')
        done = [
            t
            for t in client.todos__list(calendar_id=created.calendar_id, include_completed=True)
            if t.uid == created.uid
        ]
        print(f'[todo] read back completed: {[(t.summary, t.status, t.completed) for t in done]}')
        still_pending = [t for t in client.todos__list(calendar_id=created.calendar_id) if t.uid == created.uid]
        if still_pending:
            raise AssertionError('completed todo still listed as pending')
    finally:
        deleted = client.todo__delete(created.uid, calendar_id=created.calendar_id)
        print(f'[todo] deleted: {deleted.action}')
    remaining = [
        t for t in client.todos__list(calendar_id=created.calendar_id, include_completed=True) if t.uid == created.uid
    ]
    if remaining:
        raise AssertionError('todo still exists after delete')
    print('[todo] confirmed gone')


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    if settings.mode != 'write':
        print('YANDEX_CALDAV_MODE must be "write" for this check', file=sys.stderr)
        return 2
    client = YandexCalendarClient(settings)
    check_event(client)
    check_attendees(client)
    check_series_and_sync(client)
    check_todo(client)
    print('OK: write cycle for event, attendees, series and todo passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
