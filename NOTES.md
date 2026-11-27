# Notes — things that cost time

Every entry here is a real failure from building glance, with the symptom that
was actually visible, what turned out to be causing it, and how it was
confirmed. Most of them look like one thing and are another; several were only
reachable once an earlier one was fixed.

If you are extending glance, skim the headings. If you are debugging something
strange, the symptom you are seeing is probably one of these.

Everything up to [Windows](#windows) was measured on **macOS 27.2 (arm64),
Python 3.13**. The Windows section is marked separately because it is written
from the documented API and has never been run — it is kept apart rather than
blended in, so nothing there reads as verified when it is not.

---

## macOS permissions

### A grant follows the *running binary*, not the app you think you launched

Screen Recording, Accessibility and Input Monitoring are attached by TCC to
the code that is actually executing. Run glance from a terminal and the grant
belongs to Terminal or VS Code. Package it and the grant belongs to the
bundle — a separate identity that inherits nothing.

Verified by asking the same interpreter from both sides:

```
inside glance.app : screen_recording = False
under VS Code     : screen_recording = True
```

### Accessibility and Input Monitoring are two different grants

With only Accessibility, the hotkey listener starts cleanly, logs that it
registered every combo, and then **never receives a keystroke**. No error, no
warning, nothing in the log. `scripts/doctor.py` checks both for this reason;
`AXIsProcessTrusted()` answers the first, `IOHIDCheckAccess()` the second
(through ctypes — pyobjc has no IOKit binding).

### Querying a permission is not the same as asking for one

`CGPreflightScreenCaptureAccess()` and `AXIsProcessTrusted()` only report.
They do not prompt, and they do not put the app in the Privacy list — so
"enable glance under Accessibility" pointed at a row that did not exist, and
with `LSUIElement` set there is not even a Dock icon to drag in. The bundled
build now calls `CGRequestScreenCaptureAccess()` and
`AXIsProcessTrustedWithOptions({prompt: True})`, which raise the real dialogs
*and* register the app. Running from a terminal it still only warns, because
that grant would attach to the terminal.

### macOS re-reads grants at launch

Granting something to a running app changes nothing until it restarts. Quit
from the menu bar and start it again.

### Ad-hoc signing ties the identity to the bundle's hash

`codesign -s -` is free and needs no Apple account, but the identity is the
cdhash. Rebuild the bundle and macOS may ask for the grants again. Editing the
*source* is fine — the bundle points at the project — so only
`scripts/build_app.py` runs disturb it.

---

## Packaging

### `exec` replaces the process image, so a wrapper script defeats the bundle

The first launcher did `exec /path/to/.venv/bin/python main.py`. That looks
reasonable and cannot work: after `exec`, the running binary was
`/Library/Frameworks/Python.framework/.../python3.13`, outside `glance.app`
entirely. Screen Recording could be granted to glance.app any number of times
and nothing changed, through both LaunchServices and launchd.

### `bin/python3.13` is itself a stub — and `sys.executable` will lie to you

Copying the interpreter into the bundle does not fix it. On a framework build
that binary immediately re-execs `Python.app/Contents/MacOS/Python`. Measured
with `proc_pidpath`, before Qt was even imported:

```
real running image : /Library/Frameworks/.../Python.app/.../Python
sys.executable     : /…/glance.app/Contents/MacOS/python     <- not the truth
```

`sys.executable` is preserved across that re-exec, so a check written against
it reports success while the process is somewhere else. The crash report
agreed: `codeSigningID = org.python.python`.

**If you need to know what is really running, use `proc_pidpath`, not
`sys.executable`.**

### Copying `Python.app`'s own binary is killed outright

Exit 137 — SIGKILL — because its signature does not survive being moved out of
the framework.

### What works: embed CPython in a small compiled launcher

`scripts/launcher.c` calls `Py_BytesMain`. Nothing re-execs, the running image
is the launcher itself, and the launcher is signed as part of the bundle. ~34
KB, and the bundle stays thin — it points at the project's `.venv` rather than
vendoring anything.

### A symlink out of the bundle invalidates the signature

Linking `Contents/lib` at the project's `.venv/lib` works at runtime and fails
`codesign --verify --deep --strict` with *"invalid destination for symbolic
link in bundle"*. An invalid signature means no stable identity, which defeats
the entire exercise. `site-packages` is supplied through `PYTHONPATH` instead.

### launchd starts agents with `/` as the working directory

So a relative `config.yaml` resolves to `/config.yaml` and the app exits with
"no config". Running from a terminal never hits this, because you happen to be
in the project directory.

### stdout is block-buffered when it is a file

The session URL never reached `~/Library/Logs/glance.log`, so a packaged user
could not open the web view at all — the URL carries the token. Invisible from
a terminal, fatal when installed. `banner()` flushes; the launcher also sets
`PYTHONUNBUFFERED`.

### launchd and LaunchServices export different things

LaunchServices sets `__CFBundleIdentifier`; launchd does not. Detecting
"am I bundled?" from that alone silently gave a bundled app the terminal's
permission instructions. The launcher exports `GLANCE_BUNDLE_ID` itself.

---

## The floating panel

### Floating over fullscreen is an *application* property, not a window level

This is the one that looks most like a window-level problem and is not. At
`NSStatusWindowLevel` (25), 101 and 1000 the panel behaved identically:
visible windowed, invisible over another app's fullscreen Space. A window's
Space membership follows the app's **activation policy**.

```
policy = regular    windowed True   fullscreen False
policy = accessory  windowed True   fullscreen True
```

`NSApplicationActivationPolicyAccessory`, set *before* the window is created.
With it, all three levels work — so the level stays at the polite 25 rather
than covering system alerts.

### `Qt::Tool` is an `NSPanel`, and an NSPanel hides when the app deactivates

Reported as "it overlays some apps until I close them and reopen". The panel
existed at layer 25, at the right position, with `onscreen=None`. Not behind
anything — gone. glance is an accessory app, so it is inactive nearly always.

`setHidesOnDeactivate_(False)` fixes it, but **Qt re-applies its own value**,
so setting it once at startup does not hold.

### Qt resets window properties it owns, so assert them on a timer

Level, collection behaviour, `hidesOnDeactivate` and `sharingType` are all
re-applied every two seconds (`hud.keep_on_top_s`). `orderFrontRegardless()`
raises the window *without activating the app*, so the panel never steals
focus from whatever is being typed into.

### Do not gate a watchdog on `isVisible()`

Qt reports a window as not visible once AppKit has hidden it — which is
exactly the condition the watchdog exists to repair. It stood down at the only
moment it mattered. Track your own intent instead.

### A frameless window can be dragged off screen and lost

Found at `y = -58`: above the menu bar, unreachable, and with `LSUIElement`
there is no Dock icon to recover from. Dragging is clamped, and there is a
"Reset position" menu item as a second way back.

### `objc.objc_object()` on a bad handle segfaults rather than raising

`winId()` is 0 before a window is realised, and Qt's `offscreen`/`minimal`
platforms have no NSWindow at all. Check `QApplication.platformName()` and the
handle first — otherwise any headless run dies instantly.

---

## Hotkeys

### pynput ignores injected events, so hotkeys cannot be auto-tested

`GlobalHotKeys._on_press(self, key, injected)` skips injected events by
design, so a `keyboard.Controller()` press can never fire a real hotkey. The
tap receives them — a plain `Listener` sees all ten events — but the hotkey
never matches. **A physical keypress is the only way to test this.**

### pynput calls Carbon Text Services on its own thread

Which is fine until an AppKit run loop exists. With the HUD up, HIToolbox
asserts those calls are main-queue only and SIGTRAPs the process:

```
dispatch_assert_queue_fail
HIToolbox  TSMGetInputSourceProperty
thread_run                       <- not the main thread
```

So glance aborted on launch whenever `--hud` and hotkeys were both on — which
is the default for the packaged app, and a combination that had never been
tested together because every HUD test passed `--no-hotkeys`.

The context is plain data — `(keyboard_type, layout_data)`, an int and a bytes
— so `hotkeys.prime_keycode_context()` builds it on the main thread and hands
the listener a cached copy. Trade-off: changing keyboard layout needs a
restart.

### pynput does not consume the keystroke

`suppress` defaults off, so the focused application also receives ⌘⇧2. A web
page watching for unusual modifier combinations can see it.

---

## Model and conversation

### History must be what was actually sent

The orchestrator recorded the user turn as `"[solve] describe the current
screen"` while the processor really sent the mode's `user_prompt`. A follow-up
then saw a transcript where the assistant answered a question nobody had
asked, and an 8B model loses the thread completely — it replied with nothing
at all. `DEFAULT_USER_PROMPT` now lives in one place.

### The user turn steers the answer more than the system prompt does

A mode told to answer still described the screen, because the user turn said
*"Here is the current screen capture."* — an invitation to describe. Modes set
their own `user_prompt` for this reason.

### Never record an empty reply

A blank panel looks like a hang, and recording the empty turn teaches the
model, through its own transcript, that saying nothing is acceptable here.

---

## Process lifecycle

### A PID alone is not identity

The pidfile outlives an unclean shutdown and PIDs are reused, so after a
reboot the recorded number often belongs to something else. Trusting it meant
glance decided it was already running and exited — nothing started at login —
and sent `SIGUSR1` to whatever held that PID, whose default disposition is to
**terminate** it. A stale pidfile could kill an unrelated process.

The executable path is now recorded beside the PID and verified:

```
INFO glance.singleton pidfile points at /bin/sleep, not glance; treating it as stale
```

### Qt's run loop does not service Python signal handlers

So `SIGTERM` was ignored — `launchctl bootout` and `pkill` did nothing, stale
instances survived holding the port, and the *next* instance then failed to
bind. One bug manufacturing another. A low-frequency `QTimer` gives the
interpreter a chance to run the handler.

### A failed server must not leave a live window behind

uvicorn could not bind, which killed the asyncio side, while Qt kept the
process alive. The panel sat there looking healthy with no server behind it,
and every WebSocket connection got a bare 403 with nothing in the log.

### Ollama ships no LaunchAgent

Its server is a child of Ollama.app, started by hand. So glance came back
after a reboot and failed every capture. `scripts/launch_agent.py --service
ollama` runs it headlessly — and **stands down if something is already
listening**, because two servers race for 11434 and the loser exits with
`address already in use`, respawning into the same failure under KeepAlive.

---

## What a page can and cannot see

`testsite/` is a detector that tries every signal a browser actually has:
focus/blur, `hasFocus`, Page Visibility, pointer leave, window and screen
geometry, `devicePixelRatio`, frame-timing jitter, long tasks,
`elementFromPoint`, keystroke observation and `getDisplayMedia`.

With the panel measurably over the page (560×298 px of overlap) and a capture
running: **0 of 10 signals fired.**

That is not stealth engineering. It is the browser sandbox — a page is told
about its own DOM and its own input, never about other windows. Raycast,
1Password and a screen reader would all score the same zero.

**Two things do see the panel**, and both are documented in the page itself:

- a screen share, if the user grants it
- the hotkeys, which pynput does not consume

And none of it applies to native software. Anything installed to inspect the
machine finds glance immediately, with no permissions at all:

```
layer=25 alpha=0.28 glance 560x298    <- CGWindowListCopyWindowInfo
/Users/…/glance.app/Contents/MacOS/glance   <- plain ps
glance 127.0.0.1:8765                 <- lsof
dist/glance.app, com.glance.app.plist <- on disk
```

`hud.hide_from_screen_recording` (`NSWindowSharingNone`, the mechanism
1Password uses) removes the window's **pixels** from other software's
captures. Measured: an ordinary capture and one that explicitly excludes the
panel differ by **0.00** over the panel's rect. It does not hide glance from
any of the four signals above. It is a privacy feature for your own
recordings, not concealment.

---

## Windows

> Everything above was measured on macOS 27.2 (arm64). This section is
> different in kind: `glance/platform_win.py` is written against the
> documented Win32 API and **has never been run on Windows**. What follows
> separates what is structurally true from what still needs checking. Do not
> read the second list as fact.

### Most of the macOS pain simply does not exist here

- **No permission system for screen capture or input.** There is no TCC
  equivalent, so there is nothing to grant, nothing to re-grant, and no
  identity for a grant to attach to. The entire "permissions" and most of the
  "packaging" sections above are macOS-only problems.
- **No framework-Python re-exec.** `python.exe` is not a stub that re-execs
  into an app bundle, so the saga that produced `scripts/launcher.c` has no
  Windows counterpart. PyInstaller is the ordinary route to an `.exe`.
- **No Spaces, so no activation-policy trap.** The thing that took longest to
  diagnose on macOS — floating over fullscreen being an *application*
  property — has no Windows equivalent.
- **`Qt::Tool` is not an `NSPanel`.** The hide-on-deactivate behaviour that
  made the panel vanish is specific to AppKit.

### The one real asymmetry: who does the excluding

|  | macOS | Windows |
|---|---|---|
| Who excludes | the **capturer**, per capture | the **window itself**, once |
| API | `SCContentFilter(display:excludingWindows:)` | `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` |
| Scope | glance's own frames only | **every** capture on the system |

This is not a setting, it is the shape of the API. On macOS the panel is
absent from glance's frames and visible to Zoom and OBS unless you opt in with
`hud.hide_from_screen_recording`. On Windows there is no per-capture
exclusion: keeping the panel out of glance's own captures necessarily keeps it
out of screen sharing and screenshots too.

`WDA_EXCLUDEFROMCAPTURE` needs **Windows 10 2004 (build 19041)** or newer.
glance refuses to fall back to `WDA_MONITOR` on older builds, because that
blacks the window out *on screen* rather than removing it from the capture —
visibly worse than doing nothing. It logs and leaves the panel visible
instead.

### Known gaps, not yet solved

- **Virtual desktops.** There is no `NSWindowCollectionBehaviorCanJoinAllSpaces`
  equivalent. A window belongs to the desktop it was created on. Pinning
  across desktops needs the undocumented `IVirtualDesktopManagerInternal` COM
  interface, whose GUIDs change between Windows builds — deliberately not
  chased.
- **Autostart** is a `shell:startup` shortcut or an
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry.
  `scripts/launch_agent.py` is macOS-only and will not help.
- **Capture backend.** `dxcam` (DXGI Desktop Duplication) is intended;
  `uv sync --extra windows`. `mss` also works and is the safer first try.
- **Code signing.** An unsigned `.exe` triggers SmartScreen. An OV certificate
  earns reputation slowly; an EV one is immediate and costs roughly $300/yr.
  As on macOS, building from source avoids the question entirely.

### What to check first, in this order

Each step depends on the one before it, which is the lesson from the macOS
side — several bugs there were only reachable once an earlier one was fixed.

1. `scripts/doctor.py` runs and the imports resolve.
2. Headless path: `--source file:assets/sample.png --processor mock`.
3. Real capture: `--source screen --processor mock`.
4. Hotkeys fire **by physical keypress** — synthetic ones cannot test this
   (see above), and the HUD-plus-hotkeys combination is exactly what aborted
   on macOS, so test them together rather than separately.
5. Panel floats and stays on top; check it survives another app launching.
6. `SetWindowDisplayAffinity` returns true **and** the panel is still visible
   on screen afterwards — verify both, since the failure mode of the older
   flag is a window that blanks itself.
7. Record the screen and confirm the panel is absent from the file.

### Smaller things that will differ

- `.venv\Scripts\python.exe`, not `.venv/bin/python`.
- Ollama on Windows runs as a background application; it has no LaunchAgent to
  install, so the `--service ollama` command does not apply.
- `127.0.0.1` binding and the token scheme are unchanged — the server is
  platform-neutral.

---

## Testing habits that paid off

- **Measure, do not eyeball.** "The panel is excluded" became *inside its rect,
  plain-vs-excluded differs by 58.8 while plain-vs-plain is 0.00; outside,
  both are 1.0.*
- **Test the combination, not just the parts.** HUD and hotkeys each worked;
  together they aborted on launch. Every HUD test had passed `--no-hotkeys`.
- **Install it.** The working-directory bug, the buffered session URL, the
  permission text and the TCC identity were all invisible from a terminal.
- **Distrust the convenient check.** `sys.executable` said the fix had worked
  while the process was running from somewhere else entirely.
