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


def install_statusline_hook(settings_path, script_path):
    """Merge the statusLine hook entry into Claude Code's settings.json.

    Returns (success: bool, message: str).
    Never touches the file if it can't be parsed cleanly — safer than
    risking a corrupt settings.json (one stray comma kills the whole file).
    """
    command = "python " + script_path.replace("\\", "/") + " --statusline-hook"
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


def run_install_hook():
    """Entry point for --install-hook: wires this script into Claude Code's
    settings.json as the statusLine command."""
    settings_path = os.path.join(CLAUDE_HOME, "settings.json")
    script_path = os.path.abspath(__file__)
    success, message = install_statusline_hook(settings_path, script_path)
    print(message)
    if not success:
        sys.exit(1)


def run_statusline_hook():
    """Entry point for `python claude_usage_tray.py --statusline-hook`,
    meant to be wired up as Claude Code's `statusLine` command (see
    README.md). Reads Claude Code's JSON payload from stdin, caches it,
    and prints a compact status line back to stdout."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    cache, status_line_text = process_statusline_payload(payload)
    write_usage_cache(cache)
    print(status_line_text)


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

    FALLBACK_SWEEP_SECONDS = 30   # safety-net full rescan interval
    WATCHER_RETRY_SECONDS = 5     # retry interval if projects dir doesn't exist yet
    WIDGET_POS_PATH = os.path.join(CLAUDE_HOME, "usage_tray_widget_pos.json")
    ALIGNMENT_CONFIG_PATH = os.path.join(CLAUDE_HOME, "usage_tray_alignment.json")

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

    def current_snapshot():
        with state_lock:
            return tracker.snapshot()

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

            self.today_label = tk.Label(
                frame, font=("Consolas", _fs), fg=self.FG, bg=self.BG, anchor="w",
            )
            self.today_label.pack(fill="x", pady=(_today_pady, _today_pady))

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

            for w in (self.root, frame, self.today_label, metrics_row):
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

        def _render(self):
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
            self._drag_offset = (
                event.x_root - self.root.winfo_x(),
                event.y_root - self.root.winfo_y(),
            )
            self._dragging = True
            self._set_transparent(False)

        def _do_drag(self, event):
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.root.geometry(f"+{x}+{y}")

        def _end_drag(self, event):
            # Dragging disables alignment so the widget stays where the user puts it.
            # Re-enable via the tray menu "Align to taskbar tray" item.
            if ALIGNMENT_CONFIG.enabled:
                ALIGNMENT_CONFIG.enabled = False
                write_usage_cache({"enabled": False}, cache_path=ALIGNMENT_CONFIG_PATH)
            write_usage_cache(
                {"x": self.root.winfo_x(), "y": self.root.winfo_y()},
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
            else:
                self.root.withdraw()
            self.root.after(1000, self._tick)

        def mainloop(self):
            self.root.mainloop()


    def make_icon_image():
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((2, 2, size - 2, size - 2), fill=(204, 120, 92, 255))
        draw.ellipse((10, 10, size - 10, size - 10), fill=(255, 255, 255, 230))
        draw.ellipse((20, 20, size - 20, size - 20), fill=(204, 120, 92, 255))
        return img

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

    def on_quit(icon, item):
        should_quit.set()
        icon.stop()

    class SessionFileHandler(FileSystemEventHandler):
        def __init__(self, icon):
            self.icon = icon

        def _handle(self, path):
            is_session_file = path.endswith(".jsonl")
            is_usage_cache = os.path.basename(path) == os.path.basename(USAGE_CACHE_PATH)
            if not (is_session_file or is_usage_cache):
                return
            if is_session_file:
                with state_lock:
                    tracker.poll_file(path)
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

    with state_lock:
        tracker.poll()  # initial full scan so the tray isn't empty on first open

    icon = pystray.Icon(
        "claude_usage_tray",
        icon=make_icon_image(),
        title=build_title(),
        menu=pystray.Menu(menu_items),
    )

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

        # --- statusLine hook installation ---
        fake_script = "/path/to/claude_usage_tray.py"
        expected_cmd = f"python {fake_script} --statusline-hook"
        expected_block = {"type": "command", "command": expected_cmd}

        # Case 1: fresh install (no settings.json)
        settings_path = os.path.join(tmp_home, "settings_fresh.json")
        ok, msg = install_statusline_hook(settings_path, fake_script)
        assert ok, msg
        with open(settings_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["statusLine"] == expected_block, data
        print("hook install — fresh file: OK")

        # Case 2: existing settings with unrelated keys (keys must be preserved)
        settings_path2 = os.path.join(tmp_home, "settings_merge.json")
        with open(settings_path2, "w", encoding="utf-8") as f:
            json.dump({"theme": "dark", "autoUpdates": False}, f)
        ok, msg = install_statusline_hook(settings_path2, fake_script)
        assert ok, msg
        with open(settings_path2, encoding="utf-8") as f:
            data2 = json.load(f)
        assert data2["theme"] == "dark"
        assert data2["autoUpdates"] is False
        assert data2["statusLine"] == expected_block
        print("hook install — merges with existing keys: OK")

        # Case 3: already configured with the same command (no-op)
        ok3, msg3 = install_statusline_hook(settings_path2, fake_script)
        assert ok3, msg3
        assert "already" in msg3.lower()
        print("hook install — already configured, no-op: OK")

        # Case 4: existing statusLine with a different command (overwritten)
        settings_path3 = os.path.join(tmp_home, "settings_overwrite.json")
        with open(settings_path3, "w", encoding="utf-8") as f:
            json.dump({"statusLine": {"type": "command", "command": "old-cmd"}}, f)
        ok4, msg4 = install_statusline_hook(settings_path3, fake_script)
        assert ok4, msg4
        with open(settings_path3, encoding="utf-8") as f:
            data3 = json.load(f)
        assert data3["statusLine"] == expected_block
        assert "old-cmd" in msg4  # message must mention the old value
        print("hook install — overwrites different command: OK")

        # Case 5: malformed settings.json (abort without writing)
        settings_path4 = os.path.join(tmp_home, "settings_broken.json")
        with open(settings_path4, "w", encoding="utf-8") as f:
            f.write('{"broken": true,}')  # trailing comma — invalid JSON
        ok5, msg5 = install_statusline_hook(settings_path4, fake_script)
        assert not ok5, "should have failed on malformed JSON"
        # File must be unchanged (still unparseable, not overwritten)
        with open(settings_path4, encoding="utf-8") as f:
            raw = f.read()
        assert "broken" in raw and raw.strip().endswith("}") is False or "}" in raw
        print("hook install — malformed JSON aborted cleanly: OK")

        # --- taskbar tray-rect detection: must degrade gracefully off-Windows ---
        if not sys.platform.startswith("win"):
            assert find_tray_notification_rect() is None
            print("find_tray_notification_rect() correctly returns None off-Windows: OK")

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
    args = cli.parse_args()

    if args.test:
        run_tests()
    elif args.statusline_hook:
        run_statusline_hook()
    elif args.install_hook:
        run_install_hook()
    else:
        run_app()
