"""Plugin zero (the SSH protocol backend) must be a pure indirection over the
existing build_ssh_connection() output: the SpawnSpec it returns has to be
byte-identical to what terminal.py consumed before the plugin seam."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sshpilot.ssh_connection_builder as builder
from sshpilot.connection_manager import Connection
from sshpilot.plugins.api import Capability, PluginContext, ProtocolError
from sshpilot.plugins.builtin.ssh_protocol import SshProtocolBackend
from sshpilot.plugins.registry import ProtocolRegistry
from sshpilot.ssh_connection_builder import SSHConnectionCommand


def _ctx(config=None, connection_manager=None):
    return PluginContext(
        plugin_id="ssh",
        app_config=config,
        connection_manager=connection_manager,
        protocol_registry=ProtocolRegistry(),
    )


def test_prepared_command_passthrough():
    """When Connection.native_connect() already prepared ssh_connection_cmd,
    build_spawn must reproduce it exactly (argv, env, auth flags)."""
    conn = Connection({'nickname': 'example', 'hostname': 'example.com'})
    prepared = SSHConnectionCommand(
        command=['ssh', '-F', '/tmp/config', 'example'],
        env={'SSH_ASKPASS': '/usr/libexec/askpass', 'TERM': 'xterm'},
        use_sshpass=True,
        password='hunter2',
        use_askpass=False,
    )
    conn.ssh_connection_cmd = prepared

    spec = SshProtocolBackend().build_spawn(conn, _ctx())

    assert spec.argv == list(prepared.command)
    assert spec.env == dict(prepared.env)
    assert spec.extras['use_sshpass'] is True
    assert spec.extras['password'] == 'hunter2'
    assert spec.extras['use_askpass'] is False
    # And the original objects are not aliased (terminal.py mutates its copies).
    assert spec.argv is not prepared.command
    assert spec.env is not prepared.env


def test_fresh_build_uses_unified_builder(monkeypatch):
    """Without a prepared command (reconnects, provider-injected connections),
    build_spawn must call the single unified builder with the same context a
    native connect would use, and pass its result through unchanged."""
    conn = Connection({'nickname': 'example', 'hostname': 'example.com'})
    fake_cfg = object()
    fake_cm = object()
    seen = {}

    fresh = SSHConnectionCommand(
        command=['ssh', '-F', '/tmp/config', 'example'],
        env={'A': 'b'},
        use_sshpass=False,
        password=None,
        use_askpass=True,
    )

    def fake_build(ctx):
        seen['ctx'] = ctx
        return fresh

    monkeypatch.setattr(builder, 'build_ssh_connection', fake_build)

    spec = SshProtocolBackend().build_spawn(
        conn, _ctx(config=fake_cfg, connection_manager=fake_cm))

    ctx = seen['ctx']
    assert ctx.connection is conn
    assert ctx.connection_manager is fake_cm
    assert ctx.config is fake_cfg
    assert ctx.command_type == 'ssh'
    assert ctx.native_mode is True

    assert spec.argv == list(fresh.command)
    assert spec.env == dict(fresh.env)
    assert spec.extras == {
        'use_sshpass': False, 'password': None, 'use_askpass': True,
    }


def test_builder_failure_becomes_protocol_error(monkeypatch):
    conn = Connection({'nickname': 'example', 'hostname': 'example.com'})

    def fake_build(ctx):
        raise OSError("no ssh binary")

    monkeypatch.setattr(builder, 'build_ssh_connection', fake_build)

    with pytest.raises(ProtocolError, match="no ssh binary"):
        SshProtocolBackend().build_spawn(conn, _ctx())


def test_ssh_backend_metadata():
    backend = SshProtocolBackend()
    assert backend.protocol_id == 'ssh'
    assert backend.default_port == 22
    caps = backend.capabilities()
    assert Capability.FILE_TRANSFER in caps
    assert Capability.KEY_DEPLOYMENT in caps
    assert Capability.PORT_FORWARDING in caps
    # SSH keeps the hand-built dialog: no declarative fields.
    assert backend.connection_fields() == []
