"""Thin GTK-facing helper that combines launcher startup with reconnect policy."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.daemon.launcher import (
    DaemonLaunchError,
    DaemonLauncher,
    DaemonLaunchResult,
)
from sshpilot.daemon.reconnect import (
    DaemonReconnectPolicy,
    ReconnectDecision,
    ReconnectOutcome,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DaemonReconnectResult:
    """Outcome of one bounded reconnect attempt."""

    client: Optional[DaemonClient]
    launched: Optional[DaemonLaunchResult]
    decision: ReconnectDecision
    transport_loss: bool = False


class DaemonReconnectHelper:
    """Plan and execute daemon reconnect without restoring live resources."""

    def __init__(
        self,
        *,
        policy: Optional[DaemonReconnectPolicy] = None,
        launcher: Optional[DaemonLauncher] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._policy = policy or DaemonReconnectPolicy()
        self._launcher = launcher or DaemonLauncher()
        self._sleep = sleep
        self._lock = threading.Lock()

    @property
    def policy(self) -> DaemonReconnectPolicy:
        return self._policy

    def note_transport_loss(self) -> None:
        """Forget the last instance id after an unexpected transport close."""

        with self._lock:
            self._policy.clear_stale_instance()

    def reconnect(self, *, wait_for_backoff: bool = True) -> DaemonReconnectResult:
        """Try one reconnect cycle following the configured policy."""

        with self._lock:
            decision = self._policy.plan_attempt()
            if decision.outcome is ReconnectOutcome.GIVE_UP:
                return DaemonReconnectResult(
                    client=None,
                    launched=None,
                    decision=decision,
                )
            if (
                decision.outcome is ReconnectOutcome.BACKOFF
                and wait_for_backoff
                and decision.delay_seconds > 0
            ):
                self._sleep(decision.delay_seconds)
                decision = ReconnectDecision(
                    outcome=ReconnectOutcome.ATTEMPT,
                    failures_in_window=decision.failures_in_window,
                    message="daemon reconnect backoff elapsed",
                )
            if decision.outcome is not ReconnectOutcome.ATTEMPT:
                return DaemonReconnectResult(
                    client=None,
                    launched=None,
                    decision=decision,
                )
            try:
                launched = self._launcher.connect_or_start()
            except DaemonLaunchError as error:
                failure = self._policy.record_failure(error.reason.value)
                logger.warning(
                    "daemon reconnect failed reason=%s outcome=%s",
                    error.reason.value,
                    failure.outcome.value,
                )
                return DaemonReconnectResult(
                    client=None,
                    launched=None,
                    decision=failure,
                    transport_loss=True,
                )
            instance_id = launched.client.server_instance_id
            self._policy.record_success(instance_id)
            logger.info(
                "daemon reconnect succeeded instance=%s",
                instance_id,
            )
            return DaemonReconnectResult(
                client=launched.client,
                launched=launched,
                decision=ReconnectDecision(
                    outcome=ReconnectOutcome.ATTEMPT,
                    message="daemon reconnect succeeded",
                ),
            )
