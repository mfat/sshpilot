"""Integration: master-first file-manager auth against a REAL 2FA SSH server.

Drives the actual stack end to end — real ssh, real PTY, real ControlMaster
socket, real SFTP worker — against the mock 2FA sshd from
``tests/integration/interactive_ssh_server`` (password "password" + PAM
verification code "123456" over keyboard-interactive):

1. ``MasterSession`` spawns ``ssh -N`` on an app-owned PTY, auto-answers the
   stored password, and hands the PTY over when the "Verification code:"
   prompt appears (what the AuthTerminalDialog receives in the app);
2. the test plays the user, writing the code to the handed-over fd;
3. the master socket goes live and the PTY-less ``OpenSSHSFTPManager`` worker
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
def test_master_first_2fa_end_to_end(monkeypatch, ssh_config):
    import sshpilot.ssh_master_session as mss

    monkeypatch.setattr(mss, "_get_stored_password", lambda *a: PASSWORD)
    monkeypatch.setattr(mss, "_CHECK_INTERVAL", 0.2)

    conn = _connection(ssh_config)
    ready = threading.Event()
    failed = threading.Event()
    interaction = threading.Event()
    state = {}

    session = mss.MasterSession(
        conn,
        None,
        None,
        on_ready=ready.set,
        on_needs_interaction=lambda fd, transcript: (
            state.update(fd=fd, transcript=transcript), interaction.set()),
        on_need_password=lambda prompt: state.update(pw_prompt=prompt),
        on_failed=lambda tail: (state.update(tail=tail), failed.set()),
    )
    try:
        session.start()

        # The stored password is auto-answered invisibly; the next prompt is
        # the verification code, which must be escalated with the PTY fd.
        assert interaction.wait(30), (
            f"no interaction callback; failed={failed.is_set()} state={state}")
        assert "verification code" in state["transcript"].lower()
        assert not failed.is_set()

        # Play the user typing the code into the revealed terminal.
        os.write(state["fd"], (CODE + "\n").encode())

        assert ready.wait(30), f"master never ready; state={state}"
        assert mss.check_master_alive(conn), "-O check should pass after auth"

        # PTY-less SFTP worker rides the socket with zero authentication.
        import gi  # real gi on this box; the worker only needs GObject

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
            manager.close()
    finally:
        session.cancel()
        os.close(state["fd"]) if "fd" in state else None
        _stop_master(ssh_config)


@_needs_server
@_needs_gi
def test_worker_without_master_times_out_fast(monkeypatch, ssh_config):
    """The old failure mode, now bounded: a PTY-less worker straight at the 2FA
    host (no master, no stored creds usable for kbd-interactive) fails within
    the connect timeout instead of hanging until LoginGraceTime."""
    import gi

    from sshpilot.file_manager.openssh_backend import OpenSSHSFTPManager

    _stop_master(ssh_config)  # ensure no socket to ride
    conn = _connection(ssh_config)
    manager = OpenSSHSFTPManager(
        "127.0.0.1", "testuser", PORT, connection=conn,
        ssh_config={"file_manager": {"sftp_connect_timeout": 8}},
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
    )
    try:
        import time

        start = time.monotonic()
        with pytest.raises(Exception) as excinfo:
            manager._connect_impl()
        elapsed = time.monotonic() - start
        # The point of the watchdog: bound the wait. Before the fix this hung
        # until the server's LoginGraceTime (~120s); now it must fail well
        # inside that. (The exact message depends on whether a desktop askpass
        # is installed — ssh's compiled-in default fires regardless of
        # SSH_ASKPASS — so we assert on timing + that it raised, not on text.)
        assert elapsed < 60, f"took {elapsed:.0f}s — watchdog did not bound the wait"
        assert str(excinfo.value), "failure must carry a message"
    finally:
        manager.close()
