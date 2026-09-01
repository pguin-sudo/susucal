import logging
import os
from pathlib import Path

from susucal import config
from susucal.filters import apply as apply_filters
from susucal.icloud import ICloudSync
from susucal.models import Event
from susucal.sources.moodle import MoodleSource
from susucal.sources.univeris import UniverisSource

log = logging.getLogger("susucal")


def state_dir(cfg_path: Path) -> Path:
    d = Path(os.getenv("SUSUCAL_STATE_DIR") or (cfg_path.parent / ".susucal-state"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_all(
    settings: config.Settings,
    sdir: Path,
    *,
    only: list[str] | None,
    dry_run: bool,
    plan_only: bool,
) -> bool:
    targets = [u for u in settings.users if u.enabled and (not only or u.name in only)]
    if not targets:
        log.error("нет подходящих пользователей (enabled + --only)")
        return False
    ok = True
    for user in targets:
        try:
            ok &= run_user(settings, user, sdir, dry_run=dry_run, plan_only=plan_only)
        except Exception:
            log.exception("%s: необработанная ошибка", user.name)
            ok = False
    return ok


def run_user(
    settings: config.Settings,
    user: config.UserCfg,
    sdir: Path,
    *,
    dry_run: bool,
    plan_only: bool,
) -> bool:
    defaults = settings.resolved(user)
    log.info(
        "=== %s === (tz=%s, окно -%d/+%d дн, on_vanished=%s)",
        user.name,
        defaults.timezone,
        defaults.horizon_days_past,
        defaults.horizon_days_future,
        defaults.on_vanished,
    )

    events: list[Event] = []
    active: set[str] = set()
    fetch_errors = 0

    if user.univeris is not None:
        try:
            login, password = user.univeris_credentials()
            got = UniverisSource(
                login,
                password,
                base_url=user.univeris.base_url,
                group_id=user.univeris.group_id,
                state_path=sdir / f"univeris-{user.name}.json",
                tz=defaults.timezone,
            ).fetch()
            events += got
            active.add("univeris")
            log.info("univeris: %d событий", len(got))
        except Exception as e:
            fetch_errors += 1
            log.error("univeris: провал: %s", e)

    if user.moodle is not None:
        try:
            got = MoodleSource(user.moodle.ics_url).fetch()
            events += got
            active.add("moodle")
            log.info("moodle: %d событий", len(got))
        except Exception as e:
            fetch_errors += 1
            log.error("moodle: провал: %s", e)

    if not active:
        log.error("%s: ни один источник не отработал, пропускаю синк", user.name)
        return False

    kept, dropped = apply_filters(events, user.filters, defaults)
    log.info("после фильтров: %d событий (отброшено %d)", len(kept), len(dropped))
    if log.isEnabledFor(logging.DEBUG):
        for d in dropped:
            log.debug(
                "  drop [%s] %s: %s",
                d.event.source,
                d.event.start.strftime("%d.%m %H:%M"),
                d.reason,
            )

    if plan_only:
        for e in sorted(kept, key=lambda x: x.start):
            when = (
                e.start.strftime("%a %d.%m [весь день]")
                if e.all_day
                else e.start.strftime("%a %d.%m %H:%M")
            )
            log.info(
                "  %-18s %-7s %s%s",
                when,
                e.source,
                e.title,
                f"  · {e.location}" if e.location else "",
            )
        return fetch_errors == 0

    try:
        rep = ICloudSync(user.icloud, dry_run=dry_run).sync(
            kept, defaults=defaults, active_sources=active
        )
    except Exception as e:
        log.exception("%s: синк с iCloud провалился: %s", user.name, e)
        return False

    cals = sorted({c for s in active for c in user.icloud.all_calendars_for(s)})
    log.info("%s: iCloud [%s] %s", user.name, ", ".join(f"«{c}»" for c in cals), rep.line())
    for err in rep.errors[:10]:
        log.warning("  %s", err)
    return fetch_errors == 0 and not rep.errors
