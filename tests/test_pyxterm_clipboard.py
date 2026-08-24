"""PyXterm copy/paste uses the GTK system clipboard (not navigator.clipboard)."""
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock

from sshpilot.terminal_backends import PyXtermBridgeBackend, PyXtermTerminalBackend
from sshpilot import terminal_backends


@pytest.fixture(autouse=True)
def _synchronous_clipboard_verification(monkeypatch):
    """Clipboard success is confirmed via a deferred ``is_local()`` check, which
    needs a main loop.  These tests cover routing, not the verification itself,
    so resolve it inline and successfully; the verification has its own tests."""
    monkeypatch.setattr(
        terminal_backends, "verify_clipboard_ownership",
        lambda clipboard, on_result: on_result(True),
    )




def _bridge_backend():
    b = object.__new__(PyXtermBridgeBackend)
    b.owner = None
    b._bridge = None
    b._js_ready = True
    b._pending_spawn = None
    b._preready_output = []
    b._preready_bytes = 0
    b._stored_font = None
    b._last_size = (24, 80)
    b._webview = None
    b.widget = object()
    b.available = True
    b._clipboard_copy_serial = 0
    b._clipboard_copy_callbacks = {}
    b._has_selection = False
    b._selection_changed_cb = None
    b._shortcut_passthrough = False
    return b


def _msg(payload: dict):
    import json

    return SimpleNamespace(to_json=lambda _indent: json.dumps(payload))


def test_paste_message_reads_system_clipboard(monkeypatch):
    b = _bridge_backend()
    called = []
    monkeypatch.setattr(b, "paste_clipboard", lambda: called.append("paste"))
    b._on_pty_message(None, _msg({"type": "paste"}))
    assert called == ["paste"]


def test_copy_message_sets_system_clipboard(monkeypatch):
    b = _bridge_backend()
    written = []
    monkeypatch.setattr(
        b, "_set_system_clipboard_text",
        lambda text: written.append(text) or True,
    )
    b._on_pty_message(None, _msg({"type": "copy", "text": "hello from term"}))
    assert written == ["hello from term"]


def test_embedded_shortcut_reports_actual_copy_result_to_owner(monkeypatch):
    b = _bridge_backend()
    reported = []
    b.owner = SimpleNamespace(handle_backend_copy_result=reported.append)
    monkeypatch.setattr(b, "_set_system_clipboard_text", lambda _text: True)

    b._on_pty_message(None, _msg({"type": "copy", "text": "selected"}))

    assert reported == [True]


def test_correlated_copy_does_not_duplicate_owner_notification(monkeypatch):
    b = _bridge_backend()
    reported = []
    completed = []
    b.owner = SimpleNamespace(handle_backend_copy_result=reported.append)
    b._run_javascript = lambda _script: None
    monkeypatch.setattr(b, "_set_system_clipboard_text", lambda _text: True)

    b.copy_clipboard(on_complete=completed.append)
    b._on_pty_message(
        None,
        _msg({"type": "copy", "requestId": 1, "text": "selected"}),
    )

    assert completed == [True]
    assert reported == []


def test_selection_message_updates_state_and_notifies_copy_on_select_path():
    b = _bridge_backend()
    changes = []
    b.setup_link_handling(None, None, lambda widget: changes.append(widget))

    b._on_pty_message(
        None,
        _msg({"type": "selection-changed", "hasSelection": True}),
    )

    assert b.get_has_selection() is True
    assert changes == [b.widget]


def test_shortcut_passthrough_is_forwarded_to_embedded_terminal():
    b = _bridge_backend()
    scripts = []
    b._run_javascript = scripts.append

    b.set_shortcut_passthrough(True)

    assert b._shortcut_passthrough is True
    assert scripts == ["window.sshpilotShortcutPassthrough = true;"]


def test_copy_completion_follows_correlated_clipboard_write():
    b = _bridge_backend()
    scripts = []
    b._run_javascript = scripts.append
    completed = []

    b.copy_clipboard(on_complete=completed.append)

    assert completed == []
    assert 'requestId: 1' in scripts[0]

    b._set_system_clipboard_text = lambda text: text == "selected"
    b._on_pty_message(
        None,
        _msg({"type": "copy", "requestId": 1, "text": "selected"}),
    )
    assert completed == [True]


def test_copy_empty_selection_reports_failure_without_clipboard_write(monkeypatch):
    b = _bridge_backend()
    b._run_javascript = lambda _script: None
    write = MagicMock(return_value=False)
    monkeypatch.setattr(b, "_set_system_clipboard_text", write)
    completed = []

    b.copy_clipboard(on_complete=completed.append)
    b._on_pty_message(
        None,
        _msg({"type": "copy", "requestId": 1, "text": ""}),
    )

    write.assert_called_once_with("")
    assert completed == [False]


def test_copy_requests_complete_only_their_matching_callback():
    b = _bridge_backend()
    b._run_javascript = lambda _script: None
    b._set_system_clipboard_text = lambda _text: True
    first, second = [], []

    b.copy_clipboard(on_complete=first.append)
    b.copy_clipboard(on_complete=second.append)
    b._on_pty_message(None, _msg({"type": "copy", "requestId": 2, "text": "two"}))
    assert first == []
    assert second == [True]
    b._on_pty_message(None, _msg({"type": "copy", "requestId": 1, "text": "one"}))
    assert first == [True]


def test_destroy_fails_pending_copy_without_late_success():
    b = _bridge_backend()
    b._run_javascript = lambda _script: None
    completed = []

    b.copy_clipboard(on_complete=completed.append)
    PyXtermTerminalBackend.destroy(b)

    assert completed == [False]
    assert b._clipboard_copy_callbacks == {}


def test_copy_javascript_failure_reports_failure_once():
    b = _bridge_backend()
    b._run_javascript = MagicMock(side_effect=RuntimeError("webview gone"))
    completed = []

    b.copy_clipboard(on_complete=completed.append)

    assert completed == [False]
    assert b._clipboard_copy_callbacks == {}


def test_paste_text_injects_js_literal():
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    scripts = []
    b._run_javascript = scripts.append
    b._paste_text('say "hi"\n')
    assert len(scripts) == 1
    assert "window.term.paste(" in scripts[0]
    assert '\\"hi\\"' in scripts[0] or '"hi"' in scripts[0]
    assert "\\n" in scripts[0]


def test_paste_clipboard_uses_gtk_read_async(monkeypatch):
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    b._webview = None
    pasted = []
    b._paste_text = pasted.append

    clipboard = MagicMock()
    monkeypatch.setattr(b, "_get_system_clipboard", lambda: clipboard)

    def fake_read_async(_cancellable, callback):
        callback(clipboard, object())

    clipboard.read_text_async.side_effect = fake_read_async
    clipboard.read_text_finish.return_value = "from other app"

    b.paste_clipboard()

    clipboard.read_text_async.assert_called_once()
    assert pasted == ["from other app"]


def test_paste_clipboard_ignores_empty(monkeypatch):
    b = object.__new__(PyXtermTerminalBackend)
    b.available = True
    b._webview = None
    pasted = []
    b._paste_text = pasted.append

    clipboard = MagicMock()
    monkeypatch.setattr(b, "_get_system_clipboard", lambda: clipboard)

    def fake_read_async(_cancellable, callback):
        callback(clipboard, object())

    clipboard.read_text_async.side_effect = fake_read_async
    clipboard.read_text_finish.return_value = ""

    b.paste_clipboard()
    assert pasted == []
