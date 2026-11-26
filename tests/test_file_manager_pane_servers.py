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


def test_paste_between_two_server_panes_copies_through_the_daemon():
    window_module = _module()
    window = _window(window_module)
    left_manager = MagicMock(name="left-manager")
    window._left_connection = _connection("oracle")
    window._left_manager = left_manager
    window._connection = _connection("googlerouter")
    window._show_progress_dialog = MagicMock()
    window._attach_refresh = MagicMock()
    entry = SimpleNamespace(name="docs", is_dir=True)

    window._perform_pane_to_pane_remote_operation(
        [entry], "/root", "/srv", False,
        source_pane=window._left_pane, destination_pane=window._right_pane,
    )

    left_manager.copy_remote.assert_called_once_with(
        "/root/docs", "/srv/docs", recursive=True, move=False,
        destination_manager=window._manager,
    )
    window._manager.copy_remote.assert_not_called()
