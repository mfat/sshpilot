"""Regression tests for the "Closing Connections" cleanup progress dialog.

On macOS, quitting while the main window is fullscreen made the native
``Adw.MessageDialog`` progress window join native fullscreen as a separate
window. The dialog must instead be an in-window ``Adw.AlertDialog`` presented
with the main window as parent, so it stays inside the existing fullscreen
window.

These are pure unit tests: ``Adw``/``Gtk`` are replaced with fakes, so no real
desktop is required.
"""

import types

import pytest

from gi.repository import Adw, Gtk

from sshpilot import shutdown


class FakeAlertDialog:
    """Records how shutdown.py drives the cleanup progress dialog."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.heading = kwargs.get("heading")
        self.extra_child = None
        self.presented_parent = None
        self.close_calls = 0

    def set_heading(self, value):
        self.heading = value

    def set_extra_child(self, child):
        self.extra_child = child

    def present(self, parent=None):
        self.presented_parent = parent

    def close(self):
        self.close_calls += 1


class FakeMessageDialog:
    """Sibling of FakeAlertDialog, NOT a subclass: the invariant checks that
    the progress dialog is not a MessageDialog at all."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.heading = kwargs.get("heading")
        self.extra_child = None
        self.presented_parent = None
        self.close_calls = 0

    def set_heading(self, value):
        self.heading = value

    def set_extra_child(self, child):
        self.extra_child = child

    def present(self, parent=None):
        self.presented_parent = parent

    def close(self):
        self.close_calls += 1


class FakeBox:
    def __init__(self, *args, **kwargs):
        self.children = []

    def append(self, child):
        self.children.append(child)

    def set_margin_top(self, value):
        pass

    def set_margin_bottom(self, value):
        pass

    def set_margin_start(self, value):
        pass

    def set_margin_end(self, value):
        pass


class FakeProgressBar:
    def __init__(self):
        self.fraction = 0.0

    def set_fraction(self, value):
        self.fraction = value


class FakeLabel:
    def __init__(self):
        self.text = ""

    def set_text(self, value):
        self.text = value


@pytest.fixture
def fake_gtk(monkeypatch):
    """Swap every Gtk/Adw type shutdown.py touches for a recording fake."""
    monkeypatch.setattr(Adw, "AlertDialog", FakeAlertDialog)
    monkeypatch.setattr(Adw, "MessageDialog", FakeMessageDialog)
    monkeypatch.setattr(Gtk, "Box", FakeBox)
    monkeypatch.setattr(Gtk, "ProgressBar", FakeProgressBar)
    monkeypatch.setattr(Gtk, "Label", FakeLabel)
    return types.SimpleNamespace(
        AlertDialog=FakeAlertDialog,
        MessageDialog=FakeMessageDialog,
        Box=FakeBox,
        ProgressBar=FakeProgressBar,
        Label=FakeLabel,
    )


def make_window():
    return types.SimpleNamespace()


def test_show_cleanup_progress_uses_in_window_alert_dialog(fake_gtk):
    """The progress dialog must be an in-window Adw.AlertDialog presented with
    the main window as parent — never a native MessageDialog window."""
    window = make_window()

    shutdown._show_cleanup_progress(window, 3)

    dialog = window._progress_dialog
    assert isinstance(dialog, fake_gtk.AlertDialog)
    assert not isinstance(dialog, fake_gtk.MessageDialog)
    assert dialog.heading == "Closing Connections"
    assert dialog.presented_parent is window
    assert "transient_for" not in dialog.kwargs


def test_show_cleanup_progress_populates_progress_bar_and_label(fake_gtk):
    """The progress bar and label are attached as the dialog's extra child."""
    window = make_window()

    shutdown._show_cleanup_progress(window, 3)

    dialog = window._progress_dialog
    assert dialog.extra_child is not None
    assert window._progress_bar in dialog.extra_child.children
    assert window._progress_label in dialog.extra_child.children
    assert window._progress_bar.fraction == 0.0


def test_update_cleanup_progress_updates_bar_and_label(fake_gtk):
    """Progress updates keep working: 2 of 4 -> fraction 0.5 and label text."""
    window = types.SimpleNamespace(
        _progress_bar=fake_gtk.ProgressBar(),
        _progress_label=fake_gtk.Label(),
    )

    shutdown._update_cleanup_progress(window, 2, 4)

    assert window._progress_bar.fraction == 0.5
    assert "Closed 2 of 4" in window._progress_label.text


def test_hide_cleanup_progress_closes_once_and_clears_state(fake_gtk):
    """Closing hides the dialog exactly once and clears all window state."""
    window = make_window()
    shutdown._show_cleanup_progress(window, 3)
    dialog = window._progress_dialog

    shutdown._hide_cleanup_progress(window)

    assert dialog.close_calls == 1
    assert window._progress_dialog is None
    assert window._progress_bar is None
    assert window._progress_label is None


def test_hide_cleanup_progress_safe_when_close_fails(fake_gtk):
    """A failing close() (e.g. dialog already gone) must not prevent state
    cleanup — shutdown can fail midway."""
    window = make_window()
    shutdown._show_cleanup_progress(window, 3)

    def boom():
        raise RuntimeError("dialog already closed")

    window._progress_dialog.close = boom

    shutdown._hide_cleanup_progress(window)

    assert window._progress_dialog is None
    assert window._progress_bar is None
    assert window._progress_label is None


def test_hide_cleanup_progress_noop_when_not_shown(fake_gtk):
    """Hiding before anything was shown is a safe no-op."""
    window = make_window()

    shutdown._hide_cleanup_progress(window)

    assert getattr(window, "_progress_dialog", None) is None
    assert getattr(window, "_progress_bar", None) is None
    assert getattr(window, "_progress_label", None) is None