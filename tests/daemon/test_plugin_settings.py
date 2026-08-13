"""Daemon ownership tests for the plugin settings compatibility API."""

from sshpilot.api import DaemonClient
from sshpilot.api.capabilities import Capability
from sshpilot.daemon.plugin_settings import PluginSettingsService
from sshpilot.daemon.server import CoreServices, DaemonServer
from tests.helpers.fake_connection_repository import make_test_connection_service


def test_plugin_settings_use_daemon_transaction_store(tmp_path):
    server = DaemonServer(
        lambda: CoreServices(
            connections=make_test_connection_service(),
            plugin_settings=PluginSettingsService(tmp_path / "config.json"),
        ),
        socket_path=tmp_path / "daemon.sock",
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=tmp_path / "daemon.sock")
    try:
        assert Capability.PLUGIN_SETTINGS_READ in client.get_capabilities().supported
        assert Capability.PLUGIN_SETTINGS_WRITE in client.get_capabilities().supported
        assert client.get_plugin_setting("docker", "region", "fra1") == "fra1"
        client.set_plugin_setting("docker", "region", "ams1")
        assert client.get_plugin_setting("docker", "region", "fra1") == "ams1"
        assert client.get_plugin_setting("other", "region", "fra1") == "fra1"
    finally:
        client.close()
        server.shutdown()
        assert server.wait_stopped()
