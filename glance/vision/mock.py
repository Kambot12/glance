"""Deterministic processor so the whole pipeline runs with no API key."""
from __future__ import annotations

import asyncio
from typing import AsyncIterator

from glance.config import ModeConfig
from glance.models import Exchange, Frame


class MockVisionProcessor:
    name = "mock"
    endpoint = "mock://local"

    def __init__(self, chunk_delay_s: float = 0.04):
        self.chunk_delay_s = chunk_delay_s

    async def stream(
        self,
        frame: Frame,
        mode: ModeConfig,
        history: list[Exchange],
        user_text: str | None = None,
    ) -> AsyncIterator[str]:
        w, h = frame.size
        words = (
            f"[mock:{mode.name}] Received a {w}x{h} frame from display "
            f"{frame.display} with {len(history)} prior exchange(s). "
            f"Region: {frame.region or 'full display'}. "
            f"OCR: {'attached' if frame.ocr_text else 'none'}. "
            f"Question: {user_text or 'none'}. "
            f"This is a fixed response, so the capture, transport and web view "
            f"can all be verified without a model or an API key."
        ).split(" ")
        for word in words:
            await asyncio.sleep(self.chunk_delay_s)
            yield word + " "

    async def aclose(self) -> None:
        pass
