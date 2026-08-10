"""Frontend-neutral core event records and subscription infrastructure."""

import logging
import threading
from collections import deque
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any, Callable, Deque, Dict, Generic, Mapping, Optional, Tuple, TypeVar

from ._safe_values import copy_safe_details
from .models.common import ConnectionId, RequestId, SessionId, utc_now
from .models.connection_store import ConnectionStoreSnapshot
from .models.connections import ConnectionSummary
from .models.interactions import InteractionRequest, InteractionSummary
from .models.operations import ForwardSummary, OperationSummary, SftpServiceSummary
from .models.broadcast import BroadcastCommandOutput
from .models.sessions import SessionExitInfo, SessionSummary
from .models.terminal import TerminalOutput
from .models.daemon import DaemonStatus
from .models.transfers import TransferSummary

logger = logging.getLogger(__name__)

PayloadT = TypeVar("PayloadT")
CoreEventCallback = Callable[["CoreEvent[Any]"], None]


class EventType(str, Enum):
    CONNECTION_CREATED = "connection.created"
    CONNECTION_UPDATED = "connection.updated"
    CONNECTION_DELETED = "connection.deleted"
    CONNECTION_STORE_CHANGED = "connection_store.changed"
    SESSION_CREATED = "session.created"
    SESSION_STATE_CHANGED = "session.state_changed"
    SESSION_OUTPUT = "session.output"
    SESSION_INTERACTION_REQUESTED = "session.interaction_requested"
    SESSION_EXITED = "session.exited"
    SESSION_CLOSED = "session.closed"
    INTERACTION_CREATED = "interaction.created"
    INTERACTION_STATE_CHANGED = "interaction.state_changed"
    SFTP_CREATED = "sftp.created"
    SFTP_STATE_CHANGED = "sftp.state_changed"
    SFTP_CLOSED = "sftp.closed"
    SFTP_FAILED = "sftp.failed"
    TRANSFER_CREATED = "transfer.created"
    TRANSFER_STARTED = "transfer.started"
    TRANSFER_PROGRESS = "transfer.progress"
    TRANSFER_ITEM_COMPLETED = "transfer.item_completed"
    TRANSFER_COMPLETED = "transfer.completed"
    TRANSFER_CANCELLED = "transfer.cancelled"
    TRANSFER_FAILED = "transfer.failed"
    FORWARD_CREATED = "forward.created"
    FORWARD_STARTING = "forward.starting"
    FORWARD_ACTIVE = "forward.active"
    FORWARD_CLOSED = "forward.closed"
    FORWARD_FAILED = "forward.failed"
    OPERATION_CREATED = "operation.created"
    OPERATION_STATE_CHANGED = "operation.state_changed"
    BROADCAST_OUTPUT = "broadcast.output"
    DAEMON_STATE_CHANGED = "daemon.state_changed"
    ERROR_OCCURRED = "error.occurred"


@dataclass(frozen=True)
class CoreEvent(Generic[PayloadT]):
    type: EventType
    payload: PayloadT = field(repr=False)
    sequence: int
    timestamp: datetime = field(default_factory=utc_now)
    request_id: Optional[RequestId] = None
    connection_id: Optional[ConnectionId] = None
    session_id: Optional[SessionId] = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, EventType):
            raise TypeError("event type must be an EventType")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 0
        ):
            raise ValueError("event sequence must not be negative")
        if type(self.timestamp) is not datetime:
            raise TypeError("event timestamp must be a datetime")
        for identifier_name, identifier in (
            ("request_id", self.request_id),
            ("connection_id", self.connection_id),
            ("session_id", self.session_id),
        ):
            if identifier is not None and (
                type(identifier) is not str or not identifier.strip()
            ):
                raise TypeError(
                    f"event {identifier_name} must be a non-empty string or None"
                )
        safe_payload = validate_event_payload(self.type, self.payload)
        if safe_payload is not self.payload:
            object.__setattr__(self, "payload", safe_payload)


_EVENT_PAYLOAD_TYPES = {
    EventType.CONNECTION_CREATED: ConnectionSummary,
    EventType.CONNECTION_UPDATED: ConnectionSummary,
    EventType.CONNECTION_DELETED: ConnectionSummary,
    EventType.CONNECTION_STORE_CHANGED: ConnectionStoreSnapshot,
    EventType.SESSION_CREATED: SessionSummary,
    EventType.SESSION_STATE_CHANGED: SessionSummary,
    EventType.SESSION_OUTPUT: TerminalOutput,
    EventType.SESSION_INTERACTION_REQUESTED: InteractionRequest,
    EventType.SESSION_EXITED: SessionExitInfo,
    EventType.SESSION_CLOSED: SessionSummary,
    EventType.INTERACTION_CREATED: InteractionSummary,
    EventType.INTERACTION_STATE_CHANGED: InteractionSummary,
    EventType.SFTP_CREATED: SftpServiceSummary,
    EventType.SFTP_STATE_CHANGED: SftpServiceSummary,
    EventType.SFTP_CLOSED: SftpServiceSummary,
    EventType.SFTP_FAILED: SftpServiceSummary,
    EventType.TRANSFER_CREATED: TransferSummary,
    EventType.TRANSFER_STARTED: TransferSummary,
    EventType.TRANSFER_PROGRESS: TransferSummary,
    EventType.TRANSFER_ITEM_COMPLETED: TransferSummary,
    EventType.TRANSFER_COMPLETED: TransferSummary,
    EventType.TRANSFER_CANCELLED: TransferSummary,
    EventType.TRANSFER_FAILED: TransferSummary,
    EventType.FORWARD_CREATED: ForwardSummary,
    EventType.FORWARD_STARTING: ForwardSummary,
    EventType.FORWARD_ACTIVE: ForwardSummary,
    EventType.FORWARD_CLOSED: ForwardSummary,
    EventType.FORWARD_FAILED: ForwardSummary,
    EventType.OPERATION_CREATED: OperationSummary,
    EventType.OPERATION_STATE_CHANGED: OperationSummary,
    EventType.BROADCAST_OUTPUT: BroadcastCommandOutput,
    EventType.DAEMON_STATE_CHANGED: DaemonStatus,
    EventType.ERROR_OCCURRED: Mapping,
}

_ERROR_EVENT_KEYS = frozenset(
    {
        "code",
        "message",
        "details",
        "retryable",
        "request_id",
        "connection_id",
        "session_id",
    }
)
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def _freeze_public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_public_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_public_value(item) for item in value)
    return value


def _thaw_public_value(value: Any) -> Any:
    if type(value) in (dict, _MAPPING_PROXY_TYPE):
        return {
            key: _thaw_public_value(item)
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        return [_thaw_public_value(item) for item in value]
    return value


def _copy_error_event_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _thaw_public_value(payload)
    unknown = set(payload) - _ERROR_EVENT_KEYS
    if unknown:
        raise ValueError("error.occurred contains unsupported envelope fields")
    if type(payload.get("code")) is not str or not payload["code"]:
        raise TypeError("error.occurred code must be a non-empty string")
    if type(payload.get("message")) is not str or not payload["message"]:
        raise TypeError("error.occurred message must be a non-empty string")
    if "retryable" in payload and type(payload["retryable"]) is not bool:
        raise TypeError("error.occurred retryable must be a boolean")
    if "details" in payload and not isinstance(payload["details"], dict):
        raise TypeError("error.occurred details must be a dictionary")
    for id_name in ("request_id", "connection_id", "session_id"):
        if (
            id_name in payload
            and payload[id_name] is not None
            and type(payload[id_name]) is not str
        ):
            raise TypeError(f"error.occurred {id_name} must be a string or None")
    return _freeze_public_value(copy_safe_details(payload))


def _validate_dto_value(
    value: Any,
    *,
    path: str,
    allow_bytes: bool = False,
) -> None:
    if value is None or type(value) in (str, bool, int, datetime):
        return
    if isinstance(value, Enum):
        if not type(value).__module__.startswith("sshpilot.api."):
            raise TypeError(
                f"{path} contains unsupported public value type "
                f"{type(value).__name__}"
            )
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        return
    if isinstance(value, bytes):
        if allow_bytes:
            return
        raise TypeError(f"{path} contains bytes outside TerminalOutput")
    if isinstance(value, (tuple, list, frozenset)):
        for item in value:
            _validate_dto_value(item, path=f"{path}[]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} keys must be strings")
            _validate_dto_value(item, path=f"{path}.{key}")
        return
    if is_dataclass(value) and not isinstance(value, type):
        if not type(value).__module__.startswith("sshpilot.api.models."):
            raise TypeError(
                f"{path} contains unsupported public value type "
                f"{type(value).__name__}"
            )
        for model_field in fields(value):
            field_value = getattr(value, model_field.name)
            _validate_dto_value(
                field_value,
                path=f"{path}.{model_field.name}",
                allow_bytes=(
                    isinstance(value, TerminalOutput)
                    and model_field.name == "data"
                ),
            )
        return
    raise TypeError(
        f"{path} contains unsupported public value type "
        f"{type(value).__name__}"
    )


def validate_event_payload(event_type: EventType, payload: Any) -> Any:
    """Return a detached/validated payload for one public event type."""

    if not isinstance(event_type, EventType):
        raise TypeError(
            "event type must be an EventType, not "
            f"{type(event_type).__name__}"
        )
    expected = _EVENT_PAYLOAD_TYPES[event_type]
    if event_type is EventType.ERROR_OCCURRED:
        if type(payload) not in (dict, _MAPPING_PROXY_TYPE):
            raise TypeError(
                f"{event_type.value} requires payload type Mapping, "
                f"not {type(payload).__name__}"
            )
        return _copy_error_event_payload(payload)
    if type(payload) is not expected:
        raise TypeError(
            f"{event_type.value} requires payload type {expected.__name__}, "
            f"not {type(payload).__name__}"
        )
    _validate_dto_value(payload, path=f"{event_type.value}.payload")
    return payload


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

    def _deactivate(self) -> None:
        with self._lock:
            self._active = False

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.unsubscribe()


class EventPublisher:
    """Serial FIFO publisher used by the in-process adapter.

    The first publisher becomes the dispatcher. Concurrent and re-entrant
    publications are queued and delivered in sequence order without creating a
    worker thread or recursively growing the callback stack.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._subscribers: Dict[
            int,
            Tuple[CoreEventCallback, Subscription],
        ] = {}
        self._pending: Deque[_QueuedDelivery] = deque()
        self._next_subscriber_id = 1
        self._next_sequence = 0
        self._dispatching = False
        self._dispatcher_thread_id: Optional[int] = None
        self._closed = False

    def subscribe(self, callback: CoreEventCallback) -> Subscription:
        if not callable(callback):
            raise TypeError("event callback must be callable")

        subscriber_id = 0

        def _unsubscribe() -> None:
            with self._lock:
                self._subscribers.pop(subscriber_id, None)

        subscription = Subscription(_unsubscribe)
        with self._lock:
            if self._closed:
                subscription._deactivate()
                raise RuntimeError("event publisher is closed")
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = (callback, subscription)
        return subscription

    def publish(
        self,
        event_type: EventType,
        payload: Any,
        *,
        request_id: Optional[RequestId] = None,
        connection_id: Optional[ConnectionId] = None,
        session_id: Optional[SessionId] = None,
    ) -> CoreEvent[Any]:
        safe_payload = validate_event_payload(event_type, payload)
        with self._condition:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            sequence = self._next_sequence
            self._next_sequence += 1
            event = CoreEvent(
                type=event_type,
                payload=safe_payload,
                sequence=sequence,
                request_id=request_id,
                connection_id=connection_id,
                session_id=session_id,
            )
            should_drain = self._enqueue_locked(event)

        if should_drain:
            self._drain()
        return event

    def publish_existing(self, event: CoreEvent[Any]) -> CoreEvent[Any]:
        """Deliver a validated event while preserving an external sequence."""

        if not isinstance(event, CoreEvent):
            raise TypeError("existing event must be a CoreEvent")
        with self._condition:
            if self._closed:
                raise RuntimeError("event publisher is closed")
            if event.sequence < self._next_sequence:
                raise ValueError("existing event sequence is not monotonic")
            self._next_sequence = event.sequence + 1
            should_drain = self._enqueue_locked(event)

        if should_drain:
            self._drain()
        return event

    def _enqueue_locked(self, event: CoreEvent[Any]) -> bool:
        publisher_thread_id = threading.get_ident()
        delivery = _QueuedDelivery(
            event=event,
            callbacks=tuple(
                callback
                for callback, _subscription in self._subscribers.values()
            ),
        )
        self._pending.append(delivery)

        if not self._dispatching:
            self._dispatching = True
            self._dispatcher_thread_id = publisher_thread_id
            return True
        elif self._dispatcher_thread_id == publisher_thread_id:
            # Re-entrant publication is queued behind the current event.
            return False
        else:
            while not delivery.completed:
                self._condition.wait()
            return False

    def _drain(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    self._dispatching = False
                    self._dispatcher_thread_id = None
                    self._condition.notify_all()
                    return
                delivery = self._pending.popleft()

            for callback in delivery.callbacks:
                try:
                    callback(delivery.event)
                except Exception:
                    logger.exception(
                        "Core event subscriber failed for %s",
                        delivery.event.type.value,
                    )

            with self._condition:
                delivery.completed = True
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            subscriptions = tuple(
                subscription
                for _callback, subscription in self._subscribers.values()
            )
            self._subscribers.clear()
            self._condition.notify_all()
        for subscription in subscriptions:
            subscription._deactivate()


@dataclass
class _QueuedDelivery:
    event: CoreEvent[Any] = field(repr=False)
    callbacks: Tuple[CoreEventCallback, ...] = field(repr=False)
    completed: bool = False
