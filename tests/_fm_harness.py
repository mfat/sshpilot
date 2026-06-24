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
        # Exception types the manager's connect path references (except clauses).
        AuthenticationException=type("AuthenticationException", (Exception,), {}),
        SSHException=type("SSHException", (Exception,), {}),
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

    # AsyncSFTPManager._connect_impl lazily does ``from ..config import Config`` and,
    # when that Config exposes ``get_file_manager_config``, prefers it over the
    # ``ssh_config`` passed to the manager — so the app's default
    # sftp_keepalive_interval/sftp_connect_timeout would shadow the per-test values.
    # In isolation Config() happens to raise (dummy gi), but in full-suite order a
    # sibling makes it importable, flipping these results. Pin a Config that lacks
    # get_file_manager_config so the explicit ssh_config is always honoured.
    config_stub = types.ModuleType("sshpilot.config")

    class _StubConfig:
        def get_ssh_config(self):
            return {}

    config_stub.Config = _StubConfig
    monkeypatch.setitem(sys.modules, "sshpilot.config", config_stub)

    # Force a fresh import of the file-manager chain so it binds the stubbed
    # gi above, regardless of what a prior test imported (full-suite order).
    for mod in ("sshpilot.file_manager_window",
                "sshpilot.file_manager.openssh_backend",
                "sshpilot.file_manager"):
        sys.modules.pop(mod, None)
    module = importlib.import_module("sshpilot.file_manager_window")
    module._fake_ssh_config = ssh_config_stub
    return module
