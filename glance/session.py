"""In-memory session state. Frames never leave this process."""
from __future__ import annotations

from collections import deque

from glance.models import Exchange, Frame


class SessionState:
    def __init__(self, max_exchanges: int):
        self.max_exchanges = max_exchanges
        self.last_frame: Frame | None = None
        self.last_mode: str | None = None
        self.paused = False
        self._history: dict[str, deque[Exchange]] = {}

    def history(self, mode: str) -> list[Exchange]:
        return list(self._history.get(mode, ()))

    def record(self, mode: str, question: str, answer: str) -> None:
        if self.max_exchanges == 0:
            return
        q = self._history.setdefault(mode, deque(maxlen=self.max_exchanges))
        q.append(Exchange(question=question, answer=answer))

    def clear(self, mode: str | None = None) -> None:
        if mode is None:
            self._history.clear()
        else:
            self._history.pop(mode, None)
