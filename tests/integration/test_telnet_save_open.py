"""Integration: save a telnet connection, then open it with the real binary.

Covers the daemon save → launch-provider → system ``telnet`` path that GTK
uses for non-SSH connections (create via ``CreateConnectionRequest``, spawn
via ``DaemonConnectionLaunchProvider``). Skips unless ``telnet`` and
``pexpect`` are present.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytestmark = pytest.mark.integration

pexpect = pytest.importorskip("pexpect")

from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider
from sshpilot.plugins import registry as registry_mod


BANNER = b"SSHPILOT-TELNET-OK\r\n"


class _BannerServer:
    """Accept one TCP client and send a short banner (enough for telnet)."""

    def __init__(self):
        self.port = None
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._error = None

    def start(self):
        self._thread.start()
        assert self._ready.wait(5), "banner server failed to bind"
        if self._error is not None:
            raise self._error
        assert self.port is not None

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
            sock.listen(1)
            sock.settimeout(0.25)
            self._ready.set()
            while not self._stop.is_set():
                try:
                    conn, _addr = sock.accept()
                except socket.timeout:
                    continue
                with conn:
                    try:
                        conn.sendall(BANNER)
                        conn.settimeout(2.0)
                        # Drain telnet option negotiation so the client stays up.
                        conn.recv(1024)
                    except OSError:
                        pass
        except Exception as exc:  # pragma: no cover - bind/listen failure
            self._error = exc
            self._ready.set()
        finally:
            try:
                sock.close()
            except Exception:
                pass


def _repo(tmp_path: Path) -> ConnectionRepository:
    root = tmp_path / "ssh_config"
    root.write_text("# empty\n", encoding="utf-8")
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


@pytest.fixture
def telnet_registry(monkeypatch, tmp_path):
    # Fresh empty registry — the launch provider must auto-register builtins.
    monkeypatch.setattr(registry_mod, "_registry", None)
    import sshpilot.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "_builtins_ensured_for", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert registry_mod.protocol_registry().get_or_none("telnet") is None
    return registry_mod.protocol_registry()


@pytest.mark.skipif(not shutil.which("telnet"), reason="telnet not installed")
def test_telnet_save_then_open_reaches_listener(tmp_path, telnet_registry):
    server = _BannerServer()
    server.start()
    try:
        repository = _repo(tmp_path)
        launch_provider = DaemonConnectionLaunchProvider(
            repository.get_record,
            secret_provider=None,
            app_config=None,
        )
        core = ConnectionApplicationService(
            repository,
            launch_provider=launch_provider,
            client_name="telnet-save-open",
            allow_cross_thread_commands=True,
        )
        try:
            # Same shape GTK DaemonConnectionServices emits for telnet:
            # host becomes hostname; port is a core field; plugin_data is empty.
            created = core.create_connection(
                CreateConnectionRequest(
                    nickname="lab-switch",
                    hostname="127.0.0.1",
                    port=server.port,
                    protocol="telnet",
                    plugin_data={},
                )
            )
            assert created.connection_id == "lab-switch"
            record = repository.get_record("lab-switch")
            assert record is not None
            assert record.protocol == "telnet"
            assert record.hostname == "127.0.0.1"
            assert int(record.port) == server.port

            # Provider must register built-in protocols itself (daemon process).
            argv, _env = launch_provider.prepare_terminal_launch(
                "lab-switch",
                interaction_policy="none",
            )
            assert registry_mod.protocol_registry().get_or_none("telnet") is not None
            assert os.path.basename(argv[0]) == "telnet"
            assert list(argv[1:]) == ["127.0.0.1", str(server.port)]

            child = pexpect.spawn(
                argv[0], list(argv[1:]), timeout=15, encoding="utf-8"
            )
            try:
                idx = child.expect(
                    ["SSHPILOT-TELNET-OK", pexpect.EOF, pexpect.TIMEOUT]
                )
                assert idx == 0, f"telnet did not reach listener: {child.before!r}"
            finally:
                if child.isalive():
                    child.terminate(force=True)
                child.close(force=True)
        finally:
            core.close()
    finally:
        server.stop()
