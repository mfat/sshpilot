"""Client-side daemon reconnect policy with crash-loop protection."""

from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Deque, Optional


class ReconnectOutcome(str, Enum):
    """What a reconnect planner should do next."""

    ATTEMPT = "attempt"
    BACKOFF = "backoff"
    GIVE_UP = "give_up"


@dataclass(frozen=True)
class ReconnectDecision:
    """Safe, secret-free reconnect planning result."""

    outcome: ReconnectOutcome
    delay_seconds: float = 0.0
    failures_in_window: int = 0
    message: str = ""


@dataclass
class DaemonReconnectPolicy:
    """Exponential backoff with jitter, bounded deadline, and crash-loop guard.

    Successful handshakes clear failure history and replace the tracked daemon
    instance id. This policy never restores sessions, transfers, or other live
    resources — callers must take a fresh snapshot after reconnect.
    """

    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    deadline_seconds: float = 60.0
    max_failures_in_window: int = 5
    failure_window_seconds: float = 120.0
    jitter_fraction: float = 0.2
    monotonic: Callable[[], float] = time.monotonic
    _random: Callable[[], float] = random.random
    _failures: Deque[float] = field(default_factory=deque, init=False, repr=False)
    _attempt_count: int = field(default=0, init=False, repr=False)
    _cycle_started_at: Optional[float] = field(default=None, init=False, repr=False)
    _last_known_instance_id: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if self.deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be positive")
        if self.max_failures_in_window <= 0:
            raise ValueError("max_failures_in_window must be positive")
        if self.failure_window_seconds <= 0:
            raise ValueError("failure_window_seconds must be positive")
        if not 0.0 <= self.jitter_fraction <= 1.0:
            raise ValueError("jitter_fraction must be between 0 and 1")

    @property
    def last_known_instance_id(self) -> Optional[str]:
        return self._last_known_instance_id

    def record_success(self, instance_id: str) -> None:
        """Replace tracked instance id and reset failure/backoff state."""

        if not instance_id:
            raise ValueError("instance_id must be non-empty")
        self._last_known_instance_id = instance_id
        self._failures.clear()
        self._attempt_count = 0
        self._cycle_started_at = None

    def clear_stale_instance(self) -> bool:
        """Drop a previously tracked instance id after transport loss."""

        if self._last_known_instance_id is None:
            return False
        self._last_known_instance_id = None
        return True

    def record_failure(self, reason: str = "") -> ReconnectDecision:
        """Record one failed reconnect attempt and return the next decision."""

        now = self.monotonic()
        if self._cycle_started_at is None:
            self._cycle_started_at = now
        self._prune_failures_locked(now)
        self._failures.append(now)
        self._attempt_count += 1
        failures = len(self._failures)
        if failures >= self.max_failures_in_window:
            return ReconnectDecision(
                outcome=ReconnectOutcome.GIVE_UP,
                failures_in_window=failures,
                message=(
                    "daemon reconnect stopped after repeated startup failures"
                    + (f" ({reason})" if reason else "")
                ),
            )
        if now - self._cycle_started_at >= self.deadline_seconds:
            return ReconnectDecision(
                outcome=ReconnectOutcome.GIVE_UP,
                failures_in_window=failures,
                message="daemon reconnect deadline exceeded",
            )
        delay = self._backoff_delay_locked()
        return ReconnectDecision(
            outcome=ReconnectOutcome.BACKOFF,
            delay_seconds=delay,
            failures_in_window=failures,
            message="daemon reconnect backing off",
        )

    def plan_attempt(self) -> ReconnectDecision:
        """Return ATTEMPT when the caller may try immediately, else BACKOFF/GIVE_UP."""

        now = self.monotonic()
        if self._cycle_started_at is None:
            self._cycle_started_at = now
        self._prune_failures_locked(now)
        failures = len(self._failures)
        if failures >= self.max_failures_in_window:
            return ReconnectDecision(
                outcome=ReconnectOutcome.GIVE_UP,
                failures_in_window=failures,
                message="daemon reconnect stopped after repeated startup failures",
            )
        if now - self._cycle_started_at >= self.deadline_seconds:
            return ReconnectDecision(
                outcome=ReconnectOutcome.GIVE_UP,
                failures_in_window=failures,
                message="daemon reconnect deadline exceeded",
            )
        if self._attempt_count == 0 and failures == 0:
            return ReconnectDecision(
                outcome=ReconnectOutcome.ATTEMPT,
                failures_in_window=0,
                message="daemon reconnect may proceed",
            )
        delay = self._backoff_delay_locked()
        return ReconnectDecision(
            outcome=ReconnectOutcome.BACKOFF,
            delay_seconds=delay,
            failures_in_window=failures,
            message="daemon reconnect waiting for backoff",
        )

    def _prune_failures_locked(self, now: float) -> None:
        cutoff = now - self.failure_window_seconds
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def _backoff_delay_locked(self) -> float:
        exponent = max(0, self._attempt_count - 1)
        base = min(
            self.max_delay_seconds,
            self.base_delay_seconds * (2**exponent),
        )
        jitter_span = base * self.jitter_fraction
        return max(0.0, base + (self._random() * 2.0 - 1.0) * jitter_span)
