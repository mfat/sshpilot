"""Real GTK/VTE coverage for event-driven terminal grid tracking."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import GridTrackingVteTerminal, VTETerminalBackend

pytestmark = pytest.mark.gui


def _pump(milliseconds=150):
    context = GLib.MainContext.default()
    done = {"value": False}
    GLib.timeout_add(
        milliseconds,
        lambda: done.__setitem__("value", True) or GLib.SOURCE_REMOVE,
    )
    while not done["value"]:
        context.iteration(True)


def _grid(terminal):
    return (int(terminal.get_row_count()), int(terminal.get_column_count()))


def test_grid_tracker_reports_allocation_and_zoom_but_not_subcell_resize():
    terminal = GridTrackingVteTerminal()
    events = []
    terminal.connect(
        "grid-size-changed",
        lambda widget, columns, rows: events.append(
            (rows, columns, widget.get_width(), widget.get_height())
        ),
    )
    window = Gtk.Window()
    window.set_default_size(800, 552)
    window.set_child(terminal)
    window.present()
    try:
        _pump()
        assert events
        initial_grid = _grid(terminal)
        initial_allocation = (terminal.get_width(), terminal.get_height())

        events.clear()
        window.set_default_size(window.get_width() + 1, window.get_height() + 1)
        _pump()
        assert (terminal.get_width(), terminal.get_height()) != initial_allocation
        assert _grid(terminal) == initial_grid
        assert events == []

        events.clear()
        window.set_default_size(
            window.get_width() + int(terminal.get_char_width()) * 20,
            window.get_height() + int(terminal.get_char_height()) * 10,
        )
        _pump()
        resized_grid = _grid(terminal)
        assert resized_grid != initial_grid
        assert [(rows, columns) for rows, columns, _width, _height in events] == [
            resized_grid
        ]

        events.clear()
        allocation_before_zoom = (terminal.get_width(), terminal.get_height())
        terminal.set_font_scale(1.5)
        _pump(200)
        assert (terminal.get_width(), terminal.get_height()) == allocation_before_zoom
        assert _grid(terminal) != resized_grid
        assert [(rows, columns) for rows, columns, _width, _height in events] == [
            _grid(terminal)
        ]

        events.clear()
        terminal.disable_grid_tracking()
        window.set_default_size(window.get_width() + 100, window.get_height() + 100)
        _pump()
        assert events == []
    finally:
        window.destroy()


def test_hidden_terminal_converges_when_shown_and_after_window_state_restore():
    terminal = GridTrackingVteTerminal()
    events = []
    terminal.connect(
        "grid-size-changed",
        lambda _widget, columns, rows: events.append((rows, columns)),
    )
    stack = Gtk.Stack()
    visible = Gtk.Label(label="visible page")
    stack.add_named(visible, "visible")
    stack.add_named(terminal, "terminal")
    stack.set_visible_child(visible)
    window = Gtk.Window()
    window.set_default_size(600, 400)
    window.set_child(stack)
    window.present()
    try:
        _pump()
        window.set_default_size(900, 650)
        _pump()
        assert terminal.get_mapped() is False
        assert events == []

        stack.set_visible_child(terminal)
        _pump()
        assert terminal.get_mapped() is True
        assert events == [_grid(terminal)]

        window.maximize()
        _pump()
        assert window.is_maximized()
        assert events[-1] == _grid(terminal)
        window.unmaximize()
        _pump(200)
        restored_grid = _grid(terminal)
        assert events[-1] == restored_grid

        window.fullscreen()
        _pump()
        assert window.is_fullscreen()
        assert events[-1] == _grid(terminal)
        window.unfullscreen()
        _pump(200)
        assert events[-1] == _grid(terminal)
        assert _grid(terminal) == restored_grid
    finally:
        window.destroy()


def test_ownership_acquisition_resyncs_current_grid_without_another_resize():
    backend = VTETerminalBackend(owner=None)
    controller = SimpleNamespace(resizes=[])
    controller.resize = controller.resizes.append
    owner = SimpleNamespace(
        backend=backend,
        _daemon_controller=controller,
        has_input_ownership=False,
    )
    owner._daemon_terminal_dimensions = lambda: (
        TerminalWidget._daemon_terminal_dimensions(owner)
    )
    backend.connect_size_changed(
        lambda *args: TerminalWidget._on_daemon_size_changed(owner, *args)
    )
    window = Gtk.Window()
    window.set_default_size(700, 450)
    window.set_child(backend.vte)
    window.present()
    destroyed = False
    try:
        _pump()
        window.set_default_size(1000, 700)
        _pump()
        assert controller.resizes == []

        owner.has_input_ownership = True
        TerminalWidget._resync_daemon_terminal_size(owner)
        _pump()

        assert controller.resizes
        final = controller.resizes[-1]
        assert (final.rows, final.columns) == _grid(backend.vte)

        delivered_before_destroy = len(controller.resizes)
        backend.destroy()
        destroyed = True
        window.set_default_size(window.get_width() + 100, window.get_height() + 100)
        _pump()
        assert len(controller.resizes) == delivered_before_destroy
    finally:
        if not destroyed:
            backend.destroy()
        window.destroy()
