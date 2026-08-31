"""The confirmation dialogs must pre-select the action the user asked for.

GH #1231: pressing Enter on the quit prompt or on a disconnect prompt hit
Cancel instead of the action, because the dialog either defaulted to Cancel
or named a response id that was never added (an unknown default leaves the
dialog with no default widget at all, so keyboard focus lands on the first
button — Cancel).

Getting the default right is only half of it: GTK draws no focus ring until
the window is in focus-visible mode, so a dialog opened with the mouse showed
two identical-looking buttons and no hint that Enter confirms. Each of these
dialogs must therefore also mark its default response visible.

Pure unit tests: ``Adw``/``Gtk`` are replaced with recording fakes, so no
real desktop is required.
"""

import sys
import types

import pytest

from sshpilot import daemon_quit_policy, dialog_focus, terminal_manager, window_tabs
from sshpilot.window_tabs import WindowTabsMixin


class RecordingDialog:
    """Records the response wiring of one confirmation dialog."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.heading = kwargs.get("heading")
        self.body = kwargs.get("body")
        self.responses = []
        self.default_response = None
        self.close_response = None
        self.extra_child = None
        self.focus_visible = False
        RecordingDialog.instances.append(self)

    @classmethod
    def new(cls, heading, body):
        return cls(heading=heading, body=body)

    def add_response(self, response_id, label):
        self.responses.append(response_id)

    def set_response_appearance(self, response_id, appearance):
        pass

    def set_default_response(self, response_id):
        self.default_response = response_id

    def set_close_response(self, response_id):
        self.close_response = response_id

    def set_extra_child(self, child):
        self.extra_child = child

    def present(self, parent=None):
        pass

    def destroy(self):
        pass

    def close(self):
        pass

    def connect(self, signal, handler, *args):
        pass

    def set_focus_visible(self, visible):
        self.focus_visible = visible


class FakeCheckButton:
    def __init__(self, *args, **kwargs):
        pass

    def set_halign(self, value):
        pass

    def set_margin_top(self, value):
        pass

    def get_active(self):
        return False


class FocusRecordingWindow:
    """Stands in for the main window: records the focus-ring flag."""

    def __init__(self, **kwargs):
        self.client = object()
        self.focus_visible = False
        for key, value in kwargs.items():
            setattr(self, key, value)

    def set_focus_visible(self, visible):
        self.focus_visible = visible


class FakeConfig:
    def __init__(self, **settings):
        self.settings = dict(settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture(autouse=True)
def reset_instances():
    RecordingDialog.instances = []
    yield
    RecordingDialog.instances = []


def only_dialog():
    assert len(RecordingDialog.instances) == 1, (
        f"expected exactly one dialog, got {len(RecordingDialog.instances)}"
    )
    return RecordingDialog.instances[0]


def assert_default_is_registered(dialog):
    """An unknown default response id is the same as having no default."""
    assert dialog.default_response in dialog.responses, (
        f"default response {dialog.default_response!r} was never added "
        f"(responses: {dialog.responses})"
    )


# --- Quit -----------------------------------------------------------------


def test_daemon_quit_dialog_defaults_to_quit(monkeypatch):
    adw = types.SimpleNamespace(
        AlertDialog=RecordingDialog,
        ResponseAppearance=types.SimpleNamespace(DESTRUCTIVE=object()),
    )
    # daemon_quit_policy imports Adw inside the function, so the swap has to
    # happen on the gi.repository package itself.
    monkeypatch.setattr(sys.modules["gi.repository"], "Adw", adw)
    monkeypatch.setattr(
        daemon_quit_policy,
        "daemon_active_work_summary",
        lambda client: {"sessions_active": 2},
    )

    window = FocusRecordingWindow()
    daemon_quit_policy.present_daemon_quit_dialog(window, on_decision=lambda d: None)

    dialog = only_dialog()
    assert dialog.responses == ["cancel", "terminate"]
    assert_default_is_registered(dialog)
    # Enter confirms the quit the user asked for; Escape still cancels.
    assert dialog.default_response == "terminate"
    assert dialog.close_response == "cancel"
    # The in-window dialog's focus ring belongs to the window it sits in.
    assert window.focus_visible is True


# --- Disconnect -----------------------------------------------------------


def test_disconnect_confirmation_defaults_to_disconnect(monkeypatch):
    monkeypatch.setattr(terminal_manager.Adw, "MessageDialog", RecordingDialog)

    class Connection:
        nickname = "web01"
        hostname = "web01.example"

    connection = Connection()
    window = types.SimpleNamespace(
        active_terminals={connection: object()},
        config=FakeConfig(**{"confirm-disconnect": True}),
    )
    manager = terminal_manager.TerminalManager.__new__(terminal_manager.TerminalManager)
    manager.window = window

    manager.disconnect_from_host(connection)

    dialog = only_dialog()
    assert dialog.responses == ["cancel", "disconnect"]
    assert_default_is_registered(dialog)
    assert dialog.default_response == "disconnect"
    assert dialog.close_response == "cancel"
    assert dialog.focus_visible is True


# --- Tab close ------------------------------------------------------------


@pytest.fixture
def fake_tab_gtk(monkeypatch):
    monkeypatch.setattr(window_tabs.Adw, "AlertDialog", RecordingDialog)
    monkeypatch.setattr(window_tabs.Gtk, "CheckButton", FakeCheckButton)
    monkeypatch.setattr(window_tabs.Gtk, "Align", types.SimpleNamespace(START=object()))


class FakeTerminal:
    def disconnect(self):
        pass


class FakePage:
    def __init__(self, child):
        self._child = child

    def get_child(self):
        return self._child


class FakeTabView:
    def __init__(self, pages):
        self.pages = pages

    def get_pages(self):
        return list(self.pages)

    def close_page_finish(self, page, confirmed):
        pass


def make_window(**kwargs):
    window = WindowTabsMixin()
    window.focus_visible = False
    window.set_focus_visible = lambda visible: setattr(window, "focus_visible", visible)
    window.config = FakeConfig(**{"confirm-disconnect": True})
    window.terminal_to_connection = {}
    window._is_start_tab_page = lambda page: False
    for key, value in kwargs.items():
        setattr(window, key, value)
    return window


def test_single_tab_close_defaults_to_close(fake_tab_gtk):
    terminal = FakeTerminal()
    page = FakePage(child=terminal)
    tab_view = FakeTabView([page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={
            terminal: types.SimpleNamespace(
                display_name="web01", nickname="web01", hostname="web01"
            )
        },
    )

    assert window.on_tab_close(tab_view, page) is True

    dialog = only_dialog()
    assert dialog.responses == ["cancel", "close"]
    assert_default_is_registered(dialog)
    assert dialog.default_response == "close"
    assert dialog.close_response == "cancel"
    assert window.focus_visible is True
    assert window.focus_visible is True


def test_bulk_tab_close_defaults_to_close(fake_tab_gtk):
    terminal = FakeTerminal()
    target_page = FakePage(child=None)
    other_page = FakePage(child=terminal)
    tab_view = FakeTabView([target_page, other_page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: types.SimpleNamespace(nickname="web01")},
    )

    window._confirm_then_bulk_close(target_page, lambda: None, False)

    dialog = only_dialog()
    assert dialog.responses == ["cancel", "close"]
    assert_default_is_registered(dialog)
    assert dialog.default_response == "close"
    assert dialog.close_response == "cancel"
    assert window.focus_visible is True


# --- dialog_focus helper --------------------------------------------------


class FakeToplevel:
    def __init__(self):
        self.focus_visible = False

    def get_root(self):
        return self

    def set_focus_visible(self, visible):
        self.focus_visible = visible


def test_new_dialog_marking_skips_windows_that_were_already_open(monkeypatch):
    """Gtk.AlertDialog hides its window, so we mark whatever toplevel is new."""
    existing = FakeToplevel()
    toplevels = [existing]
    monkeypatch.setattr(
        dialog_focus,
        "Gtk",
        types.SimpleNamespace(
            Window=types.SimpleNamespace(get_toplevels=lambda: list(toplevels))
        ),
    )
    monkeypatch.setattr(
        dialog_focus, "GLib", types.SimpleNamespace(idle_add=lambda fn: fn())
    )

    before = dialog_focus.capture_toplevels()
    fresh = FakeToplevel()
    toplevels.append(fresh)
    dialog_focus.mark_new_dialog_default_visible(before)

    assert fresh.focus_visible is True
    assert existing.focus_visible is False


def test_marking_a_widget_walks_up_to_its_window():
    window = FakeToplevel()
    widget = types.SimpleNamespace(get_root=lambda: window)

    assert dialog_focus.mark_default_response_visible(widget) is True
    assert window.focus_visible is True


def test_marking_is_best_effort():
    """A dialog we cannot reach still works; it just looks the way it did."""
    assert dialog_focus.mark_default_response_visible(None) is False
    assert dialog_focus.mark_default_response_visible(object()) is False
