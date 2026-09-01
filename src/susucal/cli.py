import argparse
import logging
import os
import re
import sys
import time
from typing import override

from susucal import config
from susucal.pipeline import run_all, state_dir

log = logging.getLogger("susucal")

_SECRET_HINTS = ("authtoken=", "password", "bearer ", "refreshtoken")


class _RedactFilter(logging.Filter):
    """Не дает в логи попасть секретам"""

    @override
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if any(h in msg.lower() for h in _SECRET_HINTS):
            msg = re.sub(r"(authtoken=)[^&\s]+", r"\1<redacted>", msg, flags=re.IGNORECASE)
            msg = re.sub(r"(bearer\s+)[A-Za-z0-9._\-]+", r"\1<redacted>", msg, flags=re.IGNORECASE)
            record.msg, record.args = msg, ()
        return True


def setup_logging(verbose: bool) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    h.addFilter(_RedactFilter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_interval(raw: str | None) -> int:
    """'10800' | '3h' | '90m' | '45s' -> секунды. Пусто/0 -> 0 (один прогон)."""
    if not raw:
        return 0
    m = re.fullmatch(r"(\d+)\s*([hms]?)", raw.strip(), re.IGNORECASE)
    if not m:
        raise ValueError(f"не понял интервал: {raw!r}")
    return int(m[1]) * {"h": 3600, "m": 60, "s": 1, "": 1}[m[2].lower()]


class _Args(argparse.Namespace):
    config: str | None = None
    only: list[str] | None = None
    dry_run: bool = False
    plan: bool = False
    interval: str | None = None
    verbose: bool = False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="susucal", description=__doc__)
    _ = ap.add_argument(
        "-c",
        "--config",
        help="путь к settings.yml/json (иначе автопоиск / $SUSUCAL_CONFIG)",
    )
    _ = ap.add_argument(
        "--only",
        action="append",
        metavar="NAME",
        help="синкать только этих пользователей (можно повторять)",
    )
    _ = ap.add_argument(
        "--dry-run",
        action="store_true",
        help="подключиться к iCloud, но ничего не писать",
    )
    _ = ap.add_argument(
        "--plan",
        action="store_true",
        help="только фетч+фильтр, распечатать список событий; iCloud не трогать",
    )
    _ = ap.add_argument(
        "--interval",
        default=os.getenv("SUSUCAL_INTERVAL"),
        help="повторять с этим интервалом ('3h', '90m', секунды); иначе один прогон",
    )
    _ = ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=bool(os.getenv("SUSUCAL_VERBOSE")),
        help="debug-логи (или env SUSUCAL_VERBOSE=1)",
    )
    args = ap.parse_args(argv, namespace=_Args())

    setup_logging(args.verbose)
    try:
        interval = parse_interval(args.interval)
    except ValueError as e:
        log.error("%s", e)
        return 2

    try:
        settings = config.load(args.config)
    except config.ConfigError as e:
        log.error("конфиг: %s", e)
        return 2
    sdir = state_dir(config.find_config(args.config))

    while True:
        try:
            ok = run_all(
                settings,
                sdir,
                only=args.only,
                dry_run=args.dry_run,
                plan_only=args.plan,
            )
        except KeyboardInterrupt:
            log.info("остановлено")
            return 0
        log.info("готово%s", "" if ok else " (были ошибки, см. выше)")
        if interval <= 0 or args.plan:
            return 0 if ok else 1
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("остановлено")
            return 0
        try:
            settings = config.load(args.config)
        except config.ConfigError as e:
            log.error("конфиг: %s (оставляю прежний)", e)


if __name__ == "__main__":
    raise SystemExit(main())
