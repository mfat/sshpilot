"""The keyring identifier for non-SSH (plugin protocol) passwords is scoped by
protocol so two connections sharing a host — or with empty usernames — can't
collide on a single keyring slot."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import ConnectionManager

_host = ConnectionManager._non_ssh_password_host


def test_protocol_scopes_the_key():
    a = _host({'protocol': 'docker', 'nickname': 'web'})
    b = _host({'protocol': 'k8s', 'nickname': 'web'})
    assert a != b
    assert a == 'docker:web'
    assert b == 'k8s:web'


def test_same_host_distinct_nicknames_dont_collide():
    a = _host({'protocol': 'serial', 'nickname': 'board-a', 'host': 'ttyUSB0'})
    b = _host({'protocol': 'serial', 'nickname': 'board-b', 'host': 'ttyUSB0'})
    assert a != b


def test_falls_back_to_host_then_hostname():
    assert _host({'protocol': 'mosh', 'host': 'srv'}) == 'mosh:srv'
    assert _host({'protocol': 'mosh', 'hostname': 'srv2'}) == 'mosh:srv2'


def test_missing_protocol_defaults_to_plugin():
    assert _host({'nickname': 'thing'}) == 'plugin:thing'


def test_no_identifier_returns_empty():
    assert _host({'protocol': 'docker'}) == ''


def test_accepts_object_with_data_attr():
    class _Conn:
        data = {'protocol': 'docker', 'nickname': 'web'}

    assert _host(_Conn()) == 'docker:web'
