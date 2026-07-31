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
from typing import Callable, Optional

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
    RemoteFileEntry,
    SftpPathRequest,
    SftpRenameRequest,
    SftpServiceState,
    SftpServiceSummary,
)

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
        self._ensure_events()

        def _op():
            return self._client.open_sftp(
                OpenSftpRequest(connection_id=self._connection_id)
            )

        self._submit(
            _op,
            on_success=lambda summary: self._on_open_accepted(summary, generation),
            on_error=lambda error: self._fail(error, generation),
        )

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

    def mkdir(
        self,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._path_mutation("sftp_mkdir", path, on_success=on_success, on_error=on_error)

    def rmdir(
        self,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._path_mutation("sftp_rmdir", path, on_success=on_success, on_error=on_error)

    def remove(
        self,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._path_mutation("sftp_remove", path, on_success=on_success, on_error=on_error)

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

    def _path_mutation(
        self,
        method_name: str,
        path: str,
        *,
        on_success: Callable[[object], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        service_id = self._ready_service_id_or_error(on_error)
        if service_id is None:
            return
        method = getattr(self._client, method_name)

        def _op():
            return method(SftpPathRequest(service_id=service_id, path=path))

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
                if summary.id != self._service_id and self._service_id is not None:
                    return
                generation = self._generation
            self._on_open_accepted(summary, generation)

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

    def _on_open_accepted(self, summary: SftpServiceSummary, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._service_id = summary.id
            if summary.state is SftpServiceState.READY:
                self._state = SftpControllerState.READY
            elif summary.state is SftpServiceState.FAILED:
                self._state = SftpControllerState.FAILED
            elif summary.state is SftpServiceState.CLOSED:
                self._state = SftpControllerState.CLOSED
            else:
                self._state = SftpControllerState.OPENING
        if self._on_state_changed is not None:
            self._on_state_changed(summary)
        if summary.state is SftpServiceState.READY and self._on_ready is not None:
            self._on_ready(summary)
        if summary.state is SftpServiceState.FAILED and self._on_error is not None:
            message = summary.failure.message if summary.failure else "SFTP failed"
            self._on_error(
                SshPilotError(ErrorCode.SFTP_SERVICE_NOT_READY, message)
            )

    def _fail(self, error: BaseException, generation: int) -> None:
        with self._lock:
            if generation != self._generation:
                return
            self._state = SftpControllerState.FAILED
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
