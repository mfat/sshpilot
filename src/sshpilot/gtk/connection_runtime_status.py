"""GTK-side connection status derived from daemon runtime lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable, Dict, Optional

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.operations import SftpServiceState, SftpServiceSummary
from sshpilot.api.models.sessions import SessionExitInfo, SessionState, SessionSummary
from sshpilot.connection_model import ConnectionState

from .sftp_failure_messages import format_sftp_failure


_SESSION_EVENTS = frozenset(
    {
        EventType.SESSION_CREATED,
        EventType.SESSION_STATE_CHANGED,
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
    }
)

# A file manager is a connection to the host just as much as a terminal is, and
# the daemon runs it as an SFTP service rather than a session. Watching only the
# session family left a host with an open file manager showing no indicator at
# all (GH #1193).
_SFTP_EVENTS = frozenset(
    {
        EventType.SFTP_CREATED,
        EventType.SFTP_STATE_CHANGED,
        EventType.SFTP_CLOSED,
        EventType.SFTP_FAILED,
    }
)

_TRACKED_EVENTS = _SESSION_EVENTS | _SFTP_EVENTS


@dataclass(frozen=True)
class ConnectionRuntimeStatus:
    """Aggregated runtime state for one connection without mutating its DTO."""

    state: ConnectionState = ConnectionState.UNKNOWN
    reason: str = ""


UNKNOWN_RUNTIME_STATUS = ConnectionRuntimeStatus()


class ConnectionRuntimeStatusStore:
    """Project daemon sessions and SFTP services into one status per connection."""

    def __init__(
        self,
        *,
        dispatch: Optional[Callable[[Callable[[], None]], object]] = None,
        on_changed: Optional[
            Callable[[str, ConnectionRuntimeStatus], None]
        ] = None,
    ) -> None:
        self._lock = RLock()
        self._sessions: Dict[str, SessionSummary] = {}
        self._sftp: Dict[str, SftpServiceSummary] = {}
        self._statuses: Dict[str, ConnectionRuntimeStatus] = {}
        self._client = None
        self._subscription = None
        self._generation = 0
        self._last_sequence = -1
        self._refreshing = False
        self._pending_events = []
        self._dispatch = dispatch or (lambda callback: callback())
        self._on_changed = on_changed

    def status_for(self, connection_or_id) -> ConnectionRuntimeStatus:
        connection_id = self._connection_id(connection_or_id)
        with self._lock:
            return self._statuses.get(connection_id, UNKNOWN_RUNTIME_STATUS)

    def attach_client(self, client) -> None:
        """Subscribe first, then snapshot runtime state without an event-loss gap."""
        with self._lock:
            old_subscription = self._subscription
            self._subscription = None
            self._generation += 1
            generation = self._generation
            self._client = client
            self._last_sequence = -1
            self._refreshing = True
            self._pending_events = []
        if old_subscription is not None:
            old_subscription.unsubscribe()

        instance_id = getattr(client, "server_instance_id", None)
        subscription = client.subscribe_events(
            lambda event: self._accept_event(event, generation, instance_id)
        )
        try:
            replacement = self._validated_sessions(client.list_sessions())
            sftp_replacement = self._validated_sftp_services(
                self._list_sftp_services(client)
            )
        except BaseException:
            subscription.unsubscribe()
            with self._lock:
                if generation == self._generation:
                    self._refreshing = False
                    self._pending_events = []
            raise

        with self._lock:
            if generation != self._generation or client is not self._client:
                subscription.unsubscribe()
                return
            previous = dict(self._statuses)
            self._sessions = replacement
            self._sftp = sftp_replacement
            for event in sorted(self._pending_events, key=lambda item: item.sequence):
                self._apply_event_locked(event)
            self._pending_events = []
            self._refreshing = False
            self._statuses = self._aggregate_locked()
            current = dict(self._statuses)
            self._subscription = subscription
        self._notify_changes(previous, current, generation)

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            generation = self._generation
            subscription, self._subscription = self._subscription, None
            self._client = None
            previous = dict(self._statuses)
            self._sessions = {}
            self._sftp = {}
            self._statuses = {}
            self._pending_events = []
            self._refreshing = False
        if subscription is not None:
            subscription.unsubscribe()
        self._notify_changes(previous, {}, generation)

    def _accept_event(
        self,
        event: CoreEvent,
        generation: int,
        instance_id: Optional[str],
    ) -> None:
        if event.type not in _TRACKED_EVENTS:
            return
        with self._lock:
            current_instance = getattr(self._client, "server_instance_id", None)
            if generation != self._generation or current_instance != instance_id:
                return
            if self._refreshing:
                self._pending_events.append(event)
                return
            previous = dict(self._statuses)
            if not self._apply_event_locked(event):
                return
            self._statuses = self._aggregate_locked()
            current = dict(self._statuses)
        self._notify_changes(previous, current, generation)

    def _apply_event_locked(self, event: CoreEvent) -> bool:
        if event.sequence <= self._last_sequence:
            return False
        self._last_sequence = event.sequence

        if event.type is EventType.SESSION_EXITED:
            if not isinstance(event.payload, SessionExitInfo) or event.session_id is None:
                return False
            existing = self._sessions.get(str(event.session_id))
            if existing is None:
                return False
            self._sessions[str(event.session_id)] = replace(
                existing,
                state=SessionState.EXITED,
                exit_info=event.payload,
            )
            return True

        if event.type in _SFTP_EVENTS:
            if not isinstance(event.payload, SftpServiceSummary):
                return False
            self._sftp[str(event.payload.id)] = event.payload
            return True

        if not isinstance(event.payload, SessionSummary):
            return False
        self._sessions[str(event.payload.id)] = event.payload
        return True

    def _aggregate_locked(self) -> Dict[str, ConnectionRuntimeStatus]:
        grouped: Dict[str, list] = {}
        for record in (*self._sessions.values(), *self._sftp.values()):
            grouped.setdefault(str(record.connection_id), []).append(record)

        statuses = {}
        for connection_id, records in grouped.items():
            status = self._aggregate_records(records)
            if status.state is not ConnectionState.UNKNOWN:
                statuses[connection_id] = status
        return statuses

    @staticmethod
    def _is_live(record) -> bool:
        """The host is reachable right now: a running shell or a ready SFTP service."""
        if isinstance(record, SftpServiceSummary):
            return record.state is SftpServiceState.READY
        return record.state is SessionState.RUNNING

    @staticmethod
    def _is_starting(record) -> bool:
        if isinstance(record, SftpServiceSummary):
            return record.state in {
                SftpServiceState.CREATED,
                SftpServiceState.STARTING,
            }
        return record.state in {SessionState.CREATED, SessionState.STARTING}

    @staticmethod
    def _is_failed(record) -> bool:
        # Compared per record type on purpose: SessionState and SftpServiceState
        # are str enums, so their like-named members compare equal across the
        # two and a shared membership test would only work by accident.
        if isinstance(record, SftpServiceSummary):
            return record.state is SftpServiceState.FAILED
        return record.state is SessionState.FAILED

    @staticmethod
    def _is_terminal_outcome(record) -> bool:
        """Whether this record still carries a reportable "went down" result.

        CLOSED is a retention/lifecycle state, not a loss of the outcome. Daemon
        summaries retain failure/exit metadata after cleanup, so keep projecting
        that final result instead of flashing FAILED and immediately reverting
        the sidebar to UNKNOWN. A clean close with nothing recorded is an
        intentional close and reports nothing at all.
        """
        if isinstance(record, SftpServiceSummary):
            return record.state in {
                SftpServiceState.FAILED,
                SftpServiceState.CLOSING,
            } or (
                record.state is SftpServiceState.CLOSED
                and record.failure is not None
            )
        return record.state in {
            SessionState.FAILED,
            SessionState.CLOSING,
            SessionState.EXITED,
        } or (
            record.state is SessionState.CLOSED
            and (record.failure is not None or record.exit_info is not None)
        )

    @classmethod
    def _aggregate_records(cls, records) -> ConnectionRuntimeStatus:
        """Fold a connection's sessions and SFTP services into one status.

        Both are connections to the host, so either kind being live makes the
        connection connected; only when nothing is live does the most recent
        "went down" result get reported.
        """
        if any(cls._is_live(record) for record in records):
            return ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected")
        if any(cls._is_starting(record) for record in records):
            return ConnectionRuntimeStatus(ConnectionState.CONNECTING, "Connecting")

        terminal_outcomes = [
            record for record in records if cls._is_terminal_outcome(record)
        ]
        if terminal_outcomes:
            latest = max(terminal_outcomes, key=lambda record: record.created_at)
            if latest.failure is not None or cls._is_failed(latest):
                if latest.failure is None:
                    reason = ""
                elif isinstance(latest, SftpServiceSummary):
                    reason = format_sftp_failure(latest.failure)
                else:
                    reason = latest.failure.message
                return ConnectionRuntimeStatus(ConnectionState.FAILED, reason)
            exit_info = getattr(latest, "exit_info", None)
            reason = exit_info.reason if exit_info is not None else ""
            return ConnectionRuntimeStatus(ConnectionState.DISCONNECTED, reason)
        return UNKNOWN_RUNTIME_STATUS

    def _notify_changes(self, previous, current, generation: int) -> None:
        if self._on_changed is None:
            return
        for connection_id in previous.keys() | current.keys():
            old_status = previous.get(connection_id, UNKNOWN_RUNTIME_STATUS)
            new_status = current.get(connection_id, UNKNOWN_RUNTIME_STATUS)
            if old_status == new_status:
                continue

            def wrapped(cid=connection_id, status=new_status):
                with self._lock:
                    if generation != self._generation:
                        return
                self._on_changed(cid, status)

            self._dispatch(wrapped)

    @staticmethod
    def _validated_sessions(sessions) -> Dict[str, SessionSummary]:
        replacement = {}
        for session in sessions:
            if not isinstance(session, SessionSummary):
                raise TypeError("runtime status store only accepts SessionSummary DTOs")
            replacement[str(session.id)] = session
        return replacement

    @staticmethod
    def _list_sftp_services(client):
        """Snapshot SFTP services, tolerating a client that cannot list them.

        The session snapshot is required — without it the store would report a
        live host as idle. A daemon too old to list SFTP services is a lesser
        problem: file-manager-only connections stay unreported, exactly as they
        were before, while everything else keeps working.
        """
        lister = getattr(client, "list_sftp_services", None)
        if not callable(lister):
            return ()
        return lister()

    @staticmethod
    def _validated_sftp_services(services) -> Dict[str, SftpServiceSummary]:
        replacement = {}
        for service in services:
            if not isinstance(service, SftpServiceSummary):
                raise TypeError(
                    "runtime status store only accepts SftpServiceSummary DTOs"
                )
            replacement[str(service.id)] = service
        return replacement

    @staticmethod
    def _connection_id(connection_or_id) -> str:
        value = getattr(connection_or_id, "id", connection_or_id)
        return str(value or "")
