from abc import ABC, abstractmethod

from susucal.models import Event


class Source(ABC):
    name: str  # "moodle" | "univeris", попадает в Event.source

    @abstractmethod
    def fetch(self) -> list[Event]:
        raise NotImplementedError
