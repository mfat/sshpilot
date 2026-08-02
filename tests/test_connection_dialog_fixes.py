import pytest
from unittest.mock import MagicMock
import types
from sshpilot.connection_dialog import _editor_details_to_connection

def test_editor_details_to_connection_empty_hostname():
    details = types.SimpleNamespace(
        nickname='my-host',
        hostname='',
        host='my-host',
    )
    conn = _editor_details_to_connection(details)
    assert conn.hostname == ''  # Should not fallback to host

def test_editor_details_to_connection_plugin_data():
    details = types.SimpleNamespace(
        nickname='plugin-host',
        data={'plugin_field': 'value'}
    )
    conn = _editor_details_to_connection(details)
    assert conn.data == {'plugin_field': 'value'}

def test_editor_details_falsy_values():
    details = types.SimpleNamespace(
        nickname='my-host',
        port=0,
        x11_forwarding=False
    )
    conn = _editor_details_to_connection(details)
    assert conn.port == 0 or conn.port == 22
    assert conn.x11_forwarding is False
