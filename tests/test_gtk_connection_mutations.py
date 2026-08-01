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

    def create_connection(self, request):
        self.created.append(request)
        return SimpleNamespace(
            id="demo"
        )

    def update_connection(self, connection_id, request):
        self.updated.append((connection_id, request))
        return SimpleNamespace(
            id="demo"
        )

    def delete_connection(self, request):
        self.deleted.append(request)
        return SimpleNamespace(connection_id=request.connection_id, deleted=True)


class _Manager:
    def __init__(self):
        self.reloads = 0

    def load_ssh_config(self):
        self.reloads += 1


class _MutationWindow:
    _daemon_mode_active = MainWindow._daemon_mode_active
    _normalise_daemon_editor_value = staticmethod(
        MainWindow._normalise_daemon_editor_value
    )
    prepare_connection_save_for_client = (
        MainWindow.prepare_connection_save_for_client
    )
    _save_connection_via_client = MainWindow._save_connection_via_client
    on_connection_saved = MainWindow.on_connection_saved
    _delete_connections_via_client = MainWindow._delete_connections_via_client

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


def test_daemon_create_waits_for_success_and_sends_create_dto_with_config_patch():
    window = _MutationWindow()
    completed = []
    data = _basic_data(proxy_jump=["jump.example.test"], __save_completion=completed.append)
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
    assert completed == [True]
    assert window.connection_manager.reloads == 1
    assert window.rebuilds == 1


def test_daemon_mutation_failure_keeps_ui_state_and_uses_safe_completion():
    window = _MutationWindow()
    completed = []
    data = _basic_data(__save_completion=completed.append)
    dialog = SimpleNamespace(is_editing=False, connection=None)

    window.on_connection_saved(dialog, data)
    _operation, _success, failure = window.client_bridge.calls[0]
    failure(
        SshPilotError(
            ErrorCode.PERSISTENCE_FAILED,
            "safe",
            details={"field": "nickname"},
        )
    )

    assert completed == [False]
    assert window.connection_manager.reloads == 0
    assert window.rebuilds == 0


def test_daemon_editor_rejects_secret_changes_without_mutation():
    window = _MutationWindow()
    dialog = SimpleNamespace(
        is_editing=False,
        connection=None,
        key_editor=None,
    )

    secret_problem = window.prepare_connection_save_for_client(
        dialog,
        _basic_data(password="do-not-send", password_changed=True),
    )

    assert "Passwords" in secret_problem
    assert window.client_bridge.calls == []


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
        connection=SimpleNamespace(nickname="Oracle", id="Oracle"),
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
        id="demo",
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
                connections=[SimpleNamespace(nickname="demo", id="demo")],
                load_ssh_config=lambda: None,
            )
            self._find_connection_id_for_nickname = MainWindow._find_connection_id_for_nickname.__get__(self)

    window = GroupTestWindow()
    def failing_assign(connection_id, target_group_id):
        raise SshPilotError(ErrorCode.INTERNAL_ERROR, "RPC error")

    window.client.assign_connection_to_group = failing_assign

    MainWindow.move_connection_to_group(window, "demo", "group-1")

    assert window.group_manager.moved == []

