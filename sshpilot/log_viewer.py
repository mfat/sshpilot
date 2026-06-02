"""Log viewer dialog.

Lets ordinary users review the application log and copy a tidy diagnostic
bundle (platform info + recent log tail) to the clipboard so they can paste
it into a bug report — no command-line, no file-system spelunking.

Lives under the main window's Help submenu (``win.view-logs``).
"""

from __future__ import annotations

import collections
import logging
import os
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
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
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
        with open(path, 'r', encoding='utf-8', errors='replace') as fh:
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
    # Import here so this module stays cheap to import.
    try:
        from .startup_info import StartupInfo
        info = StartupInfo().info
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


class LogViewerWindow(Adw.Window):
    """Main window's "Help → View Logs" target.

    Designed so a non-technical user can hit Help → View Logs, click
    *Copy for bug report*, and paste a self-contained, code-fenced summary
    into a GitHub issue. The full log file is also reachable via *Open Log
    Folder* in case a developer asks for it.
    """

    def __init__(self, parent: Optional[Gtk.Window] = None):
        super().__init__()
        if parent is not None:
            self.set_transient_for(parent)
        # Non-modal so the user can keep using the app while reviewing logs.
        self.set_modal(False)
        self.set_default_size(900, 640)
        self.set_title(_("Application Log"))

        # Ordered tuple of (category-id, display-label, file-path). Drives
        # the category dropdown. We rebuild path lookups on each refresh in
        # case files appear/disappear between viewings (rotation, sandbox
        # quirks).
        self._categories: List[tuple] = [
            (_CATEGORY_MASTER,  _("All"),     _resolve_log_path(_CATEGORY_MASTER)),
            (_CATEGORY_APP,     _("App"),     _resolve_log_path(_CATEGORY_APP)),
            (_CATEGORY_SSH,     _("SSH"),     _resolve_log_path(_CATEGORY_SSH)),
            (_CATEGORY_ASKPASS, _("Askpass"), _resolve_log_path(_CATEGORY_ASKPASS)),
        ]
        self._current_category: str = _CATEGORY_MASTER
        self._log_path: str = self._categories[0][2]
        self._tail_lines = _DEFAULT_TAIL_LINES
        self._showing_full_file = False
        self._current_lines: List[str] = []
        self._current_total: int = 0

        toolbar = Adw.ToolbarView()
        self.set_content(toolbar)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Application Log")))
        toolbar.add_top_bar(header)

        # Category dropdown — sits at the start of the header so it reads
        # as the primary view-switcher control.
        cat_model = Gtk.StringList()
        for _id, label, _path in self._categories:
            cat_model.append(label)
        self._category_dropdown = Gtk.DropDown(model=cat_model)
        self._category_dropdown.set_selected(0)
        self._category_dropdown.set_tooltip_text(
            _("Which log file to view. The bug-report bundle always uses the "
              "complete master log regardless of this selection.")
        )
        self._category_dropdown.connect(
            "notify::selected", self._on_category_changed
        )
        header.pack_start(self._category_dropdown)

        # Primary action: copy a self-contained diagnostic bundle.
        copy_btn = Gtk.Button(label=_("Copy log output"))
        copy_btn.add_css_class("suggested-action")
        copy_btn.set_tooltip_text(
            _("Copy platform info and recent log lines to the clipboard, "
              "formatted for pasting into a bug report. Always uses the "
              "master log regardless of the current view selection.")
        )
        copy_btn.connect("clicked", self._on_copy_clicked)
        header.pack_end(copy_btn)

        # Secondary actions: folder, refresh, save-as, toggle full file.
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_tooltip_text(_("More log actions"))

        action_group = Gio.SimpleActionGroup()
        for name, handler in (
            ("refresh", self._do_refresh),
            ("open-folder", self._do_open_folder),
            ("save-as", self._do_save_as),
            ("toggle-full", self._do_toggle_full),
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
        menu_model.append(_("Toggle Full File"), "logview.toggle-full")
        menu_button.set_menu_model(menu_model)
        header.pack_end(menu_button)

        # Content: privacy banner + log path strip + scrollable log text.
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        toolbar.set_content(body)

        self._banner = Adw.Banner.new(
            _("Log lines may contain hostnames, usernames, and file paths. "
              "Review before sharing.")
        )
        self._banner.set_revealed(True)
        body.append(self._banner)

        # Path strip — small monospace row showing exactly where the log
        # lives on disk so users can find it without us.
        path_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        path_row.set_margin_top(8)
        path_row.set_margin_bottom(4)
        path_row.set_margin_start(12)
        path_row.set_margin_end(12)
        path_label_caption = Gtk.Label(label=_("Log file:"))
        path_label_caption.add_css_class("caption")
        path_label_caption.add_css_class("dim-label")
        path_row.append(path_label_caption)
        self._path_label = Gtk.Label(label=self._log_path)
        self._path_label.add_css_class("caption")
        self._path_label.set_selectable(True)
        self._path_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._path_label.set_hexpand(True)
        self._path_label.set_xalign(0.0)
        self._path_label.set_tooltip_text(self._log_path)
        path_row.append(self._path_label)
        self._stats_label = Gtk.Label()
        self._stats_label.add_css_class("caption")
        self._stats_label.add_css_class("dim-label")
        path_row.append(self._stats_label)
        body.append(path_row)

        # Scrollable monospace log view.
        self._textview = Gtk.TextView()
        self._textview.set_monospace(True)
        self._textview.set_editable(False)
        self._textview.set_cursor_visible(False)
        self._textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._textview.set_top_margin(8)
        self._textview.set_bottom_margin(8)
        self._textview.set_left_margin(12)
        self._textview.set_right_margin(12)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_child(self._textview)
        body.append(scrolled)

        self._do_refresh()

    # ------------------------------------------------------------------ load

    def _load(self) -> None:
        """Read the log (either tail or full) and render it."""
        if self._showing_full_file:
            text = _read_full_file(self._log_path)
            self._current_lines = text.splitlines()
            self._current_total = len(self._current_lines)
        else:
            self._current_lines, self._current_total = _tail_file(
                self._log_path, self._tail_lines
            )

        buf = self._textview.get_buffer()
        if not self._current_lines and not os.path.isfile(self._log_path):
            buf.set_text(_(
                "No log file at {path} yet.\n\n"
                "Use the app for a moment and refresh — entries will appear "
                "as soon as something is logged."
            ).format(path=self._log_path))
            self._stats_label.set_text("")
            return

        buf.set_text("\n".join(self._current_lines) if self._current_lines else _("(log is empty)"))

        if self._showing_full_file:
            self._stats_label.set_text(
                _("{n} lines (full file)").format(n=self._current_total)
            )
        elif self._current_total > len(self._current_lines):
            self._stats_label.set_text(
                _("last {shown} of {total} lines").format(
                    shown=len(self._current_lines), total=self._current_total
                )
            )
        else:
            self._stats_label.set_text(
                _("{n} lines").format(n=self._current_total)
            )

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

        Always sources from the master ``sshpilot.log`` so bug reports
        contain the full picture, regardless of which category the user is
        currently viewing.
        """
        master_path = _resolve_log_path(_CATEGORY_MASTER)
        master_lines, master_total = _tail_file(master_path, self._tail_lines)
        bundle = _build_diagnostic_bundle(master_lines, master_total, master_path)
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
