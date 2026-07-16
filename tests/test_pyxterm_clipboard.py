"""PyXterm copy/paste uses the GTK system clipboard (not navigator.clipboard)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.terminal_backends import PyXtermBridgeBackend, PyXtermTerminalBackend


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
    b.available = True
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
    monkeypatch.setattr(b, "_set_system_clipboard_text", written.append)
    b._on_pty_message(None, _msg({"type": "copy", "text": "hello from term"}))
    assert written == ["hello from term"]


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
