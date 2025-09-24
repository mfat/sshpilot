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
        self.closed = False
        self.last_sftp = None
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
        sftp = types.SimpleNamespace(close=lambda: setattr(self, "_sftp_closed", True))
        self.last_sftp = sftp
        self._sftp_closed = False
        return sftp

    def close(self):
        self.closed = True


class DummyProxyCommand:
    instances = []

    def __init__(self, command: str) -> None:
        self.command = command
        self.closed = False
        DummyProxyCommand.instances.append(self)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def reset_dummy_client():
    DummyClient.instances.clear()
    DummyProxyCommand.instances.clear()
    yield
    DummyClient.instances.clear()
    DummyProxyCommand.instances.clear()


def _load_file_manager_module(monkeypatch):
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

    proxy_module = types.SimpleNamespace(ProxyCommand=DummyProxyCommand)
    paramiko_stub = types.SimpleNamespace(
        SSHClient=lambda: DummyClient(),
        AutoAddPolicy=lambda: DummyPolicy("auto"),
        RejectPolicy=lambda: DummyPolicy("reject"),
        WarningPolicy=lambda: DummyPolicy("warn"),
        MissingHostKeyPolicy=object,
        proxy=proxy_module,
    )
    monkeypatch.setitem(sys.modules, "paramiko", paramiko_stub)
    monkeypatch.setitem(sys.modules, "paramiko.proxy", proxy_module)

    sys.modules.pop("sshpilot.file_manager_window", None)
    return importlib.import_module("sshpilot.file_manager_window")


def test_async_sftp_manager_uses_stored_password(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

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


def test_async_sftp_manager_uses_proxy_command(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    connection = types.SimpleNamespace(
        hostname="example.com",
        host="example.com",
        username="alice",
        auth_method=0,
        key_select_mode=0,
        proxy_command="ssh -W %h:%p bastion",
    )

    manager = module.AsyncSFTPManager(
        "example.com",
        "alice",
        port=22,
        connection=connection,
        connection_manager=None,
        ssh_config={"auto_add_host_keys": True},
    )

    manager._connect_impl()

    assert DummyClient.instances, "Expected AsyncSFTPManager to create a client"
    client = DummyClient.instances[-1]
    assert client.connect_calls, "Expected a connection attempt"
    kwargs = client.connect_calls[-1]
    assert "sock" in kwargs, "ProxyCommand should provide a socket to connect()"
    proxy_instance = kwargs["sock"]
    assert isinstance(proxy_instance, DummyProxyCommand)
    assert proxy_instance.command == "ssh -W example.com:22 bastion"
    assert not proxy_instance.closed


def test_async_sftp_manager_expands_proxy_tokens(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    connection = types.SimpleNamespace(
        hostname="example.com",
        host="example.com",
        username="alice",
        nickname="prod",
        auth_method=0,
        key_select_mode=0,
        proxy_command="ssh -W %h:%p %r@%n-%%bastion",
    )

    manager = module.AsyncSFTPManager(
        "example.com",
        "alice",
        port=2200,
        connection=connection,
        connection_manager=None,
        ssh_config={"auto_add_host_keys": True},
    )

    manager._connect_impl()

    client = DummyClient.instances[-1]
    kwargs = client.connect_calls[-1]
    proxy_instance = kwargs["sock"]
    assert proxy_instance.command == "ssh -W example.com:2200 alice@prod-%bastion"


def test_async_sftp_manager_builds_proxy_jump_command(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    connection = types.SimpleNamespace(
        hostname="example.com",
        host="example.com",
        username="alice",
        auth_method=0,
        key_select_mode=0,
        proxy_jump=["Router"],
    )

    manager = module.AsyncSFTPManager(
        "example.com",
        "alice",
        port=2222,
        connection=connection,
        connection_manager=None,
        ssh_config={"auto_add_host_keys": True},
    )

    manager._connect_impl()

    client = DummyClient.instances[-1]
    kwargs = client.connect_calls[-1]
    proxy_instance = kwargs["sock"]
    assert proxy_instance.command == "ssh -W example.com:2222 -J Router example.com"

    manager.close()
    assert proxy_instance.closed, "ProxyCommand should be closed when manager closes"
