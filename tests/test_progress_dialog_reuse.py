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


def test_is_reusable_false_when_not_visible():
    assert _bare_dialog(visible=False).is_reusable() is False


def test_complete_delete_progress_reports_partial_failures(load_file_manager_window):
    window_module = load_file_manager_window()
    window = window_module.FileManagerWindow.__new__(window_module.FileManagerWindow)
    dialog = _bare_dialog(operation_type="delete", total_files=3)
    window._progress_dialog = dialog
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
    window._progress_dialog = dialog
    future = Future()
    future.set_result(([], 1))
    window._complete_delete_progress(future)
    assert dialog._completions == []
