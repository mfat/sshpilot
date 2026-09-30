"""``SftpServiceRuntime.resolve_ids`` names remote ids the way the server does.

Drives the runtime with the real client against OpenSSH's sftp-server binary,
which answers ``users-groups-by-id@openssh.com`` from the machine's own user
database, so the test can check the names with ``pwd``/``grp``.
"""

from __future__ import annotations

import grp
import os
import pwd
import shutil
import subprocess

import pytest

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.operations import SftpResolveIdsRequest
from sshpilot.api.transport.codec import sftp_id_names_from_wire, sftp_id_names_to_wire
from sshpilot.sftp.client import OpenSSHSFTPClient
from tests.daemon.test_transfer_runtime import _make_ready_sftp_service

_SFTP_SERVER = next(
    (
        path
        for path in (
            "/usr/lib/openssh/sftp-server",
            "/usr/libexec/openssh/sftp-server",
            "/usr/lib/ssh/sftp-server",
            "/usr/libexec/sftp-server",
        )
        if os.access(path, os.X_OK)
    ),
    shutil.which("sftp-server"),
)

pytestmark = pytest.mark.skipif(_SFTP_SERVER is None, reason="OpenSSH sftp-server not installed")

_OWNER = ClientId("client:owner")


def _unused_id() -> int:
    candidate = 4_000_000_000
    while True:
        try:
            pwd.getpwuid(candidate)
        except KeyError:
            try:
                grp.getgrgid(candidate)
            except KeyError:
                return candidate
        candidate += 1


@pytest.fixture
def service():
    process = subprocess.Popen([_SFTP_SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    client = OpenSSHSFTPClient(process.stdin, process.stdout, on_close=process.terminate)
    client.start()
    if not client.supports_users_groups_by_id():
        client.close()
        process.wait(timeout=5)
        process.stdout.close()
        pytest.skip("sftp-server predates users-groups-by-id@openssh.com")
    runtime, service_id, _ = _make_ready_sftp_service(_OWNER, client)
    try:
        yield runtime, service_id, client
    finally:
        client.close()
        process.wait(timeout=5)
        process.stdout.close()


def test_resolve_ids_names_users_and_groups_in_request_order(service):
    runtime, service_id, _client = service
    uid, gid = os.getuid(), os.getgid()
    missing = _unused_id()

    names = runtime.resolve_ids(
        SftpResolveIdsRequest(service_id=service_id, uids=(0, uid, missing), gids=(gid, 0)),
        client_id=_OWNER,
    )

    assert names.uids == (0, uid, missing)
    assert names.user_names == (pwd.getpwuid(0).pw_name, pwd.getpwuid(uid).pw_name, None)
    assert names.group_names == (grp.getgrgid(gid).gr_name, grp.getgrgid(0).gr_name)
    assert sftp_id_names_from_wire(sftp_id_names_to_wire(names)) == names


def test_resolve_ids_with_only_groups(service):
    runtime, service_id, _client = service

    names = runtime.resolve_ids(
        SftpResolveIdsRequest(service_id=service_id, gids=(0,)), client_id=_OWNER
    )

    assert names.user_names == ()
    assert names.group_names == (grp.getgrgid(0).gr_name,)


def test_resolve_ids_without_the_extension_is_unsupported(service):
    runtime, service_id, client = service
    client.extensions.pop("users-groups-by-id@openssh.com")

    with pytest.raises(SshPilotError) as excinfo:
        runtime.resolve_ids(
            SftpResolveIdsRequest(service_id=service_id, uids=(0,)), client_id=_OWNER
        )
    assert excinfo.value.code is ErrorCode.REMOTE_UNSUPPORTED_OPERATION


def test_resolve_ids_request_needs_ids_in_range():
    with pytest.raises(ValueError):
        SftpResolveIdsRequest(service_id="sftp-1")
    with pytest.raises(ValueError):
        SftpResolveIdsRequest(service_id="sftp-1", uids=(-1,))
    with pytest.raises(ValueError):
        SftpResolveIdsRequest(service_id="sftp-1", uids=tuple(range(1025)))
