"""
claudebar.py — single-file Windows tray app showing live Claude Code
token usage and estimated cost, read straight from Claude Code's local
session logs.

Usage:
    python claudebar.py                  # run the tray app
    python claudebar.py --test           # run the built-in test suite
    python claudebar.py --statusline-hook  # used internally as Claude
                                                    # Code's `statusLine` command
                                                    # (see README.md)

No API key needed, no network access — everything is read from
~/.claude/projects (on Windows: %USERPROFILE%\\.claude\\projects), plus
(for session/weekly limit %) a small local cache file written by the
statusLine hook described in README.md. Only model names, token counts,
and rate-limit percentages are read; conversation content is never
touched.

Claude Code writes one JSONL session-transcript file per session. Each
assistant turn looks roughly like:
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

Updates are event-driven: a filesystem watcher reacts the moment a session
file changes (typically well under a second), rather than checking on a
fixed timer. A slower periodic sweep runs in the background purely as a
safety net.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------
# Data layer: find/read Claude Code's session logs and aggregate usage.
# Pure stdlib only — no GUI dependencies here, so --test works anywhere,
# including machines/sandboxes without a tray backend.
# --------------------------------------------------------------------------

CLAUDE_HOME = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
PROJECTS_DIR = os.path.join(CLAUDE_HOME, "projects")

# Where the --statusline-hook mode caches the session/weekly rate-limit
# numbers it receives from Claude Code, for the tray app to read.
USAGE_CACHE_PATH = os.path.join(CLAUDE_HOME, "claudebar_cache.json")

# Our own marker recording that we've wired the statusLine hook into Claude
# Code's settings.json (see ensure_hook_installed). Deliberately a separate
# sidecar, NOT a key inside settings.json -- that file's parse is strict and
# fragile (one stray key/comment breaks all of it), so we never write anything
# but the `statusLine` entry into it.
HOOK_INSTALLED_MARKER_PATH = os.path.join(CLAUDE_HOME, "claudebar_hook_installed.json")

# Where the --state-hook mode caches Claude Code's *current working state*
# (working / waiting-on-you / done), for the widget and tray icon to colour a
# Red/Amber/Green indicator from. This is a THIRD data source, unrelated to the
# token logs and the statusLine rate-limit cache: it comes from Claude Code's
# event-hooks system (Stop/Notification/UserPromptSubmit/PreToolUse), the only
# mechanism that exposes live per-turn lifecycle state. See ARCHITECTURE.md §2.
STATE_CACHE_PATH = os.path.join(CLAUDE_HOME, "claudebar_state_cache.json")

# Marker recording that we've merged our --state-hook entries into settings.json's
# `hooks` array (see ensure_state_hooks_installed). Separate sidecar for the same
# reason as HOOK_INSTALLED_MARKER_PATH above.
STATE_HOOKS_MARKER_PATH = os.path.join(CLAUDE_HOME, "claudebar_state_hooks_installed.json")

# A cached working state older than this (seconds) is treated as unknown (dim),
# so a missed Stop hook or an app started mid-turn can't leave "working" stuck on.
STATE_STALE_SECONDS = 300

# Small user-preferences sidecar (currently just the "notify when Claude needs
# input" toggle). Same fail-soft atomic-write shape as the other sidecars; a
# missing or corrupted file degrades to defaults, never a crash.
PREFS_PATH = os.path.join(CLAUDE_HOME, "claudebar_prefs.json")

# Claude Code hook events we register --state-hook against, and the working state
# each maps to. Order/keys mirror derive_working_state()'s logic; used both to
# install the hooks and to reason about them. Notification == "Claude needs you".
# PostToolUse is deliberately NOT registered: PreToolUse alone keeps "working"
# fresh (every tool start refreshes it, Stop ends the turn), and each registered
# event costs one process spawn per occurrence — on a frozen onefile build that
# is a full self-extraction, so halving the per-tool spawns matters.
STATE_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "Stop", "Notification", "SessionEnd")

# Events we used to register but no longer do. install_state_hooks() removes
# OUR OWN command from these on its next run (never the user's own hooks).
REMOVED_STATE_HOOK_EVENTS = ("PostToolUse",)

# App version -- single source of truth, mirrored by the VERSION file at the
# repo root that the release workflow reads to tag the build.
__version__ = "1.2.0"

# Approximate USD price per 1M tokens: (input, output, cache_write, cache_read)
# Anthropic's published per-model rates as of PRICES_AS_OF -- treat as a rough
# estimate and check https://platform.claude.com/docs/en/about-claude/pricing
# if you need exact numbers; rates do change over time.
PRICES_AS_OF = "mid-2026"
PRICES_PER_MILLION = {
    "opus":   (5.00, 25.00, 6.25, 0.50),
    "sonnet": (3.00, 15.00, 3.75, 0.30),
    "haiku":  (1.00, 5.00, 1.25, 0.10),
}
DEFAULT_PRICE = PRICES_PER_MILLION["sonnet"]

USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _as_number(value):
    """value as a float when it's a real number (bools excluded), else None.
    Used to sanitise externally-written cache/payload fields before they reach
    formatting or comparison code — a corrupted cache file must degrade to
    "unavailable", never crash a render loop."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def record_local_date(ts):
    """Local calendar date for an ISO-8601 timestamp string, or None.

    Claude Code's JSONL timestamps are UTC ("2026-06-26T10:15:00.000Z");
    comparing their date *prefix* against the local date misattributes
    anything logged between local midnight and UTC midnight. Parse and
    convert to the local zone before taking the date."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date()


def price_for_model(model_name):
    name = (model_name or "").lower()
    for key, prices in PRICES_PER_MILLION.items():
        if key in name:
            return prices
    return DEFAULT_PRICE


def cost_for_record(record):
    in_p, out_p, cw_p, cr_p = price_for_model(record["model"])
    return (
        record["input_tokens"] / 1_000_000 * in_p
        + record["output_tokens"] / 1_000_000 * out_p
        + record["cache_creation_input_tokens"] / 1_000_000 * cw_p
        + record["cache_read_input_tokens"] / 1_000_000 * cr_p
    )


def find_session_files(projects_dir):
    files = []
    if not os.path.isdir(projects_dir):
        return files
    for root, _dirs, filenames in os.walk(projects_dir):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                files.append(os.path.join(root, fn))
    return files


def project_name_from_path(filepath, projects_dir):
    rel = os.path.relpath(filepath, projects_dir)
    parts = rel.split(os.sep)
    return parts[0] if parts else "unknown"


class UsageTracker:
    """Running totals across all sessions, updated incrementally: each
    poll only reads bytes appended since the last call, so it stays cheap
    even with a large session history."""

    def __init__(self, projects_dir=None):
        self.projects_dir = projects_dir or PROJECTS_DIR
        self.file_pos = {}          # filepath -> byte offset already read
        self.seen_uuids = set()
        self.totals = defaultdict(int)
        self.today_totals = defaultdict(int)
        self.by_model = defaultdict(lambda: defaultdict(int))
        self.by_project = defaultdict(lambda: defaultdict(int))
        self.cost_total = 0.0
        self.cost_today = 0.0
        self.sessions = set()
        self._today = date.today()

    def _maybe_roll_day(self):
        today = date.today()
        if today != self._today:
            self._today = today
            self.today_totals = defaultdict(int)
            self.cost_today = 0.0

    def _record_from_line(self, line, project):
        line = line.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return None
        if obj.get("type") != "assistant":
            return None
        message = obj.get("message") or {}
        usage = message.get("usage")
        if not usage:
            return None
        uuid = obj.get("uuid")
        if uuid:
            if uuid in self.seen_uuids:
                return None
            self.seen_uuids.add(uuid)
        return {
            "model": message.get("model", "unknown"),
            "timestamp": obj.get("timestamp") or "",
            "project": project,
            "session_id": obj.get("sessionId"),
            "input_tokens": usage.get("input_tokens", 0) or 0,
            "output_tokens": usage.get("output_tokens", 0) or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        }

    def _apply(self, record):
        session_id = record.get("session_id")
        if session_id:
            self.sessions.add(session_id)
        for key in USAGE_KEYS:
            self.totals[key] += record[key]
            self.by_model[record["model"]][key] += record[key]
            self.by_project[record["project"]][key] += record[key]
        cost = cost_for_record(record)
        self.cost_total += cost
        if record_local_date(record["timestamp"]) == self._today:
            for key in USAGE_KEYS:
                self.today_totals[key] += record[key]
            self.cost_today += cost

    def poll_file(self, filepath):
        """Read any new lines appended to ONE specific session file.
        Cheap enough to call from a filesystem-change callback."""
        try:
            size = os.path.getsize(filepath)
        except OSError:
            return
        last_pos = self.file_pos.get(filepath, 0)
        if size < last_pos:
            last_pos = 0  # file was rotated/truncated; restart
        if size == last_pos:
            return
        project = project_name_from_path(filepath, self.projects_dir)
        new_records = []
        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(last_pos)
                for line in f:
                    record = self._record_from_line(line, project)
                    if record:
                        new_records.append(record)
                self.file_pos[filepath] = f.tell()
        except OSError:
            return
        for record in new_records:
            self._apply(record)

    def poll(self, files=None):
        """Full sweep: read any new lines appended to ANY session file.

        `files` lets a caller pre-scan the tree with find_session_files()
        *outside* whatever lock guards this tracker — the walk is the slow
        part on a big history; the incremental per-file reads are cheap."""
        self._maybe_roll_day()
        if files is None:
            files = find_session_files(self.projects_dir)
        for filepath in files:
            self.poll_file(filepath)

    def snapshot(self):
        return {
            "totals": dict(self.totals),
            "today_totals": dict(self.today_totals),
            "by_model": {k: dict(v) for k, v in self.by_model.items()},
            "by_project": {k: dict(v) for k, v in self.by_project.items()},
            "cost_total": self.cost_total,
            "cost_today": self.cost_today,
            "session_count": len(self.sessions),
        }


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(c):
    return f"${c:.2f}" if c >= 0.01 else f"${c:.4f}"


def fmt_reset_relative(ts):
    """'2h14m' / '3d4h' style countdown to a unix timestamp, or None."""
    if ts is None:
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    delta = ts - time.time()
    if delta <= 0:
        return "now"
    total_minutes = int(delta // 60)
    days, rem_minutes = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem_minutes, 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_reset_clock(ts):
    """'Thu 18:42' style absolute local time for a unix timestamp, or None."""
    if ts is None:
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts).strftime("%a %H:%M")


def fmt_reset_full(ts):
    rel, clock = fmt_reset_relative(ts), fmt_reset_clock(ts)
    if rel is None or clock is None:
        return "unknown"
    return f"in {rel} ({clock})"


def pct_tag(pct):
    """Color-threshold name for a percentage: green < 50, yellow < 80, else red.
    Non-numeric input (a corrupted cache value) degrades to "dim", never raises."""
    pct = _as_number(pct)
    if pct is None:
        return "dim"
    if pct < 50:
        return "green"
    if pct < 80:
        return "yellow"
    return "red"


def effective_rate_limits(cache, now=None):
    """Sanitised, display-ready view of the statusline rate-limit cache.

    The cache is only refreshed when Claude Code renders a statusline, so it
    can be arbitrarily old. Two rules keep stale data from being shown as
    current:
      - every percentage / timestamp is coerced through _as_number() (a
        corrupted or hand-edited cache degrades to "unavailable", not a crash);
      - once a window's `resets_at` has passed, its percentage is
        definitionally wrong (the window rolled over) — both fields are nulled
        until the next Claude Code turn writes fresh numbers.

    Returns {"model", "context_pct", "session_pct", "session_resets_at",
    "weekly_pct", "weekly_resets_at"} with None for anything unavailable."""
    cache = cache if isinstance(cache, dict) else {}
    now = time.time() if now is None else now
    model = cache.get("model")
    out = {
        "model": model if isinstance(model, str) else None,
        "context_pct": _as_number(cache.get("context_used_percentage")),
    }
    for prefix, pct_key, reset_key in (
        ("session", "session_used_percentage", "session_resets_at"),
        ("weekly", "weekly_used_percentage", "weekly_resets_at"),
    ):
        pct = _as_number(cache.get(pct_key))
        resets_at = _as_number(cache.get(reset_key))
        if resets_at is not None and resets_at <= now:
            pct, resets_at = None, None
        out[prefix + "_pct"] = pct
        out[prefix + "_resets_at"] = resets_at
    return out


# Map an abstract working state to a WIDGET_COLORS key. RAG semantics (locked
# with the user): working -> amber (yellow), waiting-on-you -> red, done ->
# green. Kept as a table so derive_working_state, the widget dot, and the tray
# pip all agree.
STATE_TAGS = {"working": "yellow", "waiting": "red", "done": "green"}
STATE_LABELS = {"working": "working", "waiting": "waiting for you", "done": "done"}


def working_state_tag(state, updated_at=None, now=None):
    """Color-threshold name for a working state, honouring staleness.

    Returns a WIDGET_COLORS key ("yellow"/"red"/"green"/"dim"). An unknown
    state, or one whose ``updated_at`` is older than STATE_STALE_SECONDS,
    degrades to "dim" so a missed Stop hook can't leave the indicator stuck.
    """
    if state not in STATE_TAGS:
        return "dim"
    if updated_at is not None:
        now = now if now is not None else time.time()
        if now - updated_at > STATE_STALE_SECONDS:
            return "dim"
    return STATE_TAGS[state]


def working_state_label(state, updated_at=None, now=None):
    """Short human label for a working state ("working"/"waiting for you"/
    "done"), or a dash when unknown or stale (mirrors working_state_tag)."""
    if working_state_tag(state, updated_at, now) == "dim":
        return "—"
    return STATE_LABELS[state]


def should_notify_waiting(prev_tag, new_tag):
    """Pure: True when the aggregate RAG tag has just *entered* "red".

    Operates on WIDGET_COLORS keys (the working_state_tag output), so
    staleness is already folded in — a stale/unknown state arrives here as
    "dim", i.e. not-waiting. Only the transition fires: staying red never
    re-notifies; leaving red (working/done/dim) re-arms it so the next red
    notifies again."""
    return new_tag == "red" and prev_tag != "red"


# When several Claude Code sessions are live at once, the indicator shows the
# most attention-worthy state across them: red ("needs you") must win over
# amber ("working"), which wins over green ("done").
_STATE_PRIORITY = ("waiting", "working", "done")


def aggregate_working_state(cache, now=None):
    """(state, updated_at) across all live sessions in a state-cache dict.

    The cache's "sessions" map (see update_state_cache) holds one
    {state, updated_at} entry per Claude Code session; entries older than
    STATE_STALE_SECONDS are ignored. Priority across fresh entries is
    waiting > working > done. A cache without a sessions map (written by an
    older build) falls back to its legacy top-level state/updated_at fields.
    Returns (None, None) when nothing fresh is known."""
    now = time.time() if now is None else now
    cache = cache if isinstance(cache, dict) else {}
    sessions = cache.get("sessions")
    if not isinstance(sessions, dict):
        state = cache.get("state")
        ts = _as_number(cache.get("updated_at"))
        if state in STATE_TAGS and (ts is None or now - ts <= STATE_STALE_SECONDS):
            return state, ts
        return None, None
    freshest = {}
    for entry in sessions.values():
        if not isinstance(entry, dict):
            continue
        state = entry.get("state")
        ts = _as_number(entry.get("updated_at"))
        if state not in STATE_TAGS or ts is None or now - ts > STATE_STALE_SECONDS:
            continue
        if state not in freshest or ts > freshest[state]:
            freshest[state] = ts
    for state in _STATE_PRIORITY:
        if state in freshest:
            return state, freshest[state]
    return None, None


def update_state_cache(cache, payload, now=None):
    """Pure: fold one Claude Code hook payload into the state-cache dict.

    Keeps one entry per session_id in cache["sessions"] so concurrent Claude
    Code sessions can't clobber each other's state (a Stop in one terminal no
    longer turns the dot green while another session is mid-task). Stale
    entries are pruned on every write; SessionEnd removes its session outright
    (no live session -> dim, not a lingering green/red). The top-level
    state/updated_at fields are kept as the write-time aggregate for
    compatibility with anything reading the old single-slot shape.

    Returns the new cache dict, or None when the event carries no state
    transition (leave the file untouched, same contract as before)."""
    now = time.time() if now is None else now
    payload = payload if isinstance(payload, dict) else {}
    event = payload.get("hook_event_name")
    state = derive_working_state(payload)
    ended = event == "SessionEnd"
    if state is None and not ended:
        return None

    sessions = {}
    old = (cache if isinstance(cache, dict) else {}).get("sessions")
    if isinstance(old, dict):
        for sid, entry in old.items():
            if not isinstance(entry, dict):
                continue
            ts = _as_number(entry.get("updated_at"))
            if ts is None or now - ts > STATE_STALE_SECONDS:
                continue
            if entry.get("state") in STATE_TAGS:
                sessions[sid] = {"state": entry["state"], "updated_at": ts}

    session_id = payload.get("session_id") or "unknown"
    if ended:
        sessions.pop(session_id, None)
    else:
        sessions[session_id] = {"state": state, "updated_at": now}

    agg_state, agg_ts = aggregate_working_state({"sessions": sessions}, now)
    return {
        "sessions": sessions,
        "state": agg_state,
        "event": event,
        "session_id": payload.get("session_id"),
        "updated_at": agg_ts if agg_ts is not None else now,
    }


# --------------------------------------------------------------------------
# Taskbar alignment: configuration, diagnostics, and tray-rect detection.
#
# All coordinate-returning functions produce Win32 physical pixels.  On
# modern Python 3.x + Windows 10/11 (per-monitor DPI aware), Tkinter's
# geometry() also takes physical pixels, so no unit conversion is needed
# between Win32 rect values and Tk geometry strings.
#
# Uses ctypes (stdlib) only -- pywin32 is no longer required.
# --------------------------------------------------------------------------

class TaskbarAlignmentConfig:
    """Controls how the floating widget is anchored next to the system tray.

    Attributes
    ----------
    enabled : bool
        When True the widget is positioned (and periodically re-positioned)
        left of the tray rather than at the last-dragged location.
        Dragging the widget disables this mode automatically.
    gap_px : int
        Clear space between the widget's right edge and the tray's left edge.
    vertical_mode : str
        "inside-taskbar" — widget is vertically centred on the taskbar.
        "above-taskbar" — widget floats above the taskbar top edge.
    vertical_offset_px : int
        Additional vertical nudge (positive = downward).
    overlap_px : int
        For "above-taskbar" mode: pixels of overlap with the taskbar top.
    fallback_reserved_tray_width_px : int
        When no tray child window can be located, treat this many pixels at
        the taskbar's right end as the tray area (logged loudly when used).
    debug_logging : bool
        Print a full diagnostic tree on startup and log all alignment
        decisions.  Enable via the TRAY_DEBUG=1 environment variable.
    """

    def __init__(self):
        self.enabled = True
        self.gap_px = 8
        self.vertical_mode = "inside-taskbar"
        self.vertical_offset_px = 0
        self.overlap_px = 0
        self.fallback_reserved_tray_width_px = 300
        self.debug_logging = os.environ.get("TRAY_DEBUG") == "1"


def _taskbar_log(msg, config=None):
    """Print msg if config.debug_logging is True (or config is None)."""
    if config is None or config.debug_logging:
        print(f"[taskbar] {msg}", flush=True)


def _w32_class_name(hwnd):
    """GetClassNameW → string.  Returns '' on failure."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ""


def _w32_window_text(hwnd):
    """GetWindowTextW → string.  Returns '' on failure."""
    try:
        import ctypes
        n = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def _w32_get_rect(hwnd):
    """GetWindowRect → (left, top, right, bottom) in physical pixels, or None."""
    try:
        import ctypes
        import ctypes.wintypes
        r = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def _w32_enum_children(parent_hwnd):
    """EnumChildWindows → list of all child HWNDs (recursive), or []."""
    try:
        import ctypes
        import ctypes.wintypes
        children = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
        )
        def _cb(hwnd, _lparam):
            children.append(hwnd)
            return True
        cb = WNDENUMPROC(_cb)
        ctypes.windll.user32.EnumChildWindows(parent_hwnd, cb, 0)
        return children
    except Exception:
        return []


def _w32_find_window(cls, title=None):
    """FindWindowW → HWND (int) or 0."""
    try:
        import ctypes
        return ctypes.windll.user32.FindWindowW(cls, title) or 0
    except Exception:
        return 0


def _zorder_position(hwnd_chain, our_hwnd, taskbar_hwnd):
    """Pure z-order check: is our_hwnd above or below taskbar_hwnd?

    hwnd_chain is an iterable of HWNDs ordered top→bottom (as returned by
    walking GetTopWindow → GetWindow(GW_HWNDNEXT)).

    Returns 'above' if our_hwnd appears before taskbar_hwnd in the chain,
    'below' if it appears after, or 'unknown' if either HWND is absent.

    Kept at module level (not inside FloatingWidget) so it can be unit-tested
    without a GUI or Win32 dependency.
    """
    our_seen = False
    for hwnd in hwnd_chain:
        if hwnd == our_hwnd:
            our_seen = True
        elif hwnd == taskbar_hwnd:
            return "above" if our_seen else "below"
    return "unknown"


def diagnose_taskbar_windows(config=None):
    """Walk the Shell_TrayWnd child tree and log every window's class/text/rect.

    Returns a list of dicts (hwnd, class, text, rect, width, height) for every
    window in the tree.  Always safe to call; returns [] on non-Windows.
    Logging output is suppressed unless config.debug_logging is True.

    Classes looked for specifically:
      TrayNotifyWnd, SysPager, ToolbarWindow32, ReBarWindow32,
      MSTaskSwWClass, Shell_SecondaryTrayWnd, ClockButton,
      TrayShowDesktopButtonWClass
    """
    result = []
    if not sys.platform.startswith("win"):
        return result

    taskbar_hwnd = _w32_find_window("Shell_TrayWnd")
    if not taskbar_hwnd:
        _taskbar_log("diagnose: Shell_TrayWnd not found", config)
        return result

    rect = _w32_get_rect(taskbar_hwnd)
    result.append({
        "hwnd": taskbar_hwnd, "class": "Shell_TrayWnd",
        "text": _w32_window_text(taskbar_hwnd), "rect": rect,
        "width": (rect[2] - rect[0]) if rect else None,
        "height": (rect[3] - rect[1]) if rect else None,
    })
    _taskbar_log(f"Shell_TrayWnd hwnd=0x{taskbar_hwnd:08X}  rect={rect}", config)

    children = _w32_enum_children(taskbar_hwnd)
    _taskbar_log(f"  {len(children)} child window(s) under Shell_TrayWnd:", config)

    INTERESTING = {
        "TrayNotifyWnd", "SysPager", "ToolbarWindow32", "ReBarWindow32",
        "MSTaskSwWClass", "Shell_SecondaryTrayWnd", "ClockButton",
        "TrayShowDesktopButtonWClass",
    }
    for child in children:
        cls = _w32_class_name(child)
        text = _w32_window_text(child)
        r = _w32_get_rect(child)
        result.append({
            "hwnd": child, "class": cls, "text": text, "rect": r,
            "width": (r[2] - r[0]) if r else None,
            "height": (r[3] - r[1]) if r else None,
        })
        if cls in INTERESTING:
            _taskbar_log(
                f"  0x{child:08X}  {cls:<32}  {text!r:<14}  rect={r}", config,
            )
    return result


def find_taskbar_tray_rect(config=None):
    """Locate the system tray notification area in Win32 physical pixels.

    Strategy (each step falls through to the next on failure):
      Taskbar rect:
        1. SHAppBarMessage(ABM_GETTASKBARPOS) — authoritative.
        2. GetWindowRect(Shell_TrayWnd) — fallback.
      Tray rect (the notification / icon area at the right end):
        3. TrayNotifyWnd child of Shell_TrayWnd.
        4. SysPager child (Windows 10 sometimes restructures the tree).
        5. Rightmost plausible child whose rect overlaps the taskbar.
        6. taskbar.right − fallback_reserved_tray_width_px (logged loudly).

    Returns a dict on success:
        {
          "taskbar_rect":  (l, t, r, b),   # whole taskbar, physical px
          "tray_rect":     (l, t, r, b),   # notification area, physical px
          "monitor_rect":  (l, t, r, b) | None,
          "work_area":     (l, t, r, b) | None,
          "tray_hwnd":     int,             # 0 when fallback was used
          "tray_class":    str,
          "fallback_used": bool,
          "dpi":           int,
          "source":        "SHAppBarMessage" | "FindWindow",
        }
    Returns None if Shell_TrayWnd can't be found at all.
    """
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        import ctypes.wintypes
    except ImportError:
        return None

    # 1. Taskbar HWND
    taskbar_hwnd = _w32_find_window("Shell_TrayWnd")
    if not taskbar_hwnd:
        _taskbar_log("Shell_TrayWnd not found", config)
        return None
    _taskbar_log(f"Shell_TrayWnd hwnd=0x{taskbar_hwnd:08X}", config)

    # 2. Taskbar rect — prefer SHAppBarMessage
    taskbar_rect = None
    source = "FindWindow"
    try:
        class _ABD(ctypes.Structure):
            _fields_ = [
                ("cbSize",           ctypes.wintypes.DWORD),
                ("hWnd",             ctypes.wintypes.HWND),
                ("uCallbackMessage", ctypes.wintypes.UINT),
                ("uEdge",            ctypes.wintypes.UINT),
                ("rc",               ctypes.wintypes.RECT),
                ("lParam",           ctypes.wintypes.LPARAM),
            ]
        abd = _ABD()
        abd.cbSize = ctypes.sizeof(_ABD)
        abd.hWnd = taskbar_hwnd
        if ctypes.windll.shell32.SHAppBarMessage(0x00000005, ctypes.byref(abd)):
            r = abd.rc
            taskbar_rect = (r.left, r.top, r.right, r.bottom)
            source = "SHAppBarMessage"
            _taskbar_log(f"Taskbar rect via SHAppBarMessage: {taskbar_rect}", config)
    except Exception as exc:
        _taskbar_log(f"SHAppBarMessage failed ({exc}), using GetWindowRect", config)

    if not taskbar_rect:
        taskbar_rect = _w32_get_rect(taskbar_hwnd)
        _taskbar_log(f"Taskbar rect via GetWindowRect: {taskbar_rect}", config)
    if not taskbar_rect:
        return None

    # 3. DPI for the taskbar window
    dpi = 96
    try:
        dpi = ctypes.windll.user32.GetDpiForWindow(taskbar_hwnd)
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:
            pass
    _taskbar_log(f"DPI={dpi} ({dpi / 96 * 100:.0f}%)", config)

    # 4. Monitor info (best-effort; not fatal if unavailable)
    monitor_rect = work_area = None
    try:
        hmon = ctypes.windll.user32.MonitorFromRect(
            ctypes.byref(ctypes.wintypes.RECT(*taskbar_rect)), 2,  # MONITOR_DEFAULTTONEAREST
        )
        if hmon:
            class _MI(ctypes.Structure):
                _fields_ = [
                    ("cbSize",    ctypes.wintypes.DWORD),
                    ("rcMonitor", ctypes.wintypes.RECT),
                    ("rcWork",    ctypes.wintypes.RECT),
                    ("dwFlags",   ctypes.wintypes.DWORD),
                ]
            mi = _MI()
            mi.cbSize = ctypes.sizeof(_MI)
            ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
            r = mi.rcMonitor
            monitor_rect = (r.left, r.top, r.right, r.bottom)
            r = mi.rcWork
            work_area = (r.left, r.top, r.right, r.bottom)
            _taskbar_log(f"Monitor={monitor_rect}  WorkArea={work_area}", config)
    except Exception as exc:
        _taskbar_log(f"GetMonitorInfo failed: {exc}", config)

    # 5. Find the tray child rect
    children = _w32_enum_children(taskbar_hwnd)
    tray_hwnd, tray_rect, tray_class, fallback_used = 0, None, "", False

    # Priority 1: TrayNotifyWnd — canonical location of the notification area
    for child in children:
        if _w32_class_name(child) == "TrayNotifyWnd":
            r = _w32_get_rect(child)
            if r:
                tray_hwnd, tray_rect, tray_class = child, r, "TrayNotifyWnd"
                _taskbar_log(
                    f"TrayNotifyWnd hwnd=0x{child:08X}  rect={r}", config,
                )
                break

    # Priority 2: SysPager (sometimes the direct host on older Windows builds)
    if not tray_rect:
        for child in children:
            if _w32_class_name(child) == "SysPager":
                r = _w32_get_rect(child)
                if r:
                    tray_hwnd, tray_rect, tray_class = child, r, "SysPager"
                    _taskbar_log(
                        f"SysPager (stand-in) hwnd=0x{child:08X}  rect={r}", config,
                    )
                    break

    # Priority 3: rightmost child that plausibly overlaps the right end of the taskbar
    if not tray_rect:
        tb_right = taskbar_rect[2]
        best_hwnd, best_rect, best_cls, best_right = 0, None, "", -1
        for child in children:
            r = _w32_get_rect(child)
            if not r:
                continue
            if r[2] < tb_right - 600:          # too far left to be the tray
                continue
            if r[3] < taskbar_rect[1] or r[1] > taskbar_rect[3]:  # no vertical overlap
                continue
            if r[2] > best_right:
                best_hwnd, best_rect, best_cls = child, r, _w32_class_name(child)
                best_right = r[2]
        if best_rect:
            tray_hwnd, tray_rect, tray_class = best_hwnd, best_rect, best_cls
            _taskbar_log(
                f"Using rightmost child hwnd=0x{best_hwnd:08X}  "
                f"class={best_cls!r}  rect={best_rect}",
                config,
            )

    # Priority 4: synthetic reserved-width fallback (logged loudly)
    if not tray_rect:
        fallback_used = True
        fw = config.fallback_reserved_tray_width_px if config else 300
        tray_rect = (
            taskbar_rect[2] - fw, taskbar_rect[1],
            taskbar_rect[2],      taskbar_rect[3],
        )
        _taskbar_log(
            f"WARNING: no tray child found — using fallback reserved_width={fw}px  "
            f"synthetic_tray_rect={tray_rect}",
            config,
        )

    _taskbar_log(
        f"Result: source={source!r}  taskbar={taskbar_rect}  tray={tray_rect}  "
        f"tray_class={tray_class!r}  dpi={dpi}  fallback={fallback_used}",
        config,
    )
    return {
        "taskbar_rect": taskbar_rect,
        "tray_rect":    tray_rect,
        "monitor_rect": monitor_rect,
        "work_area":    work_area,
        "tray_hwnd":    tray_hwnd,
        "tray_class":   tray_class,
        "fallback_used": fallback_used,
        "dpi":          dpi,
        "source":       source,
    }


def find_tray_notification_rect():
    """Thin backward-compatible wrapper around find_taskbar_tray_rect().
    Returns {"left", "top", "height"} or None.
    New callers should use find_taskbar_tray_rect() directly."""
    if not sys.platform.startswith("win"):
        return None
    info = find_taskbar_tray_rect()
    if not info:
        return None
    tray = info["tray_rect"]
    tb   = info["taskbar_rect"]
    return {"left": tray[0], "top": tb[1], "height": tb[3] - tb[1]}


def clamp_position(x, y, w, h, bounds):
    """Clamp a w×h window's top-left corner so it stays inside bounds
    (left, top, right, bottom). Pure — used to keep a remembered widget
    position on screen after a monitor is removed or the resolution shrinks
    (an overrideredirect window parked off-screen is otherwise unrecoverable
    without the tray menu)."""
    left, top, right, bottom = bounds
    x = max(left, min(int(x), right - int(w)))
    y = max(top, min(int(y), bottom - int(h)))
    return x, y


def virtual_screen_bounds():
    """(left, top, right, bottom) of the Windows virtual screen — the bounding
    box of ALL monitors — in physical pixels, or None off-Windows / on failure.
    Same lazy-ctypes, fail-soft pattern as the taskbar helpers above."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        left = user32.GetSystemMetrics(76)    # SM_XVIRTUALSCREEN
        top = user32.GetSystemMetrics(77)     # SM_YVIRTUALSCREEN
        width = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
        height = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            return None
        return (left, top, left + width, top + height)
    except Exception:
        return None


# Handle of the single-instance mutex, kept alive for the process lifetime
# (releasing it would let a second instance start).
_single_instance_handle = None


def acquire_single_instance_lock(name="Local\\Claudebar-single-instance"):
    """Try to become the one running instance (named Win32 mutex).

    Returns True when acquired, False when another instance already holds it,
    None when unsupported (non-Windows) or on any failure — callers treat
    only an explicit False as "don't start", so a broken mutex API can never
    lock the user out of their own app. Two instances fighting over TOPMOST
    every second and racing on the sidecar files is the failure this guards
    against (e.g. Windows-startup entry + a manual launch)."""
    global _single_instance_handle
    if not sys.platform.startswith("win"):
        return None
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return None
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _single_instance_handle = handle
        return True
    except Exception:
        return None


# --------------------------------------------------------------------------
# Windows startup registration: HKCU\...\CurrentVersion\Run entry.
# Uses winreg (stdlib) only -- imported lazily and only on Windows, same
# lazy-import-behind-a-platform-guard pattern as the ctypes helpers above.
# No admin rights needed (per-user hive). Registry ops fail soft, same as
# file I/O elsewhere in this file -- never raise into a long-running thread.
# --------------------------------------------------------------------------

STARTUP_APP_NAME = "Claudebar"   # matches build_exe.bat's --name
STARTUP_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def build_startup_command(frozen, executable, script_path):
    """Pure: decide the command line to register for Windows startup.

    frozen/executable/script_path are explicit (not read from sys.* here)
    so this is unit-testable on any OS without a real frozen build.

    - frozen=True  -> the executable itself, quoted. A bare invocation
      with no args already hits the `else: run_app()` branch at the
      bottom of this file, so no extra args are needed.
    - frozen=False -> prefers pythonw.exe next to `executable` (avoids a
      console window flashing at login), falling back to `executable`
      itself if no pythonw.exe is found alongside it. script_path is
      quoted and passed as the sole argument.
    """
    if frozen:
        return f'"{executable}"'
    pythonw = os.path.join(os.path.dirname(executable), "pythonw.exe")
    interpreter = pythonw if os.path.isfile(pythonw) else executable
    return f'"{interpreter}" "{script_path}"'


def build_hook_command(frozen, executable, script_path):
    """Pure: decide the statusLine command line to register in Claude Code's
    settings.json.

    frozen/executable/script_path are explicit (not read from sys.*) so this
    is unit-testable on any OS without a real frozen build.

    - frozen=True  -> the exe itself plus --statusline-hook. Uses the console
      subsystem's raw fds at runtime (see _read_hook_stdin), so a windowed
      build still works as the hook.
    - frozen=False -> the running interpreter + this script + --statusline-hook
      (the plain `python claudebar.py --statusline-hook` form).
    """
    if frozen:
        return f'"{executable}" --statusline-hook'
    return f'"{executable}" "{script_path}" --statusline-hook'


def build_state_hook_command(frozen, executable, script_path):
    """Pure: the command line to register in settings.json's `hooks` array for
    the working-state indicator. Mirror of build_hook_command, but with the
    --state-hook flag. Same frozen-vs-python shapes so the single windowed exe
    handles this mode too (the hook path reads fd 0 directly, see
    _read_hook_stdin)."""
    if frozen:
        return f'"{executable}" --state-hook'
    return f'"{executable}" "{script_path}" --state-hook'


def _startup_registry_read(value_name, subkey=None, hive=None):
    """Read a REG_SZ value from the given registry subkey.

    subkey/hive default to STARTUP_RUN_SUBKEY/HKEY_CURRENT_USER but can be
    overridden -- used by --test to point at a disposable test subkey
    instead of the real Run key. Returns the string value, or None on any
    failure (key/value missing, non-Windows, winreg unavailable,
    permission error, etc.) Never raises.
    """
    if not sys.platform.startswith("win"):
        return None
    subkey = subkey if subkey is not None else STARTUP_RUN_SUBKEY
    try:
        import winreg
        root = hive if hive is not None else winreg.HKEY_CURRENT_USER
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as key:
            value, _regtype = winreg.QueryValueEx(key, value_name)
            return value
    except (ImportError, OSError):
        return None


def _startup_registry_set(value_name, command, subkey=None, hive=None):
    """Write value_name = command (REG_SZ) under the given registry
    subkey, creating the subkey if needed. Returns True on success, False
    on any failure. Never raises -- this may run on the tray's background
    thread from a menu click, and must not kill it.
    """
    if not sys.platform.startswith("win"):
        return False
    subkey = subkey if subkey is not None else STARTUP_RUN_SUBKEY
    try:
        import winreg
        root = hive if hive is not None else winreg.HKEY_CURRENT_USER
        with winreg.CreateKeyEx(root, subkey, 0, winreg.KEY_WRITE) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command)
        return True
    except (ImportError, OSError):
        return False


def _startup_registry_delete(value_name, subkey=None, hive=None):
    """Remove value_name from the given registry subkey, if present.

    Returns True if the value is now absent (whether just deleted or
    already missing), False only on an unexpected failure (e.g.
    permission error). Never raises.
    """
    if not sys.platform.startswith("win"):
        return False
    subkey = subkey if subkey is not None else STARTUP_RUN_SUBKEY
    try:
        import winreg
        root = hive if hive is not None else winreg.HKEY_CURRENT_USER
        with winreg.OpenKey(root, subkey, 0, winreg.KEY_WRITE) as key:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass  # already absent -- not an error
        return True
    except (ImportError, OSError):
        return False


def is_startup_enabled(value_name=None, subkey=None, hive=None):
    """True if a Run-key entry named value_name currently exists. Any
    value content counts as enabled -- a stale/renamed-path entry still
    reads as enabled rather than silently reporting disabled."""
    value_name = value_name or STARTUP_APP_NAME
    return _startup_registry_read(value_name, subkey, hive) is not None


def set_startup_enabled(enabled, value_name=None, subkey=None, hive=None,
                          frozen=None, executable=None, script_path=None):
    """Register or unregister the app for Windows startup.

    frozen/executable/script_path default to the real running process's
    sys.frozen/sys.executable/abspath(__file__), but are overridable so
    --test can pass fabricated values without needing a real frozen exe.
    Returns True on success, False on failure (including non-Windows,
    where it's always a no-op False).
    """
    value_name = value_name or STARTUP_APP_NAME
    if enabled:
        frozen = getattr(sys, "frozen", False) if frozen is None else frozen
        executable = sys.executable if executable is None else executable
        script_path = os.path.abspath(__file__) if script_path is None else script_path
        command = build_startup_command(frozen, executable, script_path)
        return _startup_registry_set(value_name, command, subkey, hive)
    else:
        return _startup_registry_delete(value_name, subkey, hive)


# --------------------------------------------------------------------------
# Rate-limit cache layer: session (5-hour) and weekly (7-day) usage % and
# reset times aren't in the local JSONL logs -- they only exist inside
# Claude Code's live connection to Anthropic's servers. Recent Claude Code
# versions (>=1.2.80) expose them by passing a `rate_limits` field to
# whatever command is configured as the `statusLine` hook, on every turn.
# `run_statusline_hook()` below is meant to be set as that hook: it reads
# the JSON Claude Code sends, caches the numbers we care about to a small
# local file, and prints a normal status line back so the terminal still
# shows something useful. The tray app then just reads that cache file.
# --------------------------------------------------------------------------

def process_statusline_payload(payload):
    """Pure function: turns one statusLine JSON payload into (cache_dict,
    status_line_text). Kept separate from stdin/file I/O so it's testable.

    Every field is type-guarded (_as_number / isinstance): the payload is
    external input, and one non-numeric percentage must not crash the hook
    (which would blank the user's statusline every turn)."""
    payload = payload if isinstance(payload, dict) else {}

    def _dict(value):
        return value if isinstance(value, dict) else {}

    model = _dict(payload.get("model")).get("display_name")
    if not isinstance(model, str) or not model:
        model = "Claude"
    ctx = _dict(payload.get("context_window"))
    rate_limits = _dict(payload.get("rate_limits"))
    five_hour = _dict(rate_limits.get("five_hour"))
    seven_day = _dict(rate_limits.get("seven_day"))

    cache = {
        "fetched_at": time.time(),
        "model": model,
        "context_used_percentage": _as_number(ctx.get("used_percentage")),
        "session_used_percentage": _as_number(five_hour.get("used_percentage")),
        "session_resets_at": _as_number(five_hour.get("resets_at")),
        "weekly_used_percentage": _as_number(seven_day.get("used_percentage")),
        "weekly_resets_at": _as_number(seven_day.get("resets_at")),
    }

    parts = [model]
    if cache["context_used_percentage"] is not None:
        parts.append(f"ctx {cache['context_used_percentage']:.0f}%")
    if cache["session_used_percentage"] is not None:
        parts.append(f"5h {cache['session_used_percentage']:.0f}%")
    if cache["weekly_used_percentage"] is not None:
        parts.append(f"7d {cache['weekly_used_percentage']:.0f}%")
    status_line_text = " | ".join(parts)
    return cache, status_line_text


def derive_working_state(payload):
    """Pure: turn one Claude Code hook payload into a working-state string, or
    None when the event doesn't map to a state we track.

    Mapping (locked with the user):
      UserPromptSubmit / PreToolUse / PostToolUse -> "working"  (amber)
      Stop                                        -> "done"     (green)
      Notification                                -> "waiting"  (red, needs you)

    None (unrecognised or missing ``hook_event_name``) means "leave the last
    known state alone" -- so an older Claude Code that omits the field, or a
    future event we don't handle, simply doesn't overwrite the cache. Kept
    separate from I/O so it's testable."""
    event = (payload or {}).get("hook_event_name")
    if event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        return "working"
    if event == "Stop":
        return "done"
    if event == "Notification":
        return "waiting"
    return None


def write_usage_cache(cache, cache_path=None):
    cache_path = cache_path or USAGE_CACHE_PATH
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp_path, cache_path)  # atomic on both Windows and POSIX
    except OSError:
        pass


def read_usage_cache(cache_path=None):
    cache_path = cache_path or USAGE_CACHE_PATH
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def read_notify_pref(prefs_path=None):
    """Whether to toast when Claude enters "waiting" (default True).

    Reads the prefs sidecar fail-soft: a missing, corrupted, or wrong-typed
    file (or key) degrades to the default, never a crash."""
    prefs = read_usage_cache(cache_path=prefs_path or PREFS_PATH)
    if isinstance(prefs, dict) and isinstance(prefs.get("notify_on_waiting"), bool):
        return prefs["notify_on_waiting"]
    return True


def write_notify_pref(enabled, prefs_path=None):
    """Persist the notify-on-waiting toggle, preserving any other keys the
    prefs sidecar may grow later. Atomic + fail-soft via write_usage_cache."""
    path = prefs_path or PREFS_PATH
    prefs = read_usage_cache(cache_path=path)
    prefs = prefs if isinstance(prefs, dict) else {}
    prefs["notify_on_waiting"] = bool(enabled)
    write_usage_cache(prefs, cache_path=path)


def _is_our_statusline_command(command):
    """True when a statusLine command string is "ours-shaped" — i.e. it ends
    in this app's --statusline-hook flag, so it was written by some (possibly
    older, possibly differently-pathed) install of this app and is safe to
    upgrade in place. A genuinely foreign command never carries our flag."""
    return isinstance(command, str) and command.rstrip().endswith("--statusline-hook")


def _is_our_state_command(command):
    """Mirror of _is_our_statusline_command for --state-hook entries."""
    return isinstance(command, str) and command.rstrip().endswith("--state-hook")


def install_statusline_hook(settings_path, command, force=True):
    """Merge the statusLine hook entry into Claude Code's settings.json.

    `command` is the fully-formed command line (see build_hook_command).

    force=True  -> always (re)write our entry, even over a different existing
                   statusLine command (the --install-hook escape hatch).
    force=False -> add our entry when there's NO statusLine at all, or upgrade
                   in place when the existing one is ours-shaped (ends in
                   --statusline-hook — e.g. an older install path); never
                   clobber a genuinely foreign statusLine (the auto path).

    Returns (success: bool, message: str). Never touches the file if it can't
    be parsed cleanly — safer than risking a corrupt settings.json (one stray
    comma kills the whole file).
    """
    desired = {"type": "command", "command": command}

    # Read existing settings (or start fresh if the file doesn't exist yet)
    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, ValueError) as exc:
            return False, (
                f"settings.json exists but couldn't be parsed: {exc}\n"
                "Fix the JSON manually, then re-run --install-hook."
            )
        except OSError as exc:
            return False, f"Couldn't read settings.json: {exc}"

    existing = settings.get("statusLine")
    if existing == desired:
        return True, "statusLine hook already configured — nothing to change."

    existing_command = existing.get("command") if isinstance(existing, dict) else None
    if existing is not None and not force and not _is_our_statusline_command(existing_command):
        return True, (
            "Left the existing statusLine untouched:\n"
            f"  {json.dumps(existing)}\n"
            "Run with --install-hook to overwrite it with this app's hook."
        )

    if existing is not None:
        msg_prefix = f"Replacing existing statusLine:\n  {json.dumps(existing)}\nwith:"
    else:
        msg_prefix = "Adding statusLine:"

    settings["statusLine"] = desired

    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        tmp = settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, settings_path)
    except OSError as exc:
        return False, f"Couldn't write settings.json: {exc}"

    return True, (
        f"{msg_prefix}\n  {json.dumps(desired)}\n"
        f"Written to: {settings_path}\n"
        "Restart Claude Code for the change to take effect."
    )


def _current_statusline_command(settings_path):
    """The `statusLine.command` string currently in settings.json, or None."""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    sl = settings.get("statusLine")
    return sl.get("command") if isinstance(sl, dict) else None


def write_hook_marker(command, marker_path=None):
    """Record that our statusLine hook is in place. Atomic, fail-soft."""
    marker_path = marker_path or HOOK_INSTALLED_MARKER_PATH
    write_usage_cache({"installed": True, "command": command}, cache_path=marker_path)


def read_hook_marker(marker_path=None):
    """Read the hook-installed marker sidecar, or None if absent/unreadable."""
    marker_path = marker_path or HOOK_INSTALLED_MARKER_PATH
    return read_usage_cache(cache_path=marker_path)


def ensure_hook_installed(settings_path, command, marker_path, force=False):
    """Make sure Claude Code's statusLine points at our hook, and record a
    marker sidecar when it does.

    force=False (auto, called on every app startup): install only if there's
      no statusLine yet, so a fresh run — or a run after the user deleted the
      setting — wires it up automatically, while a user's own statusLine is
      left alone. Idempotent when ours is already configured.
    force=True (--install-hook): always (re)write ours.

    Returns (success, message). Fail-soft; never raises.
    """
    success, message = install_statusline_hook(settings_path, command, force=force)
    if success and _current_statusline_command(settings_path) == command:
        write_hook_marker(command, marker_path)
    return success, message


# ---------------------------------------------------------------------------
# Slim companion hook exe. A PyInstaller --onefile build self-extracts its
# whole bundle (Pillow/pystray/watchdog/tkinter) on EVERY launch — and Claude
# Code launches the hook command on every statusline render and every
# registered lifecycle event, PreToolUse included (which is blocking). So the
# release build also produces ClaudebarHook.exe: the same script, GUI
# packages excluded, several times smaller and correspondingly faster to
# extract. It is bundled INSIDE the main exe (single-file download preserved),
# extracted to ~/.claude/bin/ at startup, and registered as the hook command.
# Every step fails soft back to registering the main exe itself.
# ---------------------------------------------------------------------------

HOOK_EXE_NAME = "ClaudebarHook.exe"
HOOK_EXE_INSTALL_DIR = os.path.join(CLAUDE_HOME, "bin")


def _bundled_hook_exe_path():
    """Path of the slim hook exe inside the frozen onefile bundle, or None
    (not frozen, or this build doesn't carry one)."""
    if not getattr(sys, "frozen", False):
        return None
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    path = os.path.join(base, HOOK_EXE_NAME)
    return path if os.path.isfile(path) else None


def install_hook_exe(bundled_path=None, dest_dir=None):
    """Copy the bundled slim hook exe to ~/.claude/bin (atomic, and only when
    the bytes actually differ, so a normal startup is one read + compare).

    Returns the installed exe's path, or None when there's nothing to install
    or installation failed with no previous copy present — callers then fall
    back to registering the main exe itself. If a previous copy exists but
    can't be replaced (e.g. a hook process is executing it right now,
    sharing-violation on os.replace), the previous copy's path is returned:
    an older hook that works beats no hook."""
    bundled_path = bundled_path or _bundled_hook_exe_path()
    if not bundled_path:
        return None
    dest_dir = dest_dir or HOOK_EXE_INSTALL_DIR
    dest = os.path.join(dest_dir, HOOK_EXE_NAME)
    try:
        with open(bundled_path, "rb") as f:
            data = f.read()
        try:
            with open(dest, "rb") as f:
                if f.read() == data:
                    return dest
        except OSError:
            pass
        os.makedirs(dest_dir, exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return dest
    except OSError:
        return dest if os.path.isfile(dest) else None


def _resolve_hook_command():
    """The statusLine command line for the current process: the slim installed
    hook exe when running frozen (see install_hook_exe), else the main
    exe / plain-python form via build_hook_command."""
    frozen = getattr(sys, "frozen", False)
    if frozen:
        hook_exe = install_hook_exe()
        if hook_exe:
            return f'"{hook_exe}" --statusline-hook'
    return build_hook_command(frozen, sys.executable, os.path.abspath(__file__))


def run_install_hook():
    """Entry point for --install-hook: force-(re)writes this app as Claude
    Code's statusLine command, overwriting any existing one."""
    settings_path = os.path.join(CLAUDE_HOME, "settings.json")
    success, message = ensure_hook_installed(
        settings_path, _resolve_hook_command(), HOOK_INSTALLED_MARKER_PATH, force=True,
    )
    # Use the fd-safe writer, not print(): a windowed (--noconsole) exe has
    # sys.stdout == None, which would make print() raise after the install.
    _write_hook_stdout(message)
    if not success:
        sys.exit(1)


def install_state_hooks(settings_path, command, force=False):
    """Merge our working-state hook `command` into settings.json's `hooks` array
    for each event in STATE_HOOK_EVENTS.

    Non-destructive and idempotent: for each event we append a matcher group
    pointing at `command` only when it isn't already registered there; we never
    remove or edit the user's own hook groups. Writes only when something
    actually changed, so a normal startup doesn't churn settings.json.

    Two kinds of *our own* entries are also maintained (never the user's):
      - an ours-shaped command (ends in --state-hook, see
        _is_our_state_command) that differs from `command` — e.g. written by
        an older install at a different path — is upgraded in place rather
        than left to run alongside a freshly-appended duplicate;
      - our command is removed from events in REMOVED_STATE_HOOK_EVENTS
        (events we used to register but no longer do, e.g. PostToolUse).

    Refuses to touch the file if it exists but won't parse — a corrupt
    settings.json is worse than a missing indicator (one stray comma kills the
    whole file). `force` is accepted for signature-symmetry with
    install_statusline_hook; there's no foreign command to overwrite here, so it
    has no effect.

    Returns (success: bool, message: str). Fail-soft on write errors.
    """
    entry = {"type": "command", "command": command}

    settings = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, ValueError) as exc:
            return False, (
                f"settings.json exists but couldn't be parsed: {exc}\n"
                "Fix the JSON manually, then re-run --install-state-hooks."
            )
        except OSError as exc:
            return False, f"Couldn't read settings.json: {exc}"

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}

    changed = False

    for event in STATE_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        present = False
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            inner = []
            for h in group["hooks"]:
                if isinstance(h, dict) and _is_our_state_command(h.get("command")):
                    if present:
                        changed = True   # a duplicate of ours -- drop it
                        continue
                    if h.get("command") != command:
                        h = dict(h, command=command)   # upgrade ours in place
                        changed = True
                    present = True
                inner.append(h)
            if inner != group["hooks"]:
                group = dict(group, hooks=inner)
            if not inner and set(group.keys()) <= {"matcher", "hooks"}:
                changed = True           # a group of ours emptied out -- drop it
                continue
            new_groups.append(group)
        if not present:
            new_groups.append({"matcher": "", "hooks": [dict(entry)]})
            changed = True
        hooks[event] = new_groups

    # Retire OUR command from events we no longer register. User hooks on
    # these events are untouched.
    for event in REMOVED_STATE_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                inner = [h for h in group["hooks"]
                         if not (isinstance(h, dict) and _is_our_state_command(h.get("command")))]
                if len(inner) != len(group["hooks"]):
                    changed = True
                    if not inner and set(group.keys()) <= {"matcher", "hooks"}:
                        continue
                    group = dict(group, hooks=inner)
            new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            hooks.pop(event, None)

    if not changed:
        return True, "working-state hooks already configured — nothing to change."

    settings["hooks"] = hooks

    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        tmp = settings_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")
        os.replace(tmp, settings_path)
    except OSError as exc:
        return False, f"Couldn't write settings.json: {exc}"

    return True, (
        f"Registered the working-state indicator hooks "
        f"({', '.join(STATE_HOOK_EVENTS)}).\n"
        f"Written to: {settings_path}\n"
        "Restart Claude Code for the change to take effect."
    )


def _state_hooks_installed(settings_path, command):
    """True when `command` is registered under EVERY event in STATE_HOOK_EVENTS
    in settings.json — i.e. our state hooks are fully in place."""
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    for event in STATE_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            return False
        found = False
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                if isinstance(h, dict) and h.get("command") == command:
                    found = True
        if not found:
            return False
    return True


def write_state_hooks_marker(command, marker_path=None):
    """Record that our working-state hooks are in place. Atomic, fail-soft."""
    marker_path = marker_path or STATE_HOOKS_MARKER_PATH
    write_usage_cache({"installed": True, "command": command}, cache_path=marker_path)


def read_state_hooks_marker(marker_path=None):
    """Read the state-hooks-installed marker sidecar, or None if absent."""
    marker_path = marker_path or STATE_HOOKS_MARKER_PATH
    return read_usage_cache(cache_path=marker_path)


def ensure_state_hooks_installed(settings_path, command, marker_path, force=False):
    """Make sure our --state-hook command is registered for every
    STATE_HOOK_EVENTS event, and record a marker sidecar when it is.

    Called on every app startup (force is a no-op here — see install_state_hooks):
    idempotent, only appends our own entries, never removes/alters the user's
    other hooks. Returns (success, message). Fail-soft; never raises."""
    success, message = install_state_hooks(settings_path, command, force=force)
    if success and _state_hooks_installed(settings_path, command):
        write_state_hooks_marker(command, marker_path)
    return success, message


def _resolve_state_hook_command():
    """The --state-hook command line for the current process: the slim
    installed hook exe when running frozen (see install_hook_exe), else the
    main exe / plain-python form via build_state_hook_command."""
    frozen = getattr(sys, "frozen", False)
    if frozen:
        hook_exe = install_hook_exe()
        if hook_exe:
            return f'"{hook_exe}" --state-hook'
    return build_state_hook_command(frozen, sys.executable, os.path.abspath(__file__))


def run_install_state_hooks():
    """Entry point for --install-state-hooks: (re)registers this app's
    --state-hook command for the working-state indicator events."""
    settings_path = os.path.join(CLAUDE_HOME, "settings.json")
    success, message = ensure_state_hooks_installed(
        settings_path, _resolve_state_hook_command(), STATE_HOOKS_MARKER_PATH, force=True,
    )
    _write_hook_stdout(message)
    if not success:
        sys.exit(1)


def _read_hook_stdin():
    """Read the raw statusLine payload bytes from standard input.

    A PyInstaller ``--noconsole`` (GUI-subsystem) build sets ``sys.stdin``
    to ``None`` because there's no console -- but Claude Code invokes the
    statusLine command with stdin *redirected* to a pipe, so the raw OS
    file descriptor 0 is still valid. So prefer ``sys.stdin`` when it
    exists (plain ``python`` invocation), and fall back to reading fd 0
    directly (frozen exe). Fails soft to empty bytes.
    """
    stream = getattr(sys, "stdin", None)
    if stream is not None:
        try:
            return stream.buffer.read()
        except (AttributeError, ValueError, OSError):
            try:
                return stream.read().encode("utf-8", "replace")
            except (ValueError, OSError):
                return b""
    # No sys.stdin (windowed frozen build): read the redirected fd 0 directly.
    try:
        chunks = []
        while True:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return b""


def _write_hook_stdout(text):
    """Write the status line back to standard output.

    Mirror of _read_hook_stdin: ``sys.stdout`` is ``None`` in a windowed
    frozen build, but fd 1 is a valid redirected pipe when Claude Code
    captures the status line. Fails soft.
    """
    data = (text + "\n").encode("utf-8", "replace")
    stream = getattr(sys, "stdout", None)
    if stream is not None:
        try:
            stream.buffer.write(data)
            stream.flush()
            return
        except (AttributeError, ValueError, OSError):
            pass
    try:
        os.write(1, data)
    except OSError:
        pass


def run_statusline_hook():
    """Entry point for `... --statusline-hook`, meant to be wired up as
    Claude Code's `statusLine` command (see README.md). Reads Claude Code's
    JSON payload from stdin, caches it, and prints a compact status line
    back to stdout.

    Reads/writes stdin/stdout via raw OS file descriptors when needed so
    this works from a windowed (``--noconsole``) PyInstaller exe, where
    ``sys.stdin``/``sys.stdout`` are ``None`` -- see _read_hook_stdin."""
    try:
        payload = json.loads(_read_hook_stdin().decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        payload = {}
    try:
        cache, status_line_text = process_statusline_payload(payload)
        write_usage_cache(cache)
    except Exception:
        # Whatever happens, print SOMETHING back — an empty/erroring
        # statusline command blanks the user's terminal status every turn.
        status_line_text = "Claude"
    _write_hook_stdout(status_line_text)


def run_state_hook():
    """Entry point for `... --state-hook`, wired into Claude Code's `hooks`
    array for several lifecycle events (see STATE_HOOK_EVENTS). Reads the hook
    JSON payload from stdin, derives Claude's current working state, and — when
    the event maps to one — atomically caches it so the widget and tray icon can
    colour a Red/Amber/Green indicator.

    Emits nothing on stdout and always exits 0: a hook that errors or prints
    junk would surface to the user on every turn. Uses the fd-safe stdin reader
    so the windowed frozen exe works too (see _read_hook_stdin)."""
    try:
        payload = json.loads(_read_hook_stdin().decode("utf-8", "replace"))
    except (json.JSONDecodeError, ValueError):
        payload = {}
    try:
        # Read-modify-write of the per-session map (see update_state_cache).
        # Two hook processes racing is last-writer-wins, same as the old
        # single-slot shape — acceptable, the next event corrects it.
        new_cache = update_state_cache(
            read_usage_cache(cache_path=STATE_CACHE_PATH), payload,
        )
        if new_cache is not None:
            write_usage_cache(new_cache, cache_path=STATE_CACHE_PATH)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Tray app. GUI/watcher dependencies (pystray, Pillow, watchdog) are
# imported lazily inside this function, only when actually running the
# app — so --test never needs a tray backend to be available.
# --------------------------------------------------------------------------

def run_app():
    # Two live instances fight over TOPMOST every second and race on the
    # sidecar files — bail quietly if one is already running. Only an explicit
    # False stops us; None (non-Windows / mutex failure) never locks the user out.
    if acquire_single_instance_lock() is False:
        return

    import pystray
    import tkinter as tk
    from PIL import Image, ImageDraw
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    # Zero-setup: on every launch, make sure Claude Code is piping rate-limit
    # data to us. Only adds our statusLine when none exists (so a fresh install,
    # or a rerun after the user deleted the setting, self-heals); never clobbers
    # a user's own statusLine. Fully fail-soft — must never block the tray.
    try:
        ensure_hook_installed(
            os.path.join(CLAUDE_HOME, "settings.json"),
            _resolve_hook_command(), HOOK_INSTALLED_MARKER_PATH, force=False,
        )
    except Exception:
        pass

    # Same zero-setup treatment for the working-state indicator: register our
    # --state-hook command for the lifecycle events (STATE_HOOK_EVENTS) so
    # Claude Code reports live working/waiting/done state. Only appends our own
    # hook entries, never touching the user's other hooks. Fail-soft.
    try:
        ensure_state_hooks_installed(
            os.path.join(CLAUDE_HOME, "settings.json"),
            _resolve_state_hook_command(), STATE_HOOKS_MARKER_PATH, force=False,
        )
    except Exception:
        pass

    FALLBACK_SWEEP_SECONDS = 30   # safety-net full rescan interval
    WATCHER_RETRY_SECONDS = 5     # retry interval if projects dir doesn't exist yet
    WIDGET_POS_PATH = os.path.join(CLAUDE_HOME, "claudebar_widget_pos.json")
    ALIGNMENT_CONFIG_PATH = os.path.join(CLAUDE_HOME, "claudebar_alignment.json")
    WIDGET_PIN_PATH = os.path.join(CLAUDE_HOME, "claudebar_widget_pin.json")
    WIDGET_FAVORITE_POS_PATH = os.path.join(CLAUDE_HOME, "claudebar_widget_favorite_pos.json")

    ALIGNMENT_CONFIG = TaskbarAlignmentConfig()
    _saved_align = read_usage_cache(cache_path=ALIGNMENT_CONFIG_PATH) or {}
    ALIGNMENT_CONFIG.enabled = _saved_align.get("enabled", True)
    WIDGET_COLORS = {
        "green": "#5fb85f", "yellow": "#e0b341", "red": "#e0605a", "dim": "#888888",
    }

    tracker = UsageTracker()
    state_lock = threading.Lock()
    widget_visible = threading.Event()
    widget_visible.set()  # shown by default
    should_quit = threading.Event()
    position_pinned = threading.Event()
    _saved_pin = read_usage_cache(cache_path=WIDGET_PIN_PATH) or {}
    if _saved_pin.get("pinned", False):
        position_pinned.set()
    load_favorite_requested = threading.Event()
    widget = None  # reassigned to the real FloatingWidget near the end of
                   # run_app(); forward-declared so on_save_favorite/
                   # on_load_favorite (tray thread, defined below) can
                   # reference it as a free variable without a NameError if
                   # clicked in the sub-second window before it's constructed.

    def current_snapshot():
        with state_lock:
            return tracker.snapshot()

    def current_state_tag():
        """WIDGET_COLORS key for Claude's current working state, read fresh from
        the state cache (dim when unknown/stale). Aggregates across all live
        sessions (waiting > working > done). Shared by the widget dot and
        the tray-icon pip."""
        state, updated_at = aggregate_working_state(
            read_usage_cache(cache_path=STATE_CACHE_PATH)
        )
        return working_state_tag(state, updated_at)

    class FloatingWidget:
        """Small always-on-top, borderless window showing the same live
        numbers as the tray menu, without needing to hover/click anything.
        Position is draggable and remembered across restarts. Updates are
        a simple self-poll (every 1s) rather than cross-thread signaling,
        since reading a snapshot + one small cache file is cheap.

        Percentage bars are drawn on a Canvas (real rectangles) rather
        than built from Unicode block characters: those glyphs aren't
        guaranteed uniform width across fonts/platforms, which previously
        caused text to overflow the box Tk had sized for it."""

        BAR_W, BAR_H = 90, 10
        BG, FG, DIM, TRACK = "#1e1e1e", "#e6e6e6", "#888888", "#3a3a3a"
        # COLORREF (0x00BBGGRR) for BG="#1e1e1e"; used as colorkey so the
        # background is punched through in idle mode (data-only, no dark panel).
        _BG_COLORREF = 0x001E1E1E

        def __init__(self):
            self.root = tk.Tk()
            self.root.overrideredirect(True)
            self.root.configure(bg=self.BG)

            # 2-row layout: today on row 1, session+weekly combined on row 2.
            # 2 rows of 9pt Consolas always fit in any standard taskbar (40\u201360px)
            # without font scaling.  Row 2 is wider than a single metric row was,
            # which is what makes the widget "wider" to compensate for being shorter.
            import tkinter.font as tkfont
            _tb_info = find_taskbar_tray_rect(ALIGNMENT_CONFIG)
            _tb_h = ((_tb_info["taskbar_rect"][3] - _tb_info["taskbar_rect"][1])
                     if _tb_info else 48)   # 48px: Win11 @ 100% DPI fallback
            self._tb_h = _tb_h
            for _fs in (9, 8, 7, 6):
                _lh = tkfont.Font(family="Consolas", size=_fs).metrics("linespace")
                if 2 * _lh <= _tb_h:
                    break
            self._fs = _fs
            self.BAR_H = max(4, min(10, _lh - 2))   # shadows class-level BAR_H
            self.BAR_W = 65                           # shadows class-level BAR_W=90
            _slack        = max(0, _tb_h - 2 * _lh)
            _frame_pady   = min(4, _slack // 4)
            _slack       -= 2 * _frame_pady
            _today_pady   = min(2, _slack // 3)
            _slack       -= 2 * _today_pady
            self._row_pady = min(1, _slack // 4)

            frame = tk.Frame(self.root, bg=self.BG, padx=10, pady=_frame_pady)
            frame.pack(fill="both", expand=True)

            # Row 1: RAG working-state dot + today's tokens/cost + a short state
            # label ("working"/"waiting for you"/"done"). The dot is a drawn
            # Canvas oval, never a Unicode glyph, so its size is font-independent
            # (ARCHITECTURE.md §6).
            today_row = tk.Frame(frame, bg=self.BG)
            today_row.pack(fill="x", pady=(_today_pady, _today_pady))

            self._dot_d = max(6, self.BAR_H)
            self.state_canvas = tk.Canvas(
                today_row, width=self._dot_d, height=self._dot_d,
                bg=self.BG, highlightthickness=0,
            )
            self.state_canvas.pack(side="left", padx=(0, 5))

            self.today_label = tk.Label(
                today_row, font=("Consolas", _fs), fg=self.FG, bg=self.BG, anchor="w",
            )
            self.today_label.pack(side="left", fill="x", expand=True)

            # Pin toggle: a small drawn pushpin in the top-right corner
            # (ARCHITECTURE.md §6 — drawn on a Canvas, never a glyph). It is
            # hover-revealed, and stays faintly visible while pinned so a locked
            # widget always advertises that it's locked. Deliberately NOT in the
            # drag-binding loop below — its own Button-1 toggles the pin and
            # breaks the event chain so it never starts a window drag.
            self.pin_canvas = tk.Canvas(
                today_row, width=self._dot_d, height=self._dot_d,
                bg=self.BG, highlightthickness=0,
            )
            self.pin_canvas.pack(side="right", padx=(6, 0))
            self.pin_canvas.bind("<Button-1>", self._toggle_pin)
            self.pin_canvas.bind("<B1-Motion>", lambda e: "break")
            self.pin_canvas.bind("<ButtonRelease-1>", lambda e: "break")
            self._pin_drawn = None  # (visible, bright, pinned) of last draw

            self.state_label = tk.Label(
                today_row, font=("Consolas", _fs), fg=self.DIM, bg=self.BG, anchor="e",
            )
            self.state_label.pack(side="right", padx=(6, 0))

            # Single row: session 5h and weekly 7d side-by-side
            metrics_row = tk.Frame(frame, bg=self.BG)
            metrics_row.pack(fill="x", pady=self._row_pady)

            tk.Label(metrics_row, text="5h", font=("Consolas", _fs), fg=self.FG, bg=self.BG,
                     ).pack(side="left", padx=(0, 4))
            self.session_canvas = tk.Canvas(
                metrics_row, width=self.BAR_W, height=self.BAR_H, bg=self.BG, highlightthickness=0,
            )
            self.session_canvas.pack(side="left", padx=(0, 4))
            self.session_reset_lbl = tk.Label(
                metrics_row, font=("Consolas", _fs), fg=self.DIM, bg=self.BG, anchor="w",
            )
            self.session_reset_lbl.pack(side="left", padx=(4, 10))

            tk.Label(metrics_row, text="\u00b7", font=("Consolas", _fs), fg=self.DIM, bg=self.BG,
                     ).pack(side="left", padx=(0, 10))

            tk.Label(metrics_row, text="7d", font=("Consolas", _fs), fg=self.FG, bg=self.BG,
                     ).pack(side="left", padx=(0, 4))
            self.weekly_canvas = tk.Canvas(
                metrics_row, width=self.BAR_W, height=self.BAR_H, bg=self.BG, highlightthickness=0,
            )
            self.weekly_canvas.pack(side="left", padx=(0, 4))
            self.weekly_reset_lbl = tk.Label(
                metrics_row, font=("Consolas", _fs), fg=self.DIM, bg=self.BG, anchor="w",
            )
            self.weekly_reset_lbl.pack(side="left", padx=(4, 0))

            for w in (self.root, frame, today_row, self.state_canvas,
                      self.today_label, self.state_label, metrics_row):
                w.bind("<Button-1>", self._start_drag)
                w.bind("<B1-Motion>", self._do_drag)
                w.bind("<ButtonRelease-1>", self._end_drag)

            self._drag_offset = (0, 0)
            self._last_alignment_check = 0.0
            self._topmost_hwnd = None  # resolved on first _reassert_topmost call
            self._dragging = False
            self._win_event_hook = None   # WinEventHook handle (Windows only)
            self._win_event_proc = None   # keep reference — ctypes GC will break the hook
            self._topmost_dirty = False   # set by the WinEvent callback, applied in _fast_tick
            self._overlay_hwnd = None     # resolved by _apply_overlay_styles()
            self._render()                  # render real content first...
            self.root.update_idletasks()    # ...so reqwidth/reqheight reflect it...
            self._place_initial()           # ...before sizing/positioning the window.
            self.root.update_idletasks()    # flush geometry to Win32 before style changes
            # Mirrors the widget's current screen position into a plain tuple so
            # a tray-thread callback (on_save_favorite) can read "where is the
            # widget right now" without ever calling a Tkinter method itself.
            # Only ever written from the Tk thread (here, in _tick(), and in
            # _end_drag()) -- read cross-thread the same way ALIGNMENT_CONFIG.enabled
            # already is.
            self.last_known_pos = (self.root.winfo_x(), self.root.winfo_y())
            self._apply_overlay_styles()    # atomic Win32 layered-overlay setup
            self._set_transparent(True)
            self._refresh_pin(self._pointer_inside())  # faint pin if starting pinned
            self.root.after(150, self._fast_tick)
            self.root.after(1000, self._tick)

        def _apply_overlay_styles(self):
            """Apply WS_EX_LAYERED + WS_EX_TOPMOST + WS_EX_TOOLWINDOW + WS_EX_NOACTIVATE
            atomically, then configure LWA_COLORKEY+LWA_ALPHA for per-pixel
            transparency and promote z-order via SetWindowPos(HWND_TOPMOST).
            All in one place so there is exactly one WM_STYLECHANGED, not three."""
            if not sys.platform.startswith("win"):
                return
            try:
                import ctypes
                import ctypes.wintypes
                GWL_EXSTYLE      = -20
                WS_EX_LAYERED    = 0x00080000
                WS_EX_TOOLWINDOW = 0x00000080
                WS_EX_NOACTIVATE = 0x08000000
                LWA_ALPHA        = 0x2

                inner = self.root.winfo_id()
                outer = ctypes.windll.user32.GetAncestor(inner, 2) or inner
                self._overlay_hwnd = outer
                self._topmost_hwnd = outer  # used by _reassert_topmost

                # ONE SetWindowLongW — avoids the WM_STYLECHANGED storm that the
                # old code caused by calling it separately for each style bit.
                # WS_EX_TOPMOST is intentionally NOT set here; SetWindowPos manages it.
                cur = ctypes.windll.user32.GetWindowLongW(outer, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(
                    outer, GWL_EXSTYLE,
                    cur | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
                )

                # Placeholder: alpha=1, no colorkey.  _set_transparent(True) is
                # called immediately after and switches to colorkey+255 (idle mode).
                ctypes.windll.user32.SetLayeredWindowAttributes(
                    outer, 0, 1, LWA_ALPHA,
                )

                # Schedule NOTOPMOST→TOPMOST for the first mainloop iteration.
                # We do NOT call root.attributes("-topmost", True) here because
                # that call goes through Tk's WM machinery and triggers a
                # SetWindowPos internally, which (while the geometry from
                # _place_initial is still being committed) cancels the pending
                # window move and leaves the widget stuck at (0, 0).
                # The after(0) fires after the geometry is fully applied.
                self.root.after(0, self._reassert_topmost)

                # Event-driven z-order recovery: fires when any window becomes
                # foreground (e.g. user clicks taskbar).  WINEVENT_OUTOFCONTEXT=0
                # routes the callback through our message queue — no in-process DLL.
                _WinEventProc = ctypes.WINFUNCTYPE(
                    None,
                    ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
                    ctypes.wintypes.HWND,   ctypes.wintypes.LONG,
                    ctypes.wintypes.LONG,   ctypes.wintypes.DWORD,
                    ctypes.wintypes.DWORD,
                )
                def _on_foreground_change(_h, _e, _w, _o, _c, _t, _ms):
                    # This ctypes callback is dispatched re-entrantly inside
                    # Tcl_DoOneEvent's message pump, where _tkinter has saved
                    # its thread state. Any Tcl call from here (e.g.
                    # root.attributes) corrupts that bookkeeping and fatals
                    # with "PyEval_RestoreThread: ... thread state is NULL".
                    # Only touch plain Python state; _fast_tick applies it.
                    self._topmost_dirty = True
                self._win_event_proc = _WinEventProc(_on_foreground_change)
                self._win_event_hook = ctypes.windll.user32.SetWinEventHook(
                    0x0003, 0x0003, None, self._win_event_proc,
                    0, 0, 0x0000,
                )

            except Exception as exc:
                print(f"[overlay] WARNING: failed to apply overlay styles: {exc}")

        def _set_alpha(self, alpha_byte, *, colorkey=False):
            """Set window opacity via SetLayeredWindowAttributes.
            colorkey=True → also punch through the BG colour so only the data
            (text, bars) is visible; the dark panel background disappears.
            Does NOT trigger WM_STYLECHANGED (unlike SetWindowLongW)."""
            if sys.platform.startswith("win") and self._overlay_hwnd:
                try:
                    import ctypes
                    if colorkey:
                        ctypes.windll.user32.SetLayeredWindowAttributes(
                            self._overlay_hwnd, self._BG_COLORREF, alpha_byte,
                            0x3,   # LWA_COLORKEY | LWA_ALPHA
                        )
                    else:
                        ctypes.windll.user32.SetLayeredWindowAttributes(
                            self._overlay_hwnd, 0, alpha_byte, 0x2,   # LWA_ALPHA only
                        )
                except Exception:
                    pass
            else:
                # Non-Windows: no colorkey support; best effort is full visibility.
                try:
                    self.root.attributes("-alpha", 1.0 if colorkey else alpha_byte / 255)
                except Exception:
                    pass

        def _set_transparent(self, on):
            """on=True → idle: data fully visible, background punched through.
            on=False → drag: background panel visible at 92%."""
            if on:
                self._set_alpha(255, colorkey=True)
            else:
                self._set_alpha(235)

        def _draw_bar(self, canvas, pct):
            canvas.delete("all")
            canvas.create_rectangle(0, 0, self.BAR_W, self.BAR_H, fill=self.TRACK, outline="")
            if pct is not None:
                fill_w = self.BAR_W * max(0.0, min(100.0, pct)) / 100
                canvas.create_rectangle(
                    0, 0, fill_w, self.BAR_H, fill=WIDGET_COLORS[pct_tag(pct)], outline="",
                )
                label, text_color = f"{pct:.0f}%", "#ffffff"
            else:
                label, text_color = "--", self.DIM
            canvas.create_text(
                self.BAR_W // 2, self.BAR_H // 2,
                text=label, fill=text_color,
                font=("Consolas", self._fs, "bold"), anchor="center",
            )

        def _render_metric_row(self, canvas, reset_lbl, pct, resets_at):
            self._draw_bar(canvas, pct)
            if pct is not None:
                reset_lbl.config(text=fmt_reset_clock(resets_at) or "?", fg=self.DIM)
            else:
                reset_lbl.config(text="--", fg=self.DIM)

        def _draw_status_dot(self):
            """Draw the RAG working-state dot and set its label from the state
            cache (written by --state-hook). Pixel-precise Canvas oval, not a
            glyph, so it's font/DPI-independent."""
            state, updated_at = aggregate_working_state(
                read_usage_cache(cache_path=STATE_CACHE_PATH)
            )
            tag = working_state_tag(state, updated_at)
            d = self._dot_d
            self.state_canvas.delete("all")
            # 1px inset so the oval's antialiased edge isn't clipped by the canvas.
            self.state_canvas.create_oval(
                1, 1, d - 1, d - 1, fill=WIDGET_COLORS[tag], outline="",
            )
            self.state_label.config(
                text=working_state_label(state, updated_at),
                fg=(self.DIM if tag == "dim" else WIDGET_COLORS[tag]),
            )

        def _draw_pin(self, *, visible, bright):
            """Draw the pushpin toggle. Drawn on a Canvas, never a glyph
            (ARCHITECTURE.md §6). Filled when pinned, outline when not;
            bright while hovering, dim when only shown because it's pinned."""
            c = self.pin_canvas
            c.delete("all")
            if not visible:
                return
            pinned = position_pinned.is_set()
            color = self.FG if bright else self.DIM
            d = self._dot_d
            cx = d / 2
            # Stem down to the tip, then the round head above it.
            hr = max(2, d * 0.28)          # head radius
            head_cy = d * 0.38
            c.create_line(cx, head_cy, cx, d - 1, fill=color, width=max(1, int(d * 0.14)))
            if pinned:
                c.create_oval(cx - hr, head_cy - hr, cx + hr, head_cy + hr,
                              fill=color, outline="")
            else:
                c.create_oval(cx - hr, head_cy - hr, cx + hr, head_cy + hr,
                              fill="", outline=color, width=max(1, int(d * 0.12)))

        def _refresh_pin(self, inside=None):
            """Keep the pin's drawn state in sync with hover + pinned state.
            Only redraws when something actually changed (called every 150ms
            from _fast_tick). visible = hovering OR pinned; bright = hovering."""
            pinned = position_pinned.is_set()
            inside = bool(inside)
            visible = inside or pinned
            state = (visible, inside, pinned)
            if state == self._pin_drawn:
                return
            self._pin_drawn = state
            self._draw_pin(visible=visible, bright=inside)

        def _toggle_pin(self, event=None):
            """Tk-thread canvas callback — flips the pin lock and persists it.
            Safe to touch Tk directly here (unlike the tray-thread callbacks).
            Returns "break" so the toplevel's drag bindings don't also fire."""
            if position_pinned.is_set():
                position_pinned.clear()
                write_usage_cache({"pinned": False}, cache_path=WIDGET_PIN_PATH)
            else:
                position_pinned.set()
                write_usage_cache({"pinned": True}, cache_path=WIDGET_PIN_PATH)
            self._pin_drawn = None  # force redraw of filled/outline variant
            self._refresh_pin(self._pointer_inside())
            return "break"

        def _pointer_inside(self):
            """True if the mouse pointer is currently over the widget window."""
            try:
                mx = self.root.winfo_pointerx()
                my = self.root.winfo_pointery()
                wx = self.root.winfo_rootx()
                wy = self.root.winfo_rooty()
                ww = self.root.winfo_width()
                wh = self.root.winfo_height()
                return wx <= mx < wx + ww and wy <= my < wy + wh
            except Exception:
                return False

        def _render(self):
            self._draw_status_dot()
            snap = current_snapshot()
            today_tok = sum(snap["today_totals"].values())
            self.today_label.config(
                text=f"Today  {fmt_tokens(today_tok):>7} tok   {fmt_cost(snap['cost_today'])}"
            )
            eff = effective_rate_limits(read_usage_cache())
            self._render_metric_row(
                self.session_canvas, self.session_reset_lbl,
                eff["session_pct"], eff["session_resets_at"],
            )
            self._render_metric_row(
                self.weekly_canvas, self.weekly_reset_lbl,
                eff["weekly_pct"], eff["weekly_resets_at"],
            )

        def _compute_aligned_position(self, w, h, info):
            """Return (x, y) in physical pixels for the aligned position.

            Applies the gap, vertical mode, and monitor-clamp from
            ALIGNMENT_CONFIG.  All values are physical-pixel coordinates
            matching the Win32 rects returned by find_taskbar_tray_rect().
            """
            tray = info["tray_rect"]
            tb   = info["taskbar_rect"]
            mon  = info.get("monitor_rect") or tb

            x = tray[0] - w - ALIGNMENT_CONFIG.gap_px

            if ALIGNMENT_CONFIG.vertical_mode == "inside-taskbar":
                tb_h = tb[3] - tb[1]
                y = tb[1] + (tb_h - h) // 2 + ALIGNMENT_CONFIG.vertical_offset_px
            else:  # "above-taskbar"
                y = (tb[1] - h
                     + ALIGNMENT_CONFIG.overlap_px
                     + ALIGNMENT_CONFIG.vertical_offset_px)

            # Clamp so the widget stays within the monitor
            x = max(mon[0], min(x, mon[2] - w))
            y = max(mon[1], min(y, mon[3] - h))

            _taskbar_log(
                f"aligned position: {w}x{h}+{x}+{y}  tray_left={tray[0]}  "
                f"gap={ALIGNMENT_CONFIG.gap_px}  tb={tb[1]}-{tb[3]}",
                ALIGNMENT_CONFIG,
            )
            return x, y

        def _recheck_alignment(self):
            """Recompute and apply the aligned position (called from _tick).

            Uses the actual rendered window size (winfo_width/height) rather
            than the requisition size, so it stays correct after any resize.
            Repositions via geometry() without altering the window size.
            """
            self._last_alignment_check = time.monotonic()
            info = find_taskbar_tray_rect(ALIGNMENT_CONFIG)
            if not info:
                return
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w < 1 or h < 1:
                w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
            x, y = self._compute_aligned_position(w, h, info)
            self.root.geometry(f"+{int(x)}+{int(y)}")

        def _apply_favorite_position(self):
            """Tk-thread-only, invoked from _tick() when load_favorite_requested
            was set by on_load_favorite() (tray thread). Never call this, or
            touch self.root, from an on_* tray callback directly -- see
            ARCHITECTURE.md §5 on cross-thread Tkinter calls.

            Disables taskbar alignment (same as a manual drag) so the 30s
            re-align check in _tick() doesn't immediately undo the loaded
            position, and persists the new position to WIDGET_POS_PATH so a
            restart keeps the loaded favorite as the last-known position.
            """
            if position_pinned.is_set():
                return
            saved = read_usage_cache(cache_path=WIDGET_FAVORITE_POS_PATH)
            if not saved or saved.get("x") is None or saved.get("y") is None:
                return
            x, y = int(saved["x"]), int(saved["y"])
            # A favorite saved on a monitor that's since been unplugged would
            # park the widget off-screen — clamp into the current virtual screen.
            bounds = virtual_screen_bounds()
            if bounds:
                w = self.root.winfo_width() or self.root.winfo_reqwidth()
                h = self.root.winfo_height() or self.root.winfo_reqheight()
                x, y = clamp_position(x, y, w, h, bounds)
            self.root.geometry(f"+{x}+{y}")
            # Flush the queued move to the real Win32 window *now* -- _tick()
            # calls _reassert_topmost() right after this method returns, which
            # does a synchronous SetWindowPos via root.attributes("-topmost", ...).
            # Without this flush, that call can win the race and the pending
            # geometry change is silently dropped -- the same class of bug as
            # the _place_initial()/_apply_overlay_styles() ordering documented
            # in ARCHITECTURE.md §6.
            self.root.update_idletasks()
            self.last_known_pos = (x, y)
            if ALIGNMENT_CONFIG.enabled:
                ALIGNMENT_CONFIG.enabled = False
                write_usage_cache({"enabled": False}, cache_path=ALIGNMENT_CONFIG_PATH)
            write_usage_cache({"x": x, "y": y}, cache_path=WIDGET_POS_PATH)

        def _place_initial(self):
            w = self.root.winfo_reqwidth()
            h = self.root.winfo_reqheight()

            if ALIGNMENT_CONFIG.enabled:
                if ALIGNMENT_CONFIG.debug_logging:
                    diagnose_taskbar_windows(ALIGNMENT_CONFIG)
                info = find_taskbar_tray_rect(ALIGNMENT_CONFIG)
                if info:
                    self._last_alignment_check = time.monotonic()
                    x, y = self._compute_aligned_position(w, h, info)
                    self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")
                    return
                _taskbar_log(
                    "alignment enabled but taskbar not found — using saved/heuristic",
                    ALIGNMENT_CONFIG,
                )

            saved = read_usage_cache(cache_path=WIDGET_POS_PATH)
            if saved and saved.get("x") is not None and saved.get("y") is not None:
                x, y = saved["x"], saved["y"]
                # The saved spot may be on a monitor that no longer exists
                # (unplugged / resolution change) — clamp it back on screen,
                # or an overrideredirect window is invisible and undraggable.
                bounds = virtual_screen_bounds()
                if bounds:
                    x, y = clamp_position(x, y, w, h, bounds)
            else:
                x, y = self._default_position(w, h)
            self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

        def _default_position(self, w, h):
            """Heuristic fallback when alignment is off and no drag position is saved.
            Tries the tray rect first; falls back to a screen-corner estimate."""
            info = find_taskbar_tray_rect(ALIGNMENT_CONFIG)
            if info:
                return self._compute_aligned_position(w, h, info)
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            return sw - w - 220, sh - h - 50

        def _start_drag(self, event):
            if position_pinned.is_set():
                return
            self._drag_offset = (
                event.x_root - self.root.winfo_x(),
                event.y_root - self.root.winfo_y(),
            )
            self._dragging = True
            self._set_transparent(False)

        def _do_drag(self, event):
            if position_pinned.is_set():
                return
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.root.geometry(f"+{x}+{y}")

        def _end_drag(self, event):
            # Dragging disables alignment so the widget stays where the user puts it.
            # Re-enable via the tray menu "Align to taskbar tray" item.
            if ALIGNMENT_CONFIG.enabled:
                ALIGNMENT_CONFIG.enabled = False
                write_usage_cache({"enabled": False}, cache_path=ALIGNMENT_CONFIG_PATH)
            self.last_known_pos = (self.root.winfo_x(), self.root.winfo_y())
            write_usage_cache(
                {"x": self.last_known_pos[0], "y": self.last_known_pos[1]},
                cache_path=WIDGET_POS_PATH,
            )
            self._dragging = False
            # Stay at hover alpha if mouse is still over the widget; else go idle.
            mx, my = event.x_root, event.y_root
            wx = self.root.winfo_rootx()
            wy = self.root.winfo_rooty()
            ww = self.root.winfo_width()
            wh = self.root.winfo_height()
            inside = wx <= mx < wx + ww and wy <= my < wy + wh
            if inside:
                self._set_alpha(191)
            else:
                self._set_alpha(255, colorkey=True)
            self._refresh_pin(inside)

        def _zorder_vs_taskbar(self):
            """Walk the z-order chain from the top and return our position
            relative to Shell_TrayWnd: 'above', 'below', or 'unknown'."""
            if not sys.platform.startswith("win") or not self._topmost_hwnd:
                return "unknown"
            try:
                import ctypes
                taskbar = _w32_find_window("Shell_TrayWnd")
                if not taskbar:
                    return "unknown"
                GW_HWNDNEXT = 2
                chain = []
                hwnd = ctypes.windll.user32.GetTopWindow(0)
                while hwnd:
                    chain.append(hwnd)
                    hwnd = ctypes.windll.user32.GetWindow(hwnd, GW_HWNDNEXT)
                return _zorder_position(chain, self._topmost_hwnd, taskbar)
            except Exception:
                return "error"

        def _reassert_topmost(self):
            """Re-promote the widget to the very top of the HWND_TOPMOST z-layer.

            Must go through root.attributes("-topmost") rather than raw ctypes
            SetWindowPos because Tkinter's WndProc intercepts WM_WINDOWPOSCHANGING
            and silently reverts any topmost promotion that did not originate from
            Tk's own machinery (it checks its internal wmPtr->flags and cancels
            external SetWindowPos(HWND_TOPMOST) calls).

            NOTOPMOST first: SetWindowPos(HWND_TOPMOST) on a window already in the
            topmost group is a z-order no-op — "remains in its original location."
            Setting NOTOPMOST first re-enters the window as a new TOPMOST entrant,
            placing it above all other topmost windows including the taskbar.
            Both steps are synchronous with no message-loop iteration between them
            so there is no DWM repaint of the briefly-not-topmost state.
            """
            if not sys.platform.startswith("win"):
                return
            try:
                self.root.attributes("-topmost", False)
                self.root.attributes("-topmost", True)
            except Exception:
                pass

        def _fast_tick(self):
            """Runs every 150ms: drives hover alpha and applies any pending
            topmost re-assert flagged by the WinEvent hook. The hook callback
            itself must never call into Tk (see _on_foreground_change), so it
            just sets _topmost_dirty and this loop does the actual Tcl call."""
            if should_quit.is_set():
                return
            if self._topmost_dirty:
                self._topmost_dirty = False
                if widget_visible.is_set():
                    self._reassert_topmost()
            if widget_visible.is_set() and not self._dragging:
                try:
                    mx = self.root.winfo_pointerx()
                    my = self.root.winfo_pointery()
                    wx = self.root.winfo_rootx()
                    wy = self.root.winfo_rooty()
                    ww = self.root.winfo_width()
                    wh = self.root.winfo_height()
                    inside = wx <= mx < wx + ww and wy <= my < wy + wh
                    if inside:
                        self._set_alpha(191)
                    else:
                        self._set_alpha(255, colorkey=True)
                    self._refresh_pin(inside)
                except Exception:
                    pass
            self.root.after(150, self._fast_tick)

        def _tick(self):
            if should_quit.is_set():
                if self._win_event_hook:
                    try:
                        import ctypes
                        ctypes.windll.user32.UnhookWinEvent(self._win_event_hook)
                    except Exception:
                        pass
                self.root.quit()
                return
            # The whole body is guarded so ONE bad render (e.g. a corrupted
            # cache value) degrades one tick — an uncaught exception here
            # would end the after() chain and freeze the widget permanently.
            try:
                if load_favorite_requested.is_set():
                    load_favorite_requested.clear()
                    self._apply_favorite_position()
                if widget_visible.is_set():
                    if not self.root.winfo_viewable():
                        self.root.deiconify()
                        self._set_transparent(True)
                    self._render()
                    # 1-second safety net for z-order. Only re-assert when we
                    # are demonstrably below the taskbar (or can't tell) — an
                    # unconditional NOTOPMOST→TOPMOST cycle every second churns
                    # the z-order and jumps above other topmost apps for no reason.
                    if self._zorder_vs_taskbar() != "above":
                        self._reassert_topmost()
                    # Periodically re-align to the tray (handles taskbar moves, DPI changes,
                    # monitor layout changes, and tray overflow expand/collapse).
                    # A pinned widget is locked in place — never auto-moved.
                    if ALIGNMENT_CONFIG.enabled and not position_pinned.is_set():
                        now = time.monotonic()
                        if now - self._last_alignment_check > 30.0:
                            self._recheck_alignment()
                    self.last_known_pos = (self.root.winfo_x(), self.root.winfo_y())
                else:
                    self.root.withdraw()
            except Exception:
                pass
            self.root.after(1000, self._tick)

        def mainloop(self):
            self.root.mainloop()


    # RGB tuples for the RAG working-state pip, keyed like WIDGET_COLORS.
    # Duplicated as tuples (not the widget's hex strings) because Pillow wants
    # RGBA; kept visually in sync with WIDGET_COLORS.
    STATE_PIP_RGB = {
        "green": (95, 184, 95, 255),
        "yellow": (224, 179, 65, 255),
        "red": (224, 96, 90, 255),
        "dim": (136, 136, 136, 255),
    }

    def make_icon_image(state_tag=None):
        # Drawn as a speedometer-style usage gauge -- green/yellow/red bands
        # plus a needle -- using the same thresholds as pct_tag()/
        # WIDGET_COLORS, so the tray icon is a tiny picture of what the app
        # actually shows. Drawn at 4x and downsampled for anti-aliased edges;
        # kept in sync by hand with generate_icon.py, which produces the
        # static icon.ico used for the packaged .exe's own file icon.
        #
        # state_tag (a WIDGET_COLORS key) adds a small RAG working-state pip in
        # the bottom-right corner; None leaves the plain gauge (used before any
        # state is known).
        size = 64
        scale = 4
        s = size * scale
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        margin = s * 0.06
        bbox = (margin, margin, s - margin, s - margin)
        cx = cy = s / 2
        r = (s - 2 * margin) / 2

        gap_deg = 90
        sweep = 360 - gap_deg
        start = 90 + gap_deg / 2  # Pillow angles: 0=east, 90=south, clockwise
        green_end = start + sweep * 0.50
        yellow_end = green_end + sweep * 0.30
        red_end = yellow_end + sweep * 0.20

        draw.pieslice(bbox, start, green_end, fill=(95, 184, 95, 255))
        draw.pieslice(bbox, green_end, yellow_end, fill=(224, 179, 65, 255))
        draw.pieslice(bbox, yellow_end, red_end, fill=(224, 96, 90, 255))

        # Punch a hole through the middle so the wedges read as a ring/gauge
        # face rather than a solid pie.
        hole_r = r * 0.55
        draw.ellipse((cx - hole_r, cy - hole_r, cx + hole_r, cy + hole_r), fill=(0, 0, 0, 0))

        needle_angle = math.radians(start + sweep * 0.62)
        needle_len = r * 0.98
        nx = cx + needle_len * math.cos(needle_angle)
        ny = cy + needle_len * math.sin(needle_angle)
        draw.line((cx, cy, nx, ny), fill=(45, 42, 40, 255), width=max(1, round(s * 0.05)))

        pivot_r = s * 0.09
        draw.ellipse((cx - pivot_r, cy - pivot_r, cx + pivot_r, cy + pivot_r), fill=(45, 42, 40, 255))

        # RAG working-state pip in the bottom-right corner, with a small dark
        # ring so it reads clearly over any wedge colour behind it.
        if state_tag in STATE_PIP_RGB:
            pip_r = s * 0.20
            pcx = s - pip_r - s * 0.02
            pcy = s - pip_r - s * 0.02
            ring = pip_r * 1.28
            draw.ellipse((pcx - ring, pcy - ring, pcx + ring, pcy + ring), fill=(30, 30, 30, 255))
            draw.ellipse(
                (pcx - pip_r, pcy - pip_r, pcx + pip_r, pcy + pip_r),
                fill=STATE_PIP_RGB[state_tag],
            )

        return img.resize((size, size), Image.LANCZOS)

    # Last RAG tag pushed to the tray icon, so apply_icon_state() only rebuilds
    # the Pillow image when the state actually changes (not on every 30s sweep).
    _icon_state = {"tag": None}

    def apply_icon_state(icon):
        """Recolor the tray icon's RAG pip from the current working state, but
        only when it changed. Runs on the tray/watcher threads (never touches
        Tk); pystray supports reassigning .icon at runtime.

        Also the single place that sees state *transitions*, so the
        notify-on-waiting toast fires from here: entering red (Claude needs
        input) shows a native balloon via pystray's Shell_NotifyIcon wrapper.
        The startup seed of _icon_state["tag"] means an app launched while
        already red stays quiet — only a live transition notifies."""
        tag = current_state_tag()
        prev_tag = _icon_state["tag"]
        if tag == prev_tag:
            return
        _icon_state["tag"] = tag
        if should_notify_waiting(prev_tag, tag) and read_notify_pref():
            try:
                icon.notify("Claude is waiting for your input", "Claude Code")
            except Exception:
                pass  # a failed balloon must never take down the watcher thread
        try:
            icon.icon = make_icon_image(tag)
        except Exception:
            pass

    def build_title():
        snap = current_snapshot()
        today_tok = sum(snap["today_totals"].values())
        lines = [
            "Claude Code usage",
            f"Today: {fmt_tokens(today_tok)} tok ({fmt_cost(snap['cost_today'])})",
        ]
        eff = effective_rate_limits(read_usage_cache())
        if eff["session_pct"] is not None:
            lines.append(
                f"Session: {eff['session_pct']:.0f}% "
                f"(resets {fmt_reset_clock(eff['session_resets_at']) or '?'})"
            )
        if eff["weekly_pct"] is not None:
            lines.append(
                f"Weekly: {eff['weekly_pct']:.0f}% "
                f"(resets {fmt_reset_clock(eff['weekly_resets_at']) or '?'})"
            )
        state, state_ts = aggregate_working_state(
            read_usage_cache(cache_path=STATE_CACHE_PATH)
        )
        st_label = working_state_label(state, state_ts)
        if st_label != "—":
            lines.append(f"State: {st_label}")
        # Windows Shell_NotifyIcon's szTip is capped at 128 UTF-16 chars incl.
        # the NUL terminator (127 usable); pystray raises ValueError past that.
        # Drop whole trailing lines until it fits — never emit a partial line —
        # then hard-cap as a final backstop. The dropped info (state) also lives
        # on the widget dot and in the menu, so nothing is lost outright.
        while len(lines) > 1 and len("\n".join(lines)) > 127:
            lines.pop()
        return "\n".join(lines)[:127]

    def menu_items():
        snap = current_snapshot()
        today_tok = sum(snap["today_totals"].values())
        total_tok = sum(snap["totals"].values())
        top_models = sorted(
            snap["by_model"].items(), key=lambda kv: sum(kv[1].values()), reverse=True
        )[:4]

        items = [
            pystray.MenuItem(
                f"Today:    {fmt_tokens(today_tok)} tok   {fmt_cost(snap['cost_today'])}",
                None, enabled=False,
            ),
            pystray.MenuItem(
                f"All-time: {fmt_tokens(total_tok)} tok   {fmt_cost(snap['cost_total'])}",
                None, enabled=False,
            ),
            pystray.MenuItem(f"Sessions seen: {snap['session_count']}", None, enabled=False),
            pystray.MenuItem(
                f"(costs are estimates; prices as of {PRICES_AS_OF})",
                None, enabled=False,
            ),
        ]

        state, state_ts = aggregate_working_state(
            read_usage_cache(cache_path=STATE_CACHE_PATH)
        )
        st_label = working_state_label(state, state_ts)
        if st_label != "—":
            items.append(pystray.MenuItem(f"State:    Claude is {st_label}", None, enabled=False))

        items.append(pystray.Menu.SEPARATOR)
        cache = read_usage_cache()
        eff = effective_rate_limits(cache)
        if eff["session_pct"] is not None or eff["weekly_pct"] is not None:
            if eff["session_pct"] is not None:
                items.append(pystray.MenuItem(
                    f"Session (5h): {eff['session_pct']:.0f}% used "
                    f"\u2014 resets {fmt_reset_full(eff['session_resets_at'])}",
                    None, enabled=False,
                ))
            if eff["weekly_pct"] is not None:
                items.append(pystray.MenuItem(
                    f"Weekly (7d): {eff['weekly_pct']:.0f}% used "
                    f"\u2014 resets {fmt_reset_full(eff['weekly_resets_at'])}",
                    None, enabled=False,
                ))
            if eff["context_pct"] is not None:
                items.append(pystray.MenuItem(
                    f"Context window: {eff['context_pct']:.0f}% used",
                    None, enabled=False,
                ))
        elif cache:
            # A cache exists but its windows reset (or held unusable values) --
            # a different situation from "the hook never ran".
            items.append(pystray.MenuItem(
                "Session/weekly %: awaiting next Claude Code turn", None, enabled=False,
            ))
        else:
            items.append(pystray.MenuItem("Session/weekly %: not available yet", None, enabled=False))
            items.append(pystray.MenuItem("(set up the statusLine hook \u2014 see README)", None, enabled=False))

        if top_models:
            items.append(pystray.Menu.SEPARATOR)
            items.append(pystray.MenuItem("Top models", None, enabled=False))
            for model, vals in top_models:
                items.append(
                    pystray.MenuItem(f"   {model}: {fmt_tokens(sum(vals.values()))}", None, enabled=False)
                )
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(
            "Floating widget",
            on_toggle_widget,
            checked=lambda item: widget_visible.is_set(),
        ))
        items.append(pystray.MenuItem(
            "Align to taskbar tray",
            on_toggle_alignment,
            checked=lambda item: ALIGNMENT_CONFIG.enabled,
        ))
        items.append(pystray.MenuItem(
            "Run on Windows startup",
            on_toggle_startup,
            checked=lambda item: is_startup_enabled(),
            enabled=lambda item: sys.platform.startswith("win"),
        ))
        items.append(pystray.MenuItem(
            "Notify when Claude needs input",
            on_toggle_notify,
            checked=lambda item: read_notify_pref(),
        ))
        items.append(pystray.MenuItem("Save current position as favorite", on_save_favorite))
        items.append(pystray.MenuItem(
            "Load favorite position",
            on_load_favorite,
            enabled=lambda item: read_usage_cache(cache_path=WIDGET_FAVORITE_POS_PATH) is not None,
        ))
        items.append(pystray.MenuItem("Refresh now", on_refresh))
        items.append(pystray.MenuItem("Open logs folder", on_open_folder))
        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem(f"Claudebar v{__version__}", None, enabled=False))
        items.append(pystray.MenuItem("Quit", on_quit))
        return items

    def on_toggle_widget(icon, item):
        if widget_visible.is_set():
            widget_visible.clear()
        else:
            widget_visible.set()

    def on_refresh(icon, item):
        with state_lock:
            tracker.poll()
        icon.title = build_title()

    def on_open_folder(icon, item):
        if os.path.isdir(PROJECTS_DIR):
            if sys.platform.startswith("win"):
                os.startfile(PROJECTS_DIR)  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", PROJECTS_DIR])

    def on_toggle_alignment(icon, item):
        ALIGNMENT_CONFIG.enabled = not ALIGNMENT_CONFIG.enabled
        write_usage_cache({"enabled": ALIGNMENT_CONFIG.enabled}, cache_path=ALIGNMENT_CONFIG_PATH)

    def on_toggle_startup(icon, item):
        # Tray thread. Fails soft -- see set_startup_enabled()/_startup_registry_*.
        # The registry value itself is the source of truth, re-read fresh
        # here and in the checked= lambda above -- no in-memory mirror needed.
        set_startup_enabled(not is_startup_enabled())

    def on_toggle_notify(icon, item):
        # Tray thread. The prefs sidecar is the source of truth, re-read fresh
        # here and in the checked= lambda above -- no in-memory mirror needed
        # (same shape as on_toggle_startup).
        write_notify_pref(not read_notify_pref())

    def on_save_favorite(icon, item):
        # Tray thread. Reads the widget's position mirror (a plain tuple kept
        # fresh by the Tk thread's _tick()/_end_drag()) -- never calls a
        # Tkinter method directly, per ARCHITECTURE.md §5.
        if widget is None:
            return  # clicked before FloatingWidget() finished constructing
        x, y = widget.last_known_pos
        write_usage_cache({"x": x, "y": y}, cache_path=WIDGET_FAVORITE_POS_PATH)

    def on_load_favorite(icon, item):
        # Tray thread. Never touches widget.root directly -- just signals the
        # Tk thread via an Event, same pattern as should_quit/widget_visible.
        # The actual geometry() call happens inside
        # FloatingWidget._tick() -> _apply_favorite_position().
        if position_pinned.is_set():
            return
        if read_usage_cache(cache_path=WIDGET_FAVORITE_POS_PATH) is None:
            return
        load_favorite_requested.set()

    def on_quit(icon, item):
        should_quit.set()
        icon.stop()

    class SessionFileHandler(FileSystemEventHandler):
        def __init__(self, icon):
            self.icon = icon

        def _handle(self, path):
            is_session_file = path.endswith(".jsonl")
            # Exact-path match (not basename): a stray file with the same name
            # elsewhere in the tree must not masquerade as one of our caches.
            norm = os.path.normcase(os.path.abspath(path))
            is_usage_cache = norm == os.path.normcase(os.path.abspath(USAGE_CACHE_PATH))
            is_state_cache = norm == os.path.normcase(os.path.abspath(STATE_CACHE_PATH))
            if not (is_session_file or is_usage_cache or is_state_cache):
                return
            if is_session_file:
                with state_lock:
                    tracker.poll_file(path)
            if is_state_cache:
                apply_icon_state(self.icon)   # recolor the RAG pip promptly
            self.icon.title = build_title()

        def on_modified(self, event):
            if not event.is_directory:
                self._handle(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                self._handle(event.src_path)

        def on_moved(self, event):
            # The caches are written atomically (temp file + os.replace), and
            # on Windows that replace surfaces as a single "moved" event whose
            # dest_path is the real cache file — no modified/created ever fires
            # on the target path. Without this handler, cache updates only
            # landed on the 30s fallback sweep (a visibly delayed toast/pip).
            if not event.is_directory:
                self._handle(event.dest_path)

    def watcher_loop(icon):
        observer = None       # watches PROJECTS_DIR recursively for session files
        home_observer = None  # watches CLAUDE_HOME (non-recursive) for the usage cache
        while True:
            if observer is None and os.path.isdir(PROJECTS_DIR):
                observer = Observer()
                observer.schedule(SessionFileHandler(icon), PROJECTS_DIR, recursive=True)
                observer.start()
            if home_observer is None and os.path.isdir(CLAUDE_HOME):
                home_observer = Observer()
                home_observer.schedule(SessionFileHandler(icon), CLAUDE_HOME, recursive=False)
                home_observer.start()
            time.sleep(WATCHER_RETRY_SECONDS)

    def fallback_sweep_loop(icon):
        while True:
            time.sleep(FALLBACK_SWEEP_SECONDS)
            # Walk the tree OUTSIDE the lock — on a big history the walk is
            # the slow part, and holding state_lock through it would stall
            # the widget's once-per-second snapshot read.
            files = find_session_files(PROJECTS_DIR)
            with state_lock:
                tracker.poll(files)
            icon.title = build_title()
            apply_icon_state(icon)   # safety-net recolor (e.g. rolls to dim when stale)

    with state_lock:
        tracker.poll()  # initial full scan so the tray isn't empty on first open

    icon = pystray.Icon(
        "claudebar",
        icon=make_icon_image(current_state_tag()),
        title=build_title(),
        menu=pystray.Menu(menu_items),
    )
    _icon_state["tag"] = current_state_tag()   # seed so apply_icon_state dedupes

    threading.Thread(target=watcher_loop, args=(icon,), daemon=True).start()
    threading.Thread(target=fallback_sweep_loop, args=(icon,), daemon=True).start()

    # The tray icon runs detached (its own background thread) so the main
    # thread is free to run the floating widget's Tk mainloop -- Tkinter
    # needs to own the main thread, pystray's run_detached() exists
    # specifically to support that kind of integration.
    icon.run_detached()
    widget = FloatingWidget()
    widget.mainloop()


# --------------------------------------------------------------------------
# Built-in test suite — run with: python claudebar.py --test
# Exercises the data layer (and the live filesystem watcher, if the
# `watchdog` package is installed) against a synthetic projects tree.
# No tray/GUI dependencies required.
# --------------------------------------------------------------------------

def run_tests():
    import shutil

    # The suite is assert-based; under `python -O` every assert is stripped
    # and the whole run would silently "pass" without testing anything.
    if not __debug__:
        raise SystemExit(
            "--test requires asserts to be enabled: run without python's -O/-OO flags."
        )

    # VERSION (read by the release workflow to tag builds) must match the
    # in-code __version__, or a release ships mislabelled.
    _version_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
    if os.path.isfile(_version_file):
        with open(_version_file, encoding="utf-8") as _vf:
            _file_version = _vf.read().strip()
        assert _file_version == __version__, (
            f"VERSION file ({_file_version!r}) and __version__ ({__version__!r}) drifted"
        )
        print("VERSION file matches __version__: OK")

    tmp_home = tempfile.mkdtemp()
    projects_dir = os.path.join(tmp_home, "projects")

    def write_lines(path, lines):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def make_assistant_line(uuid, model, ts, usage, session_id="sess-1"):
        return {
            "type": "assistant",
            "uuid": uuid,
            "sessionId": session_id,
            "timestamp": ts,
            "message": {"model": model, "usage": usage},
        }

    try:
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        yesterday_iso = "2020-01-01T00:00:00.000Z"

        proj_a = os.path.join(projects_dir, "-home-user-projA")
        proj_b = os.path.join(projects_dir, "-home-user-projB")
        file_a = os.path.join(proj_a, "session1.jsonl")
        file_b = os.path.join(proj_b, "session2.jsonl")

        write_lines(file_a, [
            make_assistant_line("u1", "claude-sonnet-4-6", today_iso,
                                 {"input_tokens": 100, "output_tokens": 50,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}),
            {"type": "user", "uuid": "u-irrelevant"},  # should be ignored
            make_assistant_line("u2", "claude-sonnet-4-6", yesterday_iso,
                                 {"input_tokens": 200, "output_tokens": 80,
                                  "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000}),
        ])
        write_lines(file_b, [
            make_assistant_line("u3", "claude-opus-4-8", today_iso,
                                 {"input_tokens": 10, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                                 session_id="sess-2"),
        ])

        tracker = UsageTracker(projects_dir=projects_dir)
        tracker.poll()
        snap = tracker.snapshot()

        assert snap["totals"]["input_tokens"] == 310, snap["totals"]
        assert snap["totals"]["output_tokens"] == 150, snap["totals"]
        assert snap["session_count"] == 2, snap["session_count"]
        assert snap["today_totals"]["input_tokens"] == 110, snap["today_totals"]  # u1 (100) + u3 (10)
        assert "claude-opus-4-8" in snap["by_model"]
        assert "-home-user-projA" in snap["by_project"]
        assert snap["cost_total"] > 0
        print("Initial aggregation: OK")
        print("  totals:", snap["totals"])
        print("  today_totals:", snap["today_totals"])
        print("  cost_total: $%.6f  cost_today: $%.6f" % (snap["cost_total"], snap["cost_today"]))

        tracker.poll()
        snap2 = tracker.snapshot()
        assert snap2["totals"] == snap["totals"], "Re-polling unchanged files must not double count"
        print("Idempotent re-poll: OK")

        write_lines(file_a, [
            make_assistant_line("u4", "claude-sonnet-4-6", today_iso,
                                 {"input_tokens": 5, "output_tokens": 5,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}),
        ])
        tracker.poll()
        snap3 = tracker.snapshot()
        assert snap3["totals"]["input_tokens"] == 315, snap3["totals"]
        print("Incremental append picked up: OK")

        write_lines(file_b, [
            make_assistant_line("u3", "claude-opus-4-8", today_iso,
                                 {"input_tokens": 10, "output_tokens": 20,
                                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
                                 session_id="sess-2"),
        ])
        tracker.poll()
        snap4 = tracker.snapshot()
        assert snap4["totals"]["input_tokens"] == 315, snap4["totals"]
        print("Duplicate uuid correctly ignored: OK")

        # --- live filesystem-watcher latency check (skipped if watchdog isn't installed) ---
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            class _Handler(FileSystemEventHandler):
                def __init__(self):
                    self.fired = threading.Event()

                def on_any_event(self, event):
                    if not event.is_directory:
                        self.fired.set()

            handler = _Handler()
            observer = Observer()
            observer.schedule(handler, projects_dir, recursive=True)
            observer.start()
            time.sleep(0.3)  # let the observer attach

            t0 = time.time()
            write_lines(file_a, [
                make_assistant_line("u5", "claude-sonnet-4-6", today_iso,
                                     {"input_tokens": 1, "output_tokens": 1,
                                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}),
            ])
            detected = handler.fired.wait(timeout=3)
            observer.stop()
            observer.join(timeout=2)
            if detected:
                print(f"Filesystem watcher latency: {time.time() - t0:.3f}s — OK")
            else:
                print("Filesystem watcher: no event observed in 3s (may be unsupported in this environment)")
        except ImportError:
            print("watchdog not installed — skipping live watcher latency check")

        # --- atomic cache writes must be observable by the watcher ---
        # write_usage_cache uses temp file + os.replace; on Windows that
        # surfaces as a single "moved" event (dest_path = the cache file) with
        # NO modified/created on the target path. SessionFileHandler therefore
        # handles on_moved via dest_path — without it, cache updates only land
        # on the 30s fallback sweep (delayed toast / tray pip recolor).
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            watch_dir = os.path.join(tmp_home, "atomic_watch")
            os.makedirs(watch_dir, exist_ok=True)
            atomic_target = os.path.join(watch_dir, "claudebar_state_cache.json")
            write_usage_cache({"state": "working"}, cache_path=atomic_target)  # preexisting

            class _AtomicHandler(FileSystemEventHandler):
                """Mirrors SessionFileHandler's event coverage: src_path for
                modified/created, dest_path for moved."""
                def __init__(self):
                    self.fired = threading.Event()

                def _check(self, path):
                    if path and os.path.normcase(os.path.abspath(path)) == \
                            os.path.normcase(os.path.abspath(atomic_target)):
                        self.fired.set()

                def on_modified(self, event):
                    if not event.is_directory:
                        self._check(event.src_path)

                def on_created(self, event):
                    if not event.is_directory:
                        self._check(event.src_path)

                def on_moved(self, event):
                    if not event.is_directory:
                        self._check(event.dest_path)

            _ah = _AtomicHandler()
            _aobs = Observer()
            _aobs.schedule(_ah, watch_dir, recursive=False)
            _aobs.start()
            time.sleep(0.3)  # let the observer attach
            write_usage_cache({"state": "waiting"}, cache_path=atomic_target)
            _adetected = _ah.fired.wait(timeout=3)
            _aobs.stop()
            _aobs.join(timeout=2)
            if _adetected:
                print("atomic cache write observed by watcher (moved/modified/created): OK")
            else:
                print("atomic cache write: no event observed in 3s (may be unsupported here)")
        except ImportError:
            pass  # already reported by the latency check above

        # --- statusLine rate-limit cache layer ---
        sample_payload = {
            "model": {"display_name": "Claude Sonnet 4.6"},
            "context_window": {"used_percentage": 22.4},
            "rate_limits": {
                "five_hour": {"used_percentage": 34.0, "resets_at": time.time() + 3600},
                "seven_day": {"used_percentage": 12.5, "resets_at": time.time() + 86400 * 2},
            },
        }
        cache, status_text = process_statusline_payload(sample_payload)
        assert cache["session_used_percentage"] == 34.0, cache
        assert cache["weekly_used_percentage"] == 12.5, cache
        assert cache["context_used_percentage"] == 22.4, cache
        assert "Sonnet" in status_text and "5h 34%" in status_text and "7d 12%" in status_text, status_text
        print("statusLine payload parsing: OK")
        print("  status line text:", status_text)

        cache_path = os.path.join(tmp_home, "claudebar_cache.json")
        write_usage_cache(cache, cache_path=cache_path)
        read_back = read_usage_cache(cache_path=cache_path)
        assert read_back == cache, (read_back, cache)
        print("Usage cache write/read round-trip: OK")

        empty_cache, empty_text = process_statusline_payload({})
        assert empty_cache["session_used_percentage"] is None
        assert empty_text == "Claude"
        print("statusLine payload with missing fields handled gracefully: OK")

        missing = read_usage_cache(cache_path=os.path.join(tmp_home, "does-not-exist.json"))
        assert missing is None
        print("Reading a missing usage cache returns None: OK")

        # --- reset-time formatting ---
        future_ts = time.time() + 3725  # 1h 2m from now
        assert fmt_reset_relative(future_ts) == "1h 2m", fmt_reset_relative(future_ts)
        assert fmt_reset_relative(None) is None
        assert fmt_reset_clock(None) is None
        assert fmt_reset_full(None) == "unknown"
        assert "in 1h 2m" in fmt_reset_full(future_ts)
        print("Reset-time formatting: OK")

        # --- floating-widget pure helpers ---
        assert pct_tag(10) == "green"
        assert pct_tag(60) == "yellow"
        assert pct_tag(95) == "red"
        assert pct_tag(None) == "dim"
        # corrupted cache values degrade to dim, never raise (a TypeError here
        # once could have frozen the widget's _tick loop permanently)
        assert pct_tag("95") == "dim"
        assert pct_tag({"pct": 5}) == "dim"
        assert pct_tag(True) == "dim"
        print("Widget color-threshold helper (incl. garbage input): OK")

        # --- local-date attribution (UTC JSONL timestamps vs local "today") ---
        # 23:30 UTC on Jan 1 is Jan 2 in UTC+2 — the naive [:10] prefix
        # comparison misattributed exactly this window every night.
        _d = record_local_date("2026-01-01T23:30:00.000Z")
        _expected = (
            datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc).astimezone().date()
        )
        assert _d == _expected, (_d, _expected)
        assert record_local_date(None) is None
        assert record_local_date("") is None
        assert record_local_date("not-a-timestamp") is None
        print("record_local_date — UTC-to-local conversion + garbage input: OK")

        # --- effective_rate_limits: staleness + type sanitisation ---
        _now = 1_000_000.0
        _fresh = {
            "model": "Claude Sonnet 4.6",
            "context_used_percentage": 20.0,
            "session_used_percentage": 34.0, "session_resets_at": _now + 3600,
            "weekly_used_percentage": 12.5, "weekly_resets_at": _now + 86400,
        }
        eff = effective_rate_limits(_fresh, now=_now)
        assert eff["session_pct"] == 34.0 and eff["weekly_pct"] == 12.5, eff
        assert eff["context_pct"] == 20.0 and eff["model"] == "Claude Sonnet 4.6"
        # a window whose resets_at has passed is definitionally over — both
        # fields null out until the next Claude turn writes fresh numbers
        _reset = dict(_fresh, session_resets_at=_now - 10)
        eff = effective_rate_limits(_reset, now=_now)
        assert eff["session_pct"] is None and eff["session_resets_at"] is None, eff
        assert eff["weekly_pct"] == 12.5, eff   # weekly window still live
        # non-numeric garbage degrades to None, never raises
        _garbage = {"session_used_percentage": "90", "session_resets_at": [],
                    "weekly_used_percentage": True, "model": 7}
        eff = effective_rate_limits(_garbage, now=_now)
        assert eff["session_pct"] is None and eff["weekly_pct"] is None, eff
        assert eff["model"] is None
        assert effective_rate_limits(None)["session_pct"] is None
        print("effective_rate_limits — staleness + type sanitisation: OK")

        # --- process_statusline_payload: type-hostile payload must not raise ---
        _bad_cache, _bad_text = process_statusline_payload({
            "model": "not-a-dict",
            "context_window": {"used_percentage": "22"},
            "rate_limits": {"five_hour": {"used_percentage": None, "resets_at": "soon"}},
        })
        assert _bad_cache["session_used_percentage"] is None
        assert _bad_cache["context_used_percentage"] is None
        assert _bad_text == "Claude", _bad_text
        _bad_cache2, _ = process_statusline_payload("not even a dict")
        assert _bad_cache2["model"] == "Claude"
        print("process_statusline_payload — type-hostile payload handled: OK")

        # --- clamp_position: keep a remembered position on screen ---
        _bounds = (0, 0, 1920, 1080)
        assert clamp_position(100, 200, 300, 50, _bounds) == (100, 200)   # already inside
        assert clamp_position(5000, 200, 300, 50, _bounds) == (1620, 200)  # off right
        assert clamp_position(-500, -50, 300, 50, _bounds) == (0, 0)       # off left/top
        assert clamp_position(100, 2000, 300, 50, _bounds) == (100, 1030)  # off bottom
        # negative-origin virtual screen (monitor left of primary)
        assert clamp_position(-3000, 0, 300, 50, (-1920, 0, 1920, 1080)) == (-1920, 0)
        print("clamp_position — clamps into virtual-screen bounds: OK")

        # --- single-instance lock (Windows: real mutex; elsewhere: None) ---
        if sys.platform.startswith("win"):
            _lock_name = f"Local\\ClaudebarTest-{os.getpid()}"
            assert acquire_single_instance_lock(_lock_name) is True
            # second acquisition (same name, held by this very process) must
            # report "already running"
            assert acquire_single_instance_lock(_lock_name) is False
            print("single-instance mutex — acquire once, second try refused: OK")
        else:
            assert acquire_single_instance_lock() is None
            print("single-instance lock degrades to None off-Windows: OK")

        # --- statusLine hook command construction (pure, frozen-aware) ---
        exe = r"C:\Apps\Claudebar.exe"
        script = r"C:\code\claudebar.py"
        frozen_cmd = build_hook_command(True, exe, script)
        assert frozen_cmd == f'"{exe}" --statusline-hook', frozen_cmd
        py = r"C:\Python312\python.exe"
        nonfrozen_cmd = build_hook_command(False, py, script)
        assert nonfrozen_cmd == f'"{py}" "{script}" --statusline-hook', nonfrozen_cmd
        assert "python" not in frozen_cmd.lower()  # frozen exe never invokes python
        print("build_hook_command (frozen exe vs. python script): OK")

        # --- statusLine hook installation ---
        expected_cmd = build_hook_command(False, "python", "/path/to/claudebar.py")
        expected_block = {"type": "command", "command": expected_cmd}

        # Case 1: fresh install (no settings.json)
        settings_path = os.path.join(tmp_home, "settings_fresh.json")
        ok, msg = install_statusline_hook(settings_path, expected_cmd)
        assert ok, msg
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["statusLine"] == expected_block, data
        print("hook install — fresh file: OK")

        # Case 2: existing settings with unrelated keys (keys must be preserved)
        settings_path2 = os.path.join(tmp_home, "settings_merge.json")
        with open(settings_path2, "w", encoding="utf-8") as f:
            json.dump({"theme": "dark", "autoUpdates": False}, f)
        ok, msg = install_statusline_hook(settings_path2, expected_cmd)
        assert ok, msg
        with open(settings_path2, encoding="utf-8") as f:
            data2 = json.load(f)
        assert data2["theme"] == "dark"
        assert data2["autoUpdates"] is False
        assert data2["statusLine"] == expected_block
        print("hook install — merges with existing keys: OK")

        # Case 3: already configured with the same command (no-op)
        ok3, msg3 = install_statusline_hook(settings_path2, expected_cmd)
        assert ok3, msg3
        assert "already" in msg3.lower()
        print("hook install — already configured, no-op: OK")

        # Case 4: existing statusLine with a different command (force overwrites)
        settings_path3 = os.path.join(tmp_home, "settings_overwrite.json")
        with open(settings_path3, "w", encoding="utf-8") as f:
            json.dump({"statusLine": {"type": "command", "command": "old-cmd"}}, f)
        ok4, msg4 = install_statusline_hook(settings_path3, expected_cmd, force=True)
        assert ok4, msg4
        with open(settings_path3, encoding="utf-8") as f:
            data3 = json.load(f)
        assert data3["statusLine"] == expected_block
        assert "old-cmd" in msg4  # message must mention the old value
        print("hook install — force overwrites different command: OK")

        # Case 4b: force=False must NOT clobber a foreign statusLine
        settings_path3b = os.path.join(tmp_home, "settings_foreign.json")
        foreign = {"statusLine": {"type": "command", "command": "someone-elses-cmd"}}
        with open(settings_path3b, "w", encoding="utf-8") as f:
            json.dump(foreign, f)
        ok4b, msg4b = install_statusline_hook(settings_path3b, expected_cmd, force=False)
        assert ok4b, msg4b
        with open(settings_path3b, encoding="utf-8") as f:
            data3b = json.load(f)
        assert data3b["statusLine"]["command"] == "someone-elses-cmd", data3b
        print("hook install — force=False leaves a foreign statusLine untouched: OK")

        # Case 5: malformed settings.json (abort without writing)
        settings_path4 = os.path.join(tmp_home, "settings_broken.json")
        broken_content = '{"broken": true,}'  # trailing comma — invalid JSON
        with open(settings_path4, "w", encoding="utf-8") as f:
            f.write(broken_content)
        ok5, msg5 = install_statusline_hook(settings_path4, expected_cmd)
        assert not ok5, "should have failed on malformed JSON"
        # File must be byte-identical (aborted without writing anything)
        with open(settings_path4, encoding="utf-8") as f:
            raw = f.read()
        assert raw == broken_content, raw
        print("hook install — malformed JSON aborted cleanly: OK")

        # Case 6: an ours-shaped statusLine (older install path, same
        # --statusline-hook flag) is upgraded in place even without force
        settings_path5 = os.path.join(tmp_home, "settings_upgrade.json")
        old_ours = '"C:\\OldPlace\\Claudebar.exe" --statusline-hook'
        with open(settings_path5, "w", encoding="utf-8") as f:
            json.dump({"statusLine": {"type": "command", "command": old_ours}}, f)
        ok6, _ = install_statusline_hook(settings_path5, expected_cmd, force=False)
        assert ok6
        assert _current_statusline_command(settings_path5) == expected_cmd
        print("hook install — force=False upgrades our own older command: OK")

        # --- ensure_hook_installed: auto (force=False) vs. force, plus marker ---
        auto_settings = os.path.join(tmp_home, "settings_auto.json")
        auto_marker = os.path.join(tmp_home, "hook_marker.json")
        # (a) fresh: no statusLine -> installs ours + writes the marker
        okA, _ = ensure_hook_installed(auto_settings, expected_cmd, auto_marker, force=False)
        assert okA
        assert _current_statusline_command(auto_settings) == expected_cmd
        marker = read_hook_marker(marker_path=auto_marker)
        assert marker and marker.get("installed") is True
        assert marker.get("command") == expected_cmd
        print("ensure_hook_installed — auto install on empty settings + marker: OK")
        # (b) idempotent: running again changes nothing, marker still ours
        okB, _ = ensure_hook_installed(auto_settings, expected_cmd, auto_marker, force=False)
        assert okB and _current_statusline_command(auto_settings) == expected_cmd
        print("ensure_hook_installed — idempotent when already ours: OK")
        # (c) foreign statusLine: auto path must NOT write our marker or clobber
        foreign_settings = os.path.join(tmp_home, "settings_auto_foreign.json")
        foreign_marker = os.path.join(tmp_home, "hook_marker_foreign.json")
        with open(foreign_settings, "w", encoding="utf-8") as f:
            json.dump({"statusLine": {"type": "command", "command": "mine"}}, f)
        okC, _ = ensure_hook_installed(foreign_settings, expected_cmd, foreign_marker, force=False)
        assert okC and _current_statusline_command(foreign_settings) == "mine"
        assert read_hook_marker(marker_path=foreign_marker) is None
        print("ensure_hook_installed — foreign statusLine left intact, no marker: OK")
        # (d) force: overwrites the foreign command and now writes our marker
        okD, _ = ensure_hook_installed(foreign_settings, expected_cmd, foreign_marker, force=True)
        assert okD and _current_statusline_command(foreign_settings) == expected_cmd
        assert read_hook_marker(marker_path=foreign_marker).get("command") == expected_cmd
        print("ensure_hook_installed — force overwrites foreign + writes marker: OK")

        # --- statusLine hook stdin/stdout round-trip (raw-fd path fallback) ---
        _hook_payload = json.dumps(
            {"model": {"display_name": "hooktest"},
             "rate_limits": {"five_hour": {"used_percentage": 50, "resets_at": 1782500000}}}
        )
        _hcache, _hline = process_statusline_payload(json.loads(_hook_payload))
        assert "hooktest" in _hline and "5h 50%" in _hline, _hline
        print("statusline payload -> status line text: OK")

        # --- working-state indicator: event -> state mapping ---
        assert derive_working_state({"hook_event_name": "UserPromptSubmit"}) == "working"
        assert derive_working_state({"hook_event_name": "PreToolUse"}) == "working"
        assert derive_working_state({"hook_event_name": "PostToolUse"}) == "working"
        assert derive_working_state({"hook_event_name": "Stop"}) == "done"
        assert derive_working_state({"hook_event_name": "Notification"}) == "waiting"
        assert derive_working_state({"hook_event_name": "SessionStart"}) is None
        # SessionEnd carries no *state* — update_state_cache handles it as a
        # session removal instead
        assert derive_working_state({"hook_event_name": "SessionEnd"}) is None
        assert derive_working_state({}) is None           # missing field -> no state
        assert derive_working_state(None) is None
        print("derive_working_state — event mapping + missing field: OK")

        # --- working_state_tag / label: colour + staleness ---
        _now = 1_000_000.0
        assert working_state_tag("working", _now, _now) == "yellow"
        assert working_state_tag("done", _now, _now) == "green"
        assert working_state_tag("waiting", _now, _now) == "red"
        assert working_state_tag(None, _now, _now) == "dim"
        assert working_state_tag("bogus", _now, _now) == "dim"
        # older than STATE_STALE_SECONDS -> dim, regardless of state
        assert working_state_tag("working", _now - STATE_STALE_SECONDS - 1, _now) == "dim"
        assert working_state_tag("working", None) == "yellow"   # no timestamp -> never stale
        assert working_state_label("waiting", _now, _now) == "waiting for you"
        assert working_state_label("working", _now - STATE_STALE_SECONDS - 1, _now) == "—"
        print("working_state_tag/label — colour + staleness: OK")

        # --- per-session state cache: update + aggregate ---
        # session A starts working
        _sc1 = update_state_cache(None, {"hook_event_name": "PreToolUse", "session_id": "A"}, now=_now)
        assert _sc1["sessions"]["A"]["state"] == "working", _sc1
        assert _sc1["state"] == "working"
        # session B finishes while A is still working -> aggregate stays working
        _sc2 = update_state_cache(_sc1, {"hook_event_name": "Stop", "session_id": "B"}, now=_now + 1)
        assert _sc2["sessions"]["B"]["state"] == "done"
        assert _sc2["state"] == "working", _sc2   # A's amber outlives B's green
        # waiting (red, "needs you") beats working
        _sc3 = update_state_cache(_sc2, {"hook_event_name": "Notification", "session_id": "B"}, now=_now + 2)
        assert _sc3["state"] == "waiting", _sc3
        # SessionEnd removes the session entirely
        _sc4 = update_state_cache(_sc3, {"hook_event_name": "SessionEnd", "session_id": "B"}, now=_now + 3)
        assert "B" not in _sc4["sessions"], _sc4
        assert _sc4["state"] == "working"          # only A remains
        _sc5 = update_state_cache(_sc4, {"hook_event_name": "SessionEnd", "session_id": "A"}, now=_now + 4)
        assert _sc5["sessions"] == {} and _sc5["state"] is None, _sc5
        # unmapped events leave the cache alone (None = don't write)
        assert update_state_cache(_sc4, {"hook_event_name": "SessionStart", "session_id": "A"}) is None
        assert update_state_cache(_sc4, {}) is None
        # stale entries are pruned on the next write
        _stale = {"sessions": {"old": {"state": "working",
                                       "updated_at": _now - STATE_STALE_SECONDS - 5}}}
        _sc6 = update_state_cache(_stale, {"hook_event_name": "Stop", "session_id": "C"}, now=_now)
        assert "old" not in _sc6["sessions"] and _sc6["state"] == "done", _sc6
        # missing session_id falls back to a shared slot, never crashes
        _sc7 = update_state_cache(None, {"hook_event_name": "Stop"}, now=_now)
        assert _sc7["sessions"]["unknown"]["state"] == "done", _sc7
        print("update_state_cache — per-session map, priority, SessionEnd, pruning: OK")

        # aggregate_working_state: legacy single-slot cache still readable
        assert aggregate_working_state(
            {"state": "waiting", "updated_at": _now}, now=_now
        ) == ("waiting", _now)
        assert aggregate_working_state(
            {"state": "working", "updated_at": _now - STATE_STALE_SECONDS - 1}, now=_now
        ) == (None, None)
        assert aggregate_working_state(None, now=_now) == (None, None)
        assert aggregate_working_state({"sessions": {"x": "garbage"}}, now=_now) == (None, None)
        print("aggregate_working_state — legacy fallback + garbage tolerance: OK")

        # --- should_notify_waiting: only the transition INTO red fires ---
        assert should_notify_waiting("yellow", "red")          # working -> waiting
        assert should_notify_waiting("green", "red")           # done -> waiting
        assert should_notify_waiting("dim", "red")             # unknown -> waiting
        assert should_notify_waiting(None, "red")              # first-ever observation
        assert not should_notify_waiting("red", "red")         # stays waiting: no re-fire
        assert not should_notify_waiting("red", "yellow")      # leaving red is silent
        assert not should_notify_waiting("red", "dim")         # going stale is silent
        assert not should_notify_waiting("yellow", "green")    # non-red transitions silent
        # waiting -> working -> waiting notifies again (re-armed by leaving red)
        _tags = ["yellow", "red", "yellow", "red"]
        _fires = [should_notify_waiting(a, b) for a, b in zip(_tags, _tags[1:])]
        assert _fires == [True, False, True], _fires
        print("should_notify_waiting — transition-only, re-arm on leaving red: OK")

        # --- notify pref sidecar: default, round-trip, corruption fallback ---
        prefs_path = os.path.join(tmp_home, "prefs.json")
        assert read_notify_pref(prefs_path=prefs_path) is True   # missing file -> default on
        write_notify_pref(False, prefs_path=prefs_path)
        assert read_notify_pref(prefs_path=prefs_path) is False
        write_notify_pref(True, prefs_path=prefs_path)
        assert read_notify_pref(prefs_path=prefs_path) is True
        # other keys in the sidecar survive a toggle write
        write_usage_cache({"notify_on_waiting": False, "future_key": 7}, cache_path=prefs_path)
        write_notify_pref(True, prefs_path=prefs_path)
        _prefs = read_usage_cache(cache_path=prefs_path)
        assert _prefs == {"notify_on_waiting": True, "future_key": 7}, _prefs
        # corrupted file / wrong-typed value degrade to the default, no crash
        with open(prefs_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert read_notify_pref(prefs_path=prefs_path) is True
        write_usage_cache({"notify_on_waiting": "yes"}, cache_path=prefs_path)
        assert read_notify_pref(prefs_path=prefs_path) is True
        print("notify pref sidecar — default, round-trip, corruption fallback: OK")

        # --- state cache write/read round-trip (temp path, never real ~/.claude) ---
        state_cache_path = os.path.join(tmp_home, "state_cache.json")
        write_usage_cache(
            {"state": "working", "event": "PreToolUse", "session_id": "s1", "updated_at": _now},
            cache_path=state_cache_path,
        )
        _sc = read_usage_cache(cache_path=state_cache_path)
        assert _sc["state"] == "working" and _sc["event"] == "PreToolUse", _sc
        print("state cache write/read round-trip: OK")

        # --- state-hook command construction (pure, frozen-aware) ---
        _sframe = build_state_hook_command(True, exe, script)
        assert _sframe == f'"{exe}" --state-hook', _sframe
        _spy = build_state_hook_command(False, py, script)
        assert _spy == f'"{py}" "{script}" --state-hook', _spy
        assert "python" not in _sframe.lower()
        print("build_state_hook_command (frozen exe vs. python script): OK")

        # --- install_state_hooks: register all events, idempotent, non-destructive ---
        state_cmd = build_state_hook_command(False, "python", "/path/to/claudebar.py")
        # (a) fresh install into a settings file that already has a foreign statusLine
        #     and an unrelated user hook -> both must survive untouched.
        st_settings = os.path.join(tmp_home, "settings_state.json")
        with open(st_settings, "w", encoding="utf-8") as f:
            json.dump({
                "statusLine": {"type": "command", "command": "user-status"},
                "hooks": {"Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": "user-own-stop-hook"}]}]},
            }, f)
        okS, msgS = install_state_hooks(st_settings, state_cmd)
        assert okS, msgS
        with open(st_settings, encoding="utf-8") as f:
            sdata = json.load(f)
        assert sdata["statusLine"]["command"] == "user-status", sdata   # untouched
        for ev in STATE_HOOK_EVENTS:
            cmds = [h.get("command")
                    for g in sdata["hooks"][ev] for h in (g.get("hooks") or [])]
            assert state_cmd in cmds, (ev, cmds)
        # the user's own Stop hook must still be there alongside ours
        stop_cmds = [h.get("command")
                     for g in sdata["hooks"]["Stop"] for h in (g.get("hooks") or [])]
        assert "user-own-stop-hook" in stop_cmds and state_cmd in stop_cmds, stop_cmds
        print("install_state_hooks — registers all events, preserves user hooks + statusLine: OK")

        # (b) idempotent: a second call changes nothing and reports so
        okS2, msgS2 = install_state_hooks(st_settings, state_cmd)
        assert okS2 and "already" in msgS2.lower(), msgS2
        with open(st_settings, encoding="utf-8") as f:
            sdata2 = json.load(f)
        # no duplicate groups added for any event
        for ev in STATE_HOOK_EVENTS:
            our = [1 for g in sdata2["hooks"][ev] for h in (g.get("hooks") or [])
                   if h.get("command") == state_cmd]
            assert sum(our) == 1, (ev, sdata2["hooks"][ev])
        print("install_state_hooks — idempotent, no duplicates: OK")

        # (c) malformed settings.json -> abort without writing
        st_broken = os.path.join(tmp_home, "settings_state_broken.json")
        with open(st_broken, "w", encoding="utf-8") as f:
            f.write('{"hooks": {},}')  # trailing comma -> invalid
        okS3, _ = install_state_hooks(st_broken, state_cmd)
        assert not okS3, "should refuse to write a malformed settings.json"
        print("install_state_hooks — malformed JSON aborted cleanly: OK")

        # (d) ensure_state_hooks_installed writes the marker once fully installed
        st_marker = os.path.join(tmp_home, "state_hooks_marker.json")
        okS4, _ = ensure_state_hooks_installed(st_settings, state_cmd, st_marker)
        assert okS4 and _state_hooks_installed(st_settings, state_cmd)
        _m = read_state_hooks_marker(marker_path=st_marker)
        assert _m and _m.get("command") == state_cmd, _m
        print("ensure_state_hooks_installed — marker written when in place: OK")

        # (e) upgrade: an older ours-shaped command (different path, same
        # --state-hook flag) is rewritten in place — NOT left to run alongside
        # a freshly-appended duplicate — and our command is retired from
        # events we no longer register (PostToolUse), while the user's own
        # hook on that event survives.
        st_upgrade = os.path.join(tmp_home, "settings_state_upgrade.json")
        old_state_cmd = '"C:\\OldPlace\\Claudebar.exe" --state-hook'
        with open(st_upgrade, "w", encoding="utf-8") as f:
            json.dump({"hooks": {
                "Stop": [{"matcher": "", "hooks": [
                    {"type": "command", "command": old_state_cmd}]}],
                "PostToolUse": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": old_state_cmd}]},
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "user-posttool-hook"}]},
                ],
            }}, f)
        okU, _ = install_state_hooks(st_upgrade, state_cmd)
        assert okU
        with open(st_upgrade, encoding="utf-8") as f:
            udata = json.load(f)
        stop_cmds = [h.get("command")
                     for g in udata["hooks"]["Stop"] for h in (g.get("hooks") or [])]
        assert stop_cmds.count(state_cmd) == 1 and old_state_cmd not in stop_cmds, stop_cmds
        ptu_cmds = [h.get("command")
                    for g in udata["hooks"]["PostToolUse"] for h in (g.get("hooks") or [])]
        assert ptu_cmds == ["user-posttool-hook"], ptu_cmds
        for ev in STATE_HOOK_EVENTS:
            evc = [h.get("command")
                   for g in udata["hooks"].get(ev, []) for h in (g.get("hooks") or [])]
            assert evc.count(state_cmd) == 1, (ev, evc)
        print("install_state_hooks — upgrades our old command, retires PostToolUse, keeps user hooks: OK")

        # (f) a PostToolUse entry that is ONLY ours disappears along with its event key
        st_prune = os.path.join(tmp_home, "settings_state_prune.json")
        with open(st_prune, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PostToolUse": [{"matcher": "", "hooks": [
                {"type": "command", "command": old_state_cmd}]}]}}, f)
        okP, _ = install_state_hooks(st_prune, state_cmd)
        assert okP
        with open(st_prune, encoding="utf-8") as f:
            pdata = json.load(f)
        assert "PostToolUse" not in pdata["hooks"], pdata["hooks"].get("PostToolUse")
        print("install_state_hooks — fully-ours PostToolUse registration removed: OK")

        # --- slim hook exe installation (pure copy logic, temp dirs only) ---
        fake_bundle_dir = os.path.join(tmp_home, "bundle")
        os.makedirs(fake_bundle_dir, exist_ok=True)
        fake_bundled = os.path.join(fake_bundle_dir, HOOK_EXE_NAME)
        with open(fake_bundled, "wb") as f:
            f.write(b"hook-exe-v1")
        hook_dest_dir = os.path.join(tmp_home, "bin")
        installed = install_hook_exe(bundled_path=fake_bundled, dest_dir=hook_dest_dir)
        assert installed == os.path.join(hook_dest_dir, HOOK_EXE_NAME), installed
        with open(installed, "rb") as f:
            assert f.read() == b"hook-exe-v1"
        # idempotent: same content -> same path, no rewrite needed
        assert install_hook_exe(bundled_path=fake_bundled, dest_dir=hook_dest_dir) == installed
        # new content -> replaced
        with open(fake_bundled, "wb") as f:
            f.write(b"hook-exe-v2")
        assert install_hook_exe(bundled_path=fake_bundled, dest_dir=hook_dest_dir) == installed
        with open(installed, "rb") as f:
            assert f.read() == b"hook-exe-v2"
        # nothing bundled (plain-python run) -> None, callers fall back to
        # registering the main exe / python command
        assert _bundled_hook_exe_path() is None   # --test never runs frozen
        assert install_hook_exe(bundled_path=None, dest_dir=hook_dest_dir) is None
        print("install_hook_exe — install, idempotent re-run, upgrade, no-bundle fallback: OK")

        # --- taskbar tray-rect detection: must degrade gracefully off-Windows ---
        if not sys.platform.startswith("win"):
            assert find_tray_notification_rect() is None
            print("find_tray_notification_rect() correctly returns None off-Windows: OK")

        # --- Windows startup registration: pure command-string logic ---
        # Pure path/string logic -- runs on any OS, no registry involved.
        cmd = build_startup_command(
            frozen=True, executable=r"C:\Apps\Claudebar.exe",
            script_path=r"C:\Apps\claudebar.py",
        )
        assert cmd == '"C:\\Apps\\Claudebar.exe"', cmd

        cmd = build_startup_command(
            frozen=False, executable=r"C:\Python312\python.exe",
            script_path=r"C:\code\claudebar.py",
        )
        # no real pythonw.exe on disk at that fabricated path -> falls back to executable
        assert cmd == '"C:\\Python312\\python.exe" "C:\\code\\claudebar.py"', cmd
        print("build_startup_command (frozen / non-frozen, no pythonw present): OK")

        # pythonw.exe preference: create a *real* file in the tmp sandbox so
        # os.path.isfile() finds it, without touching any real Python install.
        fake_pyw = os.path.join(tmp_home, "pythonw.exe")
        with open(fake_pyw, "w", encoding="utf-8"):
            pass
        fake_py = os.path.join(tmp_home, "python.exe")
        cmd = build_startup_command(
            frozen=False, executable=fake_py, script_path=r"C:\code\claudebar.py",
        )
        assert cmd == f'"{fake_pyw}" "C:\\code\\claudebar.py"', cmd
        print("build_startup_command prefers pythonw.exe when present alongside python.exe: OK")

        # --- Windows startup registration: real registry round-trip (Windows only) ---
        # Always uses a disposable test subkey, never STARTUP_RUN_SUBKEY (the real
        # HKCU\...\CurrentVersion\Run) -- this must never register the test run itself.
        if sys.platform.startswith("win"):
            try:
                import winreg
                test_subkey = r"Software\ClaudebarTestOnly_DoNotUseForRealStartup"
                test_value_name = "ClaudebarTest"
                try:
                    assert is_startup_enabled(test_value_name, test_subkey) is False

                    ok = set_startup_enabled(
                        True, value_name=test_value_name, subkey=test_subkey,
                        frozen=True, executable=r"C:\fake\test.exe", script_path=r"C:\fake\test.py",
                    )
                    assert ok is True
                    assert is_startup_enabled(test_value_name, test_subkey) is True

                    ok = set_startup_enabled(
                        False, value_name=test_value_name, subkey=test_subkey,
                    )
                    assert ok is True
                    assert is_startup_enabled(test_value_name, test_subkey) is False
                    print("Windows startup registry read/write/delete (disposable test subkey): OK")
                finally:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, test_subkey)
                    except OSError:
                        pass
            except ImportError:
                print("winreg not available — skipping Windows startup registry test")
        else:
            assert is_startup_enabled() is False
            assert set_startup_enabled(True) is False
            print("Windows startup registration degrades gracefully off-Windows: OK")

        # --- z-order position algorithm ---
        # _zorder_position walks a top→bottom HWND chain and reports our position
        # relative to the taskbar.  Tests use fake integer HWNDs.
        OUR, TB, OTHER1, OTHER2 = 100, 200, 300, 400

        assert _zorder_position([OUR, OTHER1, TB], OUR, TB) == "above"
        assert _zorder_position([OTHER1, TB, OUR], OUR, TB) == "below"
        assert _zorder_position([OTHER1, OUR, TB, OTHER2], OUR, TB) == "above"
        # taskbar found but our HWND never appeared before it → we are below
        assert _zorder_position([OTHER1, OTHER2, TB], OUR, TB) == "below"
        # taskbar absent from chain entirely → cannot determine position
        assert _zorder_position([OUR, OTHER1, OTHER2], OUR, TB) == "unknown"
        assert _zorder_position([], OUR, TB) == "unknown"                     # empty chain
        assert _zorder_position([TB, OUR], OUR, TB) == "below"                # taskbar first
        assert _zorder_position([OUR, TB], OUR, TB) == "above"                # adjacent
        print("z-order position algorithm: OK")

        # --- overlay machinery regression guards ---
        # Each test here encodes a specific bug that was introduced during development
        # and cost significant debugging time.  If the fix is ever accidentally reverted,
        # the test name and message say exactly what broke and why.
        import re

        with open(__file__, encoding="utf-8") as _f:
            _src = _f.read()

        def _method_body(name):
            """Extract the body of a method named `name` from the source, stripping docstrings."""
            m = re.search(
                rf"def {re.escape(name)}\(self\):(.*?)(?=\n        def |\Z)",
                _src, re.DOTALL,
            )
            assert m, f"{name} not found in source"
            return re.sub(r'""".*?"""', "", m.group(1), flags=re.DOTALL)

        # Guard 1: _reassert_topmost must use root.attributes(), not ctypes SetWindowPos.
        #
        # Background: Tk's WndProc intercepts WM_WINDOWPOSCHANGING and silently reverts
        # any HWND_TOPMOST promotion that did not originate from Tk's own machinery
        # (it checks wmPtr->flags and cancels external SetWindowPos(-1) calls).
        # Using root.attributes("-topmost") goes through Tk's own path, so it sticks.
        _rt = _method_body("_reassert_topmost")
        assert 'attributes("-topmost", False)' in _rt, \
            "REGRESSION: _reassert_topmost must call root.attributes('-topmost', False) first"
        assert 'attributes("-topmost", True)' in _rt, \
            "REGRESSION: _reassert_topmost must call root.attributes('-topmost', True)"
        assert _rt.index('attributes("-topmost", False)') < _rt.index('attributes("-topmost", True)'), \
            "REGRESSION: _reassert_topmost must set False (NOTOPMOST) before True (TOPMOST); " \
            "SetWindowPos(HWND_TOPMOST) on an already-topmost window is a z-order no-op"
        assert "windll.user32.SetWindowPos" not in _rt, \
            "REGRESSION: _reassert_topmost must not call ctypes.SetWindowPos directly — " \
            "Tk's WndProc vetoes it, leaving TOPMOST=False every time"
        print("_reassert_topmost uses root.attributes (not ctypes SetWindowPos): OK")

        # Guard 2: _apply_overlay_styles must NOT call root.attributes("-topmost") synchronously.
        #
        # Background: calling root.attributes("-topmost", True) inside _apply_overlay_styles()
        # triggers Tk's SetWindowPos internally while the geometry from _place_initial() is
        # still queued.  The synchronous Win32 message processing during that SetWindowPos
        # cancels the pending window move, leaving the widget stuck at (0, 0) permanently.
        _ao = _method_body("_apply_overlay_styles")
        # Strip comment-only lines before scanning for attribute calls (comments
        # in the method body may legitimately mention the attribute by name)
        _ao_code = "\n".join(
            ln for ln in _ao.splitlines() if not ln.lstrip().startswith("#")
        )
        direct_topmost = [c for c in re.findall(r'.{0,60}attributes\("-topmost"', _ao_code)
                          if "after(" not in c]
        assert not direct_topmost, \
            "REGRESSION: _apply_overlay_styles must not call root.attributes('-topmost') " \
            "synchronously — it cancels the pending geometry from _place_initial(), " \
            f"locking the widget at (0,0). Found: {direct_topmost}"
        print("_apply_overlay_styles has no synchronous root.attributes(-topmost) call: OK")

        # Guard 3: FloatingWidget.__init__ must flush geometry to Win32 between
        # _place_initial() and _apply_overlay_styles().
        #
        # Background: root.geometry() queues the window move but does not immediately
        # call SetWindowPos.  Without update_idletasks() to flush the queue first,
        # the Win32 message processing inside _apply_overlay_styles() (specifically
        # SetWindowLongW posting WM_STYLECHANGED) cancels the queued move.
        # Result: the widget is positioned correctly in Tk's internal state but sits
        # at (0, 0) in Win32 / GetWindowRect — permanently.
        #
        # We scan the source text directly between the two call sites rather than
        # trying to extract __init__'s body via regex (the file has multiple nested
        # __init__ definitions at the same indentation level which confuse the extractor).
        _pi_src = _src.find("self._place_initial()")
        _ao_src = _src.find("self._apply_overlay_styles()")
        assert 0 < _pi_src < _ao_src, \
            "_place_initial() must appear before _apply_overlay_styles() in source"
        _between_calls = _src[_pi_src:_ao_src]
        assert "update_idletasks()" in _between_calls, \
            "REGRESSION: __init__ must call update_idletasks() between _place_initial() " \
            "and _apply_overlay_styles() — without it, the pending geometry move is " \
            "cancelled by Win32 message processing during style setup, leaving widget at (0,0)"
        print("__init__ flushes geometry before _apply_overlay_styles: OK")

        # --- favorite-position feature: cross-thread-safety guards ---
        # Unlike Guards 1-3 above, these don't encode a bug that actually
        # happened here — they encode CLAUDE.md's "no cross-thread Tkinter
        # calls" rule preemptively for this new feature, so a future refactor
        # can't casually reintroduce a tray-thread call into widget.root.

        def _handler_body(name):
            """Extract the body of a 4-space-indented on_* tray handler
            (signature `(icon, item)`) from the source, stripping docstrings
            and comment-only lines."""
            m = re.search(
                rf"\n    def {re.escape(name)}\(icon, item\):(.*?)(?=\n    def |\Z)",
                _src, re.DOTALL,
            )
            assert m, f"{name} not found in source"
            body = re.sub(r'""".*?"""', "", m.group(1), flags=re.DOTALL)
            return "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))

        # Guard 4: on_save_favorite (tray thread) must read the position mirror,
        # never touch Tkinter directly.
        _osf = _handler_body("on_save_favorite")
        for _forbidden in ("winfo_x", "winfo_y", ".geometry(", "widget.root"):
            assert _forbidden not in _osf, (
                f"REGRESSION: on_save_favorite (tray thread) must not call {_forbidden!r} "
                "directly -- read widget.last_known_pos (a plain tuple mirrored by the Tk "
                "thread's _tick()/_end_drag()) instead."
            )
        assert "last_known_pos" in _osf, (
            "REGRESSION: on_save_favorite must read widget.last_known_pos, not "
            "recompute the position itself"
        )
        print("on_save_favorite does not touch Tkinter directly: OK")

        # Guard 5: on_load_favorite must only signal an Event.
        _olf = _handler_body("on_load_favorite")
        for _forbidden in ("winfo_x", "winfo_y", ".geometry(", "widget.root"):
            assert _forbidden not in _olf, (
                f"REGRESSION: on_load_favorite (tray thread) must not call {_forbidden!r} "
                "directly -- it must only set load_favorite_requested and let the Tk "
                "thread's _tick() apply the geometry change itself."
            )
        assert "load_favorite_requested.set()" in _olf, (
            "REGRESSION: on_load_favorite must signal the Tk thread via "
            "load_favorite_requested.set(), not move the window itself"
        )
        print("on_load_favorite only signals an Event, never touches Tkinter: OK")

        # Guard 6: the WinEvent hook callback must never call into Tk/Tcl.
        # This DID happen: _on_foreground_change called _reassert_topmost()
        # (-> root.attributes) directly. The callback is dispatched
        # re-entrantly inside Tcl_DoOneEvent's message pump, where _tkinter
        # has saved its thread state; re-entering Tcl from there corrupts
        # that bookkeeping and crashes the whole app with
        # "Fatal Python error: PyEval_RestoreThread: ... thread state is
        # NULL" (observed on real Windows, 2026-07-04, triggered by opening
        # the tray menu -> EVENT_SYSTEM_FOREGROUND). The callback may only
        # set the plain _topmost_dirty flag; _fast_tick applies it.
        _m = re.search(
            r"\n( +)def _on_foreground_change\(.*?\):(.*?)(?=\n\1\S|\n {,15}\S)",
            _src, re.DOTALL,
        )
        assert _m, "_on_foreground_change not found in source"
        _ofc = "\n".join(
            ln for ln in _m.group(2).splitlines()
            if not ln.lstrip().startswith("#")
        )
        for _forbidden in ("_reassert_topmost", "self.root", ".attributes(", ".after("):
            assert _forbidden not in _ofc, (
                f"REGRESSION: _on_foreground_change (a ctypes WinEvent callback "
                f"dispatched inside Tcl's message pump) must not call {_forbidden!r} "
                "-- any Tk/Tcl call from that context fatals with "
                "PyEval_RestoreThread: NULL thread state. Set _topmost_dirty and "
                "let _fast_tick do the Tcl call."
            )
        assert "_topmost_dirty" in _ofc, (
            "REGRESSION: _on_foreground_change must signal via self._topmost_dirty"
        )
        assert "_topmost_dirty" in _src[_src.find("def _fast_tick"):
                                        _src.find("def _tick")], (
            "REGRESSION: _fast_tick must consume _topmost_dirty and call "
            "_reassert_topmost from the Tk after() loop"
        )
        print("_on_foreground_change never touches Tk (sets _topmost_dirty only): OK")

        # Guard 6: the Tk-side application of a loaded favorite must happen
        # inside _tick() (or a method _tick() calls), never inside an on_* handler.
        _tick_body = _method_body("_tick")
        assert "_apply_favorite_position()" in _tick_body, (
            "REGRESSION: _tick() must call self._apply_favorite_position() -- this "
            "is the only place load_favorite_requested may be consumed and applied"
        )
        _afp = _method_body("_apply_favorite_position")
        assert ".geometry(" in _afp, (
            "_apply_favorite_position must actually call self.root.geometry(...) -- "
            "this is the one and only place a favorite-position load may touch Tkinter"
        )
        print("_apply_favorite_position is Tk-thread-only, invoked from _tick(): OK")

        # Guard 7: _apply_favorite_position must flush the queued geometry move
        # with update_idletasks() before returning.
        #
        # Background: _tick() calls _reassert_topmost() (root.attributes(
        # "-topmost", ...), a synchronous SetWindowPos) immediately after
        # _apply_favorite_position() returns, within the same tick. Without
        # flushing first, that call wins the race and silently drops the
        # pending geometry() move -- confirmed on real Windows hardware: the
        # favorite position saved correctly but "Load favorite position"
        # visibly did nothing. Same underlying class of bug as the
        # _place_initial()/_apply_overlay_styles() ordering fix above.
        assert _afp.index(".geometry(") < _afp.index("update_idletasks()"), (
            "REGRESSION: _apply_favorite_position must call update_idletasks() "
            "right after self.root.geometry(...) -- otherwise _reassert_topmost(), "
            "called next in the same _tick() invocation, can cancel the pending move"
        )
        print("_apply_favorite_position flushes geometry before returning: OK")

        # Guard: SessionFileHandler must handle on_moved via dest_path.
        #
        # Background: all our caches are written atomically (temp + os.replace),
        # which Windows reports as one "moved" event — never modified/created on
        # the target. Dropping on_moved silently re-breaks watcher-driven cache
        # updates (state toast, tray pip), deferring them to the 30s sweep.
        _sfh = re.search(
            r"class SessionFileHandler\(FileSystemEventHandler\):(.*?)(?=\n    def )",
            _src, re.DOTALL,
        )
        assert _sfh, "SessionFileHandler not found in source"
        _sfh_body = _sfh.group(1)
        assert "def on_moved" in _sfh_body, \
            "REGRESSION: SessionFileHandler must handle on_moved — atomic os.replace " \
            "cache writes surface as 'moved' events on Windows, not modified/created"
        assert "event.dest_path" in _sfh_body.split("def on_moved", 1)[1], \
            "REGRESSION: on_moved must dispatch on event.dest_path (the real cache " \
            "path after an atomic replace), not src_path (the .tmp file)"
        print("SessionFileHandler handles on_moved (atomic-replace cache writes): OK")

        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Claudebar — Claude Code usage tray app")
    cli.add_argument("--test", action="store_true", help="run the built-in test suite and exit")
    cli.add_argument(
        "--statusline-hook", action="store_true",
        help="read a Claude Code statusLine JSON payload from stdin, cache the "
             "rate-limit fields, and print a status line back (see README.md). "
             "Note: reads stdin to EOF, so a manual run without redirected "
             "input (e.g. `exe --statusline-hook < payload.json`) will block",
    )
    cli.add_argument(
        "--install-hook", action="store_true",
        help="add the statusLine hook entry to ~/.claude/settings.json so Claude "
             "Code starts piping rate-limit data to this script automatically",
    )
    cli.add_argument(
        "--state-hook", action="store_true",
        help="read a Claude Code hook JSON payload from stdin and cache Claude's "
             "current working state (working/waiting/done) for the RAG indicator; "
             "wired into settings.json's hooks array (see README.md). Reads "
             "stdin to EOF — redirect input when running it manually",
    )
    cli.add_argument(
        "--install-state-hooks", action="store_true",
        help="register the working-state indicator hooks in ~/.claude/settings.json "
             "so Claude Code reports its live state to this app",
    )
    args = cli.parse_args()

    if args.test:
        run_tests()
    elif args.statusline_hook:
        run_statusline_hook()
    elif args.state_hook:
        run_state_hook()
    elif args.install_hook:
        run_install_hook()
    elif args.install_state_hooks:
        run_install_state_hooks()
    else:
        try:
            run_app()
        except ImportError as exc:
            # The slim hook build (ClaudebarHook.exe) excludes the GUI
            # packages on purpose; someone double-clicking it should get a
            # pointer at the real app instead of a silent crash.
            message = (
                f"This build can't run the tray app ({exc}).\n\n"
                "It is the statusline/state hook helper — run Claudebar.exe "
                "for the tray app, or `pip install -r requirements.txt` when "
                "running from source."
            )
            shown = False
            if sys.platform.startswith("win") and getattr(sys, "frozen", False):
                try:
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(0, message, "Claudebar", 0x10)
                    shown = True
                except Exception:
                    pass
            if not shown:
                _write_hook_stdout(message)
            sys.exit(1)
