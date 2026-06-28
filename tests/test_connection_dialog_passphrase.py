import types

from sshpilot.connection_dialog import ConnectionDialog


class DummyEntry:
    def __init__(self, text=""):
        self._text = text

    def set_text(self, text):
        self._text = text

    def get_text(self):
        return self._text

    def set_visible(self, *_args, **_kwargs):
        return None

    def set_sensitive(self, *_args, **_kwargs):
        return None

    def add_css_class(self, *_args, **_kwargs):
        return None

    def remove_css_class(self, *_args, **_kwargs):
        return None


class DummySubtitleRow(DummyEntry):
    def __init__(self, text=""):
        super().__init__(text)
        self._subtitle = ""

    def set_subtitle(self, value):
        self._subtitle = value

    def get_subtitle(self):
        return self._subtitle


class DummyToggle:
    def __init__(self, active=False):
        self._active = bool(active)

    def set_active(self, value):
        self._active = bool(value)

    def get_active(self):
        return self._active

    def set_visible(self, *_args, **_kwargs):
        return None

    def set_sensitive(self, *_args, **_kwargs):
        return None


class DummyCombo:
    def __init__(self, selected=0):
        self._selected = selected

    def get_selected(self):
        return self._selected

    def set_selected(self, value):
        self._selected = value

    def set_sensitive(self, *_args, **_kwargs):
        return None

    def set_visible(self, *_args, **_kwargs):
        return None

    def connect(self, *_args, **_kwargs):
        return None

    def get_model(self):
        return None


class DummyButton:
    def set_sensitive(self, *_args, **_kwargs):
        return None

    def connect(self, *_args, **_kwargs):
        return None

    def add_css_class(self, *_args, **_kwargs):
        return None


class DummyAdvancedTab:
    def get_extra_ssh_config(self):
        return ""

    def set_extra_ssh_config(self, *_args, **_kwargs):
        return None

    def update_config_preview(self):
        return None


class DummyConnectionManager:
    def __init__(self):
        self.stored = {}

    def get_key_passphrase(self, key_path):
        return self.stored.get(key_path)

    def store_key_passphrase(self, key_path, value):
        self.stored[key_path] = value

    def delete_key_passphrase(self, key_path):
        self.stored.pop(key_path, None)


def _build_dialog_with_passphrase():
    dialog = ConnectionDialog.__new__(ConnectionDialog)

    connection = types.SimpleNamespace(
        nickname="example",
        hostname="example.com",
        username="demo",
        port=22,
        keyfile="/home/demo/.ssh/id_ed25519",
        key_passphrase="existing-secret",
        password="",
        proxy_jump=[],
        forward_agent=False,
        forwarding_rules=[],
        aliases=[],
        data={},
    )

    manager = DummyConnectionManager()

    dialog.connection = connection
    dialog.is_editing = True
    dialog.connection_manager = manager
    dialog.parent_window = types.SimpleNamespace(connection_manager=manager)
    dialog.validator = types.SimpleNamespace(verify_key_passphrase=lambda *_args: True)

    dialog.nickname_row = DummyEntry(connection.nickname)
    dialog.hostname_row = DummyEntry(connection.hostname)
    dialog.username_row = DummyEntry(connection.username)
    dialog.port_row = DummyEntry(str(connection.port))
    dialog.proxy_jump_row = DummyEntry("")
    dialog.forward_agent_row = DummyToggle(False)
    dialog.auth_method_row = DummyCombo(0)
    dialog.keyfile_row = DummySubtitleRow()
    dialog.keyfile_row.set_subtitle(connection.keyfile)
    dialog.keyfile_btn = DummyButton()
    dialog.key_dropdown = DummyCombo(0)
    dialog._key_paths = [connection.keyfile]
    dialog.key_select_row = DummyCombo(1)
    dialog.key_only_row = DummyToggle(True)
    dialog.key_passphrase_row = DummyEntry(connection.key_passphrase)
    dialog.password_row = DummyEntry("")
    dialog.pubkey_auth_row = DummyToggle(False)
    dialog.certificate_row = DummySubtitleRow()
    dialog.cert_dropdown = DummyCombo(0)
    dialog._cert_paths = []
    dialog.x11_row = DummyToggle(False)
    dialog.local_command_row = DummyEntry("")
    dialog.remote_command_row = DummyEntry("")
    dialog.forwarding_rules = []
    dialog.advanced_tab = DummyAdvancedTab()

    dialog._orig_password = dialog.password_row.get_text()
    dialog._selected_keyfile_path = connection.keyfile
    dialog._active_key_path = connection.keyfile
    dialog._save_buttons = []

    def _show_error(message):
        raise AssertionError(f"Unexpected error: {message}")

    dialog.show_error = _show_error
    dialog._validate_all_required_for_save = lambda: None
    dialog._focus_row = lambda *_args, **_kwargs: None
    dialog.emit = lambda *_args, **_kwargs: None
    dialog.close = lambda: None

    return dialog, manager, connection


def test_edit_connection_retains_passphrase_without_keyring():
    dialog, manager, connection = _build_dialog_with_passphrase()

    dialog._loading_connection_data = True
    dialog.on_key_select_changed(dialog.key_select_row, None)
    assert dialog.key_passphrase_row.get_text() == "existing-secret"

    dialog._loading_connection_data = False
    dialog.on_save_clicked()

    # Per-key passphrases are persisted to the keyring as the user edits each key
    # row (see ConnectionDialog._commit_passphrase); on_save_clicked no longer
    # mirrors the passphrase into connection.data nor re-stores it. Saving must
    # retain the passphrase that was loaded into the editor.
    assert dialog.key_passphrase_row.get_text() == "existing-secret"


def test_filelisteditor_defers_passphrase_when_vault_locked(monkeypatch):
    # With a locked session vault, committing a passphrase must NOT store (and fail
    # silently); it stays pending and is flushed after the save-flow unlock.
    import sshpilot.secret_storage as ss
    from sshpilot.connection_dialog import FileListEditor

    ed = FileListEditor.__new__(FileListEditor)
    ed._with_passphrase = True
    ed._verify = None
    cm = DummyConnectionManager()
    ed._connection_manager = cm
    entry = DummyEntry('secret')
    ed._rows = [types.SimpleNamespace(_pass_entry=entry, _pass_path='/k', _pass_norm='/k')]

    sm = ss.get_secret_manager()
    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: True)

    ed._commit_passphrase(entry, '/k', '/k')          # locked -> deferred
    assert cm.stored == {}
    assert ed.has_pending_passphrases() is True

    ed.flush_passphrases()                             # after unlock
    assert cm.stored == {'/k': 'secret'}


def test_filelisteditor_stores_passphrase_when_unlocked(monkeypatch):
    import sshpilot.secret_storage as ss
    from sshpilot.connection_dialog import FileListEditor

    ed = FileListEditor.__new__(FileListEditor)
    ed._with_passphrase = True
    ed._verify = None
    cm = DummyConnectionManager()
    ed._connection_manager = cm
    ed._rows = []

    sm = ss.get_secret_manager()
    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: False)

    ed._commit_passphrase(DummyEntry('secret'), '/k', '/k')   # unlocked -> stored now
    assert cm.stored == {'/k': 'secret'}


def test_save_gate_detects_pending_passphrase_when_locked(monkeypatch):
    import sshpilot.secret_storage as ss

    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: True)
    sm = ss.get_secret_manager()

    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: True)
    assert dialog._needs_secret_unlock_before_save({'password': ''}) is True   # passphrase
    assert dialog._needs_secret_unlock_before_save({'password': 'p'}) is True  # password

    # No pending passphrase and no password -> no prompt even when locked.
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: False)
    assert dialog._needs_secret_unlock_before_save({'password': ''}) is False

    # Unlocked -> never needs a prompt.
    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: False)
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: True)
    assert dialog._needs_secret_unlock_before_save({'password': 'p'}) is False


def test_rule_editor_remote_to_local_resets_host_to_localhost():
    dialog = ConnectionDialog.__new__(ConnectionDialog)

    listen_addr_row = DummyEntry("")
    listen_port_row = DummyEntry("1433")
    remote_host_row = DummyEntry("10.20.30.40")
    remote_port_row = DummyEntry("1433")

    # Simulate changing the editor type from Remote (1) to Local (0).
    dialog._apply_rule_editor_defaults_for_type(
        0,
        listen_addr_row,
        listen_port_row,
        remote_host_row,
        remote_port_row,
        1,
    )

    assert remote_host_row.get_text() == "localhost"
