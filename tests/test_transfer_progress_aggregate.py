"""Tests for multi-file transfer progress aggregation."""

from __future__ import annotations

from sshpilot.file_manager.transfer_progress import (
    aggregate_batch_bytes,
    progress_key_for_future,
)


def test_aggregate_known_totals_sums_active_and_queued():
    batch = aggregate_batch_bytes(
        expected={"a": 100, "b": 300, "c": 200},
        active={"a": (50, 100), "b": (100, 300)},
        settled={},
        files_completed=0,
        total_files=3,
    )
    assert batch.bytes_done == 150
    assert batch.bytes_total == 600
    assert batch.fraction == 0.25


def test_aggregate_includes_settled_and_keeps_monotonic_fraction():
    batch = aggregate_batch_bytes(
        expected={"a": 100, "b": 100, "c": 100},
        active={"b": (40, 100), "c": (10, 100)},
        settled={"a": (100, 100)},
        files_completed=1,
        total_files=3,
    )
    assert batch.bytes_done == 150
    assert batch.bytes_total == 300
    assert batch.fraction == 0.5


def test_aggregate_unknown_total_falls_back_to_file_fractions():
    # Concurrent transfers at 50% and 10% must not jump the bar backwards the
    # way a naive "(completed + latest_fraction) / n" would.
    batch = aggregate_batch_bytes(
        expected={"a": None, "b": None, "c": None},
        active={"a": (50, 100), "b": (10, 100)},
        settled={},
        files_completed=0,
        total_files=3,
    )
    assert batch.bytes_done == 60
    assert batch.bytes_total is None
    assert batch.fraction == (0.5 + 0.1) / 3


def test_aggregate_caps_fraction_while_files_remain():
    batch = aggregate_batch_bytes(
        expected={"a": None, "b": None},
        active={"a": (100, 100)},
        settled={},
        files_completed=0,
        total_files=2,
    )
    assert batch.fraction == 0.5


def test_progress_key_for_future_is_stable():
    class _Fut:
        pass

    fut = _Fut()
    assert progress_key_for_future(fut) == f"fut-{id(fut)}"
    assert progress_key_for_future(fut) == progress_key_for_future(fut)
