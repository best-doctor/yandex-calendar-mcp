"""Доменные ошибки клиента календаря."""

from __future__ import annotations


class CalendarNotFoundError(LookupError):
    pass


class EventNotFoundError(LookupError):
    pass


class EventConflictError(RuntimeError):
    """Объект изменился на сервере после чтения (412 на If-Match)."""


class InvalidEventError(ValueError):
    pass


class SyncTokenError(ValueError):
    """Сервер не принял sync-token (устарел или чужой) или не поддерживает синхронизацию."""
