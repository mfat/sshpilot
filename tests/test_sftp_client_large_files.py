"""Files larger than one SFTP packet round-trip through ``OpenSSHSFTPFile``.

OpenSSH's sftp-server answers a READ with at most ~255 KiB and drops the
session on a message over 256 KiB, so a single-request read silently truncated
the remote editor's content and a single-request write killed the service.
Transfers also size their chunks from ``limits@openssh.com`` and keep several
in flight, so a save over a slow link costs a few round trips, not dozens.
Runs against the real sftp-server binary.
"""

from __future__ import annotations

import heapq
import os
import shutil
import subprocess
import threading
import time

import pytest

from sshpilot.sftp import protocol as proto
from sshpilot.sftp.client import OpenSSHSFTPClient

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

_SIZE = 900_000  # well past sftp-server's 256 KiB packet limit


class _DelayLine:
    """Delivers the server's replies ``delay`` seconds late, in order, so a
    request/reply costs one simulated round trip while pipelined requests
    still overlap."""

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
                    heapq.heappush(self._queue, (time.monotonic() + self._delay, self._seq, data))
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


def _start_client(delay: float = 0.0):
    process = subprocess.Popen(
        [_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    )
    stdout = process.stdout if not delay else _DelayLine(process.stdout, delay).reader
    sftp = OpenSSHSFTPClient(process.stdin, stdout, on_close=process.terminate)
    sftp.start()
    return sftp, process, stdout


def _stop_client(sftp, process, stdout) -> None:
    sftp.close()
    process.wait(timeout=5)
    process.stdout.close()
    if stdout is not process.stdout:
        stdout.close()


@pytest.fixture
def client():
    sftp, process, stdout = _start_client()
    try:
        yield sftp
    finally:
        _stop_client(sftp, process, stdout)


def test_sized_read_returns_the_whole_file(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    with client.file(str(path), "rb") as handle:
        assert handle.read(1024 * 1024 + 1) == content


def test_sized_read_stops_at_the_requested_size(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    with client.file(str(path), "rb") as handle:
        assert handle.read(300_000) == content[:300_000]
        assert handle.read(10) == content[300_000:300_010]


def test_write_larger_than_one_packet_lands_intact(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "out.bin"

    with client.file(str(path), "wb", create_mode=0o600) as handle:
        handle.write(content)

    assert path.read_bytes() == content


def test_read_to_eof_returns_the_whole_file(client, tmp_path):
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"
    path.write_bytes(content)

    with client.file(str(path), "rb") as handle:
        assert handle.read() == content
        assert handle.read() == b""


def test_chunk_sizes_come_from_the_server_limits(client):
    # OpenSSH advertises limits@openssh.com; a bigger chunk than the 32 KiB
    # fallback is what turns a 1 MiB save into a handful of requests.
    assert "limits@openssh.com" in client.extensions
    assert client.max_read_length > 32768
    assert client.max_write_length > 32768
    # Still inside the 256 KiB message sftp-server accepts.
    assert client.max_write_length <= 256 * 1024 - 1024


def test_default_chunks_still_round_trip(client, tmp_path):
    # Servers without limits@openssh.com get 32 KiB chunks, many in flight.
    client.max_read_length = client.max_write_length = 32768
    content = os.urandom(_SIZE)
    path = tmp_path / "big.bin"

    with client.file(str(path), "wb") as handle:
        handle.write(content)
    with client.file(str(path), "rb") as handle:
        assert handle.read(1024 * 1024 + 1) == content


def test_server_side_copy(client, tmp_path):
    content = os.urandom(_SIZE)
    source = tmp_path / "source.bin"
    source.write_bytes(content)
    destination = tmp_path / "copy.bin"

    assert client.supports_copy_data()
    with client.file(str(source), "rb") as src, client.file(str(destination), "wb") as dst:
        client.copy_data(src.handle, dst.handle)

    assert destination.read_bytes() == content


def test_write_error_surfaces_once_all_chunks_settle(client, tmp_path):
    path = tmp_path / "read-only.bin"
    path.write_bytes(b"")
    with client.file(str(path), "rb") as handle:
        with pytest.raises(proto.SFTPError):
            handle.write(os.urandom(_SIZE))
    # The session survives the failed pipeline.
    assert client.stat(str(path)).st_size == 0


def test_save_over_a_slow_link_takes_a_few_round_trips(tmp_path):
    """Read then rewrite 1 MiB, as an editor save does, at 200 ms per round
    trip. One request per 32 KiB chunk made this 64+ round trips (~13 s)."""
    delay = 0.2
    sftp, process, stdout = _start_client(delay)
    try:
        content = os.urandom(1024 * 1024)
        path = tmp_path / "big.bin"
        path.write_bytes(content)
        started = time.monotonic()
        with sftp.file(str(path), "rb") as handle:
            assert handle.read(1024 * 1024 + 1) == content
        with sftp.file(str(path), "wb") as handle:
            handle.write(content)
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    assert path.read_bytes() == content
    # open, pipelined read, EOF follow-up, close, open, pipelined write, close.
    assert round_trips < 12, f"save took {round_trips:.1f} round trips"
