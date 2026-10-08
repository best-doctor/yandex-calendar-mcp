"""Smoke-тест чтения: подключение, календари, события на неделю, открытые задачи."""

from __future__ import annotations

import datetime
import logging
import sys

from yandex_calendar_mcp.client import YandexCalendarClient
from yandex_calendar_mcp.config import Settings

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
# caldav пишет diff каждого исправленного объекта на уровне WARNING, это шум
logging.getLogger('caldav').setLevel(logging.ERROR)


def main() -> int:
    settings = Settings()  # type: ignore[call-arg]
    print(f'login: {settings.email}  server: {settings.url}  tz: {settings.tz}')
    client = YandexCalendarClient(settings)
    print(f'principal: {client.principal.url}')
    calendars = client.calendars__list()
    print(f'calendars: {len(calendars)}')
    for calendar in calendars:
        print(f'  - {calendar.id!r:30} {calendar.name!r:30} {calendar.supported_components}')
    today = datetime.datetime.now(client.tz).date()
    events = client.events__list(start=today, end=today + datetime.timedelta(days=7))
    print(f'events next 7 days: {len(events)}')
    for event in events[:15]:
        print(f'  {event.start} | {event.summary} | cal={event.calendar_id} | uid={event.uid[:12]}…')
    todos = client.todos__list()
    print(f'open todos: {len(todos)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
