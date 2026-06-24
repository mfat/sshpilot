"""Regression tests for SFTP progress dialog reuse detection."""

from sshpilot.file_manager.progress_dialog import SFTPProgressDialog


def _bare_dialog(**overrides):
    dlg = SFTPProgressDialog.__new__(SFTPProgressDialog)
    dlg.is_cancelled = overrides.get("is_cancelled", False)
    dlg._closed = overrides.get("_closed", False)
    dlg._completion_shown = overrides.get("_completion_shown", False)
    visible = overrides.get("visible", True)
    dlg.get_visible = lambda: visible
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
