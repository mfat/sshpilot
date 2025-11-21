from __future__ import annotations

import argparse
import curses
import locale
import logging
import shlex
import subprocess
import time
from typing import List, Optional, Sequence

from sshpilot.config import Config
from sshpilot.connection_manager import ConnectionManager
from sshpilot.tui.command_builder import build_ssh_command
from sshpilot.tui.editor import ConnectionEditSession

LOG = logging.getLogger(__name__)


class SshPilotTuiApp:
    """
    Curses-based interface for browsing and launching sshPilot connections.
    """

    def __init__(self, *, isolated_mode: bool = False):
        self.isolated_mode = isolated_mode
        self.config = Config()
        self.connection_manager = ConnectionManager(self.config, isolated_mode=isolated_mode)
        try:
            self.connection_manager._post_init_slow_path()
        except Exception:
            LOG.debug("Post init slow path failed", exc_info=True)

        self.connections = []
        self.filtered_connections: List = []
        self.selected_index = 0
        self.scroll_offset = 0
        self.filter_text = ""
        self.mode = "navigate"
        self.show_help = False
        self.status_message = ""
        self.status_error = False
        self.status_expiry: Optional[float] = None
        self.visible_list_rows = 1
        self.screen = None

        self.reload_connections(initial=True)

    # ------------------------------------------------------------------ helpers
    def reload_connections(self, *, initial: bool = False):
        self.connection_manager.load_ssh_config()
        self.connections = sorted(
            list(self.connection_manager.connections),
            key=lambda c: (str(getattr(c, "nickname", "") or "").lower(), str(getattr(c, "hostname", "") or "").lower()),
        )
        self.apply_filter()
        if not initial:
            self.set_status(f"Loaded {len(self.connections)} connection(s)")

    def apply_filter(self):
        if not self.filter_text:
            self.filtered_connections = list(self.connections)
        else:
            needle = self.filter_text.lower()
            self.filtered_connections = [
                conn
                for conn in self.connections
                if self._matches(conn, needle)
            ]
        if self.filtered_connections:
            self.selected_index = max(0, min(self.selected_index, len(self.filtered_connections) - 1))
        else:
            self.selected_index = 0
        self.scroll_offset = 0

    @staticmethod
    def _matches(connection, needle: str) -> bool:
        fields = [
            getattr(connection, "nickname", ""),
            getattr(connection, "hostname", ""),
            getattr(connection, "host", ""),
            getattr(connection, "username", ""),
            getattr(connection, "source", ""),
        ]
        for field in fields:
            if field and needle in str(field).lower():
                return True
        return False

    def set_status(self, message: str, *, error: bool = False, persist: bool = False):
        self.status_message = message
        self.status_error = error
        self.status_expiry = None if persist else (time.time() + 6)

    def clear_status_if_needed(self):
        if self.status_message and self.status_expiry and time.time() > self.status_expiry:
            self.status_message = ""
            self.status_expiry = None

    # ----------------------------------------------------------------- safe UI
    def _safe_addstr(self, row: int, col: int, text: str, attr: int = 0):
        if not self.screen:
            return
        try:
            self.screen.addstr(row, col, text, attr)
        except curses.error:
            pass

    def _safe_addnstr(self, row: int, col: int, text: str, max_len: int, attr: int = 0):
        if not self.screen:
            return
        if max_len < 0:
            max_len = 0
        try:
            self.screen.addnstr(row, col, text, max_len, attr)
        except curses.error:
            pass

    def _safe_move(self, row: int, col: int):
        if not self.screen:
            return
        try:
            max_row, max_col = self.screen.getmaxyx()
            row = min(max(row, 0), max_row - 1)
            col = min(max(col, 0), max_col - 1)
            self.screen.move(row, col)
        except curses.error:
            pass

    def get_selected_connection(self):
        if not self.filtered_connections:
            return None
        try:
            return self.filtered_connections[self.selected_index]
        except IndexError:
            return None

    # ------------------------------------------------------------------- curses
    def run(self):
        locale.setlocale(locale.LC_ALL, "")
        curses.wrapper(self._curses_main)

    def _curses_main(self, stdscr):
        self.screen = stdscr
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass
        curses.noecho()
        curses.cbreak()
        stdscr.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self._init_colors()
        try:
            self._main_loop()
        finally:
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            curses.echo()
            curses.nocbreak()
            stdscr.keypad(False)

    def _init_colors(self):
        try:
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_RED, -1)
            curses.init_pair(4, curses.COLOR_GREEN, -1)
        except Exception:
            pass

    def _main_loop(self):
        while True:
            self.clear_status_if_needed()
            self._draw()
            key = self.screen.getch()
            if key == -1:
                continue
            if self.show_help:
                if key in (ord("?"), 27, curses.KEY_EXIT, ord("q")):
                    self.show_help = False
                continue
            if self.mode == "filter":
                if self._handle_filter_key(key):
                    break
                continue
            if self._handle_nav_key(key):
                break

    # ------------------------------------------------------------------ drawing
    def _draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 10 or width < 60:
            self._safe_addstr(0, 0, "Window too small for sshpilot-tui (min 60x10)")
            self.screen.refresh()
            return

        if self.show_help:
            self._draw_help(height, width)
            self.screen.refresh()
            return

        title = f"sshPilot TUI — {len(self.filtered_connections)} of {len(self.connections)} connections"
        mode_suffix = " FILTER" if self.mode == "filter" else ""
        self._safe_addnstr(0, 0, title + mode_suffix, width)

        filter_line = f"/ {self.filter_text}" if self.mode == "filter" else f"Filter: {self.filter_text or '(none)'}"
        self._safe_addnstr(1, 0, filter_line.ljust(width), width)
        if self.mode == "filter":
            cursor_col = min(2 + len(self.filter_text), width - 1)
            self._safe_move(1, cursor_col)

        header = "{:<28} {:<20} {:<12} {:>5}".format("Nickname", "Host/Hostname", "User", "Port")
        header_attr = curses.color_pair(2) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
        self._safe_addnstr(2, 0, header.ljust(width), width, header_attr)

        list_start = 3
        status_row = height - 1
        detail_height = max(3, min(6, height // 3))
        detail_start = max(list_start + 1, status_row - detail_height)
        list_height = max(1, detail_start - list_start)
        self.visible_list_rows = list_height
        self._ensure_visible(list_height)

        for row_idx in range(list_height):
            conn_idx = self.scroll_offset + row_idx
            screen_row = list_start + row_idx
            if conn_idx >= len(self.filtered_connections):
                self._safe_addnstr(screen_row, 0, "".ljust(width), width)
                continue
            conn = self.filtered_connections[conn_idx]
            nickname = getattr(conn, "nickname", "") or "(unnamed)"
            host = getattr(conn, "hostname", "") or getattr(conn, "host", "") or "?"
            user = getattr(conn, "username", "") or "-"
            port = getattr(conn, "port", 22)
            line = "{:<28} {:<20} {:<12} {:>5}".format(nickname[:28], host[:20], user[:12], str(port))
            if conn_idx == self.selected_index:
                attr = curses.color_pair(1) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE
                self._safe_addnstr(screen_row, 0, line.ljust(width), width, attr)
            else:
                self._safe_addnstr(screen_row, 0, line.ljust(width), width)

        self._draw_details(detail_start, status_row - 1, width)
        self._draw_status(status_row, width)
        self.screen.refresh()

    def _draw_details(self, start_row: int, end_row: int, width: int):
        conn = self.get_selected_connection()
        if not conn:
            message = "No connections available" if not self.connections else "No matches for current filter"
            self._draw_wrapped(start_row, end_row, width, message)
            return

        host_fn = getattr(conn, "get_effective_host", None)
        effective_host = host_fn() if callable(host_fn) else (getattr(conn, "hostname", "") or getattr(conn, "host", ""))
        source = getattr(conn, "source", "") or "ssh config"
        identity = getattr(conn, "keyfile", "") or "default"
        remote_cmd = getattr(conn, "remote_command", "") or ""
        data = getattr(conn, "data", None)
        if not remote_cmd and isinstance(data, dict):
            remote_cmd = data.get("remote_command", "") or ""
        local_cmd = getattr(conn, "local_command", "") or ""
        rules = [rule for rule in (getattr(conn, "forwarding_rules", []) or []) if rule.get("enabled", True)]

        details = [
            f"Nickname : {getattr(conn, 'nickname', '')}",
            f"Target   : {effective_host} (user: {getattr(conn, 'username', '') or '-'})",
            f"Port     : {getattr(conn, 'port', 22)}    Key: {identity}",
            f"Source   : {source}",
        ]
        if local_cmd:
            details.append(f"Local cmd : {local_cmd}")
        if remote_cmd:
            details.append(f"Remote cmd: {remote_cmd}")
        if rules:
            details.append(f"Forwarding: {len(rules)} rule(s) enabled")

        self._draw_wrapped(start_row, end_row, width, "\n".join(details))

    def _draw_wrapped(self, start_row: int, end_row: int, width: int, text: str):
        lines = []
        for paragraph in text.split("\n"):
            while paragraph:
                lines.append(paragraph[: width - 1])
                paragraph = paragraph[width - 1 :]
        lines = lines[: max(0, end_row - start_row + 1)]
        for idx, line in enumerate(lines):
            self._safe_addnstr(start_row + idx, 0, line.ljust(width), width)

    def _draw_status(self, row: int, width: int):
        if self.status_message:
            attr = curses.color_pair(3) if self.status_error and curses.has_colors() else 0
            msg = self.status_message
        else:
            attr = curses.color_pair(4) if curses.has_colors() else 0
            msg = "Enter: connect  e: edit  /: filter  r: reload  ?: help  q: quit"
        self._safe_addnstr(row, 0, msg.ljust(width), width, attr)

    def _draw_help(self, height: int, width: int):
        help_lines = [
            "sshPilot TUI — key bindings",
            "",
            "Arrow keys / j k : Navigate",
            "PgUp / PgDn      : Scroll a page",
            "Home / End       : Jump to start or end",
            "Enter / c        : Connect to highlighted host",
            "e                : Edit highlighted host",
            "r                : Reload SSH config",
            "/                : Filter connections",
            "Esc              : Cancel filter / exit help",
            "q                : Quit",
            "",
            "Press ? again or Esc to return",
        ]
        start_row = max(0, (height - len(help_lines)) // 2)
        for idx, line in enumerate(help_lines):
            centered = line.center(width)
            self._safe_addnstr(start_row + idx, 0, centered[:width], width, curses.A_BOLD if idx == 0 else 0)

    # --------------------------------------------------------------- key input
    def _handle_filter_key(self, key: int) -> bool:
        if key in (curses.KEY_ENTER, 10, 13):
            self.mode = "navigate"
            curses.curs_set(0)
            self.apply_filter()
            return False
        if key in (27, curses.KEY_EXIT):
            self.mode = "navigate"
            curses.curs_set(0)
            self.filter_text = ""
            self.apply_filter()
            return False
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.filter_text = self.filter_text[:-1]
            self.apply_filter()
            return False
        if 32 <= key <= 126:
            self.filter_text += chr(key)
            self.apply_filter()
            return False
        return False

    def _handle_nav_key(self, key: int) -> bool:
        if key in (ord("q"), 27):
            return True
        if key in (curses.KEY_DOWN, ord("j")):
            self._move_selection(1)
        elif key in (curses.KEY_UP, ord("k")):
            self._move_selection(-1)
        elif key in (curses.KEY_NPAGE,):
            self._move_selection(self.visible_list_rows)
        elif key in (curses.KEY_PPAGE,):
            self._move_selection(-self.visible_list_rows)
        elif key in (curses.KEY_HOME,):
            self.selected_index = 0
        elif key in (curses.KEY_END,):
            if self.filtered_connections:
                self.selected_index = len(self.filtered_connections) - 1
        elif key in (10, 13, ord("c")):
            self._connect_selected()
        elif key in (ord("r"), curses.KEY_F5):
            self.reload_connections()
        elif key in (ord("e"),):
            self._edit_selected()
        elif key == ord("/"):
            self.mode = "filter"
            curses.curs_set(1)
        elif key in (ord("?"), curses.KEY_F1):
            self.show_help = True
        return False

    def _move_selection(self, delta: int):
        if not self.filtered_connections:
            return
        self.selected_index = min(max(0, self.selected_index + delta), len(self.filtered_connections) - 1)
        self._ensure_visible(self.visible_list_rows)

    def _ensure_visible(self, list_height: int):
        if not list_height:
            list_height = 1
        max_index = len(self.filtered_connections) - 1
        if self.selected_index > max_index:
            self.selected_index = max_index
        if self.selected_index < 0:
            self.selected_index = 0
        if self.selected_index < self.scroll_offset:
            self.scroll_offset = self.selected_index
        elif self.selected_index >= self.scroll_offset + list_height:
            self.scroll_offset = self.selected_index - list_height + 1
        max_offset = max(0, len(self.filtered_connections) - list_height)
        if self.scroll_offset > max_offset:
            self.scroll_offset = max_offset

    # ------------------------------------------------------------- connections
    def _connect_selected(self):
        conn = self.get_selected_connection()
        if not conn:
            self.set_status("No connection selected", error=True)
            return
        try:
            cmd = build_ssh_command(conn, self.config, known_hosts_path=self.connection_manager.known_hosts_path)
        except Exception as exc:
            LOG.exception("Failed to build SSH command")
            self.set_status(f"Failed to prepare SSH command: {exc}", error=True)
            return

        display_cmd = " ".join(shlex.quote(part) for part in cmd)
        self.set_status(f"Connecting with: {display_cmd}", persist=True)
        self._suspend_curses()
        try:
            proc = subprocess.run(cmd)
            rc = proc.returncode
        except FileNotFoundError:
            rc = -1
            print("ssh executable was not found on PATH.")
        except Exception as exc:
            rc = -1
            print(f"Connection failed: {exc}")
        finally:
            self._resume_curses()
        if rc == 0:
            self.set_status("SSH session ended", error=False)
        else:
            self.set_status(f"SSH exited with code {rc}", error=True)

    def _edit_selected(self):
        conn = self.get_selected_connection()
        if not conn:
            self.set_status("No connection selected", error=True)
            return

        self._suspend_curses()
        try:
            session = ConnectionEditSession(conn)
            new_data = session.run()
        finally:
            self._resume_curses()

        if not new_data:
            self.set_status("Edit cancelled", error=False)
            return

        try:
            updated = self.connection_manager.update_connection(conn, new_data)
        except Exception:
            LOG.exception("Failed to update connection from TUI")
            updated = False

        if updated:
            self.reload_connections(initial=True)
            self.set_status("Connection updated", persist=True)
        else:
            self.set_status("Failed to update connection", error=True)

    def _suspend_curses(self):
        curses.def_prog_mode()
        curses.endwin()

    def _resume_curses(self):
        curses.reset_prog_mode()
        curses.curs_set(0)
        if self.screen:
            self.screen.refresh()


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
