"""Socket-level DaemonClient known-hosts tests.

Proves list/remove over a real in-process daemon, stale error mapping,
capability gating, daemon-unavailable behavior, and absence of local
fallback I/O.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models import (
    KnownHostEntryId,
    RemoveKnownHostEntriesRequest,
)
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.known_hosts_service import KnownHostsService
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service

HOSTS = (
    b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGkVdummy\n"
    b"\n"
    b"# a comment\n"
    b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGkVdummy\n"
)


def _core_factory(known_hosts_path: Path):
    def _build():
        connections = make_test_connection_service(client_name="sshpilotd")
        service = KnownHostsService(lambda: known_hosts_path)
        return CoreServices(connections=connections, known_hosts=service)

    return _build


def _plain_factory():
    return lambda: CoreServices(
        connections=make_test_connection_service(client_name="sshpilotd")
    )


@pytest.fixture
def kh_server(tmp_path):
    """Start an in-process daemon with a known-hosts service installed."""
    servers = []

    def _start(hosts_path: Path, *, with_service: bool = True):
        socket_path = tmp_path / f"kh-sock-{len(servers)}" / "sshpilotd.sock"
        socket_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        factory = _core_factory(hosts_path) if with_service else _plain_factory()
        server = DaemonServer(factory, socket_path=socket_path)
        server.start_in_thread()
        servers.append(server)
        return server

    yield _start
    for server in servers:
        server.shutdown()
        server.wait_stopped()


def test_list_known_hosts_over_socket(tmp_path, kh_server):
    hosts = tmp_path / "known_hosts"
    hosts.write_bytes(HOSTS)
    server = kh_server(hosts)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        snapshot = client.list_known_hosts()
        assert snapshot.revision
        assert len(snapshot.entries) == 2
        assert [entry.hostname for entry in snapshot.entries] == [
            "example.test",
            "example.test",
        ]
        assert snapshot.entries[0].entry_id != snapshot.entries[1].entry_id
    finally:
        client.close()


def test_remove_known_host_entries_over_socket(tmp_path, kh_server):
    hosts = tmp_path / "known_hosts"
    hosts.write_bytes(HOSTS)
    server = kh_server(hosts)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        snapshot = client.list_known_hosts()
        result = client.remove_known_host_entries(
            RemoveKnownHostEntriesRequest(
                revision=snapshot.revision,
                entry_ids=(snapshot.entries[1].entry_id,),
            )
        )
        assert result.removed_count == 1
        assert len(result.entries) == 1
        assert hosts.read_bytes() == (
            b"example.test ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGkVdummy\n"
            b"\n# a comment\n"
        )
    finally:
        client.close()


def test_stale_revision_maps_to_stale_editor(tmp_path, kh_server):
    hosts = tmp_path / "known_hosts"
    hosts.write_bytes(HOSTS)
    server = kh_server(hosts)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        snapshot = client.list_known_hosts()
        stale = "0" * 64
        with pytest.raises(SshPilotError) as excinfo:
            client.remove_known_host_entries(
                RemoveKnownHostEntriesRequest(
                    revision=stale,
                    entry_ids=(snapshot.entries[0].entry_id,),
                )
            )
        assert excinfo.value.code is ErrorCode.STALE_EDITOR
        assert excinfo.value.retryable is True
        details = excinfo.value.details or {}
        assert details.get("resource") == "known_hosts"
        assert details.get("expected_revision") == stale
        assert details.get("current_revision") == snapshot.revision
    finally:
        client.close()


def test_unknown_entry_id_maps_to_validation_failed(tmp_path, kh_server):
    hosts = tmp_path / "known_hosts"
    hosts.write_bytes(HOSTS)
    server = kh_server(hosts)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        snapshot = client.list_known_hosts()
        with pytest.raises(SshPilotError) as excinfo:
            client.remove_known_host_entries(
                RemoveKnownHostEntriesRequest(
                    revision=snapshot.revision,
                    entry_ids=(KnownHostEntryId("unknown-id"),),
                )
            )
        assert excinfo.value.code is ErrorCode.VALIDATION_FAILED
    finally:
        client.close()


def test_missing_service_does_not_advertise_known_hosts(tmp_path, kh_server):
    from sshpilot.api import Capability

    server = kh_server(tmp_path / "known_hosts", with_service=False)
    client = DaemonClient(socket_path=server.socket_path)
    try:
        capabilities = client.get_capabilities()
        assert not capabilities.supports(Capability.KNOWN_HOSTS_READ)
        with pytest.raises(SshPilotError) as excinfo:
            client.list_known_hosts()
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        client.close()


def test_daemon_unavailable_raises_daemon_unavailable(tmp_path):
    socket_path = tmp_path / "missing" / "sshpilotd.sock"
    socket_path.parent.mkdir(parents=True)
    with pytest.raises(SshPilotError) as excinfo:
        DaemonClient(socket_path=socket_path, connect_timeout=0.2)
    assert excinfo.value.code is ErrorCode.DAEMON_UNAVAILABLE


def test_no_local_fallback_io(tmp_path, kh_server, monkeypatch):
    """The client must never fall back to the legacy local load/save helpers."""
    import sshpilot.core.known_hosts.service as kh_service

    hosts = tmp_path / "known_hosts"
    hosts.write_bytes(HOSTS)
    server = kh_server(hosts)
    client = DaemonClient(socket_path=server.socket_path)

    def _boom(*_args, **_kwargs):
        raise AssertionError("client fell back to local known-hosts I/O")

    monkeypatch.setattr(kh_service, "load_known_hosts", _boom)
    monkeypatch.setattr(kh_service, "save_known_hosts", _boom)
    try:
        snapshot = client.list_known_hosts()
        assert snapshot.entries
        client.remove_known_host_entries(
            RemoveKnownHostEntriesRequest(
                revision=snapshot.revision,
                entry_ids=(snapshot.entries[0].entry_id,),
            )
        )
    finally:
        client.close()
