"""Real GTK: navigating a pane must not leave the old directory's rows painted.

The unit-level guard is tests/test_file_pane_stale_rows.py; this one asserts on
what GTK actually renders, because the bug was in *when* GTK binds rows relative
to the pane publishing ``self._entries``.

    SSHPILOT_GUI_TESTS=1 pytest -m gui tests/gui/test_file_pane_navigation_rows.py
"""

import pytest

from tests._gui_harness import requires_gui

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui


def _pump(iterations: int = 120) -> None:
    context = GLib.MainContext.default()
    for _ in range(iterations):
        while context.pending():
            context.iteration(False)


def _visible_labels(widget, out):
    if isinstance(widget, Gtk.Label):
        text = widget.get_text()
        if text:
            out.append(text)
    child = widget.get_first_child()
    while child is not None:
        _visible_labels(child, out)
        child = child.get_next_sibling()
    return out


@pytest.fixture
def pane():
    from sshpilot.file_manager.pane import FilePane

    pane = FilePane("Local")
    window = Gtk.Window()
    window.set_default_size(700, 600)
    window.set_child(pane)
    window.present()
    _pump()
    try:
        yield pane
    finally:
        window.destroy()
        _pump(20)


def _entries(prefix, count):
    from sshpilot.file_manager.common import FileEntry

    return [
        FileEntry(name=f"{prefix}{index}", is_dir=False, size=index, modified=1700000000.0)
        for index in range(count)
    ]


def test_navigating_replaces_every_visible_row(pane):
    pane.show_entries("/root", _entries("alpha_", 30))
    _pump()
    assert any("alpha_0" == text for text in _visible_labels(pane._list_view, []))

    pane.show_entries("/", _entries("beta_", 30))
    _pump()

    labels = _visible_labels(pane._list_view, [])
    assert not [text for text in labels if text.startswith("alpha_")]
    assert [text for text in labels if text.startswith("beta_")]


def test_a_shorter_listing_drops_the_extra_rows(pane):
    pane.show_entries("/root", _entries("alpha_", 30))
    _pump()

    pane.show_entries("/tmp", _entries("beta_", 2))
    _pump()

    labels = _visible_labels(pane._list_view, [])
    assert not [text for text in labels if text.startswith("alpha_")]
    assert sorted(text for text in labels if text.startswith("beta_")) == [
        "beta_0",
        "beta_1",
    ]
