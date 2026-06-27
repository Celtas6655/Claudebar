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


def find_tray_notification_rect():
    """Best-effort lookup of Windows' taskbar notification area (where the
    tray icons live), in screen pixels: {'left', 'top', 'height'}.
    Returns None on non-Windows platforms, if pywin32 isn't installed, or
    if the lookup fails for any reason -- callers must have a fallback."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import win32gui
    except ImportError:
        return None
    try:
        taskbar = win32gui.FindWindow("Shell_TrayWnd", None)
        if not taskbar:
            return None
        notify = win32gui.FindWindowEx(taskbar, 0, "TrayNotifyWnd", None)
        if not notify:
            return None
        left, _top, _right, _bottom = win32gui.GetWindowRect(notify)
        _t_left, t_top, _t_right, t_bottom = win32gui.GetWindowRect(taskbar)
        return {"left": left, "top": t_top, "height": t_bottom - t_top}
    except Exception:
        return None


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

        def __init__(self):
            self.root = tk.Tk()
            self.root.overrideredirect(True)
            self.root.attributes("-topmost", True)
            try:
                self.root.attributes("-alpha", 0.92)
            except tk.TclError:
                pass
            self.root.configure(bg=self.BG)

            frame = tk.Frame(self.root, bg=self.BG, padx=10, pady=8)
            frame.pack(fill="both", expand=True)

            title_row = tk.Frame(frame, bg=self.BG)
            title_row.pack(fill="x")
            tk.Label(
                title_row, text="Claude Code", font=("Consolas", 9, "bold"),
                fg=self.FG, bg=self.BG,
            ).pack(side="left")
            close_btn = tk.Label(
                title_row, text="\u2715", font=("Consolas", 9, "bold"),
                fg=self.DIM, bg=self.BG, cursor="hand2",
            )
            close_btn.pack(side="right")
            close_btn.bind("<Button-1>", lambda e: widget_visible.clear())

            self.today_label = tk.Label(
                frame, font=("Consolas", 9), fg=self.FG, bg=self.BG, anchor="w",
            )
            self.today_label.pack(fill="x", pady=(4, 4))

            self.session_canvas, self.session_pct_lbl, self.session_reset_lbl, session_row = (
                self._build_metric_row(frame, "Session 5h")
            )
            self.weekly_canvas, self.weekly_pct_lbl, self.weekly_reset_lbl, weekly_row = (
                self._build_metric_row(frame, "Weekly  7d")
            )

            for w in (self.root, frame, title_row, self.today_label, session_row, weekly_row):
                w.bind("<Button-1>", self._start_drag)
                w.bind("<B1-Motion>", self._do_drag)
                w.bind("<ButtonRelease-1>", self._end_drag)

            self._drag_offset = (0, 0)
            self._render()                  # render real content first...
            self.root.update_idletasks()    # ...so reqwidth/reqheight reflect it...
            self._place_initial()           # ...before sizing/positioning the window.
            self.root.after(1000, self._tick)

        def _build_metric_row(self, parent, name):
            row = tk.Frame(parent, bg=self.BG)
            row.pack(fill="x", pady=2)
            tk.Label(
                row, text=name, font=("Consolas", 9), fg=self.FG, bg=self.BG,
                width=10, anchor="w",
            ).pack(side="left")
            canvas = tk.Canvas(
                row, width=self.BAR_W, height=self.BAR_H, bg=self.BG, highlightthickness=0,
            )
            canvas.pack(side="left", padx=(0, 6))
            pct_lbl = tk.Label(row, font=("Consolas", 9, "bold"), bg=self.BG, width=4, anchor="e")
            pct_lbl.pack(side="left")
            reset_lbl = tk.Label(row, font=("Consolas", 9), fg=self.DIM, bg=self.BG, anchor="w")
            reset_lbl.pack(side="left", padx=(6, 0))
            return canvas, pct_lbl, reset_lbl, row

        def _draw_bar(self, canvas, pct):
            canvas.delete("all")
            canvas.create_rectangle(0, 0, self.BAR_W, self.BAR_H, fill=self.TRACK, outline="")
            if pct is not None:
                fill_w = self.BAR_W * max(0.0, min(100.0, pct)) / 100
                canvas.create_rectangle(
                    0, 0, fill_w, self.BAR_H, fill=WIDGET_COLORS[pct_tag(pct)], outline="",
                )

        def _render_metric_row(self, canvas, pct_lbl, reset_lbl, pct, resets_at):
            self._draw_bar(canvas, pct)
            if pct is not None:
                pct_lbl.config(text=f"{pct:.0f}%", fg=WIDGET_COLORS[pct_tag(pct)])
                reset_lbl.config(text=f"resets {fmt_reset_clock(resets_at) or '?'}", fg=self.DIM)
            else:
                pct_lbl.config(text="--", fg=self.DIM)
                reset_lbl.config(text="(needs statusLine hook)", fg=self.DIM)

        def _render(self):
            snap = current_snapshot()
            today_tok = sum(snap["today_totals"].values())
            self.today_label.config(
                text=f"Today  {fmt_tokens(today_tok):>7} tok   {fmt_cost(snap['cost_today'])}"
            )
            cache = read_usage_cache() or {}
            self._render_metric_row(
                self.session_canvas, self.session_pct_lbl, self.session_reset_lbl,
                cache.get("session_used_percentage"), cache.get("session_resets_at"),
            )
            self._render_metric_row(
                self.weekly_canvas, self.weekly_pct_lbl, self.weekly_reset_lbl,
                cache.get("weekly_used_percentage"), cache.get("weekly_resets_at"),
            )

        def _place_initial(self):
            saved = read_usage_cache(cache_path=WIDGET_POS_PATH)
            w = self.root.winfo_reqwidth()
            h = self.root.winfo_reqheight()
            if saved and saved.get("x") is not None and saved.get("y") is not None:
                x, y = saved["x"], saved["y"]
            else:
                x, y = self._default_position(w, h)
            self.root.geometry(f"{w}x{h}+{int(x)}+{int(y)}")

        def _default_position(self, w, h):
            """On first run (no saved drag position), try to sit just to
            the left of the real taskbar notification area (where the
            tray icons live). Falls back to a screen-corner estimate if
            that can't be detected (non-Windows, pywin32 missing, or the
            lookup fails for any reason)."""
            tray = find_tray_notification_rect()
            if tray:
                x = tray["left"] - w - 8
                y = tray["top"] + (tray["height"] - h) // 2
                return x, y
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            return sw - w - 220, sh - h - 50

        def _start_drag(self, event):
            self._drag_offset = (
                event.x_root - self.root.winfo_x(),
                event.y_root - self.root.winfo_y(),
            )

        def _do_drag(self, event):
            x = event.x_root - self._drag_offset[0]
            y = event.y_root - self._drag_offset[1]
            self.root.geometry(f"+{x}+{y}")

        def _end_drag(self, event):
            write_usage_cache(
                {"x": self.root.winfo_x(), "y": self.root.winfo_y()},
                cache_path=WIDGET_POS_PATH,
            )

        def _tick(self):
            if should_quit.is_set():
                self.root.quit()
                return
            if widget_visible.is_set():
                if not self.root.winfo_viewable():
                    self.root.deiconify()
                self._render()
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

        # --- taskbar tray-rect detection: must degrade gracefully off-Windows ---
        if not sys.platform.startswith("win"):
            assert find_tray_notification_rect() is None
            print("find_tray_notification_rect() correctly returns None off-Windows: OK")

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
    args = cli.parse_args()

    if args.test:
        run_tests()
    elif args.statusline_hook:
        run_statusline_hook()
    else:
        run_app()
