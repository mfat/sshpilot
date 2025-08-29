import os
import sys
import types
import subprocess
import asyncio

# Stub external modules not available in the test environment before importing the app code.
# Create dummy 'gi' module
if 'gi' not in sys.modules:
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args, **kwargs: None
    repository = types.SimpleNamespace()

    class DummyGLib:
        MainLoop = object

        @staticmethod
        def idle_add(func, *args, **kwargs):
            return None

    class DummyGObject:
        class Object:
            pass

        class SignalFlags:
            RUN_FIRST = 0

    repository.GLib = DummyGLib
    repository.GObject = DummyGObject
    repository.Gtk = types.SimpleNamespace()
    gi.repository = repository
    sys.modules['gi'] = gi
    sys.modules['gi.repository'] = repository
    sys.modules['gi.repository.GLib'] = repository.GLib
    sys.modules['gi.repository.GObject'] = repository.GObject
    sys.modules['gi.repository.Gtk'] = repository.Gtk

# Stub 'secretstorage' module
if 'secretstorage' not in sys.modules:
    secretstorage = types.ModuleType('secretstorage')
    secretstorage.dbus_init = lambda: None
    secretstorage.get_default_collection = lambda bus: None
    sys.modules['secretstorage'] = secretstorage

# Ensure the project root is on sys.path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Now import application classes
from sshpilot.connection_manager import Connection, ConnectionManager

# Ensure an event loop is available for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())


def _generate_key_and_certificate(tmpdir: str) -> tuple[str, str]:
    """Generate an SSH key pair and a self-signed certificate using ssh-keygen."""
    key_path = os.path.join(tmpdir, 'id_rsa')
    ca_key_path = os.path.join(tmpdir, 'ca')

    # Create user key and CA key
    subprocess.run(['ssh-keygen', '-t', 'rsa', '-q', '-N', '', '-f', key_path], check=True)
    subprocess.run(['ssh-keygen', '-t', 'rsa', '-q', '-N', '', '-f', ca_key_path], check=True)

    # Sign the user key to produce a certificate
    subprocess.run([
        'ssh-keygen', '-s', ca_key_path, '-I', 'test', '-V', '+1h',
        '-n', 'testuser', f'{key_path}.pub'
    ], check=True)
    cert_path = f'{key_path}-cert.pub'
    return key_path, cert_path


def test_certificate_support(tmp_path):
    key_path, cert_path = _generate_key_and_certificate(str(tmp_path))

    data = {
        'nickname': 'cert-test',
        'host': 'localhost',
        'username': 'testuser',
        'keyfile': key_path,
        'certificate': cert_path,
        'auth_method': 0,
        'key_select_mode': 1,
    }

    conn = Connection(data)
    asyncio.get_event_loop().run_until_complete(conn.connect())

    assert any(f'CertificateFile={cert_path}' in part for part in conn.ssh_cmd)

    # Verify parsing from SSH config format
    cm = ConnectionManager.__new__(ConnectionManager)
    parsed = ConnectionManager.parse_host_config(cm, {
        'host': 'cert-test',
        'hostname': 'localhost',
        'user': 'testuser',
        'identityfile': key_path,
        'certificatefile': cert_path,
    })
    assert parsed['certificate'] == cert_path

    # Ensure updates propagate the certificate field
    conn2 = Connection({'host': 'localhost'})
    conn2.update_data({'certificate': cert_path})
    assert conn2.certificate == cert_path
