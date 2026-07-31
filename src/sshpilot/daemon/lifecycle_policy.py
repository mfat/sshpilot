"""Daemon lifecycle policy: states, idle shutdown, and drain decisions."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sshpilot.api.models.daemon import (
    DaemonDisconnectReason,
    DaemonIdleInfo,
    DaemonLifecycleState,
    DaemonResourceCounts,
    DaemonStatus,
    DaemonStopResult,
    RestartDaemonRequest,
    StopDaemonRequest,
    default_idle_shutdown_seconds,
    is_valid_lifecycle_transition,
)
from sshpilot.api.version import API_IMPLEMENTATION_VERSION, PROTOCOL_VERSION

logger = logging.getLogger(__name__)

# Exit status the process may use so a supervisor can respawn after restart.
RESTART_EXIT_CODE = 75

# Sentinel so ``None`` still means "idle shutdown disabled" while omitting the
# argument selects the environment default (dev/packaged/service).
_IDLE_SHUTDOWN_UNSET: object = object()


@dataclass
class _IdleClock:
    """Monotonic idle bookkeeping (wall clock only for diagnostics stamps)."""

    idle_since_mono: Optional[float] = None
    idle_deadline_mono: Optional[float] = None
    idle_since_wall: Optional[datetime] = None
    idle_deadline_wall: Optional[datetime] = None


class DaemonLifecycleController:
    """Own the daemon lifecycle state machine and idle shutdown policy.

    The selector loop calls :meth:`select_timeout` and :meth:`poll_idle`.
    Management RPCs call :meth:`request_stop` / :meth:`request_restart`.
    """

    def __init__(
        self,
        *,
        server_instance_id: str,
        started_at: datetime,
        daemon_version: str,
        development_revision: str = "",
        idle_shutdown_seconds: object = _IDLE_SHUTDOWN_UNSET,
        service_mode: bool = False,
        packaged: bool = False,
        drain_timeout_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        on_state_changed: Optional[Callable[[DaemonStatus], None]] = None,
        on_shutdown: Optional[Callable[[], None]] = None,
    ) -> None:
        if idle_shutdown_seconds is _IDLE_SHUTDOWN_UNSET:
            configured = default_idle_shutdown_seconds(
                service_mode=service_mode,
                packaged=packaged,
            )
        elif idle_shutdown_seconds is None:
            configured = None
        elif isinstance(idle_shutdown_seconds, (int, float)) and not isinstance(
            idle_shutdown_seconds, bool
        ):
            configured = float(idle_shutdown_seconds)
        else:
            raise TypeError("idle_shutdown_seconds must be a number or None")
        if configured is not None:
            if configured < 0:
                raise ValueError("idle_shutdown_seconds must not be negative")
        if drain_timeout_seconds < 0:
            raise ValueError("drain_timeout_seconds must not be negative")
        self._server_instance_id = server_instance_id
        self._started_at = started_at
        self._daemon_version = daemon_version
        self._development_revision = development_revision or ""
        self._idle_shutdown_seconds = configured
        self._drain_timeout_seconds = float(drain_timeout_seconds)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._on_state_changed = on_state_changed
        self._on_shutdown = on_shutdown
        self._lock = threading.RLock()
        self._state = DaemonLifecycleState.STARTING
        self._idle = _IdleClock()
        self._shutdown_deadline_wall: Optional[datetime] = None
        self._shutdown_deadline_mono: Optional[float] = None
        self._disconnect_reason: Optional[str] = None
        self._restart_requested = False
        self._keep_alive_lease = False
        self._pending_confirmation: Optional[str] = None
        self._pending_confirm_snapshot: Optional[DaemonResourceCounts] = None
        self._resource_provider: Optional[Callable[[], DaemonResourceCounts]] = None
        self._config_reload_active = False
        self._immediate_shutdown = False

    # -- wiring ---------------------------------------------------------
    def set_resource_provider(
        self,
        provider: Callable[[], DaemonResourceCounts],
    ) -> None:
        self._resource_provider = provider

    def set_keep_alive_lease(self, active: bool) -> None:
        with self._lock:
            self._keep_alive_lease = bool(active)
            self._recompute_idle_locked()

    def set_config_reload_active(self, active: bool) -> None:
        with self._lock:
            self._config_reload_active = bool(active)
            self._recompute_idle_locked()

    @property
    def state(self) -> DaemonLifecycleState:
        with self._lock:
            return self._state

    @property
    def restart_requested(self) -> bool:
        with self._lock:
            return self._restart_requested

    @property
    def disconnect_reason(self) -> Optional[str]:
        with self._lock:
            return self._disconnect_reason

    @property
    def keep_alive_lease(self) -> bool:
        with self._lock:
            return self._keep_alive_lease

    @property
    def idle_shutdown_seconds(self) -> Optional[float]:
        return self._idle_shutdown_seconds

    def accepting_new_work(self) -> bool:
        with self._lock:
            return self._state in {
                DaemonLifecycleState.READY,
                DaemonLifecycleState.IDLE,
            }

    def is_draining_or_stopping(self) -> bool:
        with self._lock:
            return self._state in {
                DaemonLifecycleState.DRAINING,
                DaemonLifecycleState.STOPPING,
                DaemonLifecycleState.STOPPED,
                DaemonLifecycleState.FAILED,
            }

    # -- transitions ----------------------------------------------------
    def mark_ready(self) -> None:
        with self._lock:
            self._transition_locked(DaemonLifecycleState.READY)
            self._recompute_idle_locked()

    def mark_failed(self, reason: str = DaemonDisconnectReason.CRASH.value) -> None:
        with self._lock:
            self._disconnect_reason = reason
            self._transition_locked(DaemonLifecycleState.FAILED)

    def mark_stopped(self) -> None:
        with self._lock:
            self._transition_locked(DaemonLifecycleState.STOPPED)

    def note_activity(self) -> None:
        """Cancel idle countdown after client/resource activity."""

        with self._lock:
            self._recompute_idle_locked()

    def begin_drain(
        self,
        *,
        reason: str,
        force: bool = False,
        restart: bool = False,
    ) -> None:
        with self._lock:
            if self._state in {
                DaemonLifecycleState.DRAINING,
                DaemonLifecycleState.STOPPING,
                DaemonLifecycleState.STOPPED,
            }:
                if restart:
                    self._restart_requested = True
                return
            self._disconnect_reason = reason
            self._restart_requested = bool(restart)
            now_mono = self._monotonic()
            self._shutdown_deadline_mono = now_mono + self._drain_timeout_seconds
            self._shutdown_deadline_wall = self._wall_clock() + timedelta(
                seconds=self._drain_timeout_seconds
            )
            self._clear_idle_locked()
            self._transition_locked(DaemonLifecycleState.DRAINING)
            if force:
                # Forced stop still drains briefly so clients get a reason,
                # then the server proceeds to STOPPING on the same path.
                pass

    def begin_stopping(self) -> None:
        with self._lock:
            if self._state is DaemonLifecycleState.STOPPED:
                return
            if self._state is not DaemonLifecycleState.DRAINING:
                self._transition_locked(DaemonLifecycleState.DRAINING)
            self._transition_locked(DaemonLifecycleState.STOPPING)

    # -- management RPCs ------------------------------------------------
    def request_stop(self, request: StopDaemonRequest) -> DaemonStopResult:
        return self._request_control(request, restart=False)

    def request_restart(self, request: RestartDaemonRequest) -> DaemonStopResult:
        return self._request_control(request, restart=True)

    def _request_control(
        self,
        request: StopDaemonRequest | RestartDaemonRequest,
        *,
        restart: bool,
    ) -> DaemonStopResult:
        with self._lock:
            resources = self._resources_locked()
            will_lose = tuple(
                name for name in resources.live_blockers if name != "clients"
            )
            if self._state in {
                DaemonLifecycleState.DRAINING,
                DaemonLifecycleState.STOPPING,
                DaemonLifecycleState.STOPPED,
            }:
                if restart:
                    self._restart_requested = True
                return DaemonStopResult(
                    accepted=True,
                    state=self._state,
                    resources=resources,
                    will_lose=will_lose,
                    message="Shutdown already in progress",
                    restart_requested=self._restart_requested,
                )
            if will_lose and not request.force:
                token = request.confirmation
                if (
                    token is None
                    or self._pending_confirmation is None
                    or token != self._pending_confirmation
                ):
                    self._pending_confirmation = secrets.token_hex(16)
                    self._pending_confirm_snapshot = resources
                    action = "restart" if restart else "stop"
                    return DaemonStopResult(
                        accepted=False,
                        state=self._state,
                        resources=resources,
                        will_lose=will_lose,
                        confirmation=self._pending_confirmation,
                        message=(
                            f"Daemon {action} would destroy live resources; "
                            "retry with confirmation or force=true"
                        ),
                        restart_requested=False,
                    )
            reason = (
                DaemonDisconnectReason.RESTART.value
                if restart
                else (
                    DaemonDisconnectReason.FORCED_STOP.value
                    if request.force
                    else DaemonDisconnectReason.CLEAN_SHUTDOWN.value
                )
            )
            self._pending_confirmation = None
            self._pending_confirm_snapshot = None
            if self._state in {
                DaemonLifecycleState.DRAINING,
                DaemonLifecycleState.STOPPING,
                DaemonLifecycleState.STOPPED,
            }:
                if restart:
                    self._restart_requested = True
            else:
                self._disconnect_reason = reason
                self._restart_requested = bool(restart)
                now_mono = self._monotonic()
                if will_lose and not request.force:
                    # Graceful stop: keep serving close/status during drain.
                    self._shutdown_deadline_mono = (
                        now_mono + self._drain_timeout_seconds
                    )
                    self._shutdown_deadline_wall = self._wall_clock() + timedelta(
                        seconds=self._drain_timeout_seconds
                    )
                    self._immediate_shutdown = False
                else:
                    # No live work, or force=True (Terminate everything): do not
                    # wait out the drain window — tear down immediately.
                    self._shutdown_deadline_mono = now_mono
                    self._shutdown_deadline_wall = self._wall_clock()
                    self._immediate_shutdown = True
                self._clear_idle_locked()
                self._transition_locked(DaemonLifecycleState.DRAINING)
            should_wake = self._on_shutdown is not None
        # Wake the selector so poll_idle runs after the RPC response is queued.
        # Do not tear down clients inline — that would drop the stop reply.
        if should_wake and self._on_shutdown is not None:
            self._on_shutdown()
        with self._lock:
            return DaemonStopResult(
                accepted=True,
                state=self._state,
                resources=self._resources_locked(),
                will_lose=will_lose,
                message="Shutdown accepted",
                restart_requested=self._restart_requested,
            )

    # -- idle / selector ------------------------------------------------
    def select_timeout(self) -> Optional[float]:
        """Return a selector timeout, or None to block indefinitely."""

        with self._lock:
            candidates = []
            if self._idle.idle_deadline_mono is not None:
                remaining = self._idle.idle_deadline_mono - self._monotonic()
                candidates.append(max(0.0, remaining))
            if self._shutdown_deadline_mono is not None:
                remaining = self._shutdown_deadline_mono - self._monotonic()
                candidates.append(max(0.0, remaining))
            if not candidates:
                return None
            return min(candidates)

    def poll_idle(self) -> bool:
        """Advance idle/drain deadlines. Return True if shutdown should start."""

        with self._lock:
            self._recompute_idle_locked()
            now = self._monotonic()
            if (
                self._state is DaemonLifecycleState.IDLE
                and self._idle.idle_deadline_mono is not None
                and now >= self._idle.idle_deadline_mono
            ):
                self._disconnect_reason = DaemonDisconnectReason.IDLE_SHUTDOWN.value
                self._transition_locked(DaemonLifecycleState.DRAINING)
                self._shutdown_deadline_mono = now + self._drain_timeout_seconds
                self._shutdown_deadline_wall = self._wall_clock() + timedelta(
                    seconds=self._drain_timeout_seconds
                )
                self._clear_idle_locked()
                return True
            if (
                self._state is DaemonLifecycleState.DRAINING
                and self._shutdown_deadline_mono is not None
                and now >= self._shutdown_deadline_mono
            ):
                return True
            return False

    # -- snapshots ------------------------------------------------------
    def status(self) -> DaemonStatus:
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> DaemonStatus:
        resources = self._resources_locked()
        blockers = list(resources.live_blockers)
        if self._keep_alive_lease:
            blockers.append("keep_alive")
        if self._config_reload_active:
            blockers.append("config_reload")
        idle = DaemonIdleInfo(
            idle_shutdown_enabled=bool(
                self._idle_shutdown_seconds and self._idle_shutdown_seconds > 0
            ),
            idle_shutdown_seconds=self._idle_shutdown_seconds,
            idle_since=self._idle.idle_since_wall,
            idle_deadline=self._idle.idle_deadline_wall,
            idle_blockers=tuple(blockers),
        )
        return DaemonStatus(
            state=self._state,
            server_instance_id=self._server_instance_id,
            started_at=self._started_at,
            protocol_version=PROTOCOL_VERSION,
            api_implementation_version=API_IMPLEMENTATION_VERSION,
            daemon_version=self._daemon_version,
            development_revision=self._development_revision,
            resources=resources,
            idle=idle,
            shutdown_deadline=self._shutdown_deadline_wall,
            disconnect_reason=self._disconnect_reason,
            restart_requested=self._restart_requested,
        )

    def _resources_locked(self) -> DaemonResourceCounts:
        if self._resource_provider is None:
            return DaemonResourceCounts()
        return self._resource_provider()

    def _recompute_idle_locked(self) -> None:
        if self._state not in {
            DaemonLifecycleState.READY,
            DaemonLifecycleState.IDLE,
        }:
            self._clear_idle_locked()
            return
        resources = self._resources_locked()
        blockers = list(resources.live_blockers)
        if self._keep_alive_lease:
            blockers.append("keep_alive")
        if self._config_reload_active:
            blockers.append("config_reload")
        enabled = bool(self._idle_shutdown_seconds and self._idle_shutdown_seconds > 0)
        if blockers or not enabled:
            was_idle = self._state is DaemonLifecycleState.IDLE
            self._clear_idle_locked()
            if was_idle:
                self._transition_locked(DaemonLifecycleState.READY)
            return
        # No blockers — enter or continue IDLE.
        now_mono = self._monotonic()
        if self._idle.idle_since_mono is None:
            self._idle.idle_since_mono = now_mono
            self._idle.idle_since_wall = self._wall_clock()
            assert self._idle_shutdown_seconds is not None
            self._idle.idle_deadline_mono = now_mono + float(
                self._idle_shutdown_seconds
            )
            self._idle.idle_deadline_wall = self._wall_clock() + timedelta(
                seconds=float(self._idle_shutdown_seconds)
            )
            if self._state is DaemonLifecycleState.READY:
                self._transition_locked(DaemonLifecycleState.IDLE)

    def _clear_idle_locked(self) -> None:
        self._idle = _IdleClock()

    def _transition_locked(self, new_state: DaemonLifecycleState) -> None:
        current = self._state
        if current is new_state:
            return
        if not is_valid_lifecycle_transition(current, new_state):
            logger.warning(
                "ignoring invalid lifecycle transition %s -> %s",
                current.value,
                new_state.value,
            )
            return
        self._state = new_state
        logger.info(
            "daemon lifecycle state=%s instance=%s",
            new_state.value,
            self._server_instance_id,
        )
        if self._on_state_changed is not None:
            try:
                self._on_state_changed(self._status_locked())
            except Exception:  # pragma: no cover - defensive
                logger.exception("lifecycle state callback failed")
