"""xterm.js-style pending-callback flow control for PyXtermBridgeBackend."""
from sshpilot.terminal_backends import PyXtermBridgeBackend


class _FakeBridge:
    def __init__(self):
        self.paused = False
        self.pause_calls = 0
        self.resume_calls = 0

    def pause(self):
        self.paused = True
        self.pause_calls += 1

    def resume(self):
        self.paused = False
        self.resume_calls += 1


class _FakeJsValue:
    def __init__(self, payload):
        import json
        self._json = json.dumps(payload)

    def to_json(self, _indent):
        return self._json


def _make_backend():
    b = object.__new__(PyXtermBridgeBackend)
    b._bridge = _FakeBridge()
    b._fc_written = 0
    b._fc_pending = 0
    b._fc_paused = False
    b._fc_safety_id = None
    b._FC_CALLBACK_BYTE_LIMIT = 100
    b._FC_HIGH = 2
    b._FC_LOW = 1
    b._FC_SAFETY_MS = 2000
    b._scripts = []
    b._run_javascript = b._scripts.append
    return b


def test_small_chunks_use_fast_path_without_ack():
    b = _make_backend()
    b._write_to_term("hi")
    assert b._scripts[-1].endswith(", false);")
    assert b._fc_pending == 0
    assert b._bridge.pause_calls == 0


def test_crossing_byte_limit_requests_ack():
    b = _make_backend()
    b._write_to_term("x" * 50)
    assert b._scripts[-1].endswith(", false);")
    b._write_to_term("y" * 60)  # cumulative >= 100
    assert b._scripts[-1].endswith(", true);")
    assert b._fc_pending == 1
    assert b._fc_written == 0


def test_pending_above_high_pauses_pty():
    b = _make_backend()
    # Three ACK'd writes → pending 3 > HIGH(2)
    for _ in range(3):
        b._fc_written = b._FC_CALLBACK_BYTE_LIMIT
        b._write_to_term("z")
    assert b._fc_pending == 3
    assert b._fc_paused is True
    assert b._bridge.pause_calls == 1
    assert b._bridge.paused is True


def test_acks_below_low_resume_pty():
    b = _make_backend()
    for _ in range(3):
        b._fc_written = b._FC_CALLBACK_BYTE_LIMIT
        b._write_to_term("z")
    assert b._fc_paused is True

    b._on_write_ack()  # pending 2 — still >= LOW(1), stay paused? LOW is 1, resume when < LOW
    assert b._fc_pending == 2
    assert b._fc_paused is True  # 2 < 1 is false

    b._on_write_ack()  # pending 1 — 1 < 1 is false
    assert b._fc_pending == 1
    assert b._fc_paused is True

    b._on_write_ack()  # pending 0 — 0 < 1 → resume
    assert b._fc_pending == 0
    assert b._fc_paused is False
    assert b._bridge.resume_calls == 1


def test_write_ack_message_dispatches():
    b = _make_backend()
    b._fc_pending = 2
    b._fc_paused = True
    b._bridge.paused = True
    b._on_pty_message(None, _FakeJsValue({"type": "write-ack"}))
    assert b._fc_pending == 1


def test_bridge_pause_resume_idempotent():
    from sshpilot.xterm_pty_bridge import XtermPtyBridge

    br = XtermPtyBridge(on_output=lambda _c: None, flush_ms=50)
    # No PTY yet — pause/resume must be safe no-ops.
    br.pause()
    assert br.paused is True
    br.pause()  # idempotent
    br.resume()
    assert br.paused is False
    br.resume()
    br.close()
