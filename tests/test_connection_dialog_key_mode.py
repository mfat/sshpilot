"""The key rows edit the Host block directly; there is no key selection mode.

The identity file list and "Only offer listed identities" (IdentitiesOnly:
Default / Yes / No) are always editable; only a linked login profile that
sets one of them locks its row. The stored ``key_select_mode`` is derived
from them, so the writer, the daemon and login profiles read it unchanged.
"""

import pytest

from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.connection_dialog_login_profile import PROFILE_FIELD_ROWS

ROWS = ("key_editor", "cert_editor", "key_only_row")


class _Combo:
    def __init__(self, selected=0):
        self.selected = selected
        self.sensitive = True

    def set_sensitive(self, value):
        self.sensitive = value

    def set_tooltip_text(self, _text):
        pass

    def get_selected(self):
        return self.selected

    def set_selected(self, value):
        self.selected = value


class _Widget:
    def __init__(self):
        self.sensitive = True

    def set_sensitive(self, value):
        self.sensitive = value

    def set_tooltip_text(self, _text):
        pass


class _Editor(_Widget):
    def __init__(self, paths=()):
        super().__init__()
        self.paths = list(paths)

    def get_paths(self):
        return list(self.paths)


def _dialog(identities_only=0, paths=()):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_only_row = _Combo(identities_only)
    dialog.key_editor = _Editor(paths)
    dialog.cert_editor = _Widget()
    return dialog


def _sensitivity(dialog):
    return {name: getattr(dialog, name).sensitive for name in ROWS}


@pytest.mark.parametrize("choice, paths, mode, value", [
    (0, [], 0, ""),                   # nothing set
    (0, ["~/.ssh/work"], 2, ""),      # keys listed
    (1, [], 1, "yes"),                # IdentitiesOnly yes, no key of its own
    (1, ["~/.ssh/work"], 1, "yes"),
    (2, [], 0, "no"),
    (2, ["~/.ssh/work"], 2, "no"),
])
def test_key_mode_is_derived_from_the_list_and_identities_only(choice, paths, mode, value):
    dialog = _dialog(choice, paths)
    assert dialog._selected_identities_only() == value
    assert dialog._selected_key_mode() == mode


def test_every_key_row_is_editable_without_a_profile():
    dialog = _dialog()
    dialog.on_key_select_changed()
    assert _sensitivity(dialog) == dict.fromkeys(ROWS, True)


def test_a_linked_profile_locks_only_the_rows_it_sets():
    dialog = _dialog()
    dialog._profile_locked_fields = {"identity_files"}
    dialog.on_key_select_changed()
    assert _sensitivity(dialog) == {
        "key_editor": False, "cert_editor": True, "key_only_row": True,
    }


def test_unlinking_a_profile_unlocks_the_key_rows():
    dialog = _dialog()
    for names in PROFILE_FIELD_ROWS.values():
        for name in names:
            if not hasattr(dialog, name):
                setattr(dialog, name, _Widget())
    dialog.auth_policy = None
    dialog._apply_login_profile_locks(None)
    assert _sensitivity(dialog) == dict.fromkeys(ROWS, True)
