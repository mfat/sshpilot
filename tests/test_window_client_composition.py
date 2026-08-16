"""M1: window composition attaches client-backed frontend services once.

Every client-backed service (connection presentation stores + the key manager)
is attached through a single helper, for both an already-selected client and a
newly completed asynchronous selection. With no client there is no key manager
and no local fallback.
"""

import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("gi")

from sshpilot import window as window_module  # noqa: E402
from sshpilot.api.models.keys import KeyStoreScope  # noqa: E402
from sshpilot.api.models.daemon import OperationMode  # noqa: E402
from sshpilot.key_manager import KeyManager  # noqa: E402


def _make_window():
    window = window_module.MainWindow.__new__(window_module.MainWindow)
    window._key_scope = KeyStoreScope.DEFAULT
    window._confirmed_operation_mode = OperationMode.DEFAULT
    window.client = None
    window.client_bridge = None
    window.key_manager = None
    window.connection_manager = MagicMock()
    window.connection_runtime_status = MagicMock()
    window.plugin_connection_services = MagicMock()
    window.group_manager = MagicMock()
    window._group_mutation_controller = None
    return window


# ---------------------------------------------------------------------------
# _attach_client_backed_services
# ---------------------------------------------------------------------------
def test_client_present_attaches_stores_and_builds_key_manager():
    window = _make_window()
    window.client = object()

    window._attach_client_backed_services()

    window.connection_manager.attach_client.assert_called_once_with(window.client)
    window.connection_runtime_status.attach_client.assert_called_once_with(window.client)
    window.plugin_connection_services.attach_client.assert_called_once_with(window.client)
    assert isinstance(window.key_manager, KeyManager)
    assert window.key_manager._scope is KeyStoreScope.DEFAULT


def test_unconfirmed_mode_does_not_construct_default_key_manager():
    window = _make_window()
    window._confirmed_operation_mode = None
    window.client = object()

    window._attach_client_backed_services()

    assert window.key_manager is None


def test_confirmed_mode_updates_scope_key_manager_and_config_cache():
    window = _make_window()
    window.client = object()
    window.config = types.SimpleNamespace(
        config_data={"config_version": 3, "ssh": {"use_isolated_config": False}}
    )

    window._apply_confirmed_operation_mode(OperationMode.ISOLATED)

    assert window._confirmed_operation_mode is OperationMode.ISOLATED
    assert window._key_scope is KeyStoreScope.ISOLATED
    assert window.key_manager._scope is KeyStoreScope.ISOLATED
    assert window.config.config_data["ssh"]["use_isolated_config"] is True

    def set_setting(key, value):
        target = window.config.config_data
        parts = key.split(".")
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value

    window.config.set_setting = set_setting
    window.config.set_setting("terminal.theme", "light")
    assert window.config.config_data["ssh"]["use_isolated_config"] is True


def test_delayed_mode_confirmation_keeps_key_actions_unavailable():
    window = _make_window()
    window._confirmed_operation_mode = None
    window.key_manager = None
    window.config = types.SimpleNamespace(
        config_data={"ssh": {"use_isolated_config": False}}
    )

    class DelayedClient:
        def get_capabilities(self):
            return types.SimpleNamespace(supports=lambda _capability: True)

        def get_operation_mode(self):
            raise AssertionError("the test bridge owns completion")

    class DelayedBridge:
        def __init__(self):
            self.success = None
            self.error = None

        def submit(self, _operation, *, on_success, on_error):
            self.success = on_success
            self.error = on_error

    bridge = DelayedBridge()
    window.client = DelayedClient()
    window.client_bridge = bridge

    window._refresh_operation_mode_scope()

    assert window.key_manager is None
    assert window._confirmed_operation_mode is None
    assert bridge.success is not None

    bridge.success(
        types.SimpleNamespace(active_mode=OperationMode.ISOLATED)
    )

    assert window._confirmed_operation_mode is OperationMode.ISOLATED
    assert window._key_scope is KeyStoreScope.ISOLATED
    assert window.key_manager._scope is KeyStoreScope.ISOLATED


def test_isolated_scope_builds_isolated_key_manager():
    window = _make_window()
    window._key_scope = KeyStoreScope.ISOLATED
    window.client = object()

    window._attach_client_backed_services()

    assert window.key_manager._scope is KeyStoreScope.ISOLATED


def test_no_client_leaves_key_manager_none_and_attaches_nothing():
    window = _make_window()
    window.client = None

    window._attach_client_backed_services()

    assert window.key_manager is None
    window.connection_manager.attach_client.assert_not_called()
    window.connection_runtime_status.attach_client.assert_not_called()
    window.plugin_connection_services.attach_client.assert_not_called()


def test_detached_client_never_creates_fallback_manager():
    """A failed/absent daemon must not produce a local key manager."""
    window = _make_window()
    window.client = None
    window._attach_client_backed_services()
    assert window.key_manager is None
    # Re-attaching later with a real client replaces the None state.
    window.client = object()
    window._attach_client_backed_services()
    assert isinstance(window.key_manager, KeyManager)


# ---------------------------------------------------------------------------
# _compose_api_client (already-selected client)
# ---------------------------------------------------------------------------
def test_compose_api_client_reused_selection_does_not_attach():
    """Composition only stores the client; attachment is the caller's job."""
    window = _make_window()
    app = types.SimpleNamespace(
        _api_client_selection=types.SimpleNamespace(client=object()),
        _api_client_bridge=object(),
    )
    attached = []
    window._attach_client_backed_services = lambda: attached.append(True)

    window._compose_api_client(app)

    assert window.client is app._api_client_selection.client
    assert window.client_bridge is app._api_client_bridge
    assert attached == []
    assert window.key_manager is None


def test_init_sequence_attaches_exactly_once_for_reused_selection():
    """The ``__init__`` sequence (compose, then attach-when-present) attaches once."""
    window = _make_window()
    app = types.SimpleNamespace(
        _api_client_selection=types.SimpleNamespace(client=object()),
        _api_client_bridge=MagicMock(),
    )

    window._compose_api_client(app)
    if window.client is not None:
        window._attach_client_backed_services()

    window.connection_manager.attach_client.assert_called_once_with(window.client)
    window.connection_runtime_status.attach_client.assert_called_once_with(window.client)
    window.plugin_connection_services.attach_client.assert_called_once_with(window.client)
    assert isinstance(window.key_manager, KeyManager)
    assert window.key_manager._scope is KeyStoreScope.DEFAULT


def test_failed_selection_leaves_key_manager_none():
    """A failed selection clears any previously attached key manager."""
    window = _make_window()
    window.client = object()
    window.key_manager = KeyManager(window.client, KeyStoreScope.DEFAULT)
    window._api_client_selection_pending = True
    window._api_client_selection_request = object()
    window.plugin_connection_services.detach_client = MagicMock()
    window.connection_runtime_status.close = MagicMock()
    window._show_client_mode_warning = lambda: None

    window._handle_client_selection_error(Exception("boom"))

    assert window.key_manager is None
    assert window.client is None
    window.plugin_connection_services.detach_client.assert_called_once_with()


# ---------------------------------------------------------------------------
# _apply_client_selection (newly completed async selection)
# ---------------------------------------------------------------------------
def test_apply_client_selection_uses_the_same_helper():
    window = _make_window()
    window._is_quitting = False
    window._api_client_selection_pending = True
    window._api_client_selection_request = object()
    window.get_application = lambda: None
    window._maybe_restore_daemon_sessions = lambda: None
    attached = []
    window._attach_client_backed_services = lambda: attached.append(True)
    controller = object()
    preferences = types.SimpleNamespace(
        set_ssh_overrides_controller=MagicMock()
    )
    window._preferences_window = preferences
    window._build_ssh_overrides_controller = lambda: controller

    selection = types.SimpleNamespace(client=object())
    window._apply_client_selection(selection)

    assert attached == [True]
    assert window.client is selection.client
    assert window._api_client_selection_pending is False
    assert window._api_client_selection_request is None
    preferences.set_ssh_overrides_controller.assert_called_once_with(controller)


def test_apply_client_selection_quitting_does_not_attach():
    window = _make_window()
    window._is_quitting = True
    attached = []
    window._attach_client_backed_services = lambda: attached.append(True)
    client = types.SimpleNamespace(close=MagicMock())

    selection = types.SimpleNamespace(client=client)
    window._apply_client_selection(selection)

    assert attached == []
    client.close.assert_called_once_with()
