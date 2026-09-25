"""Large files and directories through ``OpenSSHSFTPClient``.

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
from pathlib import Path

import pytest

from sshpilot.sftp import protocol as proto
from sshpilot.sftp.client import (
    OpenSSHSFTPClient,
    PipelinedWriter,
    _AdaptivePipeline,
    _Pending,
    _INITIAL_PIPELINE_DEPTH,
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


def test_listing_a_large_directory_returns_every_entry(client, tmp_path):
    names = {f"file-{index:05d}" for index in range(2500)}
    for name in names:
        (tmp_path / name).touch()
    (tmp_path / "sub").mkdir()

    listed = client.listdir_attr(str(tmp_path))

    assert sorted(entry.filename for entry in listed) == sorted(names | {"sub"})


def test_listing_an_empty_directory(client, tmp_path):
    assert client.listdir_attr(str(tmp_path)) == []
    # The session is still usable after the read-aheads past EOF.
    assert client.listdir_attr(str(tmp_path)) == []


def test_remove_many_pipelines_over_a_slow_link(tmp_path):
    """Mass file delete should pay about one RTT per pipeline window."""
    paths = []
    for index in range(200):
        path = tmp_path / f"file-{index:03d}.txt"
        path.write_text("x")
        paths.append(str(path))
    delay = 0.05
    sftp, process, stdout = _start_client(delay)
    try:
        started = time.monotonic()
        failures = sftp.remove_many(paths, continue_on_error=True)
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    assert failures == []
    assert all(not os.path.exists(path) for path in paths)
    # 200 removes / depth 100 ≈ 2 windows; leave headroom for handshake jitter.
    # Depth 16 would need ~12.5 windows and fail this bound on a slow link.
    assert round_trips < 5, f"remove_many took {round_trips:.1f} round trips"


def test_rmdir_many_removes_empty_dirs_and_reports_the_rest(client, tmp_path):
    empty = [tmp_path / f"empty-{index}" for index in range(3)]
    for path in empty:
        path.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "f").write_bytes(b"x")

    failures = client.rmdir_many(
        [str(empty[0]), str(full), str(tmp_path / "missing"), str(empty[1]), str(empty[2])],
        continue_on_error=True,
    )

    # Missing is idempotent; only the non-empty directory fails.
    assert [path for path, _ in failures] == [str(full)]
    assert all(not path.exists() for path in empty)
    assert full.exists()


def test_tree_delete_over_a_slow_link_costs_rtts_per_level(tmp_path):
    """A wide tree deletes in a few round trips per level, not per directory."""
    from sshpilot.daemon.sftp_runtime import SftpServiceRuntime

    root = tmp_path / "tree"
    root.mkdir()
    for index in range(40):
        child = root / f"d{index}"
        child.mkdir()
        (child / "f").write_bytes(b"x")
        (child / "sub").mkdir()
    delay = 0.05
    sftp, process, stdout = _start_client(delay)
    try:
        runtime = SftpServiceRuntime.__new__(SftpServiceRuntime)
        started = time.monotonic()
        runtime._remove_recursive(sftp, str(root))
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    assert not root.exists()
    # 81 directories took ~6 round trips each (~490) when walked one by one.
    assert round_trips < 40, f"tree delete took {round_trips:.1f} round trips"


def test_adaptive_pipeline_grows_on_high_rtt():
    """FileZilla's 500 ms target grows the window when one RTT is large."""
    pipeline = _AdaptivePipeline(32768)
    assert pipeline.max_pending == _INITIAL_PIPELINE_DEPTH
    pipeline.note_full(marker=32768 * 16)
    # Simulate a 100 ms RTT: ideal = 16 * 500 / 100 = 80.
    time.sleep(0.1)
    pipeline.on_catch_up(marker=32768 * 16)
    assert pipeline.max_pending > _INITIAL_PIPELINE_DEPTH
    assert pipeline.peak_pending == pipeline.max_pending


def test_adaptive_pipeline_respects_bytes_in_flight_cap():
    pipeline = _AdaptivePipeline(32768)
    # Many growth steps as if RTT were tiny; must stop at the hard cap.
    for marker in range(1, 64):
        pipeline.note_full(marker=marker)
        pipeline._probe_started = time.monotonic() - 0.001
        pipeline.on_catch_up(marker=marker)
    assert pipeline.max_pending == pipeline._hard_cap
    assert pipeline.peak_pending == pipeline._hard_cap


def test_adaptive_pipeline_never_shrinks_below_initial():
    pipeline = _AdaptivePipeline(32768)
    pipeline.max_pending = _INITIAL_PIPELINE_DEPTH
    pipeline.note_full(marker=1)
    # Two-second drain → ideal = 16 * 500 / 2000 = 4, so shrink toward floor.
    pipeline._probe_started = time.monotonic() - 2.0
    pipeline.on_catch_up(marker=1)
    assert pipeline.max_pending == _INITIAL_PIPELINE_DEPTH


def test_adaptive_read_grows_window_over_a_slow_link(tmp_path):
    """Multi-MiB download on a slow link must raise outstanding depth above 16.

    Fixed depth 16 with 32 KiB chunks needs one RTT per 512 KiB; adaptive growth
    toward a 500 ms window uses far fewer windows for the same payload.
    """
    delay = 0.05
    size = 2 * 1024 * 1024
    content = os.urandom(size)
    path = tmp_path / "big.bin"
    path.write_bytes(content)
    sftp, process, stdout = _start_client(delay)
    try:
        # Force small chunks so request count (not OpenSSH's ~255 KiB) dominates.
        sftp.max_read_length = 32768
        started = time.monotonic()
        with sftp.file(str(path), "rb") as handle:
            assert handle.read(size + 1) == content
        round_trips = (time.monotonic() - started) / delay
        peak = sftp.last_transfer_peak_pending
    finally:
        _stop_client(sftp, process, stdout)
    assert peak > _INITIAL_PIPELINE_DEPTH, f"peak pending stayed at {peak}"
    # 2 MiB / 32 KiB = 64 chunks. Fixed-16 needs ≥4 windows (~4 RTTs of data)
    # plus open/close; adaptive should finish the data phase in fewer windows.
    # Bound leaves room for handshake and EOF follow-up.
    assert round_trips < 10, f"adaptive read took {round_trips:.1f} round trips"


def test_adaptive_write_grows_window_over_a_slow_link(tmp_path):
    delay = 0.05
    size = 2 * 1024 * 1024
    content = os.urandom(size)
    path = tmp_path / "out.bin"
    sftp, process, stdout = _start_client(delay)
    try:
        sftp.max_write_length = 32768
        started = time.monotonic()
        with sftp.file(str(path), "wb") as handle:
            handle.write(content)
        round_trips = (time.monotonic() - started) / delay
        peak = sftp.last_transfer_peak_pending
    finally:
        _stop_client(sftp, process, stdout)
    assert path.read_bytes() == content
    assert peak > _INITIAL_PIPELINE_DEPTH, f"peak pending stayed at {peak}"
    assert round_trips < 10, f"adaptive write took {round_trips:.1f} round trips"


def test_pipelined_writer_drains_down_to_a_shrunk_window():
    """A shrunk window must lower writes in flight, not keep the old depth."""

    class _AckingClient:
        max_write_length = 10

        def _send_write(self, handle, offset, data):
            slot = _Pending()
            slot.response = (proto.FXP_STATUS, b"")
            slot.event.set()
            return slot

        @staticmethod
        def _wait(slot):
            return slot.response

        @staticmethod
        def _expect_ok(resp):
            pass

    writer = PipelinedWriter(_AckingClient(), b"h")
    writer._pipeline.max_pending = 200
    writer.write(b"x" * 10 * 199)
    assert len(writer._inflight) == 199
    writer._pipeline.max_pending = _INITIAL_PIPELINE_DEPTH
    writer.write(b"x" * 10)
    assert len(writer._inflight) < _INITIAL_PIPELINE_DEPTH


def test_tiny_read_does_not_grow_the_pipeline(client, tmp_path):
    """A short file never fills the initial window, so depth stays at 16."""
    path = tmp_path / "tiny.bin"
    path.write_bytes(b"hello")
    with client.file(str(path), "rb") as handle:
        assert handle.read() == b"hello"
    assert client.last_transfer_peak_pending == _INITIAL_PIPELINE_DEPTH


def test_listing_a_large_directory_over_a_slow_link(tmp_path):
    """2500 entries are ~25 READDIR batches from OpenSSH; one request at a
    time made that ~27 round trips."""
    for index in range(2500):
        (tmp_path / f"file-{index:05d}").touch()
    delay = 0.1
    sftp, process, stdout = _start_client(delay)
    try:
        started = time.monotonic()
        listed = sftp.listdir_attr(str(tmp_path))
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    assert len(listed) == 2500
    # OPENDIR, ceil(26 / 8) READDIR windows, CLOSE.
    assert round_trips < 10, f"listing took {round_trips:.1f} round trips"


def test_atomic_upload_many_hides_rtt_for_small_files(tmp_path):
    """Fifty tiny files must share control-plane RTTs, not pay six each.

    Serial open/write/fsetstat/close/rename is ≈6 RTTs/file (≈300 RTTs here).
    Pipelined phases collapse that to roughly one window per phase.
    """
    from sshpilot.sftp.client import AtomicUploadItem

    delay = 0.05
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    items = []
    for index in range(50):
        local = tmp_path / f"src-{index:03d}.bin"
        local.write_bytes(os.urandom(512))
        info = local.stat()
        items.append(
            AtomicUploadItem(
                local_path=str(local),
                remote_temp=str(remote_dir / f".tmp-{index:03d}"),
                remote_dst=str(remote_dir / f"dst-{index:03d}.bin"),
                create_mode=0o600,
                existing_mode=None,
                atime=int(info.st_atime),
                mtime=int(info.st_mtime),
            )
        )
    sftp, process, stdout = _start_client(delay)
    try:
        started = time.monotonic()
        sftp.atomic_upload_many(items)
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    for item in items:
        assert Path(item.remote_dst).read_bytes() == Path(item.local_path).read_bytes()
        assert not Path(item.remote_temp).exists()
    # Serial would be ~300 RTTs. Allow generous headroom for five phases +
    # window draining on a 50-file batch.
    assert round_trips < 40, f"pipelined upload took {round_trips:.1f} round trips"


def test_stat_many_pipelines_missing_and_present(tmp_path):
    present = tmp_path / "here.bin"
    present.write_bytes(b"x")
    missing = tmp_path / "gone.bin"
    sftp, process, stdout = _start_client(0.0)
    try:
        attrs = sftp.stat_many([str(present), str(missing), str(present)])
    finally:
        _stop_client(sftp, process, stdout)
    assert attrs[0] is not None and attrs[0].st_size == 1
    assert attrs[1] is None
    assert attrs[2] is not None


def _upload_items(tmp_path, remote_dir, count, *, size=16, prefix="f"):
    from sshpilot.sftp.client import AtomicUploadItem

    local_dir = tmp_path / f"local-{prefix}"
    local_dir.mkdir(exist_ok=True)
    items = []
    for index in range(count):
        local = local_dir / f"{prefix}{index:05d}"
        local.write_bytes(os.urandom(size))
        items.append(
            AtomicUploadItem(
                local_path=str(local),
                remote_temp=str(remote_dir / f".tmp-{prefix}{index:05d}"),
                remote_dst=str(remote_dir / f"{prefix}{index:05d}"),
                create_mode=0o644,
                existing_mode=None,
                atime=0,
                mtime=0,
            )
        )
    return items


def test_atomic_upload_many_stays_within_the_servers_handle_limit(tmp_path):
    """Every item used to be opened before any was closed, so a tree larger
    than the server's RLIMIT_NOFILE failed outright."""
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    items = _upload_items(tmp_path, remote_dir, 1500)
    process = subprocess.Popen(
        ["/bin/sh", "-c", f'ulimit -n 256 && exec "{_SFTP_SERVER}"'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    sftp = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    sftp.start()
    try:
        sftp.atomic_upload_many(items)
    finally:
        _stop_client(sftp, process, process.stdout)
    assert sorted(os.listdir(remote_dir)) == [Path(i.remote_dst).name for i in items]


def test_atomic_upload_many_failed_open_leaves_no_temps(client, tmp_path):
    from sshpilot.sftp.client import AtomicUploadItem

    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    items = _upload_items(tmp_path, remote_dir, 49)
    local = items[0].local_path
    items.insert(
        0,
        AtomicUploadItem(
            local_path=local,
            remote_temp=str(remote_dir / "missing" / ".tmp-x"),
            remote_dst=str(remote_dir / "missing" / "x"),
            create_mode=0o644,
            existing_mode=None,
            atime=0,
            mtime=0,
        ),
    )
    with pytest.raises(proto.SFTPError):
        client.atomic_upload_many(items)
    assert os.listdir(remote_dir) == []


def test_atomic_upload_many_replaces_files_without_posix_rename(client, tmp_path):
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    items = _upload_items(tmp_path, remote_dir, 30)
    for item in items[::2]:
        Path(item.remote_dst).write_bytes(b"old")
    client.extensions.pop("posix-rename@openssh.com", None)
    assert not client.supports_posix_rename()
    client.atomic_upload_many(items)
    for item in items:
        assert Path(item.remote_dst).read_bytes() == Path(item.local_path).read_bytes()
    assert sorted(os.listdir(remote_dir)) == [Path(i.remote_dst).name for i in items]


def test_atomic_upload_many_writes_a_file_that_outgrew_one_chunk(client, tmp_path):
    remote_dir = tmp_path / "remote"
    remote_dir.mkdir()
    items = _upload_items(tmp_path, remote_dir, 3, size=client.max_write_length * 3 + 7)
    client.atomic_upload_many(items)
    for item in items:
        assert Path(item.remote_dst).read_bytes() == Path(item.local_path).read_bytes()


def test_stat_many_missing_on_error_tolerates_non_enoent_failures(client, tmp_path):
    present = tmp_path / "here.bin"
    present.write_bytes(b"x")
    # ENAMETOOLONG comes back as FX_FAILURE, not FX_NO_SUCH_FILE.
    odd = str(tmp_path / ("n" * 300))
    with pytest.raises(proto.SFTPError):
        client.stat_many([odd])
    attrs = client.stat_many([odd, str(present)], missing_on_error=True)
    assert attrs[0] is None
    assert attrs[1] is not None


def test_listdir_many_matches_listdir_attr(client, tmp_path):
    dirs = []
    for index in range(5):
        path = tmp_path / f"d{index}"
        path.mkdir()
        for child in range(index * 90):  # up to 360 names: several READDIRs
            (path / f"c{child}").write_bytes(b"")
        dirs.append(str(path))
    listings = client.listdir_many(dirs)
    for path, listing in zip(dirs, listings):
        expected = sorted(a.filename for a in client.listdir_attr(path))
        assert sorted(a.filename for a in listing) == expected
    with pytest.raises(proto.SFTPError):
        client.listdir_many([dirs[0], str(tmp_path / "missing")])
    # Nothing leaked: a later listing still works.
    assert len(client.listdir_many(dirs[1:2])[0]) == 90


def test_listdir_many_over_a_slow_link_lists_a_level_together(tmp_path):
    delay = 0.05
    dirs = []
    for index in range(40):
        path = tmp_path / f"d{index}"
        path.mkdir()
        (path / "f").write_bytes(b"x")
        dirs.append(str(path))
    sftp, process, stdout = _start_client(delay)
    try:
        started = time.monotonic()
        listings = sftp.listdir_many(dirs)
        round_trips = (time.monotonic() - started) / delay
    finally:
        _stop_client(sftp, process, stdout)
    assert all([a.filename for a in listing] == ["f"] for listing in listings)
    # OPENDIR, one READDIR round (names + EOF), CLOSE — not 3 per directory.
    assert round_trips < 8, f"listing took {round_trips:.1f} round trips"


def test_mkdir_many_reports_each_failure(client, tmp_path):
    (tmp_path / "exists").mkdir()
    errors = client.mkdir_many(
        [str(tmp_path / "a"), str(tmp_path / "exists"), str(tmp_path / "b")]
    )
    assert errors[0] is None and errors[2] is None
    assert isinstance(errors[1], proto.SFTPError)
    assert (tmp_path / "a").is_dir() and (tmp_path / "b").is_dir()


def test_read_small_files_handles_size_drift(client, tmp_path):
    exact = tmp_path / "exact"
    exact.write_bytes(os.urandom(500))
    grew = tmp_path / "grew"
    grew.write_bytes(os.urandom(client.max_read_length * 2 + 3))
    shrank = tmp_path / "shrank"
    shrank.write_bytes(b"abc")
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    items = [(str(exact), 500), (str(grew), 10), (str(shrank), 400), (str(empty), 0)]
    contents = []
    for start, window in client.read_small_files(items):
        assert start == len(contents)
        contents.extend(window)
    assert contents == [p.read_bytes() for p in (exact, grew, shrank, empty)]
    with pytest.raises(proto.SFTPError):
        list(client.read_small_files([(str(exact), 500), (str(tmp_path / "gone"), 1)]))


def test_read_small_files_stays_within_the_servers_handle_limit(tmp_path):
    paths = []
    for index in range(1500):
        path = tmp_path / f"f{index:05d}"
        path.write_bytes(str(index).encode())
        paths.append(path)
    process = subprocess.Popen(
        ["/bin/sh", "-c", f'ulimit -n 256 && exec "{_SFTP_SERVER}"'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    sftp = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    sftp.start()
    try:
        contents = []
        for _start, window in sftp.read_small_files([(str(p), p.stat().st_size) for p in paths]):
            contents.extend(window)
    finally:
        _stop_client(sftp, process, process.stdout)
    assert contents == [p.read_bytes() for p in paths]
