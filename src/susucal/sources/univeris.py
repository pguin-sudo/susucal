"""Источник «ЮУрГУ-Онлайн» (online.susu.ru/microgateway).

Аутентификация JWT Bearer:
  POST /api/auth/login        form identity + LoginForm[login]/[password] -> accessToken + refreshToken
  POST /api/auth/UpdateToken  form userName + identity + refreshToken     -> accessToken
  GET  /api/User/GetUserInfo                    -> student[0].groupId
  GET  /api/Schedule/GetGroupSchedule/{groupId} -> весь семестр, query-параметры игнорит :(

identity это постоянный device-id (32 символа), генерим один раз и держим в
state-файле вместе с refreshToken.
"""

import base64
import contextlib
import logging
import secrets
import string
import time as _time
from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import ClassVar, cast, final, override
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from susucal.models import Event, make_uid
from susucal.sources.base import Source

log = logging.getLogger("susucal.univeris")

_IDENTITY_ALPHABET = string.ascii_letters + string.digits
_TOKEN_SKEW = 120  # запас до exp, чтобы не влететь в 401 на самом запросе

_EVENT_TYPE_SHORT = {
    "Лекции": "Лекция",
    "Лабораторные занятия": "Лаб. работа",
    "Практические занятия и семинары": "Практика",
    "Внеучебные мероприятия": "Внеучебное",
}


class _M(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")


class _State(_M):
    identity: str = ""
    access_token: str = ""
    access_exp: float = 0.0
    refresh_token: str = ""
    user_name: str = ""
    group_id: str = ""
    group_name: str = ""


class _Student(_M):
    groupId: str = ""
    groupName: str = ""


class _AuthResp(_M):
    isLogged: bool = False
    accessToken: str = ""
    refreshToken: str = ""
    userName: str = ""
    translatedMessage: str = ""
    message: str = ""
    student: list[_Student] = Field(default_factory=list)


class _UserInfo(_M):
    student: list[_Student] = Field(default_factory=list)


class _JwtPayload(_M):
    exp: float = 0.0


class _Instructor(_M):
    name: str = ""
    location: str = ""


class _Item(_M):
    subject: str = ""
    eventType: str = ""
    eventDate: str = ""
    beginTime: str = ""
    endTime: str = ""
    instructors: list[_Instructor] = Field(default_factory=list)


class UniverisError(RuntimeError):
    pass


@final
class UniverisSource(Source):
    name = "univeris"

    def __init__(
        self,
        login: str,
        password: str,
        *,
        base_url: str = "https://online.susu.ru/microgateway",
        group_id: str | None = None,
        identity: str | None = None,
        state_path: str | Path = ".univeris_state.json",
        tz: str = "Asia/Yekaterinburg",
        timeout: float = 30.0,
    ) -> None:
        self._login = login
        self._password = password
        self.base_url = base_url.rstrip("/")
        self._group_id = group_id or ""
        self.tz = ZoneInfo(tz)
        self._state_path = Path(state_path)
        self._state = self._load_state()
        if identity:
            self._state.identity = identity
        if not self._state.identity:
            self._state.identity = _gen_identity()
        self._client = httpx.Client(timeout=timeout, base_url=self.base_url)

    @override
    def fetch(self) -> list[Event]:
        try:
            token = self._access_token()
            gid = self._group_id or self._state.group_id or self._resolve_group(token)
            if self._state.group_name:
                log.info("группа %s (%s)", self._state.group_name, gid)
            raw = self._get_json(f"/api/Schedule/GetGroupSchedule/{gid}", token)
            if not isinstance(raw, list):
                raise UniverisError(f"GetGroupSchedule вернул не массив: {type(raw).__name__}")
            items = [_Item.model_validate(x) for x in cast("list[object]", raw)]
            events = [e for it in items for e in self._normalize(it, gid)]
            log.info("univeris: %d занятий по группе %s", len(events), gid)
            return events
        finally:
            self._client.close()

    def _access_token(self) -> str:
        s = self._state
        if s.access_token and s.access_exp - _time.time() > _TOKEN_SKEW:
            return s.access_token
        if s.refresh_token:
            try:
                return self._refresh()
            except UniverisError as e:
                log.info("refresh не удался (%s), логинюсь заново", e)
        return self._do_login()

    def _do_login(self) -> str:
        log.info("login как %s", _mask(self._login))
        r = self._client.post(
            "/api/auth/login",
            data={
                "identity": self._state.identity,
                "LoginForm[login]": self._login,
                "LoginForm[password]": self._password,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = _AuthResp.model_validate(_json(r, "login"))
        if not data.isLogged:
            raise UniverisError(f"login отклонён: {data.translatedMessage or data.message}")
        return self._store_tokens(data)

    def _refresh(self) -> str:
        r = self._client.post(
            "/api/auth/UpdateToken",
            data={
                "userName": self._state.user_name or self._login,
                "identity": self._state.identity,
                "refreshToken": self._state.refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = _AuthResp.model_validate(_json(r, "UpdateToken"))
        if not data.isLogged:
            raise UniverisError("refresh-токен протух")
        log.info("access-токен обновлён по refresh")
        return self._store_tokens(data)

    def _store_tokens(self, data: _AuthResp) -> str:
        if not data.accessToken:
            raise UniverisError("в ответе нет accessToken")
        s = self._state
        s.access_token = data.accessToken
        s.access_exp = _jwt_exp(data.accessToken) or (_time.time() + 1800)
        if data.refreshToken:
            s.refresh_token = data.refreshToken
        if data.userName:
            s.user_name = data.userName
        if data.student:
            s.group_id = data.student[0].groupId or s.group_id
            s.group_name = data.student[0].groupName or s.group_name
        self._save_state()
        return data.accessToken

    def _get_json(self, path: str, token: str) -> object:
        r = self._client.get(path, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 401:
            log.info("401 на %s, форсирую релогин", path)
            self._state.access_token = ""
            token = self._do_login()
            r = self._client.get(path, headers={"Authorization": f"Bearer {token}"})
        return _json(r, path)

    def _resolve_group(self, token: str) -> str:
        info = _UserInfo.model_validate(self._get_json("/api/User/GetUserInfo", token))
        gid = info.student[0].groupId if info.student else ""
        if not gid:
            raise UniverisError("GetUserInfo без student[].groupId, задай group_id вручную")
        log.info("группа: %s (%s)", info.student[0].groupName or "?", gid)
        return gid

    def _normalize(self, item: _Item, gid: str) -> Iterator[Event]:
        subject = item.subject.strip() or "(без названия)"
        etype = item.eventType.strip()
        d = _parse_date(item.eventDate)
        t0 = _parse_time(item.beginTime)
        t1 = _parse_time(item.endTime)
        if d is None or t0 is None:
            log.warning("занятие без даты/времени, пропуск: %r", item)
            return

        start = datetime.combine(d, t0, tzinfo=self.tz)
        if t1 is None or t1 <= t0:
            end, all_day = start + timedelta(minutes=95), False
        else:
            end = datetime.combine(d, t1, tzinfo=self.tz)
            all_day = (end - start) > timedelta(hours=6)  # 08:00-17:00 и т.п. -> весь день
        if all_day:
            start = datetime.combine(d, time.min, tzinfo=self.tz)
            end = start + timedelta(days=1)

        instructors = item.instructors or [_Instructor()]
        multi = len(instructors) > 1
        for instr in instructors:
            teacher = instr.name.strip()
            teacher = "" if teacher in ("", "-") else teacher
            location = instr.location.strip() or None

            descr: list[str] = []
            if etype:
                descr.append(f"Тип: {_EVENT_TYPE_SHORT.get(etype, etype)}")
            if teacher:
                descr.append(f"Преподаватель: {teacher}")
            if location:
                descr.append(f"Аудитория: {location}")
            if multi:
                descr.append("Пара разбита на подгруппы")

            uid = make_uid(
                "univeris",
                gid,
                item.eventDate,
                item.beginTime,
                item.endTime,
                subject,
                etype,
                location or "",
            )
            yield Event(
                uid=uid,
                title=subject,
                start=start,
                end=end,
                location=location,
                description="\n".join(descr) or None,
                source="univeris",
                all_day=all_day,
                tags={
                    "event_type": etype,
                    "teacher": teacher,
                    "split": "1" if multi else "0",
                    "group_id": gid,
                },
            )

    def _load_state(self) -> _State:
        try:
            return _State.model_validate_json(self._state_path.read_bytes())
        except (OSError, ValueError):
            return _State()

    def _save_state(self) -> None:
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        _ = tmp.write_text(self._state.model_dump_json(indent=2), "utf-8")
        _ = tmp.replace(self._state_path)
        with contextlib.suppress(OSError):
            self._state_path.chmod(0o600)


def _gen_identity() -> str:
    return "".join(secrets.choice(_IDENTITY_ALPHABET) for _ in range(32))


def _mask(login: str) -> str:
    return login[:3] + "***" if len(login) > 3 else "***"


def _jwt_exp(token: str) -> float | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = _JwtPayload.model_validate_json(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return None
    return data.exp or None


def _json(r: httpx.Response, what: str) -> object:
    if r.status_code >= 400:
        raise UniverisError(f"{what}: HTTP {r.status_code} {r.text[:200]}")
    try:
        return cast("object", r.json())
    except ValueError as e:
        raise UniverisError(f"{what}: ответ не JSON ({e})") from e


def _parse_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def _parse_time(s: str) -> time | None:
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(s.strip(), fmt).time()
        except ValueError:
            continue
    return None
