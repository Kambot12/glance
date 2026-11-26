"""OCR augment / fallback behaviour, and confidence filtering."""
import asyncio
import types

from PIL import Image

from glance.capture import build_source
from glance.config import AppConfig
from glance.models import Allow, Event, Frame
from glance.ocr import EMPTY, NullOcrEngine, OcrResult, RapidOcrEngine, build_engine
from glance.orchestrator import Orchestrator
from glance.server.app import Hub
from glance.session import SessionState
from glance.vision import build_processor


class RecordingHub(Hub):
    def __init__(self):
        super().__init__()
        self.events: list[dict] = []

    async def broadcast(self, payload):
        self.events.append(payload)


class AllowAll:
    def check(self, region=None):
        return Allow()


class FakeOcr:
    name = "fake"

    def __init__(self, text="ERROR orders connection refused", lines=1):
        self.result = OcrResult(text=text, line_count=lines, mean_confidence=0.91)
        self.calls = 0

    def extract(self, frame):
        self.calls += 1
        return self.result


class ExplodingOcr:
    name = "boom"

    def extract(self, frame):
        raise RuntimeError("onnxruntime blew up")


class ExplodingProcessor:
    name = "exploding"
    endpoint = "http://127.0.0.1:11434/v1 (unreachable)"

    async def stream(self, frame, mode, history, user_text=None):
        raise ConnectionError("endpoint refused the connection")
        yield ""  # pragma: no cover

    async def aclose(self):
        pass


def cfg_with(ocr_mode, max_chars=4000):
    return AppConfig.model_validate(
        {
            "capture": {"max_edge": 512},
            "ocr": {"mode": ocr_mode, "max_chars": max_chars},
            "modes": [
                {"name": "summarize", "hotkey": "<cmd>+<shift>+1", "system_prompt": "go"}
            ],
        }
    )


def build(tmp_path, cfg, *, ocr=None, processor=None):
    png = tmp_path / "s.png"
    Image.new("RGB", (900, 600), (30, 90, 160)).save(png)
    source = build_source(f"file:{png}", max_edge=cfg.capture.max_edge)
    hub = RecordingHub()
    orch = Orchestrator(
        cfg, source, processor or build_processor("mock", cfg.provider), AllowAll(),
        SessionState(cfg.history.max_exchanges), hub, asyncio.Queue(), ocr=ocr,
    )
    return orch, hub


def text_of(hub):
    return "".join(e["text"] for e in hub.events if e["type"] == "chunk")


# -- defaults ----------------------------------------------------------------

def test_default_engine_is_null_without_the_extra():
    assert isinstance(build_engine("off"), NullOcrEngine)
    assert build_engine("off").extract(None) is EMPTY


def test_missing_extra_degrades_instead_of_raising():
    """A missing 'ocr' extra must not crash the app."""
    engine = build_engine("fallback")
    assert engine.name in ("none", "rapidocr")


def test_empty_result_is_falsy():
    assert not EMPTY
    assert OcrResult(text="   ", line_count=0, mean_confidence=0.0).__bool__() is False
    assert OcrResult(text="hi", line_count=1, mean_confidence=0.9)


# -- augment -----------------------------------------------------------------

async def test_augment_attaches_text_to_the_frame(tmp_path):
    ocr = FakeOcr()
    orch, hub = build(tmp_path, cfg_with("augment"), ocr=ocr)
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert ocr.calls == 1
    assert "OCR: attached" in text_of(hub)
    assert orch.state.last_frame.ocr_text == "ERROR orders connection refused"


async def test_augment_truncates_to_max_chars(tmp_path):
    ocr = FakeOcr(text="x" * 9000)
    orch, _ = build(tmp_path, cfg_with("augment", max_chars=100), ocr=ocr)
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert len(orch.state.last_frame.ocr_text) == 100


async def test_off_never_calls_the_engine(tmp_path):
    ocr = FakeOcr()
    orch, hub = build(tmp_path, cfg_with("off"), ocr=ocr)
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert ocr.calls == 0
    assert "OCR: none" in text_of(hub)


async def test_a_broken_engine_does_not_break_the_capture(tmp_path):
    orch, hub = build(tmp_path, cfg_with("augment"), ocr=ExplodingOcr())
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert hub.events[-1]["type"] == "end"
    assert "OCR: none" in text_of(hub)


# -- fallback ----------------------------------------------------------------

async def test_fallback_serves_ocr_text_when_the_endpoint_dies(tmp_path):
    ocr = FakeOcr()
    orch, hub = build(
        tmp_path, cfg_with("fallback"), ocr=ocr, processor=ExplodingProcessor()
    )
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    body = text_of(hub)
    assert hub.events[-1]["type"] == "end"
    assert not any(e["type"] == "error" for e in hub.events)
    assert "ConnectionError" in body
    assert "ERROR orders connection refused" in body


async def test_fallback_is_not_used_to_augment(tmp_path):
    """mode=fallback must not run OCR on the happy path."""
    ocr = FakeOcr()
    orch, hub = build(tmp_path, cfg_with("fallback"), ocr=ocr)
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert ocr.calls == 0
    assert "OCR: none" in text_of(hub)


async def test_a_dead_endpoint_with_no_ocr_text_still_reports_the_error(tmp_path):
    orch, hub = build(
        tmp_path, cfg_with("fallback"), ocr=NullOcrEngine(),
        processor=ExplodingProcessor(),
    )
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert hub.events[-1]["type"] == "error"
    assert "ConnectionError" in hub.events[-1]["text"]


async def test_off_mode_does_not_fall_back(tmp_path):
    ocr = FakeOcr()
    orch, hub = build(
        tmp_path, cfg_with("off"), ocr=ocr, processor=ExplodingProcessor()
    )
    await orch._dispatch(Event(kind="mode", mode="summarize", manual=True))
    await orch._active
    assert ocr.calls == 0
    assert hub.events[-1]["type"] == "error"


# -- confidence filtering ----------------------------------------------------

def test_rapidocr_filters_low_confidence_lines(monkeypatch):
    engine = RapidOcrEngine(min_confidence=0.6)
    fake_out = types.SimpleNamespace(
        txts=["Requests", "1.24M", "garbage", "  "],
        scores=[0.98, 0.91, 0.20, 0.99],
    )
    monkeypatch.setattr(engine, "_load", lambda: (lambda _img: fake_out))
    frame = Frame(image=Image.new("RGB", (10, 10)))
    result = engine.extract(frame)
    assert result.text == "Requests\n1.24M"
    assert result.line_count == 2
    assert 0.9 < result.mean_confidence < 1.0


def test_rapidocr_returns_empty_when_nothing_is_read(monkeypatch):
    engine = RapidOcrEngine()
    monkeypatch.setattr(
        engine, "_load",
        lambda: (lambda _img: types.SimpleNamespace(txts=[], scores=[])),
    )
    assert engine.extract(Frame(image=Image.new("RGB", (10, 10)))) is EMPTY


def test_rapidocr_survives_missing_scores(monkeypatch):
    engine = RapidOcrEngine(min_confidence=0.5)
    monkeypatch.setattr(
        engine, "_load",
        lambda: (lambda _img: types.SimpleNamespace(txts=["hello"], scores=None)),
    )
    result = engine.extract(Frame(image=Image.new("RGB", (10, 10))))
    assert result.text == "hello"


async def test_warmup_loads_the_engine_once(tmp_path):
    ocr = FakeOcr()
    orch, _ = build(tmp_path, cfg_with("augment"), ocr=ocr)
    await orch.warm_ocr()
    assert ocr.calls == 1, "warmup should push one frame through the engine"


async def test_warmup_is_skipped_when_ocr_is_off(tmp_path):
    ocr = FakeOcr()
    orch, _ = build(tmp_path, cfg_with("off"), ocr=ocr)
    await orch.warm_ocr()
    assert ocr.calls == 0


async def test_warmup_is_skipped_for_the_null_engine(tmp_path):
    orch, _ = build(tmp_path, cfg_with("fallback"), ocr=NullOcrEngine())
    await orch.warm_ocr()  # must not raise
