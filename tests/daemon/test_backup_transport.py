"""The remote transports behind the SSH-server backup destination.

These prove the delegation: a backup opens the file manager's own SFTP service
and hands the byte copy to ``TransferRuntime``, and a host with no sftp
subsystem falls back to the one-shot command service Host Info uses. Neither
store may build an ssh command line of its own.
"""
from __future__ import annotations

import base64
import threading
from types import SimpleNamespace

import pytest

from sshpilot.api.events import EventType, Subscription
from sshpilot.api.models.broadcast import HostCommandResult, HostCommandState
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.operations import RemoteFileType, SftpServiceState
from sshpilot.api.models.secrets import SecretTransferMessageCode
from sshpilot.api.models.transfers import TransferDirection, TransferState
from sshpilot.backup_backends import BackupError
from sshpilot.daemon.backup_transport import (
    BackupTransportProvider,
    BackupTransportUnavailable,
    ExecBackupStore,
    SftpBackupStore,
    _parse_df_avail_kb,
    _q,
)

CLIENT = ClientId("test-backup")


# --- fakes -------------------------------------------------------------------


class FakeSftpRuntime:
    """Models the daemon SFTP runtime over an in-memory remote filesystem."""

    def __init__(self, *, ready=True, home="/home/u", dirs=("/home/u",), files=()):
        self.ready = ready
        self.home = home
        self.dirs = set(dirs)
        self.files = dict.fromkeys(files, b"")
        self.opened = []
        self.closed = []
        self.renames = []
        self.removed = []

    def prepare_open_service(self, request, *, client_id):
        self.opened.append((request.connection_id, client_id))
        return SimpleNamespace(id="svc-1")

    def start_service(self, service_id):
        return None

    def get_service(self, service_id):
        return SimpleNamespace(
            state=SftpServiceState.READY if self.ready else SftpServiceState.FAILED
        )

    def prepare_close_service(self, request, *, client_id):
        return None

    def finish_close_service(self, service_id):
        self.closed.append(service_id)

    def realpath(self, request, *, client_id):
        return self.home

    def stat_path(self, request, *, client_id):
        if request.path in self.dirs:
            return SimpleNamespace(file_type=RemoteFileType.DIRECTORY)
        if request.path in self.files:
            return SimpleNamespace(file_type=RemoteFileType.REGULAR)
        raise FileNotFoundError(request.path)

    def mkdir(self, request, *, client_id):
        self.dirs.add(request.path)

    def list_directory(self, request, *, client_id):
        prefix = request.path.rstrip("/") + "/"
        names = [
            key[len(prefix):]
            for key in sorted(self.files)
            if key.startswith(prefix) and "/" not in key[len(prefix):]
        ]
        return SimpleNamespace(
            entries=[
                SimpleNamespace(name=n, file_type=RemoteFileType.REGULAR) for n in names
            ]
        )

    def rename(self, request, *, client_id):
        self.renames.append((request.source_path, request.destination_path))
        self.files[request.destination_path] = self.files.pop(request.source_path, b"")

    def remove(self, request, *, client_id):
        self.removed.append(request.path)
        self.files.pop(request.path, None)


class FakeTransferRuntime:
    """Models TransferRuntime: prepare, run on a thread, publish a terminal event."""

    def __init__(self, sftp: FakeSftpRuntime, *, state=TransferState.COMPLETED):
        self.sftp = sftp
        self.state = state
        self.requests = []
        self._subs = []
        self._records = {}

    def subscriber_count(self):
        return len(self._subs)

    def subscribe_events(self, callback):
        self._subs.append(callback)
        # Same surface as sshpilot.api.events.Subscription (unsubscribe(), not
        # cancel()); getting this wrong once leaked a subscriber per transfer.
        return Subscription(lambda: self._subs.remove(callback))

    def prepare_start_transfer(self, request, *, client_id):
        self.requests.append(request)
        transfer_id = f"t-{len(self.requests)}"
        self._records[transfer_id] = SimpleNamespace(
            id=transfer_id, state=TransferState.QUEUED, failure=None
        )
        return self._records[transfer_id]

    def get_transfer(self, transfer_id):
        return self._records[transfer_id]

    def run_transfer(self, transfer_id):
        record = self._records[transfer_id]
        request = self.requests[int(transfer_id.split("-")[1]) - 1]
        if self.state is TransferState.COMPLETED:
            if request.direction is TransferDirection.UPLOAD:
                with open(request.local_path, "rb") as handle:
                    self.sftp.files[request.remote_path] = handle.read()
            else:
                with open(request.local_path, "wb") as handle:
                    handle.write(self.sftp.files.get(request.remote_path, b""))
        record.state = self.state
        for callback in list(self._subs):
            callback(
                SimpleNamespace(
                    type=EventType.TRANSFER_COMPLETED, payload=record, sequence=1
                )
            )


class FakeBroadcast:
    """Models BroadcastCommandService for the one-shot fallback."""

    def __init__(self, responder):
        self._responder = responder
        self.commands = []
        self._results = {}
        self._n = 0

    def start(self, request, *, owner_client_id, input_data=None):
        self._n += 1
        operation_id = f"op-{self._n}"
        self.commands.append(request.command)
        code, out, err = self._responder(request.command, input_data)
        state = (
            HostCommandState.SUCCEEDED
            if code == 0
            else HostCommandState.FAILED
        )
        self._results[operation_id] = HostCommandResult(
            request.connection_ids[0], state, exit_code=code, stdout=out, stderr=err
        )
        return SimpleNamespace(
            operation=SimpleNamespace(operation_id=operation_id),
            targets=(self._results[operation_id],),
        )

    def get(self, operation_id, *, client_id):
        return SimpleNamespace(
            operation=SimpleNamespace(operation_id=operation_id),
            targets=(self._results[operation_id],),
        )


# --- SFTP store ---------------------------------------------------------------


def test_sftp_store_uploads_through_the_transfer_runtime(tmp_path):
    """The archive must reach the host as a TransferRuntime upload, staged under
    .part and renamed into place -- not as bytes this module writes itself."""
    sftp = FakeSftpRuntime()
    transfers = FakeTransferRuntime(sftp)
    local = tmp_path / "backup.spbk"
    local.write_bytes(b"\x00\x01binary\xff")

    with SftpBackupStore(sftp, transfers, "host", client_id=CLIENT) as store:
        store.ensure_directory("~/sshpilot-backups")
        store.upload(str(local), "~/sshpilot-backups/b.spbk")

    assert [r.direction for r in transfers.requests] == [TransferDirection.UPLOAD]
    # Staged, then renamed: no reader ever sees a half-written archive.
    assert transfers.requests[0].remote_path.endswith(".part")
    assert sftp.renames == [
        ("/home/u/sshpilot-backups/b.spbk.part", "/home/u/sshpilot-backups/b.spbk")
    ]
    assert sftp.files["/home/u/sshpilot-backups/b.spbk"] == b"\x00\x01binary\xff"
    assert sftp.closed == ["svc-1"]
    # The event subscription is released. Teardown is deliberately best-effort,
    # so a wrong method name there fails silently and leaks a subscriber on
    # every single transfer; only asserting the effect catches that.
    assert transfers.subscriber_count() == 0


def test_sftp_store_round_trips_binary_without_corruption(tmp_path):
    sftp = FakeSftpRuntime()
    transfers = FakeTransferRuntime(sftp)
    payload = bytes(range(256)) * 8
    source, restored = tmp_path / "a.spbk", tmp_path / "b.spbk"
    source.write_bytes(payload)

    with SftpBackupStore(sftp, transfers, "host", client_id=CLIENT) as store:
        store.ensure_directory("~/sshpilot-backups")
        store.upload(str(source), "~/sshpilot-backups/a.spbk")
        store.download("~/sshpilot-backups/a.spbk", str(restored))

    assert restored.read_bytes() == payload


def test_sftp_store_expands_tilde_to_the_accounts_home(tmp_path):
    """Only list_directory expands ~ inside the runtime, so the store has to
    resolve it before every other call or mkdir/rename hit a literal '~'."""
    sftp = FakeSftpRuntime(home="/var/users/bob", dirs={"/var/users/bob"})
    transfers = FakeTransferRuntime(sftp)
    with SftpBackupStore(sftp, transfers, "host", client_id=CLIENT) as store:
        store.ensure_directory("~/backups")
    assert "/var/users/bob/backups" in sftp.dirs


def test_sftp_store_creates_missing_parents(tmp_path):
    sftp = FakeSftpRuntime(dirs={"/home/u"})
    transfers = FakeTransferRuntime(sftp)
    with SftpBackupStore(sftp, transfers, "host", client_id=CLIENT) as store:
        store.ensure_directory("~/a/b/c")
    assert {"/home/u/a", "/home/u/a/b", "/home/u/a/b/c"} <= sftp.dirs


def test_sftp_store_reports_a_failed_transfer(tmp_path):
    sftp = FakeSftpRuntime()
    transfers = FakeTransferRuntime(sftp, state=TransferState.FAILED)
    local = tmp_path / "b.spbk"
    local.write_bytes(b"x")
    with SftpBackupStore(sftp, transfers, "host", client_id=CLIENT) as store:
        with pytest.raises(BackupError):
            store.upload(str(local), "~/sshpilot-backups/b.spbk")
    # The partial upload is cleaned up rather than left to accumulate.
    assert sftp.removed == ["/home/u/sshpilot-backups/b.spbk.part"]


def test_sftp_store_free_space_is_unknown():
    # No statvfs@openssh.com in the client, so the honest answer is "unknown"
    # and the backend skips the pre-check rather than inventing a number.
    sftp = FakeSftpRuntime()
    store = SftpBackupStore(sftp, FakeTransferRuntime(sftp), "h", client_id=CLIENT)
    assert store.free_space_bytes("~/x") is None


def test_sftp_store_signals_unavailable_when_the_service_never_readies():
    sftp = FakeSftpRuntime(ready=False)
    with pytest.raises(BackupTransportUnavailable):
        SftpBackupStore(sftp, FakeTransferRuntime(sftp), "h", client_id=CLIENT).__enter__()


# --- exec fallback ------------------------------------------------------------


def _exec_host(files=None):
    """A tiny remote shell understanding only the commands ExecBackupStore issues."""
    files = dict(files or {})

    def respond(command, input_data):
        if command.startswith("mkdir -p"):
            return 0, "", ""
        if command.startswith("df -Pk"):
            return 0, "/dev/sda1 1000000 1 99000 1% /home", ""
        if command.startswith("base64 -d >"):
            target = command.split(">")[1].split("&&")[0].strip().strip("'")
            files[target.replace(".part", "")] = base64.b64decode(input_data)
            return 0, "", ""
        if command.startswith("base64 <"):
            path = command.split("<")[1].strip().strip("'")
            if path not in files:
                return 1, "", "No such file"
            return 0, base64.b64encode(files[path]).decode(), ""
        if command.startswith("ls -1"):
            hits = sorted(k for k in files if k.endswith(".spbk"))
            return (0, "\n".join(hits), "") if hits else (1, "", "")
        return 0, "", ""

    return respond, files


def test_exec_store_round_trips_binary_via_base64(tmp_path):
    """The one-shot service captures output as text, so a raw `cat` would mangle
    the archive; the fallback has to frame it."""
    respond, remote = _exec_host()
    broadcast = FakeBroadcast(respond)
    payload = bytes(range(256)) * 4
    source, restored = tmp_path / "a.spbk", tmp_path / "b.spbk"
    source.write_bytes(payload)

    store = ExecBackupStore(broadcast, "host", client_id=CLIENT)
    store.ensure_directory("~/sshpilot-backups")
    store.upload(str(source), "~/sshpilot-backups/a.spbk")
    store.download("~/sshpilot-backups/a.spbk", str(restored))

    assert restored.read_bytes() == payload
    assert any(c.startswith("base64 -d >") for c in broadcast.commands)


def test_exec_store_reads_free_space_from_df():
    respond, _ = _exec_host()
    store = ExecBackupStore(FakeBroadcast(respond), "host", client_id=CLIENT)
    assert store.free_space_bytes("~/x") == 99000 * 1024


def test_exec_store_unreachable_host_is_a_connection_failure():
    def respond(command, input_data):
        return None, "", "ssh: connect to host failed"

    store = ExecBackupStore(FakeBroadcast(respond), "host", client_id=CLIENT)
    with pytest.raises(BackupError) as excinfo:
        store.ensure_directory("~/x")
    assert (
        excinfo.value.transfer_message.code
        is SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED
    )


def test_exec_store_empty_listing_is_not_an_error():
    respond, _ = _exec_host()
    store = ExecBackupStore(FakeBroadcast(respond), "host", client_id=CLIENT)
    assert store.list_backups("~/sshpilot-backups") == []


# --- transport selection ------------------------------------------------------


def test_provider_prefers_sftp():
    sftp = FakeSftpRuntime()
    provider = BackupTransportProvider(
        sftp_runtime=sftp,
        transfer_runtime=FakeTransferRuntime(sftp),
        broadcast_service=FakeBroadcast(_exec_host()[0]),
    )
    store = provider.open("host", client_id=CLIENT)
    assert isinstance(store, SftpBackupStore)
    store.close()


def test_provider_opens_the_store_as_the_requesting_client():
    """The frontend that asked for the backup must own what the provider opens.

    Interactions are visible only to the client that owns their scope, so a
    store opened under any other identity raises the connect's password /
    passphrase / host-key prompt where nobody can answer it: the backup then
    just waits out the interaction timeout and reports a failed connection.
    """
    sftp = FakeSftpRuntime()
    provider = BackupTransportProvider(
        sftp_runtime=sftp,
        transfer_runtime=FakeTransferRuntime(sftp),
        broadcast_service=FakeBroadcast(_exec_host()[0]),
    )
    caller = ClientId("client:the-frontend")
    store = provider.open("host", client_id=caller)
    try:
        assert sftp.opened == [("host", caller)]
        assert store._client_id == caller
    finally:
        store.close()


def test_exec_fallback_also_runs_as_the_requesting_client():
    """A host without sftp authenticates too, so the fallback needs the owner
    just as much as the SFTP transport does."""
    sftp = FakeSftpRuntime(ready=False)
    provider = BackupTransportProvider(
        sftp_runtime=sftp,
        transfer_runtime=FakeTransferRuntime(sftp),
        broadcast_service=FakeBroadcast(_exec_host()[0]),
    )
    caller = ClientId("client:the-frontend")
    store = provider.open("host", client_id=caller)
    assert isinstance(store, ExecBackupStore)
    assert store._client_id == caller


def test_provider_falls_back_when_the_host_has_no_sftp():
    sftp = FakeSftpRuntime(ready=False)
    provider = BackupTransportProvider(
        sftp_runtime=sftp,
        transfer_runtime=FakeTransferRuntime(sftp),
        broadcast_service=FakeBroadcast(_exec_host()[0]),
    )
    assert isinstance(provider.open("host", client_id=CLIENT), ExecBackupStore)


def test_provider_without_any_transport_fails_clearly():
    provider = BackupTransportProvider()
    with pytest.raises(BackupError):
        provider.open("host", client_id=CLIENT)


# --- helpers ------------------------------------------------------------------


def test_q_quotes_metacharacters_but_expands_tilde():
    assert _q("~/sshpilot-backups") == "~/sshpilot-backups"
    q = _q("~/my backups; rm -rf x")
    assert q.startswith("~/")               # tilde stays bare so the remote shell expands it
    assert q != "~/my backups; rm -rf x"    # the rest is quoted, not bare
    assert "rm -rf x" in q                  # preserved as literal, inside quotes
    assert _q("/tmp/abs dir") == "'/tmp/abs dir'"


def test_parse_df_avail_kb():
    assert _parse_df_avail_kb("/dev/sda1 1000000 1 99000 1% /home") == 99000
    assert _parse_df_avail_kb("") is None
    assert _parse_df_avail_kb("garbage line") is None


# --- end to end through the daemon entry points -------------------------------


def _provider(sftp, transfers):
    return BackupTransportProvider(
        sftp_runtime=sftp, transfer_runtime=transfers
    )


def test_export_list_preview_round_trip_over_the_provider(tmp_path, monkeypatch):
    """The whole SSH destination, end to end, through the public daemon entry
    points: the archive is uploaded, listed and read back over the delegated
    transport, with no ssh command composed anywhere in between."""
    import sshpilot.backup_manager as bm
    from sshpilot.daemon.secret_transfer import (
        daemon_export_backup,
        daemon_list_ssh_backups,
        daemon_preview_ssh_backup,
    )

    config_dir = tmp_path / "config"
    ssh_dir = tmp_path / "ssh"
    config_dir.mkdir()
    ssh_dir.mkdir()
    (config_dir / "config.json").write_text('{"config_version": 5}')
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(ssh_dir))

    sftp = FakeSftpRuntime(dirs={"/home/u"})
    transfers = FakeTransferRuntime(sftp)
    provider = _provider(sftp, transfers)

    class _Mgr:
        def lookup(self, *a, **k):
            return None

        def lookup_in_keyring(self, *a, **k):
            return None

    options = {"app_settings": True, "ssh_config": False, "known_hosts": False,
               "secrets": False, "private_keys": False}
    result = daemon_export_backup(
        _Mgr(),
        destination="ssh:host:~/sshpilot-backups",
        options=options,
        connections_source=list,
        settings_path=config_dir / "config.json",
        transport=provider, client_id=CLIENT,
    )
    assert result.status.value == "success", result.message

    # The bytes really landed on the (fake) remote, via a TransferRuntime upload.
    stored = [k for k in sftp.files if k.endswith(".spbk")]
    assert len(stored) == 1
    assert transfers.requests[0].direction is TransferDirection.UPLOAD

    listed = daemon_list_ssh_backups(
        _Mgr(), connection_id="host", remote_dir="~/sshpilot-backups",
        connections_source=list, settings_path=config_dir / "config.json",
        transport=provider, client_id=CLIENT,
    )
    assert [e["name"] for e in listed] == [stored[0].rsplit("/", 1)[-1]]

    preview, manifest, _staged = daemon_preview_ssh_backup(
        _Mgr(), connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=listed[0]["id"], connections_source=list,
        settings_path=config_dir / "config.json", transport=provider,
        client_id=CLIENT,
    )
    assert preview.error is None
    assert isinstance(manifest, dict)
    # Downloading the archive is a TransferRuntime download, not a shell `cat`.
    assert transfers.requests[-1].direction is TransferDirection.DOWNLOAD


def test_missing_transport_is_reported_not_crashed(tmp_path, monkeypatch):
    """Without an attached transport the destination must fail with a clear
    message rather than an AttributeError deep in a store."""
    import sshpilot.backup_manager as bm
    from sshpilot.daemon.secret_transfer import daemon_export_backup

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"config_version": 5}')
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(tmp_path))

    class _Mgr:
        def lookup(self, *a, **k):
            return None

    result = daemon_export_backup(
        _Mgr(),
        destination="ssh:host:~/backups",
        options={"app_settings": True, "ssh_config": False, "known_hosts": False,
                 "secrets": False, "private_keys": False},
        connections_source=list,
        settings_path=config_dir / "config.json",
    )
    assert result.status.value == "failed"
    assert (
        result.message.code is SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED
    )


def test_daemon_server_attaches_the_backup_transport(tmp_path, monkeypatch):
    """The real composition must hand the secrets service a provider carrying
    the SFTP, transfer and one-shot runtimes -- otherwise every SSH-server
    backup fails with "no remote backup transport is configured"."""
    import logging

    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.backup_transport import BackupTransportProvider

    monkeypatch.setenv("SSHPILOT_SSH_DIR", str(tmp_path / "ssh"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    from sshpilot.daemon import cli

    socket_dir = tmp_path / "run"
    socket_dir.mkdir(mode=0o700)
    server = DaemonServer(
        cli._production_core_services, socket_path=socket_dir / "sshpilotd.sock"
    )
    server.start_in_thread()
    try:
        secrets = server._secrets_service
        assert secrets is not None
        transport = secrets._backup_transport
        assert isinstance(transport, BackupTransportProvider)
        # All three delegated transports are present, so SFTP is tried first and
        # a host without it still has the one-shot fallback.
        assert transport._sftp_runtime is server._sftp_runtime
        assert transport._transfer_runtime is server._transfer_runtime
        assert transport._broadcast_service is server._broadcast_service
        assert transport._broadcast_service is not None
    finally:
        server.shutdown()
        server.wait_stopped()


# --- encrypted archives -------------------------------------------------------


def _remote_with_encrypted_backup(tmp_path, passphrase, manifest=None):
    """A fake remote holding one encrypted .spbk, plus a provider for it."""
    from sshpilot.backup_archive import write_spbk

    local = tmp_path / "seed.spbk"
    write_spbk(
        str(local),
        manifest or {"version": 1, "format": "sshpilot-backup",
                     "app_config": {"k": "v"}, "credentials": []},
        passphrase,
    )
    sftp = FakeSftpRuntime(dirs={"/home/u", "/home/u/sshpilot-backups"})
    sftp.files["/home/u/sshpilot-backups/sshpilot_backup_20260908_0300.spbk"] = (
        local.read_bytes()
    )
    transfers = FakeTransferRuntime(sftp)
    return sftp, BackupTransportProvider(
        sftp_runtime=sftp, transfer_runtime=transfers
    )


def _entry_id():
    return "/home/u/sshpilot-backups/sshpilot_backup_20260908_0300.spbk"


def test_encrypted_preview_asks_for_a_passphrase_instead_of_failing(tmp_path):
    """An encrypted remote backup must report encrypted=True with no error, so
    the service prompts. Reporting SSH_BACKUP_READ_FAILED here made every
    encrypted SSH backup impossible to import."""
    from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    preview, manifest, _staged = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=provider, client_id=CLIENT,
    )
    assert preview.encrypted is True
    assert preview.error is None
    assert manifest is None


def test_encrypted_preview_decrypts_with_the_right_passphrase(tmp_path):
    from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    preview, manifest, _staged = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=provider, client_id=CLIENT, passphrase="pw",
    )
    assert preview.error is None
    assert preview.encrypted is True
    assert isinstance(manifest, dict)


def test_encrypted_preview_reports_a_wrong_passphrase_distinctly(tmp_path):
    """The service retries on WRONG_PASSPHRASE_OR_CORRUPT_BACKUP, so a bad
    passphrase must not be reported as an unreadable backup."""
    from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    preview, manifest, _staged = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=provider, client_id=CLIENT, passphrase="wrong",
    )
    assert manifest is None
    assert (
        preview.error.code
        is SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
    )


def test_encrypted_import_accepts_a_passphrase(tmp_path, monkeypatch):
    """When the preview's cached manifest has expired, the import downloads and
    decrypts the archive itself rather than failing."""
    import sshpilot.backup_manager as bm
    from sshpilot.daemon.secret_transfer import daemon_import_ssh_backup

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"config_version": 5}')
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(tmp_path / "ssh"))

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    result = daemon_import_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), options={"mode": "merge"},
        settings_path=config_dir / "config.json", transport=provider,
        client_id=CLIENT,
        passphrase="pw",
    )
    assert result.status.value == "success", result.message


def test_encrypted_import_without_a_passphrase_asks_rather_than_giving_up(tmp_path, monkeypatch):
    import sshpilot.backup_manager as bm
    from sshpilot.daemon.secret_transfer import daemon_import_ssh_backup

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.json").write_text('{"config_version": 5}')
    monkeypatch.setattr(bm, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(bm, "get_ssh_dir", lambda: str(tmp_path / "ssh"))

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    result = daemon_import_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), options={"mode": "merge"},
        settings_path=config_dir / "config.json", transport=provider,
        client_id=CLIENT,
    )
    assert result.status.value == "failed"
    # This code is what makes the service prompt and retry.
    assert (
        result.message.code
        is SecretTransferMessageCode.WRONG_PASSPHRASE_OR_CORRUPT_BACKUP
    )


def test_encrypted_preview_reuses_the_archive_it_already_downloaded(tmp_path):
    """The passphrase round trip must not re-fetch the archive.

    The first pass downloads, discovers the archive is encrypted, and hands the
    file back; the service prompts and then decrypts *that* file. Downloading
    twice doubled the cost of every encrypted remote preview -- and over the
    exec fallback, where a download is bounded at roughly 12 MB, it doubled the
    traffic for the archives closest to that ceiling.
    """
    import os

    from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    preview, manifest, staged = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=provider, client_id=CLIENT,
    )
    assert preview.encrypted is True and manifest is None
    assert staged and os.path.exists(staged), "the fetched archive was thrown away"

    # transport=None proves the retry touches no remote at all: opening a store
    # without one raises SSH_SERVER_CONNECTION_FAILED.
    preview, manifest, again = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=None, client_id=CLIENT, passphrase="pw",
        archive_path=staged,
    )
    assert preview.error is None
    assert isinstance(manifest, dict)
    assert again is None
    # A caller-supplied archive stays the caller's to delete.
    assert os.path.exists(staged)
    os.unlink(staged)


def test_preview_deletes_the_archive_it_downloads_on_the_happy_path(tmp_path):
    """Only the encrypted-without-a-passphrase case retains a file."""
    import os

    from sshpilot.daemon.secret_transfer import daemon_preview_ssh_backup

    _sftp, provider = _remote_with_encrypted_backup(tmp_path, "pw")
    preview, manifest, staged = daemon_preview_ssh_backup(
        None, connection_id="host", remote_dir="~/sshpilot-backups",
        entry_id=_entry_id(), settings_path=tmp_path / "config.json",
        transport=provider, client_id=CLIENT, passphrase="pw",
    )
    assert preview.error is None and isinstance(manifest, dict)
    assert staged is None
