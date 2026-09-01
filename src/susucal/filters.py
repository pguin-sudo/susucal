import logging
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from susucal.config import Defaults, Filters, SlotRule, SubgroupPick
from susucal.models import Event

log = logging.getLogger("susucal.filters")

_WEEKDAYS: dict[str, int] = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

# расписание звонков ЮУрГУ: номер пары -> время начала
SUSU_BELLS: dict[int, str] = {
    1: "08:00",
    2: "09:45",
    3: "11:30",
    4: "13:35",
    5: "15:20",
    6: "17:05",
    7: "18:50",
    8: "20:35",
}
_BELL_BY_TIME: dict[str, int] = {v: k for k, v in SUSU_BELLS.items()}


def week_number(d: date, semester_start: date) -> int:
    """1-based номер учебной недели. Неделя 1 содержит semester_start."""
    start_monday = semester_start - timedelta(days=semester_start.weekday())
    return (d - start_monday).days // 7 + 1


def pair_number(dt: datetime) -> int | None:
    return _BELL_BY_TIME.get(dt.strftime("%H:%M"))


@dataclass(slots=True)
class Dropped:
    event: Event
    reason: str


def apply(
    events: list[Event],
    filters: Filters,
    defaults: Defaults,
    *,
    today: date | None = None,
) -> tuple[list[Event], list[Dropped]]:
    today = today or datetime.now().date()
    lo = today - timedelta(days=defaults.horizon_days_past)
    hi = today + timedelta(days=defaults.horizon_days_future)

    dropped: list[Dropped] = []

    windowed: list[Event] = []
    for ev in events:
        if lo <= ev.start.date() <= hi:
            windowed.append(ev)
        else:
            dropped.append(Dropped(ev, f"вне окна {lo}..{hi}"))

    uni = [e for e in windowed if e.source == "univeris"]
    other = [e for e in windowed if e.source != "univeris"]

    if filters.event_types:
        allow = set(filters.event_types)
        uni, drop = _split(uni, lambda e: e.tags.get("event_type", "") in allow)
        dropped += [Dropped(e, "тип занятия не в event_types") for e in drop]

    sf = filters.subjects
    if sf.mode == "include":
        wanted = set(sf.names)
        uni, drop = _split(uni, lambda e: e.title in wanted)
        dropped += [Dropped(e, "нет в subjects.names (include)") for e in drop]
    elif sf.mode == "exclude":
        banned = set(sf.names)
        uni, drop = _split(uni, lambda e: e.title not in banned)
        dropped += [Dropped(e, "в subjects.names (exclude)") for e in drop]

    if filters.weekday_whitelist:
        wl: dict[str, set[str]] = {
            wd: set(names) for wd, names in filters.weekday_whitelist.items()
        }
        rev: dict[int, str] = {v: k for k, v in _WEEKDAYS.items()}

        def wl_ok(e: Event) -> bool:
            allow = wl.get(rev[e.start.weekday()])
            return allow is None or e.title in allow

        uni, drop = _split(uni, wl_ok)
        dropped += [Dropped(e, "не в weekday_whitelist для этого дня") for e in drop]

    # для разбитых пар оставляем один вариант
    uni, sub_drop = _pick_subgroups(uni, filters)
    dropped += sub_drop

    uni, slot_drop = _apply_slots(uni, filters.exclude_slots, defaults.semester_start)
    dropped += slot_drop

    return other + uni, dropped


def _split(events: list[Event], pred: Callable[[Event], bool]) -> tuple[list[Event], list[Event]]:
    keep: list[Event] = []
    drop: list[Event] = []
    for e in events:
        (keep if pred(e) else drop).append(e)
    return keep, drop


def _pick_subgroups(events: list[Event], filters: Filters) -> tuple[list[Event], list[Dropped]]:
    groups: dict[tuple[str, datetime, datetime], list[Event]] = defaultdict(list)
    kept: list[Event] = []
    for e in events:
        if e.tags.get("split") == "1":
            groups[(e.title, e.start, e.end)].append(e)
        else:
            kept.append(e)

    dropped: list[Dropped] = []
    for (title, _s, _e), variants in groups.items():
        rule = filters.subgroups.get(title)
        if rule is None:
            mode = filters.split_without_rule
            if mode == "keep_all":
                kept += variants
            elif mode == "first":
                kept.append(variants[0])
                dropped += [Dropped(v, "split без правила, взят первый") for v in variants[1:]]
            else:
                dropped += [Dropped(v, f"split '{title}' без правила subgroups") for v in variants]
            continue
        chosen = [v for v in variants if _subgroup_match(v, rule)]
        if not chosen:
            log.warning("подгруппа для '%s': ни один вариант не совпал с правилом", title)
            kept += variants
        else:
            kept += chosen[:1]
            dropped += [
                Dropped(v, f"другая подгруппа '{title}'") for v in variants if v not in chosen[:1]
            ]
    return kept, dropped


def _subgroup_match(ev: Event, rule: SubgroupPick) -> bool:
    if rule.location and (ev.location or "").strip() != rule.location.strip():
        return False
    return not (
        rule.instructor and rule.instructor.lower() not in ev.tags.get("teacher", "").lower()
    )


def _apply_slots(
    events: list[Event], rules: list[SlotRule], semester_start: date
) -> tuple[list[Event], list[Dropped]]:
    if not rules:
        return events, []
    kept: list[Event] = []
    dropped: list[Dropped] = []
    for e in events:
        hit = next((r for r in rules if _slot_match(e, r, semester_start)), None)
        if hit is None:
            kept.append(e)
        else:
            dropped.append(Dropped(e, f"слот-правило {_rule_repr(hit)}"))
    return kept, dropped


def _slot_match(ev: Event, r: SlotRule, semester_start: date) -> bool:
    d = ev.start.date()
    if r.weekday is not None and d.weekday() != _WEEKDAYS[r.weekday]:
        return False
    if r.subject is not None and ev.title != r.subject:
        return False
    if r.pair is not None and pair_number(ev.start) != r.pair:
        return False
    if r.begin_time is not None and ev.start.strftime("%H:%M") != _norm_hhmm(r.begin_time):
        return False
    if r.week != "all" or r.week_numbers:
        wn = week_number(d, semester_start)
        if r.week_numbers and wn not in r.week_numbers:
            return False
        if r.week == "odd" and wn % 2 == 0:
            return False
        if r.week == "even" and wn % 2 == 1:
            return False
    return True


def _norm_hhmm(s: str) -> str:
    h, m = s.split(":")
    return f"{int(h):02d}:{m}"


def _rule_repr(r: SlotRule) -> str:
    bits: list[str] = []
    if r.weekday:
        bits.append(r.weekday)
    if r.pair:
        bits.append(f"пара {r.pair}")
    if r.begin_time:
        bits.append(r.begin_time)
    if r.week != "all":
        bits.append(f"{r.week} нед.")
    if r.week_numbers:
        bits.append(f"нед.{r.week_numbers}")
    if r.subject:
        bits.append(f"«{r.subject}»")
    return ", ".join(bits)
