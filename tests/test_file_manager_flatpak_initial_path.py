"""Flatpak file-manager startup must open a portal grant, not sandbox ``~``.

Loading sandbox home after (or instead of) restoring a document-portal grant
empties the local pane and forces the user to re-grant access every open.
"""
import sys
import types


def _ensure_cairo_stub():
    if "cairo" not in sys.modules:
        sys.modules["cairo"] = types.SimpleNamespace()


def test_initial_local_path_non_flatpak_uses_home(monkeypatch):
    _ensure_cairo_stub()
    import sshpilot.file_manager_window as fmw

    monkeypatch.setattr(fmw, "is_flatpak", lambda: False)
    monkeypatch.setattr(fmw.os.path, "expanduser", lambda p: "/home/mahdi" if p == "~" else p)

    assert fmw.FileManagerWindow._initial_local_path() == "/home/mahdi"


def test_initial_local_path_flatpak_prefers_home_grant(monkeypatch):
    _ensure_cairo_stub()
    import sshpilot.file_manager_window as fmw

    home_grant = ("/run/user/1000/doc/HOME/mahdi", "HOME", {"path": "/run/user/1000/doc/HOME/mahdi"})
    monkeypatch.setattr(fmw, "is_flatpak", lambda: True)
    monkeypatch.setattr(fmw.os.path, "expanduser", lambda p: "/home/mahdi" if p == "~" else p)
    monkeypatch.setattr(fmw, "_load_grant_for_host", lambda host: home_grant if host == "/home/mahdi" else None)
    monkeypatch.setattr(fmw, "_load_first_doc_path", lambda: (_ for _ in ()).throw(AssertionError("should not fall back")))

    assert fmw.FileManagerWindow._initial_local_path() == "/run/user/1000/doc/HOME/mahdi"


def test_initial_local_path_flatpak_falls_back_to_recent_grant(monkeypatch):
    _ensure_cairo_stub()
    import sshpilot.file_manager_window as fmw

    recent = ("/run/user/1000/doc/DOCS/Documents", "DOCS", {"path": "/run/user/1000/doc/DOCS/Documents"})
    monkeypatch.setattr(fmw, "is_flatpak", lambda: True)
    monkeypatch.setattr(fmw.os.path, "expanduser", lambda p: "/home/mahdi" if p == "~" else p)
    monkeypatch.setattr(fmw, "_load_grant_for_host", lambda host: None)
    monkeypatch.setattr(fmw, "_load_first_doc_path", lambda: recent)

    assert fmw.FileManagerWindow._initial_local_path() == "/run/user/1000/doc/DOCS/Documents"


def test_initial_local_path_flatpak_none_without_grant(monkeypatch):
    _ensure_cairo_stub()
    import sshpilot.file_manager_window as fmw

    monkeypatch.setattr(fmw, "is_flatpak", lambda: True)
    monkeypatch.setattr(fmw.os.path, "expanduser", lambda p: "/home/mahdi" if p == "~" else p)
    monkeypatch.setattr(fmw, "_load_grant_for_host", lambda host: None)
    monkeypatch.setattr(fmw, "_load_first_doc_path", lambda: None)

    assert fmw.FileManagerWindow._initial_local_path() is None
