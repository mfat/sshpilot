"""Frontend-neutral port-forward controller for daemon-owned forwards.

Mirrors :mod:`sshpilot.transfer_service_controller`: RPCs go through
``bridge.submit`` and state transitions arrive via daemon ``forward.*``
events, filtered per-forward. Used by the plugin API's ``ensure_local_forward``
when daemon forwarding capabilities are available, and is reusable for a
future forwarding-management dialog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .api.capabilities import Capability
from .api.errors import SshPilotError
from .api.events import EventType
from .api.models.common import ForwardId
from .api.models.operations import (
    CloseForwardRequest,
    ForwardState,
    ForwardSummary,
    OpenForwardRequest,
)

logger = logging.getLogger(__name__)

_TERMINAL_FORWARD_STATES = frozenset({ForwardState.CLOSED, ForwardState.FAILED})
_FORWARD_EVENT_TYPES = frozenset(
    {
        EventType.FORWARD_CREATED,
        EventType.FORWARD_STARTING,
        EventType.FORWARD_ACTIVE,
        EventType.FORWARD_CLOSED,
        EventType.FORWARD_FAILED,
    }
)


@dataclass(frozen=True)
class _ForwardWatch:
    on_state_changed: Optional[Callable[[ForwardSummary], None]]
    on_closed: Optional[Callable[[ForwardSummary], None]]


class ForwardServiceController:
    """Bridge-backed controller for opening, tracking, and closing forwards."""

    def __init__(self, client, bridge) -> None:
        missing = daemon_forward_capabilities_missing(client)
        if missing:
            raise RuntimeError(
                f"Required daemon forward capabilities unavailable: {missing}"
            )
        self._client = client
        self._bridge = bridge
        self._watchers: Dict[ForwardId, _ForwardWatch] = {}
        self._event_subscription = None
        self._closed = False

    def list_forwards(
        self,
        *,
        on_success: Callable[[list], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._submit(lambda: self._client.list_forwards(), on_success=on_success, on_error=on_error)

    def get_forward(
        self,
        forward_id: ForwardId,
        *,
        on_success: Callable[[ForwardSummary], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._submit(
            lambda: self._client.get_forward(forward_id),
            on_success=on_success,
            on_error=on_error,
        )

    def open_forward(
        self,
        request: OpenForwardRequest,
        *,
        on_state_changed: Optional[Callable[[ForwardSummary], None]] = None,
        on_closed: Optional[Callable[[ForwardSummary], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """Open a daemon forward; state transitions arrive via events."""
        if self._closed:
            if on_error:
                on_error(RuntimeError("Forward controller is closed"))
            return

        def _on_success(summary: ForwardSummary) -> None:
            if self._closed:
                return
            self._watchers[summary.id] = _ForwardWatch(on_state_changed, on_closed)
            self._ensure_subscription()
            self._deliver(summary)

        try:
            self._bridge.submit(
                lambda: self._client.open_forward(request),
                on_success=_on_success,
                on_error=on_error or (lambda _exc: None),
            )
        except RuntimeError as exc:
            if on_error:
                on_error(exc)

    def close_forward(
        self,
        forward_id: ForwardId,
        *,
        on_success: Optional[Callable[[None], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._submit(
            lambda: self._client.close_forward(CloseForwardRequest(forward_id=forward_id)),
            on_success=on_success or (lambda _v: None),
            on_error=on_error or (lambda _exc: None),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._watchers.clear()
        self._unsubscribe_events()

    # -- events ------------------------------------------------------
    def _ensure_subscription(self) -> None:
        if self._event_subscription is not None or self._closed:
            return
        subscribe = getattr(self._client, "subscribe_events", None)
        if not callable(subscribe):
            return

        def _on_event(event) -> None:
            if self._closed or event.type not in _FORWARD_EVENT_TYPES:
                return
            summary = event.payload
            if summary.id not in self._watchers:
                return
            try:
                self._bridge.submit(
                    lambda: summary,
                    on_success=self._deliver,
                    on_error=lambda _e: None,
                )
            except RuntimeError:
                pass

        try:
            self._event_subscription = subscribe(_on_event)
        except SshPilotError:
            pass

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
                logger.debug("Forward event unsubscription failed", exc_info=True)

    def _deliver(self, summary: ForwardSummary) -> None:
        watch = self._watchers.get(summary.id)
        if watch is None:
            return
        if summary.state in _TERMINAL_FORWARD_STATES:
            self._watchers.pop(summary.id, None)
            if watch.on_closed is not None:
                watch.on_closed(summary)
        elif watch.on_state_changed is not None:
            watch.on_state_changed(summary)

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


def required_daemon_forward_capabilities() -> frozenset:
    """Required capabilities for daemon-backed port forwarding."""
    return frozenset({Capability.FORWARDS_READ, Capability.FORWARDS_WRITE})


def daemon_forward_capabilities_missing(client) -> frozenset:
    """Return missing required capabilities for daemon-backed port forwarding."""
    required = required_daemon_forward_capabilities()
    supported = client.get_capabilities().supported
    return required - supported
