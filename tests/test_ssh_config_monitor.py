from unittest.mock import MagicMock

from sshpilot.window import MainWindow


def test_gtk_window_does_not_own_ssh_config_filesystem_monitors():
    """The daemon owns config observation after the GTK migration."""

    removed_callbacks = {
        "_on_ssh_config_directory_changed",
        "_reload_ssh_config_if_changed",
        "_on_connection_manager_config_written",
        "_refresh_ssh_config_monitors",
        "_setup_ssh_config_monitor",
    }

    assert removed_callbacks.isdisjoint(MainWindow.__dict__)


def test_compatibility_reload_refreshes_daemon_projection():
    window = MainWindow.__new__(MainWindow)
    window.connection_manager = MagicMock()
    window.connection_manager.get_connections.return_value = ["demo"]
    window.group_manager = MagicMock()
    window.rebuild_connection_list = MagicMock()
    window.effective_config_checker = MagicMock()

    window._reload_ssh_config(create_missing=False)

    window.connection_manager.refresh.assert_called_once_with()
    window.group_manager.bind_connections.assert_called_once_with(["demo"])
    window.effective_config_checker.invalidate.assert_called_once_with()
    window.rebuild_connection_list.assert_called_once_with()


def test_monitor_teardown_is_a_compatibility_noop():
    window = MainWindow.__new__(MainWindow)

    assert window._teardown_ssh_config_monitor() is None
