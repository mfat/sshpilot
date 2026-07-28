"""Frontend-neutral core event records and subscription infrastructure."""

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Generic, Optional, TypeVar

from .models.common import ConnectionId, RequestId, SessionId, utc_now

logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")
CoreEventCallback = Callable[["CoreEvent[Any]"], None]


class EventType(str, Enum):
    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    SESSION_CREATED = "session.created"
    SESSION_STATE_CHANGED = "session.state_changed"
    SESSION_OUTPUT = "session.output"
    SESSION_INTERACTION_REQUESTED = "session.interaction_requested"
    SESSION_EXITED = "session.exited"
    SESSION_CLOSED = "session.closed"
    ERROR_OCCURRED = "error.occurred"


@dataclass(frozen=True)
class CoreEvent(Generic[PayloadT]):
    type: EventType
    payload: PayloadT
    sequence: int
    timestamp: datetime = field(default_factory=utc_now)
    request_id: Optional[RequestId] = None
    connection_id: Optional[ConnectionId] = None
    session_id: Optional[SessionId] = None

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("event sequence must not be negative")


class Subscription:
    """Idempotent handle for removing an event subscriber."""

    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe = unsubscribe
        self._active = True
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def unsubscribe(self) -> None:
        with self._lock:
            if not self._active:
                return
            self._active = False
        self._unsubscribe()

    def close(self) -> None:
        self.unsubscribe()

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.unsubscribe()


class EventPublisher:
    """Small synchronous publisher used by the in-process adapter.

    Delivery occurs on the source thread. Subscribers are isolated from one
    another, and registration order defines delivery order.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscribers: Dict[int, CoreEventCallback] = {}
        self._next_subscriber_id = 1
        self._next_sequence = 0
        self._closed = False

    def subscribe(self, callback: CoreEventCallback) -> Subscription:
        if not callable(callback):
            raise TypeError("event callback must be callable")
        with self._lock:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = callback

        def _unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

        return Subscription(_unsubscribe)

    def publish(
        self,
        event_type: EventType,
        payload: Any,
        *,
        request_id: Optional[RequestId] = None,
        connection_id: Optional[ConnectionId] = None,
        session_id: Optional[SessionId] = None,
    ) -> CoreEvent[Any]:
        with self._lock:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            sequence = self._next_sequence
            self._next_sequence += 1
            callbacks = tuple(self._subscribers.values())
        event = CoreEvent(
            type=event_type,
            payload=payload,
            sequence=sequence,
            request_id=request_id,
            connection_id=connection_id,
            session_id=session_id,
        )
        for callback in callbacks:
            try:
                callback(event)
            except Exception:
                logger.exception("Core event subscriber failed for %s", event_type.value)
        return event

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._subscribers.clear()

