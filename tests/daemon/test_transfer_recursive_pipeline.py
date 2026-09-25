"""Recursive small-file uploads pipeline control-plane RTTs.

Drives TransferRuntime against a real OpenSSH sftp-server with an artificial
delay so a serial per-file upload would take many seconds; the pipelined path
must finish in a handful of round-trip windows.
"""

from __future__ import annotations

import heapq
import os
import shutil
import subprocess
import threading
import time

import pytest

from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.transfers import (
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
)
from sshpilot.daemon.transfer_runtime import TransferRuntime
from sshpilot.sftp.client import OpenSSHSFTPClient
from tests.daemon.test_transfer_runtime import (
    _make_ready_sftp_service,
    _wait_for_terminal_state,
)

_SFTP_SERVER = next(
    (
        path
        for path in (
            "/usr/lib/openssh/sftp-server",
            "/usr/libexec/openssh/sftp-server",
            "/usr/lib/ssh/sftp-server",
            "/usr/libexec/sftp-server",
        )
        if os.access(path, os.X_OK)
    ),
    shutil.which("sftp-server"),
)

pytestmark = pytest.mark.skipif(_SFTP_SERVER is None, reason="OpenSSH sftp-server not installed")

_OWNER = ClientId("client:owner")


class _DelayLine:
    def __init__(self, source, delay: float) -> None:
        self._source = source
        self._delay = delay
        read_fd, write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "rb")
        self._writer = os.fdopen(write_fd, "wb", buffering=0)
        self._queue: list = []
        self._seq = 0
        self._cond = threading.Condition()
        self._eof = False
        threading.Thread(target=self._pump_in, daemon=True).start()
        threading.Thread(target=self._pump_out, daemon=True).start()

    def _pump_in(self) -> None:
        while True:
            data = self._source.read1(65536)
            with self._cond:
                if not data:
                    self._eof = True
                else:
                    self._seq += 1
                    heapq.heappush(
                        self._queue, (time.monotonic() + self._delay, self._seq, data)
                    )
                self._cond.notify()
            if not data:
                return

    def _pump_out(self) -> None:
        try:
            while True:
                with self._cond:
                    while not self._queue and not self._eof:
                        self._cond.wait()
                    if not self._queue:
                        break
                    due, _, data = self._queue[0]
                    wait = due - time.monotonic()
                    if wait > 0:
                        self._cond.wait(wait)
                        continue
                    heapq.heappop(self._queue)
                self._writer.write(data)
        except OSError:
            pass
        finally:
            self._writer.close()


def test_recursive_small_file_upload_pipelines_over_rtt(tmp_path):
    delay = 0.03
    local_root = tmp_path / "src"
    local_root.mkdir()
    sub = local_root / "sub"
    sub.mkdir()
    payloads = {}
    for index in range(40):
        path = sub / f"f{index:03d}.bin"
        data = os.urandom(256)
        path.write_bytes(data)
        payloads[path.name] = data
    remote_root = tmp_path / "dst"

    process = subprocess.Popen(
        [_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    delayed = _DelayLine(process.stdout, delay)
    client = OpenSSHSFTPClient(process.stdin, delayed.reader, on_close=process.terminate)
    client.start()
    try:
        sftp_runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
        runtime = TransferRuntime(sftp_runtime)
        started = time.monotonic()
        prepared = runtime.prepare_start_transfer(
            StartTransferRequest(
                connection_id=ConnectionId("demo"),
                sftp_service_id=service_id,
                direction=TransferDirection.UPLOAD,
                remote_path=str(remote_root),
                local_path=str(local_root),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
                recursive=True,
            ),
            client_id=_OWNER,
        )
        runtime.run_transfer(prepared.id)
        summary = _wait_for_terminal_state(runtime, prepared.id, timeout=30.0)
        elapsed = time.monotonic() - started
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()
        delayed.reader.close()

    assert summary.state is TransferState.COMPLETED, summary
    for name, data in payloads.items():
        assert (remote_root / "sub" / name).read_bytes() == data
    # Serial: 40 files × ~6 RTTs × 30 ms ≈ 7.2 s. Pipelined should finish in
    # a small multiple of one window (mkdir + 6 phases).
    round_trips = elapsed / delay
    assert round_trips < 50, f"recursive upload took {round_trips:.1f} round trips ({elapsed:.2f}s)"
