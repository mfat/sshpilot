"""Tests for modal dialog parent resolution (Wayland stacking)."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

if "cairo" not in sys.modules:
    sys.modules["cairo"] = types.ModuleType("cairo")

from sshpilot.window import resolve_app_modal_parent


class MainWindow:
    pass


class FileManagerWindow:
    pass


def test_resolve_app_modal_parent_prefers_application_window():
    main = MainWindow()
    app = MagicMock()
    app.window = main
    app.get_windows.return_value = [main]

    widget = MagicMock()
    widget.get_application.return_value = app

    assert resolve_app_modal_parent(widget) is main


def test_resolve_app_modal_parent_falls_back_to_transient_for():
    fm = FileManagerWindow()
    main = MainWindow()

    app = MagicMock()
    app.window = None
    app.get_windows.return_value = [fm, main]
    app.get_active_window.return_value = fm

    widget = MagicMock()
    widget.get_application.return_value = app
    widget._embedded_parent = None
    widget.get_transient_for.return_value = main
    widget.get_root.return_value = fm

    assert resolve_app_modal_parent(widget) is main


def test_resolve_app_modal_parent_embedded_uses_root():
    main = MainWindow()
    embedded = MagicMock()
    embedded.get_root.return_value = main

    app = MagicMock()
    app.window = None
    app.get_windows.return_value = []

    widget = MagicMock()
    widget._embedded_parent = embedded
    widget.get_application.return_value = app

    assert resolve_app_modal_parent(widget) is main


def test_show_ssh_password_dialog_delegates_to_shared_helper(monkeypatch):
    from sshpilot.window import show_ssh_password_dialog

    calls = {}

    def fake_dialog(parent, **kwargs):
        calls["parent"] = parent
        calls.update(kwargs)
        return "secret"

    monkeypatch.setattr(
        "sshpilot.window._show_password_passphrase_dialog", fake_dialog
    )
    monkeypatch.setattr(
        "sshpilot.window.present_for_modal_dialog", lambda _w: None
    )

    main = MainWindow()
    app = MagicMock()
    app.window = main
    app.get_windows.return_value = [main]
    widget = MagicMock()
    widget.get_application.return_value = app

    conn = MagicMock(
        nickname="demo",
        username="alice",
        hostname="example.com",
    )
    result = show_ssh_password_dialog(
        from_widget=widget,
        connection=conn,
        connection_manager=MagicMock(),
    )

    assert result == "secret"
    assert calls["parent"] is main
    assert calls["prompt_type"] == "password"
    assert calls["display_name"] == "demo"
    assert calls["host"] == "example.com"
    assert calls["username"] == "alice"
