"""The Windows (threaded) bounded read of a privileged file command."""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId
from sshpilot.daemon.privileged_file_service import PrivilegedFileService


def _service(timeout=10.0):
    service = PrivilegedFileService.__new__(PrivilegedFileService)
    service._command_timeout = timeout
    return service


def _child(code):
    return subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def test_reads_stdout_and_stderr_concurrently():
    # 1 MiB on stderr first would block a sequential reader.
    process = _child(
        "import sys; sys.stderr.write('e' * (1 << 20)); sys.stderr.flush(); sys.stdout.write('payload')"
    )
    out, err = _service()._read_bounded_threaded(process, 1 << 16, ConnectionId("c"), time.monotonic() + 10)
    assert out == b"payload" and len(err) == 1 << 20


def test_too_large_output_is_refused():
    process = _child("import sys; sys.stdout.write('x' * 100000)")
    with pytest.raises(SshPilotError) as raised:
        _service()._read_bounded_threaded(process, 1000, ConnectionId("c"), time.monotonic() + 10)
    assert raised.value.code is ErrorCode.FILE_CONTENT_TOO_LARGE


def test_deadline_is_enforced():
    process = _child("import time; time.sleep(30)")
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _service(timeout=1)._read_bounded_threaded(process, 1000, ConnectionId("c"), time.monotonic() + 1)
    assert time.monotonic() - started < 10
