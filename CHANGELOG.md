# Changelog

Формат: [Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии по [SemVer](https://semver.org/lang/ru/).
Раздел для версии из тега попадает в описание GitHub Release автоматически.

## [Unreleased]

### Исправлено

- Пустая строка в `YANDEX_CALDAV_MODE` трактуется как `readonly`: так поле приходит из незаполненных настроек плагинов.

## [0.1.0] — 2026-10-09

Первый релиз.

### Добавлено

- Подключение к Яндекс Календарю по CalDAV по паролю приложения, режимы `readonly` и `write`
  (`YANDEX_CALDAV_MODE`), отдельная точка входа `yandex-calendar-mcp-ro`.
- Чтение: `get_connection_info`, `list_calendars`, `list_events`, `get_event` с выбором экземпляра серии,
  `get_event_raw`, `get_agenda`, `search_events`, `list_todos`, `find_free_slots`, `sync_changes`.
- `check_availability`: занятость участников через scheduling outbox Яндекса и общие свободные окна.
- Запись: `create_event`, `update_event` (вся серия или один экземпляр), `delete_event`, `respond_to_invite`,
  `create_todo`, `complete_todo`, `delete_todo`.
- Правило для модели: перед созданием и переносом встречи проверять занятость и уточнять пересечения.
- Ретраи на сетевые сбои и 5xx, ожидаемые ошибки возвращаются модели текстом.
- CI на Python 3.12 и 3.13, публикация в PyPI по тегу, GitHub Release с заметками из changelog.

[Unreleased]: https://github.com/best-doctor/yandex-calendar-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/best-doctor/yandex-calendar-mcp/releases/tag/v0.1.0
