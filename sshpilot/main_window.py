"""
Main window module for sshPilot.

Provides the top-level window that wires together core managers and UI
components. The heavy UI code from the old monolithic implementation has been
trimmed so this module focuses on orchestration.
"""

import logging
from typing import Dict, List

from gi.repository import Adw, Gio, Gtk

from .connection_manager import ConnectionManager, Connection
from .terminal import TerminalWidget
from .terminal_manager import TerminalManager
from .config import Config
from .key_manager import KeyManager
from .groups import GroupManager
from .sidebar import build_sidebar
from .actions import WindowActions, register_window_actions
from . import shutdown

logger = logging.getLogger(__name__)


class MainWindow(Adw.ApplicationWindow, WindowActions):
    """Main application window.

    This lightweight version orchestrates high level managers and wires
    basic signals. Detailed UI construction now lives in dedicated
    modules.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Core managers
        self.connection_manager = ConnectionManager()
        self.config = Config()
        self.key_manager = KeyManager()
        self.group_manager = GroupManager(self.config)

        # Terminal bookkeeping
        self.active_terminals: Dict[Connection, TerminalWidget] = {}
        self.connection_to_terminals: Dict[Connection, List[TerminalWidget]] = {}
        self.terminal_to_connection: Dict[TerminalWidget, Connection] = {}
        self.connection_rows = {}

        # Basic UI
        self.connection_list = Gtk.ListBox()
        build_sidebar(self)
        self.terminal_manager = TerminalManager(self)

        # Actions and signals
        self.activate_action = Gio.SimpleAction.new('activate-connection', None)
        self.activate_action.connect('activate', self.on_activate_connection)
        self.add_action(self.activate_action)
        register_window_actions(self)
        self.connect('close-request', self.on_close_request)

        logger.info("Main window initialized")

    # -- Signal handlers -------------------------------------------------

    def on_activate_connection(self, action, param):
        """Placeholder for connection activation logic."""
        pass

    def on_close_request(self, *args):
        """Gracefully shut down when the window is closed."""
        shutdown.cleanup_and_quit(self)
        return True

    # -- Shutdown helpers ------------------------------------------------

    def _do_quit(self):
        """Actually quit the application. Called by shutdown helpers."""
        app = self.get_application()
        if app:
            app.quit()
        else:
            self.close()
        return False

