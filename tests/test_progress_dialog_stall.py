"""Speed and time left fall when bytes stop, and a long pause reads "Stalled" (#1435)."""

from __future__ import annotations

from collections import deque

from sshpilot.file_manager import progress_dialog as module
from sshpilot.file_manager.progress_dialog import SFTPProgressDialog


def _dialog(samples, transferred, total=1000 * 1024 * 1024):
    dlg = SFTPProgressDialog.__new__(SFTPProgressDialog)
    dlg._byte_samples = deque(samples)
    dlg._transferred_bytes = transferred
    dlg._total_bytes = total
    return dlg


def test_speed_is_measured_up_to_now_so_it_falls_when_bytes_stop():
    mb = 1024 * 1024
    dlg = _dialog([(100.0, 0), (104.0, 4 * mb)], 4 * mb)
    steady, stalled = dlg._rate(now=104.0)
    later, _ = dlg._rate(now=108.0)
    assert not stalled
    assert round(steady) == mb
    assert later < steady  # no longer frozen at the last value


def test_a_long_pause_is_stalled_with_no_time_left(monkeypatch):
    dlg = _dialog([(100.0, 0), (104.0, 4096)], 4096)
    monkeypatch.setattr(module.time, "monotonic", lambda: 104.0 + dlg._STALL_SECONDS)
    rate, stalled = dlg._rate()
    assert stalled and rate is None
    assert dlg.remaining_seconds() is None


def test_nothing_received_yet_is_not_stalled():
    dlg = _dialog([(100.0, 0)], 0)
    assert dlg._rate(now=500.0) == (None, False)


def test_slow_link_keeps_an_anchor_older_than_the_window(monkeypatch):
    dlg = _dialog([], 0)
    clock = iter([100.0, 108.0, 116.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    for done in (0, 256 * 1024, 512 * 1024):  # one chunk every 8 s
        dlg.on_bytes(done, dlg._total_bytes)
    assert len(dlg._byte_samples) == 2  # the anchor survived the 5 s window
    rate, stalled = dlg._rate(now=116.0)
    assert not stalled and rate == 256 * 1024 / 8
