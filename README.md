# Claude Code Usage Tray

A small Windows system tray app that shows live token usage, estimated
cost, and your session/weekly rate-limit status for Claude Code — read
straight from Claude Code's own local data.

Everything lives in one file: `claude_usage_tray.py`.

- No API key needed
- No network access of its own
- Conversation content is never read — only model names, token counts,
  and rate-limit percentages

## Quick start (running from Python)

1. Install Python 3.10+ if you don't have it: https://www.python.org/downloads/
   (on the installer's first screen, check **"Add python.exe to PATH"**)
2. Open a terminal in this folder and run:
   ```
   pip install -r requirements.txt
   python claude_usage_tray.py
   ```
3. A small icon appears in the system tray (bottom-right, may be in the
   "hidden icons" overflow arrow). Hover it for a quick tooltip, right-click
   for the full breakdown.

## Running the test suite

```
python claude_usage_tray.py --test
```

Runs entirely against a temporary synthetic folder — never touches your
real `~/.claude` data. Covers aggregation, cost calculation, incremental
re-reads, duplicate-message handling, the statusLine payload parser, the
usage-cache read/write round-trip, reset-time formatting, and (if
`watchdog` is installed) measures live filesystem-watcher latency.

## Token totals vs. session/weekly limits — two different data sources

**Tokens, cost, top models** come from Claude Code's local session logs
(`~/.claude/projects/**/*.jsonl`), which the app reads directly. No setup
needed beyond running the app.

**Session (5-hour) % and weekly (7-day) %** — the same numbers shown by
Claude Code's `/usage` command — are *not* stored locally anywhere.
They only exist inside Claude Code's live connection to Anthropic's
servers. The way to get them is to have Claude Code hand them to you
directly, via its `statusLine` hook:

### Setting up the statusLine hook (for session/weekly %)

1. Open (or create) `~/.claude/settings.json` (on Windows:
   `%USERPROFILE%\.claude\settings.json`) and merge in:
   ```json
   {
     "statusLine": {
       "type": "command",
       "command": "python C:/full/path/to/claude_usage_tray.py --statusline-hook"
     }
   }
   ```
   Use the full path to this script, with forward slashes (Windows accepts
   them fine in commands like this, and it avoids JSON backslash-escaping
   headaches).
2. Restart Claude Code. On your next turn, it'll start piping its session
   data to the script, which caches the rate-limit numbers and also prints
   a compact status line (e.g. `Claude Sonnet 4.6 | ctx 22% | 5h 34% | 7d 12%`)
   at the bottom of your terminal.
3. The tray app picks up that cache automatically — no restart needed.

**Important:** for this hook, run it via plain `python ...`, not the
bundled `.exe`. PyInstaller's windowed/`--noconsole` build (used for the
tray icon so it doesn't pop up a console) detaches stdin/stdout, which
breaks a tool that needs to read JSON from stdin and print text back. The
tray icon and the statusLine hook are two different use cases for the
same script — one wants no console, the other needs one.

**Note:** the `rate_limits` field in Claude Code's statusLine payload is
fairly recent (Claude Code ≥ v1.2.80-ish). If after setup the tray menu
still says "Session/weekly %: not available yet", update Claude Code —
older versions don't send that field at all, so there's nothing local to
read until then.

## Building a standalone .exe (no Python needed to run it afterwards)

1. Run `build_exe.bat` (double-click it, or run it from a terminal).
2. It installs PyInstaller and bundles everything into one file.
3. Find `ClaudeUsageTray.exe` inside the new `dist` folder.
4. Copy that .exe wherever you like — it's self-contained.

By default PyInstaller stamps the .exe with its own generic icon. To give
it the app's actual icon instead (the same green/yellow/red usage gauge
the tray shows at runtime), generate `icon.ico` once and pass it to
PyInstaller:

```bash
python generate_icon.py
pyinstaller --onefile --noconsole --icon=icon.ico --name ClaudeUsageTray claude_usage_tray.py
```

Add the `--icon=icon.ico` flag to whatever `pyinstaller` line is inside
your `build_exe.bat` so future builds pick it up automatically. This is
a separate asset from the tray icon: the tray icon is drawn by Pillow at
runtime (no file needed), but the .exe's own file/taskbar icon has to be
baked in at build time, so it needs a real `.ico` on disk.

This exe is for the tray icon only. Keep using `python
claude_usage_tray.py --statusline-hook` for the statusLine hook (see
above for why).

## Run it automatically when Windows starts

1. Press `Win + R`, type `shell:startup`, press Enter.
2. Drop a shortcut to `ClaudeUsageTray.exe` into that folder.
3. It'll launch quietly in the tray every time you log in.

## Floating widget

Besides the tray icon, the app also shows a small always-on-top widget
with the same live numbers — no hovering or clicking needed. Percentage
bars are real drawn rectangles (not text characters), so they can't get
clipped or overflow regardless of font:

```
┌───────────────────────────────────────┐
│ Claude Code                         ×│
│ Today           12.3K tok   $0.05    │
│ Session 5h ███████░░░  71%  resets 18:42 │
│ Weekly  7d  ██░░░░░░░░  18%  resets Mon 09:00 │
└───────────────────────────────────────┘
```

- **Initial position**: on first run, it tries to sit just to the left of
  your real taskbar tray icons (using `pywin32` to find the actual
  notification-area rectangle on screen — see "About the taskbar
  placement" below). If that can't be detected, it falls back to a
  bottom-right corner estimate.
- **Drag it anywhere** by clicking and holding on the widget — its
  position is then remembered across restarts (the auto-placement above
  only applies the very first time, before you've ever dragged it).
- **Close it** with the small `×` in the corner, or toggle it from the
  tray's right-click menu ("Floating widget").
- The percentage bars are color-coded: green under 50%, yellow 50-80%,
  red 80%+.
- It shares the same data as the tray menu, so it needs the statusLine
  hook (below) for the session/weekly lines to populate.

It's built with `tkinter`, which ships with the standard python.org
Windows installer by default — no extra dependency for the widget itself.

### About the taskbar placement

Finding exactly where your tray icons are isn't something Python (or
even Tkinter) can do on its own — it requires asking Windows directly
for the taskbar's notification-area window handle, via `pywin32`
(`win32gui.FindWindow("Shell_TrayWnd", ...)`). This only affects *where
the widget appears the first time you ever run it* — once you've dragged
it anywhere, your position is saved and used from then on instead.

I can't fully verify this exact lookup myself (it needs a real Windows
taskbar to query — my dev environment doesn't have one), so if the
auto-placement on first launch looks off, that's the part to flag. The
rest of the widget (sizing, rendering, drag, persistence, show/hide) I
tested directly and confirmed working, including a real rendered
screenshot during development.

If `pywin32` isn't installed, the lookup is silently skipped and the
widget just uses the screen-corner fallback instead — nothing breaks.

## What it shows

- **Today** / **all-time**: total tokens and estimated USD cost
- **Session (5h)**: % of your rolling 5-hour rate limit used, and when it
  resets (needs the statusLine hook above)
- **Weekly (7d)**: % of your weekly rate limit used, and when it resets
  (needs the statusLine hook above)
- **Context window**: how full the current conversation's context window is
- **Top models**: which models are eating the most tokens
- **Sessions seen**: number of distinct Claude Code sessions counted
- **Refresh now**: force an immediate full rescan
- **Open logs folder**: jumps straight to `~/.claude/projects` in Explorer

Token totals update the moment Claude Code writes to a session file — a
filesystem watcher reacts directly rather than checking on a timer
(tested at ~6-16ms in development). Session/weekly % updates the moment
the statusLine hook (above) writes a fresh cache, which happens on every
Claude Code turn. A slower full rescan every 30 seconds runs only as a
safety net.

## About the cost estimate

There's a small price table near the top of `claude_usage_tray.py`
(`PRICES_PER_MILLION`, USD per million tokens, for input / output /
cache-write / cache-read across the Opus / Sonnet / Haiku tiers). These
rates can change — if the numbers look off, check
https://platform.claude.com/docs/en/about-claude/pricing and update the
dict.
