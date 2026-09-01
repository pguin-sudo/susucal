from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import ClassVar, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_CONFIG_NAMES = ("settings.yml", "settings.yaml", "settings.json")

Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WeekParity = Literal["odd", "even", "all"]
EventType = Literal[
    "Лекции",
    "Лабораторные занятия",
    "Практические занятия и семинары",
    "Внеучебные мероприятия",
]


class _Base(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


class SusuAccount(_Base):
    login: str
    password: str


class UniverisCreds(_Base):
    login: str | None = None  # если пусто - берётся из user.susu
    password: str | None = None
    group_id: str | None = None  # обычно не нужен, резолвится через GetUserInfo
    base_url: str = "https://online.susu.ru/microgateway"


class MoodleCfg(_Base):
    ics_url: str


class ICloudCfg(_Base):
    apple_id: str
    app_password: str
    calendar: str | None = "SUSU"  # запасной для источников без своей раскладки
    calendars: dict[str, str] = Field(default_factory=dict)  # {source: календарь}
    # {eventType: календарь}; ключ "*" - на всё остальное
    univeris_calendars: dict[str, str] = Field(default_factory=dict)
    colors: dict[str, str] = Field(default_factory=dict)  # {календарь: "#RRGGBB"}
    caldav_url: str = "https://caldav.icloud.com/"
    timeout: int = 30

    @model_validator(mode="after")
    def _check_colors(self) -> ICloudCfg:
        for name, hexv in self.colors.items():
            if not re.fullmatch(r"#[0-9A-Fa-f]{6}", hexv):
                raise ValueError(f"colors['{name}'] = {hexv!r} - нужен формат #RRGGBB")
        return self

    def calendar_for(self, source: str, event_type: str | None = None) -> str | None:
        if source == "univeris" and self.univeris_calendars:
            return (
                self.univeris_calendars.get(event_type or "")
                or self.univeris_calendars.get("*")
                or self.calendars.get("univeris")
                or self.calendar
            )
        return self.calendars.get(source) or self.calendar

    def all_calendars_for(self, source: str) -> set[str]:
        """Все календари, куда этот источник может писать (для reconcile-обхода)."""
        if source == "univeris" and self.univeris_calendars:
            return {v for v in self.univeris_calendars.values() if v}
        c = self.calendars.get(source) or self.calendar
        return {c} if c else set()


class SubjectFilter(_Base):
    mode: Literal["off", "include", "exclude"] = "off"
    names: list[str] = Field(default_factory=list)


class SubgroupPick(_Base):
    """Какой вариант разбитой пары оставить: матч по аудитории и/или
    подстроке в ФИО преподавателя (достаточно одного)."""

    location: str | None = None
    instructor: str | None = None

    @model_validator(mode="after")
    def _need_one(self) -> SubgroupPick:
        if not self.location and not self.instructor:
            raise ValueError("subgroups.<subject>: задай location и/или instructor")
        return self


class SlotRule(_Base):
    """Выкинуть слот. Пример: {weekday: fri, pair: 2, week: even} = «2 пара по
    пятницам на чётной неделе». Незаданное поле = «любой»."""

    weekday: Weekday | None = None
    pair: int | None = Field(default=None, ge=1, le=8)  # по звонкам ЮУрГУ
    begin_time: str | None = None  # альтернатива pair, "HH:MM"
    week: WeekParity = "all"
    week_numbers: list[int] = Field(default_factory=list)  # 1-based
    subject: str | None = None

    @model_validator(mode="after")
    def _sane(self) -> SlotRule:
        if self.begin_time and not re.fullmatch(r"\d{1,2}:\d{2}", self.begin_time):
            raise ValueError("begin_time должен быть 'HH:MM'")
        if not any(
            (
                self.weekday,
                self.pair,
                self.begin_time,
                self.subject,
                self.week != "all",
                self.week_numbers,
            )
        ):
            raise ValueError("SlotRule без единого условия выкинет вообще всё")
        return self


class Filters(_Base):
    subjects: SubjectFilter = Field(default_factory=SubjectFilter)
    subgroups: dict[str, SubgroupPick] = Field(default_factory=dict)
    exclude_slots: list[SlotRule] = Field(default_factory=list)
    event_types: list[EventType] = Field(default_factory=list)  # пусто = все
    # {weekday: [дисциплины]} - в этот день оставить только эти
    weekday_whitelist: dict[Weekday, list[str]] = Field(default_factory=dict)
    split_without_rule: Literal["keep_all", "drop", "first"] = "keep_all"


class Defaults(_Base):
    semester_start: date = date(2026, 9, 1)
    timezone: str = "Asia/Yekaterinburg"
    horizon_days_past: int = 7
    horizon_days_future: int = 120
    # delete - удалить; mark - пометить и удалить через mark_grace_days; keep - оставить
    on_vanished: Literal["mark", "delete", "keep"] = "delete"
    mark_prefix: str = "[отменено] "
    mark_grace_days: int = 7


class UserCfg(_Base):
    name: str
    enabled: bool = True
    susu: SusuAccount | None = None  # общий аккаунт; univeris берёт креды отсюда
    univeris: UniverisCreds | None = None
    moodle: MoodleCfg | None = None
    icloud: ICloudCfg
    filters: Filters = Field(default_factory=Filters)
    # переопределяют defaults для этого пользователя
    semester_start: date | None = None
    timezone: str | None = None
    horizon_days_past: int | None = None
    horizon_days_future: int | None = None

    @model_validator(mode="after")
    def _need_a_source(self) -> UserCfg:
        if not self.univeris and not self.moodle:
            raise ValueError(f"user '{self.name}': нужен хотя бы один источник (univeris/moodle)")
        if self.univeris is not None:
            _ = self.univeris_credentials()  # бросит, если логин/пароль нигде не заданы
        for src in self.sources():
            if not self.icloud.all_calendars_for(src):
                where = "icloud.calendar / icloud.calendars"
                if src == "univeris":
                    where += " / icloud.univeris_calendars"
                raise ValueError(f"user '{self.name}': для '{src}' не задан календарь ({where})")
        return self

    def sources(self) -> list[str]:
        s: list[str] = []
        if self.univeris is not None:
            s.append("univeris")
        if self.moodle is not None:
            s.append("moodle")
        return s

    def univeris_credentials(self) -> tuple[str, str]:
        """Эффективные логин/пароль для online.susu.ru (свои или из susu)."""
        u = self.univeris
        login = (u.login if u else None) or (self.susu.login if self.susu else None)
        password = (u.password if u else None) or (self.susu.password if self.susu else None)
        if not login or not password:
            raise ValueError(
                f"user '{self.name}': для univeris нужны login/password в блоке univeris или susu"
            )
        return login, password


class Settings(_Base):
    defaults: Defaults = Field(default_factory=Defaults)
    users: list[UserCfg]

    @model_validator(mode="after")
    def _unique_names(self) -> Settings:
        names = [u.name for u in self.users]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"дублирующиеся user.name: {sorted(dupes)}")
        return self

    def resolved(self, user: UserCfg) -> Defaults:
        """Defaults с наложенными пользовательскими переопределениями."""
        d = self.defaults.model_copy()
        if user.semester_start is not None:
            d.semester_start = user.semester_start
        if user.timezone is not None:
            d.timezone = user.timezone
        if user.horizon_days_past is not None:
            d.horizon_days_past = user.horizon_days_past
        if user.horizon_days_future is not None:
            d.horizon_days_future = user.horizon_days_future
        return d


class ConfigError(RuntimeError):
    pass


def find_config(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise ConfigError(f"конфиг не найден: {p}")
        return p
    if env := os.getenv("SUSUCAL_CONFIG"):
        return find_config(env)
    here = Path.cwd()
    for base in (here, *here.parents):
        for name in _CONFIG_NAMES:
            if (base / name).is_file():
                return base / name
    raise ConfigError(
        f"не найден ни один из {_CONFIG_NAMES}; положи рядом или задай $SUSUCAL_CONFIG"
    )


def load(path: str | os.PathLike[str] | None = None) -> Settings:
    cfg_path = find_config(path)
    text = cfg_path.read_text(encoding="utf-8")
    raw = cast(
        "object",
        json.loads(text) if cfg_path.suffix == ".json" else yaml.safe_load(text),
    )
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: ожидался объект на верхнем уровне")
    try:
        return Settings.model_validate(raw)
    except Exception as e:  # pydantic ValidationError и пр.
        raise ConfigError(f"{cfg_path}: {e}") from e
