"""ctx.list_connections() returns a read-only ConnectionInfo snapshot of every
saved connection, decoupled from the internal Connection objects."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins.api import API_VERSION, ConnectionInfo, PluginContext
from sshpilot.plugins.registry import ProtocolRegistry


class _Conn:
    def __init__(self, nickname, hostname, username, protocol, port):
        self.nickname = nickname
        self.hostname = hostname
        self.username = username
        self.protocol = protocol
        self.port = port


class _Manager:
    def __init__(self, connections):
        self.connections = connections


def _ctx(manager):
    return PluginContext(plugin_id="test-plugin", app_config=None,
                         connection_manager=manager,
                         protocol_registry=ProtocolRegistry())


def test_lists_all_connections_as_snapshots():
    manager = _Manager([
        _Conn('web', '10.0.0.1', 'root', 'ssh', 22),
        _Conn('db', '10.0.0.2', 'admin', 'ssh', 2222),
    ])
    out = _ctx(manager).list_connections()

    assert all(isinstance(c, ConnectionInfo) for c in out)
    assert [(c.nickname, c.host, c.username, c.port) for c in out] == [
        ('web', '10.0.0.1', 'root', 22),
        ('db', '10.0.0.2', 'admin', 2222),
    ]


def test_empty_when_no_connections():
    assert _ctx(_Manager([])).list_connections() == []


def test_tolerates_manager_without_connections_attr():
    class _Bare:
        pass

    assert _ctx(_Bare()).list_connections() == []


def test_api_version_is_at_least_1_4():
    assert API_VERSION >= (1, 4)
