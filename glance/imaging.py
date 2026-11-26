"""Pure image helpers: crop, downscale, compress, perceptual-hash dedup.

Deliberately free of config and I/O so the test suite can hit them directly.
"""
from __future__ import annotations

import base64
import io

import imagehash
from PIL import Image

from glance.models import Region


class RegionError(ValueError):
    """A region that cannot be reconciled with the source image."""


def crop_region(img: Image.Image, region: Region | None) -> Image.Image:
    """Crop to (x, y, w, h). Raises rather than silently returning a black frame."""
    if region is None:
        return img
    x, y, w, h = region
    if w <= 0 or h <= 0:
        raise RegionError(f"region must have positive width and height, got {w}x{h}")
    if x < 0 or y < 0:
        raise RegionError(f"region origin must be non-negative, got ({x}, {y})")
    iw, ih = img.size
    if x + w > iw or y + h > ih:
        raise RegionError(
            f"region {region} does not fit inside image {iw}x{ih}"
        )
    return img.crop((x, y, x + w, y + h))


def downscale(img: Image.Image, max_edge: int) -> Image.Image:
    """Shrink so the longest edge is max_edge. Never upscales."""
    if max_edge <= 0:
        raise ValueError("max_edge must be positive")
    w, h = img.size
    longest = max(w, h)
    if longest <= max_edge:
        return img
    scale = max_edge / longest
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def to_jpeg(img: Image.Image, quality: int) -> bytes:
    """Encode to JPEG bytes in memory."""
    if not 1 <= quality <= 95:
        raise ValueError("quality must be in 1..95")
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def to_data_url(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def phash(img: Image.Image) -> imagehash.ImageHash:
    return imagehash.phash(img)


def is_duplicate(a, b, max_distance: int) -> bool:
    """True when two perceptual hashes are within max_distance (Hamming)."""
    if a is None or b is None:
        return False
    return (a - b) <= max_distance
