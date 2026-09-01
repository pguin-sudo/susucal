import hashlib
from dataclasses import dataclass, field
from datetime import datetime

Source = str  # "moodle" | "univeris"


@dataclass(frozen=True, slots=True)
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    location: str | None
    description: str | None
    source: Source
    all_day: bool = False
    tags: dict[str, str] = field(default_factory=dict, compare=False)

    def with_uid(self, uid: str) -> "Event":
        return Event(
            uid=uid,
            title=self.title,
            start=self.start,
            end=self.end,
            location=self.location,
            description=self.description,
            source=self.source,
            all_day=self.all_day,
            tags=dict(self.tags),
        )


def make_uid(source: Source, *parts: object) -> str:
    """Детерминированный UID из значимых полей: одинаковый вход дал бы
    одинаковый UID между запусками, чтобы синк делал update, а не дубликат."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:20]
    return f"{source}-{digest}@susucal"
