"""#1356: each file-manager pane browses its own server.

The window decides which backend a pane uses, which pane a backend's signals
reach, and which connection a host switch opens. These pin those decisions
without a display.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock


def _module():
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot import file_manager_window as window_module

    return window_module


def _connection(nickname):
    return SimpleNamespace(nickname=nickname, hostname=f"{nickname}.example",
                           host=f"{nickname}.example", username="root", port=22)


def _window(window_module):
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    window._left_pane = MagicMock(name="left")
    window._right_pane = MagicMock(name="right")
    window._manager = MagicMock(name="right-manager")
    # Some test runs import the window against a stubbed GTK base whose
    # missing attributes read as callables; pin the ones handlers consult.
    window._is_disposed = False
    return window


def test_switching_the_right_host_opens_the_new_connection(monkeypatch):
    """A tab opened for one server kept its connection id, so picking another
    host in the right pane reconnected to the first one under the new name."""
    window_module = _module()
    monkeypatch.setattr(
        window_module, "file_manager_connection_id",
        lambda connection: f"id:{connection.nickname}",
    )
    window = _window(window_module)
    first, second = _connection("googlerouter"), _connection("oracle")
    window._connection = first
    window._connection_id = "id:googlerouter"
    window._ssh_config = {"hostname": "googlerouter.example"}
    window._nickname = "googlerouter"
    window._right_overlay = MagicMock()
    window._header_bar = MagicMock()
    window._update_remote_host_button = MagicMock()
    window._host_picked_callback = None
    window._start_connection = MagicMock()

    window._on_placeholder_host_picked(second)

    assert window._connection is second
    assert window._connection_id == "id:oracle"
    assert window._ssh_config is None
    window._start_connection.assert_called_once_with()


def test_left_pane_is_local_until_a_server_is_picked():
    window_module = _module()
    window = _window(window_module)
    assert window._is_local_pane(window._left_pane)
    assert window._local_pane() is window._left_pane
    assert window._manager_for(window._left_pane) is None
    assert window._manager_for(window._right_pane) is window._manager

    left_manager = MagicMock(name="left-manager")
    window._left_connection = _connection("oracle")
    window._left_manager = left_manager

    assert not window._is_local_pane(window._left_pane)
    assert window._local_pane() is None
    assert window._manager_for(window._left_pane) is left_manager
    assert window._manager_for(window._right_pane) is window._manager


def test_backend_signals_reach_only_the_pane_that_backend_serves():
    window_module = _module()
    window = _window(window_module)
    left_manager = MagicMock(name="left-manager")
    window._left_connection = _connection("oracle")
    window._left_manager = left_manager

    assert window._panes_of_sender(left_manager) == [window._left_pane]
    assert window._panes_of_sender(window._manager) == [window._right_pane]
    assert window._panes_of_sender(None) == [window._right_pane]
    assert window._panes_of_sender(MagicMock(name="stale")) == []


def test_a_listing_from_the_left_server_never_lands_in_the_right_pane():
    """Both panes may wait on the same path (/root on two servers)."""
    window_module = _module()
    window = _window(window_module)
    left_manager = MagicMock(name="left-manager")
    window._left_connection = _connection("oracle")
    window._left_manager = left_manager
    window._pending_paths = {window._left_pane: "/root", window._right_pane: "/root"}
    window._pending_highlights = {}
    window._loading_toast_timeouts = {}
    window._refreshing_panes = set()
    window._queued_refreshes = set()

    window._on_directory_loaded(left_manager, "/root", [])

    window._left_pane.show_entries.assert_called_once_with("/root", [])
    window._right_pane.show_entries.assert_not_called()
    assert window._pending_paths[window._right_pane] == "/root"


def _done(value):
    from concurrent.futures import Future

    future = Future()
    future.set_result(value)
    return future


def _two_server_window(monkeypatch, existing=()):
    """A window with a server in each pane; *existing* names the paths the
    right (destination) server already has."""
    window_module = _module()
    window = _window(window_module)
    left_manager = MagicMock(name="left-manager")
    window._left_connection = _connection("oracle")
    window._left_manager = left_manager
    window._connection = _connection("googlerouter")
    window._show_progress_dialog = MagicMock()
    window._attach_refresh = MagicMock()
    window._embedded_parent = None
    window.get_transient_for = lambda: None
    window._manager.path_exists.side_effect = lambda path: _done(path in existing)
    # The conflict check finishes on the main loop; run it at once.
    monkeypatch.setattr(
        window_module.GLib, "idle_add", lambda fn, *args: fn(*args), raising=False
    )
    return window_module, window, left_manager


class _Dialog:
    last = None

    def __init__(self, heading, body):
        self.heading, self.body, self.responses = heading, body, {}
        _Dialog.last = self

    @classmethod
    def new(cls, heading, body):
        return cls(heading, body)

    def add_response(self, response_id, label):
        self.responses[response_id] = label

    def set_default_response(self, *_args):
        pass

    def set_close_response(self, *_args):
        pass

    def connect(self, _signal, handler):
        self.handler = handler

    def present(self, *_args):
        pass

    def close(self):
        pass


def test_paste_between_two_server_panes_copies_through_the_daemon(monkeypatch):
    _module_, window, left_manager = _two_server_window(monkeypatch)
    entry = SimpleNamespace(name="docs", is_dir=True)

    window._perform_pane_to_pane_remote_operation(
        [entry], "/root", "/srv", False,
        source_pane=window._left_pane, destination_pane=window._right_pane,
    )

    left_manager.copy_remote.assert_called_once_with(
        "/root/docs", "/srv/docs", recursive=True, move=False,
        destination_manager=window._manager, overwrite=False,
    )
    window._manager.copy_remote.assert_not_called()


def _copy_over_an_existing_name(monkeypatch, response):
    window_module, window, left_manager = _two_server_window(
        monkeypatch, existing={"/srv/report.txt"}
    )
    monkeypatch.setattr(window_module.Adw, "AlertDialog", _Dialog, raising=False)
    entries = [SimpleNamespace(name="report.txt", is_dir=False),
               SimpleNamespace(name="new.txt", is_dir=False)]

    window._perform_pane_to_pane_remote_operation(
        entries, "/root", "/srv", False,
        source_pane=window._left_pane, destination_pane=window._right_pane,
    )

    dialog = _Dialog.last
    assert left_manager.copy_remote.call_count == 0  # asks first
    assert set(dialog.responses) == {"cancel", "skip", "replace"}
    assert "report.txt" in dialog.body
    dialog.handler(dialog, response)
    return {
        call.args[1]: call.kwargs["overwrite"]
        for call in left_manager.copy_remote.call_args_list
    }


def test_a_copy_over_an_existing_name_asks_and_replace_replaces(monkeypatch):
    """#1431: a copy between two servers failed on a name the other pane
    had; it now asks, as a download does."""
    copies = _copy_over_an_existing_name(monkeypatch, "replace")
    assert copies == {"/srv/report.txt": True, "/srv/new.txt": True}


def test_skip_existing_copies_only_the_new_names(monkeypatch):
    copies = _copy_over_an_existing_name(monkeypatch, "skip")
    assert copies == {"/srv/new.txt": False}


def test_cancel_copies_nothing(monkeypatch):
    assert _copy_over_an_existing_name(monkeypatch, "cancel") == {}


def test_f3_and_the_menu_move_the_layout_switch_with_them():
    """F3 and the tab menu go through set_single_pane, which hides the left
    pane and keeps the OSD bar's segmented switch in step."""
    window_module = _module()
    window = _window(window_module)
    group = MagicMock()
    window._layout_toggle = group
    window._syncing_layout = False
    window._single_pane = False

    assert window.is_single_pane() is False
    assert window._toggle_single_pane() is True

    assert window.is_single_pane() is True
    window._left_pane.set_visible.assert_called_with(False)
    group.set_active_name.assert_called_once_with("one")
    # Syncing the switch must not loop back into another toggle.
    assert window._syncing_layout is False


def test_picking_a_segment_switches_the_layout():
    window_module = _module()
    window = _window(window_module)
    window._layout_toggle = MagicMock()
    window._syncing_layout = False
    window._single_pane = False

    window._on_layout_group_changed(
        SimpleNamespace(get_active_name=lambda: "one"), None
    )
    assert window.is_single_pane() is True
    window._on_layout_group_changed(
        SimpleNamespace(get_active_name=lambda: "two"), None
    )
    assert window.is_single_pane() is False

    # While the window itself moves the switch, its signal is ignored.
    window._syncing_layout = True
    window._on_layout_group_changed(
        SimpleNamespace(get_active_name=lambda: "one"), None
    )
    assert window.is_single_pane() is False

def test_toggling_the_pane_leaves_the_setting_alone():
    """Settings decides how a file manager starts; the switch, F3 and the
    tab menu change only the one in front of you (as in Dolphin)."""
    window_module = _module()
    window = _window(window_module)
    settings = MagicMock()
    window.config = settings
    window._layout_toggle = None
    window._syncing_layout = False

    window.set_single_pane(True)

    window._left_pane.set_visible.assert_called_with(False)
    settings.set_setting.assert_not_called()

def test_a_new_file_manager_starts_as_settings_say():
    window_module = _module()
    window = _window(window_module)
    window.config = MagicMock()
    window.config.get_setting.side_effect = (
        lambda key, default=None: key == "file_manager.single_pane"
    )
    assert window._load_single_pane_preference() is True
    window.config.get_setting.side_effect = lambda key, default=None: default
    assert window._load_single_pane_preference() is False



class _Transfer:
    def __init__(self):
        from concurrent.futures import Future

        self.future = Future()
        self._futures = [self.future]
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_a_connection_closes_only_after_its_transfers_end(monkeypatch):
    """Closing the SFTP service under a transfer killed it and left its
    partial file on the server; the close now waits for the transfer."""
    window_module = _module()
    window = _window(window_module)
    transfer = _Transfer()
    window._running_transfers = lambda: [transfer]
    polls = []
    monkeypatch.setattr(
        window_module.GLib, "timeout_add",
        lambda _ms, fn, *a: polls.append(fn) or 1, raising=False,
    )
    manager = MagicMock()

    window._close_backend_when_idle(manager)
    assert polls and manager.close.call_count == 0
    assert polls[0]() is True  # still running: keep waiting
    manager.close.assert_not_called()

    transfer.future.set_result(None)
    assert polls[0]() is False
    manager.close.assert_called_once_with()


def test_without_transfers_the_connection_closes_at_once():
    window_module = _module()
    window = _window(window_module)
    window._running_transfers = list
    manager = MagicMock()
    window._close_backend_when_idle(manager)
    manager.close.assert_called_once_with()


def test_closing_the_file_manager_cancels_its_transfers_first(monkeypatch):
    window_module = _module()
    window = _window(window_module)
    transfer = _Transfer()
    window._running_transfers = lambda: [transfer] if not transfer.cancelled else []
    window._manager_signal_handlers = []
    window._left_manager = None
    window._clear_progress_toast = lambda: None
    closed = []
    window._close_backend_when_idle = lambda manager: closed.append(manager)

    right = window._manager
    window._cleanup_manager()

    assert transfer.cancelled
    assert closed == [right]
    # The futures to wait for were taken before cancelling: a cancelled
    # transfer stops reading as running while the daemon still cleans up.
    assert window._draining_futures == [transfer.future]
    window._cleanup_manager()  # destroy after close-request: once only
    assert closed == [right]


def test_a_pane_that_moved_on_waits_for_a_long_transfer(monkeypatch):
    """Only a closing file manager stops waiting (it cancelled the
    transfers). A pane that picked another server must not kill a transfer
    that takes longer than the drain time."""
    window_module = _module()
    window = _window(window_module)
    transfer = _Transfer()
    window._running_transfers = lambda: [transfer]
    polls = []
    monkeypatch.setattr(
        window_module.GLib, "timeout_add",
        lambda _ms, fn, *a: polls.append(fn) or 1, raising=False,
    )
    now = [1000.0]
    monkeypatch.setattr(window_module.time, "monotonic", lambda: now[0])
    manager = MagicMock()

    window._close_backend_when_idle(manager)
    now[0] += window_module._TRANSFER_DRAIN_SECONDS * 10
    assert polls[0]() is True
    manager.close.assert_not_called()

    # The file manager closes: from then on the wait is bounded.
    window._cleanup_done = True
    assert polls[0]() is True
    now[0] += window_module._TRANSFER_DRAIN_SECONDS + 1
    assert polls[0]() is False
    manager.close.assert_called_once_with()


def test_move_cleanup_never_deletes_on_a_server_the_pane_switched_to():
    """A cut-and-paste download runs in the background; the pane may show
    another server when it ends. The originals are removed on the server
    they are on, or not at all."""
    from concurrent.futures import Future

    window_module = _module()
    window = _window(window_module)
    pane = window._right_pane
    server_a, server_b = MagicMock(), MagicMock()
    window._manager = server_a
    window._attach_refresh = lambda *a, **k: None

    stayed = Future()
    window._schedule_remote_move_cleanup(stayed, "/var/www", pane)
    stayed.set_result(None)
    server_a.remove.assert_called_once_with(
        "/var/www", recursive=True, report_progress=False
    )

    server_a.reset_mock()
    toasts = []
    pane.show_toast = lambda text, *a, **k: toasts.append(text)
    window_module.GLib.idle_add = lambda fn, *a: fn(*a)
    switched = Future()
    window._schedule_remote_move_cleanup(switched, "/var/www", pane)
    window._schedule_remote_move_cleanup(switched, "/var/log", pane)
    window._manager = server_b
    switched.set_result(None)
    server_a.remove.assert_not_called()
    server_b.remove.assert_not_called()
    # Said once for the transfer, not once per item.
    assert len(toasts) == 1


def test_move_cleanup_survives_a_reconnect_to_the_same_server():
    """Reconnecting gives the pane a new connection to the same server:
    the move still completes, through the connection the pane has now."""
    from concurrent.futures import Future

    window_module = _module()
    window = _window(window_module)
    pane = window._right_pane
    before, after = MagicMock(), MagicMock()
    before._connection_id = after._connection_id = "conn-a"
    window._manager = before
    window._attach_refresh = lambda *a, **k: None

    done = Future()
    window._schedule_remote_move_cleanup(done, "/var/www", pane)
    window._manager = after
    done.set_result(None)

    before.remove.assert_not_called()
    after.remove.assert_called_once_with(
        "/var/www", recursive=True, report_progress=False
    )


def test_a_pane_leaving_a_server_waits_only_for_that_servers_transfers():
    window_module = _module()
    window = _window(window_module)
    here, elsewhere = _Transfer(), _Transfer()
    server_a, server_b = MagicMock(), MagicMock()
    window._note_transfer_backends(here, server_a)
    window._note_transfer_backends(elsewhere, server_b)
    untagged = _Transfer()
    window._running_transfers = lambda: [here, elsewhere, untagged]

    assert window._pending_transfer_futures(server_a) == [here.future, untagged.future]
    assert len(window._pending_transfer_futures()) == 3


def test_move_cleanup_reports_remove_failure():
    from concurrent.futures import Future

    window_module = _module()
    window = _window(window_module)
    pane = window._right_pane
    manager = MagicMock()
    window._manager = manager
    window._attach_refresh = lambda *a, **k: None
    window_module.GLib.idle_add = lambda fn, *a: fn(*a)
    toasts = []
    pane.show_toast = toasts.append
    removal = Future()
    manager.remove.return_value = removal
    transfer = Future()
    window._schedule_remote_move_cleanup(transfer, "/source", pane)
    transfer.set_result(None)
    assert toasts == []
    removal.set_exception(OSError("connection still opening"))
    assert len(toasts) == 1
    assert "copy" in toasts[0]
