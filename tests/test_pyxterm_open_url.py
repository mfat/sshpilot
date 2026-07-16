"""PyXterm WebLinks → system browser via the JS→Python bridge."""
from types import SimpleNamespace

from sshpilot.terminal_backends import PyXtermBridgeBackend


def _backend():
    b = object.__new__(PyXtermBridgeBackend)
    b.owner = None
    b._bridge = None
    b._js_ready = True
    b._pending_spawn = None
    b._preready_output = []
    b._preready_bytes = 0
    b._stored_font = None
    b._last_size = (24, 80)
    return b


def _msg(payload: dict):
    import json

    return SimpleNamespace(to_json=lambda _indent: json.dumps(payload))


def test_open_url_launches_http(monkeypatch):
    b = _backend()
    opened = []
    monkeypatch.setattr(
        "sshpilot.web_tab.open_url_in_browser",
        lambda url: opened.append(url) or True,
    )
    b._on_pty_message(None, _msg({"type": "open-url", "url": "https://example.com/x"}))
    assert opened == ["https://example.com/x"]


def test_open_url_ignores_non_http():
    b = _backend()
    # Must not raise or attempt to open file:/javascript: schemes.
    b._on_pty_message(None, _msg({"type": "open-url", "url": "file:///etc/passwd"}))
    b._on_pty_message(None, _msg({"type": "open-url", "url": "javascript:alert(1)"}))
