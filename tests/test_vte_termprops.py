"""VTE 0.78 termprop snapshots and restricted-signal delivery."""
import types
import pytest
pytest.importorskip("gi")
from sshpilot import terminal_backends
from sshpilot.terminal_backends import VTETerminalBackend


class FakeTerminal:
    def __init__(self):
        self.handler = None
        self.reads = []
    def connect(self, signal, callback):
        assert signal == "termprops-changed"
        self.handler = callback
        return 9
    def get_termprop_string_by_id(self, prop):
        self.reads.append(("string", prop)); return "shell title", 11
    def ref_termprop_uri_by_id(self, prop):
        self.reads.append(("uri", prop))
        return types.SimpleNamespace(to_string=lambda: "file:///srv/app")
    def get_termprop_uint_by_id(self, prop):
        self.reads.append(("uint", prop)); return True, 23


def make_backend():
    backend = object.__new__(VTETerminalBackend)
    backend.vte = FakeTerminal()
    backend.widget = object()
    backend._termprops_handler = None
    return backend


def test_termprops_use_ids_valueless_events_and_defer_callback(monkeypatch):
    ids = types.SimpleNamespace(XTERM_TITLE=1, CURRENT_DIRECTORY_URI=2,
                                SHELL_PREEXEC=3, SHELL_PRECMD=4, SHELL_POSTEXEC=5)
    monkeypatch.setattr(terminal_backends.Vte, "PropertyId", ids, raising=False)
    queued = []
    monkeypatch.setattr(terminal_backends.GLib, "idle_add",
                        lambda callback, *args: queued.append((callback, args)) or 1)
    delivered = []
    backend = make_backend()
    backend.connect_termprops_changed(lambda *args: delivered.append(args))
    backend.vte.handler(backend.vte, [1, 2, 3, 4, 5])
    assert delivered == []
    assert backend.vte.reads == [("string", 1), ("uri", 2), ("uint", 5)]
    callback, args = queued.pop()
    callback(*args)
    event = delivered[0][1]
    assert event == {"title": "shell title", "cwd_uri": "file:///srv/app",
                     "preexec": True, "precmd": True, "postexec": 23}


def test_unrelated_termprop_does_not_read_or_deliver(monkeypatch):
    ids = types.SimpleNamespace(XTERM_TITLE=1, CURRENT_DIRECTORY_URI=2,
                                SHELL_PREEXEC=3, SHELL_PRECMD=4, SHELL_POSTEXEC=5)
    monkeypatch.setattr(terminal_backends.Vte, "PropertyId", ids, raising=False)
    monkeypatch.setattr(terminal_backends.GLib, "idle_add",
                        lambda *args: pytest.fail("unrelated change was delivered"))
    backend = make_backend()
    backend.connect_termprops_changed(lambda *_args: None)
    backend.vte.handler(backend.vte, [99])
    assert backend.vte.reads == []


def test_native_cwd_wins_over_title_fallback(monkeypatch):
    from sshpilot.terminal import TerminalWidget
    terminal = types.SimpleNamespace(
        connection_state=None,
        _is_local_terminal=lambda: False,
        _parse_directory_from_title=lambda title: "/wrong/from/title",
        emit=lambda *args: None,
    )
    TerminalWidget._on_termprops_changed(
        terminal, None,
        {"cwd_uri": "file:///srv/native%20path", "title": "host: /wrong/from/title"})
    assert terminal._current_remote_directory == "/srv/native path"
    assert terminal._native_cwd_available is True


def test_title_is_used_only_until_native_cwd_arrives():
    from sshpilot.terminal import TerminalWidget
    terminal = types.SimpleNamespace(
        connection_state=None,
        _is_local_terminal=lambda: False,
        _parse_directory_from_title=lambda title: "/title/path",
        emit=lambda *args: None,
    )
    TerminalWidget._on_termprops_changed(terminal, None, {"title": "a title"})
    assert terminal._current_remote_directory == "/title/path"


def test_spawn_async_uses_modern_pygobject_keyword_layout(monkeypatch):
    calls = {}
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        spawn_async=lambda *args, **kwargs: calls.update(args=args, kwargs=kwargs)
    )
    monkeypatch.setattr(terminal_backends.Vte, "PtyFlags",
                        types.SimpleNamespace(DEFAULT=0), raising=False)
    callback = object()
    user_data = object()
    backend.spawn_async(["/bin/sh"], cwd="/tmp", callback=callback,
                        user_data=user_data)
    assert calls["args"] == ()
    assert calls["kwargs"]["working_directory"] == "/tmp"
    assert calls["kwargs"]["child_setup"] is None
    assert calls["kwargs"]["timeout"] == -1
    assert calls["kwargs"]["callback"] is not callback
    assert calls["kwargs"]["user_data"] is user_data


def test_modern_vte_does_not_connect_deprecated_title_signal(monkeypatch):
    monkeypatch.setattr(terminal_backends.Vte, "PropertyId", object, raising=False)
    backend = object.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace(
        get_termprop_string_by_id=lambda *_args: None,
        connect=lambda *_args: pytest.fail("legacy title signal was connected"))
    assert backend.connect_title_changed(lambda *_args: None) is None


def test_cwd_uses_generic_boxed_uri_when_ref_accessor_is_not_exposed(monkeypatch):
    ids = types.SimpleNamespace(XTERM_TITLE=1, CURRENT_DIRECTORY_URI=2,
                                SHELL_PREEXEC=3, SHELL_PRECMD=4, SHELL_POSTEXEC=5)
    monkeypatch.setattr(terminal_backends.Vte, "PropertyId", ids, raising=False)
    queued = []
    monkeypatch.setattr(terminal_backends.GLib, "idle_add",
                        lambda callback, *args: queued.append((callback, args)) or 1)

    uri = types.SimpleNamespace(to_string=lambda: "file:///generic/cwd")
    boxed = types.SimpleNamespace(get_boxed=lambda: uri)
    class GenericValueTerminal:
        def connect(self, signal, callback):
            self.handler = callback
            return 10
        def get_termprop_value_by_id(self, prop):
            assert prop == ids.CURRENT_DIRECTORY_URI
            return boxed

    delivered = []
    backend = object.__new__(VTETerminalBackend)
    backend.vte = GenericValueTerminal()
    backend.widget = object()
    backend._termprops_handler = None
    backend.connect_termprops_changed(lambda _widget, event: delivered.append(event))
    backend.vte.handler(backend.vte, [ids.CURRENT_DIRECTORY_URI])

    assert delivered == []
    callback, args = queued.pop()
    callback(*args)
    assert delivered == [{"cwd_uri": "file:///generic/cwd"}]
