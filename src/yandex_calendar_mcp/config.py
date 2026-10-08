from __future__ import annotations

import typing
import zoneinfo

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Параметры подключения к CalDAV Яндекса. Читаются из окружения и .env, пароль не логируется."""

    model_config = SettingsConfigDict(
        env_prefix='YANDEX_CALDAV_',
        env_file='.env',
        env_file_encoding='utf-8',
        extra='ignore',
    )

    email: str = Field(description='Полный адрес: login@yandex.ru или login@domain')
    key: SecretStr = Field(description='Пароль приложения для Календаря')
    url: HttpUrl = Field(default=HttpUrl('https://caldav.yandex.ru'))
    tz: str = Field(default='Europe/Moscow', description='IANA-таймзона для вывода дат')
    timeout: int = Field(default=30, ge=5, le=120)
    mode: typing.Literal['readonly', 'write'] = Field(
        default='readonly',
        description='readonly: регистрируются только тулы чтения; write: плюс создание, изменение, удаление',
    )

    @field_validator('tz')
    @classmethod
    def tz__must_be_known(cls, value: str) -> str:
        zoneinfo.ZoneInfo(value)
        return value

    @field_validator('email')
    @classmethod
    def email__must_be_full_address(cls, value: str) -> str:
        if '@' not in value:
            raise ValueError('EMAIL должен быть полным адресом вида login@yandex.ru')
        return value.strip()

    @property
    def zoneinfo(self) -> zoneinfo.ZoneInfo:
        return zoneinfo.ZoneInfo(self.tz)
