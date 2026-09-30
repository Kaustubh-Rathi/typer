# typer — StealthDesk auto-typing engine (Python port)

A standalone Python extraction of the **typer** subsystem from
`D:\StealthDesk` (the C++ project). Nothing in the original repo was modified.

Only the typer is included — the browser, overlay, OCR, hotkeys, settings and
exam-proctoring code stay in the C++ repo.

## Layout

```
D:\typer
├── main.py                       CLI entry point
├── live_test.py                  end-to-end: open Notepad, click it, type
├── answer.txt                    template for --file (edit this)
├── sample.py.txt                 sample code to type (indentation demo)
├── sample-prose.txt              sample prose to type (no indentation)
├── stealth_typer\
│   ├── core\                     pure policy, no I/O
│   │   ├── typing_policy.py      speed clamp, interval, AutoIndentMode
│   │   ├── utf8_utils.py         \r\n -> \n, UTF-8 <-> codepoints
│   │   ├── typing_session.py     session state machine (position in codepoints)
│   │   └── timing_model.py       fixed + human-like (log-normal) timing
│   ├── domain\typer_state.py     Idle/Armed/ActivationSettling/Typing/Paused
│   ├── ports\                    outbound interfaces
│   ├── adapters\                 Win32 impls + disabled/dry-run stand-ins
│   │   ├── hotkey_listener.py     global pause/resume hotkey
│   │   └── simulated_target_detector.py   offline target for --simulate
│   └── application\
│       ├── i_typer_service.py
│       └── typer_service.py      the state machine that drives it all
└── tests\test_typer.py           tests (116, all passing)
```

## Requirements

Python 3.9+ on Windows. **No third-party packages** — `ctypes` covers Win32.

## Files to type

Three ready-made files ship in this folder, so every command below works
exactly as written:

| File | What it is | Try it with |
|---|---|---|
| `answer.txt` | **Template — edit this one.** A few lines of Python plus a comment explaining what to do. | `--file .\answer.txt` |
| `sample.py.txt` | Sample code with real indentation | `--file .\sample.py.txt` |
| `sample-prose.txt` | Ordinary prose, no indentation | `--file .\sample-prose.txt` |

`answer.txt` is the one to use for your own text: open it, replace the
contents with what you want typed, save, then run the command. Any path works,
so you can equally keep your files in `C:\Users\You\Documents\` and pass the
full path.

If you point `--file` at something that does not exist, the run stops
immediately with `Could not read <path>` and exit code 2 — it will not type
anything.

## Commands

Run from `D:\typer`:

```powershell
cd D:\typer
```

The full flag list:

```
usage: main.py [--text TEXT | --file PATH | --text-from-clipboard]
               [--speed SPEED] [--humanization {high,low,medium,off}]
               [--seed SEED] [--auto-indent {raw,smart,auto}]
               [--encoding ENCODING] [--settle-ms SETTLE_MS]
               [--key SPEC] [--simulate] [--self-test]
               [--dry-run] [--no-hotkey] [--no-input] [--timing-table]
```

### 1. Safe first run — simulation, nothing is typed

```powershell
python main.py --simulate --text "Hello, world!"
```

Prints every codepoint it *would* send, plus the assembled text. Needs no
window and no keyboard.

### 2. Inspect the timing model

```powershell
python main.py --timing-table --speed 80 --humanization medium
```

### 3. Type a literal string into the focused editor

```powershell
python main.py --text "Hello, world!" --speed 60
```

### 4. Type the clipboard

```powershell
python main.py --text-from-clipboard --speed 60
```

### 5. Humanized timing (irregular, human-like cadence)

```powershell
python main.py --text-from-clipboard --speed 70 --humanization high
```

### 6. Raw indentation (Notepad mode — keeps source spaces exactly)

```powershell
python main.py --text-from-clipboard --speed 70 --auto-indent raw
```

### 7. Type a text file (the main use case)

```powershell
python main.py --file .\sample.py.txt --speed 80
```

Any text file works — `.txt`, `.py`, `.java`, `.cpp`. Put your answer in a
file and point `--file` at it; no copying required.

```powershell
python main.py --file "C:\Users\You\Documents\answer.txt" --speed 70
```

Encoding is detected automatically (UTF-8, UTF-8 with BOM, UTF-16, cp1252,
latin-1). Override it if a file is unusual:

```powershell
python main.py --file .\answer.txt --encoding utf-16
```

### 8. Type a file, letting the tool pick the indentation mode

```powershell
python main.py --file .\sample.py.txt --auto-indent auto
```

See "Indentation modes" below.

### 9. See the refusal path (no input available)

```powershell
python main.py --text "test" --no-input
```

### 10. Run the tests

```powershell
python -m unittest discover -s tests -v
```

### 11. All options

```powershell
python main.py --help
```

## How a run goes

1. Text is loaded into the session (codepoint count printed).
2. `arm()` reads the clipboard (with a 3 x 60 ms retry, because locked-down
   browsers empty it quickly) and goes **Armed**.
3. A 100 ms monitor looks for an editable foreground window — not the desktop,
   not a button, not a shell window. When it finds one: **ActivationSettling**.
4. It waits `--settle-ms` (default 250 ms) so the focus change fully settles.
5. **Typing** — one codepoint per timer tick, at the speed/humanization rate.
6. If the target window closes, changes, or stops being editable, typing
   **pauses** rather than continuing into whatever is now in front.
7. At the end it returns to **Idle**.

## How to test it

Work up through these five steps. Steps 1–3 cannot type anything into a real
window; step 4 types into Notepad; step 5 is the real thing.

### Step 1 — check the whole install (nothing typed)

```powershell
cd D:\typer
python main.py --self-test
```

Runs eight checks on your machine and prints a pass/fail table:

```
======================================================================
            SELF TEST - nothing was typed into any window
======================================================================
[PASS] speed / interval maths     speed 1 -> 500 ms, speed 100 -> 5 ms
[PASS] humanized timing model     speed 60: 142..304 ms, 68 distinct
[PASS] file encoding + newlines   cp1252 + CRLF -> normalized correctly
[PASS] smart vs raw auto-indent   smart -> 'a\nb', raw -> 'a\n  b'
[PASS] auto-indent detection      auto-indenting editor -> smart, plain -> raw
[PASS] pause / resume             paused at 4 chars, held, then resumed
[PASS] Win32 window detection     foreground hwnd 853538 detected and valid
[PASS] console excluded as target foreground is our own console - correctly refused
[PASS] pause hotkey               alt+c (global) / ctrl+alt+k (global)
======================================================================
9/9 passed. Next: --simulate, then try a real editor.
```

If anything fails, it names the check and the reason. **Do not go on to step 4
until all nine pass** — a failure means your Windows or Python setup differs
from what the code expects. The hotkey line shows the combinations that were
actually free on your machine.

### Step 2 — run the automated tests

```powershell
python -m unittest discover -s tests -v
```

116 tests, covering the ported C++ test suites plus the file, auto-indent, pause,
console-exclusion, focus, hotkey-availability and arm-grace additions.

### `--diagnose-input` — can keystrokes reach the window at all?

```powershell
python main.py --diagnose-input
```

Click into your editor first, then run it. It reports the focused window,
compares **integrity levels** (the UIPI rule that makes Windows silently
discard `SendInput` from a normal process into an elevated one), then injects
a single character:

```
focused window    : 22350576 'Notepad'
editable          : True
has keyboard focus: True

THIS process integrity   : Medium
TARGET process integrity : Medium (pid 10252)

adapter returned   : True
adapter last error : 0

Did a capital H appear in the focused window just now?
```

- **If `H` appears** — injection works, so any problem is in the typer logic.
- **If `H` does not appear** — the keystrokes are being dropped before they
  arrive. Check the integrity levels: if the target is `High` and you are
  `Medium`, start PowerShell as Administrator.
- **If `H` appears but a full run still types nothing**, the focused window
  ignores `KEYEVENTF_UNICODE` — try a native editor rather than a browser or a
  web-based exam app.

```powershell
python live_test.py
```

Opens Notepad, takes focus the way Windows requires for a background process
(`AttachThreadInput` to the foreground thread + `SetForegroundWindow`), runs
the real typer against it, and reads the text back out of Notepad's RichEdit
control.

Expected result:

```
Opening Notepad...
notepad hwnd: 31460144
foreground: 31460144 == notepad: True
before: 'Untitled - Notepad'
--- running the REAL typer ---
[typer] Armed - click target editor
[typer] Target selected - settling focus (0ms)...
[typer] Activation settled - typing started from char #1
[typer] Typing complete
============================================================
Notepad content : '*HelloLive123 - Notepad'
expected        : 'HelloLive123'
MATCH           : True
============================================================
```

The `*` is Notepad marking the document unsaved, not stray input.

Note it takes focus for itself, so Notepad will come to the front while it
runs.

### Step 3 — rehearse a full run (nothing typed, no window needed)

```powershell
python main.py --simulate --file .\sample.py.txt --speed 100
```

Pretends an editor exists, runs the whole arm → settle → type → complete
cycle, and prints what *would* have been typed. No keyboard, no clipboard, no
target window involved.

Compare the two indentation modes on the same file:

```powershell
python main.py --simulate --file .\sample.py.txt --auto-indent smart
python main.py --simulate --file .\sample.py.txt --auto-indent raw
```

`smart` leaves the leading spaces out; `raw` includes them. That difference is
the main thing worth eyeballing before going live.

### Step 4 — type into Notepad (safe, real keystrokes)

1. Open **Notepad** and leave it empty.
2. Run this in PowerShell:
   ```powershell
   python main.py --text "Hello from the typer." --speed 60
   ```
3. Click inside Notepad's text area, then press the arm key it printed.
4. Watch the text appear in Notepad.

Try the pause key mid-run: typing should stop, the console should say
`Paused at N/M`, and pressing it again should carry on from there.

Notepad is a good first real target because it does not auto-indent, so use
`--auto-indent raw` there.

### Step 5 — the real thing

Type into the actual editor, starting slow:

```powershell
python main.py --file .\answer.txt --speed 40 --auto-indent auto
```

Raise the speed once you trust it. If the target is elevated (VS Code running
as Administrator), run PowerShell as Administrator too, or Windows will block
the input and the console will say `UIPI / Elevated target`.

### What each flag is for when testing

| Flag | Types for real? | Use when |
|---|---|---|
| `--self-test` | No | First check; verifies the install |
| `--simulate` | No | Rehearsing the flow and checking indentation |
| `--dry-run` | No | Previewing real text against a real window |
| `--no-input` | No | Seeing the "driver unavailable" refusal |
| *(none)* | **Yes** | The actual thing |

`--dry-run` still needs a real editable window focused (it uses the real
detector); `--simulate` does not. Reach for `--simulate` first.


## How a run works

Three steps, and nothing types until you say so:

```
[typer] Speed: 70%
Loaded 1335 codepoints.
[typer] Armed - click target editor

  1. Click into the editor you want to type into
  2. Press alt+c to start typing
  3. While typing: ctrl+alt+k pauses/resumes
  Ctrl+C in this window aborts.
```

1. **Arm** — the text is loaded and the script waits. It deliberately does
   *not* look at what is focused yet.
2. **You click your editor**, then press the arm key.
3. **Typing starts**, latching onto whichever editable window you clicked.

You do not have to get the click and the keypress perfectly timed. If the key
is pressed a moment early — before the click has landed — the script waits up
to six seconds for a usable window and starts by itself the moment you click
into your editor:

```
[typer] Heard the arm key, but this console. Click your editor now - starting automatically.
[typer] Got it - starting.
```

So: click first, press the key whenever — it will not be lost.

### The keys are chosen for you

The default is `auto`: the script probes a list of combinations and picks the
first one **not already owned by another application**, then prints which it
chose. This matters because `Alt+X` and `Ctrl+Alt+P` are registered by other
programs on many machines (Alt+X in particular), and a fixed default would
simply be dead there.

It prints the chosen key, so there is never any doubt which to press:

```
  ONE KEY: alt+c
```

To pin it yourself:

```powershell
python main.py --file .\answer.txt --key alt+c
```

Accepted: `f1`–`f12`, `a`–`z`, `0`–`9`, `space`, `tab`, `enter`, `pause`,
`scrolllock`, joined with `+` from `ctrl`, `alt`, `shift`, `win`.

Avoid combinations you type in an editor — Windows swallows them while the
script runs. Avoid bare **Alt** on its own; some apps grab it.

### If a combination is unavailable anyway

If every candidate is taken, the script falls back to polling
`GetAsyncKeyState`, which reads physical key state regardless of which window
has focus — so the key still works while your editor is focused. It says so:

```
alt+c (polled - combo already taken by another app)
```

To disable hotkeys entirely (Ctrl+C still aborts):

```powershell
python main.py --file .\answer.txt --no-hotkey
```

With `--no-hotkey` the script falls back to the older flow: click your editor,
then press Enter in the console.

## Pausing and resuming mid-run

**There is one key. It starts, pauses and resumes.** It is a *global* hotkey, so
it works while your editor has keyboard focus — no need to click back to the
console. The script prints which combination it chose.

```
  ONE KEY: alt+x
    click into your editor, press it to START
    press it again to PAUSE, again to RESUME
```

```
[typer] Typing (state=Typing). Ctrl+C to stop.
...
[typer] Paused at 128/1335 - press alt+x again to resume
[typer] Resumed at 128/1335
```

While paused nothing is typed and the position is held exactly; resuming
continues from the same character rather than restarting or skipping ahead.

The pause key only acts while the run is actually typing. Pressed earlier, it
is reported and ignored:

```
[typer] Pause key ignored - not typing yet (state=Armed)
```

If the pause combination is already taken by another program and the polled
fallback is used, the keys must be **held** for a moment (0.25 s) before they
count. A deliberate press easily exceeds that; a modifier still held from the
arm press slipping into an ordinary keypress does not.

### Other ways it stops

| Trigger | Result |
|---|---|
| `Ctrl+C` | Aborts and resets the session |
| Target window closed | Pauses ("Target closed") — will not resume, the window is gone |
| Focus away for **more than 1.5 s** | Pauses, **resumes automatically** when you click back |
| Brief glance at the console (< 1.5 s) | **Ignored** — typing is not interrupted |
| Shell/IME/tooltip flash (`ForegroundStaging`, etc.) | **Ignored** |
| Target lost editability | Pauses ("Target lost editability") |
| Toggle key | Pauses and **holds** until you press it again |
| Text finished | Returns to Idle, prints "Typing complete" |

### Reading the console no longer stops typing

Reading the status is the natural thing to do while a run is in progress, and
every message invites it. Pausing on the first momentary focus change turned
that into a loop:

```
Focus moved to <console> - paused
Target selected -> typing started
Focus moved to <console> - paused        ... nothing ever typed
```

A focus change must now **persist for 1.5 seconds** before it counts as real,
so a glance is free. If focus really is gone, typing pauses and says so:

```
[typer] Focus moved to CASCADIA_HOSTING_WINDOW_CLASS - paused.
        Click back into your editor and typing resumes automatically.
```

While the change is undecided, **no keystrokes are sent at all** — they would
otherwise land in whichever window currently has focus. Waiting is always safe;
typing into the wrong window is not. Clicking back resumes from the exact
character it stopped on, and a pause you request with the toggle key still
holds — only focus loss resumes by itself.

### Which window is it typing into?

The target is locked when the run starts and the console says so by name:

```
[typer] Locked onto Notepad (hwnd 1970112)
```

This matters when several windows are open, because the run is bound to **one**
of them. It is re-locked rather than abandoned when you click into a different
editable window while paused, so a click into the editor you actually meant
takes over the run instead of leaving text going to a window now in the
background.

### Shell windows that flash during activation

`ForegroundStaging` is a hidden window Windows shows for a few milliseconds
during *any* window activation. It is not a real focus change and never
receives injected keystrokes, but an earlier version treated it as one and
looped forever:

```
Focus moved to ForegroundStaging - paused
Target selected -> typing started
Focus moved to ForegroundStaging - paused     ... repeating, nothing typed
```

These transient windows are now ignored: `ForegroundStaging`, `IME`,
`MSCTFIME UI`, `Default IME`, `Windows.UI.Core.CoreWindow`,
`XamlExplorerHostIslandWindow`, tooltips, taskbar previews and the search
popup. They are listed in `_TRANSIENT_CLASSES` in
`stealth_typer\adapters\win_target_detector.py`. A *real* focus change — your
console, another editor — still pauses as before.

Note that clicking the console to read the status also counts as "target
changed" and pauses typing. That is deliberate — it stops text going into the
wrong window — so use the hotkey rather than the console when you want to
pause without losing the target.


## If nothing gets typed

Work down this list — it is ordered by how often each cause turns out to be
the problem.

### 1. Read the keys the script printed

This is the first thing to check. At startup it tells you exactly what to
press:

```
  1. Click into the editor you want to type into
  2. Press alt+c to start typing
  3. While typing: ctrl+alt+k pauses/resumes
```

The keys are **chosen per machine**, because `Alt+X` and `Ctrl+Alt+P` are
already registered by other programs on many machines. Press the combination
it printed, not the one in this README.

### 2. Run the live probe

```powershell
python main.py --probe
```

This types nothing. It shows which hotkeys are free **on your machine** and a
live view of the focused window. Click into your editor and watch:

```
class                          hwnd      editable  focus  verdict
CASCADIA_HOSTING_WINDOW_CLASS  67048      False     True   THIS CONSOLE - click your editor
Notepad                        22612400   True      True   >>> READY - press alt+x <<<
```

When the line says `READY`, press the arm key. If it never turns READY, the
verdict column tells you why. If you press the key and it prints
`HOTKEY FIRED`, the key works and the problem is target selection.

### 3. Read the status line while it waits

The waiting line updates in place and turns to `[READY]` the moment you click
the right window:

```
  [waiting] CASCADIA_HOSTING_WINDOW_CLASS (this console - click your editor)  -> press alt+x
  [READY]   Notepad  -> press alt+x
```

| Message | Fix |
|---|---|
| `is editable but not focused` | Click inside the text area, then press the key |
| `(this console - click your editor)` | You armed from the terminal; click your editor |
| `is not editable - click a text field` | Click inside the actual text box, not a toolbar |
| `no window focused` | Click something first |

### 4. Click the text box, not the title bar

Arming requires keyboard focus. Clicking a Notepad title bar is not enough —
click **inside** the text area so you see a blinking caret.

### 4. Check injection works at all

```powershell
python main.py --simulate --file .\answer.txt --speed 100
```

If this prints the text, the engine is fine and the problem is target
selection. Then run `python main.py --self-test`, which prints the hotkeys it
resolved on your machine.

### 5. Elevated windows

If the target runs as Administrator (VS Code launched as admin), Windows blocks
input from a normal process. The console says
`UIPI / Elevated target - Run as Admin`. Start PowerShell as Administrator too.

### 6. Keyboard layout

A character your layout cannot produce is refused with its codepoint, e.g.
`Cannot type U+00E9 - unmapped in active layout`. Switch the target's input
language to English, or remove those characters from the file.

### 7. Force a specific key

```powershell
python main.py --file .\answer.txt --key f9
```


## Indentation modes

The one thing that goes wrong when typing code is indentation. When you press
ENTER in VS Code / PyCharm / Notepad++, the editor indents the new line itself.
Replaying the source's leading spaces on top of that **doubles the
indentation**. Notepad does not indent, so there the spaces *must* be typed.

### `smart` (default)

Skip the source's leading whitespace after each newline, assuming the editor
inserted its own.

```powershell
python main.py --file .\sample.py.txt --auto-indent smart
```

Use for: VS Code, PyCharm, IntelliJ, Notepad++, Sublime, Visual Studio.

### `raw`

Type the source's leading whitespace exactly as written.

```powershell
python main.py --file .\sample.py.txt --auto-indent raw
```

Use for: Notepad, WordPad, Word/Excel, terminals, plain text boxes.

### `auto` — both, decided for you

**Yes, you can have both.** `--auto-indent auto` inspects the target window
when you select it and picks `smart` or `raw` accordingly:

```powershell
python main.py --file .\answer.txt --auto-indent auto
```

It checks the window class name first, then the owning process name, and prints
what it decided:

```
[typer] Target selected - settling focus (250ms)...
[typer] Auto-indent resolved to smart (editor auto-indents, skipping source indentation)
```

Known editors are listed in `stealth_typer\adapters\win_target_detector.py`
(`_AUTO_INDENT_CLASSES` and `_AUTO_INDENT_PROCESSES`) — add your editor there if
it is missing.

**If the editor is not recognised, it falls back to `smart`**, because
over-skipping is easy to fix by deleting spaces, whereas typing them into an
auto-indenting editor leaves visible double-indentation you would have to clean
up afterwards.

### Which to use?

| Target | Mode |
|---|---|
| VS Code, PyCharm, IntelliJ, Notepad++, Sublime | `smart` or `auto` |
| Notepad, WordPad, Word, Excel | `raw` or `auto` |
| Plain text box on a web page | `raw` |
| Not sure | `auto` |

Prose (no leading indentation) behaves identically in every mode — the choice
only matters for code.

## Differences from the C++ original

| C++ | Python | Why |
|---|---|---|
| `WinTimer` + `UI dispatcher` on a Win32 window | `ThreadTimer` on a daemon thread | no window message loop needed |
| `Interception` keyboard driver | Win32 `SendInput` via `ctypes` | the shipped app bans `SendInput`; a standalone script has no driver, and `SendInput` works on any Windows box |
| `std::mt19937` seeded 1337 | `random.Random` seeded 1337 | same Mersenne Twister family, so the distribution matches (the exact stream differs) |
| UI window + status bar | console status lines | no GUI |
| Clipboard/image ports | text only | images belong to the OCR pipeline, not the typer |
| `AutoIndentMode{Raw, SmartIDE}` | + `AUTO` | this port adds detection of whether the target editor indents by itself |
| UI file picker | `--file` + encoding detection | CLI front end |
| `GetGUIThreadInfo` editability only | also refuses the console | the typer runs *in* a console, so it must never target one |

### Added in this port (not in the original)

* `--file` — read the text to type from a file, with encoding auto-detection.
* `--auto-indent auto` — resolve smart vs raw from the target window.
* `--dry-run` — print every codepoint instead of injecting it.
* `--timing-table` — dump sampled delays for a speed/humanization pair.
* `--self-test` — verify the install on this machine.
* `--simulate` — rehearse a full run with no real window or keyboard.
* `--key` (default `auto`) — **the single hotkey.** Click your editor and press
  it to start; press it again to pause; again to resume. One key rather than
  two, because mid-run you should not have to remember which combination was
  which. `auto` picks the first combination not already owned by another
  application, because `Alt+X` is taken on many machines. The C++ original
  drives this from its own window message loop; a console app needs
  `RegisterHotKey`.

## Safety notes

* `--dry-run` never touches the keyboard — use it first.
* Typing stops immediately if the target window is lost or becomes read-only.
* Characters outside your keyboard layout cannot be injected; the status line
  names the exact codepoint (e.g. `Cannot type U+00E9`) instead of failing
  vaguely.
* If a window is elevated, Windows blocks input from a normal process — run
  PowerShell as Administrator (or the app will report `UIPI / Elevated target`).
