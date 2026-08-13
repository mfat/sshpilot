"""Daemon SSH + PyXterm: output/input go through the terminal backend abstraction.

With daemon mode on, the PTY lives in the daemon. ``TerminalWidget`` must use
``backend.feed`` / ``connect_commit`` / ``connect_size_changed`` /
``get_size`` — not ``self.vte`` — so VTE and PyXterm share one path.
"""
import types

import pytest

pytest.importorskip("gi")

from sshpilot.terminal import TerminalWidget
from sshpilot.terminal_backends import PyXtermBridgeBackend
from sshpilot.terminal_backends import TerminalBackendCapabilityError


class _FakeJsValue:
    def __init__(self, payload):
        import json
        self._json = json.dumps(payload)

    def to_json(self, _indent):
        return self._json


def _daemon_term(*, backend):
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = True
    t._daemon_commit_handler = None
    t._daemon_size_handler = None
    t._daemon_controller = types.SimpleNamespace(
        state=types.SimpleNamespace(value="active"),
        tab_state=types.SimpleNamespace(session_id="s1", expected_sequence=0),
        input_owner=True,
        send_input=lambda data: None,
        resize=lambda dims: None,
    )
    t.backend = backend
    t.is_connected = False
    t.connection_state = type("S", (), {"CONNECTING": "connecting", "CONNECTED": "connected"})()
    t.connection_state = t.connection_state.CONNECTING
    t.connection_state_reason = ""
    t._set_connecting_overlay_visible = lambda *_a, **_k: None
    t._update_daemon_connection_state = lambda: None
    return t


class _StackingBackend:
    """VTE-like backend: each connect_* stacks another handler until disconnect."""

    def __init__(self):
        self._commit = []
        self._size = []
        self._next_id = 1
        self._by_id = {}

    def connect_commit(self, cb):
        hid = self._next_id
        self._next_id += 1
        self._commit.append(cb)
        self._by_id[hid] = ("commit", cb)
        return hid

    def connect_size_changed(self, cb):
        hid = self._next_id
        self._next_id += 1
        self._size.append(cb)
        self._by_id[hid] = ("size", cb)
        return hid

    def disconnect(self, handler_id):
        kind, cb = self._by_id.pop(handler_id)
        if kind == "commit":
            self._commit.remove(cb)
        else:
            self._size.remove(cb)

    def fire_commit(self, text="a"):
        for cb in list(self._commit):
            cb(None, text, len(text))


def test_daemon_output_feeds_backend_when_vte_is_none():
    """Prove the regression: daemon bytes must reach backend.feed without VTE."""
    fed = []
    backend = types.SimpleNamespace(feed=lambda data: fed.append(data))
    t = _daemon_term(backend=backend)

    t._on_daemon_output(b"user@host:~$ ")

    assert fed == [b"user@host:~$ "]


def test_daemon_continuity_marker_feeds_backend_when_vte_is_none():
    fed = []
    backend = types.SimpleNamespace(feed=lambda data: fed.append(data))
    t = _daemon_term(backend=backend)

    t._on_daemon_continuity_lost()

    assert len(fed) == 1
    assert b"no longer available" in fed[0]


def test_daemon_dimensions_use_backend_get_size_not_vte():
    backend = types.SimpleNamespace(get_size=lambda: (40, 120))
    t = _daemon_term(backend=backend)

    dims = t._daemon_terminal_dimensions()

    assert dims.rows == 40
    assert dims.columns == 120


def test_daemon_input_does_not_require_vte_attribute():
    sent = []
    backend = _StackingBackend()
    t = _daemon_term(backend=backend)
    t._daemon_controller.send_input = sent.append

    t._on_daemon_commit(None, "whoami\r", 7)

    assert sent == [b"whoami\r"]
    assert not hasattr(t, "vte")


def test_daemon_resize_uses_backend_dimensions():
    resized = []
    backend = types.SimpleNamespace(get_size=lambda: (30, 100))
    t = _daemon_term(backend=backend)
    t._daemon_controller.resize = resized.append

    t._on_daemon_size_changed(None, 0, 0)

    assert [(item.rows, item.columns) for item in resized] == [(30, 100)]


def test_install_daemon_backend_io_uses_abstraction():
    connected = []
    disconnected = []

    def connect_commit(cb):
        connected.append(("commit", cb))
        return "commit-id"

    def connect_size(cb):
        connected.append(("size", cb))
        return "size-id"

    backend = types.SimpleNamespace(
        connect_commit=connect_commit,
        connect_size_changed=connect_size,
        disconnect=lambda hid: disconnected.append(hid),
    )
    t = _daemon_term(backend=backend)

    t._install_daemon_backend_io()

    assert [kind for kind, _ in connected] == ["commit", "size"]
    assert connected[0][1] == t._on_daemon_commit
    assert connected[1][1] == t._on_daemon_size_changed
    assert t._daemon_commit_handler == "commit-id"
    assert t._daemon_size_handler == "size-id"
    assert disconnected == []


def test_install_daemon_backend_io_twice_sends_input_once():
    """Re-attach must replace handlers, not stack VTE-style duplicates."""
    sent = []
    backend = _StackingBackend()
    t = _daemon_term(backend=backend)
    t._daemon_controller.send_input = lambda data: sent.append(data)

    t._install_daemon_backend_io()
    t._install_daemon_backend_io()  # second attach on same widget

    assert len(backend._commit) == 1
    assert len(backend._size) == 1
    backend.fire_commit("x")
    assert sent == [b"x"]


def test_uninstall_daemon_backend_io_clears_handlers():
    backend = _StackingBackend()
    t = _daemon_term(backend=backend)
    t._install_daemon_backend_io()
    assert len(backend._commit) == 1

    t._uninstall_daemon_backend_io()

    assert backend._commit == []
    assert backend._size == []
    assert t._daemon_commit_handler is None
    assert t._daemon_size_handler is None


def test_local_input_calls_backend_only():
    fed = []
    backend = types.SimpleNamespace(
        supports_feature=lambda feature: feature == "local_process",
        feed_child_data=fed.append,
    )
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = False
    t.backend = backend

    t.feed_child_data(b"pwd\r")

    assert fed == [b"pwd\r"]
    assert not hasattr(t, "vte")


def test_unsupported_local_input_fails_clearly():
    backend = types.SimpleNamespace(supports_feature=lambda _feature: False)
    t = TerminalWidget.__new__(TerminalWidget)
    t._daemon_mode = False
    t.backend = backend

    with pytest.raises(TerminalBackendCapabilityError, match="local process input"):
        t.feed_child_data(b"pwd\r")


def test_pyxterm_bridge_feed_paints_via_write_to_term():
    """feed() is the display path for daemon-owned PTYs (no local bridge)."""
    painted = []
    b = object.__new__(PyXtermBridgeBackend)
    b._js_ready = True
    b._preready_output = []
    b._preready_bytes = 0
    b._recent_output = ""
    b._output_hooks = []
    b._write_to_term = lambda text, *, bulk=False: painted.append((text, bulk))

    b.feed(b"user@host:~$ ")

    assert painted == [("user@host:~$ ", False)]
    assert b.get_content() == "user@host:~$ "


def test_pyxterm_bridge_feed_buffers_until_js_ready():
    b = object.__new__(PyXtermBridgeBackend)
    b._js_ready = False
    b._preready_output = []
    b._preready_bytes = 0
    b._recent_output = ""
    b._output_hooks = []
    b._write_to_term = lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("too early"))

    b.feed(b"$ ")

    assert b._preready_output == ["$ "]
    assert b.get_content() == "$ "


def test_pyxterm_input_without_bridge_fires_commit_callback():
    """Daemon mode: connect_commit receives keystrokes (VTE commit parity)."""
    commits = []
    b = object.__new__(PyXtermBridgeBackend)
    b.owner = None
    b.widget = object()
    b._bridge = None
    b._js_ready = True
    b._autocompleter = None
    b._commit_cb = None
    b._size_changed_cb = None
    b.connect_commit(lambda widget, text, size: commits.append((text, size)))

    b._on_pty_message(None, _FakeJsValue({"type": "input", "data": "ls\r"}))

    assert commits == [("ls\r", 3)]


def test_pyxterm_resize_without_bridge_fires_size_callback():
    sizes = []
    b = object.__new__(PyXtermBridgeBackend)
    b.owner = None
    b.widget = object()
    b._bridge = None
    b._last_size = (24, 80)
    b._commit_cb = None
    b._size_changed_cb = None
    b.connect_size_changed(lambda widget, w, h: sizes.append(b.get_size()))

    b._on_pty_message(None, _FakeJsValue({"type": "resize", "rows": 40, "cols": 120}))

    assert b._last_size == (40, 120)
    assert sizes == [(40, 120)]


def test_pyxterm_connect_commit_replaces_previous_callback():
    """PyXterm replaces rather than stacks; disconnect clears the slot."""
    calls = []
    b = object.__new__(PyXtermBridgeBackend)
    b.widget = object()
    b._bridge = None
    b._js_ready = True
    b._autocompleter = None
    b._commit_cb = None
    b._size_changed_cb = None
    first = b.connect_commit(lambda *_a: calls.append("first"))
    second = b.connect_commit(lambda *_a: calls.append("second"))
    assert first == "pyxterm_bridge_commit"
    assert second == "pyxterm_bridge_commit"

    b._on_pty_message(None, _FakeJsValue({"type": "input", "data": "a"}))
    assert calls == ["second"]

    b.disconnect(second)
    b._on_pty_message(None, _FakeJsValue({"type": "input", "data": "b"}))
    assert calls == ["second"]
