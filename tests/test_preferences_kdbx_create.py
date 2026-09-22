"""After creating a KeePass database, Preferences must not re-lock it (GH #1281)."""

from sshpilot.preferences import PreferencesWindow


class _StubRow:
    """Minimal EntryRow: ``set_text`` fires the ``changed`` handler unless blocked."""

    def __init__(self, on_changed):
        self._on_changed = on_changed
        self._blocked = 0
        self.text = ""

    def handler_block(self, _handler_id):
        self._blocked += 1

    def handler_unblock(self, _handler_id):
        self._blocked -= 1

    def set_text(self, text):
        self.text = text
        if not self._blocked:
            self._on_changed(self)


def test_after_create_sets_row_without_relocking():
    changed = []
    prefs = PreferencesWindow.__new__(PreferencesWindow)
    prefs.kdbx_db_row = _StubRow(changed.append)
    prefs._kdbx_db_changed_id = 1
    prefs._kdbx_message = lambda *_args: None
    closed = []

    PreferencesWindow._after_create_kdbx(
        prefs, "/home/u/new.kdbx", True, "", False, lambda: closed.append(True))

    assert prefs.kdbx_db_row.text == "/home/u/new.kdbx"
    assert changed == []  # on_kdbx_database_changed (persist + lock) never ran
    assert prefs.kdbx_db_row._blocked == 0
    assert closed == [True]
