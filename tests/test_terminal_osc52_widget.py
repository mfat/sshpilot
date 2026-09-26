"""OSC 52 remote clipboard writes reach the local clipboard (GH #1297).

The scanner itself is covered in test_terminal_osc52.py; these pin the
wiring: which output gets scanned, and the policy/safety rules applied before
anything touches the clipboard.
"""

import base64
import types

import pytest

pytest.importorskip("gi")

import sshpilot.terminal as terminal_module
from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import PyXtermBridgeBackend
from sshpilot.terminal_osc52 import Osc52Scanner


def osc52(text, selection="c"):
    return f"\x1b]52;{selection};{base64.b64encode(text.encode()).decode()}\a".encode()


class _Clipboard:
    def __init__(self):
        self.values = []

    def set(self, value):
        self.values.append(value)


class _Toast:
    instances = []

    def __init__(self, title):
        self.title = title
        self.button_label = None
        self.timeout = None
        self.handlers = {}
        self.dismissed = False
        _Toast.instances.append(self)

    @classmethod
    def new(cls, title):
        return cls(title)

    def set_button_label(self, label):
        self.button_label = label

    def set_timeout(self, timeout):
        self.timeout = timeout

    def connect(self, signal, callback):
        self.handlers[signal] = callback

    def dismiss(self):
        self.dismissed = True
        handler = self.handlers.get("dismissed")
        if handler:
            handler(self)

    def click(self):
        self.handlers["button-clicked"](self)


class _Timers:
    def __init__(self):
        self.pending = []

    def timeout_add(self, _ms, callback):
        self.pending.append(callback)
        return len(self.pending)

    def source_remove(self, _source_id):
        pass

    def run(self):
        pending, self.pending = self.pending, []
        for callback in pending:
            callback()


@pytest.fixture
def timers(monkeypatch):
    timers = _Timers()
    monkeypatch.setattr(
        terminal_module,
        "GLib",
        types.SimpleNamespace(
            timeout_add=timers.timeout_add, source_remove=timers.source_remove
        ),
    )
    _Toast.instances = []
    monkeypatch.setattr(
        terminal_module, "Adw", types.SimpleNamespace(Toast=_Toast)
    )
    return timers


def _terminal(policy="always", *, focused=True, active=True, selection=False,
              replay=False):
    fed = []
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = True
    t._daemon_controller = types.SimpleNamespace(
        delivering_replay=replay,
        session_running=True,
    )
    t.backend = types.SimpleNamespace(
        feed=fed.append,
        get_has_selection=lambda: selection,
    )
    settings = {"terminal.osc52_policy": policy}
    t.config = types.SimpleNamespace(
        get_setting=lambda key, default=None: settings.get(key, default)
    )
    t._osc52_scanner = Osc52Scanner()
    t._osc52_pending = None
    t._osc52_timer_id = None
    t._osc52_toast = None
    t._shell_output_seen = True
    t._daemon_running_gate_active = lambda: False
    t._update_daemon_connection_state = lambda: None
    t._display_feed_pause = None
    t._mouse_tracking = None

    t.clipboard = _Clipboard()
    t.primary = _Clipboard()
    t.toasts = []
    t.get_clipboard = lambda: t.clipboard
    t.get_primary_clipboard = lambda: t.primary

    def add_toast(toast):
        t.toasts.append(toast)

    # The focused widget sits inside this terminal (focus.is_ancestor(t)).
    focus_widget = types.SimpleNamespace(is_ancestor=lambda widget: widget is t)
    root = types.SimpleNamespace(
        is_active=lambda: active,
        get_focus=lambda: focus_widget if focused else None,
        toast_overlay=types.SimpleNamespace(add_toast=add_toast),
    )
    t.get_root = lambda: root
    t.fed = fed
    return t


def test_daemon_output_copies_and_still_reaches_the_emulator(timers):
    t = _terminal("always")
    data = b"$ " + osc52("OSC52-TEST") + b"\r\n$ "

    t._on_daemon_output(data)
    assert t.fed == [data]  # bytes untouched
    assert t.clipboard.values == []  # coalescing window
    timers.run()

    assert t.clipboard.values == ["OSC52-TEST"]
    assert [toast.title for toast in t.toasts] == [
        "Remote program copied 10 characters to the clipboard"
    ]


def test_ask_policy_copies_only_after_confirmation(timers):
    t = _terminal("ask")
    t._on_daemon_output(osc52("secret-ish"))
    timers.run()

    assert t.clipboard.values == []
    (toast,) = t.toasts
    assert toast.button_label == "Copy"
    assert "10 characters" in toast.title
    assert "secret-ish" not in toast.title  # never shows the content

    toast.click()
    assert t.clipboard.values == ["secret-ish"]


def test_new_request_replaces_an_unanswered_prompt(timers):
    t = _terminal("ask")
    t._on_daemon_output(osc52("first"))
    timers.run()
    t._on_daemon_output(b"\r\n" + osc52("second"))
    timers.run()

    first, second = t.toasts
    assert first.dismissed
    assert not second.dismissed
    assert t._osc52_toast is second


def test_never_policy_ignores_requests(timers):
    t = _terminal("never")
    t._on_daemon_output(osc52("nope"))
    timers.run()
    assert t.clipboard.values == []
    assert t.toasts == []


def test_replayed_output_never_copies_again(timers):
    t = _terminal("always", replay=True)
    t._on_daemon_output(osc52("old copy"))
    timers.run()
    assert t.clipboard.values == []


@pytest.mark.parametrize(
    "kwargs", [{"focused": False}, {"active": False}, {"selection": True}]
)
def test_unfocused_or_selecting_terminal_does_not_copy(timers, kwargs):
    t = _terminal("always", **kwargs)
    t._on_daemon_output(osc52("blocked"))
    timers.run()
    assert t.clipboard.values == []


def test_primary_selection_target(timers):
    t = _terminal("always")
    t._on_daemon_output(osc52("to primary", selection="p"))
    timers.run()
    assert t.primary.values == ["to primary"]
    assert t.clipboard.values == []


def test_chunked_copy_is_applied_once(timers):
    t = _terminal("always")
    encoded = base64.b64encode(b"abcdefghijkl")
    data = b"".join(
        b"\x1b]52;c;" + encoded[i:i + 8] + b"\x07" for i in range(0, 16, 8)
    )
    t._on_daemon_output(data[:10])
    t._on_daemon_output(data[10:])
    timers.run()
    assert t.clipboard.values == ["abcdefghijkl"]
    assert len(t.toasts) == 1


def test_scan_failure_never_blocks_output(timers):
    t = _terminal("always")

    class _Broken:
        max_bytes = 0

        def feed(self, _data):
            raise RuntimeError("boom")

    t._osc52_scanner = _Broken()
    t._on_daemon_output(b"hello")
    assert t.fed == [b"hello"]


def test_pyxterm_local_pty_output_is_scanned_once():
    scanned, painted = [], []
    backend = PyXtermBridgeBackend.__new__(PyXtermBridgeBackend)
    backend.owner = types.SimpleNamespace(scan_remote_clipboard=scanned.append)
    backend._on_pty_output = painted.append

    backend._on_local_pty_output("chunk")
    assert scanned == ["chunk"]
    assert painted == ["chunk"]

    # Daemon bytes take feed(); the owner already scanned them.
    scanned.clear()
    backend.feed(b"daemon")
    assert scanned == []
    assert painted == ["chunk", "daemon"]
