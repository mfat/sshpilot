"""Regression tests for SFTP progress dialog reuse detection."""

from concurrent.futures import Future

from sshpilot.file_manager.progress_dialog import SFTPProgressDialog


def _bare_dialog(**overrides):
    dlg = SFTPProgressDialog.__new__(SFTPProgressDialog)
    dlg.is_cancelled = overrides.get("is_cancelled", False)
    dlg._closed = overrides.get("_closed", False)
    dlg._completion_shown = overrides.get("_completion_shown", False)
    dlg.operation_type = overrides.get("operation_type", "upload")
    dlg.total_files = overrides.get("total_files", 1)
    dlg.files_completed = overrides.get("files_completed", 0)
    visible = overrides.get("visible", True)
    dlg.get_visible = lambda: visible
    dlg._completions = []
    dlg.show_completion = lambda success=True, error_message=None: dlg._completions.append(
        (success, error_message)
    )
    return dlg


def test_is_reusable_when_open_and_active():
    assert _bare_dialog().is_reusable() is True


def test_is_reusable_false_when_closed():
    assert _bare_dialog(_closed=True).is_reusable() is False


def test_is_reusable_false_when_completed():
    assert _bare_dialog(_completion_shown=True).is_reusable() is False


def test_is_reusable_false_when_cancelled():
    assert _bare_dialog(is_cancelled=True).is_reusable() is False


def test_is_reusable_while_running_in_the_background():
    """Transfers run in the background (#1356): a dialog that was never put
    on screen still tracks its transfer and takes more of the batch."""
    assert _bare_dialog(visible=False).is_reusable() is True


def _background_dialog():
    dlg = _bare_dialog(visible=False)
    dlg._presented = False
    dlg._current_future = None
    dlg._futures = []
    dlg._render_timeout_id = None
    dlg._total_bytes = 0
    dlg._transferred_bytes = 0
    dlg._latest_fraction = None
    dlg._latest_message = None
    dlg.current_file = "big.iso"
    dlg._heading_text = "Uploading 1 File"
    dlg._status_text = ""
    dlg._speed_text = "2.0 MB/s"
    dlg._eta_text = "5s remaining"
    dlg._succeeded = None
    dlg._error_message = None
    dlg._locate_path = None
    dlg._host_label = dlg._destination_host_label = "srv"
    return dlg


def test_closing_the_details_does_not_cancel_the_transfer():
    dlg = _background_dialog()
    future = Future()
    dlg.set_future(future)
    dlg._presented = True

    dlg._on_dialog_closed(dlg)

    assert not future.cancelled()
    assert dlg.is_cancelled is False
    assert dlg._presented is False
    assert dlg.is_reusable()


def test_cancel_cancels_the_running_transfer():
    dlg = _background_dialog()
    future = Future()
    dlg.set_future(future)

    dlg.cancel()

    assert future.cancelled()
    snap = dlg.snapshot()
    assert snap["state"] == "cancelled"
    assert snap["detail"] == ""  # the route is already the subtitle


def test_snapshot_reports_progress_then_the_outcome():
    dlg = _background_dialog()
    dlg._total_bytes, dlg._transferred_bytes, dlg._latest_fraction = 100, 40, 0.4
    snap = dlg.snapshot()
    assert snap["state"] == "running" and snap["fraction"] == 0.4
    assert "2.0 MB/s" in snap["detail"] and snap["subtitle"] == "big.iso"

    dlg._completion_shown, dlg._succeeded = True, False
    dlg._error_message = "Permission denied"
    snap = dlg.snapshot()
    assert snap["state"] == "failed" and snap["error"] == "Permission denied"


def test_a_cancelled_future_reads_as_cancelled_without_the_dialog():
    """Cancelled elsewhere (the daemon, another path): not left 'running'."""
    dlg = _background_dialog()
    future = Future()
    dlg.set_future(future)
    future.cancel()
    assert dlg.snapshot()["state"] == "cancelled"


def test_complete_delete_progress_reports_partial_failures(load_file_manager_window):
    window_module = load_file_manager_window()
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    dialog = _bare_dialog(operation_type="delete", total_files=3)
    window._aggregate_dialog = dialog
    future = Future()
    future.set_result(([("/a", OSError("denied"))], 2))
    window._complete_delete_progress(future)
    assert dialog.files_completed == 2
    assert len(dialog._completions) == 1
    success, message = dialog._completions[0]
    assert success is False
    assert "1 of 3" in message
    assert "failed" in message


def test_complete_delete_progress_skips_when_dialog_already_cancelled(
    load_file_manager_window,
):
    window_module = load_file_manager_window()
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    dialog = _bare_dialog(operation_type="delete", total_files=2, is_cancelled=True)
    window._aggregate_dialog = dialog
    future = Future()
    future.set_result(([], 1))
    window._complete_delete_progress(future)
    assert dialog._completions == []


def test_delete_items_done_floors_fractional_recursive_progress():
    """Partial tree walks must not count as finished selected items."""
    done = SFTPProgressDialog._delete_items_done
    # Mid-walk on the first of two directories → still 0 completed.
    assert done(0.25, 2, 0) == 0
    assert done(0.495, 2, 0) == 0
    # First directory finished.
    assert done(0.5, 2, 0) == 1
    # Nested progress inside a 10-item batch must not round up early.
    assert done(0.06, 10, 0) == 0
    assert done(0.09, 10, 0) == 0
    assert done(0.1, 10, 0) == 1
    # Discrete file-chunk progress stays exact.
    assert done(0.64, 100, 0) == 64
    # Single recursive delete stays 0 until the operation reports 1.0.
    assert done(0.99, 1, 0) == 0
    assert done(1.0, 1, 0) == 1
    # Never move the counter backwards.
    assert done(0.1, 10, 3) == 3
