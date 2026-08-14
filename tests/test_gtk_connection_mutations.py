import logging
from types import SimpleNamespace

from sshpilot.api import ErrorCode, SshPilotError
from sshpilot.window import MainWindow


class _ControlledBridge:
    def __init__(self):
        self.calls = []

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.calls.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=lambda: None)


class _Client:
    def __init__(self):
        self.created = []
        self.updated = []
        self.deleted = []
        self.editor = None
        self.editor_calls = 0
        self.metadata = []

    def create_connection(self, request):
        self.created.append(request)
        return SimpleNamespace(
            connection_id="demo",
            generation=1
        )

    def update_connection(self, connection_id, request):
        self.updated.append((connection_id, request))
        return SimpleNamespace(
            connection_id="demo",
            generation=2
        )

    def delete_connection(self, request):
        self.deleted.append(request)
        return SimpleNamespace(connection_id=request.connection_id, deleted=True)

    def update_connection_metadata(self, connection_id, metadata):
        self.metadata.append((connection_id, metadata))
        return True

    def get_connection_editor(self, connection_id):
        self.editor_calls += 1
        if isinstance(self.editor, BaseException):
            raise self.editor
        return self.editor


class _Manager:
    def __init__(self):
        self.reloads = 0

    def refresh(self):
        self.reloads += 1


class _ProjectionGroupManager:
    def __init__(self):
        self.bound = []

    def bind_connections(self, connections):
        self.bound.append(connections)


class _ProjectionWindow:
    on_projection_reset = MainWindow.on_projection_reset

    def __init__(self):
        self.group_manager = _ProjectionGroupManager()
        self.rebuilds = 0

    def rebuild_connection_list(self):
        self.rebuilds += 1


class _MutationWindow:
    _daemon_mode_active = MainWindow._daemon_mode_active
    _prompt_reconnect_after_daemon_save = MainWindow._prompt_reconnect_after_daemon_save
    _normalise_daemon_editor_value = staticmethod(
        MainWindow._normalise_daemon_editor_value
    )
    prepare_connection_save_for_client = (
        MainWindow.prepare_connection_save_for_client
    )
    _save_connection_via_client = MainWindow._save_connection_via_client
    on_connection_saved = MainWindow.on_connection_saved
    _delete_connections_via_client = MainWindow._delete_connections_via_client
    _fetch_daemon_editor_generation = MainWindow._fetch_daemon_editor_generation
    _retry_daemon_editor_load = MainWindow._retry_daemon_editor_load
    show_connection_dialog = MainWindow.show_connection_dialog

    def __init__(self):
        self.client = _Client()
        self.client_bridge = _ControlledBridge()
        self.connection_manager = _Manager()
        self._is_quitting = False
        self.rebuilds = 0
        self.disconnected = []
        self.errors = []
        self.config = SimpleNamespace(get_connection_meta=lambda _name: {})
        mode = SimpleNamespace(value="daemon")
        self._app = SimpleNamespace(
            _api_client_selection=SimpleNamespace(
                mode=mode,
                client=self.client,
            )
        )

    def get_application(self):
        return self._app

    def rebuild_connection_list(self):
        self.rebuilds += 1

    def _disconnect_connection_terminals(self, connection):
        self.disconnected.append(connection)

    def _error_dialog(self, title, message):
        self.errors.append((title, message))


def test_noop_daemon_mutation_result_does_not_prompt_reconnect():
    window = _MutationWindow()
    prompts = []
    window.active_terminals = {}
    window.terminal_manager = SimpleNamespace(prompt_reconnect=lambda connection: prompts.append(connection))
    window._prompt_reconnect_after_daemon_save("demo", "demo")
    assert prompts == []


def test_daemon_reconnect_matches_authoritative_connection_id():
    window = _MutationWindow()
    class _ActiveConnection:
        connection_id = "demo"
        nickname = "renamed"

    active = _ActiveConnection()
    terminal = SimpleNamespace(is_connected=True)
    prompts = []
    window.active_terminals = {active: terminal}
    window.terminal_manager = SimpleNamespace(prompt_reconnect=lambda connection: prompts.append(connection))
    window._prompt_reconnect_after_daemon_save("demo", "new-name")
    assert prompts == [active]


def test_daemon_mutation_result_changed_field_controls_prompt():
    from sshpilot.api.models.connections import ConnectionMutationResult

    changed = ConnectionMutationResult("demo", "demo", 2, changed=True, changed_fields=("hostname",))
    unchanged = ConnectionMutationResult("demo", "demo", 2, changed=False)
    assert changed.changed is True
    assert unchanged.changed is False


def test_daemon_mode_detection_matches_current_mode_less_selection():
    window = _MutationWindow()
    window._app._api_client_selection = SimpleNamespace(client=window.client)

    assert window._daemon_mode_active() is True


def _basic_data(**overrides):
    data = {
        "protocol": "ssh",
        "nickname": "new",
        "hostname": "new.example",
        "username": "alice",
        "port": 22,
        "auth_method": 0,
        "keyfile": "",
        "identity_files": [],
        "certificate": "",
        "certificate_files": [],
        "key_select_mode": 0,
        "identity_agent": "",
        "add_keys_to_agent": "",
        "pkcs11_provider": "",
        "security_key_provider": "",
        "password": "",
        "password_changed": False,
        "x11_forwarding": False,
        "pubkey_auth_no": False,
        "proxy_jump": [],
        "forward_agent": False,
        "forwarding_rules": [],
        "pre_command": "",
        "local_command": "",
        "remote_command": "",
        "extra_ssh_config": "",
        "__meta": {
            "wol_mac": "",
            "wol_broadcast_ip": "",
            "wol_port": 9,
            "tags": [],
        },
    }
    data.update(overrides)
    return data


def test_projection_reset_rebinds_groups_and_rebuilds_sidebar_once():
    window = _ProjectionWindow()
    connections = (SimpleNamespace(id="one"), SimpleNamespace(id="two"))
    manager = SimpleNamespace(connections=connections)

    window.on_projection_reset(manager, None)

    assert window.group_manager.bound == [connections]
    assert window.rebuilds == 1


def test_daemon_create_waits_for_success_and_sends_create_dto_with_config_patch():
    window = _MutationWindow()
    completed = []
    data = _basic_data(proxy_jump=["jump.example.test"], __save_completion=lambda ok, *a, **k: completed.append(ok))
    dialog = SimpleNamespace(is_editing=False, connection=None)

    window.on_connection_saved(dialog, data)

    assert window.connection_manager.reloads == 0
    assert window.rebuilds == 0
    assert completed == []
    operation, success, _failure = window.client_bridge.calls[0]

    result = operation()
    assert window.client.created[0].nickname == "new"
    assert window.client.created[0].config_patch["proxy_jump"] == ["jump.example.test"]
    assert not hasattr(window.client.created[0], "password")
    assert completed == []

    success(result)
    assert len(window.client_bridge.calls) == 2
    window.client_bridge.calls[1][1](True)
    assert completed == [True]
    assert window.connection_manager.reloads == 1
    assert window.rebuilds == 1


def test_daemon_mutation_failure_keeps_ui_state_and_uses_safe_completion(caplog):
    window = _MutationWindow()
    completed = []
    data = _basic_data(__save_completion=lambda ok, *a, **k: completed.append(ok))
    dialog = SimpleNamespace(is_editing=False, connection=None)

    window.on_connection_saved(dialog, data)
    _operation, _success, failure = window.client_bridge.calls[0]
    error = SshPilotError(
        ErrorCode.PERSISTENCE_FAILED,
        "The connection state could not be stored",
        details={"field": "nickname"},
    )
    with caplog.at_level(logging.WARNING, logger="sshpilot.window"):
        failure(error)

    assert completed == [False]
    assert window.connection_manager.reloads == 0
    assert window.rebuilds == 0
    assert "persistence_failed" in caplog.text
    assert "The connection state could not be stored" in caplog.text


def test_metadata_retry_resumes_without_repeating_create():
    window = _MutationWindow()
    completed = []
    metadata = {"wol_port": 9, "tags": []}
    dialog = SimpleNamespace(is_editing=False, connection=None)

    window.on_connection_saved(
        dialog, _basic_data(), metadata, {},
        lambda ok, *args, **kwargs: completed.append(ok),
    )
    mutation, mutation_ok, _ = window.client_bridge.calls[0]
    result = mutation()
    mutation_ok(result)
    assert len(window.client.created) == 1

    # Metadata failed after create committed.  Saving again retries metadata
    # from the checkpoint instead of submitting another create operation.
    window.client_bridge.calls[1][2](RuntimeError("metadata unavailable"))
    assert completed == [False]
    window.on_connection_saved(
        dialog, _basic_data(), metadata, {},
        lambda ok, *args, **kwargs: completed.append(ok),
    )
    assert len(window.client_bridge.calls) == 3
    metadata_retry, metadata_ok, _ = window.client_bridge.calls[2]
    metadata_retry()
    metadata_ok(True)

    assert len(window.client.created) == 1
    assert completed == [False, True]


def test_metadata_false_is_failure_and_retry_does_not_repeat_create():
    window = _MutationWindow()
    completed = []
    metadata = {"tags": ["ops"]}
    dialog = SimpleNamespace(is_editing=False, connection=None)

    window.on_connection_saved(
        dialog, _basic_data(), metadata, {},
        lambda ok, result=None, meta_error=None: completed.append(
            (ok, result, meta_error)
        ),
    )
    create, create_ok, _ = window.client_bridge.calls[0]
    create_ok(create())
    window.client_bridge.calls[1][1](False)

    assert completed[0][0] is False
    assert completed[0][1].connection_id == "demo"
    assert completed[0][2]
    assert len(window.client.created) == 1


def test_changed_config_invalidates_partial_save_checkpoint():
    window = _MutationWindow()
    dialog = SimpleNamespace(is_editing=False, connection=None)
    metadata = {"tags": ["ops"]}

    window.on_connection_saved(dialog, _basic_data(), metadata, {}, lambda *_: None)
    create, create_ok, _ = window.client_bridge.calls[0]
    create_ok(create())
    window.client_bridge.calls[1][2](RuntimeError("metadata unavailable"))

    changed = _basic_data(hostname="corrected.example")
    window.on_connection_saved(dialog, changed, metadata, {}, lambda *_: None)
    update_existing = window.client_bridge.calls[2][0]
    update_existing()

    assert len(window.client.created) == 1
    assert len(window.client.updated) == 1
    assert window.client.updated[0][0] == "demo"
    assert window.client.updated[0][1].hostname == "corrected.example"


def test_changed_metadata_invalidates_only_metadata_checkpoint():
    window = _MutationWindow()
    dialog = SimpleNamespace(
        is_editing=False,
        connection=None,
        _resumable_save_active=True,
        _daemon_save_checkpoint=SimpleNamespace(connection_id="demo", generation=1),
        _daemon_config_fingerprint=MainWindow._normalise_daemon_editor_value({
            key: value for key, value in _basic_data().items()
            if not key.startswith('__')
        }),
        _daemon_metadata_saved=True,
        _daemon_metadata_fingerprint=MainWindow._normalise_daemon_editor_value(
            {"tags": ["old"]}
        ),
    )

    window.on_connection_saved(
        dialog, _basic_data(), {"tags": ["new"]}, {}, lambda *_: None
    )

    assert len(window.client.created) == 0
    metadata_operation = window.client_bridge.calls[0][0]
    assert metadata_operation() is True
    assert window.client.metadata == [("demo", {"tags": ["new"]})]


# Secret deletion idempotence is owned by DaemonConnectionSecretProvider and
# covered in tests/daemon/test_connection_secret_provider.py (including the
# non-empty absent key-passphrase path). The application service only delegates
# to the provider and never reinterprets its result.


def test_secret_plan_is_rejected_at_config_persistence_boundary():
    window = _MutationWindow()
    completed = []
    data = _basic_data()
    data["__secret_plan"] = {"password": "must-not-persist"}

    window.on_connection_saved(
        SimpleNamespace(is_editing=False, connection=None), data, {}, {},
        lambda ok: completed.append(ok),
    )

    assert completed == [False]
    assert window.client_bridge.calls == []
    assert window.client.created == []


def test_daemon_editor_save_path_never_sends_password():
    """Secrets never cross the daemon mutation wire.

    Drives the real save path (``on_connection_saved`` → bridge submission)
    with a password present in the collected form data, then asserts the
    password is stripped by ``prepare_connection_save_for_client`` and can
    never appear in the mutation request or its config patch.  Secrets go
    only through the dedicated secret-storage RPCs, never ``connections.*``.
    """
    window = _MutationWindow()
    dialog = _daemon_save_dialog()
    completed = []
    data = _basic_data(
        nickname="demo",
        hostname="demo.example",
        password="do-not-send",
        password_changed=True,
        __save_completion=lambda ok, *a, **k: completed.append(ok),
    )

    # First, the dialog's prepare step must accept the payload and strip the
    # secret before anything reaches the bridge.
    secret_problem = window.prepare_connection_save_for_client(dialog, data)
    assert secret_problem is None
    assert data["password"] == ""

    window.on_connection_saved(dialog, data)

    assert len(window.client_bridge.calls) == 1
    operation, success, _failure = window.client_bridge.calls[0]
    assert completed == []

    operation()
    request = window.client.updated[0][1]

    assert not hasattr(request, "password")
    assert "password" not in request.config_patch
    assert "do-not-send" not in repr(request)
    assert "do-not-send" not in repr(request.config_patch)

    success(SimpleNamespace(connection_id="demo", generation=8))
    assert len(window.client_bridge.calls) == 2
    window.client_bridge.calls[1][1](True)
    assert completed == [True]

    # Exactly two bridge submissions: the mutation and metadata. No secret RPC fired.
    assert len(window.client_bridge.calls) == 2


def test_daemon_editor_allows_config_patch_fields():
    window = _MutationWindow()
    dialog = SimpleNamespace(
        is_editing=False,
        connection=None,
        key_editor=None,
    )

    advanced_problem = window.prepare_connection_save_for_client(
        dialog,
        _basic_data(proxy_jump=["jump.example"]),
    )

    assert advanced_problem is None


def test_daemon_editor_handles_proxy_jump_string_and_list():
    window = _MutationWindow()
    dialog = SimpleNamespace(
        is_editing=True,
        connection=SimpleNamespace(nickname="Oracle", connection_id="Oracle"),
        key_editor=None,
    )

    data = _basic_data(
        nickname="Oracle",
        hostname="150.230.27.23",
        username="ubuntu",
        proxy_jump="ubuntu@150.230.27.23",
    )
    window._save_connection_via_client(dialog, data, lambda ok: None)

    assert len(window.client_bridge.calls) == 1
    operation, _success, _failure = window.client_bridge.calls[0]
    operation()
    updated_req = window.client.updated[0][1]
    assert updated_req.config_patch["proxy_jump"] == ["ubuntu@150.230.27.23"]



def test_daemon_delete_is_not_optimistic_and_disconnects_only_after_success():
    window = _MutationWindow()
    connection = SimpleNamespace(
        nickname="demo",
        protocol="ssh",
        connection_id="demo",
        uuid="demo",
    )

    window._delete_connections_via_client(
        [connection],
        close_terminals=True,
    )

    assert window.disconnected == []
    assert window.connection_manager.reloads == 0
    operation, success, _failure = window.client_bridge.calls[0]
    result = operation()
    assert len(window.client.deleted) == 1
    assert window.disconnected == []

    success(result)
    assert window.disconnected == [connection]
    assert window.connection_manager.reloads == 1
    assert window.rebuilds == 1


def test_daemon_group_move_failure_does_not_mutate_local_group_manager():
    from sshpilot.window import MainWindow

    class GroupTestWindow(_MutationWindow):
        def __init__(self):
            super().__init__()
            self.group_manager = SimpleNamespace(
                moved=[],
                move_connection=lambda nickname, group_id: self.group_manager.moved.append((nickname, group_id)),
            )
            self.connection_manager = SimpleNamespace(
                connections=[SimpleNamespace(nickname="demo", connection_id="demo")],
                load_ssh_config=lambda: None,
            )
            self._find_connection_id_for_nickname = MainWindow._find_connection_id_for_nickname.__get__(self)

    window = GroupTestWindow()
    def failing_assign(connection_id, target_group_id):
        raise SshPilotError(ErrorCode.INTERNAL_ERROR, "RPC error")

    window.client.assign_connection_to_group = failing_assign

    MainWindow.move_connection_to_group(window, "demo", "group-1")

    assert window.group_manager.moved == []


class _CancellableBridge(_ControlledBridge):
    def __init__(self, cancel_cb):
        super().__init__()
        self._cancel_cb = cancel_cb

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.calls.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=self._cancel_cb)


class _EditorDialog:
    """Minimal stand-in for the daemon-editor surface of ConnectionDialog."""

    def __init__(self, source="daemon"):
        self.source = source
        self._daemon_editor_loaded = False
        self._daemon_generation = None
        self._editor_load_failed = False
        self.handlers = []
        self.populated = []

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))

    def is_daemon_editor(self):
        return self.source == "daemon"

    def set_editor_source(self, source):
        self.source = source

    def set_daemon_editor_loading(self):
        self._daemon_editor_loaded = False
        self._daemon_generation = None
        self._editor_load_failed = False

    def set_daemon_editor_load_failed(self):
        self._daemon_editor_loaded = False
        self._daemon_generation = None
        self._editor_load_failed = True

    def set_daemon_editor_loaded(self, generation):
        self._daemon_editor_loaded = True
        self._daemon_generation = generation
        self._editor_load_failed = False

    def populate_from_editor_details(self, details):
        self.populated.append(details)
        self._daemon_editor_loaded = True
        self._daemon_generation = getattr(details, "generation", None)
        self._editor_load_failed = False

    def show_daemon_editor_load_error(self, on_retry=None):
        self.load_error_shown = True
        self.load_error_on_retry = on_retry


def _run_editor_fetch(window, dialog):
    dialog.set_daemon_editor_loading()
    window._fetch_daemon_editor_generation(dialog, SimpleNamespace(nickname="demo"))
    assert len(window.client_bridge.calls) == 1
    operation, success, failure = window.client_bridge.calls[0]
    return operation, success, failure


def test_daemon_editor_success_populates_form_from_dto_and_enables_save():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7, nickname="demo")
    dialog = _EditorDialog()

    _operation, success, _failure = _run_editor_fetch(window, dialog)
    success(window.client.get_connection_editor("demo"))

    assert window.client.editor_calls == 1
    assert dialog.populated == [window.client.editor]
    assert dialog._daemon_generation == 7
    assert dialog._daemon_editor_loaded is True
    assert dialog._editor_load_failed is False


def test_daemon_editor_populates_from_dto_not_stale_local_connection():
    from sshpilot.api.models.connections import (
        AuthenticationMethod,
        ForwardingRule,
    )
    from sshpilot.connection_dialog import _editor_details_to_connection

    window = _MutationWindow()
    dto = SimpleNamespace(
        nickname="demo",
        hostname="dto.example",
        username="alice",
        port=2202,
        protocol="ssh",
        proxy_jump=("jump1", "jump2"),
        forward_agent=True,
        authentication_method=AuthenticationMethod.PASSWORD,
        identity_files=["/keys/id_ed25519"],
        certificate_files=["/keys/cert"],
        key_select_mode=1,
        x11_forwarding=True,
        extra_ssh_config="SetEnv A=1",
        pre_command="echo pre",
        local_command="echo local",
        remote_command="echo remote",
        forwarding_rules=[
            ForwardingRule(
                type="local",
                listen_port=8080,
                listen_addr="127.0.0.1",
                remote_host="app.internal",
                remote_port=80,
                enabled=True,
            ),
            ForwardingRule(
                type="remote",
                listen_port=2222,
                local_host="localhost",
                local_port=22,
                enabled=False,
            ),
        ],
        aliases=("a", "b"),
        source="dto",
        generation=5,
        data={},
    )
    window.client.editor = dto
    dialog = _EditorDialog()

    _operation, success, _failure = _run_editor_fetch(window, dialog)
    success(window.client.get_connection_editor("demo"))

    # The form source is the single daemon DTO, never a local Connection.
    assert dialog.populated == [dto]
    form = _editor_details_to_connection(dto)
    assert form.hostname == "dto.example"
    assert form.proxy_jump == ("jump1", "jump2")
    assert form.keyfile == "/keys/id_ed25519"
    assert form.remote_command == "echo remote"
    assert form.auth_method == 1
    assert form.generation == 5

    # Typed ForwardingRule objects from the DTO are normalized to the dict
    # schema the dialog UI and save handler consume (``rule.get("enabled")``).
    assert form.forwarding_rules == [
        {
            "type": "local",
            "listen_addr": "127.0.0.1",
            "listen_port": 8080,
            "enabled": True,
            "remote_host": "app.internal",
            "remote_port": 80,
        },
        {
            "type": "remote",
            "listen_addr": "",
            "listen_port": 2222,
            "enabled": False,
            "local_host": "localhost",
            "local_port": 22,
        },
    ]
    assert form.forwarding_rules[0].get("enabled", True) is True
    assert form.forwarding_rules[1].get("enabled", True) is False


def test_daemon_editor_generation_zero_is_valid_loaded_state():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=0, nickname="demo")
    dialog = _EditorDialog()

    _operation, success, _failure = _run_editor_fetch(window, dialog)
    success(window.client.get_connection_editor("demo"))

    assert dialog._daemon_generation == 0
    assert dialog._daemon_editor_loaded is True
    assert dialog._editor_load_failed is False


def test_daemon_editor_pending_fetch_is_not_a_real_generation():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7, nickname="demo")
    dialog = _EditorDialog()

    _operation, _success, _failure = _run_editor_fetch(window, dialog)

    assert dialog._daemon_generation is None
    assert dialog._daemon_editor_loaded is False


def test_daemon_editor_retries_transport_churn_then_succeeds(monkeypatch):
    from sshpilot.window import GLib

    window = _MutationWindow()
    error = SshPilotError(
        ErrorCode.TRANSPORT_CLOSED,
        "The daemon transport closed unexpectedly",
        retryable=True,
    )
    window.client.editor = error
    dialog = _EditorDialog()
    run_now = []

    def _run_now(_ms, cb):
        run_now.append(cb)
        cb()

    monkeypatch.setattr(GLib, "timeout_add", _run_now)

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    failure(error)

    assert run_now
    assert len(window.client_bridge.calls) == 2

    window.client.editor = SimpleNamespace(generation=9)
    retry_operation, retry_success, _retry_failure = window.client_bridge.calls[1]
    retry_success(retry_operation())

    assert dialog._daemon_generation == 9
    assert dialog._daemon_editor_loaded is True


def test_daemon_editor_retry_resolves_current_client_and_bridge(monkeypatch):
    from sshpilot.window import GLib

    window = _MutationWindow()
    error = SshPilotError(
        ErrorCode.TRANSPORT_CLOSED,
        "The daemon transport closed unexpectedly",
        retryable=True,
    )
    window.client.editor = error
    dialog = _EditorDialog()
    scheduled = []
    monkeypatch.setattr(GLib, "timeout_add", lambda _ms, cb: scheduled.append(cb))

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    assert len(window.client_bridge.calls) == 1
    failure(error)
    assert len(scheduled) == 1

    # The daemon reconnected while the retry was pending: swap the live
    # client and bridge before the retry fires.
    new_client = _Client()
    new_client.editor = SimpleNamespace(generation=4)
    new_bridge = _ControlledBridge()
    window.client = new_client
    window.client_bridge = new_bridge

    scheduled[0]()

    assert len(new_bridge.calls) == 1
    retry_operation, retry_success, _retry_failure = new_bridge.calls[0]
    retry_success(retry_operation())

    assert new_client.editor_calls == 1
    assert dialog._daemon_generation == 4
    assert dialog._daemon_editor_loaded is True


def test_daemon_editor_exhausts_retries_then_disables_save(monkeypatch):
    from sshpilot.window import GLib

    window = _MutationWindow()
    error = SshPilotError(
        ErrorCode.TRANSPORT_CLOSED,
        "The daemon transport closed unexpectedly",
        retryable=True,
    )
    window.client.editor = error
    dialog = _EditorDialog()

    def _run_now(_ms, cb):
        cb()

    monkeypatch.setattr(GLib, "timeout_add", _run_now)

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    for _ in range(4):
        failure(error)

    assert len(window.client_bridge.calls) == 4
    assert dialog._daemon_editor_loaded is False
    assert dialog._editor_load_failed is True
    assert dialog._daemon_generation is None


def test_daemon_editor_does_not_retry_non_transport_errors():
    window = _MutationWindow()
    window.client.editor = SshPilotError(ErrorCode.PERSISTENCE_FAILED, "safe")
    dialog = _EditorDialog()

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    failure(SshPilotError(ErrorCode.PERSISTENCE_FAILED, "safe"))

    assert len(window.client_bridge.calls) == 1
    assert dialog._editor_load_failed is True
    assert dialog._daemon_editor_loaded is False
    assert dialog._daemon_generation is None


def test_daemon_editor_ignores_late_result_after_destroy():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7)
    dialog = _EditorDialog()

    _operation, success, _failure = _run_editor_fetch(window, dialog)
    handlers = dict(dialog.handlers)
    assert handlers.get("destroy") is not None
    handlers["destroy"](None)
    success(window.client.get_connection_editor("demo"))

    assert dialog.populated == []
    assert dialog._daemon_generation is None
    assert dialog._daemon_editor_loaded is False


def test_daemon_editor_close_cancels_pending_request(monkeypatch):
    from sshpilot.window import GLib

    cancelled = []

    def _cancel():
        cancelled.append(True)

    window = _MutationWindow()
    window.client_bridge = _CancellableBridge(_cancel)
    window.client.editor = SimpleNamespace(generation=7)
    dialog = _EditorDialog()
    monkeypatch.setattr(GLib, "timeout_add", lambda _ms, cb: object())

    _operation, success, failure = _run_editor_fetch(window, dialog)
    assert cancelled == []

    handlers = dict(dialog.handlers)
    handlers["destroy"](None)

    assert cancelled == [True]
    success(window.client.get_connection_editor("demo"))
    failure(SshPilotError(ErrorCode.TRANSPORT_CLOSED, "late", retryable=True))

    assert dialog.populated == []
    assert dialog._daemon_generation is None
    assert dialog._daemon_editor_loaded is False
    assert len(window.client_bridge.calls) == 1


def test_daemon_editor_close_with_retry_scheduled_submits_nothing(monkeypatch):
    from sshpilot.window import GLib

    window = _MutationWindow()
    error = SshPilotError(
        ErrorCode.TRANSPORT_CLOSED,
        "The daemon transport closed unexpectedly",
        retryable=True,
    )
    window.client.editor = error
    dialog = _EditorDialog()
    scheduled = []
    monkeypatch.setattr(GLib, "timeout_add", lambda _ms, cb: scheduled.append(cb))

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    failure(error)
    assert len(scheduled) == 1

    handlers = dict(dialog.handlers)
    handlers["destroy"](None)

    scheduled[0]()

    assert len(window.client_bridge.calls) == 1
    assert dialog._daemon_generation is None
    assert dialog._editor_load_failed is False


def test_daemon_editor_fetch_not_run_outside_daemon_mode():
    window = _MutationWindow()
    window._app._api_client_selection.client = None
    window.client.editor = SimpleNamespace(generation=7)
    dialog = _EditorDialog()

    window._fetch_daemon_editor_generation(dialog, SimpleNamespace(nickname="demo"))

    assert window.client_bridge.calls == []
    assert dialog._daemon_generation is None
    assert dialog._daemon_editor_loaded is False


def _daemon_save_dialog(**overrides):
    dialog = SimpleNamespace(
        is_editing=True,
        connection=SimpleNamespace(nickname="demo", connection_id="demo"),
        is_daemon_editor=lambda: True,
        _daemon_editor_loaded=True,
        _daemon_generation=None,
    )
    for key, value in overrides.items():
        setattr(dialog, key, value)
    return dialog


def test_daemon_editor_save_blocked_before_snapshot_arrives():
    window = _MutationWindow()
    dialog = _daemon_save_dialog(_daemon_editor_loaded=False, _daemon_generation=None)
    completed = []
    data = _basic_data(nickname="demo", hostname="demo.example")

    window._save_connection_via_client(dialog, data, lambda ok, *a, **k: completed.append(ok))

    assert completed == [False]
    assert window.client_bridge.calls == []
    assert window.client.updated == []


def test_name_only_daemon_edit_sends_no_ssh_fields():
    """A Name edit must use the sidecar-only backend mutation path."""
    from sshpilot.api.models.connections import UNSET

    window = _MutationWindow()
    dialog = _daemon_save_dialog(_daemon_generation=7)
    completed = []
    data = _basic_data(
        display_name="Production Server",
        __changed_fields=("display_name",),
    )

    window._save_connection_via_client(
        dialog, data, lambda ok, *args, **kwargs: completed.append(ok)
    )
    operation, _success, _failure = window.client_bridge.calls[0]
    operation()

    request = window.client.updated[0][1]
    assert request.nickname is UNSET
    assert request.hostname is UNSET
    assert request.username is UNSET
    assert request.port is UNSET
    assert request.config_patch == {}
    assert request.display_name == "Production Server"


def test_daemon_editor_success_sends_loaded_generation_in_update():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7, nickname="demo")
    dialog = _EditorDialog()
    _operation, success, _failure = _run_editor_fetch(window, dialog)
    success(window.client.get_connection_editor("demo"))

    save_dialog = _daemon_save_dialog(_daemon_generation=7)
    completed = []
    data = _basic_data(nickname="demo", hostname="demo.example")
    window._save_connection_via_client(save_dialog, data, lambda ok, *a, **k: completed.append(ok))

    operation, _success, _failure = window.client_bridge.calls[1]
    operation()
    request = window.client.updated[0][1]
    assert request.expected_generation == 7


def test_daemon_editor_second_save_carries_refreshed_generation_and_surfaces_stale():
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7, nickname="demo")
    dialog = _EditorDialog()
    _operation, success, _failure = _run_editor_fetch(window, dialog)
    success(window.client.get_connection_editor("demo"))

    save_dialog = _daemon_save_dialog(_daemon_generation=7)
    first_done = []
    data = _basic_data(nickname="demo", hostname="demo.example")
    window._save_connection_via_client(save_dialog, data, lambda ok, *a, **k: first_done.append(ok))
    op1, ok1, _fail1 = window.client_bridge.calls[1]
    op1()
    ok1(SimpleNamespace(connection_id="demo", generation=8))

    assert first_done == [True]
    assert save_dialog._daemon_generation == 8

    # A competing edit advanced the daemon generation; our next save is stale.
    second_done = []
    window._save_connection_via_client(save_dialog, data, lambda ok, *a, **k: second_done.append(ok))
    op2, _ok2, fail2 = window.client_bridge.calls[2]
    op2()
    assert window.client.updated[1][1].expected_generation == 8
    fail2(SshPilotError(ErrorCode.STALE_EDITOR, "stale editor", details={}))

    assert second_done == [False]


class _FakeDialog:
    """Stand-in ConnectionDialog recording what show_connection_dialog drives."""

    def __init__(self, *args, **kwargs):
        self.source = None
        self.loading = False
        self.presented = False
        self._daemon_generation = None
        self._daemon_editor_loaded = False
        self._editor_load_failed = False
        self.is_editing = False
        self.connection = None

    def connect(self, *_args, **_kwargs):
        return None

    def set_editor_source(self, source):
        self.source = source

    def set_daemon_editor_loading(self):
        self.loading = True
        self._daemon_editor_loaded = False
        self._editor_load_failed = False

    def set_daemon_editor_load_failed(self):
        self._editor_load_failed = True
        self._daemon_editor_loaded = False

    def is_daemon_editor(self):
        return self.source == "daemon"

    def populate_from_editor_details(self, details):
        self._daemon_editor_loaded = True
        self._daemon_generation = getattr(details, "generation", None)
        self._editor_load_failed = False

    def present(self):
        self.presented = True


def _patch_dialog(monkeypatch):
    from sshpilot import connection_dialog as connection_dialog_module

    created = []
    original = connection_dialog_module.ConnectionDialog

    class _Patched(_FakeDialog):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(connection_dialog_module, "ConnectionDialog", _Patched)
    return created, original


def test_daemon_new_connection_dialog_is_local_and_save_works(monkeypatch):
    """Opening the blank New Connection dialog in daemon mode must not enter
    the daemon snapshot flow or permanently disable Save."""
    window = _MutationWindow()
    created, _original = _patch_dialog(monkeypatch)

    window.show_connection_dialog(connection=None)

    assert len(created) == 1
    dialog = created[0]
    assert dialog.source == "local"
    assert dialog.loading is False
    assert dialog._editor_load_failed is False
    assert dialog.presented is True
    # No daemon editor fetch was started.
    assert window.client_bridge.calls == []

    # After valid form input, a normal create succeeds end-to-end.
    completed = []
    data = _basic_data(
        nickname="new",
        hostname="new.example",
        __save_completion=lambda ok, *a, **k: completed.append(ok),
    )
    window.on_connection_saved(dialog, data)
    operation, success, _failure = window.client_bridge.calls[0]
    operation()
    assert window.client.created[0].nickname == "new"
    success(SimpleNamespace(connection_id="new", generation=1))
    assert len(window.client_bridge.calls) == 2
    window.client_bridge.calls[1][1](True)
    assert completed == [True]


def test_daemon_as_new_dialog_is_local_not_gated(monkeypatch):
    window = _MutationWindow()
    created, _original = _patch_dialog(monkeypatch)

    window.show_connection_dialog(
        connection=SimpleNamespace(nickname="demo", connection_id="demo"),
        as_new=True,
    )

    dialog = created[0]
    assert dialog.source == "local"
    assert dialog.loading is False
    assert window.client_bridge.calls == []


def test_daemon_edit_dialog_uses_daemon_snapshot_flow(monkeypatch):
    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=7, nickname="demo")
    created, _original = _patch_dialog(monkeypatch)

    window.show_connection_dialog(
        connection=SimpleNamespace(nickname="demo", connection_id="demo"),
    )

    dialog = created[0]
    assert dialog.source == "daemon"
    assert dialog.loading is True
    assert len(window.client_bridge.calls) == 1
    operation, success, _failure = window.client_bridge.calls[0]
    success(operation())
    assert dialog._daemon_generation == 7
    assert dialog._daemon_editor_loaded is True


def test_daemon_editor_submit_runtime_error_retried_with_replacement_bridge(
    monkeypatch,
):
    from sshpilot.window import GLib

    window = _MutationWindow()
    window.client.editor = SimpleNamespace(generation=3)
    dialog = _EditorDialog()
    scheduled = []
    monkeypatch.setattr(GLib, "timeout_add", lambda _ms, cb: scheduled.append(cb))

    class _RaisingBridge:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("bridge shutting down")

    window.client_bridge = _RaisingBridge()

    dialog.set_daemon_editor_loading()
    window._fetch_daemon_editor_generation(dialog, SimpleNamespace(nickname="demo"))

    # A submit() RuntimeError is transport churn: retryable, so a retry is
    # scheduled rather than terminal failure.
    assert len(scheduled) == 1
    assert dialog._editor_load_failed is False

    new_bridge = _ControlledBridge()
    window.client_bridge = new_bridge
    scheduled[0]()

    assert len(new_bridge.calls) == 1
    retry_operation, retry_success, _retry_failure = new_bridge.calls[0]
    retry_success(retry_operation())
    assert dialog._daemon_generation == 3
    assert dialog._daemon_editor_loaded is True


def test_daemon_editor_terminal_failure_offers_retry_that_works(monkeypatch):
    from sshpilot.window import GLib

    window = _MutationWindow()
    error = SshPilotError(
        ErrorCode.TRANSPORT_CLOSED,
        "The daemon transport closed unexpectedly",
        retryable=True,
    )
    window.client.editor = error
    dialog = _EditorDialog()

    def _run_now(_ms, cb):
        cb()

    monkeypatch.setattr(GLib, "timeout_add", _run_now)

    _operation, _success, failure = _run_editor_fetch(window, dialog)
    for _ in range(4):
        failure(error)

    # Terminal failure: Save off AND a Retry/Close error is offered.
    assert dialog._editor_load_failed is True
    assert getattr(dialog, "load_error_shown", False) is True
    on_retry = dialog.load_error_on_retry
    assert callable(on_retry)

    # The Retry action re-runs the load against the now-available daemon.
    before = len(window.client_bridge.calls)
    window.client.editor = SimpleNamespace(generation=11)
    on_retry()

    assert len(window.client_bridge.calls) == before + 1
    retry_operation, retry_success, _retry_failure = window.client_bridge.calls[-1]
    retry_success(retry_operation())
    assert dialog._daemon_generation == 11
    assert dialog._daemon_editor_loaded is True
    assert dialog._editor_load_failed is False
