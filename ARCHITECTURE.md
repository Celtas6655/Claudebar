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

**Staleness on read (since 1.2.0)**: the cache is only refreshed when
Claude Code renders a statusline, so it can be arbitrarily old. All
display paths (widget bars, tray tooltip, tray menu) go through the pure
`effective_rate_limits()` helper, which (a) type-coerces every value —
a corrupted cache degrades to "unavailable", never a crash — and
(b) nulls a window's percentage once its `resets_at` has passed: the old
% is definitionally wrong after the window rolls over, and real numbers
only arrive on the next Claude Code turn. The menu distinguishes that
case ("awaiting next Claude Code turn") from "hook never ran".

**Practical consequence learned the hard way during setup**: the
`rate_limits` field is typically empty on the very first statusline
render of a brand-new or just-cleared session — it only populates after
the *first fully completed response*. Don't read "nothing showed up
after one message" as a bug; send a second message before concluding
anything is broken.

### 2c. Current working state (the Red/Amber/Green indicator)

**Source**: a *third* mechanism, separate again from the two above.
Neither the JSONL logs nor the statusLine cache carries Claude Code's
**live per-turn lifecycle** ("just started" / "running a tool" /
"finished" / "waiting on you"). The only thing that exposes it is Claude
Code's **event hooks** system: `~/.claude/settings.json` can register a
command against lifecycle events, and Claude Code invokes that command
(JSON payload on stdin, `hook_event_name` field naming the event) as each
event fires. This is a distinct pathway from `statusLine` — statusLine is
for the visible status text and only fires per render; hooks are for
lifecycle notifications.

**How we use it**: `claude_usage_tray.py --state-hook` is a third entry
point in the same script, registered (auto, on startup) against the
events in `STATE_HOOK_EVENTS`. The pure function `derive_working_state()`
maps the incoming `hook_event_name` to one of three states:

| `hook_event_name` | state | colour |
|---|---|---|
| `UserPromptSubmit`, `PreToolUse` (and `PostToolUse`, see below) | `working` | amber |
| `Stop` | `done` | green |
| `Notification` (permission / needs-you) | `waiting` | red |
| `SessionEnd` | *(removes the session from the cache)* | — |
| anything else / missing field | *(None — leave last state)* | — |

Returning `None` for an unrecognised or absent event means the hook
**doesn't overwrite** the cache, so an older Claude Code that omits
`hook_event_name`, or a future event we don't handle, simply leaves the
last known state in place rather than blanking it.

`PostToolUse` is deliberately **no longer registered** (since 1.2.0):
`PreToolUse` alone keeps "working" fresh — every tool start refreshes the
timestamp, and `Stop` ends the turn — and each registered event costs one
process spawn per occurrence (see §9 on hook-spawn latency), so dropping
it halves the per-tool overhead. The mapping keeps `PostToolUse → working`
so an entry installed by an older version still behaves; `install_state_hooks()`
actively removes *our own* command from `REMOVED_STATE_HOOK_EVENTS`
(currently just `PostToolUse`) on its next run, never the user's.

**Per-session cache shape (since 1.2.0)**: the cache is no longer a single
last-writer-wins slot — with two concurrent Claude Code sessions, a `Stop`
from one used to turn the dot green while the other was mid-task. The pure
function `update_state_cache()` maintains one `{state, updated_at}` entry
per `session_id` under a `"sessions"` map (pruning entries older than
`STATE_STALE_SECONDS` on every write; `SessionEnd` deletes its session), and
`aggregate_working_state()` reduces the map with priority
**waiting > working > done** — red means "needs you" and must win over
everything. The top-level `state`/`updated_at` keys are still written (the
write-time aggregate) for compatibility, and `aggregate_working_state()`
falls back to them when reading a cache written by an older build.

**Staleness**: the reader (`working_state_tag()`) treats any cached state
older than `STATE_STALE_SECONDS` (5 min) as unknown → a **dim** dot. This
guards against a stuck "working" if a `Stop`/`Notification` is ever missed
or the app is started mid-turn — there's no "turn ended" guarantee we can
rely on, so we time it out.

**Installation & non-clobber guarantee**: `install_state_hooks()` merges
our `--state-hook` command into `settings["hooks"][<event>]` for each
event, modelled on `install_statusline_hook()` (strict-parse guard, atomic
temp-file + `os.replace`). It is **idempotent** (skips events where our
exact command is already registered) and **non-destructive** (only ever
*appends* our own matcher group; never removes or edits the user's own
hooks). It writes only when something actually changed, so a normal
startup doesn't churn `settings.json`. Auto-installed on every launch via
`ensure_state_hooks_installed(..., force=False)`, exactly like the
statusLine hook — `--install-state-hooks` is the force escape hatch.

**Rendering**: the widget draws a small `Canvas` **oval** (RAG colour) plus
a short label ("working" / "waiting for you" / "done") on its top row —
drawn, never a Unicode `●` glyph (ARCHITECTURE.md §6). The tray icon adds a
matching RAG **pip** in the gauge's corner via a parameterised
`make_icon_image(state_tag)`, reassigned to `icon.icon` whenever the state
file changes (watched by the existing non-recursive `CLAUDE_HOME`
observer) and on the fallback sweep. The widget self-polls the state cache
in its 1-second `_tick`, consistent with §5's model.

**Notify-on-waiting toast**: when the aggregate tag *transitions into* red
(Claude needs input), the app shows a native Windows balloon/toast via
pystray's `icon.notify()` (Shell_NotifyIcon under the hood — no new
dependency). The transition detection is the pure module-level
`should_notify_waiting(prev_tag, new_tag)`: it operates on
`working_state_tag()` outputs, so staleness is already folded in ("dim" =
not-waiting), staying red never re-fires, and leaving red re-arms it. The
call site is `apply_icon_state()` — the one place that already dedupes tag
changes on the watcher/sweep threads — and because `_icon_state["tag"]` is
seeded at startup, launching the app while a session is already red stays
quiet; only a live transition notifies. A tray-menu toggle ("Notify when
Claude needs input", default on) persists to the `usage_tray_prefs.json`
sidecar via `read_notify_pref()`/`write_notify_pref()` (fail-soft, atomic,
corrupted file degrades to the default). Whether the balloon visually
renders on real Windows has NOT been field-verified (pystray's win32
backend is expected to work; the dev environment can't display it).

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

- **One instance only.** `run_app()` first calls
  `acquire_single_instance_lock()` (a named Win32 mutex via ctypes) and
  returns quietly when another instance holds it — two instances would
  fight over TOPMOST every second and race on the sidecar files. Only an
  explicit `False` stops startup; `None` (non-Windows, or any mutex API
  failure) never locks the user out. Verified on real Windows: a second
  launch of the frozen exe exits immediately.
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
statusline hook). `SessionFileHandler` must keep handling **`on_moved`
(via `event.dest_path`)** alongside modified/created: our caches are
written atomically (temp + `os.replace`), and on Windows that replace
surfaces as a single "moved" event — no modified/created ever fires on
the target path (verified empirically on real Windows, 2026-07-04; a
regression guard in `--test` pins it). Before this was handled, every
cache-driven update (tray pip recolor, the notify-on-waiting toast) was
silently deferred to the 30-second fallback sweep. Both retry on a 5-second loop until their target
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

### Bug 3 — fatal crash on tray-menu click (WinEvent hook calling into Tk)

Found on real Windows, 2026-07-04: opening the tray menu killed the whole
app with `Fatal Python error: PyEval_RestoreThread: ... the current Python
thread state is NULL`. The z-order recovery `SetWinEventHook`
(`EVENT_SYSTEM_FOREGROUND`, installed in `_apply_overlay_styles`) had its
ctypes callback call `_reassert_topmost()` → `root.attributes("-topmost",
...)` directly. A `WINEVENT_OUTOFCONTEXT` hook callback is dispatched on
the installing (Tk) thread, but **re-entrantly, from inside
`Tcl_DoOneEvent`'s Win32 message pump** — a window where `_tkinter` has
released the GIL and saved its thread state. Calling back into Tcl from
that context corrupts `_tkinter`'s thread-state bookkeeping; the next
Tcl→Python transition restores a NULL thread state and aborts the
process. The tray menu triggered it reliably because the menu popup takes
foreground, firing the event.

The rule this adds to §5's cross-thread pattern: **a ctypes callback must
never call into Tk/Tcl, even when it technically runs on the Tk thread** —
"on the Tk thread" is not the same as "at a safe point in the Tk event
loop". The fix is the same flag pattern as everything else:
`_on_foreground_change` only sets a plain `self._topmost_dirty` attribute
(no Tcl involved), and the existing 150 ms `_fast_tick` `after()` loop
consumes it and calls `_reassert_topmost()` from a safe point. Worst-case
z-order recovery latency went from ~0 to 150 ms, imperceptible in
practice. A source-inspection regression guard in `--test` pins the
callback body to the flag-only shape.

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
| `~/.claude/usage_tray_state_cache.json` | `--state-hook` mode | tray icon (RAG pip), floating widget (RAG dot), tray menu/tooltip | Claude Code's current working state (`working`/`waiting`/`done`) + `updated_at`. Coloured Red/Amber/Green; treated as unknown (dim) past `STATE_STALE_SECONDS`. Atomic write. See §2c. |
| `~/.claude/usage_tray_state_hooks_installed.json` | `write_state_hooks_marker` (via `ensure_state_hooks_installed`) | tray menu indicator; avoid redundant writes | Marker recording that *we* merged the `--state-hook` entries into `settings.json`'s `hooks` array, and the command we wrote. Separate sidecar, NOT a key in `settings.json`. Same atomic-write shape. |
| `~/.claude/bin/ClaudeUsageTrayHook.exe` | `install_hook_exe()` (extracted from the frozen main exe's bundle at startup) | Claude Code itself (spawned as the statusLine / state-hook command) | Slim GUI-free build of the same script, so per-turn hook spawns don't pay the full onefile extraction (§9). Content-compared before replacing; replace failure while a hook is running keeps the old copy (fail-soft). Absent when running from source. |
| `~/.claude/usage_tray_prefs.json` | tray menu's "Notify when Claude needs input" toggle (`on_toggle_notify` → `write_notify_pref`) | `read_notify_pref()` (menu `checked=` state and the notify decision in `apply_icon_state`) | Small user-preferences sidecar; currently just `notify_on_waiting` (bool, default true). Toggle writes preserve unknown keys. Same atomic-write, fail-soft shape. |
| `~/.claude/usage_tray_widget_pos.json` | `FloatingWidget._end_drag()` | `FloatingWidget._default_position()` | Remembers where the user dragged the widget, so the taskbar-detection heuristic only applies once, ever. |
| `~/.claude/usage_tray_widget_favorite_pos.json` | tray menu's "Save current position as favorite" (`on_save_favorite`, via `widget.last_known_pos`) | `FloatingWidget._apply_favorite_position()`; "Load favorite position" menu item's `enabled=` check | User-designated single favorite screen position, independent of the last-dragged position (`usage_tray_widget_pos.json`). Same atomic-write shape. |
| `~/.claude/settings.json` | the user, or this app's auto-install (`ensure_hook_installed` / `ensure_state_hooks_installed`), or `--install-hook` / `--install-state-hooks` (force) | Claude Code itself | Two things the app manages here: `statusLine.command` (rate-limit data, §2b) and `hooks.<event>[]` entries for `--state-hook` (working state, §2c). On startup it adds its `statusLine` when absent and its `hooks` entries when missing; it only ever **appends** its own hook groups and never removes/edits the user's own `statusLine` or `hooks` (unless the corresponding `--install-*` force flag is used). Writes nothing else — a stray key would break the file's strict parse, §10. |
| `~/.claude/usage_tray_hook_installed.json` | `write_hook_marker` (via `ensure_hook_installed`) | tray menu indicator; `ensure_hook_installed` (avoid redundant writes) | Marker recording that *we* installed the statusLine hook, and the command we wrote. Deliberately a separate sidecar, NOT a key in `settings.json`. Same atomic-write shape. |
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
- **The single windowed `.exe` handles every mode, including the hook —
  but the hook path reads/writes raw file descriptors, not `sys.*`.** A
  PyInstaller `--noconsole` (GUI-subsystem) build sets
  `sys.stdin`/`sys.stdout` to `None` (no console), so the naive
  `json.load(sys.stdin)` / `print()` would silently produce nothing. The
  fix (see `_read_hook_stdin`/`_write_hook_stdout`) is to fall back to
  `os.read(0, ...)` / `os.write(1, ...)`: when Claude Code invokes the
  statusLine command it *redirects* stdin/stdout via pipes, so fds 0 and 1
  stay valid regardless of subsystem. **Verified on real Windows**: the
  built exe run as `dist\ClaudeUsageTray.exe --statusline-hook` with
  cmd/Node-style handle redirection prints the status line and writes the
  cache correctly. One caveat that bit during testing — a **PowerShell
  pipe** (`... | exe`) does *not* capture a GUI-subsystem exe's stdout, so
  test the hook with `cmd` file redirection (`exe < in.txt > out.txt`), not
  `|`. This is also why the CI smoke-test step uses `shell: cmd`.
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
- **Hook spawns cost real wall-clock time, dominated by PyInstaller
  onefile self-extraction.** Measured on real Windows hardware
  (2026-07-03): the full 25 MB main exe takes ~1350 ms per hook
  invocation; the slim 7 MB `ClaudeUsageTrayHook.exe` (GUI packages
  excluded) takes ~540–610 ms. That's why the slim helper exists, why it —
  not the main exe — gets registered as the hook command on frozen
  installs, and why `PostToolUse` is no longer registered (§2c). Running
  from source (`python … --statusline-hook`) is faster still. If hook
  latency ever needs to go lower, the next step is a `--onedir`-style
  layout for the helper (no per-launch extraction at all).
- **The whole JSONL history is re-read on every app startup**, and
  `seen_uuids`/`file_pos` grow in memory with the total number of
  assistant turns ever logged. Fine at current scale (incremental reads
  make *steady-state* updates cheap); the known fix, if startup ever gets
  slow on a huge history, is persisting the aggregates + per-file offsets
  to a snapshot sidecar and only re-verifying changed files. Deliberately
  not built yet — it adds real invalidation/corruption edge cases for a
  problem that hasn't materialised.
- **`settings.json` updates are read-modify-write with no cross-process
  lock.** If Claude Code (or anything else) writes settings.json in the
  same instant as our installer, one side's change is lost. Probability is
  low — we only write when something actually changed, typically once per
  install/upgrade — and our write is atomic (temp + `os.replace`), so the
  file is never *corrupted*, only potentially missing one side's edit
  until the next startup re-applies ours. Accepted; a robust fix needs
  file locking across independent programs.
- **The released exe is not code-signed** (Authenticode needs a paid
  certificate). Releases ship a `SHA256SUMS.txt` instead, and users should
  expect a SmartScreen prompt. Signing is the obvious upgrade if this ever
  distributes more widely (§12).
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
- Authenticode code-signing for the released exe (needs a paid
  certificate; releases currently ship SHA256 checksums instead).
- A persisted aggregates+offsets snapshot so startup doesn't re-read the
  whole JSONL history (see §9 — deliberately deferred until it hurts).

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
