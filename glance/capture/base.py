from __future__ import annotations

from typing import Protocol, runtime_checkable

from PIL import Image

from glance import imaging
from glance.models import Frame, Region


@runtime_checkable
class CaptureSource(Protocol):
    name: str
    # True when grab() reads the real screen. The privacy denylist only means
    # anything for those; a file source reads a PNG and involves no windows.
    captures_screen: bool

    def grab(self, region: Region | None = None) -> Frame: ...
    def close(self) -> None: ...


def prepare(
    img: Image.Image, region: Region | None, max_edge: int, display: int
) -> Frame:
    """Shared post-capture path: crop -> downscale -> perceptual hash."""
    cropped = imaging.crop_region(img, region)
    scaled = imaging.downscale(cropped, max_edge)
    return Frame(
        image=scaled, display=display, region=region, phash=imaging.phash(scaled)
    )
