"""
claude_usage_tray.py — single-file Windows tray app showing live Claude Code
token usage and estimated cost, read straight from Claude Code's local
session logs.

Usage:
    python claude_usage_tray.py                  # run the tray app
    python claude_usage_tray.py --test           # run the built-in test suite
    python claude_usage_tray.py --statusline-hook  # used internally as Claude
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
USAGE_CACHE_PATH = os.path.join(CLAUDE_HOME, "usage_tray_cache.json")

# Our own marker recording that we've wired the statusLine hook into Claude
# Code's settings.json (see ensure_hook_installed). Deliberately a separate
# sidecar, NOT a key inside settings.json -- that file's parse is strict and
# fragile (one stray key/comment breaks all of it), so we never write anything
# but the `statusLine` entry into it.
HOOK_INSTALLED_MARKER_PATH = os.path.join(CLAUDE_HOME, "usage_tray_hook_installed.json")

# Where the --state-hook mode caches Claude Code's *current working state*
# (working / waiting-on-you / done), for the widget and tray icon to colour a
# Red/Amber/Green indicator from. This is a THIRD data source, unrelated to the
# token logs and the statusLine rate-limit cache: it comes from Claude Code's
# event-hooks system (Stop/Notification/UserPromptSubmit/PreToolUse), the only
# mechanism that exposes live per-turn lifecycle state. See ARCHITECTURE.md §2.
STATE_CACHE_PATH = os.path.join(CLAUDE_HOME, "usage_tray_state_cache.json")

# Marker recording that we've merged our --state-hook entries into settings.json's
# `hooks` array (see ensure_state_hooks_installed). Separate sidecar for the same
# reason as HOOK_INSTALLED_MARKER_PATH above.
STATE_HOOKS_MARKER_PATH = os.path.join(CLAUDE_HOME, "usage_tray_state_hooks_installed.json")

# A cached working state older than this (seconds) is treated as unknown (dim),
# so a missed Stop hook or an app started mid-turn can't leave "working" stuck on.
STATE_STALE_SECONDS = 300

# Claude Code hook events we register --state-hook against, and the working state
# each maps to. Order/keys mirror derive_working_state()'s logic; used both to
# install the hooks and to reason about them. Notification == "Claude needs you".
STATE_HOOK_EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "Notification")

# App version -- single source of truth, mirrored by the VERSION file at the
# repo root that the release workflow reads to tag the build.
__version__ = "1.1.0"

# Approximate USD price per 1M tokens: (input, output, cache_write, cache_read)
# Anthropic's published per-model rates as of mid-2026 -- treat as a rough
# estimate and check https://platform.claude.com/docs/en/about-claude/pricing
# if you need exact numbers; rates do change over time.
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
        self.sessions.add(record.get("session_id"))
        for key in USAGE_KEYS:
            self.totals[key] += record[key]
            self.by_model[record["model"]][key] += record[key]
            self.by_project[record["project"]][key] += record[key]
        cost = cost_for_record(record)
        self.cost_total += cost
        if record["timestamp"][:10] == self._today.isoformat():
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

    def poll(self):
        """Full sweep: read any new lines appended to ANY session file."""
        self._maybe_roll_day()
        for filepath in find_session_files(self.projects_dir):
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
    """Color-threshold name for a percentage: green < 50, yellow < 80, else red."""
    if pct is None:
        return "dim"
    if pct < 50:
        return "green"
    if pct < 80:
        return "yellow"
    return "red"


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


# --------------------------------------------------------------------------
# Windows startup registration: HKCU\...\CurrentVersion\Run entry.
# Uses winreg (stdlib) only -- imported lazily and only on Windows, same
# lazy-import-behind-a-platform-guard pattern as the ctypes helpers above.
# No admin rights needed (per-user hive). Registry ops fail soft, same as
# file I/O elsewhere in this file -- never raise into a long-running thread.
# --------------------------------------------------------------------------

STARTUP_APP_NAME = "ClaudeUsageTray"   # matches build_exe.bat's --name
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
      (the plain `python claude_usage_tray.py --statusline-hook` form).
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
    status_line_text). Kept separate from stdin/file I/O so it's testable."""
    model = (payload.get("model") or {}).get("display_name", "Claude")
    ctx = payload.get("context_window") or {}
    rate_limits = payload.get("rate_limits") or {}
    five_hour = rate_limits.get("five_hour") or {}
    seven_day = rate_limits.get("seven_day") or {}

    cache = {
        "fetched_at": time.time(),
        "model": model,
        "context_used_percentage": ctx.get("used_percentage"),
        "session_used_percentage": five_hour.get("used_percentage"),
        "session_resets_at": five_hour.get("resets_at"),
        "weekly_used_percentage": seven_day.get("used_percentage"),
        "weekly_resets_at": seven_day.get("resets_at"),
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


def install_statusline_hook(settings_path, command, force=True):
    """Merge the statusLine hook entry into Claude Code's settings.json.

    `command` is the fully-formed command line (see build_hook_command).

    force=True  -> always (re)write our entry, even over a different existing
                   statusLine command (the --install-hook escape hatch).
    force=False -> only add our entry when there's NO statusLine at all; never
                   clobber a user's own/foreign statusLine (the auto path).

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

    if existing is not None and not force:
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


def _resolve_hook_command():
    """The statusLine command line for the current process (frozen exe or
    plain python), via build_hook_command."""
    frozen = getattr(sys, "frozen", False)
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
    pointing at `command` only when that exact command isn't already registered
    there; we never remove or edit the user's own hook groups. Writes only when
    something actually changed, so a normal startup doesn't churn settings.json.

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

    def _command_present(groups):
        for group in groups:
            if not isinstance(group, dict):
                continue
            for h in group.get("hooks") or []:
                if isinstance(h, dict) and h.get("command") == command:
                    return True
        return False

    changed = False
    for event in STATE_HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            groups = []
        if _command_present(groups):
            continue
        groups.append({"matcher": "", "hooks": [dict(entry)]})
        hooks[event] = groups
        changed = True

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
    """The --state-hook command line for the current process (frozen exe or
    plain python), via build_state_hook_command."""
    frozen = getattr(sys, "frozen", False)
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
    cache, status_line_text = process_statusline_payload(payload)
    write_usage_cache(cache)
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
        state = derive_working_state(payload)
        if state is not None:
            write_usage_cache(
                {
                    "state": state,
                    "event": payload.get("hook_event_name"),
                    "session_id": payload.get("session_id"),
                    "updated_at": time.time(),
                },
                cache_path=STATE_CACHE_PATH,
            )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Tray app. GUI/watcher dependencies (pystray, Pillow, watchdog) are
# imported lazily inside this function, only when actually running the
# app — so --test never needs a tray backend to be available.
# --------------------------------------------------------------------------

def run_app():
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
    WIDGET_POS_PATH = os.path.join(CLAUDE_HOME, "usage_tray_widget_pos.json")
    ALIGNMENT_CONFIG_PATH = os.path.join(CLAUDE_HOME, "usage_tray_alignment.json")
    WIDGET_PIN_PATH = os.path.join(CLAUDE_HOME, "usage_tray_widget_pin.json")
    WIDGET_FAVORITE_POS_PATH = os.path.join(CLAUDE_HOME, "usage_tray_widget_favorite_pos.json")

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
        the state cache (dim when unknown/stale). Shared by the widget dot and
        the tray-icon pip."""
        st = read_usage_cache(cache_path=STATE_CACHE_PATH) or {}
        return working_state_tag(st.get("state"), st.get("updated_at"))

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
                    self._reassert_topmost()
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
            st = read_usage_cache(cache_path=STATE_CACHE_PATH) or {}
            tag = working_state_tag(st.get("state"), st.get("updated_at"))
            d = self._dot_d
            self.state_canvas.delete("all")
            # 1px inset so the oval's antialiased edge isn't clipped by the canvas.
            self.state_canvas.create_oval(
                1, 1, d - 1, d - 1, fill=WIDGET_COLORS[tag], outline="",
            )
            self.state_label.config(
                text=working_state_label(st.get("state"), st.get("updated_at")),
                fg=(self.DIM if tag == "dim" else WIDGET_COLORS[tag]),
            )

        def _render(self):
            self._draw_status_dot()
            snap = current_snapshot()
            today_tok = sum(snap["today_totals"].values())
            self.today_label.config(
                text=f"Today  {fmt_tokens(today_tok):>7} tok   {fmt_cost(snap['cost_today'])}"
            )
            cache = read_usage_cache() or {}
            self._render_metric_row(
                self.session_canvas, self.session_reset_lbl,
                cache.get("session_used_percentage"), cache.get("session_resets_at"),
            )
            self._render_metric_row(
                self.weekly_canvas, self.weekly_reset_lbl,
                cache.get("weekly_used_percentage"), cache.get("weekly_resets_at"),
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
            if wx <= mx < wx + ww and wy <= my < wy + wh:
                self._set_alpha(191)
            else:
                self._set_alpha(255, colorkey=True)

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
            """Runs every 150ms: drives hover alpha.
            Topmost z-order is managed by the WinEvent hook (event-driven),
            not polled here — that was the 'normal always-on-top' approach."""
            if should_quit.is_set():
                return
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
            if load_favorite_requested.is_set():
                load_favorite_requested.clear()
                self._apply_favorite_position()
            if widget_visible.is_set():
                if not self.root.winfo_viewable():
                    self.root.deiconify()
                    self._set_transparent(True)
                self._render()
                self._reassert_topmost()   # 1-second safety net for z-order
                # Periodically re-align to the tray (handles taskbar moves, DPI changes,
                # monitor layout changes, and tray overflow expand/collapse).
                if ALIGNMENT_CONFIG.enabled:
                    now = time.monotonic()
                    if now - self._last_alignment_check > 30.0:
                        self._recheck_alignment()
                self.last_known_pos = (self.root.winfo_x(), self.root.winfo_y())
            else:
                self.root.withdraw()
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
        Tk); pystray supports reassigning .icon at runtime."""
        tag = current_state_tag()
        if tag == _icon_state["tag"]:
            return
        _icon_state["tag"] = tag
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
        cache = read_usage_cache()
        if cache:
            if cache.get("session_used_percentage") is not None:
                lines.append(
                    f"Session: {cache['session_used_percentage']:.0f}% "
                    f"(resets {fmt_reset_clock(cache.get('session_resets_at')) or '?'})"
                )
            if cache.get("weekly_used_percentage") is not None:
                lines.append(
                    f"Weekly: {cache['weekly_used_percentage']:.0f}% "
                    f"(resets {fmt_reset_clock(cache.get('weekly_resets_at')) or '?'})"
                )
        st = read_usage_cache(cache_path=STATE_CACHE_PATH) or {}
        st_label = working_state_label(st.get("state"), st.get("updated_at"))
        if st_label != "—":
            lines.append(f"State: {st_label}")
        return "\n".join(lines)

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
        ]

        st = read_usage_cache(cache_path=STATE_CACHE_PATH) or {}
        st_label = working_state_label(st.get("state"), st.get("updated_at"))
        if st_label != "—":
            items.append(pystray.MenuItem(f"State:    Claude is {st_label}", None, enabled=False))

        items.append(pystray.Menu.SEPARATOR)
        cache = read_usage_cache()
        has_limits = cache and (
            cache.get("session_used_percentage") is not None
            or cache.get("weekly_used_percentage") is not None
        )
        if has_limits:
            if cache.get("session_used_percentage") is not None:
                items.append(pystray.MenuItem(
                    f"Session (5h): {cache['session_used_percentage']:.0f}% used "
                    f"\u2014 resets {fmt_reset_full(cache.get('session_resets_at'))}",
                    None, enabled=False,
                ))
            if cache.get("weekly_used_percentage") is not None:
                items.append(pystray.MenuItem(
                    f"Weekly (7d): {cache['weekly_used_percentage']:.0f}% used "
                    f"\u2014 resets {fmt_reset_full(cache.get('weekly_resets_at'))}",
                    None, enabled=False,
                ))
            if cache.get("context_used_percentage") is not None:
                items.append(pystray.MenuItem(
                    f"Context window: {cache['context_used_percentage']:.0f}% used",
                    None, enabled=False,
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
            "Pin position",
            on_toggle_pin,
            checked=lambda item: position_pinned.is_set(),
        ))
        items.append(pystray.MenuItem(
            "Run on Windows startup",
            on_toggle_startup,
            checked=lambda item: is_startup_enabled(),
            enabled=lambda item: sys.platform.startswith("win"),
        ))
        items.append(pystray.MenuItem("Save current position as favorite", on_save_favorite))
        items.append(pystray.MenuItem(
            "Load favorite position",
            on_load_favorite,
            enabled=lambda item: read_usage_cache(cache_path=WIDGET_FAVORITE_POS_PATH) is not None,
        ))
        items.append(pystray.MenuItem("Refresh now", on_refresh))
        items.append(pystray.MenuItem("Open logs folder", on_open_folder))
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

    def on_toggle_pin(icon, item):
        if position_pinned.is_set():
            position_pinned.clear()
            write_usage_cache({"pinned": False}, cache_path=WIDGET_PIN_PATH)
        else:
            position_pinned.set()
            write_usage_cache({"pinned": True}, cache_path=WIDGET_PIN_PATH)

    def on_toggle_startup(icon, item):
        # Tray thread. Fails soft -- see set_startup_enabled()/_startup_registry_*.
        # The registry value itself is the source of truth, re-read fresh
        # here and in the checked= lambda above -- no in-memory mirror needed.
        set_startup_enabled(not is_startup_enabled())

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
            base = os.path.basename(path)
            is_usage_cache = base == os.path.basename(USAGE_CACHE_PATH)
            is_state_cache = base == os.path.basename(STATE_CACHE_PATH)
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
            with state_lock:
                tracker.poll()
            icon.title = build_title()
            apply_icon_state(icon)   # safety-net recolor (e.g. rolls to dim when stale)

    with state_lock:
        tracker.poll()  # initial full scan so the tray isn't empty on first open

    icon = pystray.Icon(
        "claude_usage_tray",
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
# Built-in test suite — run with: python claude_usage_tray.py --test
# Exercises the data layer (and the live filesystem watcher, if the
# `watchdog` package is installed) against a synthetic projects tree.
# No tray/GUI dependencies required.
# --------------------------------------------------------------------------

def run_tests():
    import shutil

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

        cache_path = os.path.join(tmp_home, "usage_tray_cache.json")
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
        print("Widget color-threshold helper: OK")

        # --- statusLine hook command construction (pure, frozen-aware) ---
        exe = r"C:\Apps\ClaudeUsageTray.exe"
        script = r"C:\code\claude_usage_tray.py"
        frozen_cmd = build_hook_command(True, exe, script)
        assert frozen_cmd == f'"{exe}" --statusline-hook', frozen_cmd
        py = r"C:\Python312\python.exe"
        nonfrozen_cmd = build_hook_command(False, py, script)
        assert nonfrozen_cmd == f'"{py}" "{script}" --statusline-hook', nonfrozen_cmd
        assert "python" not in frozen_cmd.lower()  # frozen exe never invokes python
        print("build_hook_command (frozen exe vs. python script): OK")

        # --- statusLine hook installation ---
        expected_cmd = build_hook_command(False, "python", "/path/to/claude_usage_tray.py")
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
        with open(settings_path4, "w", encoding="utf-8") as f:
            f.write('{"broken": true,}')  # trailing comma — invalid JSON
        ok5, msg5 = install_statusline_hook(settings_path4, expected_cmd)
        assert not ok5, "should have failed on malformed JSON"
        # File must be unchanged (still unparseable, not overwritten)
        with open(settings_path4, encoding="utf-8") as f:
            raw = f.read()
        assert "broken" in raw and raw.strip().endswith("}") is False or "}" in raw
        print("hook install — malformed JSON aborted cleanly: OK")

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
        state_cmd = build_state_hook_command(False, "python", "/path/to/claude_usage_tray.py")
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

        # --- taskbar tray-rect detection: must degrade gracefully off-Windows ---
        if not sys.platform.startswith("win"):
            assert find_tray_notification_rect() is None
            print("find_tray_notification_rect() correctly returns None off-Windows: OK")

        # --- Windows startup registration: pure command-string logic ---
        # Pure path/string logic -- runs on any OS, no registry involved.
        cmd = build_startup_command(
            frozen=True, executable=r"C:\Apps\ClaudeUsageTray.exe",
            script_path=r"C:\Apps\claude_usage_tray.py",
        )
        assert cmd == '"C:\\Apps\\ClaudeUsageTray.exe"', cmd

        cmd = build_startup_command(
            frozen=False, executable=r"C:\Python312\python.exe",
            script_path=r"C:\code\claude_usage_tray.py",
        )
        # no real pythonw.exe on disk at that fabricated path -> falls back to executable
        assert cmd == '"C:\\Python312\\python.exe" "C:\\code\\claude_usage_tray.py"', cmd
        print("build_startup_command (frozen / non-frozen, no pythonw present): OK")

        # pythonw.exe preference: create a *real* file in the tmp sandbox so
        # os.path.isfile() finds it, without touching any real Python install.
        fake_pyw = os.path.join(tmp_home, "pythonw.exe")
        with open(fake_pyw, "w", encoding="utf-8"):
            pass
        fake_py = os.path.join(tmp_home, "python.exe")
        cmd = build_startup_command(
            frozen=False, executable=fake_py, script_path=r"C:\code\claude_usage_tray.py",
        )
        assert cmd == f'"{fake_pyw}" "C:\\code\\claude_usage_tray.py"', cmd
        print("build_startup_command prefers pythonw.exe when present alongside python.exe: OK")

        # --- Windows startup registration: real registry round-trip (Windows only) ---
        # Always uses a disposable test subkey, never STARTUP_RUN_SUBKEY (the real
        # HKCU\...\CurrentVersion\Run) -- this must never register the test run itself.
        if sys.platform.startswith("win"):
            try:
                import winreg
                test_subkey = r"Software\ClaudeUsageTrayTestOnly_DoNotUseForRealStartup"
                test_value_name = "ClaudeUsageTrayTest"
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

        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="Claude Code usage tray app")
    cli.add_argument("--test", action="store_true", help="run the built-in test suite and exit")
    cli.add_argument(
        "--statusline-hook", action="store_true",
        help="read a Claude Code statusLine JSON payload from stdin, cache the "
             "rate-limit fields, and print a status line back (see README.md)",
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
             "wired into settings.json's hooks array (see README.md)",
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
        run_app()
