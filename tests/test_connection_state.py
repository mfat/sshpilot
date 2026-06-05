"""Tests for the default-keepalive injection (Phase 1) and the authoritative
ConnectionState model (Phase 2)."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot import ssh_connection_builder as builder
from sshpilot.connection_manager import Connection, ConnectionState


def _inject(overrides=None, app_cfg=None):
    cmd = []
    builder._maybe_append_default_keepalive(cmd, overrides or [], app_cfg or {})
    return cmd


# --- Phase 1: default keepalive injection -----------------------------------

def test_default_keepalive_injected_when_unset():
    cmd = _inject(app_cfg={
        'apply_default_keepalive': True,
        'default_keepalive_interval': 15,
        'default_keepalive_count': 3,
    })
    assert cmd == ['-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']


def test_default_keepalive_respects_opt_out():
    cmd = _inject(app_cfg={'apply_default_keepalive': False})
    assert cmd == []


def test_default_keepalive_skipped_when_app_value_set():
    cmd = _inject(app_cfg={'apply_default_keepalive': True, 'keepalive_interval': 33})
    assert cmd == []


def test_default_keepalive_skipped_when_in_overrides():
    cmd = _inject(
        overrides=['-o', 'ServerAliveInterval=42'],
        app_cfg={'apply_default_keepalive': True},
    )
    assert cmd == []


def test_default_keepalive_defaults_to_on_when_flag_absent():
    # Missing apply_default_keepalive defaults to True.
    cmd = _inject(app_cfg={'default_keepalive_interval': 20, 'default_keepalive_count': 2})
    assert cmd == ['-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=2']


# --- Phase 2: ConnectionState model + is_connected compat -------------------

def test_initial_state_is_unknown():
    c = Connection({'nickname': 'a', 'host': 'a'})
    assert c.get_status() == ConnectionState.UNKNOWN
    assert c.is_connected is False


def test_is_connected_setter_maps_to_state():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.is_connected = True
    assert c.get_status() == ConnectionState.CONNECTED
    assert c.is_connected is True
    c.is_connected = False
    assert c.get_status() == ConnectionState.DISCONNECTED
    assert c.is_connected is False


def test_set_status_carries_reason():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.FAILED, 'Authentication failed')
    assert c.get_status() == ConnectionState.FAILED
    assert c.get_status_reason() == 'Authentication failed'
    assert c.is_connected is False


def test_bool_false_does_not_clobber_failed():
    # A plain is_connected=False (e.g. generic cleanup) must not erase a richer
    # FAILED reason already recorded.
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.FAILED, 'Host unreachable')
    c.is_connected = False
    assert c.get_status() == ConnectionState.FAILED
    assert c.get_status_reason() == 'Host unreachable'


def test_connecting_is_not_connected():
    c = Connection({'nickname': 'a', 'host': 'a'})
    c.set_status(ConnectionState.CONNECTING)
    assert c.is_connected is False


# --- Phase 3: ssh exit classification ---------------------------------------

import pytest


@pytest.mark.parametrize('msg, code, was_connected, exp_state, exp_reason', [
    ('Permission denied (publickey,password).', 255, False, ConnectionState.FAILED, 'Authentication failed'),
    ('ssh: connect to host x port 22: Connection refused', 255, False, ConnectionState.FAILED, 'Connection refused'),
    ('ssh: connect to host x: No route to host', 255, False, ConnectionState.FAILED, 'Host unreachable'),
    ('Could not resolve hostname x: Name or service not known', 255, False, ConnectionState.FAILED, 'Host not found'),
    ('Host key verification failed.', 255, False, ConnectionState.FAILED, 'Host key verification failed'),
    (None, 255, True, ConnectionState.DISCONNECTED, 'Connection lost'),
    (None, 255, False, ConnectionState.FAILED, 'Connection failed'),
    (None, 1, False, ConnectionState.DISCONNECTED, ''),
])
def test_classify_exit(msg, code, was_connected, exp_state, exp_reason):
    terminal_mod = pytest.importorskip('sshpilot.terminal')

    class _T:
        last_error_message = msg

    state, reason = terminal_mod.TerminalWidget._classify_exit(_T(), code, was_connected)
    assert state == exp_state
    assert reason == exp_reason
