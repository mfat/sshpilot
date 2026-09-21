"""The start page offers a Dashboard only where a probe can actually run."""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import load_plugins
from sshpilot.welcome_page import WelcomePage


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


@pytest.fixture(autouse=True)
def builtin_protocols(monkeypatch, tmp_path):
    """Real built-in backends, so the gate is read from their capabilities."""
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    load_plugins(app_config=FakeConfig(), connection_manager=None)


def _summary(protocol):
    return SimpleNamespace(nickname="demo", display_target="alice@example.test",
                           protocol=protocol)


def test_ssh_row_offers_the_dashboard():
    page = SimpleNamespace(_dashboard_callback=WelcomePage._dashboard_callback)
    assert page._dashboard_callback(page, _summary("ssh")) is not None


@pytest.mark.parametrize("protocol", ["telnet", "serial", "docker", "k8s", "mosh"])
def test_rows_without_remote_command_have_no_info_button(protocol):
    """A protocol that cannot run a command cannot be gathered from, so the
    info button must not appear -- it would open a Dashboard that never fills."""
    page = SimpleNamespace(_dashboard_callback=WelcomePage._dashboard_callback)
    assert page._dashboard_callback(page, _summary(protocol)) is None


def test_unknown_protocol_has_no_info_button():
    page = SimpleNamespace(_dashboard_callback=WelcomePage._dashboard_callback)
    assert page._dashboard_callback(page, _summary("not-a-protocol")) is None
