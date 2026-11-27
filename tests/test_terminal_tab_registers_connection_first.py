"""A new terminal tab knows its connection before the tab view selects it.

The first tab replaces the Start tab, so Adw.TabView selects it during
``append``. The tab-selected handler highlights the sidebar row from
``terminal_to_connection``; registered after ``append``, the handler saw no
connection and cleared the highlight (e.g. a connection opened from search).
"""

from unittest.mock import Mock, patch

from sshpilot import terminal_manager
from sshpilot.terminal_manager import TerminalManager


def test_connection_is_registered_when_the_tab_is_appended():
    window = Mock()
    window.connection_to_terminals = {}
    window.terminal_to_connection = {}
    window.active_terminals = {}
    seen = {}

    def append(terminal):
        seen["connection"] = window.terminal_to_connection.get(terminal)
        seen["active"] = window.active_terminals.get(connection)
        return Mock()

    window.tab_view.append.side_effect = append
    connection = Mock(nickname="ci-runner", display_name=None)
    manager = TerminalManager(window)

    with patch.object(terminal_manager, "TerminalWidget") as widget, \
            patch.object(manager, "_resolve_group_color_and_name", return_value=(None, None)), \
            patch.object(manager, "_apply_tab_group_color"), \
            patch("sshpilot.forwarding_only_ui.connection_forwarding_only", return_value=False):
        terminal, _page = manager._create_internal_terminal_tab(connection)

    assert terminal is widget.return_value
    assert seen == {"connection": connection, "active": terminal}
    assert window.connection_to_terminals[connection] == [terminal]
