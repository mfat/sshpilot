"""Daemon-backed SFTP file-manager backend.

``DaemonSftpManager`` matches the public contract of ``OpenSSHSFTPManager``
(same GObject signals, constructor kwargs, and methods) so
``FileManagerWindow`` works with either backend interchangeably. Unlike
``OpenSSHSFTPManager`` it never spawns a local ``ssh -s sftp`` subprocess --
every remote filesystem operation and file transfer is delegated to the
sshPilot daemon over the existing Protocol v1 connection via
:class:`~sshpilot.sftp_service_controller.DaemonSftpServiceController` and
:class:`~sshpilot.transfer_service_controller.TransferServiceController`.

Host-key / password / passphrase prompts during connect reuse the same
``DaemonInteractionDialogs`` GTK surface already wired for daemon terminal
sessions -- no new askpass/interaction mechanism is introduced.

The daemon has no one-shot remote-exec RPC (Phase 10 scope is SFTP/transfers/
forwards only), so ``run_command``/``run_command_async`` -- used only for the
authorized_keys editor, SSH backup export, and the text editor's sudo-write
path, never for the interactive SFTP session itself -- fall back to the same
native connect/auth builder the legacy backend uses
(``build_ssh_connection`` + ``apply_headless_askpass_env``).
"""

from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from gi.repository import GObject

from .api.errors import ErrorCode, SshPilotError
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


def daemon_file_manager_capabilities_missing(client) -> frozenset:
    """Union of SFTP + transfer capabilities the file manager backend needs."""
    return daemon_sftp_capabilities_missing(client) | daemon_transfer_capabilities_missing(client)


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
        self._command_executor = ThreadPoolExecutor(max_workers=1)

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
        if self._parent_widget is not None and self._interaction_dialogs is None:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs

            self._interaction_dialogs = DaemonInteractionDialogs(
                self._client, self._bridge, self._parent_widget
            )
        self._sftp_controller.open(self._connection_id)

    def _on_service_state_changed(self, summary) -> None:
        service_id = self._sftp_controller.service_id
        if self._interaction_dialogs is not None and service_id is not None:
            self._interaction_dialogs.set_session(SessionId(str(service_id)))

    def _on_service_ready(self, summary) -> None:
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
        self.emit("connection-error", getattr(error, "message", str(error)))

    def close(self) -> None:
        """Detach and stop using the service.

        Phase 10 policy: panel teardown detaches (``sftp.detach``) rather
        than tearing down the daemon-owned SFTP service outright, so other
        attachments (or a reconnect) keep a live service. Use
        :meth:`disconnect_service` for an explicit ``sftp.close``.
        """
        if self._closed:
            return
        self._closed = True
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None
        self._sftp_controller.detach()
        self._transfers.close()
        try:
            self._command_executor.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Error shutting down command executor: %s", exc)

    def disconnect_service(self) -> None:
        """Explicitly terminate the daemon-owned SFTP service (``sftp.close``)."""
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None
        self._sftp_controller.close()
        self._transfers.close()
        self._closed = True
        try:
            self._command_executor.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Error shutting down command executor: %s", exc)

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
        if self._sftp_controller.state is not SftpControllerState.READY or service_id is None:
            raise OSError("SFTP connection is not available")
        return service_id

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

    def _is_cancelled(self, operation_id: str) -> bool:
        return operation_id in self._cancelled_operations

    def _discard_cancellation_flag(self, operation_id: str) -> None:
        self._cancelled_operations.discard(operation_id)

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
            self.emit("operation-error", str(exc))

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
        future: Future = Future()
        target = self._expand(path)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        total_holder = {"total": 0}

        def _sum_dir(current: str, on_done: Callable[[], None], on_error: Callable[[BaseException], None]) -> None:
            def _on_list(result) -> None:
                entries = list(result.entries)

                def _process(index: int) -> None:
                    if index >= len(entries):
                        on_done()
                        return
                    entry = entries[index]
                    if entry.file_type is RemoteFileType.DIRECTORY:
                        child = current.rstrip("/") + "/" + entry.name
                        _sum_dir(child, lambda: _process(index + 1), lambda _e: _process(index + 1))
                    else:
                        total_holder["total"] += int(entry.size or 0)
                        _process(index + 1)

                _process(0)

            self._sftp_controller.list_directory(current, on_success=_on_list, on_error=on_error)

        _sum_dir(target, lambda: self._safe_set(future, result=total_holder["total"]), lambda exc: self._safe_set(future, exc=exc))
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
            on_error=lambda e: self._safe_set(future, exc=e),
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
                self._safe_set(future, exc=exc)

        self._sftp_controller.stat(target, on_success=_on_success, on_error=_on_error)
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
            on_error=lambda e: self._safe_set(future, exc=e),
        )
        return future

    def touch(self, path: str) -> Future:
        future: Future = Future()
        target = self._expand(path)
        try:
            service_id = self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        tmp_fd, tmp_path = tempfile.mkstemp(prefix="sshpilot-touch-")
        os.close(tmp_fd)

        def _cleanup_tmp() -> None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

        def _on_done(summary: TransferSummary) -> None:
            _cleanup_tmp()
            if summary.state is TransferState.COMPLETED:
                self._safe_set(future, result=None)
            elif summary.state is TransferState.CANCELLED:
                self._safe_set(future, exc=TransferCancelledException("Touch was cancelled"))
            else:
                failure = summary.failure
                if failure is not None and failure.code == ErrorCode.TRANSFER_CONFLICT.value:
                    self._safe_set(future, exc=FileExistsError(target))
                else:
                    message = failure.message if failure is not None else "Could not create file"
                    self._safe_set(future, exc=OSError(message))

        def _on_error(exc) -> None:
            _cleanup_tmp()
            self._safe_set(future, exc=exc)

        self._transfers.start_transfer(
            StartTransferRequest(
                connection_id=self._connection_id,
                sftp_service_id=service_id,
                direction=TransferDirection.UPLOAD,
                remote_path=target,
                local_path=tmp_path,
                conflict_policy=TransferConflictPolicy.FAIL,
            ),
            on_done=_on_done,
            on_error=_on_error,
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

        def _remove_path(current: str, on_done: Callable[[], None], on_error: Callable[[BaseException], None]) -> None:
            def _on_stat(entry) -> None:
                if entry.file_type is RemoteFileType.DIRECTORY:
                    _remove_dir_contents(current, on_done, on_error)
                else:
                    self._sftp_controller.remove(current, on_success=lambda _r: on_done(), on_error=on_error)

            def _on_stat_error(exc) -> None:
                if isinstance(exc, SshPilotError) and exc.code is ErrorCode.REMOTE_PATH_NOT_FOUND:
                    on_done()
                else:
                    on_error(exc)

            self._sftp_controller.stat(current, follow_symlinks=False, on_success=_on_stat, on_error=_on_stat_error)

        def _remove_dir_contents(current: str, on_done: Callable[[], None], on_error: Callable[[BaseException], None]) -> None:
            def _on_list(result) -> None:
                entries = list(result.entries)

                def _process(index: int) -> None:
                    if index >= len(entries):
                        self._sftp_controller.rmdir(current, on_success=lambda _r: on_done(), on_error=on_error)
                        return
                    child = current.rstrip("/") + "/" + entries[index].name
                    _remove_path(child, lambda: _process(index + 1), on_error)

                _process(0)

            self._sftp_controller.list_directory(current, on_success=_on_list, on_error=on_error)

        _remove_path(
            target,
            lambda: self._safe_set(future, result=None),
            lambda exc: self._safe_set(future, exc=exc),
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
            message = failure.message if failure is not None else "Transfer failed"
            self._safe_set(future, exc=OSError(message))

    @staticmethod
    def _cleanup_local(destination: pathlib.Path) -> None:
        try:
            if destination.exists():
                destination.unlink()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("Partial download cleanup failed: %s", exc)

    def _transfer_manifest(
        self,
        future: Future,
        manifest: List[Tuple[str, pathlib.Path, int]],
        direction: TransferDirection,
        operation_id: str,
        state: Dict[str, Any],
        grand_total: int,
    ) -> None:
        """Sequentially transfer a (remote_path, local_path, size) manifest,
        tracking cumulative bytes for one overall progress signal."""
        if future.done():
            return
        remaining = list(manifest)
        transferred_base = 0

        def _next_file() -> None:
            nonlocal transferred_base
            if future.done():
                return
            if self._is_cancelled(operation_id):
                self._safe_set(future, exc=TransferCancelledException("Transfer was cancelled"))
                return
            if not remaining:
                self._discard_cancellation_flag(operation_id)
                self.emit("progress", 1.0, "Transfer complete")
                self._safe_set(future, result=transferred_base)
                return
            remote_path, local_path, _size = remaining.pop(0)
            base = transferred_base

            def _on_progress(summary: TransferSummary) -> None:
                state["transfer_id"] = summary.id
                self._emit_transfer_progress(base, summary, grand_total)

            def _on_done(summary: TransferSummary) -> None:
                nonlocal transferred_base
                if summary.state is TransferState.COMPLETED:
                    transferred_base = base + summary.bytes_completed
                    _next_file()
                elif summary.state is TransferState.CANCELLED:
                    self._safe_set(future, exc=TransferCancelledException("Transfer was cancelled"))
                else:
                    failure = summary.failure
                    message = failure.message if failure is not None else f"Transfer failed: {remote_path}"
                    self._safe_set(future, exc=OSError(message))

            def _on_error(exc) -> None:
                self._safe_set(future, exc=exc)

            try:
                service_id = self._require_ready_service_id()
            except OSError as exc:
                self._safe_set(future, exc=exc)
                return

            if direction is TransferDirection.DOWNLOAD:
                try:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                except Exception as exc:  # pragma: no cover - best effort
                    logger.debug("Download directory: prepare parent failed: %s", exc)

            self._transfers.start_transfer(
                StartTransferRequest(
                    connection_id=self._connection_id,
                    sftp_service_id=service_id,
                    direction=direction,
                    remote_path=remote_path,
                    local_path=str(local_path),
                    conflict_policy=TransferConflictPolicy.OVERWRITE,
                ),
                on_progress=_on_progress,
                on_done=_on_done,
                on_error=_on_error,
            )

        _next_file()

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        future: Future = Future()
        remote_root = self._expand(source)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        self.emit("progress", 0.0, "Scanning…")
        manifest: List[Tuple[str, pathlib.Path, int]] = []
        operation_id = self._next_operation_id("download_dir")
        state: Dict[str, Any] = {"transfer_id": None}

        def _list_dir(remote_dir: str, local_dir: pathlib.Path, on_done: Callable[[], None]) -> None:
            if future.done() or self._is_cancelled(operation_id):
                on_done()
                return
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._safe_set(future, exc=exc)
                return

            def _on_success(result) -> None:
                entries = list(result.entries)

                def _process(index: int) -> None:
                    if index >= len(entries):
                        on_done()
                        return
                    entry = entries[index]
                    child_remote = remote_dir.rstrip("/") + "/" + entry.name
                    child_local = local_dir / entry.name
                    if entry.file_type is RemoteFileType.DIRECTORY:
                        _list_dir(child_remote, child_local, lambda: _process(index + 1))
                    else:
                        manifest.append((child_remote, child_local, int(entry.size or 0)))
                        _process(index + 1)

                _process(0)

            def _on_error(exc) -> None:
                self._safe_set(future, exc=exc)

            self._sftp_controller.list_directory(remote_dir, on_success=_on_success, on_error=_on_error)

        def _after_scan() -> None:
            if future.done():
                return
            grand_total = sum(size for _r, _l, size in manifest)
            self._transfer_manifest(
                future, manifest, TransferDirection.DOWNLOAD, operation_id, state, grand_total
            )

        _list_dir(remote_root, destination, _after_scan)
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        future: Future = Future()
        remote_root = self._expand(destination)
        try:
            self._require_ready_service_id()
        except OSError as exc:
            future.set_exception(exc)
            return future

        self.emit("progress", 0.0, "Scanning…")
        manifest: List[Tuple[str, pathlib.Path, int]] = []
        directories: List[str] = []
        try:
            for root, _dirs, names in os.walk(source):
                rel = os.path.relpath(root, source)
                remote_dir = (
                    remote_root.rstrip("/")
                    if rel == "."
                    else remote_root.rstrip("/") + "/" + rel.replace(os.sep, "/")
                )
                directories.append(remote_dir)
                for name in names:
                    local_path = pathlib.Path(root) / name
                    remote_path = remote_dir.rstrip("/") + "/" + name
                    try:
                        size = local_path.stat().st_size
                    except OSError:
                        size = 0
                    manifest.append((remote_path, local_path, size))
        except Exception as exc:
            future.set_exception(exc)
            return future

        grand_total = sum(size for _r, _l, size in manifest)
        operation_id = self._next_operation_id("upload_dir")
        state: Dict[str, Any] = {"transfer_id": None}

        def _mkdirs_then_transfer(index: int = 0) -> None:
            if future.done() or self._is_cancelled(operation_id):
                self._safe_set(future, exc=TransferCancelledException("Transfer was cancelled"))
                return
            if index >= len(directories):
                self._transfer_manifest(
                    future, manifest, TransferDirection.UPLOAD, operation_id, state, grand_total
                )
                return
            remote_dir = directories[index]
            self._sftp_controller.mkdir(
                remote_dir,
                on_success=lambda _r: _mkdirs_then_transfer(index + 1),
                # Already-exists / permission — let the file write surface it,
                # mirroring OpenSSHSFTPManager._mkdirs.
                on_error=lambda _e: _mkdirs_then_transfer(index + 1),
            )

        _mkdirs_then_transfer()
        return self._cancellable(future, operation_id, lambda: state["transfer_id"])

    # -- one-shot remote command execution ---------------------------------
    def _build_run_command_argv(self, command: str) -> Tuple[List[str], Dict[str, str]]:
        """Build a one-shot ``ssh <host> <command>`` argv via the shared
        native connect/auth path -- see module docstring for why this one
        ancillary path (no interactive SFTP session) still needs it."""
        from .ssh_connection_builder import (
            ConnectionContext,
            apply_headless_askpass_env,
            build_ssh_connection,
        )

        app_config = None
        try:
            from .config import Config

            app_config = Config()
        except Exception:  # pragma: no cover - defensive
            app_config = None

        if self._password and self._connection is not None:
            try:
                self._connection.password = self._password
            except Exception:  # pragma: no cover - defensive
                pass

        ctx = ConnectionContext(
            connection=self._connection,
            connection_manager=self._connection_manager,
            config=app_config,
            command_type="sftp",
            native_mode=True,
            extra_args=[],
            remote_command=command,
        )
        prepared = build_ssh_connection(ctx)
        argv = list(prepared.command)
        env = apply_headless_askpass_env(
            prepared.env,
            self._connection,
            session_password=getattr(prepared, "password", None) or self._password,
        )
        return argv, env

    def run_command(
        self, command: str, *, input: Optional[bytes] = None, timeout: float = 30
    ) -> Tuple[int, bytes, str]:
        """Run a one-shot command on this host, blocking. See module
        docstring: this is not the daemon-owned interactive SFTP session."""
        argv, env = self._build_run_command_argv(command)
        try:
            proc = subprocess.run(
                argv, env=env, input=input, capture_output=True, timeout=timeout
            )
            stderr = (proc.stderr or b"").decode("utf-8", "replace")
            return proc.returncode, (proc.stdout or b""), stderr
        except subprocess.TimeoutExpired:
            return -1, b"", "Command timed out"
        except Exception as exc:  # noqa: BLE001 - surface as a failed result
            return -1, b"", str(exc)

    def run_command_async(
        self, command: str, *, input: Optional[bytes] = None, timeout: float = 30
    ) -> Future:
        return self._command_executor.submit(
            self.run_command, command, input=input, timeout=timeout
        )
