"""Preready flush ordering for the embedded PyXterm backend.

The shell prompt is buffered until xterm.js reports ready; that flush must run
before theme/font JS so the prompt is not queued behind them, and must be a
single bulk write (not chunk replay).
"""
import base64

from sshpilot.terminal_backends import PyXtermBridgeBackend


class _FakeJsValue:
    def __init__(self, payload):
        import json
        self._json = json.dumps(payload)

    def to_json(self, _indent):
        return self._json


def _make_backend():
    b = object.__new__(PyXtermBridgeBackend)
    b._js_ready = False
    b._preready_output = ["$ "]
    b._preready_bytes = 2
    b._last_size = (24, 80)
    b._stored_font = None
    b._bridge = None
    b._pending_spawn = None
    b._output_hooks = []
    b._recent_output = ""
    order = []
    b._write_to_term = lambda text, *, bulk=False: order.append(("write", text, bulk))
    b.apply_theme = lambda: order.append(("theme", None))
    b._order = order
    return b


def test_ready_flushes_preready_before_theme_as_bulk():
    b = _make_backend()
    b._on_pty_message(None, _FakeJsValue({"type": "ready", "rows": 30, "cols": 100}))

    assert b._js_ready is True
    assert b._preready_output == []
    assert b._preready_bytes == 0
    assert b._last_size == (30, 100)
    assert b._order == [("write", "$ ", True), ("theme", None)]


def test_ready_with_empty_preready_still_applies_theme():
    b = _make_backend()
    b._preready_output = []
    b._preready_bytes = 0
    b._on_pty_message(None, _FakeJsValue({"type": "ready", "rows": 24, "cols": 80}))

    assert b._order == [("theme", None)]


def test_write_to_term_bulk_uses_base64_helper():
    scripts = []
    b = object.__new__(PyXtermBridgeBackend)
    b._run_javascript = scripts.append
    b._fc_written = 0
    b._fc_pending = 0
    b._fc_paused = False
    b._fc_safety_id = None
    b._bridge = None
    b._FC_CALLBACK_BYTE_LIMIT = PyXtermBridgeBackend._FC_CALLBACK_BYTE_LIMIT
    b._FC_HIGH = PyXtermBridgeBackend._FC_HIGH
    b._FC_LOW = PyXtermBridgeBackend._FC_LOW

    b._write_to_term("hi café", bulk=True)

    assert len(scripts) == 1
    assert "termWriteB64" in scripts[0]
    # Bulk always requests a write-ack (second arg true).
    assert ", true)" in scripts[0]
    import json
    import re
    m = re.search(r'termWriteB64\((.*), true\)', scripts[0])
    assert m
    b64 = json.loads(m.group(1))
    assert base64.b64decode(b64).decode("utf-8") == "hi café"
