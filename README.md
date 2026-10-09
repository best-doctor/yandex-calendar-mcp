# yandex-calendar-mcp

[![PyPI](https://img.shields.io/pypi/v/yandex-calendar-mcp.svg)](https://pypi.org/project/yandex-calendar-mcp/)
[![CI](https://github.com/best-doctor/yandex-calendar-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/best-doctor/yandex-calendar-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

MCP-сервер для Яндекс Календаря. Подключается по CalDAV к `caldav.yandex.ru` и даёт модели доступ
к событиям, задачам и занятости коллег: чтение всегда, запись только в режиме `write`.

## 1. Получить пароль приложения

Яндекс не пускает в CalDAV по основному паролю аккаунта, нужен отдельный пароль приложения.

1. Открыть <https://id.yandex.ru/security/app-passwords>, залогинившись в Яндекс ID.
2. В блоке «Создать пароль приложения» выбрать тип **«Календарь»**.
3. Ввести любое название, например `mcp`, и нажать «Далее».
4. Скопировать пароль из всплывающего окна. Он показывается **один раз**.
5. Пароль может заработать не сразу, Яндекс просит подождать до 2–3 часов.

Для Яндекс 360 для бизнеса логин это полный адрес на домене компании (`user@company.ru`). Если в организации
запрещены пароли приложений, запрет снимает администратор.

Источник: [инструкция Яндекса по синхронизации календаря](https://yandex.ru/support/yandex-360/business/calendar/ru/data-exchange/synchronization/sync-desktop).

## 2. Подключить

Нужен [uv](https://docs.astral.sh/uv/getting-started/installation/): команда `uvx` скачает пакет с PyPI и запустит его.
Переменные окружения:

| Переменная | Значение |
|---|---|
| `YANDEX_CALDAV_EMAIL` | полный адрес, `login@yandex.ru` или `login@домен` |
| `YANDEX_CALDAV_KEY` | пароль приложения из шага 1 |
| `YANDEX_CALDAV_MODE` | `readonly` (по умолчанию) или `write` |
| `YANDEX_CALDAV_TZ` | таймзона для дат, по умолчанию `Europe/Moscow` |

Claude Code, только чтение:

```bash
claude mcp add yandex-calendar \
  -e YANDEX_CALDAV_EMAIL=login@yandex.ru \
  -e YANDEX_CALDAV_KEY=пароль_приложения \
  -- uvx yandex-calendar-mcp@latest
```

Claude Code, с записью (`YANDEX_CALDAV_MODE=write` включает тулы создания, изменения и удаления):

```bash
claude mcp add yandex-calendar \
  -e YANDEX_CALDAV_EMAIL=login@yandex.ru \
  -e YANDEX_CALDAV_KEY=пароль_приложения \
  -e YANDEX_CALDAV_MODE=write \
  -- uvx yandex-calendar-mcp@latest
```

Claude Desktop и другие клиенты с JSON-конфигом:

```json
{
  "mcpServers": {
    "yandex-calendar": {
      "command": "uvx",
      "args": ["yandex-calendar-mcp@latest"],
      "env": {
        "YANDEX_CALDAV_EMAIL": "login@yandex.ru",
        "YANDEX_CALDAV_KEY": "пароль_приложения",
        "YANDEX_CALDAV_MODE": "readonly"
      }
    }
  }
}
```

Codex CLI (конфиг `~/.codex/config.toml`):

```bash
codex mcp add yandex-calendar \
  --env YANDEX_CALDAV_EMAIL=login@yandex.ru \
  --env YANDEX_CALDAV_KEY=пароль_приложения \
  --env YANDEX_CALDAV_MODE=readonly \
  -- uvx yandex-calendar-mcp@latest
```

То же самое руками в `~/.codex/config.toml`:

```toml
[mcp_servers.yandex-calendar]
command = "uvx"
args = ["yandex-calendar-mcp@latest"]

[mcp_servers.yandex-calendar.env]
YANDEX_CALDAV_EMAIL = "login@yandex.ru"
YANDEX_CALDAV_KEY = "пароль_приложения"
YANDEX_CALDAV_MODE = "readonly"
```

Cursor: Settings → MCP → Add new global MCP server, либо файл `~/.cursor/mcp.json` для всех проектов
или `.cursor/mcp.json` в корне проекта. Пароль можно не писать в файл, а взять из переменной окружения
через `${env:...}`:

```json
{
  "mcpServers": {
    "yandex-calendar": {
      "type": "stdio",
      "command": "uvx",
      "args": ["yandex-calendar-mcp@latest"],
      "env": {
        "YANDEX_CALDAV_EMAIL": "login@yandex.ru",
        "YANDEX_CALDAV_KEY": "${env:YANDEX_CALDAV_KEY}",
        "YANDEX_CALDAV_MODE": "readonly"
      }
    }
  }
}
```

Точка входа `yandex-calendar-mcp-ro` всегда поднимает режим чтения, что бы ни стояло в `YANDEX_CALDAV_MODE`:

```bash
uvx --from yandex-calendar-mcp@latest yandex-calendar-mcp-ro
```

Запуск из исходников и проверка подключения описаны в [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## 3. Тулы

Даты принимаются в ISO 8601: `2026-10-09` означает весь день, `2026-10-09T15:00` точный момент
в таймзоне `YANDEX_CALDAV_TZ`. `calendar_id` берётся из `list_calendars`; без него запрос идёт по всем календарям.

### Чтение

| Тул | Что делает |
|---|---|
| `get_connection_info` | Логин, сервер, principal URL, список календарей, таймзона |
| `list_calendars` | Календари аккаунта: `id`, имя, URL, поддерживаемые компоненты |
| `list_events` | События за период (по умолчанию 7 дней от сегодня), повторяющиеся раскрываются, фильтр `query` |
| `get_event` | Одно событие по `uid`: участники со статусами, организатор, ссылка на встречу. Для серии без `occurrence` отдаёт мастер, с `occurrence` экземпляр на дату |
| `get_event_raw` | Сырой VCALENDAR по `uid` для отладки |
| `get_agenda` | События, сгруппированные по дням (`date_from`, `days`) |
| `search_events` | Текстовый поиск по названию/описанию/месту в окне −30…+90 дней |
| `list_todos` | Задачи VTODO, по умолчанию только незавершённые |
| `find_free_slots` | Свободные окна в рабочих часах, считается локально по занятым событиям |
| `sync_changes` | Изменения одного календаря после `sync_token` (RFC 6578): события, задачи, URL удалённых |
| `check_availability` | Занятость участников по их календарям через scheduling outbox и общие свободные окна. Как наложение календарей в веб-интерфейсе. Уведомлений не шлёт |
| `find_people` | Люди организации по подстроке имени, фамилии, логина или e-mail, кириллицей или латиницей: имя и адрес для участников. Один запрос к справочнику, без перебора |

### Запись (только в режиме `write`)

| Тул | Что делает | Аннотация |
|---|---|---|
| `create_event` | Создать событие: название, начало, конец или длительность, место, описание, участники, RRULE. Без `calendar_id` берётся основной календарь пользователя. С участниками организатором ставится свой адрес, приглашения рассылает Яндекс | write |
| `update_event` | Частично изменить событие по `uid`: только переданные поля. Перенос `start` без `end` сохраняет длительность. Для серии без `recurrence_id` меняется вся серия, с ним один экземпляр. `add_attendees` и `remove_attendees` добавляют и убирают участников своей встречи, Яндекс рассылает приглашения и отмены | destructive |
| `delete_event` | Удалить событие по `uid`. С `recurrence_id` удаляется один экземпляр серии (EXDATE) | destructive |
| `respond_to_invite` | `accept`, `decline` или `tentative`: PARTSTAT своего адреса во всех экземплярах, ответ организатору рассылает сервер | write |
| `create_todo` | Создать задачу. Без `calendar_id` берётся первый календарь с VTODO | write |
| `complete_todo` | Отметить задачу выполненной. Повторный вызов не ошибка | destructive |
| `delete_todo` | Удалить задачу | destructive |

**Правило для встреч.** Перед `create_event`, переносом и добавлением участников через `update_event` модель обязана вызвать
`check_availability` для всех участников на нужный слот. При пересечении (`BUSY` или `BUSY-TENTATIVE`)
встреча не ставится молча: модель показывает пересечения и общие свободные окна и уточняет у пользователя.
Правило зашито в `instructions` сервера и в описания обоих тулов.

Подробности поведения, архитектура и особенности Yandex CalDAV: [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).
