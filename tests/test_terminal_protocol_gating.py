"""Terminal exit classification remains protocol-aware."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import Connection, ConnectionState
from sshpilot.terminal import TerminalWidget


class _StubTerminal:
    """Bare stand-in providing only what the methods under test touch."""

    _classify_exit = TerminalWidget._classify_exit

    def __init__(self, connection):
        self.connection = connection
        self.last_error_message = ''
        self._connect_failure_hint = ''
        self._used_stored_password = False


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
