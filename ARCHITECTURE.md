# Architecture & Decision History — Claude Code Usage Tray

This document exists to onboard an AI agent (or a human) picking up this
project for the first time, with zero prior context. It captures not
just *what* the code does, but *why* it's built this way — including the
dead ends, the bugs found and fixed, and the things that were explicitly
left untested or unsolved. Read this before changing anything
architectural; a lot of the current shape is the result of a real
debugging conversation, not a clean first draft.

`CLAUDE.md` in the repo root is the short index that points here. This
file is allowed to be long.

---

## 1. What this project is

A Windows system tray app + floating desktop widget that shows **live
Claude Code usage**: token counts, estimated cost, and — critically —
your account's **session (5-hour) and weekly (7-day) rate-limit
percentages**, with reset times. Everything updates in close to real
time (sub-second for token data, on every Claude Code turn for rate
limits), with no manual refreshing.

It was built conversationally, in stages, in response to a single
person's actual use case (they wanted to glance at usage without typing
`/usage` or running out of quota mid-task). Each stage is documented
below because the *reasons* for each decision matter if you're going to
extend this safely.

## 2. The core architectural fact: there are two unrelated data sources

This is the single most important thing to understand before touching
this codebase. The app surfaces two categories of information that come
from **completely different places** and have **completely different
freshness/availability characteristics**:

### 2a. Token counts, cost, "today" / "all-time" totals, top models

**Source**: Claude Code's own local session transcripts, written to
`~/.claude/projects/<project>/**/*.jsonl` (Windows:
`%USERPROFILE%\.claude\projects\...`). One JSONL file per session,
append-only. Each assistant turn is a line like:

```json
{
  "type": "assistant",
  "uuid": "...",
  "sessionId": "...",
  "timestamp": "2026-06-26T10:15:00.000Z",
  "message": {
    "model": "claude-sonnet-4-6",
    "usage": {
      "input_tokens": 137,
      "output_tokens": 4260,
      "cache_creation_input_tokens": 5521,
      "cache_read_input_tokens": 815193
    }
  }
}
```

This data is **fully local, retroactive, and always available** — no
live connection to anything is needed to read it. It exists the moment
Claude Code writes it, and it covers your entire history, not just the
current session.

**How we read it**: `UsageTracker` (in `claude_usage_tray.py`) walks
`~/.claude/projects` recursively, reads each `.jsonl` file, and
aggregates by day / model / project. It tracks a byte offset per file
(`file_pos` dict) so repeat reads only look at *new* bytes — this is
what makes near-instant updates cheap even with a large session history.
It also dedupes by message `uuid` (`seen_uuids` set), because a session
file can in principle be re-read or partially replayed and we never want
to double-count a turn.

**Cost estimation** is a local approximation: a small table
(`PRICES_PER_MILLION`) maps a model-name substring ("opus" / "sonnet" /
"haiku") to approximate USD-per-million-token rates for input / output /
cache-write / cache-read, sourced from Anthropic's published pricing at
the time this was built (mid-2026). **This will drift out of date.**
It's not connected to any live pricing source — there isn't a good local
one. Anyone maintaining this should periodically check
`https://platform.claude.com/docs/en/about-claude/pricing` and update
the table. This is flagged in-code and in the user-facing README too.

### 2b. Session (5h) % and weekly (7d) % rate limits, with reset times

**Source**: This is the part that is *not* stored locally anywhere, and
this took real investigation to figure out during development. Rate
limit consumption against your plan's quota is account-level state that
only Anthropic's servers know about. Claude Code has a live connection
to those servers and knows your current usage, but **it does not persist
that information to any file you can read** — except for one mechanism:
the **`statusLine` hook**.

Recent Claude Code versions (the `rate_limits` field appeared sometime
in the months before mid-2026 — exact version not pinned, see §9 on
limitations) pipe a JSON payload via **stdin** to whatever command is
configured as `statusLine` in `~/.claude/settings.json`, on every turn.
That payload looks like:

```json
{
  "model": { "display_name": "Claude Sonnet 4.6" },
  "context_window": { "used_percentage": 22.4 },
  "rate_limits": {
    "five_hour": { "used_percentage": 34.0, "resets_at": 1782496905 },
    "seven_day": { "used_percentage": 12.5, "resets_at": 1782664305 }
  }
}
```

(`resets_at` is a Unix timestamp in seconds.) Claude Code expects
whatever this command prints to stdout back to *become* the visible
status line text in the terminal.

**How we use it**: `claude_usage_tray.py --statusline-hook` is a second
entry point in the same script. It's meant to be set as the literal
`statusLine.command` in Claude Code's settings. It reads the JSON from
stdin, extracts the fields we care about via the pure function
`process_statusline_payload()`, atomically writes them to a small cache
file (`~/.claude/usage_tray_cache.json`, via temp-file + `os.replace()`
so concurrent reads never see a half-written file), and prints a compact
status line back (e.g. `Claude Sonnet 4.6 | ctx 22% | 5h 34% | 7d 12%`)
so the terminal still shows something useful.

The tray app and floating widget then just read that cache file. They
have **no way to know session/weekly % without this hook being
configured** — there is no fallback data source for this specific
metric, by design of how Claude Code itself works, not a limitation of
this app. If the cache file doesn't exist or is stale, the UI says so
explicitly ("Session/weekly %: not available yet — set up the
statusLine hook") rather than guessing or hiding the absence silently.

**Practical consequence learned the hard way during setup**: the
`rate_limits` field is typically empty on the very first statusline
render of a brand-new or just-cleared session — it only populates after
the *first fully completed response*. Don't read "nothing showed up
after one message" as a bug; send a second message before concluding
anything is broken.

## 3. Why a single Python file

This started as a multi-file Python project (`parser.py` + `app.py` +
`test_parser.py`), then was explicitly consolidated into one file,
`claude_usage_tray.py`, on request — specifically so there's one thing
to download/run/test, with `--test` and `--statusline-hook` as CLI flags
on the same script rather than separate entry points. If this project
grows substantially, splitting it back into a package is reasonable, but
that's a deliberate tradeoff to make consciously (see §11), not a
default — the single-file shape was a real, stated requirement, not an
accident of how it was first written.

## 4. Technology choices and why

| Choice | Alternatives considered | Why this one |
|---|---|---|
| Python | Node.js/Electron, C#/.NET | Lightest footprint, trivial single-`.exe` bundling via PyInstaller, no heavy runtime (~150MB+ for Electron) |
| `pystray` | Manual `win32gui` tray icon code | Cross-backend tray abstraction; supports `run_detached()` which turns out to matter a lot (see §5) |
| `Pillow` | Static `.ico` asset | Tray icon is generated programmatically (a simple drawn circle) so there's no binary asset to ship/track in the repo |
| `watchdog` | Polling on a timer | Real-time was an explicit, later request ("is this real-time?") — OS-level filesystem-change notifications (`ReadDirectoryChangesW` on Windows under the hood) react in single-digit milliseconds vs. up to N seconds of polling lag |
| `tkinter` (stdlib) | A second Electron/web view, a native win32 window | Ships with the standard python.org Windows installer by default, zero extra dependency, sufficient for a small HUD |
| `ctypes` (stdlib) taskbar lookup | `pywin32`, or skipping precise placement entirely | Asks Windows directly (via `user32` calls through `ctypes`) where the real taskbar notification area is on screen. Originally used `pywin32`; moved to `ctypes` so there's zero extra dependency to install. Wrapped so any failure degrades gracefully, never a hard requirement. |

## 5. Threading & concurrency model — read this before touching `run_app()`

This is the part most likely to bite someone making "obvious" changes.

- **`pystray`'s tray icon runs detached.** `icon.run_detached()` is
  called instead of `icon.run()`. Per pystray's own source
  (`_base.py`), `run_detached()` exists specifically "to allow
  integrating pystray with other libraries requiring a mainloop." On the
  Windows backend, `_run_detached()` just spins up a background thread
  running the normal message loop (verified by reading
  `pystray/_win32.py` directly: `threading.Thread(target=lambda:
  self._run()).start()`). This was necessary because:
- **Tkinter must own the main thread.** The floating widget's
  `root.mainloop()` blocks on whatever thread calls it, and Tkinter is
  not generally safe to drive from a non-main thread on Windows. So the
  startup sequence is: build the tray icon → `icon.run_detached()` →
  construct `FloatingWidget()` → `widget.mainloop()` on the main thread.
- **No cross-thread Tkinter calls, anywhere.** The tray's background
  thread (where menu-click callbacks like "Quit" or "Floating widget"
  toggle fire) never calls a Tkinter method directly. Instead:
  - `widget_visible` (`threading.Event`) is set/cleared by the tray
    callback; the widget's own `_tick()` (running via `root.after(1000,
    ...)`, i.e. *inside* the Tk thread) checks it once a second and
    calls `deiconify()`/`withdraw()` itself.
  - `should_quit` (`threading.Event`) works the same way for clean
    shutdown: the tray's "Quit" sets it and stops the icon; the widget's
    `_tick()` notices and calls `self.root.quit()` from the correct
    thread.
  - This pattern (flag-set-elsewhere, polled-from-the-GUI-thread) is the
    one to keep using for any future cross-thread interaction. Don't be
    tempted to call a Tk method directly from a `pystray` callback "just
    this once" — it'll work in light testing and fail unpredictably
    later.
- **The widget self-polls rather than being pushed to.** `_tick()` reads
  `tracker.snapshot()` and `read_usage_cache()` fresh every second. This
  was a deliberate simplification over wiring the `watchdog` filesystem
  events into the Tk thread — both data sources are cheap to read (an
  in-memory dict copy, or one small JSON file), so a 1-second self-poll
  is "real-time enough" without the complexity/fragility of routing
  watcher events across threads safely.
- **`state_lock`** (a plain `threading.Lock`) guards all reads/writes of
  the shared `UsageTracker` instance, since it's touched by the
  filesystem-watcher thread, the fallback-sweep thread, the tray's
  "Refresh now" callback, and indirectly by the widget's `_tick()` via
  `current_snapshot()`.

### Filesystem watching specifics

Two separate `watchdog` `Observer`s are started (in `watcher_loop()`):
one recursive on `PROJECTS_DIR` (reacts to any `.jsonl` write — token
data), one non-recursive on `CLAUDE_HOME` itself (reacts to the one
`usage_tray_cache.json` file changing — rate-limit data, written by the
statusline hook). Both retry on a 5-second loop until their target
directory exists, in case the app is started before Claude Code has ever
run (so `~/.claude/projects` doesn't exist yet). A `fallback_sweep_loop`
does a full rescan every 30 seconds regardless, purely as a safety net —
e.g. to roll "today" over at midnight even if nothing's actively
happening, and as a backstop in case a watcher event is ever missed for
any reason.

## 6. The floating widget — design history and two real bug postmortems

The widget went through three iterations worth recording, because the
bugs found are easy to reintroduce if someone "simplifies" the layout
later.

### Iteration 1: a single `tkinter.Text` widget with inline `\n`-joined lines

The first version built each line as a string with a trailing `\n`,
inserted into one `Text` widget with `width=36, height=4` (character
grid sizing), and built progress bars out of Unicode block characters
(`█`/`░`) directly in the text. Window size was computed from
`winfo_reqwidth()`/`winfo_reqheight()` **before** the real content was
rendered (i.e., sizing happened against an empty placeholder).

**Bug 1 — text clipped (`"resets Fr"` and the rest invisible)**: Unicode
block-drawing characters don't have guaranteed-uniform advance width
across fonts and platforms, even in a "monospace" font. The dev/test
environment (Linux, no real `Consolas`) measured those glyphs narrower
than real Windows `Consolas` did, so the box Tk sized based on
ASCII-character metrics for "36 characters" turned out too narrow once
real Windows rendered the bar glyphs — the right portion of each line
(where "resets ..." lived) got pushed past the widget's edge and
silently clipped, with no scrollbar to reveal it (`wrap="none"`).

**Bug 2 — a stray empty row at the bottom**: every line in the `Text`
widget ended with `"\n"`, so after the last real line there was an
*implicit* extra blank line (a `Text` widget with content ending in
`\n` always has a trailing empty line). Combined with a hardcoded
minimum-height floor (`max(reqheight, 95)`) that didn't match the actual
4-line content's true rendered height, this produced visible dead space.

### Iteration 2 (current): Canvas-drawn bars + per-row Frame/Label layout

The fix was not cosmetic patching — it changes the actual rendering
strategy:

- **Percentage bars are now real `Canvas` rectangles**, drawn pixel-precise
  (`canvas.create_rectangle(0, 0, fill_width, BAR_H, fill=color)`), not
  text glyphs. This eliminates the entire bug class — there's no font
  involved in the bar's width at all anymore.
- **Each logical line is its own `Frame` of `Label`/`Canvas` widgets**,
  packed with `pack(fill="x")`, instead of one `Text` widget with
  literal newlines. No `Text` widget means no implicit trailing blank
  line is structurally possible.
- **Render before sizing, strictly**: `__init__` calls `self._render()`
  (populate real label text and draw real bars) **then**
  `self.root.update_idletasks()` **then** `self._place_initial()` (which
  reads `winfo_reqwidth()`/`reqheight()`). The window is sized from
  what's actually on screen, not from a guess. There's no hardcoded
  minimum-size floor anymore — if this regresses, check that ordering
  first.

### How this was verified (since there's no Windows machine in the dev sandbox)

There's a recurring tension in this project: development happened in a
Linux sandbox with no real Windows taskbar, no real `pystray`
GTK/AppIndicator backend, and no real Consolas font. The verification
approach that worked, and is worth reusing for any future GUI change:

1. Install `python3-tk` and start a virtual display: `Xvfb :99 -screen 0
   1280x800x24` (background it with `setsid ... &`, not plain `&`, or it
   gets reaped when the shell session ends).
2. Stub out `pystray` with a minimal fake module (`FakeIcon`,
   `FakeMenuItem`, `FakeMenu` — just enough to satisfy `run_app()`'s
   calls without needing a real GTK/AppIndicator backend), injected via
   `sys.modules["pystray"] = fake_pystray` before importing the real
   script. This lets the **real** `tkinter` widget, the **real**
   `watchdog` observer, and the **real** `run_app()` control flow run
   end-to-end, with only the literal tray icon faked out.
3. Drive the real Tk mainloop from a background thread: find the live
   window via `tkinter._default_root`, exercise it (geometry checks,
   simulated drag, toggling visibility via the captured menu item's
   `.action()` callback), then call `root.quit()` to cleanly end the
   test.
4. For an actual visual check (not just geometry assertions), use
   ImageMagick's `import -window root screenshot.png` against the Xvfb
   display, then crop to the widget's known geometry and view the
   result. This caught the clipping bug being genuinely fixed, not just
   "the assertions pass" — a real rendered screenshot showed the clean
   4-row layout with correctly colored bars.

This whole verification harness was **dev-only** and intentionally not
shipped in the repo — `--test` (the user-facing test mode) deliberately
never imports `pystray`/`Pillow`/`tkinter` at all (see §7), so it can't
accidentally depend on Xvfb or a stub. If you need to re-verify a GUI
change, recreate this harness as a throwaway script; don't merge it into
`claude_usage_tray.py` itself.

### Initial widget placement: best-effort taskbar detection

On first run (no saved drag position yet), `_default_position()` tries
`find_tray_notification_rect()` (a thin wrapper over
`find_taskbar_tray_rect()`), which calls the Win32 API directly through
`ctypes` (`user32.FindWindowW("Shell_TrayWnd", None)` → walk its child
tree for `TrayNotifyWnd` → `GetWindowRect(...)`) to find the real screen
coordinates of the notification area where tray icons live, and
positions the widget just to its left, vertically centered on the
taskbar's height. `ctypes` is stdlib, so there's nothing to install —
the earlier `pywin32` dependency was dropped. This is
**explicitly unverified on real Windows hardware** — the dev sandbox has
no Windows taskbar to query. It's wrapped defensively (`sys.platform`
check, broad `except Exception` around every `ctypes` call, and a
reserved-width fallback when no tray child window can be located) so any
failure — wrong window classes on some Windows version, the taskbar
being on a secondary monitor, etc. — falls back to a screen-corner
heuristic
(`sw - w - 220, sh - h - 50`) rather than crashing. Because the user's
own drag position is saved and preferred from then on, this heuristic
only matters for the very first launch ever — low stakes, but worth
flagging clearly as "designed, not field-verified" if you're asked about
it.

### Favorite widget position (single slot)

A fourth small JSON sidecar, `usage_tray_widget_favorite_pos.json`
(`WIDGET_FAVORITE_POS_PATH`), holds one user-designated "return to this
spot" position — independent of `usage_tray_widget_pos.json`, which always
tracks wherever the widget last ended up (drag, alignment, or a favorite
load). It's a single slot by deliberate choice, not a named list, per an
explicit product decision — this was raised and decided directly with the
project owner rather than assumed.

Two tray menu items back it: "Save current position as favorite" reads
`FloatingWidget.last_known_pos` (a plain tuple mirrored from the Tk thread
into a shared attribute after every reposition — `__init__`, `_end_drag()`,
`_tick()`) rather than calling any Tkinter getter from the tray thread.
"Load favorite position" sets `load_favorite_requested` (a
`threading.Event`), which `FloatingWidget._tick()` polls and applies via
`_apply_favorite_position()` — the newest instance of the
flag-set-elsewhere/polled-in-`_tick()` pattern already described in §5, not
a new mechanism. Loading a favorite also disables `ALIGNMENT_CONFIG.enabled`
and overwrites `usage_tray_widget_pos.json`, mirroring exactly what
`_end_drag()` already does, so a restart keeps the loaded favorite as the
new baseline instead of reverting to wherever the widget was before the load.

## 7. Testing philosophy

`python claude_usage_tray.py --test` runs a self-contained suite with
two hard constraints that shape how it's written:

1. **It must work without a GUI backend.** `pystray`, `Pillow`, and
   `tkinter` are imported lazily *inside* `run_app()`, never at module
   level. This means `--test` can run in a bare CI container or a
   sandboxed dev environment with no display at all — which is exactly
   how it was developed and is still run today.
2. **It must never touch the real `~/.claude` directory.** Every test
   builds a temporary directory tree and passes it explicitly (e.g.
   `UsageTracker(projects_dir=...)`, `write_usage_cache(cache,
   cache_path=...)`) rather than relying on the `CLAUDE_HOME`
   environment-variable override implicitly, specifically so a test run
   can never corrupt or pollute someone's actual Claude Code data.

What's covered: aggregation correctness (totals, today-vs-all-time
split, per-model/per-project breakdown), idempotent re-polling (polling
twice with no new data must not double-count), incremental
append-detection (`poll_file` picking up only new bytes), duplicate
message-`uuid` handling, `statusLine` payload parsing (both the
fully-populated and the missing-fields case), the usage-cache
write/read round-trip, reset-time formatting (both relative "`2h 14m`"
and absolute "`Fri 18:42`" forms), the percentage color-threshold
function, live filesystem-watcher latency (this is a real, not mocked,
`watchdog` `Observer` — measured consistently in the single-digit
milliseconds in this sandbox), and graceful degradation of the
Windows-only taskbar lookup on non-Windows platforms.

What's *not* covered by `--test` (by design, per §6): anything about the
floating widget's actual rendering/layout/geometry. That was verified
separately with the Xvfb+stub approach described above, as a one-off
development check, not as a repeatable automated test. If this project
gets a CI pipeline, building a proper (containerized, Xvfb-backed)
version of that harness would be a reasonable investment — see §11.

## 8. File / data inventory

Everything this app reads or writes, and why:

| Path | Written by | Read by | Purpose |
|---|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code itself (not us) | `UsageTracker` | Token usage, cost, model/project breakdown. Read-only to us. |
| `~/.claude/usage_tray_cache.json` | `--statusline-hook` mode | tray menu, floating widget | Session (5h) %, weekly (7d) %, context-window %, reset timestamps. Atomic write (temp file + `os.replace`). |
| `~/.claude/usage_tray_widget_pos.json` | `FloatingWidget._end_drag()` | `FloatingWidget._default_position()` | Remembers where the user dragged the widget, so the taskbar-detection heuristic only applies once, ever. |
| `~/.claude/usage_tray_widget_favorite_pos.json` | tray menu's "Save current position as favorite" (`on_save_favorite`, via `widget.last_known_pos`) | `FloatingWidget._apply_favorite_position()`; "Load favorite position" menu item's `enabled=` check | User-designated single favorite screen position, independent of the last-dragged position (`usage_tray_widget_pos.json`). Same atomic-write shape. |
| `~/.claude/settings.json` | the user, manually | Claude Code itself | Where `statusLine.command` is wired to point at `python ... --statusline-hook`. Not managed by this app — just documented. |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` (`ClaudeUsageTray` value) | tray menu's "Run on Windows startup" toggle (`on_toggle_startup` → `set_startup_enabled`) | Windows itself, at login | Per-user autostart registration. Not a file, but the same "external state this app manages" category — added/removed via `winreg`, no admin rights needed (HKCU, not HKLM). |

## 9. Known limitations and things explicitly not verified

Being direct about this matters more than it might seem — several of
these were discovered by a real user hitting them, not anticipated in
advance:

- **Cost estimates are approximate and will go stale.** `PRICES_PER_MILLION`
  is a snapshot of Anthropic's pricing as understood at build time
  (mid-2026). There is no live pricing source wired in. Whoever
  maintains this should periodically diff it against
  `platform.claude.com/docs/en/about-claude/pricing`.
- **Session/weekly % is entirely dependent on Claude Code's own
  `statusLine` mechanism and the `rate_limits` field within it**, which
  is a relatively recent addition. Older Claude Code installs simply
  never send that field — there is no local fallback or workaround;
  the fix is "update Claude Code," not "fix this script."
- **The PyInstaller `--noconsole`/windowed `.exe` build (used for the
  tray icon so it doesn't pop a console window) cannot be reused for the
  `--statusline-hook` invocation.** That build mode detaches
  stdin/stdout, which breaks a tool that needs to read JSON from stdin
  and print text back. The two modes of this one script have genuinely
  different process requirements — keep using plain `python
  claude_usage_tray.py --statusline-hook` for the hook, regardless of
  whether a standalone `.exe` exists for the tray icon.
- **The taskbar-tray-rect lookup (`find_tray_notification_rect`) was
  never run against a real Windows taskbar during development** — only
  designed defensively and confirmed to fall back gracefully off-Windows.
  If someone reports the first-launch widget position looking wrong on
  real hardware, this is the first place to look, and reproducing it
  will require an actual Windows machine (the dev sandbox can't help
  here).
- **Only the primary taskbar / first monitor's notification area is
  considered.** Multi-monitor setups with the taskbar on a non-primary
  display, or a relocated taskbar (top/side of screen instead of
  bottom), aren't specially handled. Low-stakes because it's a
  first-launch-only heuristic (see above), but worth knowing if someone
  reports it.
- **`rate_limits` is typically empty on the very first statusline render
  of a new/cleared session** — populates after the first completed
  response, not before. Don't mistake this for a bug during support
  conversations.
- **The "Run on Windows startup" registry toggle's read/write/delete
  logic is unit-tested against a disposable registry subkey (never the
  real `...\CurrentVersion\Run`), and confirmed to actually round-trip on
  real Windows via `--test`** — but the end-to-end behavior (does the app
  really appear in the tray after a full logout/login cycle) has not been
  manually confirmed. If someone reports the app not launching at login
  after enabling the toggle, check Task Manager's Startup tab and
  `regedit` for the `ClaudeUsageTray` value first. Also note: if the
  `.exe`/script is moved, renamed, or rebuilt to a new path after the
  toggle was enabled, the registry entry still points at the old
  location — this isn't auto-repaired, by design (see README).

## 10. Operational gotchas discovered during real setup (worth keeping as institutional memory)

These came directly out of debugging a real installation, not
speculation — useful if you're ever helping someone set this up or
writing support docs:

- **PATH doesn't propagate to already-open terminals.** After installing
  Python (even via `winget`), a terminal opened *before* the install
  won't see `python`/`pip` until it's closed and reopened. "Run `python
  --version` on its own" is the fastest way to isolate this from an
  actual script problem.
- **A single stray trailing comma or `//` comment anywhere in
  `~/.claude/settings.json` breaks the *entire* file's parse**, not just
  the section near the syntax error — and Claude Code appears to
  silently ignore a settings file it can't parse, rather than erroring
  loudly. `Get-Content settings.json | ConvertFrom-Json` in PowerShell
  is a fast, strict way to validate before assuming the problem is
  somewhere else.
- **claude.ai (the web/desktop chat product) and the local Claude Code
  CLI are completely separate products.** Nothing typed in a claude.ai
  conversation ever touches a local `~/.claude` directory or triggers a
  local `statusLine` hook — only an actual local `claude` CLI session
  does that. This was a genuine point of confusion worth heading off
  explicitly in any user-facing docs or support response.
- **Isolate "my command is broken" from "Claude Code isn't calling my
  command" early.** Test the exact hook invocation manually first:
  ```
  echo '{"model":{"display_name":"test"},"rate_limits":{"five_hour":{"used_percentage":50,"resets_at":1782500000}}}' | python claude_usage_tray.py --statusline-hook
  ```
  If that doesn't print `test | 5h 50%`, the problem is the script/PATH,
  not Claude Code's wiring. If it does, but the real cache file never
  updates after a real Claude Code turn, swap in a trivial `cmd /c echo
  hello-world` as the `statusLine.command` temporarily to confirm Claude
  Code invokes *any* custom command at all before debugging further.

## 11. Suggested repo layout (this is a suggestion, not a mandate)

Given this is moving from "download these files" into a real,
version-controlled repo, a reasonable starting layout:

```
claude-usage-tray/
├── CLAUDE.md                 # short index, loaded automatically by Claude Code
├── ARCHITECTURE.md           # this file
├── README.md                 # user-facing install/usage instructions
├── claude_usage_tray.py       # the entire app (still single-file, deliberately)
├── requirements.txt
├── build_exe.bat
└── .gitignore                 # dist/, build/, *.spec, __pycache__/, .venv/
```

Whether to eventually split `claude_usage_tray.py` into a real package
(`tracker.py`, `statusline.py`, `widget.py`, `tray.py`, `__main__.py`) is
a legitimate question once this grows further, but it's a conscious
tradeoff against the "one file, easy to grab and run" property that was
explicitly requested earlier in this project's life — raise it as an
explicit decision with whoever's driving the project, don't just do it
unilaterally because it "looks more like a normal Python package."

## 12. Possible future directions (ideas, not commitments)

Recorded here so they're not lost, not because they're decided:

- A proper CI pipeline that containerizes the Xvfb+pystray-stub
  verification approach from §6, so widget layout changes get an
  automated visual regression check instead of relying on a one-off dev
  script.
- A live or periodically-fetched pricing source instead of the static
  `PRICES_PER_MILLION` table, if Anthropic ever exposes one
  programmatically.
- Multi-monitor-aware taskbar detection (enumerate all taskbars, not
  just the primary one) if someone reports the first-launch placement
  landing on the wrong screen.
- Native taskbar-button decorations (overlay badge icon, colored
  progress-bar fill via `ITaskbarList3`) as a complementary option to
  the floating widget — this was explicitly considered and explicitly
  deferred in favor of the floating widget for being more flexible
  (full text vs. icon-only); revisit only if specifically requested.

## 13. Style conventions to preserve when extending this code

- **Pure, testable logic lives at module level; GUI-only code lives as
  closures nested inside `run_app()`.** `price_for_model`,
  `cost_for_record`, `fmt_tokens`, `fmt_cost`, `fmt_reset_relative`,
  `fmt_reset_clock`, `fmt_reset_full`, `pct_tag`,
  `process_statusline_payload`, `write_usage_cache`, `read_usage_cache`,
  and `find_tray_notification_rect` are all module-level and exercised
  directly by `--test`. `build_title`, `menu_items`, the `on_*`
  handlers, `FloatingWidget`, `SessionFileHandler`, `watcher_loop`, and
  `fallback_sweep_loop` are nested inside `run_app()` specifically so
  `--test` never has to import a GUI dependency to exercise the logic
  that actually matters. Keep new logic on the correct side of this
  line.
- **All file I/O fails soft.** Reads/writes to the cache files and
  session logs are wrapped in `try/except OSError` (or
  `json.JSONDecodeError`/`ValueError` for parsing) and return `None` or
  do nothing on failure, rather than raising and killing a background
  thread. Don't introduce an unguarded file operation into a
  long-running loop.
- **Cache writes are atomic**: write to `<path>.tmp`, then
  `os.replace(tmp, path)`. This avoids a reader (the watcher-triggered
  re-render, for instance) ever seeing a half-written JSON file. Follow
  this pattern for any new cache file.
- **Never assume Unicode glyph metrics are uniform across fonts/platforms
  for anything that needs to fit precisely** (see §6's bug postmortem).
  If a visual needs proportional accuracy, draw it (Canvas), don't
  approximate it with text characters.
- **Render real content before computing a window's size from
  `winfo_reqwidth()`/`winfo_reqheight()`.** This was the other half of
  the same bug. `update_idletasks()` after rendering, before reading
  size.
