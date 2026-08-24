"""Regression tests for the tab-close confirmation dialogs.

The three tab-close confirmation dialogs (single tab, split view, bulk
close) previously used native ``Adw.MessageDialog`` windows. On macOS,
closing a tab while the main window is fullscreen made the confirmation
join native fullscreen as a separate window (same bug as the quit progress
dialog). All three must be in-window ``Adw.AlertDialog`` presented with the
main window as parent.

These are pure unit tests: ``Adw``/``Gtk`` are replaced with fakes, so no
real desktop is required.
"""

import types

import pytest

from sshpilot import window_tabs
from sshpilot.window_tabs import WindowTabsMixin


class FakeAlertDialog:
    """Records how window_tabs drives a confirmation dialog."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.heading = kwargs.get("heading")
        self.body = kwargs.get("body")
        self.extra_child = None
        self.presented_parent = None
        self.close_calls = 0
        self._handlers = []
        FakeAlertDialog.instances.append(self)

    def add_response(self, response_id, label):
        pass

    def set_response_appearance(self, response_id, appearance):
        pass

    def set_default_response(self, response_id):
        pass

    def set_close_response(self, response_id):
        pass

    def set_extra_child(self, child):
        self.extra_child = child

    def present(self, parent=None):
        self.presented_parent = parent

    def close(self):
        self.close_calls += 1

    def connect(self, signal, handler, *args):
        self._handlers.append((signal, handler, args))

    def emit_response(self, response_id):
        for signal, handler, args in self._handlers:
            if signal == "response":
                handler(self, response_id, *args)


class FakeMessageDialog:
    """Sibling of FakeAlertDialog, NOT a subclass: the invariant checks that
    the confirmations are not MessageDialogs at all."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.heading = kwargs.get("heading")
        self.body = kwargs.get("body")
        self.extra_child = None
        self.presented_parent = None
        self.close_calls = 0
        self._handlers = []
        FakeMessageDialog.instances.append(self)

    def add_response(self, response_id, label):
        pass

    def set_response_appearance(self, response_id, appearance):
        pass

    def set_default_response(self, response_id):
        pass

    def set_close_response(self, response_id):
        pass

    def set_extra_child(self, child):
        self.extra_child = child

    def present(self, parent=None):
        self.presented_parent = parent

    def close(self):
        self.close_calls += 1

    def connect(self, signal, handler, *args):
        self._handlers.append((signal, handler, args))

    def emit_response(self, response_id):
        for signal, handler, args in self._handlers:
            if signal == "response":
                handler(self, response_id, *args)


class FakeCheckButton:
    def __init__(self, *args, **kwargs):
        pass

    def set_halign(self, value):
        pass

    def set_margin_top(self, value):
        pass

    def get_active(self):
        return False


class FakeTabView:
    def __init__(self, pages):
        self.pages = pages
        self.close_finish_calls = []

    def get_pages(self):
        return list(self.pages)

    def close_page_finish(self, page, confirmed):
        self.close_finish_calls.append((page, confirmed))


class FakePage:
    def __init__(self, child):
        self._child = child

    def get_child(self):
        return self._child


class FakeTerminal:
    def __init__(self):
        self.disconnect_calls = 0

    def disconnect(self):
        self.disconnect_calls += 1


class FakeConnection:
    def __init__(self, display_name=None, nickname=None, hostname=None, host=None):
        self.display_name = display_name
        self.nickname = nickname
        self.hostname = hostname
        self.host = host


class FakeConfig:
    def __init__(self):
        self.settings = {}

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


@pytest.fixture
def fake_gtk(monkeypatch):
    """Swap every Gtk/Adw type window_tabs touches for a recording fake."""
    FakeAlertDialog.instances = []
    FakeMessageDialog.instances = []
    monkeypatch.setattr(window_tabs.Adw, "AlertDialog", FakeAlertDialog)
    monkeypatch.setattr(window_tabs.Adw, "MessageDialog", FakeMessageDialog)
    monkeypatch.setattr(window_tabs.Gtk, "CheckButton", FakeCheckButton)
    monkeypatch.setattr(window_tabs.Gtk, "Align", types.SimpleNamespace(START=object()))
    return types.SimpleNamespace(
        AlertDialog=FakeAlertDialog,
        MessageDialog=FakeMessageDialog,
    )


def make_window(**kwargs):
    window = WindowTabsMixin()
    window.config = FakeConfig()
    window.terminal_to_connection = {}
    window._is_start_tab_page = lambda page: False
    window._update_tab_button_visibility = lambda: None
    window.has_user_tabs = lambda: True
    window.show_start_tab = lambda: None
    for key, value in kwargs.items():
        setattr(window, key, value)
    return window


def last_dialog():
    assert FakeAlertDialog.instances, "no AlertDialog was created"
    return FakeAlertDialog.instances[-1]


def assert_not_message_dialog_created():
    assert not FakeMessageDialog.instances, (
        "a native Adw.MessageDialog window was created"
    )


def test_bulk_close_confirmation_uses_in_window_alert_dialog(fake_gtk):
    terminal = FakeTerminal()
    target_page = FakePage(child=None)
    other_page = FakePage(child=terminal)
    tab_view = FakeTabView([target_page, other_page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: FakeConnection(nickname="web01")},
    )
    close_fn_calls = []

    window._confirm_then_bulk_close(target_page, lambda: close_fn_calls.append(1), False)

    dialog = last_dialog()
    assert isinstance(dialog, fake_gtk.AlertDialog)
    assert_not_message_dialog_created()
    assert dialog.heading == "Close tabs?"
    assert "disconnect 1 session" in dialog.body
    assert dialog.presented_parent is window
    assert "transient_for" not in dialog.kwargs
    assert dialog.extra_child is not None

    dialog.emit_response("close")
    assert close_fn_calls == [1]
    assert dialog.close_calls == 1
    # Confirming the batch must disconnect the sessions it promised to close.
    # Suppressing the per-tab confirmation also skips on_tab_close's disconnect,
    # and waiting for widget destruction let daemon sessions outlive their tabs
    # (GH #1176).
    assert terminal.disconnect_calls == 1
    assert terminal._daemon_batch_close is True

    dialog.emit_response("cancel")
    assert dialog.close_calls == 2


def test_bulk_close_without_confirmation_still_disconnects(fake_gtk):
    """With confirm-disconnect off, on_tab_close does the disconnecting."""
    terminal = FakeTerminal()
    target_page = FakePage(child=None)
    other_page = FakePage(child=terminal)
    tab_view = FakeTabView([target_page, other_page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: FakeConnection(nickname="web01")},
    )
    window.config.settings["confirm-disconnect"] = False
    closed = []

    def close_fn():
        for page in (other_page,):
            closed.append(window.on_tab_close(tab_view, page))

    window._confirm_then_bulk_close(target_page, close_fn, False)

    assert closed == [False]
    assert terminal.disconnect_calls == 1


def test_bulk_close_cancel_leaves_sessions_connected(fake_gtk):
    terminal = FakeTerminal()
    target_page = FakePage(child=None)
    other_page = FakePage(child=terminal)
    tab_view = FakeTabView([target_page, other_page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: FakeConnection(nickname="web01")},
    )
    close_fn_calls = []

    window._confirm_then_bulk_close(target_page, lambda: close_fn_calls.append(1), False)
    last_dialog().emit_response("cancel")

    assert close_fn_calls == []
    assert terminal.disconnect_calls == 0


def test_single_tab_close_confirmation_uses_in_window_alert_dialog(fake_gtk):
    terminal = FakeTerminal()
    page = FakePage(child=terminal)
    tab_view = FakeTabView([page])
    connection = FakeConnection(display_name="web01", nickname="nick")
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: connection},
    )
    window.config.settings["confirm-disconnect"] = True

    result = window.on_tab_close(tab_view, page)

    assert result is True
    dialog = last_dialog()
    assert isinstance(dialog, fake_gtk.AlertDialog)
    assert_not_message_dialog_created()
    assert dialog.heading == "Close connection to web01"
    assert dialog.body == "Are you sure you want to close this connection?"
    assert dialog.presented_parent is window
    assert "transient_for" not in dialog.kwargs
    assert dialog.extra_child is not None

    dialog.emit_response("close")
    assert terminal.disconnect_calls == 1
    assert tab_view.close_finish_calls == [(page, True)]
    assert dialog.close_calls == 1
    assert window._pending_close_tab_view is None
    assert window._pending_close_page is None
    assert window._pending_close_connection is None
    assert window._pending_close_terminal is None


def test_single_tab_close_cancel_rejects_the_close(fake_gtk):
    terminal = FakeTerminal()
    page = FakePage(child=terminal)
    tab_view = FakeTabView([page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: FakeConnection(display_name="web01")},
    )
    window.config.settings["confirm-disconnect"] = True

    window.on_tab_close(tab_view, page)
    dialog = last_dialog()

    dialog.emit_response("cancel")

    assert terminal.disconnect_calls == 0
    assert tab_view.close_finish_calls == [(page, False)]
    assert dialog.close_calls == 1
    assert window._pending_close_tab_view is None


def test_split_view_close_confirmation_uses_in_window_alert_dialog(fake_gtk, monkeypatch):
    class FakeSplitViewTab:
        def __init__(self, panes):
            self._panes = panes
            self.cleanup_calls = 0

        def cleanup_all(self):
            self.cleanup_calls += 1

    class FakePane:
        def __init__(self):
            self.terminal_count = 2

        def get_terminal_count(self):
            return self.terminal_count

    monkeypatch.setattr("sshpilot.split_view.SplitViewTab", FakeSplitViewTab)

    child = FakeSplitViewTab([FakePane()])
    page = FakePage(child=child)
    tab_view = FakeTabView([page])
    window = make_window(tab_view=tab_view)
    window.config.settings["confirm-disconnect"] = True

    result = window.on_tab_close(tab_view, page)

    assert result is True
    dialog = last_dialog()
    assert isinstance(dialog, fake_gtk.AlertDialog)
    assert_not_message_dialog_created()
    assert dialog.heading == "Close split view?"
    assert "disconnect 2 terminal session(s)" in dialog.body
    assert dialog.presented_parent is window
    assert "transient_for" not in dialog.kwargs
    assert dialog.extra_child is not None

    dialog.emit_response("close")
    assert child.cleanup_calls == 1
    assert tab_view.close_finish_calls == [(page, True)]
    assert dialog.close_calls == 1
    assert window._pending_close_split_tab_view is None
    assert window._pending_close_split_page is None
    assert window._pending_close_split_child is None


def test_tab_close_confirmation_closes_via_close_not_destroy(fake_gtk):
    """Response handlers must close() the AlertDialog — destroy() does not
    exist on Adw.AlertDialog and would crash at runtime."""
    terminal = FakeTerminal()
    page = FakePage(child=terminal)
    tab_view = FakeTabView([page])
    window = make_window(
        tab_view=tab_view,
        terminal_to_connection={terminal: FakeConnection(display_name="web01")},
    )
    window.config.settings["confirm-disconnect"] = True

    window.on_tab_close(tab_view, page)
    dialog = last_dialog()

    assert not hasattr(dialog, "destroy")
    dialog.emit_response("close")
    assert dialog.close_calls == 1