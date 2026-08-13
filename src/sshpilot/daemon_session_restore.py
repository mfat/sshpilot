"""Safe restore metadata for daemon SSH sessions across GTK restarts."""

from __future__ import annotations

from .api.connection_identity import connection_id_for
import logging
import time
from dataclasses import asdict, dataclass
from typing import List

from .api.models.common import ConnectionId, SessionId
from .api.models.sessions import SessionState
from .daemon_terminal_policy import (
    RESTORE_SESSIONS_SETTING,
    SESSION_RESTORE_STATE_SETTING,
)

logger = logging.getLogger(__name__)


@dataclass
class SessionRestoreMetadata:
    session_id: str
    daemon_instance_id: str
    connection_id: str
    last_sequence: int
    tab_title: str
    view_id: str
    timestamp: float


class DaemonSessionRestoreManager:
    """Persist only safe restoration identifiers — never output or secrets."""

    def __init__(self, config):
        self.config = config

    def save_session_metadata(self, tab_state, tab_title: str = "Terminal") -> None:
        try:
            if tab_state is None or tab_state.session_id is None:
                return
            restore_state = self.config.get_setting(SESSION_RESTORE_STATE_SETTING, [])
            if not isinstance(restore_state, list):
                restore_state = []
            metadata = SessionRestoreMetadata(
                session_id=str(tab_state.session_id),
                daemon_instance_id=str(tab_state.daemon_instance_id or ""),
                connection_id=str(tab_state.connection_id),
                last_sequence=int(tab_state.expected_sequence or 0),
                tab_title=tab_title or "Terminal",
                view_id=str(tab_state.view_id),
                timestamp=time.time(),
            )
            restore_state = [
                entry
                for entry in restore_state
                if entry.get("session_id") != metadata.session_id
            ]
            restore_state.append(asdict(metadata))
            self.config.set_setting(
                SESSION_RESTORE_STATE_SETTING,
                restore_state[-50:],
            )
        except Exception:
            logger.error("Failed to save daemon session restore metadata", exc_info=True)

    def get_restorable_sessions(
        self,
        client,
        sessions=None,
    ) -> List[SessionRestoreMetadata]:
        try:
            if not self.config.get_setting(RESTORE_SESSIONS_SETTING, True):
                return []
            restore_state = self.config.get_setting(SESSION_RESTORE_STATE_SETTING, [])
            if not isinstance(restore_state, list):
                return []
            current_instance_id = getattr(client, "server_instance_id", "") or ""
            if not current_instance_id:
                return []
            try:
                if sessions is None:
                    active_sessions = list(client.list_sessions() or [])
                else:
                    active_sessions = list(sessions)
            except Exception:
                logger.warning("Failed to list daemon sessions for restore")
                return []
            active_by_id = {
                str(session.id): session
                for session in active_sessions
                if getattr(session, "state", None)
                in {SessionState.RUNNING, SessionState.STARTING, SessionState.CREATED}
            }
            restorable = []
            for entry in restore_state:
                try:
                    metadata = SessionRestoreMetadata(**entry)
                except Exception:
                    continue
                if metadata.daemon_instance_id != current_instance_id:
                    continue
                if metadata.session_id not in active_by_id:
                    continue
                restorable.append(metadata)
            return restorable
        except Exception:
            logger.error("Failed to resolve restorable daemon sessions", exc_info=True)
            return []

    def remove_session_metadata(self, session_id: str) -> None:
        try:
            restore_state = self.config.get_setting(SESSION_RESTORE_STATE_SETTING, [])
            if not isinstance(restore_state, list):
                return
            self.config.set_setting(
                SESSION_RESTORE_STATE_SETTING,
                [
                    entry
                    for entry in restore_state
                    if entry.get("session_id") != session_id
                ],
            )
        except Exception:
            logger.error("Failed to remove daemon session restore metadata", exc_info=True)

    def restore_session(self, window, metadata: SessionRestoreMetadata, client, bridge) -> bool:
        """Create a tab and attach to a retained live daemon session."""

        try:
            from .terminal import TerminalWidget
            from sshpilot import icon_utils

            connection = None
            for candidate in window.connection_manager.get_connections():
                try:
                    if (
                        connection_id_for(candidate)
                        == ConnectionId(metadata.connection_id)
                    ):
                        connection = candidate
                        break
                except Exception:
                    continue
            if connection is None:
                logger.info("Skipping restore; connection no longer exists")
                return False

            manager = window.terminal_manager
            group_color, group_name = manager._resolve_group_color_and_name(connection)
            terminal = TerminalWidget(
                connection,
                window.config,
                window.connection_manager,
                group_color=group_color,
            )
            terminal.connect("connection-established", manager.on_terminal_connected)
            terminal.connect("connection-lost", manager.on_terminal_disconnected)
            terminal.connect("title-changed", manager.on_terminal_title_changed)
            if group_name:
                setattr(terminal, "group_name", group_name)
            window.connection_to_terminals.setdefault(connection, []).append(terminal)
            window.terminal_to_connection[terminal] = connection
            window.active_terminals[connection] = terminal
            page = window.tab_view.append(terminal)
            page.set_title(metadata.tab_title or connection.nickname)
            page.set_icon(
                icon_utils.new_gicon_from_icon_name("utilities-terminal-symbolic")
            )
            window.show_tab_view()
            terminal.apply_theme()
            attached = terminal.attach_daemon_session(
                client,
                bridge,
                SessionId(metadata.session_id),
                connection_id=ConnectionId(metadata.connection_id),
                # This is a fresh widget, so the previous widget's sequence
                # cannot be used as its replay cursor. Request the daemon's
                # retained scrollback from the beginning; the daemon clamps
                # the request to the oldest retained sequence.
                from_sequence=0,
            )
            if not attached:
                self.remove_session_metadata(metadata.session_id)
            return bool(attached)
        except Exception:
            logger.error("Failed to restore daemon session tab", exc_info=True)
            return False
