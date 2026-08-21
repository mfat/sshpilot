"""Focused clipboard-result contracts for the VTE backend."""

from unittest.mock import MagicMock

from sshpilot.terminal_backends import VTETerminalBackend, Vte


def _backend(selected):
    backend = object.__new__(VTETerminalBackend)
    backend.vte = MagicMock()
    backend.vte.get_text_selected.return_value = selected
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
