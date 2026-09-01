import contextlib
import hashlib
from datetime import UTC, datetime, timedelta
from typing import cast

import icalendar

from susucal.models import Event

PRODID = "-//susucal//RU"
MANAGED = "X-SUSUCAL-MANAGED"
SOURCE = "X-SUSUCAL-SOURCE"
HASH = "X-SUSUCAL-HASH"
VANISHED_AT = "X-SUSUCAL-VANISHED-AT"


def add(comp: icalendar.Component, name: str, value: object) -> None:
    comp.add(name, value)  # pyright: ignore[reportUnknownMemberType]


def set_prop(comp: icalendar.Component, name: str, value: object) -> None:
    comp[name] = value


def vevents(comp: icalendar.Component) -> list[icalendar.Component]:
    return comp.walk("VEVENT")  # pyright: ignore[reportUnknownMemberType]


def prop(comp: icalendar.Component, name: str) -> str | None:
    v = cast("object", comp.get(name))
    return None if v is None else str(v)


def get_int(comp: icalendar.Component, name: str, default: int = 0) -> int:
    v = cast("object", comp.get(name, default))
    try:
        return int(v)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default


def parse(text: str | bytes) -> icalendar.Calendar:
    got = icalendar.Calendar.from_ical(text)
    assert isinstance(got, icalendar.Calendar)
    return got


def content_hash(ev: Event) -> str:
    parts: list[str] = [
        ev.title.strip(),
        ev.start.isoformat(),
        ev.end.isoformat(),
        (ev.location or "").strip(),
        (ev.description or "").strip(),
        "1" if ev.all_day else "0",
    ]
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:16]


def build_vcalendar(ev: Event, *, sequence: int = 0, vanished_at: str | None = None) -> bytes:
    cal = icalendar.Calendar()
    add(cal, "prodid", PRODID)
    add(cal, "version", "2.0")
    add(cal, "calscale", "GREGORIAN")

    ve = icalendar.Event()
    add(ve, "uid", ev.uid)
    add(ve, "summary", ev.title)
    now = datetime.now(UTC)
    add(ve, "dtstamp", now)
    add(ve, "last-modified", now)
    add(ve, "sequence", sequence)
    if ev.all_day:
        add(ve, "dtstart", ev.start.date())
        end = ev.end.date()
        if end <= ev.start.date():
            end = ev.start.date() + timedelta(days=1)
        add(ve, "dtend", end)
    else:
        add(ve, "dtstart", ev.start)
        add(ve, "dtend", ev.end)
    if ev.location:
        add(ve, "location", ev.location)
    if ev.description:
        add(ve, "description", ev.description)
    add(ve, MANAGED, "yes")
    add(ve, SOURCE, ev.source)
    add(ve, HASH, content_hash(ev))
    if vanished_at:
        add(ve, VANISHED_AT, vanished_at)

    cal.add_component(ve)
    with contextlib.suppress(Exception):
        cal.add_missing_timezones()
    return cal.to_ical()
