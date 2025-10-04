import importlib
import sys
import threading
import types
from typing import Optional

import pytest

from tests.test_sftp_utils_in_app_manager import setup_gi


class DummyPolicy:
    def __init__(self, name: str) -> None:
        self.name = name


class DummyChannel:
    def __init__(self, kind: str, dest_addr: tuple, src_addr: tuple) -> None:
        self.kind = kind
        self.dest_addr = dest_addr
        self.src_addr = src_addr
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DummyTransport:
    def __init__(self) -> None:
        self.open_channel_calls = []
        self.keepalive_values = []

    def open_channel(self, kind: str, dest_addr: tuple, src_addr: tuple) -> DummyChannel:
        channel = DummyChannel(kind, dest_addr, src_addr)
        self.open_channel_calls.append((kind, dest_addr, src_addr, channel))
        return channel

    def set_keepalive(self, value: int) -> None:
        self.keepalive_values.append(value)


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
        self.transport = DummyTransport()
        self.stat_event = threading.Event()
        self.sftp_stat_calls = []

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
        def _close() -> None:
            self._sftp_closed = True

        def _stat(path: str):
            self.sftp_stat_calls.append(path)
            self.stat_event.set()
            return types.SimpleNamespace()

        sftp = types.SimpleNamespace(
            close=_close,
            stat=_stat,
            stat_calls=self.sftp_stat_calls,
        )
        self.last_sftp = sftp
        self._sftp_closed = False
        return sftp

    def close(self):
        self.closed = True

    def get_transport(self):
        return self.transport



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

    ssh_config_stub = types.SimpleNamespace(config_map={}, queries=[])

    def fake_get_effective(host: str, config_file: Optional[str] = None):
        ssh_config_stub.queries.append((host, config_file))
        return ssh_config_stub.config_map.get(host, {})

    ssh_config_stub.get_effective_ssh_config = fake_get_effective

    sys.modules.pop("sshpilot.ssh_config_utils", None)
    monkeypatch.setitem(sys.modules, "sshpilot.ssh_config_utils", ssh_config_stub)

    sys.modules.pop("sshpilot.file_manager_window", None)
    module = importlib.import_module("sshpilot.file_manager_window")
    module._fake_ssh_config = ssh_config_stub
    return module



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


def test_async_sftp_manager_builds_proxy_jump_chain(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map.update(
        {
            "Router": {"hostname": "router.internal", "user": "gateway", "port": "2201"},
        }
    )


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

    assert len(DummyClient.instances) == 2
    target_client = DummyClient.instances[0]
    jump_client = DummyClient.instances[1]

    jump_kwargs = jump_client.connect_calls[-1]
    assert jump_kwargs["hostname"] == "router.internal"
    assert jump_kwargs["username"] == "gateway"
    assert jump_kwargs["port"] == 2201

    assert jump_client.transport.open_channel_calls
    channel_call = jump_client.transport.open_channel_calls[-1]
    assert channel_call[0] == "direct-tcpip"
    assert channel_call[1] == ("example.com", 2222)

    target_kwargs = target_client.connect_calls[-1]
    assert "sock" in target_kwargs
    proxy_channel = target_kwargs["sock"]
    assert isinstance(proxy_channel, DummyChannel)
    assert proxy_channel.dest_addr == ("example.com", 2222)

    assert manager._jump_clients == [jump_client]

    manager.close()
    assert jump_client.closed
    assert proxy_channel.closed


def test_async_sftp_manager_uses_effective_host_settings(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map.update(
        {
            "prod": {"hostname": "prod.internal", "user": "bob", "port": "2201"},
        }
    )

    connection = types.SimpleNamespace(
        hostname="prod",
        host="prod",
        username="alice",
        nickname="prod",
        auth_method=0,
        key_select_mode=0,
    )

    manager = module.AsyncSFTPManager(
        "prod",
        "alice",
        port=22,
        connection=connection,
        connection_manager=None,
        ssh_config={"auto_add_host_keys": True},
    )

    manager._connect_impl()

    assert DummyClient.instances
    target_client = DummyClient.instances[0]
    kwargs = target_client.connect_calls[-1]
    assert kwargs["hostname"] == "prod.internal"
    assert kwargs["username"] == "bob"
    assert kwargs["port"] == 2201


def test_async_sftp_manager_configures_keepalive(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    class DummyEvent:
        def __init__(self) -> None:
            self.wait_calls = []
            self._set = False
            self._first_wait = True

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            if self._set:
                return True
            if self._first_wait:
                self._first_wait = False
                return False
            return self._set

        def set(self) -> None:
            self._set = True

        def is_set(self) -> bool:
            return self._set

    real_event_cls = threading.Event
    monkeypatch.setattr(module.threading, "Event", DummyEvent)

    connection = types.SimpleNamespace(
        hostname="example.com",
        host="example.com",
        username="alice",
        auth_method=0,
        key_select_mode=0,
    )

    manager = module.AsyncSFTPManager(
        "example.com",
        "alice",
        port=22,
        connection=connection,
        connection_manager=None,
        ssh_config={
            "auto_add_host_keys": True,
            "keepalive_interval": 5,
            "keepalive_count_max": 2,
        },
    )

    manager._connect_impl()

    assert DummyClient.instances, "Expected AsyncSFTPManager to create a client"
    client = DummyClient.instances[-1]
    assert client.transport.keepalive_values == [5]

    client.stat_event = real_event_cls()

    assert client.stat_event.wait(1.0)

    keepalive_event = manager._keepalive_stop_event
    assert isinstance(keepalive_event, DummyEvent)
    assert keepalive_event.wait_calls
    assert keepalive_event.wait_calls[0] == 5

    manager.close()

    assert keepalive_event.is_set()
    assert manager._keepalive_thread is None or not manager._keepalive_thread.is_alive()
    assert manager._keepalive_stop_event is None
    assert client._sftp_closed

