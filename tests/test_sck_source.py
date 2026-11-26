"""ScreenCaptureKitSource: the parts that can be tested without a real screen.

The async-callback plumbing gets the most attention here because a mistake in
it is unusually expensive: the completion handler runs inside an ObjC block, so
a Python exception escaping it (or a non-None return) raises an uncaught
NSException and aborts the whole process rather than failing the capture.
"""
import threading

import pytest

from glance.capture import build_source
from glance.capture.sck_source import _await, _backing_scale


# calling _await on the main thread warns by design; these tests do it on
# purpose, and test_await_warns_when_blocking_the_main_thread asserts it.
pytestmark = pytest.mark.filterwarnings("ignore:.*main thread.*:RuntimeWarning")


# --------------------------------------------------------------- _await ---
def test_await_returns_the_callback_result():
    def fn(handler):
        handler("image", None)

    assert _await(fn, "x") == "image"


def test_await_raises_on_a_reported_error():
    def fn(handler):
        handler(None, "SCStreamErrorDomain -3801")

    with pytest.raises(RuntimeError, match="-3801"):
        _await(fn, "capture")


def test_await_times_out_instead_of_hanging_forever():
    def fn(handler):
        pass  # never calls back, as a wedged XPC daemon would

    with pytest.raises(RuntimeError, match="did not call back"):
        _await(fn, "capture", timeout=0.05)


def test_await_delivers_the_result_from_another_thread():
    def fn(handler):
        threading.Timer(0.02, lambda: handler("late", None)).start()

    assert _await(fn, "x", timeout=2.0) == "late"


def test_handler_returns_none_so_pyobjc_cannot_abort_the_process():
    """pyobjc aborts the process if a void completion block returns non-None.

    Observed for real: a handler written as `lambda c, e: (box.set(c), ev.set())`
    returns a tuple, which pyobjc turns into
    'did not return None, expecting void return value' -> uncaught NSException
    -> the whole app dies. The handler's return value is therefore part of the
    contract, not an incidental detail.
    """
    captured = {}

    def fn(handler):
        captured["handler"] = handler
        handler("image", None)

    _await(fn, "capture")
    assert captured["handler"]("anything", None) is None


def test_handler_swallows_exceptions_instead_of_escaping_the_objc_block():
    """An exception inside the block would abort the process, so it is caught."""
    captured = {}

    def fn(handler):
        captured["handler"] = handler
        handler("image", None)

    _await(fn, "capture")

    # drive the handler with an argument whose assignment into the box raises
    class Hostile:
        def __eq__(self, other):
            raise ValueError("boom")

    assert captured["handler"](Hostile(), None) is None


def test_await_warns_when_blocking_the_main_thread():
    """Blocking the main thread deadlocks once Qt owns it, so it must warn."""
    def fn(handler):
        handler("ok", None)

    with pytest.warns(RuntimeWarning, match="main thread"):
        _await(fn, "capture")


def test_await_is_silent_on_a_worker_thread():
    import warnings

    out = {}

    def run():
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning becomes an exception
            out["v"] = _await(lambda h: h("ok", None), "capture")

    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert out["v"] == "ok"


# ------------------------------------------------------- display picking ---
class _FakeDisplay:
    def __init__(self, did):
        self._id = did

    def displayID(self):
        return self._id


class _FakeContent:
    def __init__(self, n):
        self._d = [_FakeDisplay(i) for i in range(n)]

    def displays(self):
        return self._d


def _source(display):
    src = build_source.__wrapped__ if hasattr(build_source, "__wrapped__") else None
    from glance.capture.sck_source import ScreenCaptureKitSource

    s = ScreenCaptureKitSource.__new__(ScreenCaptureKitSource)
    s.display = display
    return s


def test_display_index_is_one_based_like_mss():
    s = _source(1)
    assert s._pick_display(_FakeContent(2)).displayID() == 0


def test_second_display_resolves():
    assert _source(2)._pick_display(_FakeContent(2)).displayID() == 1


def test_missing_display_is_a_clean_index_error():
    with pytest.raises(IndexError, match="display 9 not found; 2 connected"):
        _source(9)._pick_display(_FakeContent(2))


def test_no_displays_blames_the_permission():
    with pytest.raises(RuntimeError, match="Screen Recording"):
        _source(1)._pick_display(_FakeContent(0))


# ------------------------------------------------------------- factory ----
def test_unknown_source_names_all_three_forms():
    with pytest.raises(ValueError, match="'screen', 'sck' or 'file:<path>'"):
        build_source("nope")


def test_backing_scale_is_at_least_one():
    assert _backing_scale(1) >= 1.0
