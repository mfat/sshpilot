"""The specific-key rows are inactive in Automatic key selection."""

from sshpilot.connection_dialog import ConnectionDialog
from sshpilot.connection_dialog_login_profile import PROFILE_FIELD_ROWS

ROWS = ("key_editor", "cert_editor", "key_only_row")


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

    def set_tooltip_text(self, _text):
        pass


def _dialog(mode=0):
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_select_row = _Combo(mode)
    for name in ROWS:
        setattr(dialog, name, _Widget())
    return dialog


def _sensitivity(dialog):
    return {name: getattr(dialog, name).sensitive for name in ROWS}


def test_automatic_disables_keys_and_certificates():
    dialog = _dialog(mode=0)
    dialog.on_key_select_changed()
    assert _sensitivity(dialog) == dict.fromkeys(ROWS, False)


def test_specific_keys_enables_them():
    dialog = _dialog(mode=1)
    dialog.on_key_select_changed()
    assert _sensitivity(dialog) == dict.fromkeys(ROWS, True)


def test_profile_lock_keeps_its_rows_locked_in_specific_mode():
    dialog = _dialog(mode=1)
    dialog._profile_locked_fields = {"identity_files"}
    dialog.on_key_select_changed()
    assert _sensitivity(dialog) == {
        "key_editor": False, "cert_editor": True, "key_only_row": True,
    }


def test_unlinking_a_profile_does_not_enable_keys_in_automatic():
    """The profile pass used to re-enable every row it did not lock."""
    dialog = _dialog(mode=0)
    for field, names in PROFILE_FIELD_ROWS.items():
        for name in names:
            if not hasattr(dialog, name):
                setattr(dialog, name, _Widget())
    dialog.auth_policy = None
    dialog._apply_login_profile_locks(None)
    assert _sensitivity(dialog) == dict.fromkeys(ROWS, False)
