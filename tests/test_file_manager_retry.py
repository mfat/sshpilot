"""Retry in the SFTP file manager must reopen a dead session.

The pane Retry pill emits ``path-changed``. For a still-READY session that
is a re-list; after SFTP FAILED/CLOSED it must call ``_reconnect`` even
though ``DaemonSftpManager._client`` (the daemon IPC client) is still set.
"""
import sys
import types
from unittest.mock import MagicMock


def _ensure_cairo_stub():
    if "cairo" not in sys.modules:
        sys.modules["cairo"] = types.SimpleNamespace()


def _window():
    _ensure_cairo_stub()
    from sshpilot.file_manager_window import FileManagerWindow

    win = FileManagerWindow.__new__(FileManagerWindow)
    win._left_pane = MagicMock(name="left")
    win._right_pane = MagicMock(name="right")
    win._right_pane._current_path = "/home/alice"
    win._pending_paths = {win._left_pane: None, win._right_pane: None}
    win._loading_toast_timeouts = {win._left_pane: None, win._right_pane: None}
    win._refreshing_panes = set()
    win._host = "example.test"
    win._is_disposed = False
    win._connection_error_reported = False
    win._reconnect = MagicMock(name="_reconnect")
    win._clear_progress_toast = MagicMock()
    return win


def test_sftp_session_ready_ignores_daemon_ipc_client():
    from sshpilot.file_manager_window import _sftp_session_ready

    dead = MagicMock()
    dead._client = object()
    dead.is_connected.return_value = False
    dead._sftp = None
    assert _sftp_session_ready(dead) is False

    live = MagicMock()
    live._client = object()
    live.is_connected.return_value = True
    assert _sftp_session_ready(live) is True

    assert _sftp_session_ready(None) is False


def test_retry_reconnects_when_sftp_session_is_dead():
    win = _window()
    manager = MagicMock()
    manager._client = object()
    manager.is_connected.return_value = False
    manager._sftp = None
    win._manager = manager

    win._on_path_changed(win._right_pane, "/home/alice")

    win._reconnect.assert_called_once()
    manager.listdir.assert_not_called()
    assert win._pending_paths[win._right_pane] == "/home/alice"


def test_retry_relists_when_sftp_session_is_ready():
    win = _window()
    manager = MagicMock()
    manager._client = object()
    manager.is_connected.return_value = True
    win._manager = manager

    win._on_path_changed(win._right_pane, "/no/permission")

    win._reconnect.assert_not_called()
    manager.listdir.assert_called_once_with("/no/permission")
    assert win._pending_paths[win._right_pane] == "/no/permission"


def test_retry_without_manager_remembers_path_in_picker_mode():
    win = _window()
    win._manager = None

    win._on_path_changed(win._right_pane, "/home/alice")

    win._reconnect.assert_not_called()
    assert win._pending_paths[win._right_pane] == "/home/alice"


def test_pane_retry_emits_failed_path():
    _ensure_cairo_stub()
    from sshpilot.file_manager.pane import FilePane

    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._load_error_path = "/failed/dir"
    pane._current_path = "/old"
    pane.emit = MagicMock()

    pane._on_retry_load_clicked(None)

    pane.emit.assert_called_once_with("path-changed", "/failed/dir")


def test_pane_retry_falls_back_to_home_on_remote():
    _ensure_cairo_stub()
    from sshpilot.file_manager.pane import FilePane

    pane = FilePane.__new__(FilePane)
    pane._is_remote = True
    pane._load_error_path = None
    pane._current_path = None
    pane.emit = MagicMock()

    pane._on_retry_load_clicked(None)

    pane.emit.assert_called_once_with("path-changed", "~")


def test_connection_error_retry_path_falls_back_to_current(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.file_manager_window.GLib.idle_add",
        lambda fn, *args, **kwargs: (fn(*args, **kwargs), 1)[1],
    )
    win = _window()
    manager = MagicMock()
    win._manager = manager
    win._pending_paths[win._right_pane] = None

    win._on_connection_error(manager, "The SFTP connection was lost")

    win._right_pane.show_load_error.assert_called_once_with(
        "/home/alice", "The SFTP connection was lost"
    )
    assert win._connection_error_reported is True


def test_connection_error_idle_ignores_replaced_manager(monkeypatch):
    queued = []
    monkeypatch.setattr(
        "sshpilot.file_manager_window.GLib.idle_add",
        lambda fn, *args, **kwargs: queued.append(fn) or 1,
    )
    win = _window()
    old_manager = MagicMock()
    win._manager = old_manager

    win._on_connection_error(old_manager, "lost")
    win._manager = MagicMock()
    queued[0]()

    win._right_pane.show_load_error.assert_not_called()


def test_direct_operation_error_reaches_retry_pane_localized():
    win = _window()
    manager = MagicMock()
    win._manager = manager
    win._pending_paths[win._right_pane] = "/root-only"

    win._on_operation_error(manager, "Accès refusé")

    win._right_pane.show_load_error.assert_called_once_with(
        "/root-only", "Accès refusé"
    )
