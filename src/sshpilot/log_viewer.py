"""Log viewer dialog.

Lets ordinary users review the application log and copy a tidy diagnostic
bundle (platform info + recent log tail) to the clipboard so they can paste
it into a bug report — no command-line, no file-system spelunking.

Lives under the main window's Help submenu (``win.view-logs``).
"""

from __future__ import annotations

import collections
import glob
import json
import logging
import os
import re
import zipfile
from gettext import gettext as _
from typing import List, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .platform_utils import get_state_dir


logger = logging.getLogger(__name__)


# Default number of tail lines to load. Keeps the viewer snappy even when the
# rotated log has grown to its 10 MB cap. The user can ask for the full file
# from the menu — they almost never need to.
_DEFAULT_TAIL_LINES = 500


# (display label, filename in state-dir or absolute) — drives the category
# dropdown. ``master`` is the authoritative source for bug reports; the
# other two are filtered convenience views written by setup_logging. Askpass
# lives in $XDG_RUNTIME_DIR and is resolved via the askpass module so we
# don't duplicate its path logic.
_CATEGORY_MASTER = 'master'
_CATEGORY_APP = 'app'
_CATEGORY_SSH = 'ssh'
_CATEGORY_ASKPASS = 'askpass'
_CATEGORY_CRASH = 'crash'


# Level-filter dropdown options. The integer is the *minimum* logging level
# whose records pass through (0 = no filtering — show everything). Logging
# constants: DEBUG=10, INFO=20, WARNING=30, ERROR=40.
# Labels follow the "show this level and above" convention every logging
# system uses; we don't spell that out because the dropdown's tooltip
# already explains it and the labels need to fit a compact header bar.
_LEVEL_FILTER_OPTIONS = (
    # (label, minimum_level)
    ("All",      0),
    ("Info",     logging.INFO),
    ("Warning",  logging.WARNING),
    ("Error",    logging.ERROR),
)


# Map textual level → numeric. Matches our file formatter's output.
_LEVEL_NAME_TO_INT = {
    "DEBUG":    logging.DEBUG,
    "INFO":     logging.INFO,
    "WARNING":  logging.WARNING,
    "WARN":     logging.WARNING,
    "ERROR":    logging.ERROR,
    "CRITICAL": logging.CRITICAL,
    "FATAL":    logging.CRITICAL,
}


def _level_of_line(line: str) -> int:
    """Best-effort extraction of a record's level from a formatted log line.

    Our file formatter writes ``YYYY-MM-DD HH:MM:SS - name - LEVEL - msg``.
    Any line that doesn't match (e.g. multi-line tracebacks, askpass output)
    is treated as INFO so it survives all real filters — we'd rather show a
    little extra than hide something the user is looking for.
    """
    # Split on ``" - "`` up to 4 fields. The third field is the level.
    parts = line.split(" - ", 3)
    if len(parts) >= 3:
        return _LEVEL_NAME_TO_INT.get(parts[2].strip(), logging.INFO)
    return logging.INFO


def _resolve_log_path(category: str = _CATEGORY_MASTER) -> str:
    """Return the absolute path to the log file for *category*."""
    if category == _CATEGORY_APP:
        return os.path.join(get_state_dir(), 'app.log')
    if category == _CATEGORY_SSH:
        return os.path.join(get_state_dir(), 'ssh.log')
    if category == _CATEGORY_ASKPASS:
        try:
            from .askpass_utils import get_askpass_log_path
            return get_askpass_log_path()
        except Exception as exc:
            logger.debug("Could not resolve askpass log path: %s", exc)
            return ''
    if category == _CATEGORY_CRASH:
        # Prefer a non-empty crash report (rotated previous run, else live).
        return _resolve_crash_path() or os.path.join(get_state_dir(), 'crash.log')
    # Master / fallback.
    return os.path.join(get_state_dir(), 'sshpilot.log')


def _tail_file(path: str, n: int) -> tuple[List[str], int]:
    """Read the last *n* lines of *path*.

    Returns ``(lines, total_lines)``. On error or missing file, returns
    ``([], 0)`` — the caller handles the empty state.
    """
    if not os.path.isfile(path):
        return [], 0
    try:
        # Use a deque so we don't materialise the whole file. ~10 MB log =
        # ~100k lines; deque(maxlen=n) keeps memory bounded to n entries.
        with open(path, encoding='utf-8', errors='replace') as fh:
            dq: 'collections.deque[str]' = collections.deque(maxlen=n)
            total = 0
            for line in fh:
                dq.append(line.rstrip('\n'))
                total += 1
            return list(dq), total
    except Exception as exc:
        logger.warning("Could not read log file %s: %s", path, exc)
        return [], 0


def _read_full_file(path: str) -> str:
    """Return the entire file as text, or ``""`` on error."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            return fh.read()
    except Exception as exc:
        logger.warning("Could not read log file %s: %s", path, exc)
        return ""


def _build_diagnostic_bundle(log_lines: List[str], total_lines: int,
                              log_path: str) -> str:
    """Produce the clipboard-friendly diagnostic blob.

    Combines a short platform-info header with the supplied log lines. The
    whole thing is wrapped to render as a single Markdown code block when
    pasted into GitHub / Discourse / etc.
    """
    # Import here so this module stays cheap to import. verbose=True so the
    # diagnostic bundle captures full storage/tool probing (libsecret & keyring
    # accessibility, sshpass version), which the concise startup path skips.
    try:
        from .startup_info import StartupInfo
        info = StartupInfo(verbose=True).info
    except Exception:
        info = {}

    def _get(*path, default='?'):
        cur = info
        for k in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
            if cur is None:
                return default
        return cur

    libs = info.get('libraries') or {}
    def _lib(key: str) -> str:
        entry = libs.get(key)
        if isinstance(entry, dict):
            return entry.get('version') or '?'
        return '?'

    lines: List[str] = []
    lines.append("```")
    lines.append(f"sshPilot {_get('version', 'version')} — diagnostic bundle")
    lines.append("=" * 48)
    lines.append(
        f"OS:       {_get('platform', 'system')} "
        f"({_get('platform', 'distro')}) {_get('platform', 'architecture')}"
        + (" [Flatpak]" if _get('platform', 'flatpak') else "")
    )
    lines.append(
        f"Runtime:  Python {_get('python', 'version')} "
        f"({_get('python', 'implementation')}) · "
        f"GTK {_lib('gtk4')} / libadwaita {_lib('libadwaita')} / VTE {_lib('vte')}"
    )
    lines.append(
        f"Storage:  {_get('storage', 'effective_backend')}"
    )
    ssh_path = _get('tools', 'ssh', 'path', default='?')
    ssh_ver = _get('tools', 'ssh', 'version', default='?')
    lines.append(f"SSH:      {ssh_path} ({ssh_ver})")
    lines.append(f"Log file: {log_path}")

    shown = len(log_lines)
    if total_lines and shown < total_lines:
        lines.append(f"Showing last {shown} of {total_lines} log lines")
    else:
        lines.append(f"Log lines: {shown}")
    lines.append("-" * 48)
    if not log_lines:
        lines.append("(log is empty or unreadable)")
    else:
        lines.extend(log_lines)
    lines.append("```")
    return "\n".join(lines)


def _resolve_crash_path(explicit: Optional[str] = None) -> str:
    """Return the most relevant crash report path, or '' if none has content.

    Prefers an explicit path (e.g. the rotated ``crash.log.previous`` handed to
    us after a detected crash), then falls back to the live ``crash.log``.
    """
    candidates = []
    if explicit:
        candidates.append(explicit)
    state = get_state_dir()
    candidates.append(os.path.join(state, 'crash.log.previous'))
    candidates.append(os.path.join(state, 'crash.log'))
    for path in candidates:
        try:
            if path and os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
        except Exception:
            continue
    return ''


def build_report_bundle(crash_path: Optional[str] = None, tail_lines: int = 400) -> str:
    """Clipboard-ready bug report: platform info + master log tail + crash report.

    Reuses the master-log diagnostic bundle and, when a crash report exists,
    appends its tail inside the same code block so a single paste carries
    everything a maintainer needs.
    """
    master_path = _resolve_log_path(_CATEGORY_MASTER)
    master_lines, master_total = _tail_file(master_path, tail_lines)
    bundle = _build_diagnostic_bundle(master_lines, master_total, master_path)

    crash = _resolve_crash_path(crash_path)
    if not crash:
        return bundle

    crash_lines, crash_total = _tail_file(crash, 200)
    extra: List[str] = ["", "```"]
    extra.append(f"Crash report: {crash}")
    if crash_total and len(crash_lines) < crash_total:
        extra.append(f"Showing last {len(crash_lines)} of {crash_total} lines")
    extra.append("-" * 48)
    extra.extend(crash_lines or ["(crash report is empty or unreadable)"])
    extra.append("```")
    return bundle + "\n" + "\n".join(extra)


# Keys whose values are redacted from the config copy in the diagnostics ZIP.
# Secrets normally live in the OS keyring, not config.json — this is
# defence-in-depth so a bug-report bundle never leaks credentials.
_SECRET_KEY_RE = re.compile(
    r'(pass(word|phrase)?|secret|token|credential|api[_-]?key|private[_-]?key)', re.I)


def _redact_config(obj):
    """Recursively replace secret-ish values with a placeholder."""
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                out[key] = "***REDACTED***"
            else:
                out[key] = _redact_config(value)
        return out
    if isinstance(obj, list):
        return [_redact_config(item) for item in obj]
    if isinstance(obj, str) and 'PRIVATE KEY-----' in obj:
        return "***REDACTED***"
    return obj


def build_diagnostics_zip(dest_path: str) -> str:
    """Write a self-contained diagnostics ZIP to dest_path and return it.

    Contents: logs/ (all log files incl. crash reports), system-info.txt
    (platform/runtime/tool details), version.txt, and a redacted config.json.
    Connection lists / ssh_config are intentionally excluded for privacy.
    """
    from .platform_utils import get_config_dir, APP_ID

    state = get_state_dir()
    with zipfile.ZipFile(dest_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        # Log files (current + rotated backups + crash reports).
        seen = set()
        for name in ('sshpilot.log', 'app.log', 'ssh.log',
                     'crash.log', 'crash.log.previous'):
            path = os.path.join(state, name)
            if os.path.isfile(path):
                zf.write(path, arcname='logs/' + name)
                seen.add(path)
        for path in sorted(glob.glob(os.path.join(state, '*.log.*'))):
            if path not in seen and os.path.isfile(path):
                zf.write(path, arcname='logs/' + os.path.basename(path))

        # System / runtime info. verbose=True for full storage/tool probing.
        try:
            from .startup_info import StartupInfo
            info = StartupInfo(verbose=True).info
        except Exception as exc:  # pragma: no cover - defensive
            info = {"error": "could not collect system info: %s" % exc}
        zf.writestr('system-info.txt', json.dumps(info, indent=2, default=str))

        # Version.
        try:
            from . import __version__ as version
        except Exception:
            version = "unknown"
        zf.writestr('version.txt',
                    "sshPilot %s\napplication-id: %s\n" % (version, APP_ID))

        # Redacted config.
        cfg_path = os.path.join(get_config_dir(), 'config.json')
        try:
            if os.path.isfile(cfg_path):
                with open(cfg_path, encoding='utf-8') as fh:
                    data = json.load(fh)
                zf.writestr('config.json',
                            json.dumps(_redact_config(data), indent=2, default=str))
        except Exception as exc:
            zf.writestr('config.json', "(could not read/redact config: %s)" % exc)

    return dest_path


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/log_viewer_window.ui")
class LogViewerWindow(Adw.Window):
    """Main window's "Help → View Logs" target.

    Designed so a non-technical user can hit Help → View Logs, click
    *Copy for bug report*, and paste a self-contained, code-fenced summary
    into a GitHub issue. The full log file is also reachable via *Open Log
    Folder* in case a developer asks for it.

    The static chrome (header, banner, filter/search rows, log text view) is
    defined in resources/ui/log_viewer_window.blp; the dropdown models, text
    tags, live-tail wiring, and the More-actions menu stay in Python.
    """

    __gtype_name__ = "SshPilotLogViewerWindow"

    follow_toggle = Gtk.Template.Child()
    copy_btn = Gtk.Template.Child()
    menu_button = Gtk.Template.Child()
    banner = Gtk.Template.Child()
    path_label = Gtk.Template.Child()
    stats_label = Gtk.Template.Child()
    category_dropdown = Gtk.Template.Child()
    level_dropdown = Gtk.Template.Child()
    search_entry = Gtk.Template.Child()
    textview = Gtk.Template.Child()

    def __init__(self, parent: Optional[Gtk.Window] = None):
        super().__init__()
        if parent is not None:
            self.set_transient_for(parent)

        # Template-child aliases keep the rest of the class using its original
        # ``self._x`` names (the .blp ids are underscore-free identifiers).
        self._banner = self.banner
        self._path_label = self.path_label
        self._stats_label = self.stats_label
        self._category_dropdown = self.category_dropdown
        self._level_dropdown = self.level_dropdown
        self._follow_toggle = self.follow_toggle
        self._search_entry = self.search_entry
        self._textview = self.textview

        # Ordered tuple of (category-id, display-label, file-path). Drives
        # the category dropdown. We rebuild path lookups on each refresh in
        # case files appear/disappear between viewings (rotation, sandbox
        # quirks).
        self._categories: List[tuple] = [
            (_CATEGORY_MASTER,  _("All"),     _resolve_log_path(_CATEGORY_MASTER)),
            (_CATEGORY_APP,     _("App"),     _resolve_log_path(_CATEGORY_APP)),
            (_CATEGORY_SSH,     _("SSH"),     _resolve_log_path(_CATEGORY_SSH)),
            (_CATEGORY_ASKPASS, _("Askpass"), _resolve_log_path(_CATEGORY_ASKPASS)),
            (_CATEGORY_CRASH,   _("Crash"),   _resolve_log_path(_CATEGORY_CRASH)),
        ]
        self._current_category: str = _CATEGORY_MASTER
        self._log_path: str = self._categories[0][2]
        self._tail_lines = _DEFAULT_TAIL_LINES
        self._showing_full_file = False
        self._current_lines: List[str] = []
        self._current_total: int = 0

        # --- Live-tail state ---------------------------------------------
        # File monitor + last-known byte position, so we only read the bytes
        # appended since the previous tick rather than re-scanning the whole
        # file on every change event.
        self._file_monitor: Optional[Gio.FileMonitor] = None
        self._tail_pos: int = 0
        # When True, new lines auto-append AND we auto-scroll to the end.
        # Toggled by the Follow switch in the header.
        self._follow_enabled: bool = True

        # --- Level filter state ------------------------------------------
        # Viewer-side filter; INDEPENDENT of the persistent app log level
        # ("logging.level" in Preferences → Advanced). The app log level
        # controls what gets *captured* on disk; this filter only controls
        # what we *show* from what's already there. Hiding here doesn't drop
        # anything from the file.
        # Index aligns with _LEVEL_FILTER_OPTIONS below.
        self._level_filter_idx: int = 0

        # Free-text search query, lowercased. Empty string = no filter.
        # Always-visible search entry — no keyboard shortcut binding (user
        # request) — so it's discoverable purely by sight.
        self._search_query: str = ""

        # Category dropdown model (widget + tooltip are in the template).
        cat_model = Gtk.StringList()
        for _id, label, _path in self._categories:
            cat_model.append(label)
        self._category_dropdown.set_model(cat_model)
        self._category_dropdown.set_selected(0)
        self._category_dropdown.connect(
            "notify::selected", self._on_category_changed
        )

        # Level filter dropdown model — VIEWER-side, independent of the
        # persistent app log level.
        level_model = Gtk.StringList()
        for label, _lvl in _LEVEL_FILTER_OPTIONS:
            level_model.append(label)
        self._level_dropdown.set_model(level_model)
        self._level_dropdown.set_selected(self._level_filter_idx)
        self._level_dropdown.connect(
            "notify::selected", self._on_level_filter_changed
        )

        self._follow_toggle.connect("toggled", self._on_follow_toggled)
        self.copy_btn.connect("clicked", self._on_copy_clicked)

        # Secondary actions: folder, refresh, save-as, toggle full file.
        action_group = Gio.SimpleActionGroup()
        for name, handler in (
            ("refresh", self._do_refresh),
            ("open-folder", self._do_open_folder),
            ("save-as", self._do_save_as),
            ("toggle-full", self._do_toggle_full),
            ("export-diagnostics", self._do_export_diagnostics),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda _a, _p, h=handler: h())
            action_group.add_action(act)
        self.insert_action_group("logview", action_group)
        self._action_group = action_group

        menu_model = Gio.Menu()
        menu_model.append(_("Refresh"), "logview.refresh")
        menu_model.append(_("Open Log Folder"), "logview.open-folder")
        menu_model.append(_("Save Log As…"), "logview.save-as")
        menu_model.append(_("Export Diagnostics…"), "logview.export-diagnostics")
        menu_model.append(_("Toggle Full File"), "logview.toggle-full")
        self.menu_button.set_menu_model(menu_model)

        # Dynamic path-strip content (widget is templated).
        self._path_label.set_label(self._log_path)
        self._path_label.set_tooltip_text(self._log_path)

        self._search_entry.connect("search-changed", self._on_search_changed)

        # Per-level highlight tags. INFO/DEBUG are intentionally left
        # unstyled so they read as background context; only the levels that
        # warrant attention get color. Foregrounds are mid-tones chosen to
        # remain legible in both light and dark Adwaita variants.
        buf = self._textview.get_buffer()
        self._tag_warning = buf.create_tag(
            "level-warning", foreground="#e66100"
        )
        self._tag_error = buf.create_tag(
            "level-error", foreground="#c01c28",
            weight=Pango.Weight.BOLD,
        )
        self._tag_critical = buf.create_tag(
            "level-critical", foreground="#ffffff",
            background="#c01c28", weight=Pango.Weight.BOLD,
        )
        # Search-match highlight (gets layered on top of level tags).
        self._tag_match = buf.create_tag(
            "search-match", background="#f5c211",
            foreground="#000000",
        )

        # Make sure we drop the file monitor when the window closes —
        # otherwise the GFile handle stays bound for the rest of the session.
        self.connect("close-request", self._on_close_request)

        self._do_refresh()

    def _on_close_request(self, _window) -> bool:
        self._stop_monitor()
        return False  # let the window close normally

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        """(Re)read the log, render with the active level filter, and start
        watching for live updates."""
        # Drop any monitor pointing at a previously-shown file. We always
        # restart so category switches / refresh / rotation handling all
        # converge on a single code path.
        self._stop_monitor()

        if self._showing_full_file:
            text = _read_full_file(self._log_path)
            self._current_lines = text.splitlines()
            self._current_total = len(self._current_lines)
        else:
            self._current_lines, self._current_total = _tail_file(
                self._log_path, self._tail_lines
            )

        # Record the byte position so future live-tail reads only pick up
        # what's been appended since now.
        self._tail_pos = self._current_file_size()

        self._render_buffer()

        # Update the stats line — separate from the buffer text so we can
        # reuse the same code for live appends.
        self._update_stats_label()

        # Auto-scroll to the bottom on a fresh load — newest log lines are
        # the most useful.
        GLib.idle_add(self._scroll_to_end)

        # Start watching the file we just loaded.
        self._start_monitor()

    def _level_tag(self, level: int):
        """Pick the TextTag for *level*, or None for unstyled (INFO/DEBUG)."""
        if level >= logging.CRITICAL:
            return self._tag_critical
        if level >= logging.ERROR:
            return self._tag_error
        if level >= logging.WARNING:
            return self._tag_warning
        return None

    def _line_passes_filters(self, line: str, min_level: int) -> bool:
        """Apply level + search filters together. Both must allow the line."""
        if min_level > 0 and _level_of_line(line) < min_level:
            return False
        query = getattr(self, "_search_query", "")
        if query and query not in line.lower():
            return False
        return True

    def _render_buffer(self) -> None:
        """Filter ``_current_lines`` by the active filters and write to the view.

        Writes one line at a time so we can apply a level-specific TextTag
        per line. Slightly slower than a single ``set_text`` but only on the
        initial paint — subsequent live appends use the same per-line path.
        """
        buf = self._textview.get_buffer()
        buf.set_text("")  # clear, also resets tags

        if not self._current_lines and not os.path.isfile(self._log_path):
            buf.set_text(_(
                "No log file at {path} yet.\n\n"
                "Use the app for a moment and refresh — entries will appear "
                "as soon as something is logged."
            ).format(path=self._log_path))
            self._stats_label.set_text("")
            return

        if not self._current_lines:
            buf.set_text(_("(log is empty)"))
            return

        min_level = _LEVEL_FILTER_OPTIONS[self._level_filter_idx][1]
        shown_count = 0
        for line in self._current_lines:
            if not self._line_passes_filters(line, min_level):
                continue
            self._append_line_with_tag(buf, line, first=(shown_count == 0))
            shown_count += 1

        if shown_count == 0:
            buf.set_text(_(
                "No lines match the current filters. Try lowering the level "
                "filter or clearing the search box."
            ))

        self._filtered_visible = shown_count

    def _append_line_with_tag(self, buf, line: str, first: bool = False) -> None:
        """Append *line* (with leading newline if not first) and tag by level."""
        prefix = "" if first else "\n"
        end = buf.get_end_iter()
        line_start_offset = end.get_offset() + len(prefix)
        buf.insert(end, prefix + line)
        tag = self._level_tag(_level_of_line(line))
        if tag is not None:
            start_iter = buf.get_iter_at_offset(line_start_offset)
            end_iter = buf.get_iter_at_offset(line_start_offset + len(line))
            buf.apply_tag(tag, start_iter, end_iter)
        # Search-match overlay (layered on top of the level tag).
        query = getattr(self, "_search_query", "")
        if query:
            lower_line = line.lower()
            search_from = 0
            while True:
                idx = lower_line.find(query, search_from)
                if idx == -1:
                    break
                m_start = buf.get_iter_at_offset(line_start_offset + idx)
                m_end = buf.get_iter_at_offset(line_start_offset + idx + len(query))
                buf.apply_tag(self._tag_match, m_start, m_end)
                search_from = idx + len(query)

    def _update_stats_label(self) -> None:
        """Refresh the right-aligned ``last N of M lines`` indicator."""
        filtered_count = getattr(self, "_filtered_visible", len(self._current_lines))
        if self._showing_full_file:
            base = _("{n} lines (full file)").format(n=self._current_total)
        elif self._current_total > len(self._current_lines):
            base = _("last {shown} of {total} lines").format(
                shown=len(self._current_lines), total=self._current_total,
            )
        else:
            base = _("{n} lines").format(n=self._current_total)

        if self._level_filter_idx > 0 and filtered_count != len(self._current_lines):
            base += _(" · {f} after filter").format(f=filtered_count)
        self._stats_label.set_text(base)

        # Auto-scroll to the bottom — newest log lines are what's interesting.
        GLib.idle_add(self._scroll_to_end)

    def _scroll_to_end(self) -> bool:
        buf = self._textview.get_buffer()
        end_iter = buf.get_end_iter()
        # Use a mark so scroll_to_mark is honoured even after layout.
        mark = buf.create_mark(None, end_iter, False)
        self._textview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
        buf.delete_mark(mark)
        return False

    # ---------------------------------------------------------------- actions

    # --------------------------------------------------------------- live tail

    def _current_file_size(self) -> int:
        try:
            return os.path.getsize(self._log_path) if os.path.isfile(self._log_path) else 0
        except OSError:
            return 0

    def _start_monitor(self) -> None:
        """Begin watching ``self._log_path`` for appends / rotation."""
        if not self._log_path:
            return
        try:
            gfile = Gio.File.new_for_path(self._log_path)
            monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
        except Exception as exc:
            logger.debug("Could not start file monitor for %s: %s", self._log_path, exc)
            return
        # Coalesce rapid bursts so the GTK main loop isn't hammered when
        # logging is busy (e.g. DEBUG with chatty SFTP traffic).
        try:
            monitor.set_rate_limit(150)  # milliseconds
        except Exception:
            pass
        monitor.connect("changed", self._on_log_file_changed)
        self._file_monitor = monitor

    def _stop_monitor(self) -> None:
        if self._file_monitor is not None:
            try:
                self._file_monitor.cancel()
            except Exception:
                pass
            self._file_monitor = None

    def _on_log_file_changed(self, _monitor, _file, _other, event_type) -> None:
        """React to FileMonitor events: append new bytes, handle rotation."""
        if not self._follow_enabled:
            # User explicitly paused following. We still leave the monitor
            # connected so we resume cleanly when they toggle it back on,
            # but skip the work.
            return

        # Rotation / re-creation. The handler logger may have truncated the
        # old file or created a new one with the same name; either way the
        # safe play is to re-load from disk and reset our position.
        if event_type in (
            Gio.FileMonitorEvent.DELETED,
            Gio.FileMonitorEvent.MOVED_OUT,
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.MOVED_IN,
        ):
            self._reload_after_rotation()
            return

        new_size = self._current_file_size()
        if new_size < self._tail_pos:
            # The file shrank — almost always rotation.
            self._reload_after_rotation()
            return
        if new_size == self._tail_pos:
            return  # nothing actually new

        self._append_since(self._tail_pos, new_size)
        self._tail_pos = new_size

    def _reload_after_rotation(self) -> None:
        # Defer to idle so we don't fight ongoing FileMonitor events.
        def _do() -> bool:
            self._stop_monitor()
            self._load()
            return False
        GLib.idle_add(_do)

    def _append_since(self, start_pos: int, end_pos: int) -> None:
        """Read bytes ``[start_pos, end_pos)`` and append survivors to the view."""
        try:
            with open(self._log_path, 'rb') as fh:
                fh.seek(start_pos)
                chunk = fh.read(max(0, end_pos - start_pos))
        except Exception as exc:
            logger.debug("Could not read appended log bytes: %s", exc)
            return
        if not chunk:
            return
        try:
            text = chunk.decode('utf-8', errors='replace')
        except Exception:
            return

        # Split into individual lines; ignore a trailing partial line by
        # leaving it for the next tick (rare in practice — log handlers
        # flush whole records).
        new_lines = text.split('\n')
        if not new_lines:
            return
        if new_lines[-1] == '':
            new_lines.pop()
        if not new_lines:
            return

        # Track full set (for refilter on dropdown change) and filter for
        # display in one pass.
        self._current_lines.extend(new_lines)
        self._current_total += len(new_lines)

        # Keep the in-memory tail bounded: leaving the viewer open with
        # live-tail running must not grow without limit as the log file does.
        # _current_total keeps the true running count for the stats line.
        # Full-file mode is an explicit request to hold everything, so skip it.
        if not self._showing_full_file and len(self._current_lines) > self._tail_lines:
            del self._current_lines[:-self._tail_lines]

        min_level = _LEVEL_FILTER_OPTIONS[self._level_filter_idx][1]
        visible = [
            ln for ln in new_lines
            if self._line_passes_filters(ln, min_level)
        ]
        if not visible:
            # Nothing passed the filters, but the file did grow — refresh
            # the stats line so the user can see the running totals.
            self._filtered_visible = getattr(self, "_filtered_visible", 0)
            self._update_stats_label()
            return

        was_at_bottom = self._user_is_at_bottom()

        buf = self._textview.get_buffer()
        # Per-line append so each gets its level tag. First line gets the
        # leading newline only if the buffer already has content.
        first_first = (buf.get_char_count() == 0)
        for i, ln in enumerate(visible):
            self._append_line_with_tag(buf, ln, first=(first_first and i == 0))

        # Update counters and stats. _filtered_visible is the running count
        # of lines actually displayed.
        self._filtered_visible = getattr(self, "_filtered_visible", 0) + len(visible)

        # Bound the displayed buffer too — the GTK TextBuffer is the heavier
        # memory consumer. Drop the oldest visible lines from the top once we
        # exceed the tail cap. A filter-dropdown change rebuilds the buffer
        # from _current_lines, so any divergence self-heals. Trimming the top
        # doesn't affect the bottom-scroll handling below.
        if not self._showing_full_file:
            overflow = self._filtered_visible - self._tail_lines
            if overflow > 0:
                start = buf.get_start_iter()
                end = buf.get_start_iter()
                end.forward_lines(overflow)
                buf.delete(start, end)
                self._filtered_visible -= overflow

        self._update_stats_label()

        if was_at_bottom:
            GLib.idle_add(self._scroll_to_end)

    def _user_is_at_bottom(self, epsilon: float = 4.0) -> bool:
        """True when the scroll position is at (or within a few px of) the end.

        Used to decide whether to keep auto-following: yanking the scroll
        position to the bottom while the user is reading older content is
        worse UX than them missing a few live lines.
        """
        try:
            parent = self._textview.get_parent()
            # Walk up to the ScrolledWindow.
            while parent is not None and not isinstance(parent, Gtk.ScrolledWindow):
                parent = parent.get_parent()
            if parent is None:
                return True  # no scrolling possible → always at end
            vadj = parent.get_vadjustment()
            if vadj is None:
                return True
            return (
                vadj.get_value() + vadj.get_page_size()
                >= vadj.get_upper() - epsilon
            )
        except Exception:
            return True

    def _on_follow_toggled(self, button: Gtk.ToggleButton) -> None:
        self._follow_enabled = bool(button.get_active())
        if self._follow_enabled:
            # Pull in anything that arrived while we were paused.
            new_size = self._current_file_size()
            if new_size > self._tail_pos:
                self._append_since(self._tail_pos, new_size)
                self._tail_pos = new_size

    def _on_level_filter_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        idx = int(dropdown.get_selected())
        if 0 <= idx < len(_LEVEL_FILTER_OPTIONS) and idx != self._level_filter_idx:
            self._level_filter_idx = idx
            self._render_buffer()
            self._update_stats_label()
            GLib.idle_add(self._scroll_to_end)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        new_query = entry.get_text().lower()
        if new_query == self._search_query:
            return
        self._search_query = new_query
        # Re-render from the cached lines; no file I/O needed.
        self._render_buffer()
        self._update_stats_label()

    def _do_refresh(self) -> None:
        self._load()

    def _do_open_folder(self) -> None:
        """Open the directory containing sshpilot.log in the desktop file manager."""
        folder = os.path.dirname(self._log_path) or '.'
        try:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(folder))
            launcher.launch(self, None, None, None)
        except Exception as exc:
            # Fallback: try Gio.AppInfo.launch_default_for_uri.
            try:
                Gio.AppInfo.launch_default_for_uri(
                    Gio.File.new_for_path(folder).get_uri(), None
                )
            except Exception as inner_exc:
                logger.warning(
                    "Could not open log folder %s: %s / %s",
                    folder, exc, inner_exc,
                )

    def _do_export_diagnostics(self) -> None:
        """Save a full diagnostics ZIP (logs + system info + redacted config).

        Delegates to the parent window's handler (which shows a success dialog
        with "Open Folder"); falls back to a minimal self-contained save.
        """
        parent = self.get_transient_for()
        if parent is not None and hasattr(parent, 'on_export_diagnostics_action'):
            parent.on_export_diagnostics_action()
            return
        from datetime import datetime
        dialog = Gtk.FileDialog.new()
        dialog.set_title(_("Export Diagnostics"))
        dialog.set_initial_name(
            "sshpilot-diagnostics-%s.zip" % datetime.now().strftime('%Y%m%d-%H%M%S'))

        def _on_save(d, result):
            try:
                gfile = d.save_finish(result)
            except GLib.Error:
                return
            if gfile is None:
                return
            try:
                build_diagnostics_zip(gfile.get_path())
            except Exception as exc:
                logger.error("Export diagnostics failed: %s", exc, exc_info=True)

        dialog.save(self, None, _on_save)

    def _do_save_as(self) -> None:
        """Let the user save a copy of the current log file via Gtk.FileDialog."""
        if not os.path.isfile(self._log_path):
            return
        try:
            dialog = Gtk.FileDialog.new()
            dialog.set_title(_("Save Log As"))
            dialog.set_initial_name("sshpilot.log")

            def _on_done(_dlg, result):
                try:
                    gfile = dialog.save_finish(result)
                except GLib.Error:
                    return  # user cancelled
                if gfile is None:
                    return
                dest_path = gfile.get_path()
                if not dest_path:
                    return
                try:
                    # Use shutil.copyfile so we copy the *file on disk*,
                    # including lines added since the viewer opened.
                    import shutil
                    shutil.copyfile(self._log_path, dest_path)
                    logger.info("Saved log copy to %s", dest_path)
                except Exception as exc:
                    logger.error("Failed to save log copy: %s", exc)

            dialog.save(self, None, _on_done)
        except Exception as exc:
            logger.warning("Save-as failed: %s", exc)

    def _do_toggle_full(self) -> None:
        self._showing_full_file = not self._showing_full_file
        self._load()

    def _on_category_changed(self, dropdown: Gtk.DropDown, _pspec) -> None:
        idx = int(dropdown.get_selected())
        if idx < 0 or idx >= len(self._categories):
            return
        cat_id, _label, path = self._categories[idx]
        if cat_id == self._current_category:
            return
        # Re-resolve the crash path so we always point at the freshest report.
        if cat_id == _CATEGORY_CRASH:
            path = _resolve_log_path(_CATEGORY_CRASH)
        self._current_category = cat_id
        self._log_path = path or self._log_path
        # Reset the "show full file" toggle on category switch — easy to
        # surprise yourself otherwise.
        self._showing_full_file = False
        # Update the path strip so the user always sees exactly which file
        # they're looking at.
        self._path_label.set_label(self._log_path)
        self._path_label.set_tooltip_text(self._log_path)
        self._load()

    def _on_copy_clicked(self, _btn) -> None:
        """Build the diagnostic bundle and put it on the clipboard.

        Always sources from the master ``sshpilot.log`` (plus the crash report
        if present) so bug reports contain the full picture, regardless of which
        category the user is currently viewing.
        """
        bundle = build_report_bundle()
        try:
            display = self.get_display() or Gdk.Display.get_default()
            if display is not None:
                clipboard = display.get_clipboard()
                clipboard.set(bundle)
        except Exception as exc:
            logger.error("Could not copy log bundle to clipboard: %s", exc)
            return

        # Lightweight confirmation: flash the banner with a success message.
        try:
            self._banner.set_title(
                _("Copied — paste it into your bug report.")
            )
            # Restore the privacy notice after a few seconds.
            def _restore() -> bool:
                self._banner.set_title(
                    _("Log lines may contain hostnames, usernames, and file paths. "
                      "Review before sharing.")
                )
                return False
            GLib.timeout_add_seconds(4, _restore)
        except Exception:
            pass
