from __future__ import annotations

from glance.vision.base import VisionProcessor
from glance.vision.mock import MockVisionProcessor

__all__ = ["VisionProcessor", "MockVisionProcessor", "build_processor"]


def build_processor(kind: str, provider_cfg, jpeg_quality: int = 70) -> VisionProcessor:
    if kind == "mock":
        return MockVisionProcessor()
    if kind in ("openai", "real", "openai-compat"):
        from glance.vision.openai_compat import OpenAICompatProcessor

        return OpenAICompatProcessor(provider_cfg, jpeg_quality=jpeg_quality)
    raise ValueError(f"unknown processor {kind!r}; expected 'mock' or 'openai'")
