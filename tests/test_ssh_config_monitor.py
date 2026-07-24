from types import SimpleNamespace
from unittest.mock import MagicMock

import sshpilot.window as window_module
from sshpilot.window import MainWindow


class _Window:
    _on_ssh_config_directory_changed = (
        MainWindow._on_ssh_config_directory_changed)
    _reload_ssh_config_if_changed = MainWindow._reload_ssh_config_if_changed

    def __init__(self):
        self._ssh_config_reload_timeout_id = 0
        self._ssh_config_observed_fingerprint = (1, 2, 3)
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
        None, _file("/tmp/.ssh/config"), None, None)

    assert [delay for delay, _callback in scheduled] == [150, 150]
    assert removed == [41]


def test_debounced_reload_ignores_duplicate_fingerprint():
    window = _Window()
    window._ssh_config_fingerprint = lambda: (1, 2, 3)

    assert window._reload_ssh_config_if_changed() is False
    window._reload_ssh_config.assert_not_called()


def test_debounced_reload_observes_atomic_replacement():
    window = _Window()
    window._ssh_config_fingerprint = lambda: (9, 2, 3)

    assert window._reload_ssh_config_if_changed() is False
    assert window._ssh_config_observed_fingerprint == (9, 2, 3)
    window._reload_ssh_config.assert_called_once_with()
