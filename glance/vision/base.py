from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from glance.config import ModeConfig
from glance.models import Exchange, Frame


#: The user turn sent with a frame when a mode states no user_prompt of its
#: own. It lives here so the processor and the orchestrator cannot drift: the
#: orchestrator records it in the conversation history, and history that does
#: not match what was actually sent produces an incoherent transcript.
DEFAULT_USER_PROMPT = "Here is the current screen capture."


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
