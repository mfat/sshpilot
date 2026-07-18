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
    # Entry signals only validate. Backend I/O is deferred to the save worker.
    import sshpilot.secret_storage as ss
    from sshpilot.connection_dialog import FileListEditor

    ed = FileListEditor.__new__(FileListEditor)
    ed._with_passphrase = True
    ed._verify = None
    cm = DummyConnectionManager()
    ed._connection_manager = cm
    entry = DummyEntry('secret')
    ed._rows = [types.SimpleNamespace(
        _pass_entry=entry, _pass_path='/k', _pass_norm='/k', _pass_initial='')]

    sm = ss.get_secret_manager()
    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: True)

    ed._commit_passphrase(entry, '/k', '/k')          # locked -> deferred
    assert cm.stored == {}
    assert ed.has_pending_passphrases() is True

    assert ed.pending_passphrase_operations() == [('store', '/k', 'secret')]
    assert cm.stored == {}


def test_filelisteditor_defers_passphrase_when_unlocked(monkeypatch):
    import sshpilot.secret_storage as ss
    from sshpilot.connection_dialog import FileListEditor

    ed = FileListEditor.__new__(FileListEditor)
    ed._with_passphrase = True
    ed._verify = None
    cm = DummyConnectionManager()
    ed._connection_manager = cm
    entry = DummyEntry('secret')
    ed._rows = [types.SimpleNamespace(
        _pass_entry=entry, _pass_path='/k', _pass_norm='/k', _pass_initial='')]

    sm = ss.get_secret_manager()
    monkeypatch.setattr(sm, 'selected_needs_unlock', lambda: False)

    ed._commit_passphrase(entry, '/k', '/k')
    assert cm.stored == {}
    assert ed.pending_passphrase_operations() == [('store', '/k', 'secret')]


def test_connection_secret_save_runs_backend_io_in_worker(monkeypatch):
    import sshpilot.connection_dialog as dialog_module
    import sshpilot.secret_storage as ss
    import sshpilot.secret_unlock_dialog as unlock_dialog

    calls = []

    class Manager(DummyConnectionManager):
        def store_connection_password(self, connection, password, username=None,
                                      previous_connection=None):
            calls.append(('password', connection['hostname'], username, password))
            return True

        def store_key_passphrase(self, key_path, value):
            calls.append((key_path, value))
            return True

    class Spinner:
        def connect(self, signal, callback):
            assert signal == 'closed'
            self.callback = callback

    spinner = Spinner()
    monkeypatch.setattr(
        unlock_dialog,
        '_spinner_dialog',
        lambda *_args: (lambda _text: None, lambda: spinner.callback(), spinner),
    )
    monkeypatch.setattr(
        ss.get_secret_manager(),
        'selected_backend',
        lambda: types.SimpleNamespace(name='bitwarden'),
    )
    monkeypatch.setattr(
        dialog_module.GLib,
        'idle_add',
        lambda callback, *args: callback(*args),
    )

    pending_threads = []

    class DeferredThread:
        def __init__(self, target, daemon=False):
            self.target = target
            self.daemon = daemon

        def start(self):
            pending_threads.append(self)

    monkeypatch.setattr(dialog_module.threading, 'Thread', DeferredThread)

    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.connection_manager = Manager()
    dialog.key_editor = types.SimpleNamespace(
        pending_passphrase_operations=lambda: [('store', '/key', 'key-secret')])
    dialog._save_buttons = []
    emitted = []
    closed = []

    def emit(signal, data):
        emitted.append((signal, dict(data)))
        data.pop('__save_completion')(True)

    dialog.emit = emit
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: calls.append(('error', message))

    data = {
        'hostname': 'example.com',
        'nickname': 'example',
        'username': 'demo',
        'password': 'host-secret',
        'password_changed': True,
    }
    dialog._store_secrets_then_save(data)

    assert len(pending_threads) == 1
    assert pending_threads[0].daemon is True
    assert calls == []
    assert emitted[0][0] == 'connection-saved'
    assert emitted[0][1]['__secret_storage_done'] is True
    assert closed == []

    pending_threads[0].target()

    assert calls == [
        ('password', 'example.com', 'demo', 'host-secret'),
        ('/key', 'key-secret'),
    ]
    assert closed == [True]


def test_deleting_unstored_password_is_not_an_error(monkeypatch):
    # A new connection saved with an empty password queues a delete; nothing
    # stored to delete is the desired end state, not a storage failure.
    import sshpilot.connection_dialog as dialog_module
    import sshpilot.secret_storage as ss
    import sshpilot.secret_unlock_dialog as unlock_dialog

    class Manager(DummyConnectionManager):
        def delete_connection_passwords(self, connection, username=None):
            return False  # nothing was stored

    class Spinner:
        def connect(self, signal, callback):
            self.callback = callback

    spinner = Spinner()
    monkeypatch.setattr(
        unlock_dialog,
        '_spinner_dialog',
        lambda *_args: (lambda _text: None, lambda: spinner.callback(), spinner),
    )
    monkeypatch.setattr(
        ss.get_secret_manager(),
        'selected_backend',
        lambda: types.SimpleNamespace(name='bitwarden'),
    )
    monkeypatch.setattr(
        dialog_module.GLib,
        'idle_add',
        lambda callback, *args: callback(*args),
    )

    class InlineThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(dialog_module.threading, 'Thread', InlineThread)

    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.connection_manager = Manager()
    dialog.key_editor = None
    dialog._save_buttons = []
    closed = []
    errors = []

    dialog.emit = lambda signal, data: data.pop('__save_completion')(True)
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: errors.append(message)

    dialog._store_secrets_then_save({
        'hostname': 'example.com',
        'nickname': 'example',
        'username': 'demo',
        'password': '',
        'password_changed': True,
    })

    assert errors == []
    assert closed == [True]


def test_has_pending_passphrases_detects_cleared_entry():
    # A cleared passphrase (initial non-empty, entry now empty) is a pending
    # delete and must count as a change, so the unlock gate fires for it.
    from sshpilot.connection_dialog import FileListEditor

    ed = FileListEditor.__new__(FileListEditor)
    ed._with_passphrase = True
    ed._rows = [types.SimpleNamespace(
        _pass_entry=DummyEntry(''), _pass_path='/k', _pass_norm='/k',
        _pass_initial='secret')]
    assert ed.has_pending_passphrases() is True

    ed._rows[0]._pass_entry.set_text('secret')  # back to original -> no change
    assert ed.has_pending_passphrases() is False


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

    # Clearing a stored password is a vault delete -> must unlock first.
    assert dialog._needs_secret_unlock_before_save(
        {'password': '', 'password_changed': True}) is True

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
