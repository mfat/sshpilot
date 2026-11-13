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


def test_path_bar_host_path_reopens_the_portal_folder(monkeypatch):
    """#1376: the bar shows a grant's host path; pressing Enter on it (or a
    subfolder typed after it) must load the portal folder, not the empty
    sandbox directory of the same name where created folders vanish."""
    _ensure_cairo_stub()
    from unittest.mock import MagicMock
    import sshpilot.file_manager_window as fmw

    portal = "/run/user/1000/doc/AAA/Downloads"
    monkeypatch.setattr(
        fmw, "_sandbox_path_for_host",
        lambda path, current: portal + path[len("/home/u/Downloads"):]
        if path.startswith("/home/u/Downloads") else None,
    )
    window = fmw.FileManagerWindow.__new__(fmw.FileManagerWindow)
    pane = MagicMock()
    pane._current_path = portal
    pane._suppress_history_push = False
    window._left_pane = pane
    window._right_pane = MagicMock()
    window._refreshing_panes = set()
    window._load_local = MagicMock()

    window._on_path_changed(pane, "/home/u/Downloads/sub")
    window._load_local.assert_called_once_with(portal + "/sub")
    pane.push_history.assert_called_once_with(portal + "/sub")
    assert pane not in window._refreshing_panes

    window._load_local.reset_mock()
    window._on_path_changed(pane, "/home/u/Downloads")
    window._load_local.assert_called_once_with(portal)
    assert pane in window._refreshing_panes


def test_path_bar_refuses_folders_outside_the_grants(monkeypatch):
    """A typed folder no grant covers is the sandbox's empty copy: refuse it
    and offer Request Access instead of opening it."""
    _ensure_cairo_stub()
    from unittest.mock import MagicMock
    import sshpilot.file_manager_window as fmw

    monkeypatch.setattr(fmw, "_sandbox_path_for_host", lambda path, current: None)
    monkeypatch.setattr(fmw, "_is_reachable_local_path", lambda path: False)
    window = fmw.FileManagerWindow.__new__(fmw.FileManagerWindow)
    pane = MagicMock()
    pane._current_path = "/run/user/1000/doc/AAA/Downloads"
    window._left_pane = pane
    window._right_pane = MagicMock()
    window._refreshing_panes = set()
    window._load_local = MagicMock()

    window._on_path_changed(pane, "/home/u/Music")

    window._load_local.assert_not_called()
    pane.push_history.assert_not_called()
    pane.show_no_access_toast.assert_called_once_with()
