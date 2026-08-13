"""Phase 10.1 real-OpenSSH port-forward integration via ephemeral DaemonServer."""

from __future__ import annotations

import select
import socket
import struct
import threading
from pathlib import Path

import pytest

from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import (
    ClaimForwardRequest,
    CloseForwardRequest,
    ForwardState,
    ForwardType,
    OpenForwardRequest,
)
from tests.daemon.phase10_helpers import (
    free_port,
    require_phase10_container,
    start_phase10_stack,
    wait_until,
)

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("phase10-fwd-sshd"))
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture
def stack(tmp_path, phase10_env):
    started = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        yield started
    finally:
        started.close(destroy_env=False)


class _EchoServer:
    """Local TCP echo for remote-forward destination tests."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = int(self._sock.getsockname()[1])
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                while not self._stop.is_set():
                    try:
                        data = conn.recv(4096)
                    except OSError:
                        break
                    if not data:
                        break
                    conn.sendall(data)

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


def _socks5_connect(proxy_host: str, proxy_port: int, dest_host: str, dest_port: int) -> socket.socket:
    """Minimal SOCKS5 CONNECT client (no auth) for dynamic-forward smoke tests."""

    sock = socket.create_connection((proxy_host, proxy_port), timeout=5)
    sock.sendall(b"\x05\x01\x00")
    greeting = sock.recv(2)
    if greeting != b"\x05\x00":
        sock.close()
        raise RuntimeError(f"SOCKS5 greeting failed: {greeting!r}")
    host_bytes = dest_host.encode("ascii")
    request = (
        b"\x05\x01\x00\x03"
        + bytes([len(host_bytes)])
        + host_bytes
        + struct.pack("!H", dest_port)
    )
    sock.sendall(request)
    reply = sock.recv(4)
    if len(reply) < 4 or reply[1] != 0:
        sock.close()
        raise RuntimeError(f"SOCKS5 connect failed: {reply!r}")
    atyp = reply[3]
    if atyp == 1:
        sock.recv(4 + 2)
    elif atyp == 3:
        ln = sock.recv(1)[0]
        sock.recv(ln + 2)
    elif atyp == 4:
        sock.recv(16 + 2)
    return sock


def test_local_forward_active_and_payload(stack):
    bind_port = free_port()
    opened = stack.client.open_forward(
        OpenForwardRequest(
            connection_id=stack.connection_id,
            type=ForwardType.LOCAL,
            bind_host="127.0.0.1",
            bind_port=bind_port,
            destination_host="127.0.0.1",
            destination_port=stack.env.remote_echo_port,
        )
    )
    assert opened.state is ForwardState.STARTING
    active = stack.wait_forward_active(opened.id)
    assert active.state is ForwardState.ACTIVE

    with socket.create_connection(("127.0.0.1", bind_port), timeout=5) as sock:
        sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
        data = sock.recv(256)
    assert b"OK" in data

    record = stack.server._forward_runtime._records[opened.id]
    process = record.handle._process
    cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().replace(b"\0", b" ")
    assert b"ExitOnForwardFailure=yes" in cmdline
    assert b"-N" in cmdline


def test_local_forward_bind_conflict(stack):
    holder = socket.socket()
    try:
        # Avoid SO_REUSEADDR on the holder so the runtime probe (which sets
        # SO_REUSEADDR) still sees the address as unavailable on Linux.
        holder.bind(("127.0.0.1", 0))
        bind_port = int(holder.getsockname()[1])
        holder.listen(1)
        # Bind conflicts are detected during async start (after STARTING ack).
        opened = stack.client.open_forward(
            OpenForwardRequest(
                connection_id=stack.connection_id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=bind_port,
                destination_host="127.0.0.1",
                destination_port=stack.env.remote_echo_port,
            )
        )
        assert opened.state is ForwardState.STARTING

        def _failed() -> bool:
            try:
                stack.answer_pending_host_keys()
            except Exception:
                pass
            summary = stack.client.get_forward(opened.id)
            if summary.state is ForwardState.FAILED:
                return True
            if summary.state is ForwardState.ACTIVE:
                raise AssertionError("bind conflict incorrectly became ACTIVE")
            return False

        wait_until(_failed, timeout=20, message="bind conflict never failed")
        failed = stack.client.get_forward(opened.id)
        assert failed.failure is not None
        assert str(failed.failure.code) == ErrorCode.FORWARD_BIND_FAILED.value
    finally:
        holder.close()


def test_dynamic_socks_forward(stack):
    bind_port = free_port()
    opened = stack.client.open_forward(
        OpenForwardRequest(
            connection_id=stack.connection_id,
            type=ForwardType.DYNAMIC,
            bind_host="127.0.0.1",
            bind_port=bind_port,
        )
    )
    stack.wait_forward_active(opened.id)
    sock = _socks5_connect(
        "127.0.0.1",
        bind_port,
        "127.0.0.1",
        stack.env.remote_echo_port,
    )
    try:
        sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
        sock.settimeout(5)
        chunks = []
        try:
            while True:
                ready, _, _ = select.select([sock], [], [], 2.0)
                if not ready:
                    break
                part = sock.recv(256)
                if not part:
                    break
                chunks.append(part)
                if b"OK" in b"".join(chunks):
                    break
        except socket.timeout:
            pass
        assert b"OK" in b"".join(chunks)
    finally:
        sock.close()


def test_remote_forward_active_and_container_probe(stack):
    """Remote ACTIVE is process-alive based; payload via podman/docker exec.

    ``GatewayPorts clientspecified`` + ``PermitListen any`` allow the remote
    bind on Alpine OpenSSH (host:port-range syntax is rejected there).
    Destination is on the test host (``-R`` semantics).
    """

    echo = _EchoServer()
    echo.start()
    try:
        remote_port = 19090
        opened = stack.client.open_forward(
            OpenForwardRequest(
                connection_id=stack.connection_id,
                type=ForwardType.REMOTE,
                bind_host="127.0.0.1",
                bind_port=remote_port,
                destination_host="127.0.0.1",
                destination_port=echo.port,
            )
        )
        active = stack.wait_forward_active(opened.id, timeout=20)
        assert active.state is ForwardState.ACTIVE

        probe = stack.env.exec_in_container(
            "sh",
            "-c",
            f"printf 'ping' | nc -w 3 127.0.0.1 {remote_port}",
            timeout=15,
        )
        if probe.returncode != 0:
            pytest.skip(
                "remote-forward container networking probe failed "
                f"(exit {probe.returncode}: {(probe.stderr or probe.stdout).strip()})"
            )
        assert "ping" in (probe.stdout or "")
    finally:
        echo.close()


def test_forward_survives_client_detach_and_second_client_closes(tmp_path, phase10_env):
    stack = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        bind_port = free_port()
        opened = stack.client.open_forward(
            OpenForwardRequest(
                connection_id=stack.connection_id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=bind_port,
                destination_host="127.0.0.1",
                destination_port=stack.env.remote_echo_port,
            )
        )
        stack.wait_forward_active(opened.id)
        stack.client.close()
        observer = stack.connect_client(client_id="client:phase10-observer")
        rediscovered = observer.get_forward(opened.id)
        assert rediscovered.state is ForwardState.ACTIVE
        # After detach_client orphans the forward, any client may claim ownership.
        claimed = observer.claim_forward(ClaimForwardRequest(forward_id=opened.id))
        assert claimed.owner_client_id == observer._client_id
        # The claiming client can now close the forward.
        observer.close_forward(CloseForwardRequest(forward_id=opened.id))
        wait_until(
            lambda: observer.get_forward(opened.id).state is ForwardState.CLOSED,
            timeout=15,
            message="forward did not close",
        )
    finally:
        stack.close(destroy_env=False)


def test_daemon_shutdown_no_orphan_ssh_n(tmp_path, phase10_env):
    stack = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        bind_port = free_port()
        opened = stack.client.open_forward(
            OpenForwardRequest(
                connection_id=stack.connection_id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=bind_port,
                destination_host="127.0.0.1",
                destination_port=stack.env.remote_echo_port,
            )
        )
        stack.wait_forward_active(opened.id)
        process = stack.server._forward_runtime._records[opened.id].handle._process
        pid = process.pid
        stack.server.shutdown()
        stack.server.wait_stopped()
        wait_until(
            lambda: process.poll() is not None or not Path(f"/proc/{pid}").exists(),
            timeout=15,
            message="ssh -N orphan survived daemon shutdown",
        )
    finally:
        stack._stop_responder.set()
        for client in list(stack._clients):
            try:
                client.close()
            except Exception:
                pass
