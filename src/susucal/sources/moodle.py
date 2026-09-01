import logging
import re
from datetime import UTC, date, datetime, time, timedelta
from typing import cast, final, override

import httpx
from icalendar import Calendar, Component

from susucal.ical import vevents
from susucal.models import Event, make_uid
from susucal.sources.base import Source

log = logging.getLogger("susucal.moodle")

_DEADLINE_LEN = timedelta(minutes=30)


def _redact(url: str) -> str:
    return re.sub(r"(authtoken|token)=[^&]+", r"\1=<redacted>", url)


def _prop_dt(comp: Component, name: str) -> date | datetime | None:
    dt = getattr(cast("object", comp.get(name)), "dt", None)
    return dt if isinstance(dt, (date, datetime)) else None


@final
class MoodleSource(Source):
    name = "moodle"

    def __init__(self, ics_url: str, *, timeout: float = 30.0) -> None:
        self.ics_url: str = ics_url
        self.timeout: float = timeout

    @override
    def fetch(self) -> list[Event]:
        log.info("GET %s", _redact(self.ics_url))
        r = httpx.get(self.ics_url, timeout=self.timeout, follow_redirects=True)
        _ = r.raise_for_status()
        if b"BEGIN:VCALENDAR" not in r.content:
            raise RuntimeError(f"ответ не похож на .ics (токен протух?): {r.text.strip()[:120]!r}")

        cal = Calendar.from_ical(r.content)
        events = [ev for comp in vevents(cal) if (ev := self._to_event(comp))]
        log.info("moodle: получено %d событий", len(events))
        return events

    def _to_event(self, comp: Component) -> Event | None:
        raw_uid = str(comp.get("UID") or "").strip()
        summary = str(comp.get("SUMMARY") or "").strip() or "(без названия)"

        start_val = _prop_dt(comp, "DTSTART")
        if start_val is None:
            log.warning("VEVENT без DTSTART, пропуск: %s", summary)
            return None
        end_val = _prop_dt(comp, "DTEND") or _prop_dt(comp, "DUE")

        if isinstance(start_val, datetime):
            all_day = False
            start = _aware(start_val)
            if isinstance(end_val, datetime):
                end = _aware(end_val)
            elif isinstance(end_val, date):
                end = datetime.combine(end_val, time.min, tzinfo=UTC)
            else:
                end = start + _DEADLINE_LEN
        else:  # DATE -> событие на весь день
            all_day = True
            start = datetime.combine(start_val, time.min, tzinfo=UTC)
            if isinstance(end_val, datetime):
                end = end_val
            elif isinstance(end_val, date):
                end = datetime.combine(end_val, time.min, tzinfo=UTC)
            else:
                end = start + timedelta(days=1)
        if end < start:
            end = start + _DEADLINE_LEN

        description = _clean(str(comp.get("DESCRIPTION") or ""))
        cats = getattr(cast("object", comp.get("CATEGORIES")), "cats", None)
        course = ", ".join(str(c) for c in cast("list[object]", cats or []))
        if course:
            description = f"Курс: {course}\n{description}".strip()

        uid = f"moodle-{raw_uid}" if raw_uid else make_uid("moodle", summary, start.isoformat())
        return Event(
            uid=uid,
            title=summary,
            start=start,
            end=end,
            location=str(comp.get("LOCATION") or "").strip() or None,
            description=description or None,
            source="moodle",
            all_day=all_day,
            tags={"moodle_uid": raw_uid},
        )


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n").replace("\\,", ",")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
