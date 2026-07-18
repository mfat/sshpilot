"""Integration: master-first file-manager auth against a REAL 2FA SSH server.

Drives the actual stack end to end — real ssh, real PTY, real ControlMaster
socket, real SFTP worker — against the mock 2FA sshd from
``tests/integration/interactive_ssh_server`` (password "password" + PAM
verification code "123456" over keyboard-interactive):

1. ``MasterSession`` spawns ``ssh -N`` with askpass env (``REQUIRE=prefer``);
   a throwaway askpass script answers the login password then the OTP;
2. the master socket goes live and the PTY-less ``OpenSSHSFTPManager`` worker
   connects through it — no authentication of its own — and lists a directory.

Start the server first:  docker compose -f
tests/integration/interactive_ssh_server/docker-compose.yml up -d --build
Run with real PyGObject:  SSHPILOT_GUI_TESTS=1 pytest tests/integration/test_interactive_auth_e2e.py
Skips when nothing listens on 127.0.0.1:2225 or when gi is the test stub.
"""

import os
import socket
import subprocess
import sys
import threading
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
        resolved_identity_files=[],
        _resolve_config_override_path=lambda: ssh_config,
    )


def _stop_master(ssh_config):
    from sshpilot.ssh_multiplex import control_path

    subprocess.run(
        ["ssh", "-F", ssh_config, "-O", "exit",
         "-o", f"ControlPath={control_path()}", HOST_ALIAS],
        capture_output=True, timeout=10, check=False)


@_needs_server
@_needs_gi
def test_master_first_2fa_end_to_end(monkeypatch, ssh_config, askpass_script):
    import sshpilot.ssh_master_session as mss

    monkeypatch.setattr(mss, "_CHECK_INTERVAL", 0.2)
    # Force the same askpass script MasterSession would get from the resolver
    # (session password staging) without going through the GTK helper.
    monkeypatch.setattr(
        mss,
        "_askpass_env_for_connection",
        lambda *a, **k: {
            "SSH_ASKPASS": askpass_script,
            "SSH_ASKPASS_REQUIRE": "prefer",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        },
    )

    conn = _connection(ssh_config)
    ready = threading.Event()
    failed = threading.Event()
    state = {}

    session = mss.MasterSession(
        conn,
        None,
        None,
        on_ready=ready.set,
        on_failed=lambda tail: (state.update(tail=tail), failed.set()),
    )
    try:
        # Clear resolver askpass so MasterSession's force-prefer path installs
        # our script (build_ssh_connection still runs for the real argv).
        real_build = mss.build_ssh_connection

        def _build(ctx):
            prepared = real_build(ctx)
            env = dict(prepared.env or {})
            env.pop("SSH_ASKPASS", None)
            env.pop("SSH_ASKPASS_REQUIRE", None)
            return types.SimpleNamespace(
                command=prepared.command,
                env=env,
                use_sshpass=False,
                password=PASSWORD,
                use_askpass=False,
            )

        monkeypatch.setattr(mss, "build_ssh_connection", _build)
        session.start()

        assert ready.wait(60), (
            f"master never ready; failed={failed.is_set()} state={state}"
        )
        assert not failed.is_set(), f"master failed: {state}"
        assert mss.check_master_alive(conn), "-O check should pass after auth"

        # PTY-less SFTP worker rides the socket with zero authentication.
        import gi  # noqa: F401 — real gi on this box; the worker only needs GObject

        from sshpilot.file_manager.openssh_backend import OpenSSHSFTPManager

        manager = OpenSSHSFTPManager(
            "127.0.0.1", "testuser", PORT, connection=conn,
            dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
        )
        try:
            manager._connect_impl()  # synchronous; raises on any failure
            home = manager._client.realpath(".")
            assert home == "/home/testuser"
            entries = manager._client.listdir_attr(home)
            assert entries is not None  # listing succeeded (may be empty attrs)
        finally:
            try:
                manager.disconnect()
            except Exception:
                pass
    finally:
        session.cancel()
        _stop_master(ssh_config)
