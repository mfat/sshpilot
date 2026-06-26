"""Docker Console tab: Stats."""

from __future__ import annotations

from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw  # noqa: E402

from . import widgets as w  # noqa: E402


class StatsTabMixin:
    # Columns rendered in the stats grid: (header, stat-keys to read).
    _STATS_COLUMNS = (
        ("Name", ("Name", "Container")),
        ("CPU %", ("CPUPerc", "CPU")),
        ("Memory", ("MemUsage", "MemUsageLimit")),
        ("Mem %", ("MemPerc", "Mem")),
        ("Net I/O", ("NetIO",)),
        ("Block I/O", ("BlockIO",)),
    )

    def _build_stats_section(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        live = Gtk.Button()
        live.set_child(Adw.ButtonContent(icon_name="utilities-system-monitor-symbolic",
                                         label="Live stats"))
        live.set_tooltip_text("Open a streaming `docker stats` in a terminal tab")
        live.connect("clicked", lambda _b: self._open_live_stats())
        toolbar.append(live)
        box.append(toolbar)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        # A Grid keeps every column the same width across the header and all
        # rows, so the numbers line up (an HBox-per-row does not).
        self._stats_grid = Gtk.Grid(column_spacing=18, row_spacing=6)
        self._stats_grid.set_margin_top(6)
        self._stats_grid.set_margin_bottom(6)
        self._stats_grid.set_margin_start(8)
        self._stats_grid.set_margin_end(8)
        self._stats_grid.set_hexpand(True)
        scroller.set_child(self._stats_grid)

        # Pulsing Docker-mark overlay while loading (Grid has no placeholder API).
        overlay = Gtk.Overlay()
        overlay.set_vexpand(True)
        overlay.set_child(scroller)
        self._stats_pulse = self._make_docker_mark(56)
        self._stats_pulse.set_visible(False)
        overlay.add_overlay(self._stats_pulse)
        box.append(overlay)
        return box

    def _refresh_stats(self) -> None:
        client = self._client()
        if client is None or self._stats_busy:
            return
        self._stats_busy = True
        # Only show the loading logo on the first load; auto-refresh is silent.
        if not self._stats_has_data:
            self._stats_pulse.set_visible(True)
            self._pulse_start(self._stats_pulse)
        gen = self._load_gen
        self._run_async(client.stats, lambda r, e, g=gen: self._on_stats(r, e, g))

    def _open_live_stats(self) -> None:
        client = self._client()
        nick = self._current_nickname()
        if client is None or not nick:
            return
        self._warn_sudo_interactive(nick)
        ok = self._open_command_terminal(
            nick, client.stats_stream_command(), title=f"stats: {nick}")
        if not ok:
            self._toast("Could not open live stats")

    def _on_stats(self, rows: Optional[List[dict]], err: Optional[Exception],
                  gen: int = 0) -> None:
        if gen != self._load_gen:
            return  # stale result for a previous host — drop it
        self._stats_busy = False
        self._pulse_stop(self._stats_pulse)
        self._stats_pulse.set_visible(False)
        w.clear_grid(self._stats_grid)
        span = len(self._STATS_COLUMNS)
        if err is not None:
            self._stats_has_data = False
            self._stats_grid.attach(w.grid_message(w.error_text(err), error=True), 0, 0, span, 1)
            return
        if not rows:
            self._stats_has_data = False
            self._stats_grid.attach(w.grid_message("No running containers"), 0, 0, span, 1)
            return
        self._stats_has_data = True
        # Header row.
        for col, (title, _keys) in enumerate(self._STATS_COLUMNS):
            head = Gtk.Label(label=title, xalign=0, hexpand=True)
            head.add_css_class("heading")
            self._stats_grid.attach(head, col, 0, 1, 1)
        # Data rows — same column index → same column width as the header.
        for r, s in enumerate(rows, start=1):
            for col, (_title, keys) in enumerate(self._STATS_COLUMNS):
                cell = Gtk.Label(label=w.field(s, *keys) or "-", xalign=0, hexpand=True)
                self._stats_grid.attach(cell, col, r, 1, 1)

