from __future__ import annotations

import argparse
import logging
import shlex
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import DataTable, Footer, Header, Input, Static

from sshpilot.config import Config
from sshpilot.connection_manager import ConnectionManager
from sshpilot.tui.command_builder import build_ssh_command
from sshpilot.tui.editor import ConnectionEditSession

LOG = logging.getLogger(__name__)


class ConnectionTable(DataTable):
    """Connection list that emits a message when Enter is pressed."""

    BINDINGS = [Binding("enter", "connect_row", "Connect", show=False)]

    class ConnectRequested(Message):
        """Sent when the user activates the current row."""

        def __init__(self, sender: "ConnectionTable"):
            super().__init__(sender)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = True

    def action_connect_row(self) -> None:
        self.post_message(self.ConnectRequested(self))


class HelpScreen(ModalScreen[None]):
    """Modal overlay listing keyboard shortcuts."""

    def compose(self) -> ComposeResult:
        lines = [
            "[b]sshPilot Textual TUI[/b]",
            "",
            "Navigation:",
            "  ↑/↓ or j/k  Move selection",
            "  PgUp/PgDn    Scroll a page",
            "  Home/End     Jump to start or end",
            "",
            "Actions:",
            "  Enter / c    Connect to highlighted host",
            "  e            Edit connection",
            "  r / F5       Reload SSH config",
            "  / or Ctrl+F  Focus the filter",
            "  Esc          Return focus to the list",
            "  q or Ctrl+C  Quit",
            "",
            "Press Esc, q, or ? to close this help.",
        ]
        text = "\n".join(lines)
        yield Static(text, id="help-panel")

    def on_key(self, event: events.Key) -> None:
        if event.key in {"escape", "q", "?"}:
            event.stop()
            self.dismiss()


class DetailsPanel(Static):
    """Shows information about the selected connection."""

    def show_empty(self, message: str = "Select a connection to see details.") -> None:
        self.update(message)

    def show_connection(self, connection) -> None:
        if not connection:
            self.show_empty()
            return

        nickname = getattr(connection, "nickname", "") or "(unnamed)"
        username = getattr(connection, "username", "") or "-"
        host = self._effective_host(connection) or "?"
        port = getattr(connection, "port", 22)
        source = getattr(connection, "source", "") or "ssh config"
        identity = getattr(connection, "keyfile", "") or "default"
        local_cmd = self._extract(connection, "local_command")
        remote_cmd = self._extract(connection, "remote_command")
        rules = [rule for rule in (getattr(connection, "forwarding_rules", []) or []) if rule.get("enabled", True)]

        lines = [
            f"[b]Nickname[/b]  {nickname}",
            f"[b]Target[/b]    {host} (user: {username})",
            f"[b]Port[/b]      {port}",
            f"[b]Identity[/b]  {identity}",
            f"[b]Source[/b]    {source}",
        ]
        if local_cmd:
            lines.append(f"[b]Local cmd[/b] {local_cmd}")
        if remote_cmd:
            lines.append(f"[b]Remote cmd[/b] {remote_cmd}")
        if rules:
            lines.append(f"[b]Forwarding ({len(rules)})[/b]")
            for rule in rules:
                lines.append(f"  • {self._summarize_rule(rule)}")

        self.update("\n".join(lines))

    @staticmethod
    def _extract(connection, field: str) -> str:
        value = getattr(connection, field, "")
        if isinstance(value, str) and value.strip():
            return value.strip()
        data = getattr(connection, "data", None)
        if isinstance(data, dict):
            candidate = data.get(field)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @staticmethod
    def _effective_host(connection) -> str:
        getter = getattr(connection, "get_effective_host", None)
        if callable(getter):
            try:
                host = getter()
                if host:
                    return host
            except Exception:
                LOG.debug("get_effective_host failed for %s", connection, exc_info=True)
        return getattr(connection, "hostname", "") or getattr(connection, "host", "")

    @staticmethod
    def _summarize_rule(rule: Dict[str, object]) -> str:
        rtype = rule.get("type", "local")
        listen = f"{rule.get('listen_addr', 'localhost')}:{rule.get('listen_port')}"
        if rtype == "dynamic":
            return f"Dynamic SOCKS on {listen}"
        target_host = rule.get("remote_host") or rule.get("local_host") or "localhost"
        target_port = rule.get("remote_port") or rule.get("local_port") or 0
        if rtype == "remote":
            return f"Remote {listen} → {target_host}:{target_port}"
        return f"Local {listen} → {target_host}:{target_port}"


class StatusBar(Static):
    """Single-line status indicator."""

    def set_message(self, message: str, *, error: bool = False) -> None:
        self.set_class(error, "error")
        self.update(message or "")


class SshPilotTuiApp(App[None]):
    """Textual-based interface for browsing and launching sshPilot connections."""

    TITLE = "sshPilot TUI"
    CSS = """
    Screen {
        layout: vertical;
    }

    HelpScreen {
        align: center middle;
    }

    #body {
        height: 1fr;
        padding: 1 2;
        column-gap: 2;
    }

    #list-panel, #details-panel {
        height: 1fr;
    }

    #details-panel {
        border: round $secondary;
        padding: 1;
    }

    #details {
        height: 1fr;
        overflow-y: auto;
    }

    #connection-table {
        height: 1fr;
    }

    #status {
        height: 3;
        content-align: left middle;
        padding: 0 1;
        background: $boost;
    }

    #status.error {
        background: $error;
        color: $text;
    }

    .panel-title {
        text-style: bold;
        padding-bottom: 1;
    }

    #filter {
        margin-bottom: 1;
    }

    #help-panel {
        width: 70%;
        background: $surface;
        border: round $secondary;
        padding: 2;
        content-align: left top;
    }
    """

    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("ctrl+c", "quit_app", "Quit", show=False),
        Binding("c", "connect", "Connect"),
        Binding("e", "edit", "Edit"),
        Binding("r", "reload", "Reload"),
        Binding("f5", "reload", "Reload", show=False),
        Binding("/", "focus_filter", "Filter"),
        Binding("ctrl+f", "focus_filter", "Filter", show=False),
        Binding("escape", "focus_list", "Focus list", show=False),
        Binding("?", "show_help", "Help"),
    ]

    def __init__(self, *, isolated_mode: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.isolated_mode = isolated_mode
        self.config = Config()
        self.connection_manager = ConnectionManager(self.config, isolated_mode=isolated_mode)
        try:
            self.connection_manager._post_init_slow_path()
        except Exception:
            LOG.debug("Post init slow path failed", exc_info=True)

        self.connections: List = []
        self.filtered_connections: List = []
        self.row_map: Dict[str, object] = {}
        self.filter_text = ""
        self._selected_row_key: Optional[str] = None
        self._last_selection_token: Optional[Tuple[str, str, str, str]] = None
        self._status_timer: Optional[Timer] = None

    # --------------------------------------------------------------------- UI
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="list-panel"):
                yield Static("Connections", classes="panel-title")
                yield Input(placeholder="Filter connections…", id="filter")
                table = ConnectionTable(id="connection-table")
                table.add_columns("Nickname", "Host", "User", "Port")
                yield table
            with Vertical(id="details-panel"):
                yield Static("Details", classes="panel-title")
                yield DetailsPanel(id="details")
        yield Footer()
        yield StatusBar(id="status")

    async def on_mount(self) -> None:
        self.status_bar = self.query_one(StatusBar)
        self.details_panel = self.query_one(DetailsPanel)
        self.filter_input = self.query_one("#filter", Input)
        self.connection_table = self.query_one(ConnectionTable)
        self.connection_table.focus()
        self.details_panel.show_empty()
        await self._load_connections(initial=True)

    # ---------------------------------------------------------------- bindings
    def action_quit_app(self) -> None:
        self.exit()

    async def action_reload(self) -> None:
        await self._load_connections(initial=False)

    async def action_connect(self) -> None:
        conn = self.get_selected_connection()
        if not conn:
            self.set_status("No connection selected", error=True)
            return
        try:
            cmd = build_ssh_command(
                conn,
                self.config,
                known_hosts_path=self.connection_manager.known_hosts_path,
            )
        except Exception as exc:
            LOG.exception("Failed to build SSH command")
            self.set_status(f"Failed to prepare SSH command: {exc}", error=True, persist=True)
            return

        display_cmd = " ".join(shlex.quote(part) for part in cmd)
        self.set_status(f"Connecting with: {display_cmd}", persist=True)
        async with self.suspend():
            try:
                rc = subprocess.run(cmd).returncode
            except FileNotFoundError:
                rc = -1
                print("ssh executable was not found on PATH.")
            except Exception as exc:
                rc = -1
                print(f"Connection failed: {exc}")
        if rc == 0:
            self.set_status("SSH session ended")
        else:
            self.set_status(f"SSH exited with code {rc}", error=True)

    async def action_edit(self) -> None:
        conn = self.get_selected_connection()
        if not conn:
            self.set_status("No connection selected", error=True)
            return

        async with self.suspend():
            session = ConnectionEditSession(conn)
            new_data = session.run()

        if not new_data:
            self.set_status("Edit cancelled")
            return

        try:
            updated = await self.call_in_thread(self.connection_manager.update_connection, conn, new_data)
        except Exception:
            LOG.exception("Failed to update connection from Textual TUI")
            updated = False

        if updated:
            await self._load_connections(initial=True)
            self.set_status("Connection updated", persist=True)
        else:
            self.set_status("Failed to update connection", error=True)

    def action_focus_filter(self) -> None:
        self.filter_input.focus()
        self.filter_input.cursor_position = len(self.filter_input.value)

    def action_focus_list(self) -> None:
        self.connection_table.focus()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen())

    # ----------------------------------------------------------------- events
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is self.filter_input:
            self.filter_text = event.value
            self.apply_filter()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input is self.filter_input:
            self.connection_table.focus()

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table is not self.connection_table:
            return
        conn = self.row_map.get(event.row_key)
        if conn:
            self._selected_row_key = event.row_key
            self._last_selection_token = self._connection_token(conn)
            self.details_panel.show_connection(conn)

    def on_connection_table_connect_requested(self, event: ConnectionTable.ConnectRequested) -> None:
        event.stop()
        self.call_later(lambda: self.create_task(self.action_connect()))

    # ----------------------------------------------------------------- data ops
    async def _load_connections(self, *, initial: bool) -> None:
        self.set_status("Loading connections…")
        try:
            connections = await self.call_in_thread(self._load_connections_sync)
        except Exception as exc:
            LOG.exception("Failed to load connections")
            self.set_status(f"Unable to load connections: {exc}", error=True, persist=True)
            return

        self.connections = connections
        self.apply_filter(preserve_selection=not initial)
        self.set_status(f"Loaded {len(connections)} connection(s)")

    def _load_connections_sync(self) -> List:
        self.connection_manager.load_ssh_config()
        items = list(self.connection_manager.connections)
        items.sort(
            key=lambda c: (
                str(getattr(c, "nickname", "") or "").lower(),
                str(getattr(c, "hostname", "") or getattr(c, "host", "") or "").lower(),
            )
        )
        return items

    def apply_filter(self, *, preserve_selection: bool = True) -> None:
        table = self.connection_table
        needle = (self.filter_text or "").strip().lower()
        filtered: List = []
        for conn in self.connections:
            if not needle or self._matches(conn, needle):
                filtered.append(conn)

        self.filtered_connections = filtered
        table.clear(columns=False)
        self.row_map.clear()

        target_token = self._last_selection_token if preserve_selection else None
        selected_key: Optional[str] = None

        for idx, conn in enumerate(filtered):
            row_key = f"row-{idx}"
            nickname = getattr(conn, "nickname", "") or "(unnamed)"
            host = getattr(conn, "hostname", "") or getattr(conn, "host", "") or "?"
            user = getattr(conn, "username", "") or "-"
            port = getattr(conn, "port", 22)
            table.add_row(nickname, host, user, str(port), key=row_key)
            self.row_map[row_key] = conn
            token = self._connection_token(conn)
            if target_token and token == target_token:
                selected_key = row_key

        if self.row_map:
            row_key = selected_key or next(iter(self.row_map.keys()))
            table.select_row(row_key)
            table.scroll_to_row(row_key)
            self._selected_row_key = row_key
            conn = self.row_map[row_key]
            self._last_selection_token = self._connection_token(conn)
            self.details_panel.show_connection(conn)
        else:
            message = "No matches for current filter" if needle else "No connections available"
            self.details_panel.show_empty(message)
            self._selected_row_key = None

    def get_selected_connection(self):
        if not self._selected_row_key:
            return None
        return self.row_map.get(self._selected_row_key)

    @staticmethod
    def _matches(connection, needle: str) -> bool:
        fields = (
            getattr(connection, "nickname", ""),
            getattr(connection, "hostname", ""),
            getattr(connection, "host", ""),
            getattr(connection, "username", ""),
            getattr(connection, "source", ""),
        )
        for field in fields:
            if field and needle in str(field).lower():
                return True
        return False

    @staticmethod
    def _connection_token(connection) -> Tuple[str, str, str, str]:
        return (
            str(getattr(connection, "nickname", "") or ""),
            str(getattr(connection, "hostname", "") or getattr(connection, "host", "") or ""),
            str(getattr(connection, "username", "") or ""),
            str(getattr(connection, "port", 22)),
        )

    # ----------------------------------------------------------------- status
    def set_status(self, message: str, *, error: bool = False, persist: bool = False) -> None:
        if not hasattr(self, "status_bar"):
            return
        if self._status_timer:
            self._status_timer.stop()
            self._status_timer = None
        self.status_bar.set_message(message, error=error)
        if not persist:
            self._status_timer = self.set_timer(6, self._clear_status, name="status-clear")

    def _clear_status(self) -> None:
        self.status_bar.set_message("")
        self._status_timer = None


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(description="sshPilot terminal UI")
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Use the isolated sshPilot SSH config stored under ~/.config/sshpilot",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Python logging level (default: WARNING)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None):
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    app = SshPilotTuiApp(isolated_mode=args.isolated)
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    return 0


__all__ = ["main", "SshPilotTuiApp"]
