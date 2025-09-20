import importlib
import sys
import types

import pytest

from tests.test_sftp_utils_in_app_manager import setup_gi


class DummyPolicy:
    def __init__(self, name: str) -> None:
        self.name = name


class DummyClient:
    instances = []

    def __init__(self) -> None:
        self.set_missing_host_key_policy_calls = []
        self.loaded_host_keys = []
        self.connect_calls = []
        self.open_sftp_called = False
        self.loaded_system = False
        DummyClient.instances.append(self)

    def set_missing_host_key_policy(self, policy):
        self.set_missing_host_key_policy_calls.append(policy)

    def load_system_host_keys(self):
        self.loaded_system = True

    def load_host_keys(self, path):
        self.loaded_host_keys.append(path)

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)

    def open_sftp(self):
        self.open_sftp_called = True
        return object()


@pytest.fixture(autouse=True)
def reset_dummy_client():
    DummyClient.instances.clear()
    yield
    DummyClient.instances.clear()


def test_async_sftp_manager_uses_stored_password(monkeypatch):
    setup_gi(monkeypatch)

    repository = sys.modules["gi.repository"]
    gobject_module = sys.modules["gi.repository.GObject"]
    flags = getattr(gobject_module, "SignalFlags", types.SimpleNamespace())
    setattr(flags, "RUN_FIRST", getattr(flags, "RUN_FIRST", 0))
    setattr(flags, "RUN_LAST", getattr(flags, "RUN_LAST", 1))
    gobject_module.SignalFlags = flags
    setattr(repository, "GObject", gobject_module)
    pango = types.SimpleNamespace(
        EllipsizeMode=types.SimpleNamespace(END=0, MIDDLE=1),
        WrapMode=types.SimpleNamespace(WORD_CHAR=0),
    )
    monkeypatch.setitem(sys.modules, "gi.repository.Pango", pango)
    setattr(repository, "Pango", pango)
    pangoft2 = types.ModuleType("PangoFT2")
    monkeypatch.setitem(sys.modules, "gi.repository.PangoFT2", pangoft2)
    setattr(repository, "PangoFT2", pangoft2)

    paramiko_stub = types.SimpleNamespace(
        SSHClient=lambda: DummyClient(),
        AutoAddPolicy=lambda: DummyPolicy("auto"),
        RejectPolicy=lambda: DummyPolicy("reject"),
        WarningPolicy=lambda: DummyPolicy("warn"),
        MissingHostKeyPolicy=object,
    )
    monkeypatch.setitem(sys.modules, "paramiko", paramiko_stub)

    # Ensure we reload the module so it picks up our stubs
    sys.modules.pop("sshpilot.file_manager_window", None)
    module = importlib.import_module("sshpilot.file_manager_window")

    # Pretend the known hosts file exists
    known_hosts_path = "/tmp/known_hosts"
    monkeypatch.setattr(
        module.os.path,
        "exists",
        lambda path: True if path == known_hosts_path else False,
    )

    connection = types.SimpleNamespace(
        hostname="example.com",
        host="example.com",
        username="alice",
        auth_method=1,
        key_select_mode=0,
        password="",
        pubkey_auth_no=True,
    )

    class DummyManager:
        def __init__(self, path):
            self.password_calls = []
            self.known_hosts_path = path

        def get_password(self, host, username):
            self.password_calls.append((host, username))
            return "stored-secret"

    manager = DummyManager(known_hosts_path)

    sftp_manager = module.AsyncSFTPManager(
        "example.com",
        "alice",
        port=2222,
        connection=connection,
        connection_manager=manager,
        ssh_config={"auto_add_host_keys": True},
    )

    sftp_manager._connect_impl()

    assert manager.password_calls == [("example.com", "alice")]
    assert DummyClient.instances, "Expected AsyncSFTPManager to create a client"
    client = DummyClient.instances[-1]
    assert client.loaded_host_keys == [known_hosts_path]
    assert client.open_sftp_called
    assert client.connect_calls, "Expected a connection attempt"
    connect_kwargs = client.connect_calls[-1]
    assert connect_kwargs["password"] == "stored-secret"
    assert connect_kwargs["port"] == 2222
    assert connect_kwargs["allow_agent"] is False
    assert connect_kwargs["look_for_keys"] is False
    assert client.set_missing_host_key_policy_calls
    assert client.set_missing_host_key_policy_calls[-1].name == "auto"
    assert sftp_manager._password == "stored-secret"
