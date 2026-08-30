"""Debug logs for silent terminal copy failures (issue #1178).

These logs must identify *why* a copy no-op'd (empty selection, mouse
tracking, pass-through, missing clipboard) without recording the selected
text itself.
"""

import logging
from types import SimpleNamespace

from sshpilot.terminal import TerminalWidget, clipboard_debug_state
from sshpilot.terminal_backends import PyXtermBridgeBackend, PyXtermTerminalBackend
from sshpilot.terminal_input import MouseTrackingState


SECRET = "secret-clipboard-payload"


def _bridge_backend():
    backend = object.__new__(PyXtermBridgeBackend)
    backend.owner = None
    backend._bridge = None
    backend._js_ready = True
    backend._pending_spawn = None
    backend._preready_output = []
    backend._preready_bytes = 0
    backend._stored_font = None
    backend._last_size = (24, 80)
    backend._webview = None
    backend.widget = object()
    backend.available = True
    backend._clipboard_copy_serial = 0
    backend._clipboard_copy_callbacks = {}
    backend._has_selection = False
    backend._selection_changed_cb = None
    backend._shortcut_passthrough = False
    return backend


def _msg(payload: dict):
    import json

    return SimpleNamespace(to_json=lambda _indent: json.dumps(payload))


def test_clipboard_debug_state_is_privacy_safe():
    tracker = MouseTrackingState()
    tracker.feed(b"\x1b[?1000;1006h")
    widget = SimpleNamespace(
        backend=SimpleNamespace(get_has_selection=lambda: True),
        _backend_name="vte",
        _pass_through_mode=False,
        _mouse_tracking=tracker,
    )

    snapshot = clipboard_debug_state(widget)

    assert "backend=vte" in snapshot
    assert "has_selection=True" in snapshot
    assert "mouse_tracking=True" in snapshot
    assert "1000" in snapshot and "1006" in snapshot
    assert SECRET not in snapshot


def test_copy_text_logs_request_and_failure_without_payload(caplog):
    backend = SimpleNamespace(
        get_has_selection=lambda: False,
        copy_clipboard=lambda *, format="text", on_complete=None: on_complete(False),
    )
    widget = SimpleNamespace(
        backend=backend,
        _backend_name="vte",
        _pass_through_mode=False,
        _mouse_tracking=MouseTrackingState(),
        _show_toast=lambda _message: None,
    )

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        TerminalWidget.copy_text(widget)

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "Terminal copy requested" in joined
    assert "copied=False" in joined
    assert "has_selection=False" in joined
    assert SECRET not in joined


def test_backend_owned_copy_result_logs_failure(caplog):
    widget = SimpleNamespace(
        backend=SimpleNamespace(get_has_selection=lambda: False),
        _backend_name="pyxterm",
        _pass_through_mode=True,
        _mouse_tracking=MouseTrackingState(),
        _show_toast=lambda _message: None,
    )

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        TerminalWidget.handle_backend_copy_result(widget, False)

    assert "backend-owned copy result copied=False" in caplog.text
    assert "pass_through=True" in caplog.text


def test_feed_display_logs_mouse_tracking_transitions(caplog):
    widget = TerminalWidget.__new__(TerminalWidget)
    feeds = []
    widget.backend = SimpleNamespace(feed=feeds.append)
    widget._mouse_tracking = MouseTrackingState()

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal"):
        widget._feed_display(b"hello")
        widget._feed_display(b"\x1b[?1000h")
        widget._feed_display(b"\x1b[?1000h")
        widget._feed_display(b"\x1b[?1000l")

    assert feeds == [b"hello", b"\x1b[?1000h", b"\x1b[?1000h", b"\x1b[?1000l"]
    messages = [
        record.getMessage()
        for record in caplog.records
        if "mouse tracking changed" in record.getMessage()
    ]
    assert any("modes=[1000]" in message for message in messages)
    assert any("active=False" in message for message in messages)
    assert len(messages) == 2


def test_pyxterm_copy_message_logs_lengths_not_text(caplog):
    backend = _bridge_backend()
    backend._has_selection = True
    monkey_writes = []
    backend._set_system_clipboard_text = (
        lambda text: monkey_writes.append(text) or True
    )

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend._on_pty_message(
            None,
            _msg({
                "type": "copy",
                "text": SECRET,
                "hasSelection": True,
                "selectionLength": len(SECRET),
            }),
        )

    assert monkey_writes == [SECRET]
    assert "text_len=%s" % len(SECRET) in caplog.text
    assert "js_has_selection=True" in caplog.text
    assert SECRET not in caplog.text


def test_pyxterm_empty_copy_logs_empty_selection_reason(caplog):
    backend = _bridge_backend()
    backend._run_javascript = lambda _script: None
    completed = []

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend.copy_clipboard(on_complete=completed.append)
        backend._on_pty_message(
            None,
            _msg({
                "type": "copy",
                "requestId": 1,
                "text": "",
                "hasSelection": False,
                "selectionLength": 0,
            }),
        )

    assert completed == [False]
    assert "reason=empty-selection" in caplog.text
    assert "correlated=True" in caplog.text


def test_pyxterm_passthrough_copy_is_logged(caplog):
    backend = _bridge_backend()
    backend._shortcut_passthrough = True
    backend._has_selection = True

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend._on_pty_message(
            None,
            _msg({"type": "clipboard-passthrough", "key": "c", "hasSelection": True}),
        )

    assert "clipboard shortcut passed through key=c" in caplog.text
    assert "has_selection=True" in caplog.text


def test_pyxterm_selection_change_logs_transitions_only(caplog):
    backend = _bridge_backend()
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend._on_pty_message(
            None, _msg({"type": "selection-changed", "hasSelection": True})
        )
        backend._on_pty_message(
            None, _msg({"type": "selection-changed", "hasSelection": True})
        )
        backend._on_pty_message(
            None, _msg({"type": "selection-changed", "hasSelection": False})
        )

    messages = [
        record.getMessage()
        for record in caplog.records
        if "selection-changed" in record.getMessage()
    ]
    assert len(messages) == 2
    assert "has_selection=True" in messages[0]
    assert "has_selection=False" in messages[1]


def test_pyxterm_missing_clipboard_logs_reason(caplog, monkeypatch):
    backend = object.__new__(PyXtermTerminalBackend)
    backend._webview = None
    monkeypatch.setattr(backend, "_get_system_clipboard", lambda: None)
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        result = backend._set_system_clipboard_text(SECRET)

    assert result is False
    assert "reason=no-clipboard" in caplog.text
    assert SECRET not in caplog.text


def test_pyxterm_destroy_logs_pending_copy_callbacks(caplog):
    backend = _bridge_backend()
    backend._run_javascript = lambda _script: None
    completed = []
    backend.copy_clipboard(on_complete=completed.append)

    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        PyXtermTerminalBackend.destroy(backend)

    assert completed == [False]
    assert "failing 1 pending clipboard copy callback" in caplog.text
