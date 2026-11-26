"""Bundle-awareness.

A packaged glance has a different TCC identity from the terminal that built
it, so it needs different instructions. Getting this wrong is quiet and
expensive: the app tells the user to grant permission to their editor, they
do, and nothing changes.
"""
import pytest

from glance.platform_mac import permission_help, running_in_bundle


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GLANCE_BUNDLE_ID", raising=False)
    monkeypatch.delenv("__CFBundleIdentifier", raising=False)


def test_not_a_bundle_when_launched_from_a_terminal():
    assert running_in_bundle() is False


def test_launchservices_launch_is_detected(monkeypatch):
    """`open glance.app` goes through LaunchServices, which exports this."""
    monkeypatch.setenv("__CFBundleIdentifier", "com.glance.app")
    assert running_in_bundle() is True


def test_launchd_launch_is_detected(monkeypatch):
    """launchd execs the bundle script directly and sets no CF variable, so
    the launcher exports its own. Checking only __CFBundleIdentifier gave a
    bundled app the terminal instructions."""
    monkeypatch.setenv("GLANCE_BUNDLE_ID", "com.glance.app")
    assert running_in_bundle() is True


def test_terminal_help_blames_the_terminal():
    help_text = permission_help()
    assert "terminal" in help_text.lower()
    assert "glance.app has to be allowed" not in help_text


def test_bundle_help_names_the_app_not_the_terminal(monkeypatch):
    monkeypatch.setenv("GLANCE_BUNDLE_ID", "com.glance.app")
    help_text = permission_help()
    assert "glance.app" in help_text
    assert "does NOT carry over" in help_text


def test_both_messages_say_where_to_click():
    for env in (None, "com.glance.app"):
        import os

        if env:
            os.environ["GLANCE_BUNDLE_ID"] = env
        else:
            os.environ.pop("GLANCE_BUNDLE_ID", None)
        assert "Screen & System Audio Recording" in permission_help()
    os.environ.pop("GLANCE_BUNDLE_ID", None)
