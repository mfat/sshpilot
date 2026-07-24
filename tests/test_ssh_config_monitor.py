from types import SimpleNamespace
from unittest.mock import MagicMock

import sshpilot.window as window_module
from sshpilot.window import MainWindow


class _Window:
    _on_ssh_config_directory_changed = (
        MainWindow._on_ssh_config_directory_changed)
    _reload_ssh_config_if_changed = MainWindow._reload_ssh_config_if_changed
    _on_connection_manager_config_written = (
        MainWindow._on_connection_manager_config_written)
    _teardown_ssh_config_monitor = MainWindow._teardown_ssh_config_monitor

    def __init__(self):
        self._ssh_config_reload_timeout_id = 0
        self._ssh_config_observed_fingerprints = {
            "/tmp/.ssh/config": (1, 2, 3),
            "/tmp/.ssh/conf.d/work.conf": (4, 5, 6),
        }
        self._reload_ssh_config = MagicMock()

    def _ssh_config_path(self):
        return "/tmp/.ssh/config"


def _file(path):
    return SimpleNamespace(get_path=lambda: path)


def test_directory_monitor_filters_and_debounces(monkeypatch):
    window = _Window()
    scheduled = []
    removed = []
    monkeypatch.setattr(
        window_module.GLib, "timeout_add",
        lambda delay, callback: scheduled.append((delay, callback)) or 41)
    monkeypatch.setattr(
        window_module.GLib, "source_remove", removed.append)

    window._on_ssh_config_directory_changed(
        None, _file("/tmp/.ssh/known_hosts"), None, None)
    assert scheduled == []

    window._on_ssh_config_directory_changed(
        None, _file("/tmp/.ssh/config.tmp"), _file("/tmp/.ssh/config"), None)
    window._on_ssh_config_directory_changed(
        None, _file("/tmp/.ssh/conf.d/work.conf"), None, None)

    assert [delay for delay, _callback in scheduled] == [150, 150]
    assert removed == [41]


def test_debounced_reload_ignores_duplicate_fingerprint():
    window = _Window()
    window._ssh_config_fingerprints = lambda: dict(
        window._ssh_config_observed_fingerprints)

    assert window._reload_ssh_config_if_changed() is False
    window._reload_ssh_config.assert_not_called()


def test_debounced_reload_observes_atomic_replacement():
    window = _Window()
    changed = dict(window._ssh_config_observed_fingerprints)
    changed["/tmp/.ssh/config"] = (9, 2, 3)
    window._ssh_config_fingerprints = lambda: changed

    assert window._reload_ssh_config_if_changed() is False
    assert window._ssh_config_observed_fingerprints == changed
    window._reload_ssh_config.assert_called_once_with(create_missing=False)


def test_app_owned_root_config_write_updates_observed_fingerprint():
    window = _Window()
    window._ssh_config_file_fingerprint = lambda _path: (7, 8, 9)

    window._on_connection_manager_config_written(
        None, "/tmp/.ssh/config")

    assert window._ssh_config_observed_fingerprints[
        "/tmp/.ssh/config"] == (7, 8, 9)


def test_app_owned_include_write_updates_its_observed_fingerprint():
    window = _Window()
    window._ssh_config_file_fingerprint = lambda _path: (7, 8, 9)

    window._on_connection_manager_config_written(
        None, "/tmp/.ssh/conf.d/work.conf")

    assert window._ssh_config_observed_fingerprints[
        "/tmp/.ssh/conf.d/work.conf"] == (7, 8, 9)
    assert window._ssh_config_observed_fingerprints[
        "/tmp/.ssh/config"] == (1, 2, 3)


def test_teardown_releases_timeout_monitors_and_manager_signal(monkeypatch):
    window = _Window()
    monitor = MagicMock()
    manager = SimpleNamespace()
    window.connection_manager = manager
    window._ssh_config_reload_timeout_id = 41
    window._ssh_config_monitors = {"/tmp/.ssh": (monitor, 17)}
    window._ssh_config_written_handler = 23
    removed = []
    disconnected = []
    monkeypatch.setattr(window_module.GLib, "source_remove", removed.append)
    monkeypatch.setattr(
        window_module.GObject.Object, "disconnect",
        lambda obj, handler_id: disconnected.append((obj, handler_id)),
        raising=False)

    window._teardown_ssh_config_monitor()

    assert removed == [41]
    monitor.disconnect.assert_called_once_with(17)
    monitor.cancel.assert_called_once_with()
    assert disconnected == [(manager, 23)]
    assert window._ssh_config_monitors == {}
    assert window._ssh_config_written_handler is None
