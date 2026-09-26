"""Login profiles end to end over a real in-process daemon.

A real ``ConnectionRepository`` (temp SSH config) backs the connection service
and the ``LoginProfileService``, so every call exercises client encoding,
dispatch, the daemon adapter, and Host-block rendering. The profile secret
travels through the protected secret-frame transport.
"""

import threading

import pytest

from sshpilot.api import DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.capabilities import Capability
from sshpilot.api.events import EventType
from sshpilot.api.models.login_profiles import (
    AssignLoginProfileRequest,
    CreateLoginProfileRequest,
    DeleteLoginProfileRequest,
    LoginProfileDetachReason,
    LoginProfileLinkMode,
    LoginProfileSecretKind,
    LoginProfileSettings,
    PreviewLoginProfileAssignmentRequest,
    SetGroupLoginProfileRequest,
    SetLoginProfileSecretRequest,
    UpdateLoginProfileRequest,
)
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.login_profiles.service import LoginProfileService
from sshpilot.daemon import DaemonServer
from sshpilot.daemon.connection_secret_provider import DaemonLoginProfileSecretStore
from sshpilot.daemon.dispatch import (
    DAEMON_METHOD_CAPABILITIES,
    DEFERRED_DAEMON_METHODS,
    DRAIN_REJECTED_METHODS,
)
from sshpilot.daemon.login_profile_api import DaemonLoginProfileApi
from sshpilot.daemon.server import CoreServices
from tests.helpers.fake_connection_repository import make_test_connection_service

SSH_CONFIG = (
    "Host web1\n    HostName web1.example.com\n    User alice\n\n"
    "Host web2\n    HostName web2.example.com\n    User bob\n"
)
WRITE_METHODS = {
    "login_profiles.create",
    "login_profiles.update",
    "login_profiles.delete",
    "login_profiles.assign",
    "login_profiles.set_group",
    "login_profiles.set_secret",
    "login_profiles.clear_secret",
}


class _Secrets:
    def __init__(self):
        self.values = {}

    def store(self, spec, value):
        self.values[spec.keyring_account, spec.attributes.get("host")] = value
        return True

    def lookup(self, spec):
        return self.values.get((spec.keyring_account, spec.attributes.get("host")))

    def delete(self, spec):
        return self.values.pop((spec.keyring_account, spec.attributes.get("host")), None) is not None


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "config"
    root.write_text(SSH_CONFIG)
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    secrets = _Secrets()
    service = LoginProfileService(
        repo,
        tmp_path / "login_profiles.json",
        secret_store=DaemonLoginProfileSecretStore(lambda: secrets),
    )
    api = DaemonLoginProfileApi(service)
    socket_path = tmp_path / "sock" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700)

    def _build():
        return CoreServices(
            connections=make_test_connection_service(repo, client_name="sshpilotd"),
            login_profiles=api,
        )

    server = DaemonServer(_build, socket_path=socket_path)
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        yield client, repo, root, secrets, service
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_dispatch_registration():
    methods = {m for m in DAEMON_METHOD_CAPABILITIES if m.startswith("login_profiles.")}
    assert methods == WRITE_METHODS | {"login_profiles.get", "login_profiles.preview_assignment"}
    assert methods <= DEFERRED_DAEMON_METHODS
    assert WRITE_METHODS <= DRAIN_REJECTED_METHODS


def test_capability_advertised_only_with_service(env, tmp_path):
    client, *_ = env
    caps = client.get_capabilities()
    assert caps.supports(Capability.LOGIN_PROFILES_READ)
    assert caps.supports(Capability.LOGIN_PROFILES_WRITE)

    socket_path = tmp_path / "bare" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700)
    bare = DaemonServer(
        lambda: CoreServices(connections=make_test_connection_service(client_name="sshpilotd")),
        socket_path=socket_path,
    )
    bare.start_in_thread()
    other = DaemonClient(socket_path=bare.socket_path)
    try:
        assert not other.get_capabilities().supports(Capability.LOGIN_PROFILES_READ)
        with pytest.raises(SshPilotError) as info:
            other.get_login_profiles()
        assert info.value.code is ErrorCode.UNSUPPORTED_CAPABILITY
    finally:
        other.close()
        bare.shutdown()
        bare.wait_stopped()


def test_full_flow_over_the_wire(env):
    client, repo, root, secrets, _service = env
    settings = LoginProfileSettings(
        name="Deploy", username="deploy", key_select_mode=1, identity_files=("/k/deploy",)
    )
    created = client.create_login_profile(CreateLoginProfileRequest(settings))
    assert created.name == "Deploy" and created.revision == 1

    (preview,) = client.preview_login_profile_assignment(
        PreviewLoginProfileAssignmentRequest(("web1",), LoginProfileLinkMode.EXPLICIT, created.id)
    )
    labels = {c.field: (c.before, c.after) for c in preview.changes}
    assert labels["username"] == ("alice", "deploy")

    assert client.assign_login_profile(
        AssignLoginProfileRequest(("web1",), LoginProfileLinkMode.EXPLICIT, created.id)
    )
    assert "User deploy" in root.read_text()

    group = repo.create_group("Prod").id
    repo.assign_connection_to_group("web2", group)
    assert client.set_group_login_profile(
        SetGroupLoginProfileRequest(group, created.id, ("web2",))
    )
    snapshot = client.get_login_profiles()
    assert snapshot.available
    assert snapshot.profile(created.id).connection_count == 2
    assert snapshot.profile(created.id).group_count == 1
    assert snapshot.link_for("web2").group_id == group

    updated = client.update_login_profile(
        UpdateLoginProfileRequest(
            created.id, LoginProfileSettings(name="Deploy", username="release"),
            expected_revision=1,
        )
    )
    assert updated.revision == 2
    assert root.read_text().count("User release") == 2
    with pytest.raises(SshPilotError) as info:
        client.update_login_profile(
            UpdateLoginProfileRequest(created.id, settings, expected_revision=1)
        )
    assert info.value.code is ErrorCode.STALE_EDITOR

    secret = bytearray(b"hunter2")
    assert client.set_login_profile_secret(SetLoginProfileSecretRequest(created.id), secret)
    assert "hunter2" in secrets.values.values()
    assert client.get_login_profiles().profile(created.id).has_password
    assert client.set_login_profile_secret(
        SetLoginProfileSecretRequest(created.id, LoginProfileSecretKind.LOGIN, clear=True)
    )
    assert not client.get_login_profiles().profile(created.id).has_password

    result = client.delete_login_profile(DeleteLoginProfileRequest(created.id))
    assert {d.connection_id for d in result.detached} == {"web1", "web2"}
    assert client.get_login_profiles().profiles == ()
    assert "User release" in root.read_text()  # detached connections keep values


def test_validation_error_is_mapped(env):
    client, *_ = env
    with pytest.raises(SshPilotError) as info:
        client.create_login_profile(
            CreateLoginProfileRequest(
                LoginProfileSettings(name="x", extra_ssh_config="User root")
            )
        )
    assert info.value.code is ErrorCode.VALIDATION_FAILED


def test_drift_detach_publishes_event(env):
    client, repo, root, _secrets, _service = env
    created = client.create_login_profile(
        CreateLoginProfileRequest(LoginProfileSettings(name="Ops", username="ops"))
    )
    client.assign_login_profile(
        AssignLoginProfileRequest(("web1",), LoginProfileLinkMode.EXPLICIT, created.id)
    )
    received = []
    got = threading.Event()

    def _on_event(event):
        if event.type is EventType.LOGIN_PROFILES_CHANGED and event.payload.detached:
            received.append(event.payload)
            got.set()

    subscription = client.subscribe_events(_on_event)
    try:
        root.write_text(root.read_text().replace("User ops", "User root", 1))
        repo.reload()
        assert got.wait(5)
    finally:
        subscription.unsubscribe()
    (info,) = received[0].detached
    assert info.connection_id == "web1"
    assert info.reason is LoginProfileDetachReason.DRIFT
    assert client.get_login_profiles().link_for("web1") is None
