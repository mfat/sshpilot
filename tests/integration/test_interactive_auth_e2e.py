"""Integration: headless SFTP + askpass against a REAL 2FA SSH server.

Drives the PTY-less ``OpenSSHSFTPManager`` with headless askpass against the
mock 2FA sshd from ``tests/integration/interactive_ssh_server`` (password
"password" + PAM verification code "123456" over keyboard-interactive).

A throwaway askpass script answers the login password then the OTP; the
worker authenticates itself (no MasterSession).

Start the server first:  docker compose -f
tests/integration/interactive_ssh_server/docker-compose.yml up -d --build
Run with real PyGObject:  SSHPILOT_GUI_TESTS=1 pytest tests/integration/test_interactive_auth_e2e.py
Skips when nothing listens on 127.0.0.1:2225 or when gi is the test stub.
"""

import os
import socket
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

pytestmark = pytest.mark.integration

HOST_ALIAS = "sshpilot-mock-2fa"
PORT = 2225
PASSWORD = "password"
CODE = "123456"


def _server_ready() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2):
            return True
    except OSError:
        return False


def _real_gi() -> bool:
    """The suite's conftest stubs gi unless SSHPILOT_GUI_TESTS=1; the SFTP
    worker needs the real GObject to be a functioning class."""
    gi = sys.modules.get("gi")
    return gi is None or getattr(gi, "__file__", None) is not None


_needs_server = pytest.mark.skipif(
    not _server_ready(), reason="mock 2FA sshd not running on :2225")
_needs_gi = pytest.mark.skipif(
    not _real_gi(), reason="needs real PyGObject (run with SSHPILOT_GUI_TESTS=1)")


@pytest.fixture
def ssh_config(tmp_path):
    """A throwaway -F config so the test never touches ~/.ssh."""
    cfg = tmp_path / "ssh_config"
    cfg.write_text(
        f"Host {HOST_ALIAS}\n"
        f"    HostName 127.0.0.1\n"
        f"    Port {PORT}\n"
        f"    User testuser\n"
        f"    UserKnownHostsFile {tmp_path / 'known_hosts'}\n"
        f"    StrictHostKeyChecking accept-new\n"
    )
    return str(cfg)


@pytest.fixture
def askpass_script(tmp_path):
    """Minimal SSH_ASKPASS that returns password then OTP from the prompt text."""
    path = tmp_path / "askpass.py"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "prompt = ' '.join(sys.argv[1:]).lower()\n"
        "if 'verification' in prompt or 'code' in prompt or 'otp' in prompt:\n"
        f"    print({CODE!r})\n"
        "else:\n"
        f"    print({PASSWORD!r})\n"
    )
    path.chmod(0o755)
    return str(path)


def _connection(ssh_config):
    return types.SimpleNamespace(
        nickname=HOST_ALIAS,
        hostname="127.0.0.1",
        username="testuser",
        auth_method=0,
        password=PASSWORD,
        resolved_identity_files=[],
        _resolve_config_override_path=lambda: ssh_config,
    )


@_needs_server
@_needs_gi
def test_headless_sftp_2fa_end_to_end(monkeypatch, ssh_config, askpass_script):
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder._askpass_env_for_connection",
        lambda *a, **k: {
            "SSH_ASKPASS": askpass_script,
            "SSH_ASKPASS_REQUIRE": "prefer",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        },
    )

    import gi  # noqa: F401 — real gi; the worker only needs GObject

    from sshpilot.file_manager.openssh_backend import OpenSSHSFTPManager

    conn = _connection(ssh_config)
    manager = OpenSSHSFTPManager(
        "127.0.0.1", "testuser", PORT, connection=conn, password=PASSWORD,
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
    )
    try:
        manager._connect_impl()
        home = manager._client.realpath(".")
        assert home == "/home/testuser"
        entries = manager._client.listdir_attr(home)
        assert entries is not None
    finally:
        try:
            manager.disconnect()
        except Exception:
            pass
