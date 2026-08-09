"""Daemon lifecycle and management models (Protocol v1 additive surface)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Mapping, Optional, Tuple

from .common import require_identifier, utc_now


class DaemonLogLevel(str, Enum):
    """Supported public daemon logging levels."""

    WARNING = "warning"
    INFO = "info"
    DEBUG = "debug"


@dataclass(frozen=True)
class SetDaemonLogLevelRequest:
    """Typed control request for the daemon's managed logging handlers."""

    level: DaemonLogLevel

    def __post_init__(self) -> None:
        if not isinstance(self.level, DaemonLogLevel):
            raise TypeError("daemon log level must be a DaemonLogLevel")


class DaemonLifecycleState(str, Enum):
    """Explicit production lifecycle for the local daemon process."""

    STARTING = "starting"
    READY = "ready"
    IDLE = "idle"
    DRAINING = "draining"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class DaemonDisconnectReason(str, Enum):
    """Structured reasons clients use to classify daemon loss."""

    CLEAN_SHUTDOWN = "clean_shutdown"
    RESTART = "restart"
    CRASH = "crash"
    TRANSPORT_LOSS = "transport_loss"
    SOCKET_REPLACED = "socket_replaced"
    INCOMPATIBLE = "incompatible"
    IDLE_SHUTDOWN = "idle_shutdown"
    FORCED_STOP = "forced_stop"


@dataclass(frozen=True)
class DaemonResourceCounts:
    clients: int = 0
    sessions_active: int = 0
    sessions_retained: int = 0
    sftp_active: int = 0
    sftp_retained: int = 0
    transfers_queued: int = 0
    transfers_starting: int = 0
    transfers_running: int = 0
    transfers_retained: int = 0
    forwards_active: int = 0
    forwards_retained: int = 0
    interactions_pending: int = 0

    def __post_init__(self) -> None:
        for name in (
            "clients",
            "sessions_active",
            "sessions_retained",
            "sftp_active",
            "sftp_retained",
            "transfers_queued",
            "transfers_starting",
            "transfers_running",
            "transfers_retained",
            "forwards_active",
            "forwards_retained",
            "interactions_pending",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")

    @property
    def live_blockers(self) -> Tuple[str, ...]:
        """Resource categories that keep the daemon from becoming idle."""

        blockers = []
        if self.clients:
            blockers.append("clients")
        if self.sessions_active:
            blockers.append("sessions")
        if self.sftp_active:
            blockers.append("sftp")
        if self.transfers_queued or self.transfers_starting or self.transfers_running:
            blockers.append("transfers")
        if self.forwards_active:
            blockers.append("forwards")
        if self.interactions_pending:
            blockers.append("interactions")
        return tuple(blockers)

    @property
    def has_live_resources(self) -> bool:
        return bool(self.live_blockers)


@dataclass(frozen=True)
class DaemonIdleInfo:
    idle_shutdown_enabled: bool
    idle_shutdown_seconds: Optional[float]
    idle_since: Optional[datetime] = None
    idle_deadline: Optional[datetime] = None
    idle_blockers: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.idle_shutdown_enabled) is not bool:
            raise TypeError("idle_shutdown_enabled must be a boolean")
        if self.idle_shutdown_seconds is not None and (
            type(self.idle_shutdown_seconds) is not float
            and type(self.idle_shutdown_seconds) is not int
        ):
            raise TypeError("idle_shutdown_seconds must be a number or None")
        if self.idle_shutdown_seconds is not None and self.idle_shutdown_seconds < 0:
            raise ValueError("idle_shutdown_seconds must not be negative")
        for stamp_name in ("idle_since", "idle_deadline"):
            stamp = getattr(self, stamp_name)
            if stamp is not None and (
                type(stamp) is not datetime or stamp.tzinfo is None
            ):
                raise ValueError(f"{stamp_name} must be timezone-aware or None")
        if type(self.idle_blockers) is not tuple:
            raise TypeError("idle_blockers must be a tuple of strings")
        for item in self.idle_blockers:
            if type(item) is not str or not item:
                raise ValueError("idle blockers must be non-empty strings")


@dataclass(frozen=True)
class DaemonStatus:
    state: DaemonLifecycleState
    server_instance_id: str
    started_at: datetime
    protocol_version: str
    api_implementation_version: str
    daemon_version: str
    development_revision: str
    resources: DaemonResourceCounts
    idle: DaemonIdleInfo
    shutdown_deadline: Optional[datetime] = None
    disconnect_reason: Optional[str] = None
    restart_requested: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.state, DaemonLifecycleState):
            raise TypeError("daemon state must be a DaemonLifecycleState")
        require_identifier(self.server_instance_id, "server instance id")
        if type(self.started_at) is not datetime or self.started_at.tzinfo is None:
            raise ValueError("daemon started_at must be timezone-aware")
        for name in (
            "protocol_version",
            "api_implementation_version",
            "daemon_version",
        ):
            require_identifier(getattr(self, name), name.replace("_", " "))
        if type(self.development_revision) is not str:
            raise TypeError("development_revision must be a string")
        if type(self.resources) is not DaemonResourceCounts:
            raise TypeError("resources must be DaemonResourceCounts")
        if type(self.idle) is not DaemonIdleInfo:
            raise TypeError("idle must be DaemonIdleInfo")
        if self.shutdown_deadline is not None and (
            type(self.shutdown_deadline) is not datetime
            or self.shutdown_deadline.tzinfo is None
        ):
            raise ValueError("shutdown_deadline must be timezone-aware or None")
        if self.disconnect_reason is not None and (
            type(self.disconnect_reason) is not str or not self.disconnect_reason
        ):
            raise ValueError("disconnect_reason must be a non-empty string or None")
        if type(self.restart_requested) is not bool:
            raise TypeError("restart_requested must be a boolean")


@dataclass(frozen=True)
class DaemonDiagnostics:
    status: DaemonStatus
    uptime_seconds: float
    executor_queue_depth: int = 0
    thread_counts_by_role: Mapping[str, int] = field(default_factory=dict)
    open_descriptor_count: Optional[int] = None
    rss_bytes: Optional[int] = None
    socket_bound: bool = True
    keep_alive_lease: bool = False

    def __post_init__(self) -> None:
        if type(self.status) is not DaemonStatus:
            raise TypeError("diagnostics status must be DaemonStatus")
        if type(self.uptime_seconds) not in (int, float) or self.uptime_seconds < 0:
            raise ValueError("uptime_seconds must be a non-negative number")
        if type(self.executor_queue_depth) is not int or self.executor_queue_depth < 0:
            raise ValueError("executor_queue_depth must be a non-negative int")
        if not isinstance(self.thread_counts_by_role, Mapping):
            raise TypeError("thread_counts_by_role must be a mapping")
        for key, value in self.thread_counts_by_role.items():
            if type(key) is not str or not key:
                raise ValueError("thread role names must be non-empty strings")
            if type(value) is not int or value < 0:
                raise ValueError("thread counts must be non-negative ints")
        if self.open_descriptor_count is not None and (
            type(self.open_descriptor_count) is not int or self.open_descriptor_count < 0
        ):
            raise ValueError("open_descriptor_count must be a non-negative int or None")
        if self.rss_bytes is not None and (
            type(self.rss_bytes) is not int or self.rss_bytes < 0
        ):
            raise ValueError("rss_bytes must be a non-negative int or None")
        if type(self.socket_bound) is not bool:
            raise TypeError("socket_bound must be a boolean")
        if type(self.keep_alive_lease) is not bool:
            raise TypeError("keep_alive_lease must be a boolean")


@dataclass(frozen=True)
class StopDaemonRequest:
    force: bool = False
    confirmation: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.force) is not bool:
            raise TypeError("force must be a boolean")
        if self.confirmation is not None and (
            type(self.confirmation) is not str or not self.confirmation.strip()
        ):
            raise ValueError("confirmation must be a non-empty string or None")


@dataclass(frozen=True)
class RestartDaemonRequest:
    force: bool = False
    confirmation: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.force) is not bool:
            raise TypeError("force must be a boolean")
        if self.confirmation is not None and (
            type(self.confirmation) is not str or not self.confirmation.strip()
        ):
            raise ValueError("confirmation must be a non-empty string or None")


@dataclass(frozen=True)
class DaemonStopResult:
    accepted: bool
    state: DaemonLifecycleState
    resources: DaemonResourceCounts
    will_lose: Tuple[str, ...] = ()
    confirmation: Optional[str] = None
    message: str = ""
    restart_requested: bool = False

    def __post_init__(self) -> None:
        if type(self.accepted) is not bool:
            raise TypeError("accepted must be a boolean")
        if not isinstance(self.state, DaemonLifecycleState):
            raise TypeError("state must be a DaemonLifecycleState")
        if type(self.resources) is not DaemonResourceCounts:
            raise TypeError("resources must be DaemonResourceCounts")
        if type(self.will_lose) is not tuple:
            raise TypeError("will_lose must be a tuple")
        for item in self.will_lose:
            if type(item) is not str or not item:
                raise ValueError("will_lose entries must be non-empty strings")
        if self.confirmation is not None and (
            type(self.confirmation) is not str or not self.confirmation.strip()
        ):
            raise ValueError("confirmation must be a non-empty string or None")
        if type(self.message) is not str:
            raise TypeError("message must be a string")
        if type(self.restart_requested) is not bool:
            raise TypeError("restart_requested must be a boolean")


def default_idle_shutdown_seconds(
    *,
    service_mode: bool = False,
    packaged: bool = False,
) -> Optional[float]:
    """Return the recommended idle-shutdown default for an environment.

    ``None`` means idle shutdown is disabled (explicit service mode).
    """

    if service_mode:
        return None
    if packaged:
        return 120.0
    return 300.0


# Transition helper used by the daemon controller (public for tests/docs).
_VALID_LIFECYCLE_TRANSITIONS = {
    DaemonLifecycleState.STARTING: frozenset(
        {
            DaemonLifecycleState.READY,
            DaemonLifecycleState.FAILED,
            DaemonLifecycleState.STOPPING,
        }
    ),
    DaemonLifecycleState.READY: frozenset(
        {
            DaemonLifecycleState.IDLE,
            DaemonLifecycleState.DRAINING,
            DaemonLifecycleState.STOPPING,
            DaemonLifecycleState.FAILED,
        }
    ),
    DaemonLifecycleState.IDLE: frozenset(
        {
            DaemonLifecycleState.READY,
            DaemonLifecycleState.DRAINING,
            DaemonLifecycleState.STOPPING,
            DaemonLifecycleState.FAILED,
        }
    ),
    DaemonLifecycleState.DRAINING: frozenset(
        {
            DaemonLifecycleState.STOPPING,
            DaemonLifecycleState.FAILED,
        }
    ),
    DaemonLifecycleState.STOPPING: frozenset(
        {
            DaemonLifecycleState.STOPPED,
            DaemonLifecycleState.FAILED,
        }
    ),
    DaemonLifecycleState.STOPPED: frozenset(),
    DaemonLifecycleState.FAILED: frozenset(
        {
            DaemonLifecycleState.STOPPED,
        }
    ),
}


def is_valid_lifecycle_transition(
    current: DaemonLifecycleState,
    new_state: DaemonLifecycleState,
) -> bool:
    if current is new_state:
        return True
    return new_state in _VALID_LIFECYCLE_TRANSITIONS.get(current, frozenset())


def utc_now_for_daemon() -> datetime:
    """Thin alias kept for call sites that want a stable import."""

    return utc_now()
