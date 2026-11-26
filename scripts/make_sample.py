"""Generate assets/sample.png - a synthetic dashboard for the headless path."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

W, H = 1440, 900
BG, PANEL, GRID = (24, 26, 31), (31, 34, 41), (48, 52, 62)
FG, MUTED, ACCENT, WARN, OK = (
    (231, 233, 238), (154, 161, 174), (110, 168, 254), (232, 184, 75), (94, 201, 138),
)


def panel(d, box, title):
    d.rounded_rectangle(box, 8, fill=PANEL, outline=GRID)
    d.text((box[0] + 16, box[1] + 12), title, fill=MUTED)


def main() -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, W, 56), fill=PANEL)
    d.text((24, 20), "Service Overview  /  production", fill=FG)
    d.text((W - 190, 20), "last 24 hours", fill=MUTED)

    # KPI row
    for i, (label, value, color) in enumerate(
        [("Requests", "1.24M", FG), ("p95 latency", "412 ms", WARN),
         ("Error rate", "0.38 %", OK), ("Cost / day", "$184.20", FG)]
    ):
        x = 24 + i * 352
        panel(d, (x, 80, x + 328, 196), label)
        d.text((x + 16, 130), value, fill=color)

    # Line chart
    panel(d, (24, 216, 728, 600), "Latency p50 / p95 (ms)")
    x0, y0, x1, y1 = 56, 268, 700, 568
    for i in range(5):
        y = y0 + i * (y1 - y0) // 4
        d.line((x0, y, x1, y), fill=GRID)
        d.text((x0 - 28, y - 7), str(800 - i * 200), fill=MUTED)
    p50 = [(x0 + i * 8, y1 - 40 - 22 * math.sin(i / 7)) for i in range(81)]
    p95 = [(x0 + i * 8, y1 - 120 - 70 * math.sin(i / 5) - i * 0.9) for i in range(81)]
    d.line(p50, fill=ACCENT, width=2)
    d.line(p95, fill=WARN, width=2)
    d.text((x0, y1 + 14), "p50", fill=ACCENT)
    d.text((x0 + 48, y1 + 14), "p95  (rising)", fill=WARN)

    # Bar chart
    panel(d, (752, 216, 1416, 600), "Requests by endpoint")
    for i, (name, frac) in enumerate(
        [("/api/search", 0.95), ("/api/users", 0.62), ("/api/orders", 0.48),
         ("/healthz", 0.31), ("/api/export", 0.12)]
    ):
        y = 268 + i * 58
        d.text((784, y), name, fill=MUTED)
        d.rounded_rectangle((920, y - 4, 920 + int(460 * frac), y + 20), 4, fill=ACCENT)

    # Log table
    panel(d, (24, 620, 1416, 876), "Recent errors")
    rows = [
        ("12:04:11", "WARN ", "search", "upstream timeout after 3000ms (retry 2/3)"),
        ("12:03:58", "ERROR", "orders", "psycopg.OperationalError: connection refused"),
        ("12:03:40", "ERROR", "orders", "psycopg.OperationalError: connection refused"),
        ("12:02:12", "WARN ", "export", "queue depth 1842 exceeds soft limit 1000"),
        ("12:01:03", "INFO ", "users", "cache warm complete in 812ms"),
    ]
    for i, (ts, lvl, svc, msg) in enumerate(rows):
        y = 672 + i * 38
        color = {"ERROR": (239, 107, 107), "WARN ": WARN}.get(lvl, MUTED)
        d.text((48, y), ts, fill=MUTED)
        d.text((140, y), lvl, fill=color)
        d.text((212, y), svc, fill=MUTED)
        d.text((300, y), msg, fill=FG)

    out = Path(__file__).resolve().parents[1] / "assets" / "sample.png"
    out.parent.mkdir(exist_ok=True)
    img.save(out)
    print(f"wrote {out} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
