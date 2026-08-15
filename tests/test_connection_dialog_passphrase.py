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
    import sshpilot.secret_unlock_dialog as unlock_dialog

    calls = []

    class Client:
        def get_capabilities(self):
            return types.SimpleNamespace(supports=lambda _capability: True)

        def store_connection_password(self, request):
            calls.append(('password', request.password))
            return True

    class KeyManager:
        def store_key_passphrase(self, key_path, value):
            calls.append((key_path, bytes(value)))
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
    dialog.parent_window = types.SimpleNamespace(
        client=Client(),
        client_bridge=object(),
        key_manager=KeyManager(),
        secrets_controller=types.SimpleNamespace(
            load_state=lambda: types.SimpleNamespace(
                selected_backend='bitwarden', needs_unlock=False, login_required=False
            )
        ),
        _daemon_ready=lambda: True,
    )
    dialog.key_editor = types.SimpleNamespace(
        pending_passphrase_operations=lambda: [('store', '/key', 'key-secret')])
    dialog._save_mutation_result = types.SimpleNamespace(connection_id='conn-1')
    dialog._save_buttons = []
    emitted = []
    closed = []

    def emit(signal, data, metadata, secret_plan, completion):
        emitted.append((signal, dict(data), dict(metadata), dict(secret_plan)))
        completion(True)

    dialog.emit = emit
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: calls.append(('error', message))

    data = {
        'hostname': 'example.com',
        'nickname': 'example',
        'username': 'demo',
        '__secret_plan': {
            'password': 'host-secret',
            'password_changed': True,
            'passphrase_operations': [('store', '/key', 'key-secret')],
        }
    }
    secret_plan = data.pop('__secret_plan')
    dialog._store_secrets_then_save(data, {}, secret_plan)

    assert len(pending_threads) == 1
    assert pending_threads[0].daemon is True
    assert calls == []
    assert emitted[0][0] == 'connection-saved'
    assert emitted[0][1]['__secret_storage_done'] is True
    assert '__secret_plan' not in emitted[0][1]
    assert emitted[0][3]['password'] == 'host-secret'
    assert closed == []

    pending_threads[0].target()

    assert calls == [('password', 'host-secret'), ('/key', b'key-secret')]
    assert closed == [True]


def test_daemon_key_passphrase_save_uses_protected_key_manager(monkeypatch):
    import sshpilot.connection_dialog as dialog_module
    import sshpilot.secret_storage as ss
    import sshpilot.secret_unlock_dialog as unlock_dialog

    sentinel = "KEY_PASSPHRASE_SENTINEL_8F1C29"
    received = []

    class KeyManager:
        def store_key_passphrase(self, key_path, secret):
            received.append((key_path, bytes(secret)))
            secret[:] = b"\0" * len(secret)
            secret.clear()
            return True

    class Client:
        def get_capabilities(self):
            return types.SimpleNamespace(supports=lambda _capability: True)

        def store_connection_password(self, _request):
            return True

    class Spinner:
        def connect(self, _signal, callback):
            self.callback = callback

    spinner = Spinner()
    monkeypatch.setattr(
        unlock_dialog,
        "_spinner_dialog",
        lambda *_args: (lambda _text: None, lambda: spinner.callback(), spinner),
    )
    monkeypatch.setattr(
        ss.get_secret_manager(),
        "selected_backend",
        lambda: types.SimpleNamespace(name="keyring"),
    )
    monkeypatch.setattr(
        dialog_module.GLib,
        "idle_add",
        lambda callback, *args: callback(*args),
    )

    class InlineThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(dialog_module.threading, "Thread", InlineThread)

    parent = types.SimpleNamespace(
        client=Client(),
        client_bridge=object(),
        key_manager=KeyManager(),
        _daemon_ready=lambda: True,
    )
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.parent_window = parent
    dialog.connection_manager = DummyConnectionManager()
    dialog._save_buttons = []
    closed = []
    errors = []
    dialog.emit = (
        lambda _signal, _data, _metadata, _secrets, completion: completion(True)
    )
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: errors.append(message)

    dialog._store_secrets_then_save(
        {"protocol": "ssh", "hostname": "example.test"},
        {},
        {
            "passphrase_operations": [
                ("store", "/home/user/.ssh/id", sentinel)
            ]
        },
    )

    assert received == [
        ("/home/user/.ssh/id", sentinel.encode("utf-8"))
    ]
    assert errors == []
    assert closed == [True]


def test_deleting_unstored_password_is_not_an_error(monkeypatch):
    # A new connection saved with an empty password queues a delete; nothing
    # stored to delete is the desired end state, not a storage failure.
    import sshpilot.connection_dialog as dialog_module
    import sshpilot.secret_unlock_dialog as unlock_dialog

    class Client:
        def get_capabilities(self):
            return types.SimpleNamespace(supports=lambda _capability: True)

        def store_connection_password(self, _request):
            return True

        def delete_connection_password(self, _request):
            return True

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
    dialog.parent_window = types.SimpleNamespace(
        client=Client(),
        client_bridge=object(),
        secrets_controller=types.SimpleNamespace(
            load_state=lambda: types.SimpleNamespace(
                selected_backend='bitwarden', needs_unlock=False, login_required=False
            )
        ),
        _daemon_ready=lambda: True,
    )
    dialog._save_mutation_result = types.SimpleNamespace(connection_id='conn-1')
    dialog.key_editor = None
    dialog._save_buttons = []
    closed = []
    errors = []

    dialog.emit = lambda signal, data, metadata, secrets, completion: completion(True)
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: errors.append(message)

    dialog._store_secrets_then_save({
        'hostname': 'example.com',
        'nickname': 'example',
        'username': 'demo',
    }, {}, {'password': '', 'password_changed': True})

    assert errors == []
    assert closed == [True]


def test_no_secret_daemon_save_waits_for_explicit_async_claim():
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.connection_manager = DummyConnectionManager()
    dialog._save_buttons = []
    completions = []
    closed = []
    errors = []

    def emit(_signal, _data, _metadata, _secrets, request):
        request.claim()
        completions.append(request)

    dialog.emit = emit
    dialog.close = lambda: closed.append(True)
    dialog.show_error = lambda message: errors.append(message)

    dialog._store_secrets_then_save(
        {'nickname': 'demo', 'hostname': 'demo.example', 'username': 'alice'},
        {}, {},
    )

    assert closed == []
    assert len(completions) == 1
    completions[0](False)
    assert closed == []
    assert len(errors) == 1


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
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: True)
    state = types.SimpleNamespace(needs_unlock=True, login_required=False)
    dialog.parent_window = types.SimpleNamespace(
        secrets_controller=types.SimpleNamespace(load_state=lambda: state)
    )
    assert dialog._needs_secret_unlock_before_save({'password': ''}) is True   # passphrase
    assert dialog._needs_secret_unlock_before_save({'password': 'p'}) is True  # password

    # No pending passphrase and no password -> no prompt even when locked.
    dialog.key_editor = types.SimpleNamespace(has_pending_passphrases=lambda: False)
    assert dialog._needs_secret_unlock_before_save({'password': ''}) is False

    # Clearing a stored password is a vault delete -> must unlock first.
    assert dialog._needs_secret_unlock_before_save(
        {'password': '', 'password_changed': True}) is True

    # Unlocked -> never needs a prompt.
    state.needs_unlock = False
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


def test_daemon_editor_loads_password_from_protected_reveal(monkeypatch):
    import sshpilot.connection_dialog as dialog_module

    class DaemonClient:
        def __init__(self):
            self.reveal_id = None

        def reveal_connection_password(self, connection_id):
            self.reveal_id = connection_id
            return bytearray(b"hunter2")

    class Bridge:
        pass

    client = DaemonClient()
    parent = types.SimpleNamespace(
        connection_manager=object(),  # read-only projection, no secret access
        client=client,
        client_bridge=Bridge(),
        _daemon_ready=lambda: True,
    )

    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.parent_window = parent
    dialog.connection = types.SimpleNamespace(
        nickname="web", username="root",
    )
    dialog.password_row = DummyEntry("")

    idle_calls = []
    pending_threads = []

    class DeferredThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            pending_threads.append(self)

    monkeypatch.setattr(dialog_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        dialog_module.GLib,
        "idle_add",
        lambda callback, *args: idle_calls.append((callback, args)),
    )

    dialog._load_password_async()

    assert len(pending_threads) == 1
    pending_threads[0].target()
    assert client.reveal_id == "web"
    assert idle_calls
    callback, (password,) = idle_calls[0]
    callback(password)
    assert dialog.password_row.get_text() == "hunter2"
    assert dialog._password_saved is True
    assert dialog._orig_password == "hunter2"


def test_unavailable_editor_does_not_read_password_from_projection_manager():
    class Manager:
        def get_connection_password(self, _connection):
            raise AssertionError("frontend manager secret lookup is obsolete")

    parent = types.SimpleNamespace(
        connection_manager=Manager(),
        client=None,
        client_bridge=None,
        _daemon_ready=lambda: False,
    )
    dialog = ConnectionDialog.__new__(ConnectionDialog)
    dialog.parent_window = parent
    dialog.connection = types.SimpleNamespace(nickname="web", username="root")
    dialog.password_row = DummyEntry("")

    dialog._load_password_async()

    assert dialog.password_row.get_text() == ""


def test_daemon_editor_loads_passphrase_from_protected_reveal(monkeypatch):
    import sshpilot.connection_dialog as dialog_module
    from sshpilot.connection_dialog import FileListEditor

    class DaemonClient:
        def __init__(self):
            self.reveal_key = None

        def reveal_key_passphrase(self, key_path):
            self.reveal_key = key_path
            return bytearray(b"key-secret")

    class Bridge:
        pass

    client = DaemonClient()
    parent = types.SimpleNamespace(
        connection_manager=object(),  # read-only projection, no secret access
        client=client,
        client_bridge=Bridge(),
        _daemon_ready=lambda: True,
    )

    ed = FileListEditor.__new__(FileListEditor)
    ed._connection_manager = parent.connection_manager
    ed._parent_window = parent

    entry = DummyEntry("")
    row = types.SimpleNamespace(_pass_initial="", _pass_entry=entry)
    norm = "/home/demo/.ssh/id_ed25519"

    idle_calls = []
    pending_threads = []

    class DeferredThread:
        def __init__(self, target, daemon=False):
            self.target = target

        def start(self):
            pending_threads.append(self)

    monkeypatch.setattr(dialog_module.threading, "Thread", DeferredThread)
    monkeypatch.setattr(
        dialog_module.GLib,
        "idle_add",
        lambda callback, *args: idle_calls.append((callback, args)),
    )

    ed._load_passphrase_async(entry, row, norm)

    assert len(pending_threads) == 1
    pending_threads[0].target()
    assert client.reveal_key == norm
    assert idle_calls
    callback, (value,) = idle_calls[0]
    callback(value)
    assert entry.get_text() == "key-secret"
    assert row._pass_initial == "key-secret"
    assert row._pass_saved is True


def test_unavailable_editor_does_not_read_passphrase_from_projection_manager():
    from sshpilot.connection_dialog import FileListEditor

    class Manager:
        def get_key_passphrase(self, _key_path):
            raise AssertionError("frontend manager secret lookup is obsolete")

    parent = types.SimpleNamespace(
        connection_manager=Manager(),
        client=None,
        client_bridge=None,
        _daemon_ready=lambda: False,
    )
    ed = FileListEditor.__new__(FileListEditor)
    ed._connection_manager = parent.connection_manager
    ed._parent_window = parent
    entry = DummyEntry("")
    row = types.SimpleNamespace(_pass_initial="", _pass_entry=entry)

    ed._load_passphrase_async(entry, row, "/home/demo/.ssh/id_ed25519")

    assert entry.get_text() == ""
