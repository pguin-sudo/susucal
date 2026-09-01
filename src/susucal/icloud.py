import logging
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import cast, final

import icalendar
from caldav.calendarobjectresource import CalendarObjectResource
from caldav.collection import Calendar, Principal
from caldav.davclient import DAVClient
from caldav.elements.ical import CalendarColor
from caldav.lib.error import AuthorizationError, DAVError

from susucal import ical
from susucal.config import Defaults, ICloudCfg
from susucal.ical import HASH, MANAGED, SOURCE, VANISHED_AT, prop
from susucal.models import Event

log = logging.getLogger("susucal.icloud")

try:
    from niquests.exceptions import RequestException as _HTTPError  # caldav 3.x на niquests
except Exception:
    _HTTPError = None

_RETRYABLE: tuple[type[BaseException], ...] = (
    (DAVError, OSError, TimeoutError)
    if _HTTPError is None
    else (DAVError, OSError, TimeoutError, _HTTPError)
)
_RETRY_SLEEPS = (3, 8, 20)


def _retry[T](fn: Callable[[], T], what: str) -> T:
    last: BaseException | None = None
    for i, pause in enumerate((0, *_RETRY_SLEEPS)):
        if pause:
            time.sleep(pause)
        try:
            return fn()
        except AuthorizationError:
            raise
        except _RETRYABLE as e:
            last = e
            log.warning("%s: попытка %d не удалась (%s)", what, i + 1, e)
    assert last is not None
    raise last


@dataclass
class SyncReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    marked: int = 0
    moved: int = 0
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        s = (
            f"создано={self.created} обновлено={self.updated} пропущено={self.skipped} "
            f"удалено={self.deleted} помечено={self.marked} переезд={self.moved}"
        )
        if self.errors:
            s += f" ошибок={len(self.errors)}"
        return s

    def merge(self, other: "SyncReport") -> None:
        self.created += other.created
        self.updated += other.updated
        self.skipped += other.skipped
        self.deleted += other.deleted
        self.marked += other.marked
        self.moved += other.moved
        self.errors += other.errors


@final
class ICloudSync:
    def __init__(self, cfg: ICloudCfg, *, dry_run: bool = False) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self._client = DAVClient(
            url=cfg.caldav_url,
            username=cfg.apple_id,
            password=cfg.app_password,
            timeout=cfg.timeout,
        )
        self._principal: Principal | None = None
        self._calendars: dict[str, Calendar] = {}
        self._colored: set[str] = set()

    # caldav 3.x типизирует sync-методы как "sync | async"; мы всегда sync, отсюда cast'ы
    def _principal_(self) -> Principal:
        if self._principal is None:
            self._principal = self._client.principal()  # pyright: ignore[reportUnknownMemberType]
        return self._principal

    def _all_calendars(self) -> list[Calendar]:
        return cast("list[Calendar]", self._principal_().calendars())

    def _events(self, cal: Calendar, what: str) -> list[CalendarObjectResource]:
        return cast("list[CalendarObjectResource]", _retry(cal.events, what))

    @staticmethod
    def _name(cal: Calendar) -> str | None:
        return cast("str | None", cal.get_display_name())

    @staticmethod
    def _vevent(obj: CalendarObjectResource) -> icalendar.Component | None:
        try:
            inst = cast("icalendar.Calendar", obj.icalendar_instance)
        except Exception:
            return None
        found = ical.vevents(inst)
        return found[0] if found else None

    def sync(
        self, events: list[Event], *, defaults: Defaults, active_sources: set[str]
    ) -> SyncReport:
        rep = SyncReport()
        incoming = {e.uid: e for e in events}
        target_of: dict[str, str | None] = {
            e.uid: self.cfg.calendar_for(e.source, e.tags.get("event_type")) for e in events
        }

        for cname, cal in self._iter_all_calendars():
            try:
                objs = self._events(cal, f"events(«{cname}»)")
            except Exception as e:
                rep.errors.append(f"чтение «{cname}»: {e}")
                continue
            for obj in objs:
                comp = self._vevent(obj)
                if comp is None or prop(comp, MANAGED) != "yes":
                    continue
                if prop(comp, SOURCE) not in active_sources:
                    continue
                uid = prop(comp, "uid") or ""
                try:
                    if uid in incoming:
                        if target_of[uid] != cname:
                            _ = _retry(obj.delete, f"переезд {uid}")
                            log.info("переезд: %s «%s» -> «%s»", uid[:26], cname, target_of[uid])
                            rep.moved += 1
                    else:
                        self._reconcile_gone(obj, comp, defaults, rep)
                except Exception as e:
                    rep.errors.append(f"{uid} @«{cname}»: {e}")
                    log.exception("сверка %s провалилась", uid)

        by_cal: dict[str, list[Event]] = defaultdict(list)
        for ev in events:
            name = target_of[ev.uid]
            if name is None:
                rep.errors.append(f"{ev.uid}: не определён целевой календарь")
                continue
            by_cal[name].append(ev)

        for name, evs in by_cal.items():
            sub = SyncReport()
            try:
                cal = self._get_calendar(name)
                existing = self._index(cal)
                for ev in evs:
                    try:
                        self._upsert(cal, existing.get(ev.uid), ev, sub)
                    except Exception as e:
                        sub.errors.append(f"{ev.uid}: {e}")
                        log.exception("upsert %s провалился", ev.uid)
            except Exception as e:
                sub.errors.append(f"календарь «{name}»: {e}")
                log.exception("календарь «%s» провалился", name)
            log.info("  [%s] %s", name, sub.line())
            rep.merge(sub)
        return rep

    def _iter_all_calendars(self) -> Iterator[tuple[str, Calendar]]:
        for cal in self._all_calendars():
            try:
                name = self._name(cal)
            except Exception:
                continue
            if name:
                _ = self._calendars.setdefault(name, cal)
                yield name, cal

    def _get_calendar(self, name: str) -> Calendar:
        if name in self._calendars:
            cal = self._calendars[name]
        else:
            cal: Calendar | None = None
            for c in self._all_calendars():
                try:
                    if self._name(c) == name:
                        cal = c
                        break
                except Exception:
                    continue
            if cal is None:
                if self.dry_run:
                    raise RuntimeError(f"календарь «{name}» не найден (dry-run, не создаю)")
                log.info("создаю календарь iCloud «%s»", name)
                cal = _retry(
                    lambda: cast("Calendar", self._principal_().make_calendar(name=name)),  # pyright: ignore[reportUnknownMemberType]
                    f"make_calendar {name}",
                )
            else:
                log.info("календарь iCloud: «%s»", name)
            self._calendars[name] = cal
        self._apply_color(cal, name)
        return cal

    def _apply_color(self, cal: Calendar, name: str) -> None:
        want = self.cfg.colors.get(name)
        if not want or self.dry_run or name in self._colored:
            return
        _ = self._colored.add(name)
        try:
            got = cal.get_property(CalendarColor())  # pyright: ignore[reportUnknownMemberType]
            cur = str(cast("object", got) or "").lower()
            if cur[:7] != want.lower():  # Apple хранит #RRGGBBAA
                _ = cal.set_properties([CalendarColor(want)])
                log.info("цвет календаря «%s» -> %s", name, want)
        except Exception as e:
            log.warning("цвет календаря «%s» не выставлен: %s", name, e)

    def _index(self, cal: Calendar) -> dict[str, CalendarObjectResource]:
        out: dict[str, CalendarObjectResource] = {}
        for obj in self._events(cal, "events()"):
            comp = self._vevent(obj)
            if comp is None:
                continue
            uid = prop(comp, "uid")
            if uid:
                out[uid] = obj
        return out

    def _upsert(
        self, cal: Calendar, obj: CalendarObjectResource | None, ev: Event, rep: SyncReport
    ) -> None:
        if obj is None:
            if self.dry_run:
                log.info("[dry] + %s  %s", ev.start.strftime("%d.%m %H:%M"), ev.title)
            else:
                _ = _retry(
                    lambda: cast("object", cal.save_event(ical.build_vcalendar(ev).decode())),  # pyright: ignore[reportUnknownMemberType]
                    f"create {ev.uid}",
                )
            rep.created += 1
            return

        comp = self._vevent(obj)
        unchanged = (
            comp is not None
            and prop(comp, HASH) == ical.content_hash(ev)
            and prop(comp, MANAGED) == "yes"
            and not prop(comp, VANISHED_AT)
        )
        if unchanged:
            rep.skipped += 1
            return

        seq = ical.get_int(comp, "sequence") + 1 if comp is not None else 0
        if self.dry_run:
            log.info("[dry] ~ %s  %s", ev.start.strftime("%d.%m %H:%M"), ev.title)
        else:
            obj.data = ical.build_vcalendar(ev, sequence=seq).decode()
            _ = _retry(obj.save, f"update {ev.uid}")
        rep.updated += 1

    def _reconcile_gone(
        self,
        obj: CalendarObjectResource,
        comp: icalendar.Component,
        defaults: Defaults,
        rep: SyncReport,
    ) -> None:
        title = prop(comp, "summary") or "?"
        policy = defaults.on_vanished
        if policy == "keep":
            return
        if policy == "delete":
            if self.dry_run:
                log.info("[dry] - %s (пропало из источника)", title)
            else:
                _ = _retry(obj.delete, "delete (gone)")
            rep.deleted += 1
            return

        first_gone = prop(comp, VANISHED_AT)
        today = datetime.now(UTC).date()
        if first_gone:
            try:
                gone_since = date.fromisoformat(first_gone)
            except ValueError:
                gone_since = today
            if today - gone_since >= timedelta(days=defaults.mark_grace_days):
                if self.dry_run:
                    log.info("[dry] - %s (grace истёк)", title)
                else:
                    _ = _retry(obj.delete, "delete (grace)")
                rep.deleted += 1
            else:
                rep.skipped += 1
            return

        if self.dry_run:
            log.info("[dry] ! %s (помечаю отменённым)", title)
            rep.marked += 1
            return
        inst = ical.parse(cast("str", obj.data))
        ve = ical.vevents(inst)[0]
        cur = ical.prop(ve, "summary") or ""
        if not cur.startswith(defaults.mark_prefix):
            ical.set_prop(ve, "summary", defaults.mark_prefix + cur)
        ical.set_prop(ve, VANISHED_AT, today.isoformat())
        ical.set_prop(ve, "sequence", ical.get_int(ve, "sequence") + 1)
        obj.data = inst.to_ical().decode()
        _ = _retry(obj.save, "mark")
        rep.marked += 1
