"""Capture source that reads a PNG from disk, for headless runs and tests."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

from glance.capture.base import prepare
from glance.models import Frame, Region


class FileCaptureSource:
    name = "file"
    captures_screen = False

    def __init__(self, path: str | Path, max_edge: int = 1600, display: int = 1):
        self.path = Path(path).expanduser()
        if not self.path.exists():
            raise FileNotFoundError(f"no such image: {self.path}")
        self.max_edge = max_edge
        self.display = display

    def grab(self, region: Region | None = None) -> Frame:
        # Re-read every grab so the file can be swapped mid-run.
        with Image.open(self.path) as im:
            img = im.convert("RGB")
        return prepare(img, region, self.max_edge, self.display)

    def close(self) -> None:
        pass
