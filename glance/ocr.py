"""OCR via rapidocr + onnxruntime (the maintained package, not the legacy
rapidocr-onnxruntime).

Two roles, set by `ocr.mode` in config:

  augment   extract on-image text and hand it to the model alongside the image,
            so a small local VLM does not have to read 11px log lines itself.
  fallback  only used when the model endpoint fails; the extracted text is
            returned instead, so a dead endpoint still yields something useful.
  off       never run.

onnxruntime is CPU-heavy, so the engine runs in its own single-worker executor
and never blocks capture or the WebSocket.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from glance.models import Frame

log = logging.getLogger("glance.ocr")


@dataclass(frozen=True)
class OcrResult:
    text: str
    line_count: int
    mean_confidence: float

    def __bool__(self) -> bool:
        return bool(self.text.strip())


EMPTY = OcrResult(text="", line_count=0, mean_confidence=0.0)


@runtime_checkable
class OcrEngine(Protocol):
    name: str

    def extract(self, frame: Frame) -> OcrResult: ...


class NullOcrEngine:
    name = "none"

    def extract(self, frame: Frame) -> OcrResult:
        return EMPTY


class RapidOcrEngine:
    """Lazy-loads the model on first use; the session is then reused."""

    name = "rapidocr"

    def __init__(self, min_confidence: float = 0.5):
        self.min_confidence = min_confidence
        self._engine = None

    def _load(self):
        if self._engine is None:
            try:
                from rapidocr import RapidOCR  # noqa: PLC0415
            except ImportError as e:
                raise RuntimeError(
                    "OCR requires the 'ocr' extra: uv sync --extra ocr"
                ) from e
            log.info("loading rapidocr (first run downloads the ONNX models)")
            self._engine = RapidOCR()
        return self._engine

    def extract(self, frame: Frame) -> OcrResult:
        import numpy as np  # noqa: PLC0415

        engine = self._load()
        out = engine(np.asarray(frame.image))
        texts = list(getattr(out, "txts", None) or [])
        scores = list(getattr(out, "scores", None) or [])
        if not texts:
            return EMPTY

        kept: list[str] = []
        kept_scores: list[float] = []
        for i, t in enumerate(texts):
            score = float(scores[i]) if i < len(scores) else 1.0
            if score >= self.min_confidence and str(t).strip():
                kept.append(str(t).strip())
                kept_scores.append(score)
        if not kept:
            return EMPTY
        return OcrResult(
            text="\n".join(kept),
            line_count=len(kept),
            mean_confidence=sum(kept_scores) / len(kept_scores),
        )


def build_engine(mode: str, min_confidence: float = 0.5) -> OcrEngine:
    """Never raises: a missing 'ocr' extra degrades to no OCR, with a warning."""
    if mode == "off":
        return NullOcrEngine()
    try:
        import rapidocr  # noqa: F401, PLC0415
    except ImportError:
        log.warning(
            "ocr.mode=%s but the 'ocr' extra is not installed, so OCR is off. "
            "Install it with: uv sync --extra ocr",
            mode,
        )
        return NullOcrEngine()
    return RapidOcrEngine(min_confidence=min_confidence)
