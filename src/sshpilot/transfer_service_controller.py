"""Frontend-neutral file-transfer controller for daemon-owned transfers.

Mirrors :mod:`sshpilot.terminal_session_controller` / :mod:`sshpilot.sftp_service_controller`:
every RPC goes through ``bridge.submit`` and progress/completion is delivered
via daemon ``transfer.*`` events, filtered per-transfer so many concurrent
uploads/downloads can share one controller instance. Used directly by
:mod:`sshpilot.daemon_sftp_backend` for uploads/downloads, and is reusable for
a future transfer-queue UI.

Reusable overwrite/path/progress policy lives in ``sshpilot.core.transfers``;
this controller is the GTK/API adapter that submits ``StartTransferRequest``
wire models to the daemon.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from .api.capabilities import Capability
from .api.errors import ErrorCode, SshPilotError
from .api.events import EventType
from .api.models.common import TransferId
from .api.models.transfers import (
    CancelTransferRequest,
    StartTransferBatchRequest,
    StartTransferRequest,
    TransferDirection,
    TransferState,
    TransferSummary,
)

logger = logging.getLogger(__name__)

_TERMINAL_TRANSFER_STATES = frozenset(
    {TransferState.COMPLETED, TransferState.CANCELLED, TransferState.FAILED}
)
_TRANSFER_EVENT_TYPES = frozenset(
    {
        EventType.TRANSFER_STARTED,
        EventType.TRANSFER_PROGRESS,
        EventType.TRANSFER_ITEM_COMPLETED,
        EventType.TRANSFER_COMPLETED,
        EventType.TRANSFER_CANCELLED,
        EventType.TRANSFER_FAILED,
    }
)
# Events that arrive before the start reply registers their transfer are held
# (latest per id) so a transfer that finishes in that window still completes.
# Bounded because events for transfers this controller never started land here too.
_UNCLAIMED_EVENT_LIMIT = 256


@dataclass(frozen=True)
class _TransferWatch:
    on_progress: Optional[Callable[[TransferSummary], None]]
    on_done: Optional[Callable[[TransferSummary], None]]


class TransferServiceController:
    """Bridge-backed controller for starting, tracking, and cancelling transfers."""

    def __init__(self, client, bridge) -> None:
        missing = daemon_transfer_capabilities_missing(client)
        if missing:
            raise RuntimeError(
                f"Required daemon transfer capabilities unavailable: {missing}"
            )
        self._client = client
        self._bridge = bridge
        self._watchers: Dict[TransferId, _TransferWatch] = {}
        self._unclaimed: "OrderedDict[TransferId, TransferSummary]" = OrderedDict()
        self._event_subscription = None
        self._closed = False

    def list_transfers(
        self,
        *,
        on_success: Callable[[list], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._submit(lambda: self._client.list_transfers(), on_success=on_success, on_error=on_error)

    def get_transfer(
        self,
        transfer_id: TransferId,
        *,
        on_success: Callable[[TransferSummary], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._submit(
            lambda: self._client.get_transfer(transfer_id),
            on_success=on_success,
            on_error=on_error,
        )

    def start_transfer(
        self,
        request: StartTransferRequest,
        *,
        on_started: Optional[Callable[[TransferSummary], None]] = None,
        on_progress: Optional[Callable[[TransferSummary], None]] = None,
        on_done: Optional[Callable[[TransferSummary], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """Start a daemon transfer; progress/completion arrive via events."""
        self._start(
            request.direction,
            lambda: self._client.start_transfer(request),
            required=(),
            on_started=on_started,
            on_progress=on_progress,
            on_done=on_done,
            on_error=on_error,
        )

    def start_transfer_batch(
        self,
        request: StartTransferBatchRequest,
        *,
        on_started: Optional[Callable[[TransferSummary], None]] = None,
        on_progress: Optional[Callable[[TransferSummary], None]] = None,
        on_done: Optional[Callable[[TransferSummary], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        """Start several items as one daemon transfer.

        ``on_progress`` also receives ``transfer.item_completed`` summaries;
        ``on_done`` fires once for the whole batch.
        """
        self._start(
            request.direction,
            lambda: self._client.start_transfer_batch(request),
            required=(Capability.TRANSFERS_BATCH,),
            on_started=on_started,
            on_progress=on_progress,
            on_done=on_done,
            on_error=on_error,
        )

    def _start(
        self,
        direction: TransferDirection,
        start: Callable[[], TransferSummary],
        *,
        required: Tuple[Capability, ...],
        on_started: Optional[Callable[[TransferSummary], None]],
        on_progress: Optional[Callable[[TransferSummary], None]],
        on_done: Optional[Callable[[TransferSummary], None]],
        on_error: Optional[Callable[[BaseException], None]],
    ) -> None:
        if self._closed:
            if on_error:
                on_error(SshPilotError(ErrorCode.INVALID_REQUEST, "Transfer controller is closed"))
            return
        directional_capability = (
            Capability.TRANSFERS_UPLOAD
            if direction is TransferDirection.UPLOAD
            else Capability.TRANSFERS_DOWNLOAD
        )
        supported = self._client.get_capabilities().supported
        for capability in (directional_capability, *required):
            if capability not in supported:
                if on_error:
                    on_error(
                        SshPilotError(
                            ErrorCode.UNSUPPORTED_CAPABILITY,
                            f"The daemon does not support {capability.value}",
                            details={"capability": capability.value},
                        )
                    )
                return

        def _on_success(summary: TransferSummary) -> None:
            if self._closed:
                return
            self._watchers[summary.id] = _TransferWatch(on_progress, on_done)
            if on_started is not None:
                on_started(summary)
            early = self._unclaimed.pop(summary.id, None)
            if summary.state in _TERMINAL_TRANSFER_STATES:
                self._deliver(summary)
            elif early is not None:
                self._deliver(early)

        # Subscribe before starting: a small transfer can finish before the
        # start reply comes back, and its terminal event must not be missed.
        self._ensure_subscription()
        try:
            self._bridge.submit(
                start,
                on_success=_on_success,
                on_error=on_error or (lambda _exc: None),
            )
        except RuntimeError:
            logger.debug("Transfer request submission failed", exc_info=True)
            if on_error:
                on_error(SshPilotError(ErrorCode.INTERNAL_ERROR, "Transfer request submission failed"))

    def cancel_transfer(
        self,
        transfer_id: TransferId,
        *,
        on_success: Optional[Callable[[None], None]] = None,
        on_error: Optional[Callable[[BaseException], None]] = None,
    ) -> None:
        self._submit(
            lambda: self._client.cancel_transfer(CancelTransferRequest(transfer_id=transfer_id)),
            on_success=on_success or (lambda _v: None),
            on_error=on_error or (lambda _exc: None),
        )

    def rebind_client(self, client, bridge=None) -> None:
        """Follow the app onto a replacement daemon transport.

        Transfers run in the daemon, so watched ones keep reporting progress
        once the event subscription moves to the new client.
        """
        if client is None:
            raise ValueError("a daemon client is required")
        if self._closed:
            return
        self._unsubscribe_events()
        self._client = client
        if bridge is not None:
            self._bridge = bridge
        if self._watchers:
            self._ensure_subscription()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._watchers.clear()
        self._unclaimed.clear()
        self._unsubscribe_events()

    # -- events ------------------------------------------------------
    def _ensure_subscription(self) -> None:
        if self._event_subscription is not None or self._closed:
            return
        subscribe = getattr(self._client, "subscribe_events", None)
        if not callable(subscribe):
            return

        def _on_event(event) -> None:
            if self._closed or event.type not in _TRANSFER_EVENT_TYPES:
                return
            summary = event.payload
            # Watchers are only touched on the main thread; filter there.
            try:
                self._bridge.submit(
                    lambda: summary,
                    on_success=self._on_transfer_event,
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
                logger.debug("Transfer event unsubscription failed", exc_info=True)

    def _on_transfer_event(self, summary: TransferSummary) -> None:
        if self._closed:
            return
        if summary.id in self._watchers:
            self._deliver(summary)
            return
        # Start reply not handled yet, or a transfer started elsewhere.
        held = self._unclaimed.get(summary.id)
        if held is not None and held.state in _TERMINAL_TRANSFER_STATES:
            return
        self._unclaimed[summary.id] = summary
        self._unclaimed.move_to_end(summary.id)
        while len(self._unclaimed) > _UNCLAIMED_EVENT_LIMIT:
            self._unclaimed.popitem(last=False)

    def _deliver(self, summary: TransferSummary) -> None:
        watch = self._watchers.get(summary.id)
        if watch is None:
            return
        if summary.state in _TERMINAL_TRANSFER_STATES:
            self._watchers.pop(summary.id, None)
            if watch.on_done is not None:
                watch.on_done(summary)
        elif watch.on_progress is not None:
            watch.on_progress(summary)

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


def required_daemon_transfer_capabilities() -> frozenset:
    """Required capabilities for daemon-backed file transfers."""
    return frozenset({Capability.TRANSFERS_READ, Capability.TRANSFERS_WRITE})


def daemon_transfer_capabilities_missing(client) -> frozenset:
    """Return missing required capabilities for daemon-backed file transfers."""
    required = required_daemon_transfer_capabilities()
    supported = client.get_capabilities().supported
    return required - supported
