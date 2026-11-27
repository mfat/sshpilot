"""Connection sharing in the connection editor, written to the Host block.

The row is a view over the Extra Options list: Default writes nothing, On
``ControlMaster auto`` with a ``ControlPath`` and a ``ControlPersist``, Off
``ControlMaster no`` with ``ControlPath none``.
"""

import os

import pytest

from sshpilot import connection_dialog_multiplex as multiplex
from sshpilot.connection_dialog_multiplex import DEFAULT, OFF, ON, mode_of


def test_the_block_s_control_master_reads_as_the_choice():
    assert mode_of(None) == DEFAULT
    assert mode_of("  ") == DEFAULT
    assert mode_of("no") == OFF
    assert mode_of("No") == OFF
    for value in ("auto", "yes", "ask", "autoask"):
        assert mode_of(value) == ON


def test_the_socket_directory_is_created_private(tmp_path):
    target = tmp_path / "ssh" / "sockets"
    multiplex.ensure_socket_dir(str(target))
    assert target.is_dir()
    assert (target.stat().st_mode & 0o777) == 0o700


def _real_gtk_available():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


needs_gtk = [
    pytest.mark.integration,
    pytest.mark.skipif(not _real_gtk_available(), reason="needs real libadwaita"),
    pytest.mark.skipif(
        not multiplex.MULTIPLEXING_SUPPORTED, reason="no multiplexing on Windows"
    ),
]


def _open_dialog(extra_ssh_config):
    from tests.test_session_type_dialog_sync import _open_dialog as open_dialog

    return open_dialog(extra_ssh_config)


def _save(dialog):
    from tests.test_session_type_dialog_sync import _save as save

    return save(dialog)


def _lines(dialog):
    return [
        line.strip().lower()
        for line in dialog.advanced_tab.get_extra_ssh_config().splitlines()
        if line.strip().lower().startswith("control")
    ]


def _gtk(test):
    for mark in needs_gtk:
        test = mark(test)
    return test


@_gtk
def test_the_row_shows_what_the_block_sets():
    for extra, mode in (
        ("", DEFAULT),
        ("controlmaster auto\ncontrolpath ~/x/%C", ON),
        ("controlmaster no", OFF),
    ):
        dialog = _open_dialog(extra)
        try:
            assert dialog.multiplex_row.get_selected() == mode, extra
        finally:
            dialog.close()


@_gtk
def test_each_choice_writes_its_directives():
    dialog = _open_dialog("sessiontype none")
    try:
        dialog.multiplex_row.set_selected(ON)
        assert _lines(dialog) == [
            "controlmaster auto",
            "controlpath ~/.ssh/sockets/%c",
            f"controlpersist {multiplex.CONTROL_PERSIST}",
        ]
        dialog.multiplex_row.set_selected(OFF)
        assert sorted(_lines(dialog)) == ["controlmaster no", "controlpath none"]
        dialog.multiplex_row.set_selected(ON)
        # Off's "none" is replaced; a path is needed to share.
        assert "controlpath ~/.ssh/sockets/%c" in _lines(dialog)
        dialog.multiplex_row.set_selected(DEFAULT)
        assert _lines(dialog) == []
        # The sibling switch over the same list is untouched.
        assert dialog.port_forwarding_only_row.get_active() is True
    finally:
        dialog.close()


@_gtk
def test_on_keeps_a_path_and_persist_the_block_already_sets():
    dialog = _open_dialog("controlpath ~/cm/%r@%h\ncontrolpersist 10m")
    try:
        assert dialog.multiplex_row.get_selected() == DEFAULT
        dialog.multiplex_row.set_selected(ON)
        assert sorted(_lines(dialog)) == [
            "controlmaster auto", "controlpath ~/cm/%r@%h", "controlpersist 10m",
        ]
    finally:
        dialog.close()


@_gtk
def test_the_choice_survives_save_and_creates_the_socket_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    dialog = _open_dialog("")
    try:
        dialog.multiplex_row.set_selected(ON)
        saved = _save(dialog)
    finally:
        dialog.close()
    assert "controlmaster auto" in saved["extra_ssh_config"].lower()
    sockets = tmp_path / ".ssh" / "sockets"
    assert sockets.is_dir()
    assert (os.stat(sockets).st_mode & 0o777) == 0o700

    reopened = _open_dialog(saved["extra_ssh_config"])
    try:
        assert reopened.multiplex_row.get_selected() == ON
    finally:
        reopened.close()


@_gtk
def test_the_row_follows_edits_on_the_extra_options_page():
    dialog = _open_dialog("")
    try:
        dialog.advanced_tab.set_option("ControlMaster", "no")
        assert dialog.multiplex_row.get_selected() == OFF
        dialog.advanced_tab.remove_option("ControlMaster")
        assert dialog.multiplex_row.get_selected() == DEFAULT
    finally:
        dialog.close()
