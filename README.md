# glance

Hotkey → screen capture → vision model → streaming local web view, with an
always-on-top panel that stays out of its own captures.
Built and verified on **macOS 27.2 (arm64), Python 3.13**.

**Installing?** See [INSTALL.md](INSTALL.md) — app bundle, login agents,
permissions, and the Windows plan.

**Extending it, or debugging something strange?** See [NOTES.md](NOTES.md) —
every failure that cost real time, with the symptom that was visible, what was
actually causing it, and how it was confirmed. Most of them looked like one
thing and were another.

## What it needs

glance is 60 MB resident; the model is the requirement. Measured on an M1 Pro
with `qwen3-vl:8b`: **6.2 GB on disk, 6.1 GB peak RAM, ~60 s** per capture.
8 GB machines want a 3B model instead. It must be a **vision** model —
`llama3.1` and `deepseek-r1` cannot see images. Full table, including the
cloud alternative, in [INSTALL.md](INSTALL.md#what-it-needs).

## Quick start

```bash
uv sync                                  # creates .venv, installs pinned deps
uv sync --extra ocr                      # optional: OCR
uv sync --extra sck                      # optional: ScreenCaptureKit (macOS 14+)
cp config.example.yaml config.yaml
.venv/bin/python scripts/doctor.py       # what is ready, what is missing, how to fix

# headless path: no permissions, no model, no API key
.venv/bin/python main.py --source file:assets/sample.png --processor mock --no-hotkeys

# real path (needs Screen Recording + Accessibility, and a vision model)
.venv/bin/python main.py --source screen --processor openai   # mss backend
.venv/bin/python main.py --source sck    --processor openai   # ScreenCaptureKit
```

Open the URL the console prints. It carries the session token in the URL
**fragment**, so the token is never sent to the server and never lands in a log.

## Thread / event-loop layout

```
main thread ──────── asyncio.run()  (Qt QApplication.exec() once the HUD lands;
                     Qt must own the main thread on macOS, so asyncio moves to a
                     worker thread at that point)
                       ├─ uvicorn Server.serve()
                       ├─ Orchestrator.run()  — event queue consumer
                       └─ watch-mode timer task

pynput thread ─────── keyboard.GlobalHotKeys listener, its own CFRunLoop.
                     Callbacks never touch asyncio objects; they go through
                     Orchestrator.submit_from_thread → loop.call_soon_threadsafe.

capture executor ─── ThreadPoolExecutor(max_workers=1).
                     mss handles are per-thread and not thread-safe, so the
                     handle is thread-local; one worker also keeps frames ordered.

ocr executor ─────── (step 3) a second single-worker pool, so onnxruntime never
                     blocks capture or the WebSocket.
```

`qasync` was considered and rejected: running asyncio *on* the Qt loop reads
nicer but couples every path to Qt, and PyQt must stay an optional extra.

Concurrency rules: one capture at a time; a **manual hotkey press cancels** an
in-flight stream (a manual press always runs), a **watch tick defers**.

## Security posture

| Requirement | Where |
|---|---|
| Secrets only from env/.env | `config.py` — `api_key_env` holds a NAME, validated to reject a pasted key |
| Binds 127.0.0.1 by default | `main.py`; `--lan` opts in and prints a QR |
| Random per-session token | `secrets.token_urlsafe(32)`, required on every WS connection |
| Origin checked, fails closed | `server/app.py` — a *missing* Origin is rejected too |
| No raw innerHTML | `static/app.js` — `createElement`/`textContent` only, plus a CSP |
| Frames in memory only | `Frame` holds a PIL image; disk writes only under `--debug-save` |
| Logs free of bytes/responses | `Frame.__repr__` prints dimensions; streams log counts, not text |
| Hotkeys only, never keylogging | `GlobalHotKeys` fires on registered combos alone |
| Denylisted windows skipped | `privacy.py` |
| Endpoint shown per frame | `orchestrator.py` logs `frame -> endpoint` |
| Pinned dependencies | `uv.lock`, plus `requirements.txt` — 47 deps pinned with 922 sha256 hashes |

The denylist only runs for sources that actually read the screen
(`CaptureSource.captures_screen`). A `FileCaptureSource` reads a PNG from disk,
so there are no windows to check — without this, fail-closed title checking
blocked the entire headless path.

### Two deliberate deviations from the original spec

1. **Token in the URL fragment, not the query string.** Query strings reach
   access logs, `Referer` headers and browser history. The page reads `#t=…`,
   strips it from the address bar, and presents it as a WebSocket subprotocol.
2. **The denylist matches on process name first, and treats an unreadable
   window title as *unknown*.** On macOS `kCGWindowName` requires Screen
   Recording permission while `kCGWindowOwnerName` does not — measured here, 1
   of 4 on-screen windows exposed a title without it. A title-based denylist
   alone would silently read `None` and allow everything. It also checks every
   window overlapping the capture region, not just the frontmost one: a
   full-display capture picks up a password manager parked in a corner.

## Platform notes

The short version is below; [NOTES.md](NOTES.md) has the full account,
including the ones that are only reachable once an earlier bug is fixed.

- **macOS** — hotkeys need Accessibility, checked with `AXIsProcessTrusted()`;
  a missing grant only warns, since `--no-hotkeys` exists.
  Screen Recording is preflighted with `CGPreflightScreenCaptureAccess()`,
  which queries without showing a dialog. The grant belongs to the app that
  *launches* python (Terminal/VS Code), not to python. Missing permission exits
  with instructions, never a crash or a black frame.
- **Linux** — a Wayland session is detected at startup and exits with a clear
  message (mss and pynput need X11).
- **Windows** — `dxcam` and `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)`
  are stubs behind a platform guard; untested here by design.
- **Floating over fullscreen is an *application* property, not a window one.**
  The panel was invisible over another app's fullscreen Space, which looked
  like a window-level problem and was not: at `NSStatusWindowLevel` (25), 101
  and 1000 it behaved identically — visible windowed, invisible fullscreen. A
  window's Space membership follows the app's activation policy, so the answer
  is `NSApplicationActivationPolicyAccessory`, set before the window is
  created. With it, all three levels work, so the level stays at the polite 25.
- **HUD exclusion** — hiding the HUD during capture is the fallback. The real
  macOS answer is `SCContentFilter(display:excludingWindows:)` from
  ScreenCaptureKit, which excludes the window without hiding it and avoids a
  compositor-flush race. That is why the HUD and the ScreenCaptureKit backend
  land together in step 4. (mss's macOS path uses `CGDisplayCreateImage`,
  deprecated since macOS 14, so it stays the portable/dev backend.)
- **ScreenCaptureKit callbacks are process-fatal if mishandled.** Its APIs are
  async and call back inside an ObjC block. A Python exception escaping that
  block — or a handler that returns anything other than `None` — becomes an
  uncaught `NSException` and aborts the *whole process*, not just the capture.
  Hit for real with a handler written as `lambda c, e: (box.set(c), ev.set())`,
  whose tuple return killed the interpreter. `_await` in `sck_source.py` is
  written defensively because of this, and two tests pin the behaviour.

## Build status

- [x] **Step 1** — config, `FileCaptureSource`, `MockVisionProcessor`, FastAPI +
      token-gated WS, web view, console, tests. Verified end to end.
- [x] **Step 2a** — `MSSSource`, `OpenAICompatProcessor`, hotkeys, `PrivacyGuard`
- [x] **Step 2b** — real model verified: `qwen3-vl:8b` via Ollama `/v1` streamed
      214 chunks and read the sample dashboard's values correctly
- [x] **Step 2c** — real *screen* capture verified end to end with both grants
      in place: a live 1600x1039 frame through `qwen3-vl:8b` streamed 116 chunks
      and described the actual foreground window, and a follow-up answered
      against that same frame. The hotkey half is verified as far as it can be
      automated — the listener registers all four combos and stays alive, and
      its callbacks submit the right `Event` across the thread boundary — but
      **the keypress itself needs a human**: pynput 1.8.2 passes an `injected`
      flag to `GlobalHotKeys._on_press` and ignores synthesized events by
      design, so a `keyboard.Controller` press can never fire a real hotkey.
- [x] **Step 3** — OCR, `rapidocr` + `onnxruntime` behind the `ocr` extra,
      in `augment` or `fallback` mode
- [x] **Step 4** — `ScreenCaptureKitSource` + Qt HUD, both verified.
      Capture: one-shot `SCScreenshotManager`, Retina-native then downscaled,
      ~68 ms, and excluding a window blanks exactly its rectangle
      (`--source sck`). HUD: `--hud` floats a panel above everything, on every
      Space and over fullscreen apps, and publishes its CGWindowID so it is
      excluded from glance's own frames. Measured with the panel on screen —
      inside its rect, plain-vs-excluded differs by 58.8 while plain-vs-plain
      is 0.0; outside, both are 1.0. The panel is gone and nothing else moves.
- [x] **Step 4b** — packaging: `scripts/build_app.py` builds `glance.app`
      (own TCC identity, ad-hoc signed, no Developer account needed because a
      locally built app is never quarantined), `scripts/launch_agent.py`
      installs a login agent, and the permission text adapts to how the
      process was launched. See [INSTALL.md](INSTALL.md).
- [~] **Step 5** — Windows: `platform_win.py` written against the documented
      Win32 API but **not yet run on Windows**. `--lan` hardening and the QR
      on a phone still to do.

### Two bugs the end-to-end runs caught

Both were invisible to the unit tests, which is the argument for
`scripts/verify_pipeline.py` existing at all.

1. **The denylist ran against the file source.** With Screen Recording denied,
   window titles are unreadable, so the guard failed closed — as designed — and
   blocked a capture that never touched the screen. `CaptureSource` now declares
   `captures_screen`.
2. **Follow-up questions never reached the model.** The text was recorded into
   history but `VisionProcessor.stream` had no parameter for it, so the user
   turn was always the generic "Here is the current screen capture." Every
   follow-up got the same unprompted answer. Only visible with a real model: the
   mock ignores its input. `stream()` now takes `user_text`.

## Tests

```bash
.venv/bin/python -m pytest -q                      # 136 unit/integration tests
.venv/bin/python scripts/verify_pipeline.py        # real server, real WebSocket
```

`pytest` covers region cropping, compression, perceptual dedup, config
validation (including the no-secrets rule), WebSocket token/Origin rejection,
the privacy guard, hotkey-listener startup reporting, and the full headless
pipeline.

`scripts/doctor.py` reports on Python version, every import, both macOS
permissions, whether window titles are readable, config validity, endpoint
reachability, whether the configured model is actually pulled, and port
availability — each failure with its exact fix. Run it before blaming the code.

`verify_pipeline.py` launches `main.py` for real, parses the session URL from
its console output, connects the way a browser does, and asserts that the
response arrives as a stream of chunks, that a follow-up carries prior context,
and that the live server refuses both a wrong token and a foreign Origin.

### Verifying the model without screen permission

The two halves of the real path can be tested separately, which is useful
because Screen Recording needs a manual grant and a relaunch:

```bash
# model half: real VLM, no screen capture, no permissions needed
.venv/bin/python main.py --source file:assets/sample.png --processor openai --no-hotkeys

# capture half: real screen, no model
.venv/bin/python main.py --source screen --processor mock --no-hotkeys
```
