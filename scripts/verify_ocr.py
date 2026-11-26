"""Run the real rapidocr engine against assets/sample.png.

First run downloads the ONNX models. Checks that text actually drawn into the
sample is recovered, so this fails loudly rather than reporting empty output.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glance.models import Frame  # noqa: E402
from glance.ocr import RapidOcrEngine  # noqa: E402

EXPECTED = ["1.24M", "412", "0.38", "184.20", "api/search", "psycopg", "orders"]


def main() -> int:
    img = Image.open(ROOT / "assets" / "sample.png").convert("RGB")
    engine = RapidOcrEngine(min_confidence=0.5)

    t0 = time.perf_counter()
    result = engine.extract(Frame(image=img))
    load_and_first = time.perf_counter() - t0

    t1 = time.perf_counter()
    engine.extract(Frame(image=img))  # warm
    warm = time.perf_counter() - t1

    print(f"engine        : {engine.name}")
    print(f"first call    : {load_and_first:.2f}s (includes model load)")
    print(f"warm call     : {warm:.2f}s")
    print(f"lines         : {result.line_count}")
    print(f"mean conf     : {result.mean_confidence:.3f}")
    print("--- extracted (first 20 lines) ---")
    for line in result.text.splitlines()[:20]:
        print("  " + line)

    missing = [e for e in EXPECTED if e.lower() not in result.text.lower()]
    print()
    if not result:
        print("FAIL: no text extracted")
        return 1
    if missing:
        print(f"PARTIAL: {len(EXPECTED) - len(missing)}/{len(EXPECTED)} expected "
              f"strings found; missing {missing}")
        return 0
    print(f"PASS: all {len(EXPECTED)} expected strings recovered")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
