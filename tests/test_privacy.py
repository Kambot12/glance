"""Denylist guard, including the two macOS-driven corrections."""
from glance.config import PrivacyConfig
from glance.models import Allow, Block
from glance.platform_mac import WindowInfo
from glance.privacy import PrivacyGuard


def win(owner, title, bounds=(0, 0, 400, 300)):
    return WindowInfo(owner=owner, title=title, bounds=bounds, window_id=1)


def guard(cfg, windows):
    return PrivacyGuard(cfg, window_lister=lambda: windows)


def test_no_denylist_allows_everything():
    g = guard(PrivacyConfig(), [win("1Password", "Vault")])
    assert isinstance(g.check(None), Allow)


def test_denied_process_blocks():
    cfg = PrivacyConfig(denied_processes=["1Password"])
    v = guard(cfg, [win("1Password", "Vault")]).check(None)
    assert isinstance(v, Block) and "1Password" in v.reason


def test_process_match_is_case_insensitive_and_partial():
    cfg = PrivacyConfig(denied_processes=["bitwarden"])
    assert isinstance(guard(cfg, [win("Bitwarden Desktop", None)]).check(None), Block)


def test_denied_title_blocks():
    cfg = PrivacyConfig(denied_window_titles=["bank"])
    assert isinstance(guard(cfg, [win("Safari", "My Bank - Accounts")]).check(None), Block)


def test_unreadable_title_fails_closed():
    """On macOS a title needs Screen Recording permission; unknown != safe."""
    cfg = PrivacyConfig(denied_window_titles=["bank"], fail_closed_on_unknown_title=True)
    v = guard(cfg, [win("Safari", None)]).check(None)
    assert isinstance(v, Block) and "could not be read" in v.reason


def test_unreadable_title_can_be_allowed_explicitly():
    cfg = PrivacyConfig(denied_window_titles=["bank"], fail_closed_on_unknown_title=False)
    assert isinstance(guard(cfg, [win("Safari", None)]).check(None), Allow)


def test_a_denied_window_is_caught_even_when_it_is_not_frontmost():
    """A full-display capture includes a password manager parked in a corner."""
    cfg = PrivacyConfig(denied_processes=["1Password"])
    windows = [win("Code", "main.py"), win("1Password", "Vault", (1600, 900, 300, 200))]
    assert isinstance(guard(cfg, windows).check(None), Block)


def test_a_region_capture_ignores_a_denied_window_outside_it():
    cfg = PrivacyConfig(denied_processes=["1Password"])
    windows = [win("1Password", "Vault", (1600, 900, 300, 200))]
    assert isinstance(guard(cfg, windows).check((0, 0, 800, 600)), Allow)


def test_a_region_capture_catches_a_denied_window_overlapping_it():
    cfg = PrivacyConfig(denied_processes=["1Password"])
    windows = [win("1Password", "Vault", (700, 500, 300, 200))]
    assert isinstance(guard(cfg, windows).check((0, 0, 800, 600)), Block)
