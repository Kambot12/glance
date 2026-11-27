"""Core data types. Frames are in-memory only; nothing here touches disk."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:  # keep import cost off the hot path
    from PIL.Image import Image

Region = tuple[int, int, int, int]  # x, y, w, h


@dataclass(frozen=True)
class Frame:
    """A single captured image. Never serialized to disk unless --debug-save."""

    image: "Image"
    ts: float = field(default_factory=time.time)
    display: int = 1
    region: Region | None = None
    phash: object | None = None  # imagehash.ImageHash, kept loose to avoid the import
    ocr_text: str | None = None  # filled only when ocr.mode == 'augment'

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size

    def __repr__(self) -> str:  # never let image bytes reach a log line
        return f"Frame(size={self.size}, display={self.display}, region={self.region})"


@dataclass
class Exchange:
    """One question/answer pair of follow-up context."""

    question: str
    answer: str


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Block:
    reason: str


Verdict = Allow | Block

EventKind = Literal["mode", "pause", "followup", "watch", "panel"]


@dataclass
class Event:
    kind: EventKind
    mode: str | None = None
    text: str | None = None
    manual: bool = False
    #: Where the trigger came from. "hotkey" and "ui" are both manual presses
    #: and behave identically; they are distinguished only so the log can say
    #: which one actually fired - without it a keypress and a click on the web
    #: view are indistinguishable after the fact.
    origin: str = "hotkey"
