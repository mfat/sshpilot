"""The native command runner without selectors (Windows pipes)."""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from sshpilot.daemon import broadcast_service
from sshpilot.daemon.broadcast_service import NativeSshCommandRunner


@pytest.fixture
def threaded(monkeypatch):
    # The threaded path is the Windows one; force it so POSIX runs cover it too.
    monkeypatch.setattr(broadcast_service, "_pipes_need_threads", lambda: True)


def _policy(**overrides):
    values = dict(capture_stdout=True, capture_stderr=True, timeout_seconds=20, output_limit_bytes=65536)
    values.update(overrides)
    return SimpleNamespace(**values)


def _python(code):
    return [sys.executable, "-c", code]


def test_output_and_exit_code(threaded):
    seen = []
    code, stdout, stderr, truncated, timed_out = NativeSshCommandRunner().run(
        _python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"),
        {}, _policy(), cancel_event=threading.Event(),
        on_output=lambda name, text: seen.append(name),
    )
    assert (code, stdout.strip(), stderr.strip(), truncated, timed_out) == (3, "out", "err", False, False)
    assert set(seen) == {"stdout", "stderr"}


def test_output_limit_truncates(threaded):
    _code, stdout, _stderr, truncated, _timed_out = NativeSshCommandRunner().run(
        _python("print('x' * 5000)"), {}, _policy(output_limit_bytes=100),
        cancel_event=threading.Event(),
    )
    assert len(stdout) == 100 and truncated


def test_timeout_stops_the_child(threaded):
    started = time.monotonic()
    _code, _stdout, _stderr, _truncated, timed_out = NativeSshCommandRunner().run(
        _python("import time; time.sleep(30)"), {}, _policy(timeout_seconds=1),
        cancel_event=threading.Event(),
    )
    assert timed_out and time.monotonic() - started < 15


def test_cancel_raises(threaded):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    with pytest.raises(broadcast_service.OperationCancelled):
        NativeSshCommandRunner().run(
            _python("import time; time.sleep(30)"), {}, _policy(), cancel_event=cancel,
        )
