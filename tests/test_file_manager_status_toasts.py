"""Status toasts that embed a long filename stay toasts, never an "Error" (#1438)."""

from __future__ import annotations

import pathlib
import sys
import types
from unittest.mock import MagicMock

from sshpilot.file_manager.common import FileEntry

LONG_NAME = "20261128_vmware_version_backup_full_right_before_upgrade_of_host.zip"


def _pane_module():
    sys.modules.setdefault("cairo", types.SimpleNamespace())
    from sshpilot.file_manager import pane as pane_module

    return pane_module


def _pane(pane_module, monkeypatch):
    pane = pane_module.FilePane.__new__(pane_module.FilePane)
    pane._current_toast = None
    pane._overlay = MagicMock()
    alerts = []
    monkeypatch.setattr(
        pane_module, "present_error_alert", lambda anchor, text: alerts.append(text)
    )
    return pane, alerts


def test_download_of_a_long_filename_shows_a_toast_not_an_error(monkeypatch, tmp_path):
    pane_module = _pane_module()
    pane, alerts = _pane(pane_module, monkeypatch)
    pane._current_path = "/remote"
    pane.emit = MagicMock()
    entry = FileEntry(name=LONG_NAME, is_dir=False, size=1, modified=0)

    pane._emit_download([entry], str(tmp_path))

    assert alerts == []
    pane._overlay.add_toast.assert_called_once()
    assert pane_module.toast_overflows(f"Downloading {LONG_NAME}…")


def test_long_error_still_escalates_to_an_alert(monkeypatch):
    pane_module = _pane_module()
    pane, alerts = _pane(pane_module, monkeypatch)
    message = f"Download failed: permission denied while writing {LONG_NAME}"

    pane.show_toast(message)

    assert alerts == [message]
    pane._overlay.add_toast.assert_not_called()


def test_every_status_toast_with_a_name_is_marked_informational():
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "sshpilot"
    sources = (root / "file_manager" / "pane.py").read_text() + (
        root / "file_manager_window.py"
    ).read_text()
    for template in (
        '"Downloading {name}…"',
        '"Uploading {name}…"',
        '"Renamed to {name}"',
        '"Renaming to {name}…"',
        '"Skipped existing file: {filename}"',
        '"Skipping inaccessible items: {name}"',
        '"Access granted to: {path}"',
    ):
        for line in sources.splitlines():
            if template in line and "show_toast(" in line:
                assert "informational=True" in line, line
