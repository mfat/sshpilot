"""Docker Console tab: Logs — snapshot / in-pane follow, search, ring buffer."""

from __future__ import annotations

from collections import deque
from typing import Any, List, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gio, Gtk, Gdk, Pango  # noqa: E402

from . import widgets as w  # noqa: E402

_DEFAULT_LOG_TAIL = 200
_DEFAULT_MAX_LOG_LINES = 2000


class LogsTabMixin:
    """Logs tab: snapshot or Follow stream, match navigation, ring buffer."""

    def _build_logs_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.append(Gtk.Label(label="Container:"))
        self._logs_combo = Gtk.DropDown.new_from_strings(["(refresh containers)"])
        self._logs_combo.set_hexpand(True)
        self._syncing_logs_combo = False
        self._logs_combo.connect("notify::selected", self._on_logs_target_changed)
        toolbar.append(self._logs_combo)

        toolbar.append(Gtk.Label(label="Tail:"))
        self._tail_spin = Gtk.SpinButton.new_with_range(10, 5000, 50)
        self._tail_spin.set_value(self._log_tail_setting())
        toolbar.append(self._tail_spin)

        self._ts_switch = Gtk.Switch()
        self._ts_switch.set_tooltip_text("Show timestamps (-t)")
        self._ts_switch.set_valign(Gtk.Align.CENTER)
        toolbar.append(Gtk.Label(label="Timestamps"))
        toolbar.append(self._ts_switch)
        box.append(toolbar)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        load = Gtk.Button(label="Load")
        load.connect("clicked", lambda _b: self._reload_logs())
        actions.append(load)
        self._logs_follow = Gtk.ToggleButton(label="Follow")
        self._logs_follow.set_tooltip_text(
            "Stream new log lines into this view (docker logs -f)")
        self._logs_follow.connect("toggled", self._on_logs_follow_toggled)
        actions.append(self._logs_follow)
        follow_term = Gtk.Button(label="Follow in terminal")
        follow_term.connect("clicked", lambda _b: self._follow_logs_selected())
        actions.append(follow_term)
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.set_tooltip_text("Copy logs")
        copy.connect("clicked", lambda _b: self._copy_logs())
        actions.append(copy)
        save = Gtk.Button(icon_name="document-save-symbolic")
        save.set_tooltip_text("Save logs…")
        save.connect("clicked", lambda _b: self._save_logs())
        actions.append(save)
        clear = Gtk.Button(icon_name="edit-clear-all-symbolic")
        clear.set_tooltip_text("Clear")
        clear.connect("clicked", lambda _b: self._clear_logs())
        actions.append(clear)
        box.append(actions)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._logs_search = Gtk.SearchEntry()
        self._logs_search.set_placeholder_text("Search logs…")
        self._logs_search.set_hexpand(True)
        self._logs_search.connect("search-changed", lambda _e: self._on_logs_search_changed())
        self._logs_search.connect("activate", lambda _e: self._logs_match_next())
        filters.append(self._logs_search)

        self._logs_case = Gtk.ToggleButton(label="Aa")
        self._logs_case.set_tooltip_text("Case-sensitive search")
        self._logs_case.connect("toggled", lambda _b: self._on_logs_search_changed())
        filters.append(self._logs_case)

        self._logs_highlight_mode = Gtk.ToggleButton(label="Highlight")
        self._logs_highlight_mode.set_tooltip_text(
            "Highlight matches instead of filtering lines out")
        self._logs_highlight_mode.set_active(True)
        self._logs_highlight_mode.connect("toggled", lambda _b: self._apply_log_filter())
        filters.append(self._logs_highlight_mode)

        prev_btn = Gtk.Button(icon_name="go-up-symbolic")
        prev_btn.set_tooltip_text("Previous match")
        prev_btn.connect("clicked", lambda _b: self._logs_match_prev())
        filters.append(prev_btn)
        next_btn = Gtk.Button(icon_name="go-down-symbolic")
        next_btn.set_tooltip_text("Next match")
        next_btn.connect("clicked", lambda _b: self._logs_match_next())
        filters.append(next_btn)
        self._logs_match_label = Gtk.Label(label="")
        self._logs_match_label.add_css_class("dim-label")
        self._logs_match_label.add_css_class("caption")
        filters.append(self._logs_match_label)

        self._logs_errors_only = Gtk.ToggleButton(label="Errors only")
        self._logs_errors_only.set_tooltip_text(
            "Show only lines mentioning error/warn/fail/fatal")
        self._logs_errors_only.connect("toggled", lambda _b: self._apply_log_filter())
        filters.append(self._logs_errors_only)
        self._logs_autoscroll = Gtk.ToggleButton(label="Auto-scroll")
        self._logs_autoscroll.set_active(True)
        filters.append(self._logs_autoscroll)
        box.append(filters)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self._logs_view = Gtk.TextView()
        self._logs_view.set_editable(False)
        self._logs_view.set_monospace(True)
        self._logs_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._logs_buffer = self._logs_view.get_buffer()
        self._logs_tag_match = self._logs_buffer.create_tag(
            "log-match", background="#f9e2af", foreground="#1e1e2e")
        self._logs_tag_current = self._logs_buffer.create_tag(
            "log-match-current", background="#89b4fa", foreground="#1e1e2e",
            weight=Pango.Weight.BOLD)
        scroller.set_child(self._logs_view)

        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(scroller)
        self._logs_pulse = self._make_docker_mark(56)
        self._logs_pulse.set_visible(False)
        overlay.add_overlay(self._logs_pulse)
        box.append(overlay)

        # Ring buffer + follow stream state (also reset on host change).
        self._logs_lines: deque = deque(maxlen=self._max_log_lines_setting())
        self._logs_raw = ""
        self._logs_stream = None
        self._logs_stream_gen = 0
        self._logs_match_offsets: List[int] = []
        self._logs_match_index = -1
        self._logs_pending_lines: List[str] = []
        self._logs_flush_source: Optional[int] = None
        self._syncing_logs_follow = False
        return box

    def _log_tail_setting(self) -> int:
        try:
            return max(10, int(self.ctx.settings.get("log_tail", _DEFAULT_LOG_TAIL)))
        except (TypeError, ValueError):
            return _DEFAULT_LOG_TAIL

    def _max_log_lines_setting(self) -> int:
        try:
            return max(100, int(self.ctx.settings.get(
                "max_log_lines", _DEFAULT_MAX_LOG_LINES)))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_LOG_LINES

    def _resize_logs_ring(self) -> None:
        maxlen = self._max_log_lines_setting()
        if self._logs_lines.maxlen == maxlen:
            return
        self._logs_lines = deque(self._logs_lines, maxlen=maxlen)

    def _stop_logs_follow(self) -> None:
        self._logs_stream_gen += 1
        handle = getattr(self, "_logs_stream", None)
        self._logs_stream = None
        if handle is not None:
            try:
                handle.stop()
            except Exception:  # noqa: BLE001
                pass
        if getattr(self, "_logs_flush_source", None) is not None:
            GLib.source_remove(self._logs_flush_source)
            self._logs_flush_source = None
        self._logs_pending_lines = []
        follow = getattr(self, "_logs_follow", None)
        if follow is not None and follow.get_active():
            self._syncing_logs_follow = True
            try:
                follow.set_active(False)
            finally:
                self._syncing_logs_follow = False

    def _on_logs_follow_toggled(self, btn: Gtk.ToggleButton) -> None:
        if getattr(self, "_syncing_logs_follow", False):
            return
        if btn.get_active():
            self._start_logs_follow()
        else:
            self._stop_logs_follow_keep_toggle()

    def _stop_logs_follow_keep_toggle(self) -> None:
        self._logs_stream_gen += 1
        handle = self._logs_stream
        self._logs_stream = None
        if handle is not None:
            try:
                handle.stop()
            except Exception:  # noqa: BLE001
                pass

    def _start_logs_follow(self) -> None:
        client = self._client()
        nick = self._current_nickname()
        cid = self._selected_container_id()
        if client is None or not nick or not cid:
            self._stop_logs_follow()
            self._toast("Select a container to follow logs")
            return
        self._stop_logs_follow_keep_toggle()
        self._resize_logs_ring()
        if not self._logs_lines:
            # Seed from a snapshot so Follow starts with recent history.
            self._reload_logs()
        gen = self._logs_stream_gen
        cmd = client.logs_follow_stream_command(
            cid, tail=int(self._tail_spin.get_value()),
            timestamps=self._ts_switch.get_active(),
        )
        stdin = None
        if client._password_mode:  # noqa: SLF001
            stdin = f"{client.sudo_password}\n"

        def on_line(line: str) -> None:
            if gen != self._logs_stream_gen:
                return
            self._logs_pending_lines.append(w.strip_ansi(line))
            if self._logs_flush_source is None:
                self._logs_flush_source = GLib.idle_add(self._flush_pending_log_lines)

        def on_done(_code: int) -> None:
            if gen != self._logs_stream_gen:
                return
            self._logs_stream = None
            if self._logs_follow.get_active():
                self._syncing_logs_follow = True
                try:
                    self._logs_follow.set_active(False)
                finally:
                    self._syncing_logs_follow = False

        self._logs_stream = self._start_command_stream(
            nick, cmd, on_line=on_line, on_done=on_done, input=stdin)

    def _flush_pending_log_lines(self) -> bool:
        self._logs_flush_source = None
        if not self._logs_pending_lines:
            return False
        batch = self._logs_pending_lines
        self._logs_pending_lines = []
        for line in batch:
            self._logs_lines.append(line)
        self._logs_raw = "\n".join(self._logs_lines)
        self._apply_log_filter()
        return False

    def _refresh_logs_targets(self) -> None:
        names = [w.field(c, "Names", "Name", "ID", "Id") for c in self._containers]
        current = None
        # Prefer shared selection over the previous combo pick.
        if getattr(self, "_selected_cid", None):
            for c in self._containers:
                if self._container_cid(c) == self._selected_cid:
                    current = w.field(c, "Names", "Name", "ID", "Id")
                    break
        if current is None:
            old_model = self._logs_combo.get_model()
            idx = self._logs_combo.get_selected()
            if old_model is not None and 0 <= idx < old_model.get_n_items():
                current = old_model.get_string(idx)
        self._syncing_logs_combo = True
        try:
            self._logs_combo.set_model(Gtk.StringList.new(names or ["(no containers)"]))
            if current in names:
                self._logs_combo.set_selected(names.index(current))
        finally:
            self._syncing_logs_combo = False

    def _sync_logs_combo_to_selection(self) -> None:
        """Mirror ``_selected_cid`` into the Logs dropdown (no reload)."""
        if not hasattr(self, "_logs_combo"):
            return
        cid = getattr(self, "_selected_cid", None)
        if not cid or not self._containers:
            return
        names = [w.field(c, "Names", "Name", "ID", "Id") for c in self._containers]
        name = None
        for c in self._containers:
            if self._container_cid(c) == cid:
                name = w.field(c, "Names", "Name", "ID", "Id")
                break
        if name is None or name not in names:
            return
        self._syncing_logs_combo = True
        try:
            self._logs_combo.set_selected(names.index(name))
        finally:
            self._syncing_logs_combo = False

    def _on_logs_target_changed(self, *_a) -> None:
        if self._syncing_logs_combo:
            return
        # Read from the combo model directly — ``_selected_container_id`` prefers
        # the shared selection and would ignore a user pick until we sync it.
        idx = self._logs_combo.get_selected()
        if not (0 <= idx < len(self._containers)):
            return
        cid = w.field(self._containers[idx], "ID", "Id", "ContainerID")
        name = w.field(self._containers[idx], "Names", "Name", default="container")
        if not cid:
            return
        if cid != getattr(self, "_selected_cid", None):
            self._set_selected_container(cid, name, source="logs")
        self._logs_lines.clear()
        self._logs_raw = ""
        if self._logs_follow.get_active():
            self._start_logs_follow()
        else:
            self._reload_logs()

    def _selected_container_id(self) -> Optional[str]:
        if getattr(self, "_selected_cid", None):
            return self._selected_cid
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return w.field(self._containers[idx], "ID", "Id", "ContainerID")
        return None

    def _selected_container_name(self) -> str:
        if getattr(self, "_selected_name", None):
            return self._selected_name
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return w.field(self._containers[idx], "Names", "Name", default="container")
        return "container"

    def _reload_logs(self) -> None:
        client = self._client()
        cid = self._selected_container_id()
        if client is None or not cid:
            return
        if self._logs_follow.get_active():
            # Follow owns the buffer; a manual Load restarts the stream.
            self._start_logs_follow()
            return
        tail = int(self._tail_spin.get_value())
        ts = self._ts_switch.get_active()
        if not self._logs_raw and not self._logs_lines:
            self._logs_pulse.set_visible(True)
            self._pulse_start(self._logs_pulse)
        self._run_async(
            lambda: client.logs_snapshot(cid, tail=tail, timestamps=ts),
            self._on_logs,
        )

    def _on_logs(self, text: Optional[str], err: Optional[Exception]) -> None:
        self._pulse_stop(self._logs_pulse)
        self._logs_pulse.set_visible(False)
        if err is not None:
            self._logs_lines.clear()
            self._logs_raw = ""
            self._logs_buffer.set_text(f"Error: {err}", -1)
            return
        cleaned = w.strip_ansi(text or "")
        self._resize_logs_ring()
        self._logs_lines.clear()
        for line in cleaned.splitlines():
            self._logs_lines.append(line)
        self._logs_raw = "\n".join(self._logs_lines)
        self._apply_log_filter()

    _ERROR_MARKERS = ("error", "err ", "warn", "fail", "fatal", "exception",
                      "panic", "critical")

    def _on_logs_search_changed(self) -> None:
        self._logs_match_index = -1
        self._apply_log_filter()

    def _visible_log_lines(self) -> List[str]:
        lines = list(self._logs_lines)
        if self._logs_errors_only.get_active():
            lines = [ln for ln in lines
                     if any(m in ln.lower() for m in self._ERROR_MARKERS)]
        needle = self._logs_search.get_text()
        if needle and not self._logs_highlight_mode.get_active():
            if self._logs_case.get_active():
                lines = [ln for ln in lines if needle in ln]
            else:
                low = needle.lower()
                lines = [ln for ln in lines if low in ln.lower()]
        return lines

    def _apply_log_filter(self) -> None:
        lines = self._visible_log_lines()
        if not lines and not self._logs_lines:
            self._logs_buffer.set_text("(no output)", -1)
            self._logs_match_label.set_text("")
            return
        shown = "\n".join(lines) if lines else "(no matching lines)"
        self._logs_buffer.set_text(shown, -1)
        self._recompute_log_matches(shown)
        if self._logs_autoscroll.get_active() and self._logs_match_index < 0:
            GLib.idle_add(self._scroll_logs_to_end)

    def _recompute_log_matches(self, shown: str) -> None:
        needle = self._logs_search.get_text()
        self._logs_match_offsets = []
        start, end = self._logs_buffer.get_bounds()
        self._logs_buffer.remove_tag(self._logs_tag_match, start, end)
        self._logs_buffer.remove_tag(self._logs_tag_current, start, end)
        if not needle or shown.startswith("("):
            self._logs_match_label.set_text("")
            self._logs_match_index = -1
            return
        # Manual scan so we get offsets for navigation.
        hay = shown if self._logs_case.get_active() else shown.lower()
        needle_cmp = needle if self._logs_case.get_active() else needle.lower()
        pos = 0
        while True:
            idx = hay.find(needle_cmp, pos)
            if idx < 0:
                break
            self._logs_match_offsets.append(idx)
            m_start = self._logs_buffer.get_iter_at_offset(idx)
            m_end = self._logs_buffer.get_iter_at_offset(idx + len(needle))
            self._logs_buffer.apply_tag(self._logs_tag_match, m_start, m_end)
            pos = idx + max(1, len(needle_cmp))
        n = len(self._logs_match_offsets)
        if n == 0:
            self._logs_match_label.set_text("No results")
            self._logs_match_index = -1
            return
        if self._logs_match_index < 0 or self._logs_match_index >= n:
            self._logs_match_index = n - 1  # jump to last (newest) match
        self._highlight_current_match()

    def _highlight_current_match(self) -> None:
        n = len(self._logs_match_offsets)
        if n == 0 or self._logs_match_index < 0:
            self._logs_match_label.set_text("")
            return
        needle = self._logs_search.get_text()
        start, end = self._logs_buffer.get_bounds()
        self._logs_buffer.remove_tag(self._logs_tag_current, start, end)
        off = self._logs_match_offsets[self._logs_match_index]
        m_start = self._logs_buffer.get_iter_at_offset(off)
        m_end = self._logs_buffer.get_iter_at_offset(off + len(needle))
        self._logs_buffer.apply_tag(self._logs_tag_current, m_start, m_end)
        self._logs_view.scroll_to_iter(m_start, 0.2, True, 0.0, 0.3)
        self._logs_match_label.set_text(f"{self._logs_match_index + 1}/{n}")

    def _logs_match_next(self) -> None:
        n = len(self._logs_match_offsets)
        if n == 0:
            return
        self._logs_match_index = (self._logs_match_index + 1) % n
        self._highlight_current_match()

    def _logs_match_prev(self) -> None:
        n = len(self._logs_match_offsets)
        if n == 0:
            return
        self._logs_match_index = (self._logs_match_index - 1) % n
        self._highlight_current_match()

    def _scroll_logs_to_end(self) -> bool:
        end = self._logs_buffer.get_end_iter()
        self._logs_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        return False

    def _logs_text(self) -> str:
        start, end = self._logs_buffer.get_bounds()
        return self._logs_buffer.get_text(start, end, False)

    def _clear_logs(self) -> None:
        self._logs_lines.clear()
        self._logs_raw = ""
        self._logs_buffer.set_text("", 0)
        self._logs_match_label.set_text("")

    def _copy_logs(self) -> None:
        text = self._logs_text()
        if not text:
            self._toast("Nothing to copy")
            return
        display = self.get_display() or Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)
            self._toast("Logs copied")

    def _save_logs(self) -> None:
        text = self._logs_text()
        if not text:
            self._toast("Nothing to save")
            return
        dialog = Gtk.FileDialog.new()
        dialog.set_initial_name(f"{self._selected_container_name()}-logs.txt")

        def done(dlg: Gtk.FileDialog, result: Any) -> None:
            try:
                gfile = dlg.save_finish(result)
            except GLib.Error:
                return
            try:
                gfile.replace_contents(
                    text.encode("utf-8"), None, False,
                    Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                self._toast("Logs saved")
            except Exception as exc:  # noqa: BLE001
                self._toast(f"Save failed: {exc}")

        dialog.save(self._window(), None, done)

    def _follow_logs_selected(self) -> None:
        cid = self._selected_container_id()
        if cid:
            self._follow_logs(cid, self._selected_container_name())

    def _begin_follow_for_selection(self) -> None:
        """Selection-bar shortcut: switch to Logs and start in-pane Follow."""
        self._stack.set_visible_child_name("logs")
        self._sync_logs_combo_to_selection()
        if not self._logs_follow.get_active():
            self._logs_follow.set_active(True)
        else:
            self._start_logs_follow()
