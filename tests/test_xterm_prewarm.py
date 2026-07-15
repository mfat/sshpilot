"""Unit tests for PyXterm shell pool dispatch (no WebKit required)."""

from sshpilot.xterm_prewarm import XtermShellPool, _ShellEntry


class _FakeJsValue:
    def __init__(self, payload):
        import json
        self._json = json.dumps(payload)

    def to_json(self, _indent):
        return self._json


def _reset_pool():
    XtermShellPool._ready.clear()
    XtermShellPool._warming.clear()
    XtermShellPool._by_ucm.clear()


def test_ready_owned_entry_is_not_pooled_or_discarded():
    _reset_pool()
    owner = object()
    ucm = object()
    entry = _ShellEntry(webview=object(), ucm=ucm, owner=owner)
    XtermShellPool._by_ucm[id(ucm)] = entry

    XtermShellPool._dispatch_message(ucm, _FakeJsValue({"type": "ready", "rows": 24, "cols": 80}))

    assert entry.js_ready is True
    assert entry.webview is not None
    assert entry.ucm is ucm
    assert XtermShellPool._by_ucm[id(ucm)] is entry
    assert entry not in XtermShellPool._ready
    assert entry not in XtermShellPool._warming


def test_ready_warming_entry_enters_pool_when_room():
    _reset_pool()
    ucm = object()
    entry = _ShellEntry(webview=object(), ucm=ucm)
    XtermShellPool._by_ucm[id(ucm)] = entry
    XtermShellPool._warming.append(entry)

    XtermShellPool._dispatch_message(ucm, _FakeJsValue({"type": "ready", "rows": 24, "cols": 80}))

    assert entry.js_ready is True
    assert entry not in XtermShellPool._warming
    assert entry in XtermShellPool._ready


def test_ready_warming_entry_discarded_when_pool_full():
    _reset_pool()
    ucm = object()
    entry = _ShellEntry(webview=object(), ucm=ucm)
    existing = _ShellEntry(webview=object(), ucm=object())
    XtermShellPool._by_ucm[id(ucm)] = entry
    XtermShellPool._warming.append(entry)
    XtermShellPool._ready.append(existing)

    XtermShellPool._dispatch_message(ucm, _FakeJsValue({"type": "ready", "rows": 24, "cols": 80}))

    assert entry.webview is None
    assert entry.ucm is None
    assert id(ucm) not in XtermShellPool._by_ucm
    assert entry not in XtermShellPool._ready
