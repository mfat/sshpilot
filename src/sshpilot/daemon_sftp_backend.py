"""Daemon-backed SFTP file-manager backend.

``DaemonSftpManager`` matches the file-manager presentation contract (same
GObject signals, constructor kwargs, and methods) so ``FileManagerWindow``
can remain presentation-focused. It never spawns a local ``ssh -s sftp`` subprocess --
every remote filesystem operation and file transfer is delegated to the
sshPilot daemon over the existing Protocol v1 connection via
:class:`~sshpilot.sftp_service_controller.DaemonSftpServiceController` and
:class:`~sshpilot.transfer_service_controller.TransferServiceController`.

Host-key / password / passphrase prompts during connect reuse the same
``DaemonInteractionDialogs`` GTK surface already wired for daemon terminal
sessions -- no new askpass/interaction mechanism is introduced.

There is intentionally no one-shot ``ssh <host> <command>`` escape hatch here:
every remote action (including the text editor's privileged sudo read/write
and directory transfers) is owned by the daemon through the typed API.
"""

from __future__ import annotations

import errno
import logging
import os
import pathlib
import threading
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Set

from gi.repository import GObject

from .api.errors import ErrorCode, SshPilotError
from .api.capabilities import Capability
from .api.models.common import SessionId, SftpServiceId
from .api.models.operations import RemoteFileType
from .api.models.transfers import (
    StartTransferRequest,
    TransferConflictPolicy,
    TransferDirection,
    TransferState,
    TransferSummary,
)
from .file_manager.common import FileEntry
from .file_manager.exceptions import TransferCancelledException
from .gtk.sftp_error_messages import format_direct_sftp_error
from .gtk.sftp_failure_messages import format_sftp_failure
from .sftp_service_controller import (
    DaemonSftpServiceController,
    SftpControllerState,
    daemon_sftp_capabilities_missing,
)
from .transfer_service_controller import (
    TransferServiceController,
    daemon_transfer_capabilities_missing,
)

logger = logging.getLogger(__name__)


def _localized_direct_error(error: BaseException) -> BaseException:
    """Clone a direct RPC error with its frontend-rendered display message."""

    if not isinstance(error, SshPilotError):
        return error
    message = format_direct_sftp_error(error)
    if message == str(error):
        return error
    return SshPilotError(
        error.code,
        message,
        details=error.details,
        retryable=error.retryable,
        request_id=error.request_id,
        connection_id=error.connection_id,
        session_id=error.session_id,
    )


def daemon_file_manager_capabilities_missing(client) -> frozenset:
    """Union of SFTP + transfer capabilities the file manager backend needs."""
    return daemon_sftp_capabilities_missing(client) | daemon_transfer_capabilities_missing(client)


# Which live backends are using each daemon SFTP service, so the last one out
# can end it. The daemon's own attachment count cannot answer this: it counts
# client *connections*, and every view in this app shares one, so two file
# managers on the same service register as a single attachment.
_SERVICE_USERS: Dict[str, Set[int]] = {}
_SERVICE_USERS_LOCK = threading.Lock()


def _register_service_user(service_id, user_id: int) -> None:
    if not service_id:
        return
    with _SERVICE_USERS_LOCK:
        _SERVICE_USERS.setdefault(str(service_id), set()).add(user_id)


def _release_service_user(service_id, user_id: int) -> bool:
    """Drop one user of *service_id*; True when nothing is using it any more."""
    if not service_id:
        return False
    key = str(service_id)
    with _SERVICE_USERS_LOCK:
        users = _SERVICE_USERS.get(key)
        if users is None:
            # Never registered (torn down before the service came up, or already
            # released): closing is still the right call for the caller that
            # holds it, and the daemon ignores a close of an unknown service.
            return True
        users.discard(user_id)
        if users:
            return False
        del _SERVICE_USERS[key]
        return True


def _remote_entry_to_file_entry(entry) -> FileEntry:
    return FileEntry(
        name=entry.name,
        is_dir=entry.file_type is RemoteFileType.DIRECTORY,
        size=int(entry.size or 0),
        modified=(entry.modified_at.timestamp() if entry.modified_at else 0.0),
    )


class _NoOpLock:
    """Compatibility stand-in for ``threading.Lock`` (see ``_sftp`` below).

    Window code does ``with self._manager._lock:`` purely to read
    ``self._manager._sftp`` for a liveness check; every daemon RPC already
    goes through the bridge's own synchronization, so nothing here needs
    real mutual exclusion.
    """

    def __enter__(self) -> "_NoOpLock":
        return self

    def __exit__(self, *_exc) -> None:
        return None


class DaemonSftpManager(GObject.GObject):
    """File-manager backend driven by the daemon's SFTP/transfer RPCs."""

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "authentication-required": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "progress-bytes": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "operation-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "directory-loaded": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
        "directory-counts": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Optional[Callable[[Callable, tuple, dict], None]] = None,
        connection: Any = None,
        connection_manager: Any = None,
        ssh_config: Optional[Dict[str, Any]] = None,
        connection_id: Any = None,
        daemon_client: Any = None,
        bridge: Any = None,
        parent_widget: Any = None,
    ) -> None:
        super().__init__()
        del dispatcher  # unused: bridge callbacks already land on the GTK main thread
        if connection_id is None or daemon_client is None or bridge is None:
            raise ValueError(
                "DaemonSftpManager requires connection_id, daemon_client, and bridge"
            )
        missing = daemon_file_manager_capabilities_missing(daemon_client)
        if missing:
            raise RuntimeError(
                f"Required daemon SFTP/transfer capabilities unavailable: {missing}"
            )

        self._host = host
        self._username = username
        self._port = port or 22
        self._password = password
        self._connection = connection
        self._connection_manager = connection_manager
        self._ssh_config = dict(ssh_config) if ssh_config else None
        self._connection_id = connection_id
        self._client = daemon_client
        self._bridge = bridge
        self._parent_widget = parent_widget

        self._closed = False
        self._home: Optional[str] = None
        self._cancelled_operations: set = set()
        self._operation_seq = 0
        self._lock = _NoOpLock()  # compatibility alias, see class docstring
        self._interaction_dialogs = None

        self._sftp_controller = DaemonSftpServiceController(
            daemon_client,
            bridge,
            connection_id,
            on_ready=self._on_service_ready,
            on_state_changed=self._on_service_state_changed,
            on_error=self._on_service_error,
        )
        self._transfers = TransferServiceController(daemon_client, bridge)

    # -- compatibility aliases (mirrors OpenSSHSFTPManager) ---------------
    @property
    def _sftp(self):
        """Non-None while a READY SFTP service is attached (paramiko alias).

        Some window/dialog code probes ``manager._sftp`` for liveness only
        (``is None`` checks) -- see ``OpenSSHSFTPManager._sftp``.
        """
        if self._closed or self._sftp_controller.state is not SftpControllerState.READY:
            return None
        return self._sftp_controller

    @property
    def host(self) -> str:
        return self._host

    @property
    def username(self) -> str:
        return self._username

    def is_connected(self) -> bool:
        return not self._closed and self._sftp_controller.state is SftpControllerState.READY

    # -- connection ---------------------------------------------------
    def connect_to_server(self, password: Optional[str] = None) -> None:
        if password is not None:
            self._password = password
        logger.debug(
            "Daemon SFTP connect_to_server for %s@%s connection_id=%s",
            self._username,
            self._host,
            self._connection_id,
        )
        if self._parent_widget is not None and self._interaction_dialogs is None:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs

            self._interaction_dialogs = DaemonInteractionDialogs(
                self._client, self._bridge, self._parent_widget
            )
        self._sftp_controller.open(self._connection_id)

    def _on_service_state_changed(self, summary) -> None:
        service_id = self._sftp_controller.service_id
        _register_service_user(service_id, id(self))
        if self._interaction_dialogs is not None and service_id is not None:
            self._interaction_dialogs.set_session(SessionId(str(service_id)))

    def _on_service_ready(self, summary) -> None:
        _register_service_user(self._sftp_controller.service_id, id(self))
        logger.debug(
            "Daemon SFTP service ready for %s@%s id=%s state=%s",
            self._username,
            self._host,
            getattr(summary, "id", None),
            getattr(summary, "state", None),
        )
        self._resolve_home()

    def _resolve_home(self) -> None:
        def _on_success(path: str) -> None:
            self._home = path
            self.emit("connected")

        def _on_error(_exc) -> None:
            # Home resolution is best-effort -- still report connected.
            self.emit("connected")

        self._sftp_controller.realpath(".", on_success=_on_success, on_error=_on_error)

    def _on_service_error(self, error: BaseException) -> None:
        # Host-key/password/passphrase/MFA prompts are handled by
        # DaemonInteractionDialogs (the daemon's interaction broker), so
        # unlike the legacy backend this never emits authentication-required.
        message = getattr(error, "message", str(error))
        # This signal is service-level (auth/startup/session death), never a
        # per-command failure. A connection-lost SFTP status used to arrive
        # here with the canned "The SFTP command failed" text.
        if message == "The SFTP command failed":
            message = "The SFTP connection was lost"
        logger.warning(
            "Daemon SFTP service error for %s@%s: %s",
            self._username,
            self._host,
            message,
        )
        logger.debug(
            "Daemon SFTP service error detail for %s@%s",
            self._username,
            self._host,
            exc_info=error,
        )
        self.emit("connection-error", message)

    def close(self) -> None:
        """Stop using the service, ending it when nothing else needs it.

        Detaching alone left the daemon holding a READY service with no view
        attached: an SSH connection to the host that outlived the file manager
        that opened it, and a sidebar indicator that stayed green for a tab the
        user had closed (GH #1193). A service is still shared when SSH
        multiplexing reuses one across views, so end it only once the last view
        lets go; the others keep a live service exactly as before.
        """
        if self._closed:
            return
        self._closed = True
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None
        if _release_service_user(self._sftp_controller.service_id, id(self)):
            self._sftp_controller.close()
        else:
            self._sftp_controller.detach()
        self._transfers.close()

    def disconnect_service(self) -> None:
        """Terminate the daemon-owned SFTP service now (``sftp.close``).

        Unconditional: unlike :meth:`close` this does not wait for other views
        to let go.
        """
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None
        _release_service_user(self._sftp_controller.service_id, id(self))
        self._sftp_controller.close()
        self._transfers.close()
        self._closed = True

    # -- path helpers -------------------------------------------------
    def _expand(self, path: str) -> str:
        if path == "~":
            return self._home or "."
        if path.startswith("~/"):
            home = self._home or "."
            return home.rstrip("/") + "/" + path[2:]
        return path

    def _require_ready_service_id(self) -> SftpServiceId:
        service_id = self._sftp_controller.service_id
        state = self._sftp_controller.state
        if state is not SftpControllerState.READY or service_id is None:
            logger.warning(
                "SFTP connection is not available for %s@%s "
                "(closed=%s, controller_state=%s, service_id=%s, connection_id=%s)",
                self._username,
                self._host,
                self._closed,
                state.value if hasattr(state, "value") else state,
                service_id,
                self._connection_id,
            )
            raise OSError("SFTP connection is not available")
        return service_id

    def make_file_editor_service(self, path: str) -> Any:
        """Build a daemon-backed file editor service for a remote ``path``.

        The returned adapter performs the read/replace (and their sudo
        variants) through the daemon SFTP file RPCs; the editor never falls
        back to a frontend-owned SSH command."""
        from .remote_file_editor_service import DaemonRemoteFileService

        service_id = self._require_ready_service_id()
        capabilities = getattr(self._client, "get_capabilities", None)
        privileged = False
        if capabilities is not None:
            try:
                privileged = capabilities().supports(Capability.SFTP_PRIVILEGED_FILE)
            except Exception:  # noqa: BLE001 - capability inspection must not block editing
                logger.debug("Failed to inspect privileged-file capability", exc_info=True)
        return DaemonRemoteFileService(
            self._client,
            service_id,
            path,
            privileged_supported=privileged,
        )

    @staticmethod
    def _format_size(num_bytes: float) -> str:
        size = float(num_bytes)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return f"{size:.1f} TB"

    # -- futures / cancellation ----------------------------------------
    def _next_operation_id(self, kind: str) -> str:
        self._operation_seq += 1
        return f"{kind}_{id(self)}_{self._operation_seq}"

    @staticmethod
    def _safe_set(future: Future, *, result: Any = None, exc: Optional[BaseException] = None) -> None:
        if future.done():
            return
        if exc is not None:
            future.set_exception(exc)
        else:
            future.set_result(result)

    def _cancellable(
        self,
        future: Future,
        operation_id: str,
        get_transfer_id: Callable[[], Optional[Any]] = lambda: None,
    ) -> Future:
        """Cooperative cancel: mark cancelled + cancel the active daemon
        transfer (if any); the future itself resolves later with
        ``TransferCancelledException`` once the daemon confirms. Mirrors
        ``OpenSSHSFTPManager._cancellable`` -- ``future.cancelled()`` stays
        False, but ``.exception()``/``.done()`` reflect the cancellation."""

        def cancel_with_cleanup() -> bool:
            if future.done():
                return False
            self._cancelled_operations.add(operation_id)
            transfer_id = get_transfer_id()
            if transfer_id is not None:
                self._transfers.cancel_transfer(transfer_id)
            return True

        future.cancel = cancel_with_cleanup
        return future

    def _operation_cancellable(self, future: Future) -> tuple:
        """Wire ``future.cancel()`` to the daemon's ``operations.cancel``.

        The daemon operation id is only known once the start RPC returns, so
        this tracks a pending-cancel flag: if ``future.cancel()`` is called
        before the id arrives, the cancel is issued as soon as it does
        (``on_operation_started``), instead of being silently dropped.
        """
        state: Dict[str, Any] = {"operation_id": None, "cancel_requested": False}

        def _on_operation_started(operation_id) -> None:
            state["operation_id"] = operation_id
            if state["cancel_requested"]:
                self._sftp_controller.cancel_operation(operation_id)

        def cancel_with_cleanup() -> bool:
            if future.done():
                return False
            state["cancel_requested"] = True
            if state["operation_id"] is not None:
                self._sftp_controller.cancel_operation(state["operation_id"])
            return True

        future.cancel = cancel_with_cleanup
        return future, _on_operation_started

    @staticmethod
    def _resolve_operation_exception(exc: BaseException) -> BaseException:
        """Map a cancelled daemon operation onto the file-manager's cancel type.

        Mirrors ``_finish_transfer``'s ``TransferCancelledException`` handling
        so operation-backed futures (directory size, recursive copy/move,
        recursive remove) look the same to callers as transfer futures.
        """
        if isinstance(exc, SshPilotError) and exc.code is ErrorCode.OPERATION_CANCELLED:
            return TransferCancelledException(str(exc) or "Operation was cancelled")
        return exc

    # -- directory listing ------------------------------------------------
    def listdir(self, path: str) -> None:
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            self.emit("operation-error", str(exc))
            return

        def _on_success(result) -> None:
            entries = [_remote_entry_to_file_entry(e) for e in result.entries]
            self.emit("directory-loaded", target, entries)
            self._start_count_pass(target, entries)

        def _on_error(exc) -> None:
            self.emit("operation-error", format_direct_sftp_error(exc))

        self._sftp_controller.list_directory(target, on_success=_on_success, on_error=_on_error)

    def _start_count_pass(self, path: str, entries: List[FileEntry]) -> None:
        folders = [e.name for e in entries if e.is_dir]
        if not folders:
            return

        def _count_next(index: int) -> None:
            # Quit / panel close abandons the background pass — do not kick
            # off another list_directory against a detached SFTP service.
            if self._closed or index >= len(folders):
                return
            name = folders[index]
            child = path.rstrip("/") + "/" + name

            def _on_success(result) -> None:
                if self._closed:
                    return
                self.emit("directory-counts", path, {name: len(result.entries)})
                _count_next(index + 1)

            def _on_error(exc) -> None:
                if self._closed:
                    return
                if (
                    isinstance(exc, SshPilotError)
                    and exc.code is ErrorCode.SFTP_SERVICE_NOT_READY
                ):
                    return
                _count_next(index + 1)

            self._sftp_controller.list_directory(
                child, on_success=_on_success, on_error=_on_error
            )

        _count_next(0)

    def directory_size(self, path: str) -> Future:
        """Future resolving to the recursive byte size of a remote directory.

        The whole tree walk happens daemon-side as a long-lived operation
        (``sftp.directory_size`` returns an ``OperationSummary``); the frontend
        resolves the size from the succeeded operation's typed result and never
        recurses through per-directory listings.
        """
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        future, on_operation_started = self._operation_cancellable(future)

        def _on_success(result) -> None:
            self._safe_set(future, result=result.size_bytes)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=self._resolve_operation_exception(exc))

        def _on_progress(summary) -> None:
            self.emit(
                "progress",
                summary.progress or 0.0,
                summary.message or "Measuring directory…",
            )

        self._sftp_controller.directory_size(
            target,
            on_success=_on_success,
            on_error=_on_error,
            on_operation_started=on_operation_started,
            on_progress=_on_progress,
        )
        return future

    # -- simple operations ------------------------------------------------
    def mkdir(self, path: str) -> Future:
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future
        self._sftp_controller.mkdir(
            target,
            on_success=lambda r: self._safe_set(future, result=r),
            on_error=lambda e: self._safe_set(
                future, exc=_localized_direct_error(e)
            ),
        )
        return future

    def path_exists(self, path: str) -> Future:
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        def _on_success(_entry) -> None:
            self._safe_set(future, result=True)

        def _on_error(exc) -> None:
            if isinstance(exc, SshPilotError) and exc.code is ErrorCode.REMOTE_PATH_NOT_FOUND:
                self._safe_set(future, result=False)
            else:
                self._safe_set(future, exc=_localized_direct_error(exc))

        self._sftp_controller.stat(target, on_success=_on_success, on_error=_on_error)
        return future

    def stat(self, path: str, *, follow_symlinks: bool = True) -> Future:
        """Fetch typed daemon metadata for a remote ``path``.

        Resolves to a :class:`~sshpilot.api.models.operations.RemoteFileEntry`
        with mode, uid, gid, and modification time owned by the daemon.
        """
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        def _on_success(entry) -> None:
            self._safe_set(future, result=entry)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=_localized_direct_error(exc))

        self._sftp_controller.stat(
            target,
            follow_symlinks=follow_symlinks,
            on_success=_on_success,
            on_error=_on_error,
        )
        return future

    def rename(self, source: str, target: str) -> Future:
        future: Future = Future()
        source = self._expand(source)
        target = self._expand(target)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future
        self._sftp_controller.rename(
            source,
            target,
            on_success=lambda r: self._safe_set(future, result=r),
            on_error=lambda e: self._safe_set(
                future, exc=_localized_direct_error(e)
            ),
        )
        return future

    def touch(self, path: str) -> Future:
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        def _on_error(exc) -> None:
            if isinstance(exc, SshPilotError) and exc.code is ErrorCode.REMOTE_PATH_EXISTS:
                self._safe_set(
                    future,
                    exc=FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), target),
                )
            elif isinstance(exc, SshPilotError):
                self._safe_set(future, exc=OSError(format_direct_sftp_error(exc)))
            else:
                self._safe_set(future, exc=exc)

        self._sftp_controller.create_file(
            target,
            on_success=lambda _result: self._safe_set(future, result=None),
            on_error=_on_error,
        )
        return future

    def copy_remote(
        self,
        source: str,
        destination: str,
        *,
        recursive: bool = False,
        move: bool = False,
    ) -> Future:
        future: Future = Future()
        source = self._expand(source)
        destination = self._expand(destination)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        on_operation_started = None
        on_progress = None
        if recursive:
            future, on_operation_started = self._operation_cancellable(future)
            verb = "Moving" if move else "Copying"

            def on_progress(summary) -> None:
                self.emit(
                    "progress", summary.progress or 0.0, summary.message or f"{verb}…"
                )

        def _on_error(exc) -> None:
            resolved = self._resolve_operation_exception(exc)
            if not recursive:
                resolved = _localized_direct_error(resolved)
            self._safe_set(future, exc=resolved)

        self._sftp_controller.copy(
            source,
            destination,
            recursive=recursive,
            move=move,
            on_success=lambda _result: self._safe_set(future, result=None),
            on_error=_on_error,
            on_operation_started=on_operation_started,
            on_progress=on_progress,
        )
        return future

    def remove(self, path: str) -> Future:
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        future, on_operation_started = self._operation_cancellable(future)

        def _on_progress(summary) -> None:
            self.emit(
                "progress", summary.progress or 0.0, summary.message or "Deleting…"
            )

        self._sftp_controller.remove(
            target,
            recursive=True,
            on_success=lambda _result: self._safe_set(future, result=None),
            on_error=lambda exc: self._safe_set(
                future, exc=self._resolve_operation_exception(exc)
            ),
            on_operation_started=on_operation_started,
            on_progress=_on_progress,
        )
        return future

    # -- transfers --------------------------------------------------------
    def _emit_transfer_progress(self, base: int, summary: TransferSummary, grand_total: int) -> None:
        done = base + summary.bytes_completed
        self.emit("progress-bytes", done, grand_total)
        if grand_total > 0:
            self.emit(
                "progress",
                done / grand_total,
                f"Transferred {self._format_size(done)} of {self._format_size(grand_total)}",
            )
        else:
            self.emit("progress", 0.0, f"Transferred {self._format_size(done)}")

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        future: Future = Future()
        target = self._expand(destination)
        try:
            service_id = self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        try:
            total = int(source.stat().st_size)
        except OSError:
            total = 0
        state = {"transfer_id": None}

        def _on_progress(summary: TransferSummary) -> None:
            state["transfer_id"] = summary.id
            self._emit_transfer_progress(0, summary, total)

        def _on_done(summary: TransferSummary) -> None:
            self._finish_transfer(future, summary)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=exc)

        self.emit("progress", 0.0, "Starting upload…")
        self._transfers.start_transfer(
            StartTransferRequest(
                connection_id=self._connection_id,
                sftp_service_id=service_id,
                direction=TransferDirection.UPLOAD,
                remote_path=target,
                local_path=str(source),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            ),
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )
        operation_id = self._next_operation_id("upload")
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])

    def download(self, source: str, destination: pathlib.Path) -> Future:
        future: Future = Future()
        target = self._expand(source)
        try:
            service_id = self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Download: prepare parent failed: %s", exc)

        state = {"transfer_id": None}

        def _on_progress(summary: TransferSummary) -> None:
            state["transfer_id"] = summary.id
            self._emit_transfer_progress(0, summary, summary.bytes_total or 0)

        def _on_done(summary: TransferSummary) -> None:
            if summary.state is TransferState.CANCELLED:
                self._cleanup_local(destination)
            self._finish_transfer(future, summary)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=exc)

        self.emit("progress", 0.0, "Starting download…")
        self._transfers.start_transfer(
            StartTransferRequest(
                connection_id=self._connection_id,
                sftp_service_id=service_id,
                direction=TransferDirection.DOWNLOAD,
                remote_path=target,
                local_path=str(destination),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
            ),
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )
        operation_id = self._next_operation_id("download")
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])

    def _finish_transfer(self, future: Future, summary: TransferSummary) -> None:
        if summary.state is TransferState.COMPLETED:
            self._safe_set(future, result=summary.bytes_completed)
        elif summary.state is TransferState.CANCELLED:
            self._safe_set(future, exc=TransferCancelledException("Transfer was cancelled"))
        else:
            failure = summary.failure
            message = (
                format_sftp_failure(failure)
                if failure is not None
                else "Transfer failed"
            )
            self._safe_set(future, exc=OSError(message))

    @staticmethod
    def _cleanup_local(destination: pathlib.Path) -> None:
        try:
            if destination.exists():
                destination.unlink()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Partial download cleanup failed: %s", exc)

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        """Download a remote directory tree through a single daemon transfer."""
        future: Future = Future()
        target = self._expand(source)
        try:
            service_id = self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        state: Dict[str, Any] = {"transfer_id": None}

        def _on_progress(summary: TransferSummary) -> None:
            state["transfer_id"] = summary.id
            self._emit_transfer_progress(0, summary, summary.bytes_total or 0)

        def _on_done(summary: TransferSummary) -> None:
            self._finish_transfer(future, summary)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=exc)

        self.emit("progress", 0.0, "Starting download…")
        self._transfers.start_transfer(
            StartTransferRequest(
                connection_id=self._connection_id,
                sftp_service_id=service_id,
                direction=TransferDirection.DOWNLOAD,
                remote_path=target,
                local_path=str(destination),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
                recursive=True,
            ),
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )
        operation_id = self._next_operation_id("download_dir")
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        """Upload a local directory tree through a single daemon transfer."""
        future: Future = Future()
        remote_root = self._expand(destination)
        try:
            service_id = self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        state: Dict[str, Any] = {"transfer_id": None}

        def _on_progress(summary: TransferSummary) -> None:
            state["transfer_id"] = summary.id
            self._emit_transfer_progress(0, summary, summary.bytes_total or 0)

        def _on_done(summary: TransferSummary) -> None:
            self._finish_transfer(future, summary)

        def _on_error(exc) -> None:
            self._safe_set(future, exc=exc)

        self.emit("progress", 0.0, "Starting upload…")
        self._transfers.start_transfer(
            StartTransferRequest(
                connection_id=self._connection_id,
                sftp_service_id=service_id,
                direction=TransferDirection.UPLOAD,
                remote_path=remote_root,
                local_path=str(source),
                conflict_policy=TransferConflictPolicy.OVERWRITE,
                recursive=True,
            ),
            on_progress=_on_progress,
            on_done=_on_done,
            on_error=_on_error,
        )
        operation_id = self._next_operation_id("upload_dir")
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])
