"""Headless tests for the daemon-backed remote file editor service.

The adapter must keep every file I/O on the daemon SFTP RPCs: no SSH command
strings, no keyring lookups, no sudo building. The ``access`` intent and the
``expected_revision`` revision-safety fields are the contract we assert here.
"""
import types
from concurrent.futures import Future

import pytest

from sshpilot.api.models.operations import SftpFileAccess, SftpFileTarget
from sshpilot.remote_file_editor_service import DaemonRemoteFileService

SERVICE_ID = "sftp-svc-1"
PATH = "/etc/hosts"


class FakeClient:
    def __init__(self, *, access=None, revision="rev-1", content="hosts\n"):
        self.reads = []
        self.replaces = []
        self._access = access
        self._revision = revision
        self._content = content

    def sftp_read_file(self, request):
        self.reads.append(request)
        self._last = request.access
        return types.SimpleNamespace(revision=self._revision, content=self._content, exists=True)

    def sftp_replace_file(self, request):
        self.replaces.append(request)
        self._last = request.access
        new_rev = f"new-{len(self.replaces)}"
        self._revision = new_rev
        return types.SimpleNamespace(revision=new_rev)

    @property
    def last_access(self):
        return getattr(self, "_last", None)


def _make(client, *, privileged=False):
    return DaemonRemoteFileService(client, SERVICE_ID, PATH, privileged_supported=privileged)


def _wait(future: Future):
    return future.result(timeout=5)


def test_constructor_requires_client_and_service_id():
    with pytest.raises(ValueError):
        DaemonRemoteFileService(None, SERVICE_ID, PATH)
    with pytest.raises(ValueError):
        DaemonRemoteFileService(object(), None, PATH)


def test_load_uses_normal_access_and_returns_content():
    client = FakeClient(access=SftpFileAccess.NORMAL)
    service = _make(client)

    result = _wait(service.load())

    assert result.revision == "rev-1"
    assert result.content == "hosts\n"
    assert result.writable is True
    req = client.reads[0]
    assert req.access is SftpFileAccess.NORMAL
    assert req.target is SftpFileTarget.REMOTE
    assert req.path == PATH
    assert req.service_id == SERVICE_ID


def test_load_updates_revision_used_by_next_save():
    client = FakeClient(revision="loaded-rev")
    service = _make(client)

    _wait(service.load())
    _wait(service.save_text("new"))

    req = client.replaces[0]
    assert req.access is SftpFileAccess.NORMAL
    assert req.content == "new"
    assert req.expected_revision == "loaded-rev"
    assert req.backup is True


def test_save_text_keeps_normal_revision_after_save():
    client = FakeClient()
    service = _make(client)

    _wait(service.load())
    result = _wait(service.save_text("edited"))
    _wait(service.save_text("edited-again"))

    assert result.revision == "new-1"
    assert client.replaces[1].expected_revision == "new-1"


def test_privileged_supported_reflects_constructor():
    assert _make(FakeClient(), privileged=True).privileged_supported is True
    assert _make(FakeClient(), privileged=False).privileged_supported is False
    assert _make(FakeClient()).privileged_supported is False


def test_load_privileged_uses_sudo_access_and_its_own_revision():
    client = FakeClient()
    service = _make(client, privileged=True)

    result = _wait(service.load_privileged())

    assert result.revision == "rev-1"
    assert result.exists is True
    req = client.reads[0]
    assert req.access is SftpFileAccess.SUDO
    assert req.target is SftpFileTarget.REMOTE
    assert req.path == PATH


def test_save_text_privileged_uses_sudo_access_and_privileged_revision():
    client = FakeClient()
    service = _make(client, privileged=True)

    _wait(service.load_privileged())
    _wait(service.save_text_privileged("root edit"))

    req = client.replaces[0]
    assert req.access is SftpFileAccess.SUDO
    assert req.expected_revision == "rev-1"
    assert req.content == "root edit"
    assert req.backup is True


def test_save_text_privileged_tracks_its_own_revision():
    client = FakeClient()
    service = _make(client, privileged=True)

    _wait(service.load_privileged())
    _wait(service.save_text_privileged("one"))
    _wait(service.save_text_privileged("two"))

    assert client.replaces[1].expected_revision == "new-1"


def test_normal_and_privileged_revisions_are_independent():
    client = FakeClient()
    service = _make(client, privileged=True)

    _wait(service.load())
    _wait(service.load_privileged())
    _wait(service.save_text("normal"))
    _wait(service.save_text_privileged("root"))

    assert client.replaces[0].expected_revision == "rev-1"
    assert client.replaces[1].expected_revision == "rev-1"


def test_privileged_save_without_privileged_load_falls_back_to_normal_revision():
    client = FakeClient()
    service = _make(client, privileged=True)

    _wait(service.load())
    _wait(service.save_text_privileged("root edit"))

    req = client.replaces[0]
    assert req.access is SftpFileAccess.SUDO
    assert req.expected_revision == "rev-1"


def test_privileged_save_after_privileged_load_prefers_privileged_revision():
    client = FakeClient()
    service = _make(client, privileged=True)

    _wait(service.load())
    client._revision = "rev-2"
    _wait(service.load_privileged())
    _wait(service.save_text_privileged("root edit"))

    req = client.replaces[0]
    assert req.access is SftpFileAccess.SUDO
    assert req.expected_revision == "rev-2"


def test_close_shuts_down_executor():
    client = FakeClient()
    service = _make(client, privileged=True)
    service.close()

    assert service._executor._shutdown is True


def test_display_name_and_writable_accessors():
    service = _make(FakeClient(), privileged=True)
    assert service.display_name == PATH
    assert service.writable is True
    assert service.editor_owned is True


class FakeCapabilities:
    def __init__(self, supported):
        self._supported = set(supported)

    def supports(self, capability):
        return capability in self._supported


class FakeCapabilityClient:
    def __init__(self, privileged):
        from sshpilot.api.capabilities import Capability
        self._caps = FakeCapabilities(
            {Capability.SFTP_PRIVILEGED_FILE} if privileged else set()
        )

    def get_capabilities(self):
        return self._caps

    def sftp_read_file(self, request):
        return types.SimpleNamespace(revision="r", content="x", exists=True)

    def sftp_replace_file(self, request):
        return types.SimpleNamespace(revision="n")


def _ready_manager(client, *, state=None):
    from sshpilot.daemon_sftp_backend import DaemonSftpManager
    from sshpilot.sftp_service_controller import SftpControllerState

    manager = DaemonSftpManager.__new__(DaemonSftpManager)
    manager._sftp_controller = types.SimpleNamespace(
        state=state or SftpControllerState.READY,
        service_id=SERVICE_ID,
    )
    manager._client = client
    manager._username = "user"
    manager._host = "web"
    manager._closed = False
    manager._connection_id = "conn-1"
    return manager


def test_make_file_editor_service_builds_daemon_backed_service():
    manager = _ready_manager(FakeCapabilityClient(privileged=True))

    service = manager.make_file_editor_service(PATH)

    assert isinstance(service, DaemonRemoteFileService)
    assert service.privileged_supported is True
    result = _wait(service.load())
    assert result.content == "x"


def test_make_file_editor_service_flags_privileged_from_capabilities():
    manager = _ready_manager(FakeCapabilityClient(privileged=False))

    service = manager.make_file_editor_service(PATH)

    assert isinstance(service, DaemonRemoteFileService)
    assert service.privileged_supported is False


def test_make_file_editor_service_requires_ready_sftp():
    from sshpilot.sftp_service_controller import SftpControllerState

    manager = _ready_manager(
        FakeCapabilityClient(privileged=True), state=SftpControllerState.IDLE
    )

    with pytest.raises(OSError):
        manager.make_file_editor_service(PATH)
