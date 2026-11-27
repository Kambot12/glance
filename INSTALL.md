# Installing glance

glance reads your screen on a hotkey, sends the frame to a vision model, and
streams the answer back to a floating panel and a local web page.

It is developed and verified on **macOS 27.2 (arm64), Python 3.13**. Windows
support is written but not yet run on Windows — see [Windows](#windows).

---

## macOS

### 1. From source (recommended first run)

```bash
git clone <your-fork-url> glance && cd glance
uv sync --extra hud --extra sck     # add --extra ocr for on-image text
cp config.example.yaml config.yaml
.venv/bin/python scripts/doctor.py  # tells you exactly what is missing
```

Run the headless path first. It needs no permissions, no model and no API key,
so it isolates "is the install good?" from "are the permissions good?":

```bash
.venv/bin/python main.py --source file:assets/sample.png --processor mock --no-hotkeys
```

Then the real thing:

```bash
.venv/bin/python main.py --hud --processor openai
```

Run this way, macOS attributes Screen Recording to **your terminal**, not to
glance. That is fine for development and is why step 2 exists.

### 2. Install as an app

```bash
.venv/bin/python scripts/build_app.py
open dist/glance.app
```

This builds a real `glance.app`, so permissions belong to glance instead of to
whichever terminal happened to launch it. The bundle is thin — it points at the
project's `.venv` rather than vendoring Python, so the build is instant and you
can always see what it runs (`dist/glance.app/Contents/MacOS/glance`).

**Building locally is deliberate.** Gatekeeper quarantines files that were
*downloaded*; an app you built yourself is never quarantined, so this path
needs no Apple Developer account, no `$99/yr`, no notarization, and shows no
"damaged / unidentified developer" dialog. See
[Distributing builds](#distributing-builds) for when that changes.

### 3. Start it at login

```bash
.venv/bin/python scripts/launch_agent.py install     # --keep-alive to restart on exit
.venv/bin/python scripts/launch_agent.py status
.venv/bin/python scripts/launch_agent.py uninstall
```

Logs go to `~/Library/Logs/glance.log`.

### 4. Grant permissions

A bundle has its **own** identity. A grant you gave Terminal, iTerm or VS Code
does **not** carry over to `glance.app` — this is the single most common
reason a fresh install appears to do nothing.

| Permission | Needed for | Where |
|---|---|---|
| Screen & System Audio Recording | every screen capture | System Settings → Privacy & Security |
| Accessibility | global hotkeys | System Settings → Privacy & Security |
| Input Monitoring | global hotkeys | System Settings → Privacy & Security |

Accessibility and Input Monitoring are **separate grants**. With only
Accessibility, the hotkey listener starts cleanly and then never receives a
keystroke — `scripts/doctor.py` checks both for exactly this reason.

macOS only re-reads a grant at launch, so **quit glance from the menu bar and
start it again** after changing anything.

### Hotkeys

| Hotkey | Does |
|---|---|
| **⌘⇧1** | summarize what is on screen |
| **⌘⇧2** | **solve** — answers the question on screen, answer only |
| **⌘⇧3** | explain the code on screen |
| **⌘⇧4** | read a dashboard or chart |
| **⌘⇧0** | pause / resume |
| **⌘⇧.** | show / hide the panel |

All of them are `config.yaml`; the modes are just a name, a hotkey and a
system prompt, so adding your own is three lines.

### The panel and screen recording

By default the panel is kept out of screen recordings and screen shares
(`hud.hide_from_screen_recording`). On macOS that is `NSWindowSharingNone`,
the same mechanism 1Password uses to keep a vault out of a recording; on
Windows `SetWindowDisplayAffinity` does it and cannot do otherwise.

Be clear about what that is and is not. It removes the window's **pixels**
from captures made by other software. It does **not** hide glance from:

- the window list — `CGWindowListCopyWindowInfo` still enumerates it, with no
  permissions required
- the process list — plain `ps` shows `glance.app/Contents/MacOS/glance`
- the port it listens on — `127.0.0.1:8765`
- the files on disk — `glance.app` and its LaunchAgent plist

So it is a privacy feature for your own recordings, not concealment. Any
software installed specifically to inspect the machine finds glance
immediately, by any of the four signals above.

### The panel

Hiding the panel never cancels anything. A capture already in flight runs to
completion, and if it finishes while the panel is down the answer is kept and
the status reads **answer ready**; the next time you bring the panel up it is
shown at full opacity rather than fading straight back.

A finished answer also stays readable for `hud.hold_after_answer_s` seconds
(8 by default) before fading, so a response does not drop to 28% the instant
it lands.

It rests at **28% opacity** so it does not sit in front of what you are
reading, and goes solid when you point at it or while an answer is streaming
in. Both values are `hud.opacity` and `hud.opacity_active`.

### Quitting and the menu bar

A bundled glance runs as an agent: no Dock icon. The menu-bar item is the only
affordance — Show/Hide the panel, trigger a mode, **Reset position**, Quit.
Right-clicking the panel gives the same menu.

---

## Windows

> **Status: written, not yet run on Windows.** The code is behind platform
> guards and cannot affect macOS. Treat this section as the plan, and expect to
> fix things. Reports welcome.

Windows is in several ways the easier target:

- **No permission system for screen capture.** There is no TCC equivalent, so
  there is nothing to grant and nothing to re-grant. The entire macOS
  permission story above disappears.
- **Hotkeys need no permission** either.

The one real difference is **how the panel is kept out of captures**:

| | macOS | Windows |
|---|---|---|
| Who excludes | the **capturer** | the **window itself** |
| API | `SCContentFilter(display:excludingWindows:)` | `SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)` |
| Scope | per capture — hidden from glance only | system-wide — hidden from **everything** |

This is worth understanding before you rely on it. On macOS the panel is
removed from glance's frames and stays visible to Zoom, Teams and OBS. On
Windows there is no per-capture exclusion: excluding the panel from glance's
captures also hides it from screen sharing and from screenshots. The only
alternative is hiding the window around each capture, which flickers and races
the compositor.

`WDA_EXCLUDEFROMCAPTURE` needs **Windows 10 version 2004 (build 19041)** or
newer. On older builds glance logs a warning and leaves the panel visible,
rather than falling back to `WDA_MONITOR`, which blacks the window out on
screen instead of removing it from the capture.

Other known gaps:

- **Virtual desktops.** There is no equivalent of
  `NSWindowCollectionBehaviorCanJoinAllSpaces`. The panel belongs to the
  virtual desktop it was created on. Pinning across desktops needs the
  undocumented `IVirtualDesktopManagerInternal` COM interface, whose GUIDs
  change between Windows builds — not worth the fragility.
- **Capture backend.** `dxcam` (DXGI Desktop Duplication) is the intended
  source; install with `uv sync --extra windows`.

```powershell
git clone <your-fork-url> glance; cd glance
uv sync --extra hud --extra windows
copy config.example.yaml config.yaml
.venv\Scripts\python main.py --hud --processor openai
```

Autostart on Windows is a shortcut in
`shell:startup`, or a `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`
entry — the LaunchAgent script is macOS-only.

---

## Distributing builds

Everything above builds on the user's own machine, which is why it needs no
code-signing. Shipping a **prebuilt** binary is a different problem, because
the downloaded file is quarantined:

| Target | Unsigned result | What it takes to fix |
|---|---|---|
| macOS `.app` / `.dmg` | "damaged and can't be opened" | Apple Developer ID + notarization — $99/yr |
| Windows `.exe` / `.msi` | SmartScreen "unrecognised app" | OV cert (warning fades with reputation) or EV cert (immediate, ~$300+/yr) |

For an open-source project the honest recommendation is **build-from-source as
the primary path**, which is what this document leads with. Publish prebuilt
binaries only once there is a reason to, and budget for the certificates — an
unsigned download is a worse first run than a two-line `git clone`.

---

## Troubleshooting

Run the doctor first. It checks the Python version, every import, both macOS
permissions *and* Input Monitoring, window-title readability, config validity,
whether the model endpoint is reachable, whether the model is actually pulled,
and port availability — each failure with its exact fix.

```bash
.venv/bin/python scripts/doctor.py
```

| Symptom | Cause |
|---|---|
| Hotkeys do nothing, no error | Input Monitoring not granted (separate from Accessibility) |
| "no config at config.yaml" | `cp config.example.yaml config.yaml` |
| Panel missing after dragging | menu bar → **Reset position** |
| Panel not over a fullscreen app | should not happen — glance runs as an accessory app for this; please report |
| Captures are blank/black | Screen Recording granted to your terminal, not to `glance.app` |
| Panel appears in its own captures | ScreenCaptureKit missing: `uv sync --extra sck` |
