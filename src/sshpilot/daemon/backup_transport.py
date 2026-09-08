"""Remote transports for the SSH-server backup destination.

Backup and restore are file transfers, so they ride the stack the file manager
already uses: a daemon-owned SFTP service (:class:`~sshpilot.daemon.sftp_runtime.SftpServiceRuntime`)
with the byte copy driven by :class:`~sshpilot.daemon.transfer_runtime.TransferRuntime`,
which makes a backup an ordinary cancellable transfer with progress. Hosts whose
``sftp`` subsystem is unavailable fall back to one-shot shell commands run
through the very service Host Info uses
(:class:`~sshpilot.daemon.broadcast_service.BroadcastCommandService`).

Neither store builds an ssh command line or resolves a credential of its own:
authentication, host-key trust and interaction brokering all belong to the
launch provider sitting behind those runtimes. That is the whole point of this
module -- it is the seam where "which file goes where" meets transports that
already know how to connect.

This module is deliberately GTK-free.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import posixpath
import shlex
import threading
from typing import Any, List, Optional

from sshpilot.api.events import EventType
from sshpilot.api.models.broadcast import (
    MAX_BROADCAST_OUTPUT_BYTES,
    BroadcastCommandRequest,
    BroadcastExecutionPolicy,
    HostCommandState,
)
from sshpilot.api.models.common import ClientId
from sshpilot.api.models.interactions import ExecutionInteractionMode
from sshpilot.api.models.operations import (
    CloseSftpRequest,
    ListDirectoryRequest,
    OpenSftpRequest,
    RemoteFileType,
    SftpPathRequest,
    SftpRenameRequest,
    SftpServiceState,
)
from sshpilot.api.models.secrets import SecretTransferMessageCode
from sshpilot.api.models.transfers import (
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
)
from sshpilot.backup_backends import BackupEntry, BackupError, backup_entry_for

logger = logging.getLogger(__name__)

#: A backup upload/download may legitimately run for minutes on a slow link.
DEFAULT_TRANSFER_TIMEOUT_SECONDS = 900.0

#: Metadata commands (mkdir/df/ls) are small and must not hang a backup.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60.0

#: The exec fallback frames the archive in base64 (4/3 of the raw size) and the
#: one-shot service caps captured output, so a download over this transport is
#: bounded at roughly three quarters of the cap. Truncation is detected and
#: reported rather than written out as a corrupt archive.
_EXEC_OUTPUT_LIMIT_BYTES = MAX_BROADCAST_OUTPUT_BYTES

_TERMINAL_TRANSFER_STATES = frozenset(
    {TransferState.COMPLETED, TransferState.FAILED, TransferState.CANCELLED}
)

_TRANSFER_EVENTS = frozenset(
    {
        EventType.TRANSFER_COMPLETED,
        EventType.TRANSFER_FAILED,
        EventType.TRANSFER_CANCELLED,
    }
)


class BackupTransportUnavailable(Exception):
    """The remote host cannot serve this transport (so try the next one).

    Raised only for "this transport does not work here" -- a missing ``sftp``
    subsystem, a service that never reached READY. A reachable host that simply
    refuses the operation raises :class:`BackupError` instead, which is final.
    """


def _q(path: str) -> str:
    """Shell-quote a remote path while still letting the remote shell expand a leading ``~/``
    (``shlex.quote`` would neutralise the tilde). Everything after the tilde is quoted, so a
    user-typed path with spaces/metacharacters can't break out of the command."""
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _parse_df_avail_kb(text: str) -> Optional[int]:
    """Available KB from a ``df -Pk … | tail -1`` line (POSIX field 4). None if unparseable."""
    try:
        return int(text.split()[3])
    except (ValueError, IndexError):
        return None


# --- SFTP (the file manager's own stack) --------------------------------------


class SftpBackupStore:
    """Backup storage over a daemon-owned SFTP service.

    Opens one service for the life of the operation and drives every byte copy
    through :class:`TransferRuntime`, exactly as a file-manager upload does, so
    the transfer is resumable state the daemon already knows how to report and
    cancel. Use as a context manager -- the service is closed on exit even when
    the operation fails.
    """

    def __init__(
        self,
        sftp_runtime: Any,
        transfer_runtime: Any,
        connection_id: str,
        *,
        client_id: ClientId,
        timeout_seconds: float = DEFAULT_TRANSFER_TIMEOUT_SECONDS,
    ) -> None:
        self._sftp = sftp_runtime
        self._transfers = transfer_runtime
        self._connection_id = connection_id
        self._client_id = client_id
        self._timeout = float(timeout_seconds)
        self._service_id: Any = None
        self._home: Optional[str] = None

    # -- lifecycle ------------------------------------------------------

    def __enter__(self) -> "SftpBackupStore":
        try:
            prepared = self._sftp.prepare_open_service(
                OpenSftpRequest(self._connection_id), client_id=self._client_id
            )
        except Exception as exc:
            raise BackupTransportUnavailable(str(exc)) from exc
        self._service_id = prepared.id
        try:
            self._sftp.start_service(self._service_id)
            state = self._sftp.get_service(self._service_id).state
        except Exception as exc:
            self.close()
            raise BackupTransportUnavailable(str(exc)) from exc
        if state is not SftpServiceState.READY:
            # A host with no sftp subsystem fails here; the caller then retries
            # over the one-shot command transport rather than giving up.
            self.close()
            raise BackupTransportUnavailable(f"SFTP service state {state}")
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        service_id, self._service_id = self._service_id, None
        if service_id is None:
            return
        try:
            self._sftp.prepare_close_service(
                CloseSftpRequest(service_id), client_id=self._client_id
            )
            self._sftp.finish_close_service(service_id)
        except Exception:  # pragma: no cover - best effort teardown
            logger.debug("Closing the backup SFTP service failed", exc_info=True)

    # -- path helpers ---------------------------------------------------

    def _resolve_home(self) -> str:
        """The account's home, resolved once. ``sftp-server`` starts in it, so
        ``REALPATH(".")`` answers without assuming a path layout."""
        if self._home is None:
            self._home = self._sftp.realpath(
                SftpPathRequest(self._service_id, "."), client_id=self._client_id
            )
        return self._home

    def _absolute(self, path: str) -> str:
        """Expand a leading ``~``. Only ``list_directory`` expands tilde inside
        the runtime, so every other call has to arrive already absolute."""
        if path == "~":
            return self._resolve_home()
        if path.startswith("~/"):
            return posixpath.join(self._resolve_home().rstrip("/"), path[2:])
        return path

    # -- RemoteBackupStore ----------------------------------------------

    def ensure_directory(self, path: str) -> None:
        target = self._absolute(path)
        # mkdir is one level at a time over SFTP; walk the chain and let an
        # already-existing component pass.
        missing: List[str] = []
        probe = target
        while probe and probe != "/":
            try:
                attr = self._sftp.stat_path(
                    SftpPathRequest(self._service_id, probe), client_id=self._client_id
                )
                if attr.file_type is not RemoteFileType.DIRECTORY:
                    raise BackupError(
                        SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE,
                        parameters={"directory": path},
                        diagnostic="the path exists and is not a directory",
                    )
                break
            except BackupError:
                raise
            except Exception:
                missing.append(probe)
                parent = posixpath.dirname(probe)
                if parent == probe:
                    break
                probe = parent
        for directory in reversed(missing):
            try:
                self._sftp.mkdir(
                    SftpPathRequest(self._service_id, directory), client_id=self._client_id
                )
            except Exception as exc:
                raise BackupError(
                    SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE,
                    parameters={"directory": path},
                    diagnostic=str(exc) or "permission denied",
                ) from exc

    def free_space_bytes(self, path: str) -> Optional[int]:
        """Unknown over SFTP.

        The app's SFTP client does not implement ``statvfs@openssh.com``, so
        there is no free-space answer to give. ``None`` tells the backend to
        skip the pre-check; an actually full disk still fails the upload with
        the remote's own error.
        """
        return None

    def list_backups(self, path: str) -> List[BackupEntry]:
        target = self._absolute(path)
        try:
            listing = self._sftp.list_directory(
                ListDirectoryRequest(
                    connection_id=self._connection_id,
                    path=target,
                    service_id=self._service_id,
                ),
                client_id=self._client_id,
            )
        except Exception:
            # A missing or unreadable backup directory simply holds no backups;
            # a broken connection has already failed louder than this.
            logger.debug("Listing remote backups failed", exc_info=True)
            return []
        entries: List[BackupEntry] = []
        for item in getattr(listing, "entries", ()) or ():
            name = getattr(item, "name", "")
            if not name.endswith(".spbk"):
                continue
            if getattr(item, "file_type", None) is RemoteFileType.DIRECTORY:
                continue
            entries.append(backup_entry_for(posixpath.join(target, name)))
        return entries

    def upload(self, local_path: str, remote_path: str) -> None:
        destination = self._absolute(remote_path)
        staged = destination + ".part"
        try:
            self._run_transfer(
                TransferDirection.UPLOAD, local_path=local_path, remote_path=staged
            )
        except Exception:
            self._discard(staged)
            raise
        try:
            self._sftp.rename(
                SftpRenameRequest(
                    service_id=self._service_id,
                    source_path=staged,
                    destination_path=destination,
                    overwrite=True,
                ),
                client_id=self._client_id,
            )
        except Exception as exc:
            self._discard(staged)
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_WRITE_FAILED,
                diagnostic=str(exc) or "unknown error",
            ) from exc

    def download(self, remote_path: str, local_path: str) -> None:
        self._run_transfer(
            TransferDirection.DOWNLOAD,
            local_path=local_path,
            remote_path=self._absolute(remote_path),
            failure_code=SecretTransferMessageCode.SSH_BACKUP_READ_FAILED,
        )

    # -- transfer driving -----------------------------------------------

    def _discard(self, remote_path: str) -> None:
        """Best-effort removal of a partial upload (it is excluded from listings
        either way, but leaving it behind wastes remote space)."""
        try:
            self._sftp.remove(
                SftpPathRequest(self._service_id, remote_path), client_id=self._client_id
            )
        except Exception:
            logger.debug("Discarding a partial backup upload failed", exc_info=True)

    def _run_transfer(
        self,
        direction: TransferDirection,
        *,
        local_path: str,
        remote_path: str,
        failure_code: SecretTransferMessageCode = (
            SecretTransferMessageCode.SSH_SERVER_WRITE_FAILED
        ),
    ) -> None:
        request = StartTransferRequest(
            connection_id=self._connection_id,
            sftp_service_id=self._service_id,
            direction=direction,
            remote_path=remote_path,
            local_path=local_path,
            conflict_policy=TransferConflictPolicy.OVERWRITE,
        )
        prepared = self._transfers.prepare_start_transfer(request, client_id=self._client_id)
        transfer_id = prepared.id

        # Subscribe before running: the copy loop runs on its own thread and a
        # small file can reach a terminal state before this call returns.
        finished = threading.Event()

        def _observe(event) -> None:
            if event.type not in _TRANSFER_EVENTS:
                return
            if getattr(event.payload, "id", None) == transfer_id:
                finished.set()

        subscription = self._transfers.subscribe_events(_observe)
        try:
            self._transfers.run_transfer(transfer_id)
            # The event may have fired between prepare and subscribe, so consult
            # current state too rather than trusting the notification alone.
            if self._transfers.get_transfer(transfer_id).state not in _TERMINAL_TRANSFER_STATES:
                finished.wait(self._timeout)
            summary = self._transfers.get_transfer(transfer_id)
        finally:
            try:
                subscription.unsubscribe()
            except Exception:  # pragma: no cover - best effort
                logger.debug("Unsubscribing from transfer events failed", exc_info=True)

        if summary.state is TransferState.COMPLETED:
            return
        if summary.state not in _TERMINAL_TRANSFER_STATES:
            raise BackupError(failure_code, diagnostic="the transfer timed out")
        raise BackupError(
            failure_code,
            diagnostic=_failure_text(summary.failure) or f"transfer {summary.state.value}",
        )


def _failure_text(failure: Any) -> str:
    if failure is None:
        return ""
    for attribute in ("message", "detail", "code"):
        value = getattr(failure, attribute, None)
        if value:
            return str(getattr(value, "value", value))
    return str(failure)


# --- One-shot commands (the transport Host Info uses) -------------------------


class ExecBackupStore:
    """Backup storage over one-shot remote commands.

    The fallback for hosts with no usable ``sftp`` subsystem. Commands run
    through :class:`BroadcastCommandService` -- the same service behind Host
    Info -- so argv, authentication and prompting stay with the launch provider
    and the interaction broker. Archive bytes are base64-framed because that
    service captures output as text.
    """

    def __init__(
        self,
        broadcast_service: Any,
        connection_id: str,
        *,
        client_id: ClientId,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT_SECONDS,
        transfer_timeout: float = DEFAULT_TRANSFER_TIMEOUT_SECONDS,
    ) -> None:
        self._broadcast = broadcast_service
        self._connection_id = connection_id
        self._client_id = client_id
        self._command_timeout = float(command_timeout)
        self._transfer_timeout = float(transfer_timeout)

    def __enter__(self) -> "ExecBackupStore":
        return self

    def __exit__(self, *exc_info) -> None:
        return None

    def close(self) -> None:
        return None

    # -- command plumbing -----------------------------------------------

    def _run(self, command: str, *, timeout: float, input_data: Optional[bytes] = None):
        """Run one remote command, returning ``(exit_code, stdout, stderr, truncated)``.

        ``exit_code`` is ``None`` when the command never ran (unreachable host,
        authentication failure, cancellation) -- the caller reports that as a
        connection failure rather than a remote refusal. ``truncated`` says the
        capture limit cut the output off, which for a download means the bytes
        are incomplete and must not be written out.
        """
        request = BroadcastCommandRequest(
            (self._connection_id,),
            command,
            BroadcastExecutionPolicy(
                concurrency_limit=1,
                timeout_seconds=timeout,
                output_limit_bytes=_EXEC_OUTPUT_LIMIT_BYTES,
                interaction_mode=ExecutionInteractionMode.INTERACTIVE,
            ),
        )
        summary = self._broadcast.start(
            request, owner_client_id=self._client_id, input_data=input_data
        )
        operation_id = summary.operation.operation_id
        deadline = threading.Event()
        # start() returns as soon as the operation is queued; poll the summary
        # until the single target reaches a terminal state.
        waited = 0.0
        step = 0.05
        while True:
            summary = self._broadcast.get(operation_id, client_id=self._client_id)
            targets = tuple(summary.targets or ())
            result = targets[0] if targets else None
            if result is not None and result.state not in (
                HostCommandState.PENDING,
                HostCommandState.RUNNING,
            ):
                break
            if waited >= timeout + 30.0:
                return None, "", "the command timed out", False
            deadline.wait(step)
            waited += step
            step = min(step * 1.5, 1.0)
        if result is None:
            return None, "", "the command produced no result", False
        if result.exit_code is None:
            # FAILED covers both a non-zero exit and a command that never ran
            # (unreachable, auth failure, cancelled); only the latter has no
            # exit code, and only the latter is a connection failure.
            return (
                None,
                result.stdout,
                _failure_text(result.failure) or result.stderr,
                bool(result.truncated),
            )
        return result.exit_code, result.stdout, result.stderr, bool(result.truncated)

    # -- RemoteBackupStore ----------------------------------------------

    def ensure_directory(self, path: str) -> None:
        quoted = _q(path)
        code, _out, err, _truncated = self._run(
            f"mkdir -p {quoted} && test -w {quoted}", timeout=self._command_timeout
        )
        if code is None:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED,
                diagnostic=err or "ssh failed",
            )
        if code != 0:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_DIRECTORY_UNAVAILABLE,
                parameters={"directory": path},
                diagnostic=err.strip() or "permission denied",
            )

    def free_space_bytes(self, path: str) -> Optional[int]:
        # Best-effort: a missing or unusual df must not masquerade as an error.
        code, out, _err, _truncated = self._run(
            f"df -Pk {_q(path)} | tail -1", timeout=self._command_timeout
        )
        if code != 0:
            return None
        available_kb = _parse_df_avail_kb(out)
        return None if available_kb is None else available_kb * 1024

    def list_backups(self, path: str) -> List[BackupEntry]:
        code, out, err, _truncated = self._run(
            f"ls -1 {_q(path)}/*.spbk 2>/dev/null", timeout=self._command_timeout
        )
        if code is None:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED,
                diagnostic=err.strip() or "ssh failed",
            )
        if code != 0:
            # A missing or empty directory is "no backups", not a failure.
            return []
        return [
            backup_entry_for(line.strip())
            for line in out.splitlines()
            if line.strip()
        ]

    def upload(self, local_path: str, remote_path: str) -> None:
        with open(local_path, "rb") as handle:
            payload = base64.b64encode(handle.read())
        staged, final = _q(remote_path + ".part"), _q(remote_path)
        code, out, err, _truncated = self._run(
            f"base64 -d > {staged} && mv {staged} {final}",
            timeout=self._transfer_timeout,
            input_data=payload,
        )
        if code != 0:
            self._run(f"rm -f {staged}", timeout=self._command_timeout)
            if code is None:
                raise BackupError(
                    SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED,
                    diagnostic=err.strip() or "ssh failed",
                )
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_WRITE_FAILED,
                diagnostic=(err or out).strip() or "unknown error",
            )

    def download(self, remote_path: str, local_path: str) -> None:
        code, out, err, truncated = self._run(
            f"base64 < {_q(remote_path)}", timeout=self._transfer_timeout
        )
        if code != 0:
            raise BackupError(
                SecretTransferMessageCode.SSH_BACKUP_READ_FAILED,
                diagnostic=err.strip() or "unknown error",
            )
        if truncated:
            # Better to say the archive is too big for this transport than to
            # hand the import half an archive.
            raise BackupError(
                SecretTransferMessageCode.SSH_BACKUP_READ_FAILED,
                diagnostic=(
                    "the backup is too large to download without SFTP "
                    f"(limit is about {_EXEC_OUTPUT_LIMIT_BYTES * 3 // 4 // (1024 * 1024)} MB)"
                ),
            )
        try:
            raw = base64.b64decode("".join(out.split()), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BackupError(
                SecretTransferMessageCode.SSH_BACKUP_READ_FAILED,
                diagnostic="the downloaded archive was not valid base64",
            ) from exc
        with open(local_path, "wb") as handle:
            handle.write(raw)


# --- Selection ----------------------------------------------------------------


class BackupTransportProvider:
    """Hand out the best available remote store for one connection.

    SFTP first (it streams and needs no shell), the one-shot command transport
    second. Composed in the daemon server, where the runtimes exist; the backup
    code above never sees the difference.
    """

    def __init__(
        self,
        *,
        sftp_runtime: Any = None,
        transfer_runtime: Any = None,
        broadcast_service: Any = None,
    ) -> None:
        self._sftp_runtime = sftp_runtime
        self._transfer_runtime = transfer_runtime
        self._broadcast_service = broadcast_service

    def open(self, connection_id: str, *, client_id: ClientId):
        """A ready store for *connection_id*; close it when the operation ends.

        *client_id* is the frontend that asked for the backup, and it owns the
        SFTP service (or the one-shot command operation) this opens. Ownership
        is what makes the connect's own prompts -- a login password, a key
        passphrase, an unknown host key -- visible to that frontend through the
        interaction broker, exactly as the file manager's own service is. A
        store opened under any other identity authenticates against a client
        that cannot answer, so the connect can only wait for its timeout.
        """
        if self._sftp_runtime is not None and self._transfer_runtime is not None:
            store = SftpBackupStore(
                self._sftp_runtime,
                self._transfer_runtime,
                connection_id,
                client_id=client_id,
            )
            try:
                return store.__enter__()
            except BackupTransportUnavailable as exc:
                if self._broadcast_service is None:
                    raise BackupError(
                        SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED,
                        diagnostic=str(exc),
                    ) from exc
                logger.info(
                    "SFTP is unavailable for backups on %s; using one-shot commands (%s)",
                    connection_id,
                    exc,
                )
        if self._broadcast_service is None:
            raise BackupError(
                SecretTransferMessageCode.SSH_SERVER_CONNECTION_FAILED,
                diagnostic="no remote transport is available",
            )
        return ExecBackupStore(
            self._broadcast_service, connection_id, client_id=client_id
        )
