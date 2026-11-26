from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from glance.config import ModeConfig
from glance.models import Exchange, Frame


@runtime_checkable
class VisionProcessor(Protocol):
    name: str
    endpoint: str

    def stream(
        self,
        frame: Frame,
        mode: ModeConfig,
        history: list[Exchange],
        user_text: str | None = None,
    ) -> AsyncIterator[str]:
        """Async stream of text chunks. Every output path is streaming.

        user_text carries a follow-up question. None means 'just act on the
        capture under the mode's system prompt'.
        """
        ...

    async def aclose(self) -> None: ...
