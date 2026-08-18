"""Peer-process identification is a platform invariant, not a nicety.

Startup self-healing and verified quit both rest on being able to name the
process behind a Unix socket. Where that fails, sshPilot must refuse to
signal and refuse to unlink — which is safe, but silently turns off the
self-healing this suite exists to guarantee.

So these tests do not skip on the production platforms. On Linux and macOS a
failure to identify a known peer is a test failure, because in the field it
would be a downgrade nobody would notice.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sshpilot.daemon.lifecycle import (
    peer_credentials_supported,
    peer_process_id,
    probe_socket_owner,
)

IS_PRODUCTION_PLATFORM = sys.platform in {"linux", "darwin"}


def test_production_platforms_can_identify_socket_peers():
    """If this fails, eviction silently stops working on this platform."""
    if not IS_PRODUCTION_PLATFORM:
        pytest.skip(f"{sys.platform} is not a supported production platform")
    assert peer_credentials_supported() is True


def test_peer_process_id_returns_the_real_pid_for_a_connected_socket():
    """Deliberately not skip-on-None: a None here is the bug we are hunting."""
    if not IS_PRODUCTION_PLATFORM:
        pytest.skip(f"{sys.platform} is not a supported production platform")

    left, right = socket.socketpair()
    try:
        pid = peer_process_id(left)
    finally:
        left.close()
        right.close()

    assert pid == os.getpid(), (
        f"peer identification is broken on {sys.platform}: "
        f"expected {os.getpid()}, got {pid!r}"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin-specific ABI")
def test_darwin_local_peerpid_reports_a_separate_process():
    """macOS ``SOL_LOCAL``/``LOCAL_PEERPID`` must name the *other* process.

    A socketpair cannot catch a Darwin ABI mistake, because both ends are this
    process and a wrong-but-plausible readout could still equal our own PID.
    This connects a real child to a real listener instead.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "peer.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(path))
            listener.listen(1)
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import socket,sys,time\n"
                    "s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                    "s.connect(sys.argv[1])\n"
                    "time.sleep(30)\n",
                    str(path),
                ]
            )
            try:
                listener.settimeout(10)
                peer, _address = listener.accept()
                try:
                    pid = peer_process_id(peer)
                finally:
                    peer.close()
                assert pid == child.pid, (
                    "LOCAL_PEERPID did not report the connecting process: "
                    f"expected {child.pid}, got {pid!r}"
                )
            finally:
                child.kill()
                child.wait(timeout=5)
        finally:
            listener.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin-specific ABI")
def test_darwin_socket_owner_probe_finds_a_squatter(tmp_path):
    """End-to-end on macOS: bind, discover, evict, verify, replace."""
    from sshpilot.daemon.lifecycle import evict_socket_owner

    socket_path = tmp_path / "runtime" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    squatter = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,socket,sys,time\n"
            "l = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "l.bind(sys.argv[1]); os.chmod(sys.argv[1], 0o600); l.listen(4)\n"
            "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
            "time.sleep(60)\n",
            str(socket_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert squatter.stdout is not None
        assert squatter.stdout.readline().strip() == "ready"

        accepting, pid = probe_socket_owner(socket_path)
        assert accepting is True
        assert pid == squatter.pid, "the squatter's PID must be discoverable"

        assert evict_socket_owner(socket_path) is True
        deadline = time.monotonic() + 5
        while squatter.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert squatter.poll() is not None, "the exact squatter must be dead"
        assert not socket_path.exists()
        assert probe_socket_owner(socket_path) == (False, None)
    finally:
        if squatter.poll() is None:
            squatter.kill()
        squatter.wait(timeout=5)
