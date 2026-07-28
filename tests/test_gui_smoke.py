"""Smoke test: the real app boots under the GUI harness and a window comes up."""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def test_app_boots_and_window_present(gui):
    assert gui.window is not None
    # The pinned Start tab means at least one page exists on a fresh window.
    assert gui.window.tab_view.get_n_pages() >= 1
    # No stray confirmation dialogs on a clean boot.
    assert gui.message_dialogs() == []


def test_open_local_tabs(gui):
    gui.open_local_tabs(2)
    assert len(gui.user_pages()) == 2


def test_real_window_composes_welcome_page_with_in_process_client(gui):
    from sshpilot.api import InProcessClient

    window = gui.window
    client = window.client
    welcome = window.welcome_view
    client_calls = []
    manager_calls = []
    original_client_list = client.list_connections
    original_manager_list = window.connection_manager.get_connections

    def list_through_client():
        client_calls.append(True)
        return []

    def direct_manager_read():
        manager_calls.append(True)
        raise AssertionError("WelcomePage bypassed SshPilotClient")

    client.list_connections = list_through_client
    window.connection_manager.get_connections = direct_manager_read
    try:
        welcome._populate_recent_box()
        gui.pump(50)
    finally:
        client.list_connections = original_client_list
        window.connection_manager.get_connections = original_manager_list

    assert isinstance(client, InProcessClient)
    assert welcome.client is client
    assert client_calls == [True]
    assert manager_calls == []
