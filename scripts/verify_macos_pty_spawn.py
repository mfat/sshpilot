#!/usr/bin/env python3
"""Packaged PTY spawn smoke test for the macOS app bundle (GH #1166).

Companion to verify_macos_daemon_bundle.py, which only proves the daemon
handshake works. That check is not enough: on the frozen build,
sys.executable is the app binary itself, not a Python interpreter — a
regression in how PtySessionProcessRunner spawns the PTY child (or how the
askpass helper scripts invoke it) makes the OS silently relaunch the GUI
instead of ever running the target command. The daemon handshake still
succeeds fine in that case; only session PTY spawning breaks. That's exactly
what shipped as 5.8.3: the release gate's existing smoke test passed while
the feature it's supposed to protect was broken for every user.

This starts the real bundled daemon (SSHPilot --daemon), opens a real
session, and asserts a real exec'd process's output arrives over the PTY —
using a fake `ssh` shadowing the real one on PATH as a stand-in remote host,
so this needs no network access, credentials, or real SSH server.

Run manually (workflow_dispatch) rather than on every release tag: this
exercises more of the real session/PTY machinery than the handshake check
and is correspondingly slower and newer, less proven code.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sshpilot.api.daemon_client import DaemonClient  # noqa: E402
from sshpilot.api.errors import SshPilotError  # noqa: E402
from sshpilot.api.models.connections import CreateConnectionRequest  # noqa: E402
from sshpilot.api.models.sessions import (  # noqa: E402
    AttachSessionRequest,
    CloseSessionRequest,
    OpenSessionRequest,
)
from sshpilot.api.models.terminal import TerminalDimensions  # noqa: E402

_PTY_MARKER = b"SSHPILOT_PTY_OK"


def _bundle_executable(bundle: Path) -> Path:
    executable = bundle / "Contents" / "MacOS" / "SSHPilot"
    if not executable.is_file():
        raise SystemExit(f"bundle executable not found: {executable}")
    return executable


def _write_fake_ssh(bin_dir: Path) -> None:
    """A stand-in remote host: ignores all argv, prints one marker.

    What matters here isn't ssh's argv contract, only that *some* real
    process gets exec'd by the daemon's PTY child helper and its output
    reaches this script over the real socket protocol. It lingers briefly
    after printing rather than exiting immediately: attach_session is a
    separate round-trip after open_session returns, and a command that
    exits in well under a millisecond can finish (and have its session
    record evicted) before this script ever attaches — a race real ssh
    sessions don't have, since a real connection always takes measurably
    longer than one client round-trip.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    fake_ssh = bin_dir / "ssh"
    fake_ssh.write_text(f'#!/bin/sh\nprintf {_PTY_MARKER.decode()}\nsleep 5\n')
    mode = fake_ssh.stat().st_mode
    fake_ssh.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _connect_with_retry(socket_path: Path, process: subprocess.Popen, log_path: Path, deadline: float) -> DaemonClient:
    """Fast-poll handshake probe. Deliberately short-lived and short-timeout
    — never reused for the real requests afterwards, see main()."""
    client = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                "bundled daemon exited before handshake "
                f"(returncode={process.returncode})\n{log_path.read_text()}"
            )
        try:
            client = DaemonClient(
                socket_path=socket_path,
                timeout=2.0,
                connect_timeout=0.25,
                client_name="macos-pty-smoke-probe",
                frontend_type="smoke",
            )
            client.get_capabilities()
            return client
        except SshPilotError:
            if client is not None:
                client.close()
                client = None
            time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for bundled daemon handshake\n{log_path.read_text()}")


def _verify_pty_spawn(client: DaemonClient) -> None:
    connection = client.create_connection(
        CreateConnectionRequest(
            nickname="sshpilot-pty-smoke",
            hostname="127.0.0.1",
            username="smoke",
            port=22,
        )
    )
    session = client.open_session(
        OpenSessionRequest(
            connection_id=connection.connection_id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        )
    )
    try:
        output = bytearray()
        client.attach_session(
            AttachSessionRequest(
                session_id=session.id,
                request_input=False,
                want_terminal_output=True,
                from_sequence=0,
            )
        )
        client.subscribe_terminal(session.id, output.extend)

        deadline = time.monotonic() + 20.0
        while _PTY_MARKER not in output and time.monotonic() < deadline:
            time.sleep(0.1)
        if _PTY_MARKER not in output:
            raise RuntimeError(
                "bundled daemon never delivered PTY output for a session — "
                "the frozen build's PTY spawn is likely broken again (GH #1166); "
                f"captured={bytes(output)!r}"
            )
    finally:
        client.close_session(CloseSessionRequest(session_id=session.id))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()

    bundle = args.bundle.resolve()
    executable = _bundle_executable(bundle)

    with tempfile.TemporaryDirectory(prefix="sshpilot-pty-smoke-") as temp_dir:
        temp = Path(temp_dir)
        socket_path = temp / "runtime" / "sshpilotd.sock"
        for name in (
            "HOME",
            "XDG_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
        ):
            path = temp / name.lower()
            path.mkdir(parents=True, exist_ok=True)
            if name == "XDG_RUNTIME_DIR":
                path.chmod(0o700)

        fake_bin_dir = temp / "fakebin"
        _write_fake_ssh(fake_bin_dir)

        environment = dict(os.environ)
        environment.update(
            {
                "HOME": str(temp / "home"),
                "XDG_RUNTIME_DIR": str(temp / "xdg_runtime_dir"),
                "XDG_CONFIG_HOME": str(temp / "xdg_config_home"),
                "XDG_DATA_HOME": str(temp / "xdg_data_home"),
                "XDG_STATE_HOME": str(temp / "xdg_state_home"),
                "XDG_CACHE_HOME": str(temp / "xdg_cache_home"),
                "SSHPILOT_PACKAGED": "1",
                # Shadow the real ssh so this needs no network access or
                # real credentials — see _write_fake_ssh.
                "PATH": f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            }
        )
        log_path = temp / "daemon.log"
        with log_path.open("w+") as log_file:
            process = subprocess.Popen(
                [str(executable), "--daemon", "--socket", str(socket_path)],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
            client = None
            try:
                probe = _connect_with_retry(
                    socket_path, process, log_path, time.monotonic() + 15.0
                )
                probe.close()
                # Reconnect with the default request timeout for the actual
                # functional check: session open/attach/close can take
                # longer than the fast handshake probe once the daemon is
                # doing real work (keyring backend discovery, connection
                # creation). Reusing the probe's short timeout here is
                # exactly the bug class this codebase already hit once — see
                # DaemonLauncher's probe_timeout vs. request timeout split.
                client = DaemonClient(
                    socket_path=socket_path,
                    client_name="macos-pty-smoke",
                    frontend_type="smoke",
                )
                _verify_pty_spawn(client)
                return 0
            except Exception:
                log_file.flush()
                print(log_path.read_text(), file=sys.stderr)
                raise
            finally:
                if client is not None:
                    client.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5.0)


if __name__ == "__main__":
    raise SystemExit(main())
