"""Terminal copy notifications follow backend completion, not selection guesses."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from sshpilot.terminal import TerminalWidget


class _DeferredCopyBackend:
    def __init__(self):
        self.completion = None
        self.format = None

    def copy_clipboard(self, *, format="text", on_complete=None):
        self.format = format
        self.completion = on_complete


def _widget(backend):
    return SimpleNamespace(backend=backend, _show_toast=lambda message: None)


def test_copy_toast_waits_for_successful_backend_completion():
    backend = _DeferredCopyBackend()
    toasts = []
    widget = _widget(backend)
    widget._show_toast = toasts.append

    TerminalWidget.copy_text(widget)
    assert toasts == []

    backend.completion(True)
    assert toasts == ["Copied to clipboard"]


def test_failed_or_empty_copy_reports_the_failure_without_claiming_success():
    """A copy that wrote nothing must say so -- silence read as a dead clipboard.

    See issue #1178: every failing attempt ended at
    ``copied=False reason=empty-selection`` with no UI trace at all.
    """
    backend = _DeferredCopyBackend()
    toasts = []
    widget = _widget(backend)
    widget._show_toast = toasts.append

    TerminalWidget.copy_text(widget)
    backend.completion(False)

    assert toasts == ["Nothing selected to copy"]


def test_html_copy_is_forwarded_to_backend():
    backend = _DeferredCopyBackend()
    widget = _widget(backend)

    TerminalWidget.copy_text(widget, format="html")

    assert backend.format == "html"


def test_backend_owned_shortcut_uses_standard_notifications():
    toasts = []
    widget = SimpleNamespace(_show_toast=toasts.append)

    TerminalWidget.handle_backend_copy_result(widget, True)
    TerminalWidget.handle_backend_copy_result(widget, False)

    assert toasts == ["Copied to clipboard", "Nothing selected to copy"]


def test_copy_on_select_stays_silent_and_uses_backend_contract():
    copies = []
    backend = SimpleNamespace(
        get_has_selection=lambda: True,
        copy_clipboard=lambda: copies.append(True),
    )
    widget = SimpleNamespace(
        backend=backend,
        config=SimpleNamespace(get_setting=lambda _key, _default: True),
    )

    TerminalWidget._on_selection_changed(widget)

    assert copies == [True]


def test_paste_entry_point_delegates_to_active_backend():
    pastes = []
    widget = SimpleNamespace(
        backend=SimpleNamespace(paste_clipboard=lambda: pastes.append(True)))

    TerminalWidget.paste_text(widget)

    assert pastes == [True]


def test_copy_link_reports_success_only_after_clipboard_write():
    clipboard = MagicMock()
    toasts = []
    widget = SimpleNamespace(
        _context_menu_hyperlink_uri="https://example.test",
        get_clipboard=lambda: clipboard,
        _show_toast=toasts.append,
    )

    TerminalWidget._on_copy_link_activated(widget, None, None)

    clipboard.set.assert_called_once_with("https://example.test")
    assert toasts == ["Copied to clipboard"]
