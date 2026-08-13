"""Unit tests for DaemonReconnectPolicy crash-loop and backoff behavior."""

from __future__ import annotations

import pytest

from sshpilot.daemon.reconnect import (
    DaemonReconnectPolicy,
    ReconnectOutcome,
)


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class DeterministicRandom:
    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._index = 0

    def __call__(self) -> float:
        if self._index >= len(self._values):
            return 0.5
        value = self._values[self._index]
        self._index += 1
        return value


def test_record_success_clears_failures_and_sets_instance():
    clock = FakeClock()
    policy = DaemonReconnectPolicy(monotonic=clock.monotonic)
    policy.record_failure("startup_timeout")
    policy.record_success("instance-abc")
    assert policy.last_known_instance_id == "instance-abc"
    decision = policy.plan_attempt()
    assert decision.outcome is ReconnectOutcome.ATTEMPT
    assert decision.failures_in_window == 0


def test_clear_stale_instance_drops_previous_id():
    policy = DaemonReconnectPolicy()
    policy.record_success("instance-old")
    assert policy.clear_stale_instance() is True
    assert policy.last_known_instance_id is None
    assert policy.clear_stale_instance() is False


def test_exponential_backoff_with_jitter():
    clock = FakeClock()
    rng = DeterministicRandom([0.0, 1.0])
    policy = DaemonReconnectPolicy(
        base_delay_seconds=1.0,
        max_delay_seconds=8.0,
        jitter_fraction=0.1,
        monotonic=clock.monotonic,
        _random=rng,
    )
    first = policy.record_failure("handshake_failed")
    assert first.outcome is ReconnectOutcome.BACKOFF
    assert first.delay_seconds == pytest.approx(0.9)
    second = policy.record_failure("handshake_failed")
    assert second.outcome is ReconnectOutcome.BACKOFF
    assert second.delay_seconds == pytest.approx(2.2)


def test_crash_loop_stops_after_threshold():
    clock = FakeClock()
    policy = DaemonReconnectPolicy(
        max_failures_in_window=3,
        failure_window_seconds=30.0,
        monotonic=clock.monotonic,
        _random=lambda: 0.5,
    )
    for _ in range(2):
        decision = policy.record_failure("process_exited")
        assert decision.outcome is ReconnectOutcome.BACKOFF
    final = policy.record_failure("process_exited")
    assert final.outcome is ReconnectOutcome.GIVE_UP
    assert final.failures_in_window == 3


def test_deadline_exceeded_gives_up():
    clock = FakeClock()
    policy = DaemonReconnectPolicy(
        deadline_seconds=10.0,
        max_failures_in_window=100,
        monotonic=clock.monotonic,
        _random=lambda: 0.5,
    )
    policy.record_failure("startup_timeout")
    clock.advance(11.0)
    decision = policy.plan_attempt()
    assert decision.outcome is ReconnectOutcome.GIVE_UP
    assert "deadline" in decision.message


def test_failure_window_prunes_old_attempts():
    clock = FakeClock()
    policy = DaemonReconnectPolicy(
        max_failures_in_window=2,
        failure_window_seconds=5.0,
        monotonic=clock.monotonic,
        _random=lambda: 0.5,
    )
    policy.record_failure("process_exited")
    clock.advance(6.0)
    decision = policy.record_failure("process_exited")
    assert decision.outcome is ReconnectOutcome.BACKOFF
    assert decision.failures_in_window == 1
