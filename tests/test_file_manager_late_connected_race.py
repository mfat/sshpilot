"""Regression tests for file manager teardown race conditions.

Tests:
1. Late 'connected' signal emitted after FileManagerWindow teardown does not crash
   with AttributeError: 'NoneType' object has no attribute 'listdir'.
2. Signal handlers are disconnected from backend manager on _cleanup_manager.
3. Placeholder tab closed before creation timeout expires aborts embedded controller creation
   for both _open_manage_files_now_for_connection and _open_file_manager_with_picker.
4. _placeholder_is_open fails closed (returns False) on exceptions or model errors.
"""

import sys
import types
from unittest.mock import MagicMock


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


def test_late_connected_event_after_file_manager_teardown(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.file_manager_window import FileManagerWindow

    # Instantiate lightweight window
    win = FileManagerWindow.__new__(FileManagerWindow)
    win._pending_paths = {"right_pane": "/remote/path"}
    win._right_pane = MagicMock()
    win._password_retry_count = 0
    win._password_dialog_shown = False
    win._show_progress = MagicMock()

    # Mock manager
    mock_manager = MagicMock()
    win._manager = mock_manager

    # Simulate teardown: _cleanup_manager sets _is_disposed to True
    win._cleanup_manager()
    assert win._is_disposed is True

    # Emit late _on_connected callback
    try:
        win._on_connected()
    except Exception as exc:
        raise AssertionError(f"_on_connected raised exception after teardown: {exc}") from exc

    # Assert listdir was never called on the disposed manager
    mock_manager.listdir.assert_not_called()


def test_signals_disconnected_on_cleanup_manager(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.file_manager_window import FileManagerWindow

    win = FileManagerWindow.__new__(FileManagerWindow)
    win._clear_progress_toast = MagicMock()

    mock_manager = MagicMock()
    disconnected_handlers = []
    mock_manager.disconnect = lambda handler_id: disconnected_handlers.append(handler_id)

    win._manager = mock_manager
    win._manager_signal_handlers = [
        ("connected", 101),
        ("connection-error", 102),
        ("directory-loaded", 103),
    ]

    win._cleanup_manager()

    assert win._is_disposed is True
    assert win._manager_signal_handlers == []
    assert disconnected_handlers == [101, 102, 103]
    mock_manager.close.assert_called_once()


def test_placeholder_closed_before_creation_aborts(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.window_file_manager import WindowFileManagerMixin

    mixin = WindowFileManagerMixin()
    mixin.config = MagicMock()
    mixin.config.get_setting.return_value = False
    mixin.connection_manager = MagicMock()
    mixin._show_manage_files_error = MagicMock()

    # Use distinct objects so identity/equality differs
    page_in_tabview = object()
    other_page = object()

    mixin.tab_view = MagicMock()
    mixin.tab_view.get_pages.return_value = [other_page]

    created = []
    monkeypatch.setattr(
        "sshpilot.window_file_manager.create_internal_file_manager_tab",
        lambda **kwargs: (created.append(kwargs) or (MagicMock(), MagicMock())),
    )

    placeholder_info = {'page': page_in_tabview, 'container': MagicMock()}

    # Call open manage files logic
    mixin._create_file_manager_placeholder_tab = lambda *args: placeholder_info

    def capture_timeout(delay, callback):
        # Run callback immediately to test logic
        result = callback()
        assert result is False
        return 1

    monkeypatch.setattr("gi.repository.GLib.timeout_add", capture_timeout)
    monkeypatch.setattr("sshpilot.window_file_manager.has_internal_file_manager", lambda: True)

    mixin._open_manage_files_now_for_connection(MagicMock())

    # Verify controller was NOT created because placeholder tab was missing from tab_view
    assert created == []


def test_picker_placeholder_closed_before_creation_aborts(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.window_file_manager import WindowFileManagerMixin

    mixin = WindowFileManagerMixin()
    mixin.config = MagicMock()
    mixin.config.get_setting.return_value = False
    mixin.connection_manager = MagicMock()
    mixin._show_manage_files_error = MagicMock()

    page_in_tabview = object()
    other_page = object()

    mixin.tab_view = MagicMock()
    mixin.tab_view.get_pages.return_value = [other_page]

    created = []
    monkeypatch.setattr(
        "sshpilot.window_file_manager.create_internal_file_manager_tab",
        lambda **kwargs: (created.append(kwargs) or (MagicMock(), MagicMock())),
    )

    placeholder_info = {'page': page_in_tabview, 'container': MagicMock()}
    mixin._create_file_manager_placeholder_tab = lambda *args: placeholder_info

    def capture_timeout(delay, callback):
        result = callback()
        assert result is False
        return 1

    monkeypatch.setattr("gi.repository.GLib.timeout_add", capture_timeout)

    mixin._open_file_manager_with_picker()

    # Verify host picker controller was NOT created because placeholder tab was missing
    assert created == []


def test_placeholder_is_open_fails_closed_on_exception():
    _ensure_cairo_stub()
    from sshpilot.window_file_manager import WindowFileManagerMixin

    mixin = WindowFileManagerMixin()
    mixin.tab_view = MagicMock()
    mixin.tab_view.get_pages.side_effect = RuntimeError("GTK TabView destroyed")

    placeholder_info = {'page': object()}

    # Verification failure must fail closed (return False) instead of allowing creation
    assert mixin._placeholder_is_open(placeholder_info) is False
    assert mixin._placeholder_is_open(None) is False


def test_signals_disconnected_on_teardown_backend(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.file_manager_window import FileManagerWindow

    win = FileManagerWindow.__new__(FileManagerWindow)
    win._connection_error_reported = False

    mock_manager = MagicMock()
    disconnected_handlers = []
    mock_manager.disconnect = lambda handler_id: disconnected_handlers.append(handler_id)

    win._manager = mock_manager
    win._manager_signal_handlers = [
        ("connected", 201),
        ("connection-error", 202),
        ("directory-loaded", 203),
    ]

    win._teardown_backend()

    assert win._manager is None
    assert win._manager_signal_handlers == []
    assert disconnected_handlers == [201, 202, 203]
    mock_manager.close.assert_called_once()


def test_stale_manager_events_ignored(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot.file_manager_window import FileManagerWindow

    win = FileManagerWindow.__new__(FileManagerWindow)
    win._is_disposed = False
    win._pending_paths = {}

    current_manager = MagicMock()
    stale_manager = MagicMock()

    win._manager = current_manager

    # _on_connected with stale_manager should be ignored
    win._show_progress = MagicMock()
    win._on_connected(stale_manager)
    win._show_progress.assert_not_called()

    # _on_directory_loaded with stale_manager should be ignored
    win._right_pane = MagicMock()
    win._left_pane = MagicMock()
    win._on_directory_loaded(stale_manager, "/path", [])
    win._right_pane.show_entries.assert_not_called()

    # _on_progress with stale_manager should be ignored
    win._on_progress(stale_manager, 0.5, "loading")
    win._show_progress.assert_not_called()

    # _on_connection_error with stale_manager should be ignored
    win._clear_progress_toast = MagicMock()
    win._on_connection_error(stale_manager, "error")
    win._clear_progress_toast.assert_not_called()
