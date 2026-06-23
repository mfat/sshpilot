"""Docker Manager tab: Logs."""

from __future__ import annotations

from typing import Any, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gio, Gtk, Gdk  # noqa: E402

from . import widgets as w  # noqa: E402


class LogsTabMixin:
    """Logs tab: in-page snapshot with search / errors-only / auto-scroll."""

    def _build_logs_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.append(Gtk.Label(label="Container:"))
        self._logs_combo = Gtk.DropDown.new_from_strings(["(refresh containers)"])
        self._logs_combo.set_hexpand(True)
        toolbar.append(self._logs_combo)

        toolbar.append(Gtk.Label(label="Tail:"))
        self._tail_spin = Gtk.SpinButton.new_with_range(10, 5000, 50)
        self._tail_spin.set_value(100)
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
        follow = Gtk.Button(label="Follow in terminal")
        follow.connect("clicked", lambda _b: self._follow_logs_selected())
        actions.append(follow)
        self._logs_autorefresh = Gtk.ToggleButton(label="Auto")
        self._logs_autorefresh.set_tooltip_text(
            "Auto-refresh this snapshot on the polling interval")
        actions.append(self._logs_autorefresh)
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

        # Filter row: live search, errors-only, auto-scroll.
        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._logs_search = Gtk.SearchEntry()
        self._logs_search.set_placeholder_text("Filter lines…")
        self._logs_search.set_hexpand(True)
        self._logs_search.connect("search-changed", lambda _e: self._apply_log_filter())
        filters.append(self._logs_search)
        self._logs_errors_only = Gtk.ToggleButton(label="Errors only")
        self._logs_errors_only.set_tooltip_text("Show only lines mentioning error/warn/fail/fatal")
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
        scroller.set_child(self._logs_view)

        # A pulsing Docker mark overlaid on the (otherwise blank) log view while
        # a snapshot loads. TextView has no placeholder API, so use an overlay.
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(scroller)
        self._logs_pulse = self._make_docker_mark(56)
        self._logs_pulse.set_visible(False)
        overlay.add_overlay(self._logs_pulse)
        box.append(overlay)
        return box

    def _refresh_logs_targets(self) -> None:
        names = [w.field(c, "Names", "Name", "ID", "Id") for c in self._containers]
        model = Gtk.StringList.new(names or ["(no containers)"])
        self._logs_combo.set_model(model)

    def _selected_container_id(self) -> Optional[str]:
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return w.field(self._containers[idx], "ID", "Id", "ContainerID")
        return None

    def _selected_container_name(self) -> str:
        idx = self._logs_combo.get_selected()
        if 0 <= idx < len(self._containers):
            return w.field(self._containers[idx], "Names", "Name", default="container")
        return "container"

    def _reload_logs(self) -> None:
        client = self._client()
        cid = self._selected_container_id()
        if client is None or not cid:
            return
        tail = int(self._tail_spin.get_value())
        ts = self._ts_switch.get_active()
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
            self._logs_raw = ""
            self._logs_buffer.set_text(f"Error: {err}", -1)
            return
        self._logs_raw = text or ""
        self._apply_log_filter()

    # Substrings that mark a "problem" line for the Errors-only filter.
    _ERROR_MARKERS = ("error", "err ", "warn", "fail", "fatal", "exception",
                      "panic", "critical")

    def _apply_log_filter(self) -> None:
        """Re-render the cached snapshot through the search + errors-only filters."""
        text = self._logs_raw
        if not text:
            self._logs_buffer.set_text("(no output)", -1)
            return
        needle = self._logs_search.get_text().strip().lower()
        errors_only = self._logs_errors_only.get_active()
        lines = text.splitlines()
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        if errors_only:
            lines = [ln for ln in lines
                     if any(m in ln.lower() for m in self._ERROR_MARKERS)]
        shown = "\n".join(lines)
        if not shown:
            shown = "(no matching lines)"
        self._logs_buffer.set_text(shown, -1)
        if self._logs_autoscroll.get_active():
            GLib.idle_add(self._scroll_logs_to_end)

    def _scroll_logs_to_end(self) -> bool:
        end = self._logs_buffer.get_end_iter()
        self._logs_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        return False

    def _logs_text(self) -> str:
        start, end = self._logs_buffer.get_bounds()
        return self._logs_buffer.get_text(start, end, False)

    def _clear_logs(self) -> None:
        self._logs_raw = ""
        self._logs_buffer.set_text("", 0)

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
                return  # cancelled
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

