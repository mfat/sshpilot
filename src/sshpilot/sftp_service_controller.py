"""GTK-facing daemon SFTP service controller.

Mirrors :mod:`sshpilot.terminal_session_controller`: every RPC goes through
``bridge.submit``, lifecycle is driven by ``sftp.*`` events, and stale
callbacks are suppressed with a generation counter. Used by
:mod:`sshpilot.daemon_sftp_backend`.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from typing import Callable, Dict, Optional

from gi.repository import GLib

from .api.capabilities import Capability
from .api.errors import ErrorCode, SshPilotError
from .api.events import EventType
from .api.models.common import ConnectionId, SftpServiceId
from .api.models.operations import (
    AttachSftpRequest,
    CloseSftpRequest,
    ListDirectoryRequest,
    ListDirectoryResult,
    OpenSftpRequest,
    OperationId,
    OperationState,
    OperationSummary,
    RemoteFileEntry,
    SftpChmodRequest,
    SftpCopyRequest,
    SftpCreateFileRequest,
    SftpCreateFileResult,
    SftpDirectorySizeRequest,
    SftpDirectorySizeResult,
    SftpFileTarget,
    SftpPathRequest,
    SftpReadFileRequest,
    SftpReadFileResult,
    SftpReplaceFileRequest,
    SftpRenameRequest,
    SftpServiceState,
    SftpServiceSummary,
    SftpSymlinkRequest,
    is_terminal_operation_state,
)
from .api.transport.codec import sftp_directory_size_result_from_wire

logger = logging.getLogger(__name__)

_SFTP_EVENT_TYPES = frozenset(
    {
        EventType.SFTP_CREATED,
        EventType.SFTP_STATE_CHANGED,
        EventType.SFTP_CLOSED,
        EventType.SFTP_FAILED,
    }
)


class SftpControllerState(str, Enum):
    IDLE = "idle"
    OPENING = "opening"
    READY = "ready"
    CLOSING = "closing"
    FAILED = "failed"
    CLOSED = "closed"
    DETACHED = "detached"


def required_daemon_sftp_capabilities() -> frozenset:
    return frozenset(
        {
            Capability.SFTP_READ,
            Capability.SFTP_WRITE,
            Capability.SFTP_EVENTS,
            Capability.SFTP_METADATA,
            Capability.SFTP_MUTATE,
            # Directory size and recursive copy/move/remove always run as
            # daemon operations (see SftpServiceRuntime.start_directory_size /
            # start_copy / start_remove), so a daemon usable as an SFTP
            # backend must also support inspecting and cancelling them.
            Capability.OPERATIONS_READ,
            Capability.OPERATIONS_CONTROL,
        }
    )


def daemon_sftp_capabilities_missing(client) -> frozenset:
    required = required_daemon_sftp_capabilities()
    supported = client.get_capabilities().supported
    return required - supported


class DaemonSftpServiceController:
    """Daemon-backed SFTP controller used by the GTK file manager."""

    def __init__(
        self,
        client,
        bridge,
        connection_id: ConnectionId,
        *,
        on_ready: Optional[Callable[[SftpServiceSummary], None]] = None,
        on_state_changed: Optional[Callable[[SftpServiceSummary], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        missing = daemon_sftp_capabilities_missing(client)
        if missing:
            raise RuntimeError(
                f"Required daemon SFTP capabilities unavailable: {missing}"
            )
        self._client = client
        self._bridge = bridge
        self._connection_id = connection_id
        self._on_ready = on_ready
        self._on_state_changed = on_state_changed
        self._on_error = on_error
        self._lock = threading.Lock()
        self._generation = 0
        self._state = SftpControllerState.IDLE
        self._service_id: Optional[SftpServiceId] = None
        self._event_subscription = None
        self._operation_watchers: Dict[OperationId, tuple] = {}
        self._operation_poll_id = None
        self._closed = False

    @property
    def state(self) -> SftpControllerState:
        with self._lock:
            return self._state

    @property
    def service_id(self) -> Optional[SftpServiceId]:
        with self._lock:
            return self._service_id

    def open(self, connection_id: Optional[ConnectionId] = None) -> None:
        if connection_id is not None:
            self._connection_id = connection_id
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._state = SftpControllerState.OPENING
            self._service_id = None
        logger.debug(
            "DaemonSftpServiceController.open connection_id=%s generation=%s",
            self._connection_id,
            generation,
        )
        self._ensure_events()

        def _op():
            reuse = self._controlmaster_reuse_enabled()
            existing = self._ready_service_for_connection() if reuse else None
            if existing is not None:
                logger.debug(
                    "Reusing READY SFTP service %s for connection %s",
                    existing.id,
                    self._connection_id,
                )
                return self._client.attach_sftp(
                    AttachSftpRequest(service_id=existing.id)
                )
            logger.debug(
                "Opening new SFTP service for connection %s",
                self._connection_id,
            )
            return self._client.open_sftp(
                OpenSftpRequest(connection_id=self._connection_id)
            )

        self._submit(
            _op,
            on_success=lambda summary: self._on_open_accepted(summary, generation),
            on_error=lambda error: self._fail(error, generation),
        )

    def _controlmaster_reuse_enabled(self) -> bool:
        """Whether Preferences ▸ SSH multiplexing should share a live SFTP service."""
        try:
            from .config import Config

            return bool(Config().get_setting("ssh.controlmaster", False))
        except Exception:
            return False

    def _ready_service_for_connection(self) -> Optional[SftpServiceSummary]:
        """Return a READY daemon SFTP service for this connection, if any."""
        try:
            services = self._client.list_sftp_services()
        except Exception:
            logger.debug("list_sftp_services failed during reuse lookup", exc_info=True)
            return None
        for summary in services:
            try:
                if (
                    summary.connection_id == self._connection_id
                    and summary.state is SftpServiceState.READY
                ):
                    return summary
            except Exception:
                continue
        return None

    def attach(self, service_id: SftpServiceId) -> None:
        with self._lock:
            self._generation += 1
            generation = self._generation
            self._state = SftpControllerState.OPENING
            self._service_id = service_id
        self._ensure_events()

        def _op():
            return self._client.attach_sftp(AttachSftpRequest(service_id=service_id))

        self._submit(
            _op,
            on_success=lambda summary: self._on_open_accepted(summary, generation),
            on_error=lambda error: self._fail(error, generation),
        )

    def detach(self) -> None:
        service_id = self.service_id
        generation = self._bump()
        # Mark non-ready immediately so in-flight UI callbacks (e.g. the
        # background directory-count pass) cannot start new RPCs against a
        # service we are already leaving — mirrors terminal detach.
        self._mark(SftpControllerState.DETACHED, generation)
        self._stop_operation_poller()
        if service_id is None:
            return

        def _op():
            self._client.detach_sftp(service_id)
            return None

        self._submit(
            _op,
            on_success=lambda _r: None,
            on_error=lambda error: logger.debug("SFTP detach failed: %s", error),
        )

    def close(self) -> None:
        service_id = self.service_id
        generation = self._bump()
        self._unsubscribe_events()
        # Mark closed immediately so callback-driven follow-up work stops.
        self._mark(SftpControllerState.CLOSED, generation)
        self._stop_operation_poller()
        if service_id is None:
            return

        def _op():
            self._client.close_sftp(CloseSftpRequest(service_id=service_id))
            return None

        self._submit(
            _op,
            on_success=lambda _r: None,
            on_error=lambda error: self._fail(error, generation),
        )

    def list_directory(
        self,
        path: str,
        *,
        on_success: Callable[[ListDirectoryResult], None],
        on_error: Callable[[BaseException], None],
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_list_directory(
                ListDirectoryRequest(
                    connection_id=self._connection_id,
                    path=path,
                    service_id=service_id,
                    cursor=cursor,
                    limit=limit,
                )
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def realpath(
        self,
        path: str,
        *,
        on_success: Callable[[str], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_realpath(
                SftpPathRequest(service_id=service_id, path=path)
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def stat(
        self,
        path: str,
        *,
        follow_symlinks: bool = True,
        on_success: Callable[[RemoteFileEntry], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return
        method = self._client.sftp_stat if follow_symlinks else self._client.sftp_lstat

        def _op():
            return method(SftpPathRequest(service_id=service_id, path=path))

        self._submit(_op, on_success=on_success, on_error=on_error)

    def directory_size(
        self,
        path: str,
        *,
        on_success: Callable[[SftpDirectorySizeResult], None],
        on_error: Callable[[BaseException], None],
        on_operation_started: Optional[Callable[[OperationId], None]] = None,
        on_progress: Optional[Callable[[OperationSummary], None]] = None,
    ) -> None:
        """Measure a remote directory tree through a daemon operation.

        The walk runs on the daemon operation worker; the result is decoded
        from the succeeded operation summary's typed payload. ``on_operation_started``
        is invoked as soon as the operation id is known (so a caller can wire
        cancellation), and ``on_progress`` receives each non-terminal poll.
        """
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_directory_size(
                SftpDirectorySizeRequest(service_id=service_id, path=path)
            )

        def _on_started(summary) -> None:
            if on_operation_started is not None:
                on_operation_started(summary.operation_id)
            self._watch_operation(
                summary.operation_id,
                on_terminal=lambda done: self._resolve_size_result(
                    done, on_success=on_success, on_error=on_error
                ),
                on_error=on_error,
                on_progress=on_progress,
            )

        self._submit(_op, on_success=_on_started, on_error=on_error)

    def _resolve_size_result(self, summary, *, on_success, on_error) -> None:
        if summary.state is OperationState.SUCCEEDED:
            if summary.result is None:
                on_error(
                    SshPilotError(
                        ErrorCode.SFTP_PROTOCOL_ERROR,
                        "The daemon returned no directory size result",
                    )
                )
                return
            try:
                on_success(sftp_directory_size_result_from_wire(summary.result))
            except (TypeError, ValueError):
                on_error(
                    SshPilotError(
                        ErrorCode.SFTP_PROTOCOL_ERROR,
                        "The daemon returned an invalid directory size result",
                    )
                )
            return
        on_error(self._operation_failure(summary))

    def mkdir(
        self,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._path_mutation("sftp_mkdir", path, on_success=on_success, on_error=on_error)

    def create_file(
        self,
        path: str,
        *,
        on_success: Callable[[SftpCreateFileResult], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_create_file(
                SftpCreateFileRequest(service_id=service_id, path=path)
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def rmdir(
        self,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._path_mutation("sftp_rmdir", path, on_success=on_success, on_error=on_error)

    def copy(
        self,
        source_path: str,
        destination_path: str,
        *,
        recursive: bool = False,
        move: bool = False,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
        on_operation_started: Optional[Callable[[OperationId], None]] = None,
        on_progress: Optional[Callable[[OperationSummary], None]] = None,
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_copy(
                SftpCopyRequest(
                    service_id=service_id,
                    source_path=source_path,
                    destination_path=destination_path,
                    recursive=recursive,
                    move=move,
                )
            )

        if recursive:
            def _on_started(summary) -> None:
                if on_operation_started is not None:
                    on_operation_started(summary.operation_id)
                self._watch_operation(
                    summary.operation_id,
                    on_terminal=lambda done: self._resolve_tree_operation(
                        done, on_success=on_success, on_error=on_error
                    ),
                    on_error=on_error,
                    on_progress=on_progress,
                )

            self._submit(_op, on_success=_on_started, on_error=on_error)
        else:
            self._submit(_op, on_success=on_success, on_error=on_error)

    def _resolve_tree_operation(self, summary, *, on_success, on_error) -> None:
        if summary.state is OperationState.SUCCEEDED:
            on_success(None)
            return
        on_error(self._operation_failure(summary))

    def remove(
        self,
        path: str,
        *,
        recursive: bool = False,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
        on_operation_started: Optional[Callable[[OperationId], None]] = None,
        on_progress: Optional[Callable[[OperationSummary], None]] = None,
    ) -> None:
        if recursive:
            self._recursive_remove(
                path,
                on_success=on_success,
                on_error=on_error,
                on_operation_started=on_operation_started,
                on_progress=on_progress,
            )
        else:
            self._path_mutation(
                "sftp_remove",
                path,
                on_success=on_success,
                on_error=on_error,
            )

    def _recursive_remove(
        self,
        path,
        *,
        on_success,
        on_error,
        on_operation_started: Optional[Callable[[OperationId], None]] = None,
        on_progress: Optional[Callable[[OperationSummary], None]] = None,
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_remove(
                SftpPathRequest(service_id=service_id, path=path, recursive=True)
            )

        def _on_started(summary) -> None:
            if on_operation_started is not None:
                on_operation_started(summary.operation_id)
            self._watch_operation(
                summary.operation_id,
                on_terminal=lambda done: self._resolve_tree_operation(
                    done, on_success=on_success, on_error=on_error
                ),
                on_error=on_error,
                on_progress=on_progress,
            )

        self._submit(_op, on_success=_on_started, on_error=on_error)

    def rename(
        self,
        source_path: str,
        destination_path: str,
        *,
        overwrite: bool = False,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_rename(
                SftpRenameRequest(
                    service_id=service_id,
                    source_path=source_path,
                    destination_path=destination_path,
                    overwrite=overwrite,
                )
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def readlink(
        self,
        path: str,
        *,
        on_success: Callable[[str], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_readlink(
                SftpPathRequest(service_id=service_id, path=path)
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def read_file(
        self,
        path: str,
        *,
        on_success: Callable[[SftpReadFileResult], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_read_file(
                SftpReadFileRequest(
                    target=SftpFileTarget.REMOTE,
                    path=path,
                    service_id=service_id,
                )
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def replace_file(
        self,
        path: str,
        content: str,
        expected_revision: str,
        *,
        backup: bool = True,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_replace_file(
                SftpReplaceFileRequest(
                    target=SftpFileTarget.REMOTE,
                    path=path,
                    content=content,
                    expected_revision=expected_revision,
                    backup=backup,
                    service_id=service_id,
                )
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def chmod(
        self,
        path: str,
        mode: int,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_chmod(
                SftpChmodRequest(service_id=service_id, path=path, mode=mode)
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def symlink(
        self,
        target_path: str,
        link_path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return

        def _op():
            return self._client.sftp_symlink(
                SftpSymlinkRequest(
                    service_id=service_id,
                    target_path=target_path,
                    link_path=link_path,
                )
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def _path_mutation(
        self,
        method_name: str,
        path: str,
        *,
        recursive: bool = False,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return
        method = getattr(self._client, method_name)

        def _op():
            return method(
                SftpPathRequest(service_id=service_id, path=path, recursive=recursive)
            )

        self._submit(_op, on_success=on_success, on_error=on_error)

    def _ensure_events(self) -> None:
        if self._event_subscription is not None:
            return
        subscribe = getattr(self._client, "subscribe_events", None)
        if not callable(subscribe):
            return

        def _on_event(event) -> None:
            if self._closed or event.type not in _SFTP_EVENT_TYPES:
                return
            summary = event.payload
            if not isinstance(summary, SftpServiceSummary):
                return
            with self._lock:
                # While unbound, only accept events for our connection so a
                # sibling host's CREATED/STARTING cannot steal this open.
                if (
                    self._service_id is None
                    and summary.connection_id != self._connection_id
                ):
                    return
                if self._service_id is not None and summary.id != self._service_id:
                    return
                generation = self._generation
            # Daemon events are delivered on the client's reader thread, but
            # the lifecycle callbacks build and touch GTK widgets (the SCP
            # browser window, the file manager). Round-trip through the bridge
            # so _on_open_accepted runs on the GTK main context — the same
            # marshalling TransferServiceController uses. Off-thread GTK with
            # the GL renderer surfaces as gdk_gl_context_make_current()
            # failures and broken rendering.
            self._submit(
                lambda: (summary, generation),
                on_success=self._dispatch_event,
                on_error=lambda _error: None,
            )

        try:
            self._event_subscription = subscribe(_on_event)
        except Exception as exc:  # pragma: no cover
            logger.debug("SFTP event subscription failed: %s", exc)

    def _unsubscribe_events(self) -> None:
        subscription = self._event_subscription
        self._event_subscription = None
        if subscription is None:
            return
        unsubscribe = getattr(subscription, "unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                logger.debug("SFTP event unsubscription failed", exc_info=True)

    def _dispatch_event(self, item) -> None:
        """Apply one marshalled SFTP event on the GTK main context."""
        if self._closed:
            return
        summary, generation = item
        self._on_open_accepted(summary, generation)

    def _on_open_accepted(self, summary: SftpServiceSummary, generation: int) -> None:
        became_ready = False
        with self._lock:
            if generation != self._generation:
                logger.debug(
                    "Ignoring stale SFTP open result id=%s state=%s "
                    "(generation %s != %s)",
                    summary.id,
                    summary.state,
                    generation,
                    self._generation,
                )
                return
            if self._service_id is None:
                self._service_id = summary.id
            elif summary.id != self._service_id:
                return

            previous = self._state
            if summary.state is SftpServiceState.READY:
                became_ready = previous is not SftpControllerState.READY
                self._state = SftpControllerState.READY
            elif summary.state is SftpServiceState.FAILED:
                self._state = SftpControllerState.FAILED
            elif summary.state is SftpServiceState.CLOSED:
                self._state = SftpControllerState.CLOSED
            elif summary.state is SftpServiceState.CLOSING:
                # Shutdown in progress: always leave READY so UI/ops stop
                # treating the service as usable while waiting for CLOSED.
                # Do not overwrite terminal controller states.
                if previous not in (
                    SftpControllerState.CLOSED,
                    SftpControllerState.FAILED,
                    SftpControllerState.DETACHED,
                ):
                    self._state = SftpControllerState.CLOSING
                else:
                    logger.debug(
                        "Ignoring CLOSING for %s (controller already %s)",
                        summary.id,
                        previous.value,
                    )
            elif summary.state in (
                SftpServiceState.CREATED,
                SftpServiceState.STARTING,
            ):
                # Startup transitional states. Never regress READY (or a
                # terminal controller state) back to OPENING — late CREATED/
                # STARTING events commonly arrive after the open RPC already
                # reported READY, which used to break the first listdir.
                if previous in (
                    SftpControllerState.IDLE,
                    SftpControllerState.OPENING,
                ):
                    self._state = SftpControllerState.OPENING
                else:
                    logger.debug(
                        "Ignoring transitional SFTP state %s for %s "
                        "(controller already %s)",
                        summary.state,
                        summary.id,
                        previous.value,
                    )
            else:
                logger.debug(
                    "Ignoring unknown SFTP service state %s for %s",
                    summary.state,
                    summary.id,
                )
        logger.debug(
            "SFTP service %s state=%s for connection %s (controller=%s)",
            summary.id,
            summary.state,
            self._connection_id,
            self._state.value,
        )
        if self._on_state_changed is not None:
            self._on_state_changed(summary)
        if became_ready and self._on_ready is not None:
            self._on_ready(summary)
        if (
            summary.state in (SftpServiceState.FAILED, SftpServiceState.CLOSED)
            and self._on_error is not None
        ):
            # CLOSED is included alongside FAILED because a self-initiated
            # close/detach never reaches here: close() unsubscribes from
            # events before sending the close RPC, and both close()/detach()
            # set local state via _mark(), not this event-driven path. A
            # CLOSED event that does arrive here means the service went away
            # for a reason this tab did not initiate (e.g. another attached
            # client closed a shared/reused service), so it must be
            # surfaced the same way a FAILED event is.
            if summary.state is SftpServiceState.FAILED:
                message = summary.failure.message if summary.failure else "SFTP failed"
            else:
                message = "The SFTP service was closed"
            logger.warning(
                "SFTP service %s %s for connection %s: %s",
                summary.id,
                summary.state.value,
                self._connection_id,
                message,
            )
            self._on_error(
                SshPilotError(ErrorCode.SFTP_SERVICE_NOT_READY, message)
            )

    def _fail(self, error: BaseException, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                logger.debug(
                    "Ignoring stale SFTP open failure (generation %s != %s): %s",
                    generation,
                    self._generation,
                    error,
                )
                return
            self._state = SftpControllerState.FAILED
        logger.warning(
            "SFTP controller FAILED for connection %s: %s",
            self._connection_id,
            error,
        )
        logger.debug(
            "SFTP controller failure detail for connection %s",
            self._connection_id,
            exc_info=error,
        )
        if self._on_error is not None:
            self._on_error(error)

    def _mark(self, state: SftpControllerState, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._state = state

    def _bump(self) -> int:
        with self._lock:
            self._generation += 1
            return self._generation

    def _require_ready_service_id(self) -> SftpServiceId:
        with self._lock:
            if self._state is not SftpControllerState.READY or self._service_id is None:
                raise SshPilotError(
                    ErrorCode.SFTP_SERVICE_NOT_READY,
                    "The SFTP service is not ready",
                )
            return self._service_id

    def _ready_service_id_or_error(
        self,
        on_error: Callable[[BaseException], None],
    ) -> Optional[SftpServiceId]:
        """Return the ready service id, or deliver not-ready via ``on_error``.

        Callback-based APIs must not raise synchronously — callers chain work
        from bridge success callbacks (e.g. directory-count passes), and a
        raise there surfaces as an unhandled exception during shutdown.
        """
        try:
            return self._require_ready_service_id()
        except SshPilotError as exc:
            on_error(exc)
            return None

    def _submit(
        self,
        factory: Callable[[], object],
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        try:
            self._bridge.submit(factory, on_success=on_success, on_error=on_error)
        except RuntimeError as exc:
            on_error(exc)

    def _watch_operation(
        self,
        operation_id: OperationId,
        *,
        on_terminal: Callable[[OperationSummary], None],
        on_error: Callable[[BaseException], None],
        on_progress: Optional[Callable[[OperationSummary], None]] = None,
    ) -> None:
        """Track a daemon operation and resolve its callbacks once terminal.

        Polls ``get_operation`` on a short GLib timeout (the same pattern the
        SSH key-copy window uses) rather than blocking the single bridge
        worker; each poll is a quick independent RPC. ``on_progress``, when
        given, is called with every non-terminal summary so callers can
        surface the daemon-reported ``progress``/``message``.
        """
        self._operation_watchers[operation_id] = (on_terminal, on_error, on_progress)
        self._ensure_operation_poller()

    def cancel_operation(
        self,
        operation_id: OperationId,
        *,
        on_success: Optional[Callable[[OperationSummary], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """Request daemon-side cancellation of a running operation.

        Safe to call even if the operation has already reached a terminal
        state or the operation id is otherwise stale; the daemon treats
        cancelling a finished/unknown operation as a no-op rather than an
        error the caller must handle specially.
        """
        self._submit(
            lambda: self._client.cancel_operation(operation_id),
            on_success=on_success or (lambda _summary: None),
            on_error=on_error or (lambda _exc: None),
        )

    def _ensure_operation_poller(self) -> None:
        if self._operation_poll_id is None:
            self._operation_poll_id = GLib.timeout_add(
                200, self._poll_operations
            )

    def _poll_operations(self) -> bool:
        if self._closed:
            self._stop_operation_poller()
            return False
        if not self._operation_watchers:
            self._operation_poll_id = None
            return False
        for operation_id in list(self._operation_watchers):
            self._submit(
                lambda op_id=operation_id: self._client.get_operation(op_id),
                on_success=lambda summary, op_id=operation_id: (
                    self._on_operation_polled(op_id, summary)
                ),
                on_error=lambda error, op_id=operation_id: (
                    self._fail_operation_watch(op_id, error)
                ),
            )
        return True

    def _on_operation_polled(self, operation_id, summary) -> None:
        callbacks = self._operation_watchers.get(operation_id)
        if callbacks is None:
            return
        on_terminal, _on_error, on_progress = callbacks
        if not is_terminal_operation_state(summary.state):
            if on_progress is not None:
                try:
                    on_progress(summary)
                except Exception:
                    logger.debug(
                        "SFTP operation progress callback failed", exc_info=True
                    )
            return
        self._operation_watchers.pop(operation_id, None)
        on_terminal(summary)

    def _fail_operation_watch(self, operation_id, error) -> None:
        callbacks = self._operation_watchers.pop(operation_id, None)
        if callbacks is None:
            return
        _on_terminal, on_error, _on_progress = callbacks
        on_error(error)

    def _stop_operation_poller(self) -> None:
        poll_id = self._operation_poll_id
        self._operation_poll_id = None
        if poll_id is not None:
            try:
                GLib.source_remove(poll_id)
            except Exception:
                pass
        pending = self._operation_watchers
        self._operation_watchers = {}
        for _operation_id, (_on_terminal, on_error, _on_progress) in pending.items():
            try:
                on_error(
                    SshPilotError(
                        ErrorCode.SFTP_SERVICE_NOT_READY,
                        "The SFTP service closed before the operation finished",
                    )
                )
            except Exception:
                logger.debug("SFTP operation watcher teardown error", exc_info=True)

    @staticmethod
    def _operation_failure(summary: OperationSummary) -> BaseException:
        if summary.state is OperationState.CANCELLED:
            return SshPilotError(
                ErrorCode.OPERATION_CANCELLED,
                summary.message or "The operation was cancelled",
            )
        message = (
            summary.failure.message if summary.failure else summary.message
        ) or "The operation failed"
        code = summary.failure.code if summary.failure else ErrorCode.SFTP_COMMAND_FAILED.value
        try:
            return SshPilotError(ErrorCode(code), message)
        except (TypeError, ValueError):
            return SshPilotError(ErrorCode.SFTP_COMMAND_FAILED, message)
