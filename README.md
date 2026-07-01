# Claude Code Usage Tray

A small **Windows** system tray app + floating widget that shows live token
usage, estimated cost, and your session/weekly rate-limit status for
[Claude Code](https://claude.ai/code) — read straight from Claude Code's own
local data.

Everything lives in one file: `claude_usage_tray.py`.

- **No API key needed** — it reads Claude Code's local session logs, not the API
- **No network access of its own** — nothing is uploaded anywhere
- **Privacy-preserving** — conversation content is never read, only model names,
  token counts, and rate-limit percentages
- **Real-time** — token totals update the instant Claude Code writes a turn (a
  filesystem watcher reacts directly, no polling delay)

```
┌──────────────────────────────────────────────────────┐
│  Today      12.3K tok   $0.05                        │
│  5h  ███71%███  18:42   ·   7d  ██18%░░░░  Mon 09:00 │
└──────────────────────────────────────────────────────┘
```

The floating widget is a borderless, always-on-top panel — just two compact rows
(no title bar, no close button): **Today**'s tokens and cost, then the **5h** and
**7d** bars side by side. The percentage is drawn *inside* each colored bar, with
its reset time next to it. The tray icon shows the same numbers on
hover/right-click. *(Drop a real screenshot here once you have one.)*

## Requirements

- **Windows** (the tray icon, floating-widget placement, and startup toggle are
  Windows-specific; `--test` and the statusLine hook run anywhere)
- **Python 3.10+** — https://www.python.org/downloads/
- Dependencies (installed via `requirements.txt`):
  - `pystray` — system tray icon
  - `Pillow` — draws the tray icon at runtime (no image file to ship)
  - `watchdog` — filesystem watching for real-time updates
  - `tkinter` — the floating widget; ships with the standard python.org
    installer, no extra install

> **Note:** `pywin32` is **not** required. First-run taskbar placement uses the
> Windows API via `ctypes` from the standard library — nothing to install.

## Quick start

```bash
git clone https://github.com/Celtas6655/claudebar-usage.git
cd claudebar-usage
pip install -r requirements.txt
python claude_usage_tray.py
```

A small icon appears in the system tray (bottom-right, possibly tucked inside
the "hidden icons" overflow arrow). Hover it for a quick tooltip, right-click for
the full breakdown. A floating widget with the same live numbers also appears.

> On the Python installer's first screen, check **"Add python.exe to PATH"**. If
> `python` isn't found, close and reopen your terminal — a terminal opened before
> Python was installed won't see the updated PATH.

## Token totals vs. session/weekly limits — two different data sources

This is the one thing worth understanding. The app surfaces two categories of
data that come from completely different places:

**Tokens, cost, top models** come from Claude Code's local session logs
(`~/.claude/projects/**/*.jsonl`), which the app reads directly. **No setup
needed** beyond running the app.

**Session (5-hour) % and weekly (7-day) %** — the same numbers shown by Claude
Code's `/usage` command — are *not* stored locally anywhere. They only exist
inside Claude Code's live connection to Anthropic's servers. The only way to get
them is to have Claude Code hand them to you, via its `statusLine` hook.

### Setting up the statusLine hook (for session/weekly %)

**Easy way — let the app wire it up for you:**

```bash
python claude_usage_tray.py --install-hook
```

This merges the `statusLine` entry into `~/.claude/settings.json` for you
(atomic write; it refuses to touch the file if it can't be parsed cleanly, so it
won't corrupt an existing config). Then **restart Claude Code**.

**Manual way** — open (or create) `~/.claude/settings.json` (on Windows:
`%USERPROFILE%\.claude\settings.json`) and merge in:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python C:/full/path/to/claude_usage_tray.py --statusline-hook"
  }
}
```

Use the full path to the script, with forward slashes (Windows accepts them in
commands like this, and it avoids JSON backslash-escaping headaches). Restart
Claude Code.

Either way: on your next turn, Claude Code starts piping its session data to the
script, which caches the rate-limit numbers and prints a compact status line
(e.g. `Claude Sonnet 4.6 | ctx 22% | 5h 34% | 7d 12%`) at the bottom of your
terminal. The tray app and widget pick up that cache automatically — no restart
of the app needed.

> **Run the hook via plain `python ...`, not the bundled `.exe`.**
> PyInstaller's windowed/`--noconsole` build (used for the tray icon so it
> doesn't pop a console) detaches stdin/stdout, which breaks a tool that needs to
> read JSON from stdin and print text back. The tray icon and the statusLine hook
> are two different use cases for the same script.

> **The `rate_limits` field is fairly recent** in Claude Code's statusLine
> payload (Claude Code ≥ v1.2.80-ish). If after setup the tray still says
> "Session/weekly %: not available yet", update Claude Code — older versions
> don't send that field at all. Also note it's typically empty on the very first
> render of a new session and only populates after the first completed response,
> so send a second message before concluding anything is wrong.

## What it shows

Right-click the tray icon (or read the floating widget) for:

- **Today** / **all-time**: total tokens and estimated USD cost
- **Session (5h)**: % of your rolling 5-hour rate limit used, and when it resets
  *(needs the statusLine hook above)*
- **Weekly (7d)**: % of your weekly rate limit used, and when it resets
  *(needs the statusLine hook above)*
- **Context window**: how full the current conversation's context window is
- **Top models**: which models are eating the most tokens
- **Sessions seen**: number of distinct Claude Code sessions counted
- **Refresh now**: force an immediate full rescan
- **Open logs folder**: jumps straight to `~/.claude/projects` in Explorer
- **Run on Windows startup**: toggle to launch the app automatically at login
  (Windows only)

Token totals update the moment Claude Code writes to a session file — a
filesystem watcher reacts directly rather than checking on a timer (measured at
~6–16 ms in development). Session/weekly % updates the moment the statusLine hook
writes a fresh cache, which happens on every Claude Code turn. A slower full
rescan every 30 seconds runs only as a safety net.

## Floating widget

Besides the tray icon, the app shows a small borderless, always-on-top widget
with the same live numbers — no hovering or clicking needed. It's two compact
rows (a `Today` line and the `5h`/`7d` bars side by side, as shown above) sized to
tuck into your taskbar height. The percentage bars are real drawn rectangles (not
text characters), so they can't be clipped or overflow regardless of font, with
the percentage drawn centered inside each bar.

- **Initial position**: on first run, it tries to sit just to the left of your
  real taskbar tray icons, using the Windows API (via `ctypes` — stdlib, nothing
  to install) to find the actual notification-area rectangle on screen. If that
  can't be detected, it falls back to a bottom-right corner estimate.
- **Drag it anywhere** by clicking and holding on the widget — its position is
  then remembered across restarts (the auto-placement only applies the very first
  time, before you've ever dragged it).
- **Show/hide it** from the tray's right-click menu ("Floating widget"). There's
  no close button on the widget itself — it's borderless by design so it blends
  into the taskbar.
- The percentage bars are color-coded: green under 50%, yellow 50–80%, red 80%+.
- It shares the same data as the tray menu, so it needs the statusLine hook for
  the session/weekly lines to populate.

> The first-run taskbar placement is **best-effort and unverified on real
> hardware** — it needs a live Windows taskbar to query, which the dev
> environment doesn't have. It only affects where the widget appears the very
> first time (before you drag it), and it falls back gracefully if detection
> fails. If first-launch placement looks off, that's the part to flag. See
> `ARCHITECTURE.md` for details.

## Building a standalone .exe (no Python needed to run it afterwards)

Generate the icon once, then bundle with PyInstaller:

```bash
pip install pyinstaller
python generate_icon.py
pyinstaller --onefile --noconsole --icon=icon.ico --name ClaudeUsageTray claude_usage_tray.py
```

The result is `dist/ClaudeUsageTray.exe` — self-contained; copy it wherever you
like. The `--icon=icon.ico` flag stamps the app's actual usage-gauge icon onto
the `.exe`; without it PyInstaller uses its own generic icon. (The tray icon
itself is drawn by Pillow at runtime, but the `.exe`'s file/taskbar icon has to
be baked in at build time, so it needs a real `.ico` on disk.)

If you build often, wrap that `pyinstaller` line in a `build_exe.bat` of your
own for convenience.

> This `.exe` is for the tray icon only. Keep using
> `python claude_usage_tray.py --statusline-hook` for the statusLine hook (see
> above for why the windowed build can't do it).

## Run it automatically when Windows starts

Right-click the tray icon and check **"Run on Windows startup"**. This adds a
per-user entry to
`HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run` (no admin
rights needed) that launches the app quietly at login — unchecking it removes the
entry. The menu item is grayed out on non-Windows platforms.

> The registry read/write is unit-tested against a disposable subkey but hasn't
> been confirmed end-to-end across a real logout/login yet. If the app doesn't
> appear after enabling it, check Task Manager's Startup tab or `regedit` under
> the path above for a `ClaudeUsageTray` value.
>
> If you move, rename, or rebuild the `.exe` (or move the script, if running from
> source) after enabling the toggle, the registry entry still points at the old
> location and won't launch correctly until you toggle it off and back on from
> the new location. This isn't auto-repaired.

Prefer not to touch the registry? The manual alternative works too: press
`Win + R`, type `shell:startup`, press Enter, and drop a shortcut to the `.exe`
(or a `python claude_usage_tray.py` launcher) into that folder.

## About the cost estimate

There's a small price table near the top of `claude_usage_tray.py`
(`PRICES_PER_MILLION`, USD per million tokens, for input / output / cache-write /
cache-read across the Opus / Sonnet / Haiku tiers). These rates change over time
and aren't wired to any live source — if the numbers look off, check
https://platform.claude.com/docs/en/about-claude/pricing and update the dict.

## Running the test suite

```bash
python claude_usage_tray.py --test
```

Runs entirely against a temporary synthetic folder — it never touches your real
`~/.claude` data and needs no GUI backend (no `pystray`/`Pillow`/`tkinter`), so
it works in a bare CI container. Covers aggregation, cost calculation,
incremental re-reads, duplicate-message handling, the statusLine payload parser,
the usage-cache read/write round-trip, reset-time formatting, the startup-toggle
registry round-trip, and (if `watchdog` is installed) live filesystem-watcher
latency.

## Project layout & further reading

| File | What it is |
|---|---|
| `claude_usage_tray.py` | The entire app — tray, widget, tracker, statusLine hook, tests. Deliberately single-file. |
| `generate_icon.py` | Generates `icon.ico` for the PyInstaller build. |
| `requirements.txt` | Runtime dependencies. |
| `ARCHITECTURE.md` | Deep design & decision history — the two data sources, threading model, two bug postmortems, what's verified vs. not. **Read this before any architectural change.** |
| `CLAUDE.md` | Short index / guidance for AI agents working in this repo. |

## Contributing

Contributions welcome. A few conventions this project holds to:

- Run `python claude_usage_tray.py --test` before opening a PR — it must stay
  green and GUI-free.
- Keep **pure, testable logic at module level** and **GUI-only code as closures
  inside `run_app()`** — that split is what lets `--test` run without a display.
- Be direct about what's verified vs. not (some behaviors can only be confirmed
  on real Windows hardware).
- See `ARCHITECTURE.md` and `CLAUDE.md` for the non-negotiable constraints
  (atomic cache writes, no cross-thread Tkinter calls, don't size a window before
  rendering its content, etc.).

## License

Released under the [MIT License](LICENSE).
