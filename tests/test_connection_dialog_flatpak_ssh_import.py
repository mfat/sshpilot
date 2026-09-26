"""Connection dialog Flatpak key/cert browse → confirm → import into ~/.ssh."""

from __future__ import annotations

import types

from sshpilot import connection_dialog as cd
from sshpilot import key_sources
from sshpilot.key_sources import KeySourcesMixin


def test_confirm_import_passes_through_when_not_needed(monkeypatch):
    calls = []
    monkeypatch.setattr(key_sources, "needs_flatpak_ssh_import", lambda path: False)

    self = types.SimpleNamespace(show_error=lambda msg: calls.append(("error", msg)))
    method = KeySourcesMixin.__dict__["_confirm_flatpak_ssh_import"]
    types.MethodType(method, self)(
        "/home/u/.ssh/id_ed25519", calls.append, kind="key"
    )

    assert calls == ["/home/u/.ssh/id_ed25519"]


def test_confirm_import_copies_on_accept(monkeypatch, tmp_path):
    responses = []
    presented = []

    class _FakeMsg:
        def __init__(self, *a, **k):
            pass

        @staticmethod
        def new(parent, heading, body):
            responses.append(("new", parent, heading, body))
            return _FakeMsg()

        def add_response(self, *_a):
            pass

        def set_response_appearance(self, *_a):
            pass

        def set_default_response(self, *_a):
            pass

        def set_close_response(self, *_a):
            pass

        def connect(self, signal, handler):
            assert signal == "response"
            responses.append(("handler", handler))

        def present(self):
            presented.append(True)

    monkeypatch.setattr(key_sources, "needs_flatpak_ssh_import", lambda path: True)
    monkeypatch.setattr(key_sources, "import_private_key_into_ssh_dir",
        lambda path: (str(tmp_path / ".ssh" / "id_ed25519"), []),
    )
    monkeypatch.setattr(cd.Adw, "MessageDialog", _FakeMsg)
    monkeypatch.setattr(
        cd.Adw, "ResponseAppearance", types.SimpleNamespace(SUGGESTED="suggested")
    )

    chosen = []
    parent = object()
    self = types.SimpleNamespace(show_error=lambda msg: chosen.append(("error", msg)))
    method = KeySourcesMixin.__dict__["_confirm_flatpak_ssh_import"]
    types.MethodType(method, self)(
        "/run/user/1000/doc/ABC/id_ed25519",
        chosen.append,
        parent=parent,
        kind="key",
    )

    assert presented == [True]
    assert responses[0][1] is parent
    handler = responses[-1][1]
    handler(object(), "copy")
    assert chosen == [str(tmp_path / ".ssh" / "id_ed25519")]


def test_confirm_import_cancel_does_not_call_on_chosen(monkeypatch):
    class _FakeMsg:
        handler = None

        @staticmethod
        def new(*_a, **_k):
            return _FakeMsg()

        def add_response(self, *_a):
            pass

        def set_response_appearance(self, *_a):
            pass

        def set_default_response(self, *_a):
            pass

        def set_close_response(self, *_a):
            pass

        def connect(self, _signal, handler):
            _FakeMsg.handler = handler

        def present(self):
            pass

    monkeypatch.setattr(key_sources, "needs_flatpak_ssh_import", lambda path: True)
    monkeypatch.setattr(cd.Adw, "MessageDialog", _FakeMsg)
    monkeypatch.setattr(
        cd.Adw, "ResponseAppearance", types.SimpleNamespace(SUGGESTED="suggested")
    )

    chosen = []
    self = types.SimpleNamespace(show_error=lambda msg: None)
    method = KeySourcesMixin.__dict__["_confirm_flatpak_ssh_import"]
    types.MethodType(method, self)("/run/user/1/doc/X/key", chosen.append, kind="key")
    _FakeMsg.handler(object(), "cancel")
    assert chosen == []


def test_confirm_import_shows_error_on_copy_failure(monkeypatch):
    class _FakeMsg:
        handler = None

        @staticmethod
        def new(*_a, **_k):
            return _FakeMsg()

        def add_response(self, *_a):
            pass

        def set_response_appearance(self, *_a):
            pass

        def set_default_response(self, *_a):
            pass

        def set_close_response(self, *_a):
            pass

        def connect(self, _signal, handler):
            _FakeMsg.handler = handler

        def present(self):
            pass

    monkeypatch.setattr(key_sources, "needs_flatpak_ssh_import", lambda path: True)
    monkeypatch.setattr(key_sources, "import_certificate_into_ssh_dir",
        lambda path: (_ for _ in ()).throw(OSError("denied")),
    )
    monkeypatch.setattr(cd.Adw, "MessageDialog", _FakeMsg)
    monkeypatch.setattr(
        cd.Adw, "ResponseAppearance", types.SimpleNamespace(SUGGESTED="suggested")
    )

    errors = []
    chosen = []
    self = types.SimpleNamespace(show_error=lambda msg: errors.append(msg))
    method = KeySourcesMixin.__dict__["_confirm_flatpak_ssh_import"]
    types.MethodType(method, self)(
        "/run/user/1/doc/X/id-cert.pub", chosen.append, kind="cert"
    )
    _FakeMsg.handler(object(), "copy")
    assert chosen == []
    assert errors and "denied" in errors[0]


def test_browse_key_routes_through_flatpak_confirm(monkeypatch, tmp_path):
    """Successful file pick is offered to ``_confirm_flatpak_ssh_import``."""

    routed = []
    holder = {}

    class _FakeDialog:
        def __init__(self, *args, **kwargs):
            pass

        def set_initial_folder(self, gfile):
            pass

        def set_filters(self, filters):
            pass

        def open(self, parent, _cancellable, callback):
            holder["dialog"] = self
            holder["callback"] = callback
            holder["parent"] = parent

        def open_finish(self, _result):
            return types.SimpleNamespace(
                get_path=lambda: "/run/user/1000/doc/ABC/id_ed25519"
            )

    monkeypatch.setattr(cd.Gtk, "FileDialog", _FakeDialog)
    monkeypatch.setattr(key_sources, "get_ssh_dir", lambda: str(tmp_path))

    self = types.SimpleNamespace(
        get_transient_for=lambda: cd.Gtk.Window.__new__(cd.Gtk.Window)
    )

    def _confirm(path, on_chosen, parent=None, kind="key"):
        routed.append((path, parent, kind))
        on_chosen(path)

    self._confirm_flatpak_ssh_import = _confirm
    self._browse_file = types.MethodType(
        KeySourcesMixin.__dict__["_browse_file"], self
    )
    browse_key = types.MethodType(KeySourcesMixin.__dict__["_browse_key"], self)

    chosen = []
    parent = object()
    browse_key(chosen.append, parent)
    holder["callback"](holder["dialog"], object())

    assert holder["parent"] is parent
    assert routed == [("/run/user/1000/doc/ABC/id_ed25519", parent, "key")]
    assert chosen == ["/run/user/1000/doc/ABC/id_ed25519"]


def test_connection_dialog_uses_the_shared_key_sources():
    """The dialog and the login profile editor share one implementation."""
    from sshpilot.connection_dialog import ConnectionDialog

    assert issubclass(ConnectionDialog, KeySourcesMixin)
    for name in ("_open_key_chooser", "_browse_key", "_browse_cert",
                 "_discover_certs", "_confirm_flatpak_ssh_import"):
        assert getattr(ConnectionDialog, name) is KeySourcesMixin.__dict__[name]
