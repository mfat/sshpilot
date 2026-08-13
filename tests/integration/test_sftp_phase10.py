"""Phase 10.1 real-OpenSSH SFTP integration via ephemeral DaemonServer."""

from __future__ import annotations

from pathlib import Path

import pytest

from sshpilot.api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    ListDirectoryRequest,
    OpenSftpRequest,
    RemoteFileType,
    SftpChmodRequest,
    SftpPathRequest,
    SftpRenameRequest,
    SftpServiceState,
    SftpSymlinkRequest,
)
from tests.daemon.phase10_helpers import (
    require_phase10_container,
    start_phase10_stack,
    wait_until,
)

pytestmark = pytest.mark.integration

# Newlines cannot be represented in the SFTP protocol path strings we use;
# NUL is rejected by the API. Other unusual names below are expected to work.
UNUSUAL_NAMES = (
    "file with spaces.txt",
    "quote's.txt",
    "فارسی.txt",
    "emoji-📁.txt",
    "-leading-dash.txt",
    "semi;colon.txt",
    "meta*$?.txt",
)


@pytest.fixture(scope="module")
def phase10_env(tmp_path_factory):
    env = require_phase10_container(tmp_path_factory.mktemp("phase10-sftp-sshd"))
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture
def stack(tmp_path, phase10_env):
    started = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        yield started
    finally:
        started.close(destroy_env=False)


def _open_ready(stack):
    opened = stack.client.open_sftp(OpenSftpRequest(connection_id=stack.connection_id))
    assert opened.state is SftpServiceState.STARTING
    return stack.wait_sftp_ready(opened.id)


def test_sftp_open_password_auth_reaches_ready(stack):
    ready = _open_ready(stack)
    assert ready.state is SftpServiceState.READY
    record = stack.server._sftp_runtime._records[ready.id]
    process = record.handle._process
    cmdline = Path(f"/proc/{process.pid}/cmdline").read_bytes().replace(b"\0", b" ")
    assert b"sftp" in cmdline
    assert process.poll() is None


def test_sftp_filesystem_ops_and_unusual_names(stack):
    ready = _open_ready(stack)
    sid = ready.id
    client = stack.client
    base = "phase10-ops"

    client.sftp_mkdir(SftpPathRequest(service_id=sid, path=base))
    listed = client.sftp_list_directory(
        ListDirectoryRequest(
            connection_id=stack.connection_id,
            service_id=sid,
            path=".",
        )
    )
    names = {entry.name for entry in listed.entries}
    assert base in names

    for name in UNUSUAL_NAMES:
        path = f"{base}/{name}"
        client.sftp_mkdir(SftpPathRequest(service_id=sid, path=path))
        entry = client.sftp_stat(SftpPathRequest(service_id=sid, path=path))
        assert entry.name == name or entry.path.endswith(name)
        client.sftp_rmdir(SftpPathRequest(service_id=sid, path=path))

    nested = f"{base}/nested"
    renamed = f"{base}/renamed"
    client.sftp_mkdir(SftpPathRequest(service_id=sid, path=nested))
    client.sftp_rename(
        SftpRenameRequest(
            service_id=sid,
            source_path=nested,
            destination_path=renamed,
        )
    )
    client.sftp_chmod(SftpChmodRequest(service_id=sid, path=renamed, mode=0o750))
    mode = client.sftp_stat(SftpPathRequest(service_id=sid, path=renamed)).mode
    assert mode is not None
    assert mode & 0o777 == 0o750

    link = f"{base}/link-to-renamed"
    client.sftp_symlink(
        SftpSymlinkRequest(
            service_id=sid,
            target_path="renamed",
            link_path=link,
        )
    )
    target = client.sftp_readlink(SftpPathRequest(service_id=sid, path=link))
    assert "renamed" in target
    lstat = client.sftp_lstat(SftpPathRequest(service_id=sid, path=link))
    assert lstat.file_type is RemoteFileType.SYMLINK
    real = client.sftp_realpath(SftpPathRequest(service_id=sid, path=renamed))
    assert real.endswith("renamed")

    client.sftp_rmdir(SftpPathRequest(service_id=sid, path=renamed))
    client.sftp_remove(SftpPathRequest(service_id=sid, path=link))
    client.sftp_rmdir(SftpPathRequest(service_id=sid, path=base))


def test_sftp_list_truncated_with_runtime_limit(stack):
    ready = _open_ready(stack)
    sid = ready.id
    stack.server._sftp_runtime._list_limit = 5
    base = "phase10-trunc"
    stack.client.sftp_mkdir(SftpPathRequest(service_id=sid, path=base))
    for index in range(8):
        stack.client.sftp_mkdir(
            SftpPathRequest(service_id=sid, path=f"{base}/d{index:02d}")
        )
    result = stack.client.sftp_list_directory(
        ListDirectoryRequest(
            connection_id=stack.connection_id,
            service_id=sid,
            path=base,
        )
    )
    assert result.truncated is True
    assert len(result.entries) == 5
    # Runtime currently advertises truncated without a resume cursor.
    assert result.next_cursor is None


def test_sftp_attach_detach_second_client(stack):
    ready = _open_ready(stack)
    second = stack.connect_client(client_id="client:phase10-b")
    attached = second.attach_sftp(AttachSftpRequest(service_id=ready.id))
    assert attached.state is SftpServiceState.READY
    second.detach_sftp(ready.id)
    assert stack.client.get_sftp_service(ready.id).state is SftpServiceState.READY
    stack.client.close_sftp(CloseSftpRequest(service_id=ready.id))
    wait_until(
        lambda: stack.client.get_sftp_service(ready.id).state is SftpServiceState.CLOSED,
        timeout=15,
        message="SFTP service did not close",
    )


def test_sftp_auth_failure(tmp_path, phase10_env):
    stack = start_phase10_stack(
        tmp_path,
        env=phase10_env,
        auth="password",
        store_secret=True,
        auto_answer_password=False,
    )
    try:
        stack.manager.store_connection_password(stack.connection, "wrong-password")
        broker = stack.server._interaction_broker
        if broker is not None:
            broker._secret_timeout = 5.0
        stack.start_password_decline()
        opened = stack.client.open_sftp(
            OpenSftpRequest(connection_id=stack.connection_id)
        )

        def _failed() -> bool:
            try:
                stack.answer_pending_host_keys()
            except Exception:
                pass
            runtime = stack.server._sftp_runtime
            record = runtime._records.get(opened.id) if runtime else None
            if record is not None and record.state is SftpServiceState.FAILED:
                return True
            try:
                return (
                    stack.client.get_sftp_service(opened.id).state
                    is SftpServiceState.FAILED
                )
            except Exception:
                return record is not None and record.state is SftpServiceState.FAILED

        wait_until(_failed, timeout=45, message="auth failure never surfaced")
        record = stack.server._sftp_runtime._records[opened.id]
        assert record.state is SftpServiceState.FAILED
        assert record.failure is not None
    finally:
        stack.close(destroy_env=False)


def test_sftp_daemon_shutdown_cleans_ssh_child(tmp_path, phase10_env):
    stack = start_phase10_stack(tmp_path, env=phase10_env)
    try:
        ready = _open_ready(stack)
        process = stack.server._sftp_runtime._records[ready.id].handle._process
        pid = process.pid
        assert Path(f"/proc/{pid}").exists()
        stack.server.shutdown()
        stack.server.wait_stopped()
        wait_until(
            lambda: process.poll() is not None or not Path(f"/proc/{pid}").exists(),
            timeout=15,
            message="ssh -s sftp child survived daemon shutdown",
        )
    finally:
        stack._stop_responder.set()
        for client in list(stack._clients):
            try:
                client.close()
            except Exception:
                pass
        # shared module env survives for other tests
