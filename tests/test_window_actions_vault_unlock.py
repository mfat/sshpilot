"""Regression tests: more daemon-connect entry points must unlock a locked
session-backed vault first, the same way opening a terminal or the file
manager does (TerminalManager._maybe_unlock_secrets_then). Without the gate,
the daemon has no stored credential to hand off and falls back to a raw
host-password prompt instead of the vault master-password prompt.

Covers three gaps found in a follow-up sweep after the terminal/file-manager/
scp/ssh-copy-id fixes:
  - MainWindow.on_manage_authorized_keys_action (window.py)
  - MainWindow.open_in_system_terminal (window.py)
  - WindowTabsMixin._on_tabmenu_show_files_panel / _on_tabmenu_fm_new_window
    (window_tabs.py)
"""

from unittest import mock

from sshpilot.window import MainWindow
from sshpilot.window_tabs import WindowTabsMixin


def _connection():
    return mock.Mock(protocol="ssh", nickname="TestHost", id="TestHost")


def _gated_terminal_manager():
    captured_retry = []
    terminal_manager = mock.Mock()
    terminal_manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )
    return terminal_manager, captured_retry


def test_manage_authorized_keys_gates_on_vault_unlock(monkeypatch):
    import sshpilot.window as window_mod

    monkeypatch.setattr(
        window_mod, "capabilities_for",
        lambda connection: frozenset({window_mod.PluginCapability.KEY_DEPLOYMENT}),
    )

    window = MainWindow.__new__(MainWindow)
    connection = _connection()
    window._context_menu_connection = connection
    window.client = mock.Mock()
    window.client_bridge = None
    terminal_manager, captured_retry = _gated_terminal_manager()
    window.terminal_manager = terminal_manager

    window.on_manage_authorized_keys_action(None)

    # Gated: the SFTP service must not have been opened yet.
    window.client.open_sftp.assert_not_called()
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    monkeypatch.setattr(
        "sshpilot.authorized_keys_window.AuthorizedKeysWindow",
        mock.Mock(),
    )
    captured_retry[0]()

    window.client.open_sftp.assert_called_once()
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()


def test_open_in_system_terminal_gates_on_vault_unlock():
    window = MainWindow.__new__(MainWindow)
    connection = _connection()
    window._daemon_ready = lambda: True
    submit_calls = []
    window._submit_external_terminal_launch = lambda conn, on_success: submit_calls.append(
        (conn, on_success)
    )
    terminal_manager, captured_retry = _gated_terminal_manager()
    window.terminal_manager = terminal_manager

    window.open_in_system_terminal(connection)

    # Gated: the daemon launch-prep call must not have been submitted yet.
    assert submit_calls == []
    assert len(captured_retry) == 1

    # Once the vault is unlocked, the retry must proceed without re-gating.
    captured_retry[0]()

    assert len(submit_calls) == 1
    assert submit_calls[0][0] is connection
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()


class _FakeTerminalWidget:
    def has_file_panel(self):
        return False

    def set_file_panel(self, widget, teardown=None):
        self.file_panel = widget


def test_tabmenu_show_files_panel_gates_on_vault_unlock(monkeypatch):
    import sshpilot.window_tabs as wt

    window = WindowTabsMixin.__new__(WindowTabsMixin)
    connection = _connection()
    terminal_widget = _FakeTerminalWidget()
    window._tab_menu_target = lambda: (None, terminal_widget)
    window.terminal_to_connection = {terminal_widget: connection}
    window.connection_manager = mock.Mock()

    monkeypatch.setattr(wt, "TerminalWidget", _FakeTerminalWidget)

    create_calls = []
    monkeypatch.setattr(
        "sshpilot.file_manager_integration.create_internal_file_manager_tab",
        lambda **kwargs: create_calls.append(kwargs) or (mock.Mock(), mock.Mock()),
    )

    terminal_manager, captured_retry = _gated_terminal_manager()
    window.terminal_manager = terminal_manager

    window._on_tabmenu_show_files_panel(None)

    # Gated: the file manager tab must not have been created yet.
    assert create_calls == []
    assert len(captured_retry) == 1

    window._track_internal_file_manager_window = lambda *a, **k: None
    captured_retry[0]()

    assert len(create_calls) == 1
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()


def test_tabmenu_fm_new_window_gates_on_vault_unlock(monkeypatch):
    import sshpilot.window_tabs as wt

    window = WindowTabsMixin.__new__(WindowTabsMixin)
    connection = _connection()
    embed = mock.Mock()
    controller = mock.Mock(_connection=connection)
    embed._controller = controller
    window._tab_menu_target = lambda: (None, mock.Mock())
    window._file_manager_embed_for_child = lambda child: embed

    launch_calls = []
    window._launch_external_file_manager = lambda conn: launch_calls.append(conn)

    terminal_manager, captured_retry = _gated_terminal_manager()
    window.terminal_manager = terminal_manager

    window._on_tabmenu_fm_new_window(None)

    # Gated: the external file manager must not have launched yet.
    assert launch_calls == []
    assert len(captured_retry) == 1

    captured_retry[0]()

    assert launch_calls == [connection]
    terminal_manager._maybe_unlock_secrets_then.assert_called_once()
