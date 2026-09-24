"""A slow remote file save must not take the daemon transport down with it.

Over a high-latency SSH link a text-editor save can outlast the client's
request timeout while the daemon is still, correctly, writing the file. The
timeout used to fail the whole transport: the editor reported a failed save
for a file that had been saved, and every later request on that client failed
with "The daemon transport is closed".
"""

from __future__ import annotations

import threading
import time

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api import daemon_client as daemon_client_module
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import SftpServiceId
from sshpilot.api.models.operations import (
    SftpFileTarget,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpReplaceFileRequest,
    SftpReplaceFileResult,
)
from sshpilot.daemon import DaemonServer
from sshpilot.daemon import sftp_runtime
from sshpilot.daemon.server import CoreServices
from sshpilot.remote_file_editor_service import DaemonRemoteFileService
from tests.helpers.fake_connection_repository import make_test_connection_service

SAVE_SECONDS = 0.8
PATH = "/home/demo/.env"


@pytest.fixture
def slow_file_daemon(tmp_path, monkeypatch):
    """A real daemon whose remote file save outlasts a shortened timeout."""

    monkeypatch.setattr(daemon_client_module, "SFTP_FILE_REQUEST_TIMEOUT", 0.5)
    remote = {"content": "A=1\n", "saved": threading.Event()}

    def replace_file(self, request, *, client_id):
        time.sleep(SAVE_SECONDS)
        remote["content"] = request.content
        remote["saved"].set()
        return SftpReplaceFileResult(
            target=request.target,
            path=request.path,
            revision=f"rev:{request.content}",
            size=len(request.content),
            backup_path=None,
        )

    def read_file(self, request, *, client_id):
        return SftpReadFileResult(
            target=request.target,
            path=request.path,
            content=remote["content"],
            exists=True,
            revision=f"rev:{remote['content']}",
            size=len(remote["content"]),
            mode=0o600,
        )

    monkeypatch.setattr(sftp_runtime.SftpServiceRuntime, "replace_file", replace_file)
    monkeypatch.setattr(sftp_runtime.SftpServiceRuntime, "read_file", read_file)

    socket_path = tmp_path / "sock" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700)
    server = DaemonServer(
        lambda: CoreServices(
            connections=make_test_connection_service(client_name="sshpilotd")
        ),
        socket_path=socket_path,
    )
    server.start_in_thread()
    # File requests take max(client timeout, SFTP_FILE_REQUEST_TIMEOUT).
    client = DaemonClient(socket_path=server.socket_path, timeout=0.5)
    try:
        yield client, remote
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def _replace(client, content):
    return client.sftp_replace_file(
        SftpReplaceFileRequest(
            SftpFileTarget.REMOTE,
            PATH,
            content,
            "rev:A=1\n",
            backup=True,
            service_id=SftpServiceId("sftp-1"),
        )
    )


def _read(client):
    return client.sftp_read_file(
        SftpReadFileRequest(SftpFileTarget.REMOTE, PATH, SftpServiceId("sftp-1"))
    )


def test_file_requests_get_a_longer_timeout_than_the_default():
    client = DaemonClient.__new__(DaemonClient)
    client._timeout = daemon_client_module.DEFAULT_REQUEST_TIMEOUT
    for method in ("sftp.read_file", "sftp.replace_file"):
        assert client._default_timeout_for(method) >= 60.0
    assert client._default_timeout_for("sftp.list") == client._timeout


def test_timed_out_save_is_ambiguous_and_keeps_the_transport(slow_file_daemon):
    client, remote = slow_file_daemon

    with pytest.raises(SshPilotError) as raised:
        _replace(client, "A=2\n")

    # The daemon may still land the write, so the save is not reported as
    # a plain failure...
    assert raised.value.code is ErrorCode.MUTATION_AMBIGUOUS
    # ...and it does: the daemon finishes after the client stopped waiting.
    assert remote["saved"].wait(5)
    # Let the late response arrive; it must be dropped, not treated as a
    # protocol violation that fails the transport.
    time.sleep(0.3)
    assert _read(client).content == "A=2\n"


def test_editor_confirms_a_timed_out_save_that_landed(slow_file_daemon):
    client, remote = slow_file_daemon
    # The daemon runs requests for one SFTP service in order, so the editor's
    # confirming read waits for the slow save to finish before it answers.
    service = DaemonRemoteFileService(client, SftpServiceId("sftp-1"), PATH)
    service._revision = "rev:A=1\n"
    try:
        result = service.save_text("A=2\n").result(timeout=10)
    finally:
        service.close()

    assert remote["content"] == "A=2\n"
    assert result.revision == "rev:A=2\n"
    assert service._revision == "rev:A=2\n"
