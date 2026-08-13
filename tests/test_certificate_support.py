import os
import shutil
import subprocess

import pytest

from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_formatter import format_ssh_config_entry


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
    if shutil.which("ssh-keygen") is None:
        pytest.skip("ssh-keygen not available")
    key_path, cert_path = _generate_key_and_certificate(str(tmp_path))

    data = {
        'nickname': 'cert-test',
        'hostname': 'localhost',
        'username': 'testuser',
        'keyfile': key_path,
        'certificate': cert_path,
        'auth_method': 0,
        'key_select_mode': 2,
    }

    # Native mode keeps per-host settings in ~/.ssh/config; the config writer
    # emits the certificate directive.
    entry = format_ssh_config_entry(data)
    assert f'CertificateFile {cert_path}' in entry

    # Verify parsing from SSH config format
    config_path = tmp_path / "config"
    config_path.write_text(
        "Host cert-test\n"
        f"    HostName localhost\n"
        f"    User testuser\n"
        f"    IdentityFile {key_path}\n"
        f"    CertificateFile {cert_path}\n",
        encoding="utf-8",
    )
    loaded = load_ssh_configuration(config_path, isolated=False)
    parsed = loaded.connections[0].data
    assert parsed['certificate'] == cert_path
    assert parsed['certificate_files'] == [cert_path]
