"""The asyncio core: events in, streamed text out.

Concurrency rules:
  * One capture at a time. A manual hotkey press CANCELS an in-flight stream
    (the spec says a manual press always runs), while a watch tick defers.
  * Capture runs in a single-worker executor because mss handles are
    per-thread and not thread-safe; one worker also keeps frames ordered.
  * Hotkey callbacks arrive on the pynput thread and reach this loop through
    loop.call_soon_threadsafe, never by touching asyncio objects directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from glance import imaging
from glance.capture.base import CaptureSource
from glance.config import AppConfig
from glance.vision.base import DEFAULT_USER_PROMPT
from glance.models import Block, Event, Frame
from glance.ocr import EMPTY, NullOcrEngine, OcrEngine, OcrResult
from glance.privacy import PrivacyGuard
from glance.server.app import Hub
from glance.session import SessionState
from glance.vision.base import VisionProcessor

log = logging.getLogger("glance.core")


class Orchestrator:
    def __init__(
        self,
        cfg: AppConfig,
        source: CaptureSource,
        processor: VisionProcessor,
        guard: PrivacyGuard,
        state: SessionState,
        hub: Hub,
        inbox: "asyncio.Queue[Event]",
        ocr: OcrEngine | None = None,
        debug_save: Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.source = source
        self.processor = processor
        self.guard = guard
        self.state = state
        self.hub = hub
        self.inbox = inbox
        self.ocr = ocr or NullOcrEngine()
        self.debug_save = debug_save
        self._capture_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="glance-capture"
        )
        # onnxruntime is CPU-heavy; its own worker keeps it off the capture path.
        self._ocr_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="glance-ocr"
        )
        self._active: asyncio.Task | None = None
        self._last_watch_hash = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- thread bridge -----------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def submit_from_thread(self, event: Event) -> None:
        """Safe to call from the pynput listener thread."""
        if self._loop is None:
            log.warning("event dropped: loop not bound yet")
            return
        self._loop.call_soon_threadsafe(self.inbox.put_nowait, event)

    # -- main loop ---------------------------------------------------------

    async def warm_ocr(self) -> None:
        """Load the OCR model up front.

        Measured here: the first rapidocr call costs ~18s (model load) against
        ~0.9s warm, so without this the first OCR-augmented capture stalls.
        """
        if self.cfg.ocr.mode == "off" or isinstance(self.ocr, NullOcrEngine):
            return
        from PIL import Image  # noqa: PLC0415

        log.info("warming %s in the background (first load takes ~20s)", self.ocr.name)
        t0 = time.perf_counter()
        await self._ocr(Frame(image=Image.new("RGB", (64, 64), (255, 255, 255))))
        log.info("ocr ready in %.1fs", time.perf_counter() - t0)

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        watch_task = None
        if self.cfg.watch.enabled:
            watch_task = asyncio.create_task(self._watch_loop(), name="watch")
        warm_task = asyncio.create_task(self.warm_ocr(), name="ocr-warmup")
        try:
            while True:
                event = await self.inbox.get()
                await self._dispatch(event)
        except asyncio.CancelledError:
            raise
        finally:
            for t in (watch_task, warm_task):
                if t:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
            await self._cancel_active()
            self._capture_pool.shutdown(wait=False)
            self._ocr_pool.shutdown(wait=False)

    async def _dispatch(self, event: Event) -> None:
        if event.kind == "pause":
            self.state.paused = not self.state.paused
            text = "paused" if self.state.paused else "resumed"
            log.info("capture %s", text)
            await self.hub.broadcast({"type": "status", "text": text, "state": "open"})
            return

        if event.kind == "panel":
            # Checked before the pause guard: showing or hiding the panel is a
            # view control, not an action, and refusing it while paused would
            # strand the user with no way to see the panel again.
            log.info("panel toggle (%s)", event.origin)
            await self.hub.broadcast({"type": "panel", "action": "toggle"})
            return

        if self.state.paused and event.kind != "followup":
            log.info("ignoring %s while paused", event.kind)
            await self.hub.broadcast(
                {"type": "status", "text": "paused", "state": "open"}
            )
            return

        if event.kind == "watch":
            if self._active and not self._active.done():
                log.debug("watch tick skipped: a response is already streaming")
                return
            await self._start(self._run_capture(event.mode, watch=True))
            return

        if event.kind == "mode":
            mode = event.mode or self.cfg.modes[0].name
            await self._cancel_active()  # a manual press always runs
            await self._start(
                self._run_capture(mode, watch=False, origin=event.origin)
            )
            return

        if event.kind == "followup":
            await self._cancel_active()
            await self._start(self._run_followup(event.text or ""))

    async def _start(self, coro) -> None:
        self._active = asyncio.create_task(coro)

    async def _cancel_active(self) -> None:
        task = self._active
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._active = None

    # -- work --------------------------------------------------------------

    async def _watch_loop(self) -> None:
        mode = self.cfg.modes[0].name
        while True:
            await asyncio.sleep(self.cfg.watch.interval_s)
            await self.inbox.put(Event(kind="watch", mode=mode))

    async def _capture(self) -> Frame:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._capture_pool, self.source.grab, self.cfg.capture.region
        )

    async def _ocr(self, frame: Frame) -> OcrResult:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(self._ocr_pool, self.ocr.extract, frame)
        except Exception as e:
            log.error("ocr failed: %s: %s", type(e).__name__, e)
            return EMPTY
        if result:
            log.info(
                "ocr extracted %d lines (mean confidence %.2f) via %s",
                result.line_count, result.mean_confidence, self.ocr.name,
            )
        return result

    async def _run_capture(
        self, mode_name: str, *, watch: bool, origin: str = "hotkey"
    ) -> None:
        try:
            mode = self.cfg.mode(mode_name)
        except KeyError:
            await self.hub.broadcast(
                {"type": "error", "text": f"unknown mode {mode_name!r}"}
            )
            return

        # The denylist guards screen content. A file source reads a PNG from
        # disk, so there is no window on screen to check against.
        if getattr(self.source, "captures_screen", True):
            verdict = self.guard.check(self.cfg.capture.region)
            if isinstance(verdict, Block):
                log.info("capture skipped: %s", verdict.reason)
                await self.hub.broadcast(
                    {"type": "blocked", "text": f"Capture skipped - {verdict.reason}"}
                )
                return

        t0 = time.perf_counter()
        try:
            frame = await self._capture()
        except Exception as e:
            log.error("capture failed: %s: %s", type(e).__name__, e)
            await self.hub.broadcast(
                {"type": "error", "text": f"capture failed: {e}"}
            )
            return

        if watch and imaging.is_duplicate(
            frame.phash, self._last_watch_hash, self.cfg.watch.phash_distance
        ):
            log.debug("watch tick deduped (phash within %d)", self.cfg.watch.phash_distance)
            return
        if watch:
            self._last_watch_hash = frame.phash

        if self.cfg.ocr.mode == "augment":
            result = await self._ocr(frame)
            if result:
                frame = dataclasses.replace(
                    frame, ocr_text=result.text[: self.cfg.ocr.max_chars]
                )

        self.state.last_frame = frame
        self.state.last_mode = mode.name
        self._maybe_save(frame)

        log.info(
            "%s %s -> %s (capture %.0f ms)",
            "watch" if watch else origin,
            frame,
            self.processor.endpoint,
            (time.perf_counter() - t0) * 1000,
        )
        # Record the turn that was actually sent, not a description of it.
        # These had drifted: history said the user asked to "describe the
        # current screen" while the model was really asked to answer the
        # question on it, so a follow-up saw an assistant answering something
        # nobody had asked - and small models lose the thread completely.
        asked = (mode.user_prompt or "").strip() or DEFAULT_USER_PROMPT
        await self._stream(
            frame, mode.name, label=f"{mode.name}{' · watch' if watch else ''}",
            question=asked,
            # A mode may state its own ask. Without one the processor falls
            # back to a neutral "here is the screen", which nudges the model
            # towards describing rather than answering.
            user_text=mode.user_prompt,
        )

    async def _run_followup(self, text: str) -> None:
        frame = self.state.last_frame
        if frame is None:
            await self.hub.broadcast(
                {"type": "error", "text": "no capture yet - press a mode hotkey first"}
            )
            return
        mode_name = self.state.last_mode or self.cfg.modes[0].name
        log.info("follow-up -> %s", self.processor.endpoint)
        await self._stream(
            frame, mode_name, label=f"follow-up · {mode_name}",
            question=text, user_text=text,
        )

    async def _stream(
        self,
        frame: Frame,
        mode_name: str,
        *,
        label: str,
        question: str,
        user_text: str | None = None,
    ) -> None:
        mode = self.cfg.mode(mode_name)
        history = self.state.history(mode_name)
        parts: list[str] = []
        await self.hub.broadcast({"type": "start", "label": label})
        try:
            async for chunk in self.processor.stream(frame, mode, history, user_text):
                parts.append(chunk)
                await self.hub.broadcast({"type": "chunk", "text": chunk})
        except asyncio.CancelledError:
            await self.hub.broadcast({"type": "end"})
            raise
        except Exception as e:
            log.error("model stream failed: %s: %s", type(e).__name__, e)
            if self.cfg.ocr.mode == "fallback":
                result = await self._ocr(frame)
                if result:
                    note = (
                        f"The model endpoint failed ({type(e).__name__}). "
                        f"Falling back to OCR text from the capture:\n\n"
                    )
                    await self.hub.broadcast({"type": "chunk", "text": note})
                    text = result.text[: self.cfg.ocr.max_chars]
                    await self.hub.broadcast({"type": "chunk", "text": text})
                    log.info("served OCR fallback (%d lines)", result.line_count)
                    await self.hub.broadcast({"type": "end"})
                    return
            await self.hub.broadcast({"type": "error", "text": f"{type(e).__name__}: {e}"})
            return
        answer = "".join(parts)
        if not answer.strip():
            # A blank panel with no explanation looks like a hang. Recording
            # the empty turn would also be actively harmful: it teaches the
            # model, through its own history, that saying nothing is an
            # acceptable reply in this conversation.
            log.warning("model returned an empty response; not recorded")
            await self.hub.broadcast(
                {
                    "type": "error",
                    "text": "the model returned an empty response - try again, "
                            "or try a different mode",
                }
            )
            await self.hub.broadcast({"type": "end"})
            return
        self.state.record(mode_name, question, answer)
        # Count only: the response body never reaches a log line by default.
        log.info("stream complete (%d chunks, %d chars)", len(parts), len(answer))
        await self.hub.broadcast({"type": "end"})

    def _maybe_save(self, frame: Frame) -> None:
        if self.debug_save is None:
            return
        self.debug_save.mkdir(parents=True, exist_ok=True)
        path = self.debug_save / f"frame-{int(frame.ts * 1000)}.jpg"
        path.write_bytes(imaging.to_jpeg(frame.image, self.cfg.capture.jpeg_quality))
        log.warning("--debug-save wrote %s", path)
