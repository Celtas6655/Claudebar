# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@ARCHITECTURE.md

## What this is

A Windows tray app + floating widget showing live Claude Code token
usage, estimated cost, and session (5h)/weekly (7d) rate-limit % with
reset times, plus a Red/Amber/Green indicator of Claude Code's current
working state. Single-file Python app: `claude_usage_tray.py`, run with
no args for the app, `--test` for the test suite, `--statusline-hook` as
the entry point wired into Claude Code's `statusLine` config, and
`--state-hook` as the entry point wired into Claude Code's `hooks` events
(for the working-state indicator).

Full history — why it's built this way, two postmortem bug writeups,
the threading model, what's verified vs. not, setup gotchas — is in
`ARCHITECTURE.md` (imported above). Read it before any architectural
change; don't re-derive decisions that are already explained there.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the tray app
python claude_usage_tray.py

# Run the test suite (no GUI, no real ~/.claude access)
python claude_usage_tray.py --test

# Run as Claude Code's statusLine hook (reads stdin JSON, writes cache, prints status line)
python claude_usage_tray.py --statusline-hook

# Test the hook manually
echo '{"model":{"display_name":"test"},"rate_limits":{"five_hour":{"used_percentage":50,"resets_at":1782500000}}}' | python claude_usage_tray.py --statusline-hook

# Run as a Claude Code event hook (reads stdin JSON, caches Claude's working state)
python claude_usage_tray.py --state-hook

# Test the state hook manually (writes usage_tray_state_cache.json)
echo '{"hook_event_name":"PreToolUse","session_id":"s1"}' | python claude_usage_tray.py --state-hook
```

There is no linter configured. There is no build step beyond `pip install -r requirements.txt`; the optional `build_exe.bat` (not in this repo) would produce a PyInstaller `.exe` for the tray icon only.

## The one fact that matters most

The app surfaces **three unrelated data sources**, none substitutable for
another:
1. Token/cost/history — local JSONL logs (`~/.claude/projects/`).
2. Session/weekly rate-limit % — `~/.claude/usage_tray_cache.json`, written
   by `--statusline-hook` from data Claude Code pipes in. Rate-limit % is
   account-level server state, **not** derivable locally from token counts.
3. Current working state (RAG indicator) — `~/.claude/usage_tray_state_cache.json`,
   written by `--state-hook` from Claude Code's `hooks` events
   (Stop/Notification/UserPromptSubmit/PreToolUse). Live per-turn lifecycle
   state, exposed **only** through the hooks system — the statusLine payload
   does not carry it.

See ARCHITECTURE.md §2.

## Non-negotiable constraints (regressions to avoid)

- `--test` must run with **zero GUI dependencies** (no `pystray`,
  `Pillow`, or `tkinter` imports at module level — only inside
  `run_app()`). It must also never touch the real `~/.claude` directory;
  always use temp dirs / explicit path params in tests.
- The single PyInstaller `--noconsole` `.exe` handles every mode,
  **including `--statusline-hook`** — but only because the hook path
  reads/writes fds 0/1 directly (`_read_hook_stdin`/`_write_hook_stdout`),
  since a windowed build has `sys.stdin`/`sys.stdout == None`. Don't
  "simplify" the hook back to `json.load(sys.stdin)`/`print()` — that
  silently breaks the frozen exe. Test the hook with `cmd` redirection
  (`exe < in.txt > out.txt`), never a PowerShell pipe (which doesn't
  capture a GUI-subsystem exe's stdout).
- No cross-thread Tkinter calls. Use a `threading.Event` set elsewhere
  and polled from inside the Tk thread's own `after()` loop — see
  ARCHITECTURE.md §5 for the existing pattern (`widget_visible`,
  `should_quit`).
- Never build a precisely-sized visual element (a progress bar, an
  aligned column) out of Unicode characters assuming uniform glyph
  width across fonts/platforms — this caused a real clipping bug once
  already (ARCHITECTURE.md §6). Draw it instead (Tkinter `Canvas`).
- Render real content into a Tk window **before** sizing it from
  `winfo_reqwidth()`/`reqheight()`, not before.

## Conventions

- Pure/testable logic at module level; GUI-only code as closures inside
  `run_app()`. Keep new code on the correct side of that line.
- All file I/O fails soft (`try/except OSError`, return `None`/no-op).
  Cache writes are atomic (temp file + `os.replace`).
- Cost figures (`PRICES_PER_MILLION`) are a static, approximate table
  near the top of the file — flag to the user if asked, and periodically
  check `platform.claude.com/docs/en/about-claude/pricing` if
  maintaining this long-term.

## Style / communication preferences for this project

- Be direct about what's verified vs. not. Several bugs and limitations
  in this project were found by a real user on real Windows hardware
  that the dev environment (Linux, no display) couldn't reproduce —
  when in doubt, say so explicitly rather than implying something was
  tested when it wasn't.
- Prefer fixing root causes over patching symptoms (see the two bug
  postmortems in ARCHITECTURE.md §6 for the standard this project holds
  itself to).
