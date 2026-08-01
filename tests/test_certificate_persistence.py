import os
import sys

# gi.repository / Secret stubs are provided by tests/conftest.py; redefining
# them here at module level overwrote conftest's richer _DummyGIModule with
# bare SimpleNamespaces and broke every subsequently-collected test that
# needed Gtk.Box / Gdk.RGBA / etc.

# Ensure the project package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import ConnectionManager, Connection


def test_certificatefile_persistence():
    manager = ConnectionManager.__new__(ConnectionManager)
    config = {
        'host': 'example',
        'hostname': 'example.com',
        'certificatefile': '~/cert.pub',
    }

    parsed = manager.parse_host_config(config)
    expected = os.path.expanduser('~/cert.pub')
    assert parsed['certificate'] == expected

    conn = Connection(parsed)
    assert conn.certificate == expected

    # simulate reloading the SSH config
    parsed_reload = manager.parse_host_config(config)
    conn.update_data(parsed_reload)
    assert conn.certificate == expected
