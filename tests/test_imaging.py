"""Region cropping, compression and perceptual dedup."""
import pytest
from PIL import Image

from glance import imaging
from glance.imaging import RegionError


def solid(w, h, color=(120, 40, 200)):
    return Image.new("RGB", (w, h), color)


# -- region cropping ---------------------------------------------------------

def test_crop_region_returns_requested_size():
    out = imaging.crop_region(solid(800, 600), (100, 50, 200, 150))
    assert out.size == (200, 150)


def test_crop_region_none_is_identity():
    img = solid(64, 64)
    assert imaging.crop_region(img, None) is img


def test_crop_region_takes_the_right_pixels():
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    img.putpixel((7, 3), (255, 0, 0))
    out = imaging.crop_region(img, (5, 2, 4, 4))
    assert out.getpixel((2, 1)) == (255, 0, 0)


@pytest.mark.parametrize(
    "region",
    [
        (0, 0, 0, 10),      # zero width
        (0, 0, 10, -5),     # negative height
        (-1, 0, 10, 10),    # negative origin
        (90, 0, 20, 10),    # runs off the right edge
        (0, 95, 10, 20),    # runs off the bottom
    ],
)
def test_crop_region_rejects_bad_regions(region):
    """A bad region must raise, never silently yield a black or clipped frame."""
    with pytest.raises(RegionError):
        imaging.crop_region(solid(100, 100), region)


# -- downscale ---------------------------------------------------------------

def test_downscale_caps_longest_edge_and_keeps_aspect():
    out = imaging.downscale(solid(3200, 1800), 1600)
    assert max(out.size) == 1600
    assert out.size == (1600, 900)


def test_downscale_never_upscales():
    img = solid(400, 300)
    assert imaging.downscale(img, 1600) is img


# -- compression -------------------------------------------------------------

def test_to_jpeg_produces_decodable_jpeg_of_same_size():
    import io

    img = solid(320, 240)
    data = imaging.to_jpeg(img, 70)
    assert data[:2] == b"\xff\xd8"  # JPEG SOI
    assert Image.open(io.BytesIO(data)).size == (320, 240)


def test_lower_quality_is_smaller():
    img = Image.effect_noise((400, 400), 60).convert("RGB")
    assert len(imaging.to_jpeg(img, 30)) < len(imaging.to_jpeg(img, 90))


@pytest.mark.parametrize("q", [0, 96, -1, 200])
def test_to_jpeg_rejects_out_of_range_quality(q):
    with pytest.raises(ValueError):
        imaging.to_jpeg(solid(10, 10), q)


def test_data_url_prefix():
    url = imaging.to_data_url(imaging.to_jpeg(solid(8, 8), 70))
    assert url.startswith("data:image/jpeg;base64,")


# -- dedup -------------------------------------------------------------------

def test_identical_frames_are_duplicates():
    a = imaging.phash(solid(256, 256))
    b = imaging.phash(solid(256, 256))
    assert imaging.is_duplicate(a, b, 4)


def test_different_frames_are_not_duplicates():
    a = imaging.phash(solid(256, 256, (0, 0, 0)))
    noisy = Image.effect_noise((256, 256), 100).convert("RGB")
    assert not imaging.is_duplicate(a, imaging.phash(noisy), 4)


def test_tiny_change_is_deduped_but_counts_at_zero_distance():
    base = Image.effect_noise((256, 256), 50).convert("RGB")
    tweaked = base.copy()
    tweaked.putpixel((0, 0), (255, 255, 255))
    a, b = imaging.phash(base), imaging.phash(tweaked)
    assert imaging.is_duplicate(a, b, 4)


def test_missing_hash_is_never_a_duplicate():
    a = imaging.phash(solid(32, 32))
    assert not imaging.is_duplicate(a, None, 64)
    assert not imaging.is_duplicate(None, None, 64)
