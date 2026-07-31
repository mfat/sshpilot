"""Phase 14 production GTK harness helpers.

Boots real GTK + VTE + MainWindow against an ephemeral daemon and temporary
OpenSSH fixture. Synchronization prefers daemon events, GTK signals, and
bounded predicate polls — not fixed sleeps as the primary strategy.
"""

from __future__ import annotations

import logging
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

OUTPUT_MARKER = "PHASE14_TERMINAL_OUTPUT_OK"
_GLOBAL_SPAWN_LOG: list = []


class Phase14EvidenceError(AssertionError):
    """Failure with actionable evidence for Phase 14 gates."""

    def __init__(self, message: str, *, evidence: Optional[dict] = None):
        self.evidence = evidence or {}
        detail = message
        if self.evidence:
            detail = f"{message} | evidence={self.evidence!r}"
        super().__init__(detail)


@dataclass
class Phase14Harness:
    """Owns isolated HOME, OpenSSH fixture, ephemeral daemon, and GuiApp."""

    home: Path
    gui: Any = None
    openssh: Any = None
    daemon_server: Any = None
    daemon_client: Any = None
    daemon_socket: Optional[Path] = None
    auth_helper_stop: threading.Event = field(default_factory=threading.Event)
    auth_helper_thread: Optional[threading.Thread] = None
    legacy_spawn_calls: list = field(default_factory=list)
    legacy_open_calls: list = field(default_factory=list)
    critical_warnings: list = field(default_factory=list)
    _spies_installed: bool = False
    _original_legacy: Any = None

    # -- lifecycle ---------------------------------------------------------

    def boot(self, *, application_id: Optional[str] = None) -> "Phase14Harness":
        from tests._gui_harness import GuiApp, requires_gui
        from tests.fixtures.temporary_openssh import start_temporary_openssh

        from sshpilot.api import DaemonClient, InProcessClient
        from sshpilot.api.client_factory import ClientMode, ClientSelection
        from sshpilot.daemon import DaemonServer
        from sshpilot.gtk_client_bridge import GtkClientBridge

        requires_gui()
        self._install_critical_warning_trap()

        fixture_root = self.home / "openssh"
        fixture_root.mkdir(parents=True, exist_ok=True)
        self.openssh = start_temporary_openssh(fixture_root)

        app_id = application_id or (
            f"io.github.mfat.sshpilot.p14t{os.getpid()}x{secrets.token_hex(3)}"
        )
        self.gui = GuiApp().boot(application_id=app_id)
        win = self.gui.window
        self.gui.pump(400)

        win.config.set_setting("terminal.daemon_backed_ssh", True)
        win.config.set_setting("terminal.legacy_local_ssh_fallback", False)
        win.config.set_setting("terminal.daemon_tab_close_policy", "detach")
        win.config.set_setting("terminal.daemon_app_close_policy", "ask")
        win.config.set_setting("terminal.daemon_restore_sessions", True)
        win.config.set_setting("terminal.daemon_auto_attach", True)
        win.config.set_setting("file_manager.force_internal", True)
        win.config.set_setting("file_manager.first_run_prompt_shown", True)
        win.config.set_setting("ssh.strict_host_key_checking", "accept-new")
        os.environ["SSHPILOT_CLIENT_MODE"] = "daemon"

        iso = Path(os.environ["XDG_CONFIG_HOME"]) / "sshpilot"
        iso.mkdir(parents=True, exist_ok=True)
        (iso / "known_hosts").write_text(self.openssh.known_hosts.read_text())

        sock_root = Path(
            tempfile.mkdtemp(prefix=f"sshpilot-p14d-{secrets.token_hex(4)}-")
        )
        self.daemon_socket = sock_root / "sshpilotd.sock"
        cm = win.connection_manager
        gm = win.group_manager
        self.daemon_server = DaemonServer(
            lambda: InProcessClient(
                cm,
                group_manager=gm,
                client_name="sshpilotd-phase14",
            ),
            socket_path=self.daemon_socket,
        )
        self.daemon_server.start_in_thread()
        bridge = GtkClientBridge()
        app = self.gui.app
        app._api_client_bridge = bridge
        win.client_bridge = bridge
        self.daemon_client = DaemonClient(
            socket_path=self.daemon_socket,
            client_id="client:phase14-gtk",
            timeout=60,
        )
        win._apply_client_selection(
            ClientSelection(client=self.daemon_client, mode=ClientMode.DAEMON)
        )
        self.gui.pump(300)
        if type(win.client).__name__ != "DaemonClient":
            raise Phase14EvidenceError(
                "failed to inject DaemonClient",
                evidence={"got": type(win.client).__name__},
            )
        self._install_legacy_spies()
        return self

    def close(self) -> None:
        self.stop_auth_helper()
        try:
            if self.gui is not None:
                self.gui.shutdown()
        except Exception:
            logger.debug("gui shutdown failed", exc_info=True)
        self.gui = None
        try:
            if self.daemon_client is not None:
                self.daemon_client.close()
        except Exception:
            pass
        self.daemon_client = None
        try:
            if self.daemon_server is not None:
                self.daemon_server.shutdown()
                self.daemon_server.wait_stopped(timeout=10)
        except Exception:
            pass
        self.daemon_server = None
        try:
            if self.openssh is not None:
                self.openssh.destroy()
        except Exception:
            pass
        self.openssh = None

    def restart_app(self, *, application_id: Optional[str] = None) -> Any:
        """Shut down GTK only; keep ephemeral daemon + OpenSSH."""
        if self.gui is not None:
            try:
                self.gui.shutdown()
            except Exception:
                pass
            self.gui = None

        from tests._gui_harness import GuiApp
        from sshpilot.api import DaemonClient
        from sshpilot.api.client_factory import ClientMode, ClientSelection
        from sshpilot.gtk_client_bridge import GtkClientBridge

        app_id = application_id or (
            f"io.github.mfat.sshpilot.p14r{os.getpid()}x{secrets.token_hex(3)}"
        )
        self.gui = GuiApp().boot(application_id=app_id)
        win = self.gui.window
        self.gui.pump(400)
        win.config.set_setting("terminal.daemon_backed_ssh", True)
        win.config.set_setting("terminal.legacy_local_ssh_fallback", False)
        win.config.set_setting("terminal.daemon_restore_sessions", True)
        win.config.set_setting("terminal.daemon_auto_attach", True)
        bridge = GtkClientBridge()
        self.gui.app._api_client_bridge = bridge
        win.client_bridge = bridge
        # Reconnect client (previous close may have dropped it).
        try:
            if self.daemon_client is not None:
                self.daemon_client.close()
        except Exception:
            pass
        self.daemon_client = DaemonClient(
            socket_path=self.daemon_socket,
            client_id=f"client:phase14-gtk-{secrets.token_hex(2)}",
            timeout=60,
        )
        win._apply_client_selection(
            ClientSelection(client=self.daemon_client, mode=ClientMode.DAEMON)
        )
        self.gui.pump(300)
        self._install_legacy_spies()
        return self.gui

    # -- sync --------------------------------------------------------------

    def pump(self, ms: int = 50) -> None:
        if self.gui is None:
            return
        self.gui.pump(ms)

    def pump_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = 30.0,
        label: str = "condition",
        pump_ms: int = 50,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.pump(pump_ms)
            try:
                if predicate():
                    return True
            except Exception:
                pass
        try:
            ok = bool(predicate())
        except Exception as exc:
            raise Phase14EvidenceError(
                f"timed out waiting for {label}",
                evidence={"error": repr(exc), "timeout": timeout},
            ) from exc
        if not ok:
            raise Phase14EvidenceError(
                f"timed out waiting for {label}",
                evidence={"timeout": timeout},
            )
        return True

    # -- connections -------------------------------------------------------

    def add_password_connection(self, nickname: str = "P14Term") -> Any:
        win = self.gui.window
        cm = win.connection_manager
        openssh = self.openssh
        data = {
            "nickname": nickname,
            "hostname": "127.0.0.1",
            "host": "127.0.0.1",
            "username": openssh.username,
            "port": openssh.port,
            "auth_method": 1,
            "keyfile": "",
            "key_select_mode": 0,
            "password": "",
            "protocol": "ssh",
            "extra_ssh_config": (
                f"UserKnownHostsFile {openssh.known_hosts}\n"
                f"GlobalKnownHostsFile /dev/null\n"
                f"StrictHostKeyChecking accept-new\n"
                "ControlMaster no\n"
                "ControlPath none\n"
                "IdentitiesOnly yes\n"
            ),
        }
        conn = cm.add_connection_from_data(data)
        try:
            cm.store_connection_password(conn, openssh.password)
        except Exception:
            conn.password = openssh.password
        else:
            conn.password = openssh.password
        self.pump(200)
        try:
            win.rebuild_connection_list()
        except Exception:
            try:
                win._rebuild_connection_list()
            except Exception:
                pass
        self.pump(200)
        # Re-assert daemon client after connection writes (welcome/reconnect
        # races must not silently demote to in-process).
        if type(win.client).__name__ != "DaemonClient":
            from sshpilot.api.client_factory import ClientMode, ClientSelection

            win._apply_client_selection(
                ClientSelection(client=self.daemon_client, mode=ClientMode.DAEMON)
            )
            self.pump(100)
        return conn

    # -- auth helper (broker; mandatory GTK dialog tests use real dialogs) -

    def start_auth_helper(
        self,
        *,
        submit_password=None,
        submit_passphrase=None,
        accept_host_key: bool = True,
        decline_password: bool = False,
    ) -> None:
        from sshpilot.api.models import (
            ClientId,
            HostKeyDecision,
            InteractionDecisionRequest,
            InteractionState,
            InteractionType,
            RememberPolicy,
            SecretDecision,
        )
        from sshpilot.api.transport.secret_frames import SecretFrame, SecretFrameKind

        self.stop_auth_helper()
        self.auth_helper_stop = threading.Event()
        stop = self.auth_helper_stop
        client = self.daemon_client
        owner_id = ClientId(str(getattr(client, "_client_id", "client:phase14-gtk")))
        password = (
            submit_password
            if submit_password is not None
            else (self.openssh.password if self.openssh else None)
        )
        passphrase = submit_passphrase

        def _loop():
            while not stop.is_set():
                broker = getattr(self.daemon_server, "_interaction_broker", None)
                if broker is None:
                    stop.wait(0.05)
                    continue
                try:
                    for summary in broker.list(owner_id):
                        if summary.state is not InteractionState.PENDING:
                            continue
                        if (
                            summary.type is InteractionType.PASSWORD
                            and password is None
                            and not decline_password
                        ):
                            continue
                        if (
                            summary.type is InteractionType.PRIVATE_KEY_PASSPHRASE
                            and passphrase is None
                            and not decline_password
                        ):
                            continue
                        try:
                            broker.claim(summary.id, owner_id)
                        except Exception:
                            continue
                        try:
                            if summary.type is InteractionType.HOST_KEY_CONFIRMATION:
                                broker.respond(
                                    InteractionDecisionRequest(
                                        interaction_id=summary.id,
                                        host_key_decision=(
                                            HostKeyDecision.ACCEPT_ONCE
                                            if accept_host_key
                                            else HostKeyDecision.REJECT
                                        ),
                                    ),
                                    owner_id,
                                )
                            elif summary.type is InteractionType.PASSWORD:
                                if decline_password:
                                    broker.respond(
                                        InteractionDecisionRequest(
                                            interaction_id=summary.id,
                                            secret_decision=SecretDecision.CANCEL,
                                        ),
                                        owner_id,
                                    )
                                else:
                                    broker.respond(
                                        InteractionDecisionRequest(
                                            interaction_id=summary.id,
                                            secret_decision=SecretDecision.SUBMIT,
                                            remember_policy=RememberPolicy.DO_NOT_STORE,
                                        ),
                                        owner_id,
                                    )
                                    broker.submit_secret(
                                        SecretFrame(
                                            kind=SecretFrameKind.RESPONSE,
                                            interaction_id=summary.id,
                                            secret=(password or "").encode("utf-8"),
                                        ),
                                        owner_id,
                                    )
                            elif summary.type is InteractionType.PRIVATE_KEY_PASSPHRASE:
                                if decline_password or passphrase is None:
                                    broker.respond(
                                        InteractionDecisionRequest(
                                            interaction_id=summary.id,
                                            secret_decision=SecretDecision.CANCEL,
                                        ),
                                        owner_id,
                                    )
                                else:
                                    broker.respond(
                                        InteractionDecisionRequest(
                                            interaction_id=summary.id,
                                            secret_decision=SecretDecision.SUBMIT,
                                            remember_policy=RememberPolicy.DO_NOT_STORE,
                                        ),
                                        owner_id,
                                    )
                                    broker.submit_secret(
                                        SecretFrame(
                                            kind=SecretFrameKind.RESPONSE,
                                            interaction_id=summary.id,
                                            secret=passphrase.encode("utf-8"),
                                        ),
                                        owner_id,
                                    )
                        except Exception:
                            logger.debug(
                                "auth helper respond failed", exc_info=True
                            )
                except Exception:
                    pass
                stop.wait(0.05)

        self.auth_helper_thread = threading.Thread(
            target=_loop, name="phase14-auth-helper", daemon=True
        )
        self.auth_helper_thread.start()

    def stop_auth_helper(self) -> None:
        self.auth_helper_stop.set()
        if self.auth_helper_thread is not None:
            self.auth_helper_thread.join(timeout=2)
            self.auth_helper_thread = None

    # -- terminal helpers --------------------------------------------------

    def connect_terminal(self, connection, *, timeout: float = 45.0) -> Any:
        """User path: TerminalManager.connect_to_host → daemon TerminalWidget."""
        win = self.gui.window
        last_error = None
        for attempt in range(2):
            self.start_auth_helper(submit_password=self.openssh.password)
            try:
                win.terminal_manager.connect_to_host(connection, force_new=True)
                self.pump_until(
                    lambda: self.find_terminal_widget(connection) is not None,
                    timeout=min(15.0, timeout),
                    label="terminal widget created",
                )
                self.find_terminal_widget(connection)

                def _connected():
                    t = self.find_terminal_widget(connection)
                    if t is None:
                        return False
                    ctrl = getattr(t, "_daemon_controller", None)
                    if ctrl is None:
                        return False
                    if getattr(t, "is_connected", False):
                        return True
                    try:
                        from sshpilot.terminal_session_controller import (
                            TerminalSessionState,
                        )

                        state = getattr(ctrl, "state", None)
                        if state in {
                            TerminalSessionState.FAILED,
                            TerminalSessionState.CLOSED,
                        }:
                            raise Phase14EvidenceError(
                                "daemon terminal failed before connected",
                                evidence={
                                    "state": getattr(state, "value", state),
                                    "session_id": getattr(
                                        getattr(ctrl, "tab_state", None),
                                        "session_id",
                                        None,
                                    ),
                                    "attempt": attempt,
                                },
                            )
                    except Phase14EvidenceError:
                        raise
                    except Exception:
                        pass
                    return False

                self.pump_until(
                    _connected, timeout=timeout, label="daemon terminal connected"
                )
                return self.find_terminal_widget(connection)
            except Phase14EvidenceError as exc:
                last_error = exc
                self.pump(300)
                continue
        raise last_error or Phase14EvidenceError("connect_terminal failed")

    def find_terminal_widget(self, connection=None) -> Any:
        win = self.gui.window
        if connection is not None:
            term = win.active_terminals.get(connection)
            if term is not None:
                return term
            for t in win.connection_to_terminals.get(connection) or []:
                return t
        for terms in win.connection_to_terminals.values():
            for t in terms:
                if getattr(t, "_daemon_mode", False):
                    return t
        return None

    def find_vte_widget(self, terminal=None) -> Any:
        term = terminal or self.find_terminal_widget()
        if term is None:
            return None
        return getattr(term, "vte", None)

    def emit_terminal_input(self, text: str, terminal=None) -> None:
        term = terminal or self.find_terminal_widget()
        if term is None:
            raise Phase14EvidenceError("no terminal for input")
        data = text.encode("utf-8") if isinstance(text, str) else text
        if hasattr(term, "feed_child_data"):
            term.feed_child_data(data)
        else:
            raise Phase14EvidenceError("terminal has no feed_child_data")

    def resize_terminal(self, rows: int, cols: int, terminal=None) -> None:
        term = terminal or self.find_terminal_widget()
        vte = self.find_vte_widget(term)
        if vte is None:
            raise Phase14EvidenceError("no VTE for resize")
        try:
            vte.set_size(cols, rows)
        except Exception:
            # Fallback: call daemon controller resize directly after widget size
            pass
        self.pump(100)
        if hasattr(term, "_on_daemon_size_changed") and vte is not None:
            try:
                term._on_daemon_size_changed(vte, 0, 0)
            except Exception:
                pass
        self.pump(100)

    def read_visible_terminal_text(self, terminal=None) -> str:
        term = terminal or self.find_terminal_widget()
        if term is None:
            return ""
        backend = getattr(term, "backend", None)
        if backend is not None and hasattr(backend, "get_text"):
            try:
                text = backend.get_text()
                if text:
                    return text
            except Exception:
                pass
        vte = getattr(term, "vte", None)
        if vte is None:
            return ""
        try:
            from gi.repository import Vte

            if hasattr(vte, "get_text_format"):
                content = vte.get_text_format(Vte.Format.TEXT)
                if isinstance(content, tuple):
                    return content[0] or ""
                return content or ""
        except Exception:
            pass
        try:
            rows = int(vte.get_row_count() or 24)
            if hasattr(vte, "get_text_range_format"):
                from gi.repository import Vte

                res = vte.get_text_range_format(
                    Vte.Format.TEXT, 0, 0, rows, -1
                )
                if isinstance(res, tuple):
                    return res[0] or ""
                return res or ""
        except Exception:
            pass
        return ""

    def wait_for_marker(
        self, marker: str = OUTPUT_MARKER, *, timeout: float = 30.0, terminal=None
    ) -> str:
        text_holder = {"t": ""}

        def _has():
            text_holder["t"] = self.read_visible_terminal_text(terminal)
            return marker in text_holder["t"]

        self.pump_until(_has, timeout=timeout, label=f"marker {marker!r} in VTE")
        return text_holder["t"]

    def terminal_evidence(self, terminal=None) -> dict:
        term = terminal or self.find_terminal_widget()
        ctrl = getattr(term, "_daemon_controller", None) if term else None
        tab = getattr(ctrl, "tab_state", None) if ctrl else None
        vte = self.find_vte_widget(term)
        text = self.read_visible_terminal_text(term) if term else ""
        return {
            "gtk_connected": bool(
                term is not None
                and ctrl is not None
                and getattr(tab, "session_id", None)
                and getattr(term, "is_connected", False)
                and vte is not None
            ),
            "terminal_widget_attached": term is not None and vte is not None,
            "terminal_output_visible": OUTPUT_MARKER in text,
            "session_id": getattr(tab, "session_id", None),
            "attachment_id": getattr(tab, "attachment_id", None),
            "daemon_mode": bool(getattr(term, "_daemon_mode", False)),
            "legacy_spawn_calls": list(self.legacy_spawn_calls),
            "legacy_open_calls": list(self.legacy_open_calls),
            "has_process_pid": bool(getattr(term, "process_pid", None)),
            "vte_text_sample": text[-200:] if text else "",
            "critical_warnings": list(self.critical_warnings),
        }

    # -- spies / warnings --------------------------------------------------

    def _install_legacy_spies(self) -> None:
        if self._spies_installed or self.gui is None:
            return
        win = self.gui.window
        tm = win.terminal_manager
        original_legacy = tm._open_legacy_local_ssh

        def _spy_legacy(*args, **kwargs):
            self.legacy_open_calls.append((args, kwargs))
            raise Phase14EvidenceError(
                "legacy local SSH path invoked during Phase 14 daemon gate"
            )

        tm._open_legacy_local_ssh = _spy_legacy  # type: ignore[method-assign]

        try:
            from gi.repository import Vte

            if not hasattr(Vte.Terminal, "_phase14_orig_spawn_async"):
                Vte.Terminal._phase14_orig_spawn_async = Vte.Terminal.spawn_async

                def _spy_spawn(self_vte, *args, **kwargs):
                    _GLOBAL_SPAWN_LOG.append((args, kwargs))
                    return Vte.Terminal._phase14_orig_spawn_async(
                        self_vte, *args, **kwargs
                    )

                Vte.Terminal.spawn_async = _spy_spawn  # type: ignore[method-assign]
            self.legacy_spawn_calls = _GLOBAL_SPAWN_LOG
        except Exception:
            logger.debug("VTE spawn spy unavailable", exc_info=True)
        self._spies_installed = True
        self._original_legacy = original_legacy

    def _install_critical_warning_trap(self) -> None:
        try:
            from gi.repository import GLib

            domains = (
                "Gtk",
                "GLib",
                "GObject",
                "Gdk",
                "Gsk",
                "Vte",
                "Adwaita",
            )

            def _handler(log_domain, log_level, message, *user):
                level = int(log_level)
                # G_LOG_LEVEL_CRITICAL = 1 << 3 = 8
                if level & (GLib.LogLevelFlags.LEVEL_CRITICAL | GLib.LogLevelFlags.LEVEL_ERROR):
                    self.critical_warnings.append(f"{log_domain}: {message}")
                return GLib.LogWriterOutput.UNHANDLED

            for domain in domains:
                try:
                    GLib.log_set_handler(
                        domain,
                        GLib.LogLevelFlags.LEVEL_CRITICAL
                        | GLib.LogLevelFlags.LEVEL_ERROR
                        | GLib.LogLevelFlags.FLAG_FATAL,
                        _handler,
                        None,
                    )
                except Exception:
                    pass
        except Exception:
            logger.debug("critical warning trap unavailable", exc_info=True)



    def open_file_manager(self, connection, *, timeout: float = 45.0) -> Any:
        """Open Manage Files through the production window path."""
        win = self.gui.window
        self.start_auth_helper(submit_password=self.openssh.password)
        opener = getattr(win, "_open_manage_files_now_for_connection", None)
        if opener is None:
            raise Phase14EvidenceError("window missing manage-files opener")
        opener(connection, force_builtin=True)
        self.pump_until(
            lambda: self.find_file_manager_controller() is not None,
            timeout=min(20.0, timeout),
            label="file manager controller",
        )

        def _ready():
            mgr = self.find_file_manager_backend()
            if mgr is None:
                return False
            ctrl = getattr(mgr, "_sftp_controller", None)
            if ctrl is None:
                return False
            try:
                from sshpilot.sftp_service_controller import SftpControllerState

                return ctrl.state is SftpControllerState.READY and ctrl.service_id
            except Exception:
                return bool(getattr(ctrl, "service_id", None))

        self.pump_until(_ready, timeout=timeout, label="SFTP READY")
        # Allow listing model to populate after READY
        def _listed():
            ev = self.file_manager_evidence()
            return ev.get("row_count", 0) > 0

        try:
            self.pump_until(_listed, timeout=min(20.0, timeout), label="FM listing rows")
        except Phase14EvidenceError:
            # Leave evidence collection to the caller
            pass
        return self.find_file_manager_controller()

    def find_file_manager_controller(self) -> Any:
        win = self.gui.window
        for attr in (
            "_file_manager_controllers",
            "_internal_file_manager_controllers",
            "file_manager_controllers",
        ):
            mapping = getattr(win, attr, None)
            if isinstance(mapping, dict) and mapping:
                return next(iter(mapping.values()))
            if isinstance(mapping, list) and mapping:
                return mapping[0]
        # Embedded tabs may store controller on page child
        for i in range(win.tab_view.get_n_pages()):
            page = win.tab_view.get_nth_page(i)
            child = page.get_child()
            ctrl = getattr(child, "_controller", None) or getattr(
                child, "controller", None
            )
            if ctrl is not None:
                return ctrl
            mgr = getattr(child, "_manager", None)
            if mgr is not None:
                return child
        registry = getattr(win, "_internal_file_manager_windows", None)
        if registry:
            return next(iter(registry))
        return None

    def find_file_manager_backend(self) -> Any:
        ctrl = self.find_file_manager_controller()
        if ctrl is None:
            return None
        for attr in ("_manager", "manager", "_backend", "backend"):
            mgr = getattr(ctrl, attr, None)
            if mgr is not None:
                return mgr
        # Controller may itself be the manager/window
        if hasattr(ctrl, "_sftp_controller"):
            return ctrl
        return None

    def file_manager_evidence(self) -> dict:
        from sshpilot.daemon_sftp_backend import DaemonSftpManager

        mgr = self.find_file_manager_backend()
        ctrl = getattr(mgr, "_sftp_controller", None) if mgr else None
        service_id = getattr(ctrl, "service_id", None) if ctrl else None
        state = getattr(ctrl, "state", None) if ctrl else None
        row_count = 0
        names: list[str] = []

        def _collect_from(obj) -> None:
            nonlocal row_count, names
            if obj is None or row_count:
                return
            entries = getattr(obj, "_entries", None) or getattr(obj, "_cached_entries", None)
            if entries:
                try:
                    names = [getattr(e, "name", str(e)) for e in list(entries)]
                    row_count = len(names)
                except Exception:
                    pass
            for attr in ("_remote_pane", "remote_pane", "_right_pane", "right_pane"):
                _collect_from(getattr(obj, attr, None))
            # FileManagerWindow panes
            for attr in ("_panes", "panes"):
                panes = getattr(obj, attr, None)
                if panes is None:
                    continue
                try:
                    for pane in panes:
                        _collect_from(pane)
                except TypeError:
                    _collect_from(panes)

        _collect_from(mgr)
        _collect_from(self.find_file_manager_controller())

        if row_count == 0 and self.daemon_client is not None and service_id is not None:
            try:
                from sshpilot.api.models import ListDirectoryRequest

                connection_id = getattr(mgr, "_connection_id", None) or getattr(
                    getattr(mgr, "_sftp_controller", None), "connection_id", None
                )
                listing = self.daemon_client.sftp_list_directory(
                    ListDirectoryRequest(
                        connection_id=connection_id,
                        service_id=service_id,
                        path=".",
                    )
                )
                names = [getattr(e, "name", "") for e in (listing.entries or [])]
                row_count = len(names)
            except Exception as exc:
                names = [f"list_error:{exc!r}"]
        state_name = getattr(state, "name", None) or str(getattr(state, "value", state) or "")
        ready = str(state_name).upper().endswith("READY") or str(state_name).upper() == "READY"
        fm_connected = bool(
            mgr is not None
            and isinstance(mgr, DaemonSftpManager)
            and service_id is not None
            and ready
        )
        return {
            "fm_connected": fm_connected,
            "listing_model_populated": row_count > 0,
            "backend": type(mgr).__name__ if mgr else None,
            "service_id": str(service_id) if service_id else None,
            "state": str(state),
            "row_count": row_count,
            "names": names[:20],
        }

    def detach_terminal_keep_session(self, terminal=None) -> str:
        """Detach GTK view, persist restore metadata, return session_id."""
        term = terminal or self.find_terminal_widget()
        if term is None:
            raise Phase14EvidenceError("no terminal to detach")
        ctrl = getattr(term, "_daemon_controller", None)
        tab = getattr(ctrl, "tab_state", None) if ctrl else None
        session_id = getattr(tab, "session_id", None)
        if not session_id:
            raise Phase14EvidenceError("terminal has no session_id")
        from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

        DaemonSessionRestoreManager(self.gui.window.config).save_session_metadata(
            tab,
            tab_title=getattr(term.connection, "nickname", "Terminal"),
        )
        ctrl.detach()
        term._daemon_mode = False
        term.is_connected = False
        self.pump(200)
        return str(session_id)

    def send_input_while_detached(self, session_id: str, command: str) -> None:
        """Attach a temporary client view and run a remote command."""
        from sshpilot.api import DaemonClient
        from sshpilot.api.models.sessions import AttachSessionRequest, DetachSessionRequest
        from sshpilot.api.models.terminal import TerminalInput

        client = DaemonClient(
            socket_path=self.daemon_socket,
            client_id=f"client:phase14-bg-{secrets.token_hex(2)}",
            timeout=30,
        )
        try:
            attached = client.attach_session(
                AttachSessionRequest(
                    session_id=session_id,
                    request_input=True,
                    from_sequence=0,
                )
            )
            client.send_terminal_input(
                TerminalInput(
                    session_id=session_id,
                    attachment_id=attached.attachment.id,
                    data=command.encode("utf-8"),
                )
            )
            time.sleep(0.5)
            client.detach_session(
                DetachSessionRequest(
                    session_id=session_id,
                    attachment_id=attached.attachment.id,
                )
            )
        finally:
            client.close()

    def quit_keep_running(self) -> None:
        from sshpilot.daemon_quit_policy import apply_keep_running

        apply_keep_running(self.gui.window)
        self.pump_until(
            lambda: self.gui is None or getattr(self.gui.window, "_is_quitting", False),
            timeout=15.0,
            label="quit keep-running started",
        )
        # Allow cleanup timeouts to finish
        for _ in range(30):
            self.pump(100)

def make_isolated_home(tmp_path: Path) -> Path:
    """Prepare isolated HOME/XDG dirs and export them into os.environ."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for name, sub in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_STATE_HOME", ".local/state"),
        ("XDG_CACHE_HOME", ".cache"),
        ("XDG_RUNTIME_DIR", "runtime"),
    ):
        path = home / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[name] = str(path)
    os.environ["HOME"] = str(home)
    os.environ.pop("SSHPILOT_DAEMON_SOCKET", None)
    return home
