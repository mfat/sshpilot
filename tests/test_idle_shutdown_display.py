"""Preferences must show the idle shutdown the background service really uses."""

import sys

import pytest

from sshpilot.daemon_terminal_policy import effective_idle_shutdown_seconds


class _Config:
    def __init__(self, **values):
        self._values = values

    def get_setting(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    monkeypatch.delenv("SSHPILOT_PACKAGED", raising=False)
    monkeypatch.delenv("SSHPILOT_DAEMON_SERVICE_MODE", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_unset_from_source_is_300():
    assert effective_idle_shutdown_seconds(_Config()) == 300


def test_unset_in_frozen_build_is_120(monkeypatch):
    # macOS and Windows builds are frozen; the launcher marks them packaged.
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert effective_idle_shutdown_seconds(_Config()) == 120


def test_unset_with_packaged_environment_is_120(monkeypatch):
    monkeypatch.setenv("SSHPILOT_PACKAGED", "1")
    assert effective_idle_shutdown_seconds(_Config()) == 120


def test_unset_in_service_mode_never_stops():
    config = _Config(**{"daemon.service_mode": True})
    assert effective_idle_shutdown_seconds(config) == 0


def test_explicit_value_wins_everywhere(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    config = _Config(**{"daemon.idle_shutdown_seconds": 45})
    assert effective_idle_shutdown_seconds(config) == 45
    assert effective_idle_shutdown_seconds(
        _Config(**{"daemon.idle_shutdown_seconds": 0})
    ) == 0


def test_unreadable_value_falls_back_to_default():
    config = _Config(**{"daemon.idle_shutdown_seconds": "soon"})
    assert effective_idle_shutdown_seconds(config) == 300
