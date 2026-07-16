"""Unit tests for XtermPtyBridge first-drain flush + non-blocking drain."""
from gi.repository import GLib

from sshpilot import xterm_pty_bridge
from sshpilot.xterm_pty_bridge import XtermPtyBridge


def _patch_io_condition_flags(monkeypatch):
    """Stub IOCondition members are types; give them int values for ``&``."""
    monkeypatch.setattr(GLib.IOCondition, "IN", 1, raising=False)
    monkeypatch.setattr(GLib.IOCondition, "HUP", 2, raising=False)
    monkeypatch.setattr(GLib.IOCondition, "ERR", 4, raising=False)


def test_first_readable_drain_flushes_immediately(monkeypatch):
    _patch_io_condition_flags(monkeypatch)
    out = []
    bridge = XtermPtyBridge(on_output=out.append, flush_ms=50)
    reads = [b"$ ", BlockingIOError()]

    def fake_read(_fd, _size):
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(xterm_pty_bridge.os, "read", fake_read)

    result = bridge._on_readable(3, GLib.IOCondition.IN)

    assert result is GLib.SOURCE_CONTINUE
    assert out == ["$ "]
    assert bridge._flushed_once is True
    assert bridge._buf == []


def test_drain_loop_consumes_multiple_reads_before_flush(monkeypatch):
    _patch_io_condition_flags(monkeypatch)
    out = []
    bridge = XtermPtyBridge(on_output=out.append, flush_ms=50)
    reads = [b"line1\n", b"line2\n", BlockingIOError()]

    def fake_read(_fd, _size):
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(xterm_pty_bridge.os, "read", fake_read)

    bridge._on_readable(3, GLib.IOCondition.IN)

    # One on_output for the whole drain (first-flush path).
    assert out == ["line1\nline2\n"]


def test_subsequent_drains_wait_for_coalesce_timer(monkeypatch):
    _patch_io_condition_flags(monkeypatch)
    out = []
    bridge = XtermPtyBridge(on_output=out.append, flush_ms=50)
    bridge._flushed_once = True
    reads = [b"more", BlockingIOError()]

    def fake_read(_fd, _size):
        item = reads.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(xterm_pty_bridge.os, "read", fake_read)

    result = bridge._on_readable(3, GLib.IOCondition.IN)

    assert result is GLib.SOURCE_CONTINUE
    assert out == []
    assert bridge._buf == ["more"]
    assert bridge._flush() is GLib.SOURCE_CONTINUE
    assert out == ["more"]
