"""Guards the spawn-time PluginContext construction that terminal.py performs.

Regression: PluginContext.plugin_id became required (per-plugin contexts), but
the terminal seam built the context without it — raising TypeError on every
terminal open. terminal.py itself can't be imported under the gi stub, so we
test the exact construction (PluginContext.for_spawn) and the registry
plugin-id tracking it relies on."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.registry import ProtocolRegistry
from sshpilot.ssh_connection_builder import SSHConnectionCommand


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    monkeypatch.setattr(registry_mod, "_registry", None)


def test_registry_tracks_registering_plugin_id():
    from sshpilot.plugins.builtin.ssh_protocol import SshProtocolBackend
    reg = ProtocolRegistry()
    reg.register(SshProtocolBackend(), plugin_id="ssh")
    assert reg.plugin_id_for("ssh") == "ssh"
    # Unknown / registered without an id.
    assert reg.plugin_id_for("nope") is None


def test_register_protocol_records_plugin_id():
    from sshpilot.plugins.builtin.telnet_protocol import Plugin
    ctx = PluginContext(plugin_id="telnet", app_config=None,
                        connection_manager=None,
                        protocol_registry=registry_mod.protocol_registry())
    Plugin().activate(ctx)
    assert registry_mod.protocol_registry().plugin_id_for("telnet") == "telnet"


def test_for_spawn_builds_hostless_context():
    ctx = PluginContext.for_spawn(
        plugin_id="core", app_config=object(), connection_manager=object(),
        protocol_registry=ProtocolRegistry())
    assert ctx.plugin_id == "core"
    # Host-less: no event/UI facades, but secrets/settings are present.
    assert ctx.events is None
    assert ctx.ui is None
    assert ctx.secrets is not None
    assert ctx.settings is not None


def test_ssh_build_spawn_works_with_for_spawn_context():
    # Reproduces what terminal.py does for an SSH connection.
    from sshpilot.plugins.builtin.ssh_protocol import SshProtocolBackend
    conn = Connection({'nickname': 'h', 'hostname': 'h.example.com'})
    conn.ssh_connection_cmd = SSHConnectionCommand(
        command=['ssh', '-F', '/tmp/cfg', 'h'], env={'A': 'b'})
    ctx = PluginContext.for_spawn(
        plugin_id="ssh", app_config=None, connection_manager=None,
        protocol_registry=ProtocolRegistry())
    spec = SshProtocolBackend().build_spawn(conn, ctx)
    assert spec.argv == ['ssh', '-F', '/tmp/cfg', 'h']


def test_telnet_build_spawn_works_with_for_spawn_context(monkeypatch):
    import sshpilot.plugins.builtin.telnet_protocol as mod
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/usr/bin/telnet')
    conn = Connection({'nickname': 't', 'protocol': 'telnet', 'host': '10.0.0.5'})
    ctx = PluginContext.for_spawn(
        plugin_id="telnet", app_config=None, connection_manager=None,
        protocol_registry=ProtocolRegistry())
    spec = mod.TelnetProtocolBackend().build_spawn(conn, ctx)
    assert spec.argv == ['/usr/bin/telnet', '10.0.0.5']
