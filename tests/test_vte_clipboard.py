"""Focused clipboard-result contracts for the VTE backend."""

import logging
from unittest.mock import MagicMock

from sshpilot.terminal_backends import VTETerminalBackend, Vte


def _backend(selected, has_selection=None):
    backend = object.__new__(VTETerminalBackend)
    backend.vte = MagicMock()
    backend.vte.get_text_selected.return_value = selected
    backend.vte.get_has_selection.return_value = (
        bool(selected) if has_selection is None else has_selection
    )
    return backend


def test_vte_copy_rejects_empty_selection_without_clipboard_write():
    backend = _backend("")
    completed = []

    backend.copy_clipboard(on_complete=completed.append)

    backend.vte.copy_clipboard_format.assert_not_called()
    assert completed == [False]


def test_vte_text_copy_reports_success_after_write():
    backend = _backend("selected")
    completed = []

    backend.copy_clipboard(on_complete=completed.append)

    backend.vte.get_text_selected.assert_called_once_with(Vte.Format.TEXT)
    backend.vte.copy_clipboard_format.assert_called_once_with(Vte.Format.TEXT)
    assert completed == [True]


def test_vte_html_copy_uses_html_selection_and_format():
    backend = _backend("<pre>selected</pre>")
    completed = []

    backend.copy_clipboard(format="html", on_complete=completed.append)

    backend.vte.get_text_selected.assert_called_once_with(Vte.Format.HTML)
    backend.vte.copy_clipboard_format.assert_called_once_with(Vte.Format.HTML)
    assert completed == [True]


def test_vte_copy_failure_never_reports_success():
    backend = _backend("selected")
    backend.vte.copy_clipboard_format.side_effect = RuntimeError("clipboard gone")
    completed = []

    backend.copy_clipboard(on_complete=completed.append)

    assert completed == [False]


def test_vte_copy_logs_empty_selection_without_payload(caplog):
    backend = _backend("secret-clipboard-payload", has_selection=True)
    backend.vte.get_text_selected.return_value = ""
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend.copy_clipboard(on_complete=lambda _copied: None)

    assert "reason=empty-selection-flag" in caplog.text
    assert "has_selection=True" in caplog.text
    assert "secret-clipboard-payload" not in caplog.text


def test_vte_copy_logs_write_length_not_text(caplog):
    backend = _backend("secret-clipboard-payload")
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend.copy_clipboard(on_complete=lambda _copied: None)

    assert "reason=written" in caplog.text
    assert "text_len=24" in caplog.text
    assert "secret-clipboard-payload" not in caplog.text


def test_vte_copy_logs_exception_reason(caplog):
    backend = _backend("selected")
    backend.vte.copy_clipboard_format.side_effect = RuntimeError("clipboard gone")
    with caplog.at_level(logging.DEBUG, logger="sshpilot.terminal_backends"):
        backend.copy_clipboard(on_complete=lambda _copied: None)

    assert "reason=exception" in caplog.text
    assert "copied=False" in caplog.text
