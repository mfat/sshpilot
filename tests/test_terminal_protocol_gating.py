"""Non-SSH connections must not run through the SSH command-preparation
machinery (native_connect / ssh_connection_cmd) in the terminal layer."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionState
from sshpilot.terminal import TerminalWidget


class _StubTerminal:
    """Bare stand-in providing only what the methods under test touch."""

    _refresh_connection_command = TerminalWidget._refresh_connection_command
    _classify_exit = TerminalWidget._classify_exit

    def __init__(self, connection):
        self.connection = connection
        self.last_error_message = ''
        self._connect_failure_hint = ''
        self._used_stored_password = False


class _TrackingConnection(Connection):
    def __init__(self, data):
        super().__init__(data)
        self.native_connect_called = False

    async def native_connect(self):
        self.native_connect_called = True
        return True


def test_refresh_skips_native_connect_for_plugin_protocols():
    conn = _TrackingConnection({'nickname': 't', 'protocol': 'telnet',
                                'host': '10.0.0.5'})
    conn.ssh_connection_cmd = object()  # must be left alone
    term = _StubTerminal(conn)

    assert term._refresh_connection_command() is True
    assert conn.native_connect_called is False
    assert conn.ssh_connection_cmd is not None


def test_classify_exit_nonzero_is_failure_for_plugin_protocols():
    conn = Connection({'nickname': 't', 'protocol': 'telnet', 'host': 'h'})
    term = _StubTerminal(conn)

    # telnet exits 1 on connection failure (ssh reserves 255).
    state, _reason = term._classify_exit(1, was_connected=False)
    assert state == ConnectionState.FAILED

    state, _reason = term._classify_exit(1, was_connected=True)
    assert state == ConnectionState.DISCONNECTED


def test_classify_exit_unchanged_for_ssh():
    conn = Connection({'nickname': 's', 'hostname': 'h'})
    term = _StubTerminal(conn)

    # Exit 1 from a remote shell after a real session: not a failure.
    state, reason = term._classify_exit(1, was_connected=False)
    assert state == ConnectionState.DISCONNECTED
    assert reason == ''

    state, _reason = term._classify_exit(255, was_connected=False)
    assert state == ConnectionState.FAILED


def test_classify_exit_message_markers_work_for_telnet():
    conn = Connection({'nickname': 't', 'protocol': 'telnet', 'host': 'h'})
    term = _StubTerminal(conn)
    term.last_error_message = 'telnet: Unable to connect to remote host: Connection refused'

    state, reason = term._classify_exit(1, was_connected=False)
    assert state == ConnectionState.FAILED
    assert reason == 'Connection refused'
