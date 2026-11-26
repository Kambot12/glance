"""Denylist guard: skip capture when a sensitive window is on screen.

Two corrections to the naive design, both forced by measured macOS behaviour:

1. Checking only the frontmost window is not enough. A full-display capture
   also picks up a password manager parked in a corner, so every on-screen
   window overlapping the capture region is checked.
2. A window title is only readable with Screen Recording permission, while the
   owning process name always is. An unreadable title is therefore 'unknown',
   not 'safe', and with fail_closed_on_unknown_title we skip the capture.
"""
from __future__ import annotations

from glance.config import PrivacyConfig
from glance.models import Allow, Block, Region, Verdict
from glance.platform_mac import WindowInfo, list_windows


def _overlaps(win: tuple[int, int, int, int], region: Region | None) -> bool:
    if region is None:
        return True  # whole display: everything on screen is in frame
    wx, wy, ww, wh = win
    rx, ry, rw, rh = region
    if ww <= 0 or wh <= 0:
        return False
    return not (wx + ww <= rx or rx + rw <= wx or wy + wh <= ry or ry + rh <= wy)


class PrivacyGuard:
    def __init__(self, cfg: PrivacyConfig, window_lister=list_windows):
        self.cfg = cfg
        self._list = window_lister

    def check(self, region: Region | None = None) -> Verdict:
        denied_procs = [p.casefold() for p in self.cfg.denied_processes]
        denied_titles = [t.casefold() for t in self.cfg.denied_window_titles]
        if not denied_procs and not denied_titles:
            return Allow()

        windows: list[WindowInfo] = self._list()
        for w in windows:
            if not _overlaps(w.bounds, region):
                continue
            owner = (w.owner or "").casefold()
            for p in denied_procs:
                if p and p in owner:
                    return Block(f"{w.owner!r} is on the denylist")
            if w.title is None:
                if denied_titles and self.cfg.fail_closed_on_unknown_title:
                    return Block(
                        f"window title for {w.owner or 'an unknown app'} could not be "
                        "read (needs Screen Recording permission), so it cannot be "
                        "checked against the title denylist"
                    )
                continue
            title = w.title.casefold()
            for t in denied_titles:
                if t and t in title:
                    return Block(f"a window title matched {t!r}")
        return Allow()
