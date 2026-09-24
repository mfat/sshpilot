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


def test_focus_batch_transfer_updates_name_and_paths(load_file_manager_window):
    from unittest.mock import MagicMock

    window_module = load_file_manager_window()
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    dialog = MagicMock()
    dialog.total_files = 3
    window._progress_dialog = dialog
    window._reset_batch_progress_state()
    window._batch_file_meta["a"] = ("one.txt", "/local/one.txt", "/remote/one.txt")
    window._batch_file_meta["b"] = ("two.txt", "/local/two.txt", "/remote/two.txt")

    window._focus_batch_transfer("a", force=True)
    dialog.set_operation_details.assert_called_with(total_files=3, filename="one.txt")
    dialog.set_paths.assert_called_with("/local/one.txt", "/remote/one.txt")

    dialog.reset_mock()
    window._focus_batch_transfer("a")  # same key — no-op
    dialog.set_operation_details.assert_not_called()

    window._focus_batch_transfer("b")
    dialog.set_operation_details.assert_called_with(total_files=3, filename="two.txt")
    dialog.set_paths.assert_called_with("/local/two.txt", "/remote/two.txt")


def test_focus_next_active_skips_settled_key(load_file_manager_window):
    from unittest.mock import MagicMock

    window_module = load_file_manager_window()
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    dialog = MagicMock()
    dialog.total_files = 2
    window._progress_dialog = dialog
    window._reset_batch_progress_state()
    window._batch_expected = {"a": 10, "b": 20}
    window._batch_file_meta = {
        "a": ("a.txt", "/a", "/ra"),
        "b": ("b.txt", "/b", "/rb"),
    }
    window._batch_active_bytes = {"b": (5, 20)}
    window._batch_focused_key = "a"

    window._focus_next_active_batch_transfer(exclude_key="a")
    dialog.set_operation_details.assert_called_with(total_files=2, filename="b.txt")
    dialog.set_paths.assert_called_with("/b", "/rb")
