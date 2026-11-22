"""Picking a key in Automatic mode switches the dialog to specific keys."""

import types

from sshpilot.connection_dialog import ConnectionDialog


class _Combo:
    def __init__(self, selected=0):
        self.selected = selected

    def get_selected(self):
        return self.selected

    def set_selected(self, value):
        self.selected = value


class _Widget:
    def __init__(self):
        self.sensitive = True

    def set_sensitive(self, value):
        self.sensitive = value


def _editor(paths):
    return types.SimpleNamespace(get_paths=lambda: list(paths))


def _dialog(mode=0, locked=()):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_select_row = _Combo(mode)
    dialog.key_only_row = _Widget()
    dialog._profile_locked_fields = set(locked)
    return dialog


def test_adding_a_key_in_automatic_switches_to_specific():
    dialog = _dialog(mode=0)
    dialog._on_key_files_changed(_editor(["~/.ssh/termux"]))
    assert dialog.key_select_row.get_selected() == 1


def test_removing_the_last_key_keeps_the_mode():
    dialog = _dialog(mode=0)
    dialog._on_key_files_changed(_editor([]))
    assert dialog.key_select_row.get_selected() == 0


def test_only_selected_keys_follows_the_mode():
    dialog = _dialog(mode=0)
    dialog.on_key_select_changed()
    assert dialog.key_only_row.sensitive is False
    dialog.key_select_row.set_selected(1)
    dialog.on_key_select_changed()
    assert dialog.key_only_row.sensitive is True


def test_profile_lock_keeps_only_selected_keys_locked():
    dialog = _dialog(mode=1, locked={"key_select_mode"})
    dialog.on_key_select_changed()
    assert dialog.key_only_row.sensitive is False
