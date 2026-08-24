"""A copy is only reported as successful once the clipboard actually took it.

``Gdk.Clipboard.set()``/``set_text()`` return void and raise nothing when the
display server declines the write -- most commonly an unfocused Wayland
surface, where ownership is simply never granted.  Reporting success from "the
selection was non-empty" makes the "Copied to clipboard" toast claim a copy the
user cannot paste.  ``is_local()`` ("the clipboard was last claimed by the
running application") is the signal that settles it.
"""
from types import SimpleNamespace

import pytest

from sshpilot import terminal_backends
from sshpilot.terminal_backends import PyXtermBridgeBackend


def _backend():
    b = object.__new__(PyXtermBridgeBackend)
    for key, value in dict(
        owner=None, _bridge=None, _js_ready=True, _pending_spawn=None,
        _preready_output=[], _preready_bytes=0, _stored_font=None,
        _last_size=(24, 80), _webview=None, widget=object(), available=True,
        _clipboard_copy_serial=0, _clipboard_copy_callbacks={},
        _has_selection=False, _selection_changed_cb=None,
        _shortcut_passthrough=False,
    ).items():
        setattr(b, key, value)
    return b


class _Clipboard:
    def __init__(self, local):
        self._local = local
        self.written = []

    def set(self, text):
        self.written.append(text)

    def is_local(self):
        return self._local


@pytest.mark.parametrize(
    ("ownership_granted", "expected"),
    ((True, True), (False, False)),
)
def test_copy_result_follows_actual_clipboard_ownership(
    monkeypatch, ownership_granted, expected
):
    b = _backend()
    clipboard = _Clipboard(local=ownership_granted)
    monkeypatch.setattr(b, "_get_system_clipboard", lambda: clipboard)
    # Run the deferred ownership check inline.
    monkeypatch.setattr(
        terminal_backends, "verify_clipboard_ownership",
        lambda cb, on_result: on_result(bool(cb.is_local())),
    )

    results = []
    b.set_system_clipboard_text_verified("payload", results.append)

    assert clipboard.written == ["payload"], "the write must still be attempted"
    assert results == [expected]


def test_refused_write_is_not_reported_as_copied(monkeypatch):
    """The regression: a write the compositor drops used to report success."""
    b = _backend()
    monkeypatch.setattr(b, "_get_system_clipboard", lambda: _Clipboard(local=False))
    monkeypatch.setattr(
        terminal_backends, "verify_clipboard_ownership",
        lambda cb, on_result: on_result(bool(cb.is_local())),
    )

    results = []
    b.set_system_clipboard_text_verified("selected text", results.append)

    assert results == [False]


def test_empty_selection_never_touches_the_clipboard(monkeypatch):
    b = _backend()
    clipboard = _Clipboard(local=True)
    monkeypatch.setattr(b, "_get_system_clipboard", lambda: clipboard)

    results = []
    b.set_system_clipboard_text_verified("", results.append)

    assert results == [False]
    assert clipboard.written == []


def test_verification_is_deferred_not_sampled_immediately(monkeypatch):
    """is_local() still reports the local content immediately after set(), so a
    synchronous check would just relocate the lie."""
    scheduled = []
    monkeypatch.setattr(
        terminal_backends.GLib, "timeout_add",
        lambda delay, fn: scheduled.append((delay, fn)) or 1,
    )

    results = []
    terminal_backends.verify_clipboard_ownership(_Clipboard(local=False), results.append)

    assert results == [], "must not resolve synchronously"
    assert scheduled and scheduled[0][0] > 0
    scheduled[0][1]()
    assert results == [False]


def test_failed_copy_tells_the_user_when_there_was_a_selection():
    """Silence on a failed copy is what made this invisible in the field."""
    from sshpilot.terminal import TerminalWidget

    toasts = []
    widget = object.__new__(TerminalWidget)
    widget._show_toast = toasts.append

    TerminalWidget.handle_backend_copy_result(widget, False, attempted=True)
    assert toasts and "copy" in toasts[0].lower()

    toasts.clear()
    TerminalWidget.handle_backend_copy_result(widget, False, attempted=False)
    assert toasts == [], "an empty selection must stay quiet"

    toasts.clear()
    TerminalWidget.handle_backend_copy_result(widget, True, attempted=True)
    assert toasts == ["Copied to clipboard"]
