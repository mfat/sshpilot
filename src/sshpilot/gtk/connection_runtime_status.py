"""GTK-side connection status derived from daemon session lifecycle events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import RLock
from typing import Callable, Dict, Optional

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.sessions import SessionExitInfo, SessionState, SessionSummary
from sshpilot.connection_model import ConnectionState


_SESSION_EVENTS = frozenset(
    {
        EventType.SESSION_CREATED,
        EventType.SESSION_STATE_CHANGED,
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
    }
)


@dataclass(frozen=True)
class ConnectionRuntimeStatus:
    """Aggregated runtime state for one connection without mutating its DTO."""

    state: ConnectionState = ConnectionState.UNKNOWN
    reason: str = ""


UNKNOWN_RUNTIME_STATUS = ConnectionRuntimeStatus()


class ConnectionRuntimeStatusStore:
    """Project daemon sessions into one runtime status per connection ID."""

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
        """Subscribe first, then snapshot sessions without an event-loss gap."""
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
            sessions = client.list_sessions()
            replacement = self._validated_sessions(sessions)
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
        if event.type not in _SESSION_EVENTS:
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

        if not isinstance(event.payload, SessionSummary):
            return False
        self._sessions[str(event.payload.id)] = event.payload
        return True

    def _aggregate_locked(self) -> Dict[str, ConnectionRuntimeStatus]:
        grouped: Dict[str, list[SessionSummary]] = {}
        for session in self._sessions.values():
            grouped.setdefault(str(session.connection_id), []).append(session)

        statuses = {}
        for connection_id, sessions in grouped.items():
            status = self._aggregate_sessions(sessions)
            if status.state is not ConnectionState.UNKNOWN:
                statuses[connection_id] = status
        return statuses

    @staticmethod
    def _aggregate_sessions(sessions) -> ConnectionRuntimeStatus:
        if any(session.state is SessionState.RUNNING for session in sessions):
            return ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected")
        if any(
            session.state in {SessionState.CREATED, SessionState.STARTING}
            for session in sessions
        ):
            return ConnectionRuntimeStatus(ConnectionState.CONNECTING, "Connecting")

        # CLOSED is a retention/lifecycle state, not a loss of the terminal
        # outcome. Daemon summaries retain failure/exit metadata after cleanup,
        # so keep projecting that final result instead of flashing FAILED and
        # immediately reverting the sidebar to UNKNOWN.
        terminal_outcomes = [
            session for session in sessions
            if session.state in {
                SessionState.FAILED,
                SessionState.CLOSING,
                SessionState.EXITED,
            }
            or (
                session.state is SessionState.CLOSED
                and (session.failure is not None or session.exit_info is not None)
            )
        ]
        if terminal_outcomes:
            latest = max(terminal_outcomes, key=lambda session: session.created_at)
            if latest.state is SessionState.FAILED or latest.failure is not None:
                reason = latest.failure.message if latest.failure is not None else ""
                return ConnectionRuntimeStatus(ConnectionState.FAILED, reason)
            reason = latest.exit_info.reason if latest.exit_info is not None else ""
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
    def _connection_id(connection_or_id) -> str:
        value = getattr(connection_or_id, "id", connection_or_id)
        return str(value or "")
