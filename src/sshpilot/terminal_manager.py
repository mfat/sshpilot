from .api.connection_identity import connection_id_for
import os
import logging
import threading
from typing import Optional


from gi.repository import Gio, GLib, Adw, Gdk, Gtk
from gettext import gettext as _

from .terminal import TerminalWidget

logger = logging.getLogger(__name__)


class TerminalManager:
    """Manage terminal creation and connection lifecycle"""

    def __init__(self, window):
        self.window = window

    def _ensure_backend_alignment(self, terminal) -> None:
        """Ensure the given terminal uses the configured backend."""
        if not terminal or not hasattr(self.window, "config"):
            return
        desired = self.window.config.get_setting("terminal.backend", "vte")
        getter = getattr(terminal, "get_backend_name", None)
        current = getter() if callable(getter) else "vte"
        if isinstance(desired, str) and isinstance(current, str):
            if current.lower() == (desired or "vte").lower():
                return
        aligner = getattr(terminal, "ensure_backend", None)
        if callable(aligner):
            try:
                aligner(desired)
            except Exception:
                logger.error("Failed to align terminal backend", exc_info=True)

    def _iter_all_terminals(self):
        """Every known terminal widget, de-duplicated (connection map, active map, tab view)."""
        seen = set()
        collections = []
        connection_terms = getattr(self.window, "connection_to_terminals", None)
        if isinstance(connection_terms, dict):
            collections.extend(connection_terms.values())
        active = getattr(self.window, "active_terminals", None)
        if isinstance(active, dict):
            collections.append(active.values())
        for group in collections:
            for term in list(group):
                if term in seen:
                    continue
                seen.add(term)
                yield term
        tab_view = getattr(self.window, "tab_view", None)
        if tab_view is not None and hasattr(tab_view, "get_n_pages"):
            try:
                for index in range(tab_view.get_n_pages()):
                    page = tab_view.get_nth_page(index)
                    if page is None:
                        continue
                    term = page.get_child()
                    if term is None or term in seen:
                        continue
                    seen.add(term)
                    yield term
            except Exception:
                logger.debug("Failed to iterate tab view while enumerating terminals", exc_info=True)

    def refresh_backends(self) -> None:
        """Ensure all existing terminals use the configured backend."""
        for term in self._iter_all_terminals():
            self._ensure_backend_alignment(term)

    def rebind_client(self, client) -> None:
        """Push a replaced daemon client into every live terminal's session controller.

        The daemon owns session/attachment state across a transport reconnect —
        only each terminal's cached client handle needs to move. Without this,
        a deferred callback for an in-flight terminal (e.g. a session-opened
        success racing the old transport's shutdown) still calls through the
        closed old client and raises "The client is closed" uncaught."""
        for term in self._iter_all_terminals():
            rebind = getattr(term, "rebind_daemon_client", None)
            if callable(rebind):
                try:
                    rebind(client)
                except Exception:
                    logger.error("Failed to rebind terminal to new daemon client", exc_info=True)

    # Connecting/disconnecting hosts
    def _maybe_unlock_secrets_then(self, retry, _allow_reprompt: bool = True) -> bool:
        """If the selected secret backend is session-backed and locked, show the unlock
        prompt and call ``retry`` when it finishes; return True. Otherwise return False so
        the caller proceeds immediately.

        When this connection only *rode* an already-open prompt (e.g. the startup unlock)
        and that prompt resolves with the vault **still locked** (the user deferred it),
        we don't silently start a background terminal — we re-show *our own* prompt once so
        the user can unlock for this connection. After that one re-prompt we proceed
        regardless (the user cancelling their own prompt falls back to SSH's own prompt)."""
        try:
            controller = getattr(self.window, "secrets_controller", None)
            if controller is None:
                # A missing daemon controller is not permission to inspect or
                # unlock a frontend-owned backend.
                return False
            state = controller.load_state()
            if not state.needs_unlock and not state.login_required:
                return False
            from .secret_unlock_dialog import prompt_unlock

            def _show_unlock():
                def _on_done(_success):
                    still_locked = False
                    try:
                        current = controller.load_state()
                        still_locked = bool(current.needs_unlock or current.login_required)
                    except Exception:
                        still_locked = False
                    if (
                        still_locked
                        and _allow_reprompt
                        and not owned[0]
                        and self._maybe_unlock_secrets_then(retry, _allow_reprompt=False)
                    ):
                        return  # rode a deferred prompt -> our own prompt now drives the retry
                    retry()

                owned = [True]
                owned[0] = bool(prompt_unlock(self.window, on_done=_on_done))

            # A vault that isn't signed in can't be unlocked. Re-read that
            # daemon-owned state before deciding whether to show the prompt.
            def _probe():
                try:
                    needs_login = bool(controller.load_state().login_required)
                except Exception:
                    needs_login = False
                GLib.idle_add(lambda: (_after_probe(needs_login), False)[1])

            def _after_probe(needs_login):
                if needs_login:
                    retry()  # not signed in -> can't autofill; connect normally
                    return
                _show_unlock()

            threading.Thread(target=_probe, daemon=True).start()
            return True
        except Exception as exc:
            logger.error("Secret unlock prompt failed: %s", exc)
            return False

    def connect_to_host(
        self,
        connection,
        force_new: bool = False,
        remote_command: Optional[str] = None,
        tab_title: Optional[str] = None,
        force_tty: bool = False,
        pty_prompt: Optional[str] = None,
        pty_response: Optional[str] = None,
        _secret_unlock_attempted: bool = False,
    ):
        """Open an SSH terminal using an explicit route decided once up front.

        Phases:
        1. Resolve route (policy only — never readiness)
        2. External / legacy / daemon ownership branches
        3. Daemon readiness (+ bounded startup) without legacy fallthrough
        4. Construct terminal view and launch the selected backend only
        """

        from .daemon_terminal_policy import (
            SshTerminalRoute,
            resolve_ssh_terminal_route,
        )

        window = self.window
        if not force_new:
            if connection in window.active_terminals:
                terminal = window.active_terminals[connection]
                self._ensure_backend_alignment(terminal)
                page = window._page_for_child(terminal)
                if page is not None:
                    window.tab_view.set_selected_page(page)
                    return
                logger.warning(
                    f"Terminal for {connection.nickname} not found in tab view, "
                    "removing from active terminals"
                )
                del window.active_terminals[connection]
            existing_terms = window.connection_to_terminals.get(connection) or []
            for existing in reversed(existing_terms):
                page = window._page_for_child(existing)
                if page is not None:
                    self._ensure_backend_alignment(existing)
                    window.active_terminals[connection] = existing
                    window.tab_view.set_selected_page(page)
                    return

        route = resolve_ssh_terminal_route(window, connection)
        if route is None:
            route = SshTerminalRoute.DAEMON

        if route is SshTerminalRoute.EXTERNAL:
            self._open_external_ssh(
                connection,
                _secret_unlock_attempted=_secret_unlock_attempted,
            )
            return

        # Daemon route — never fall through to local SSH on readiness failure.
        self._open_daemon_ssh(
            connection,
            remote_command=remote_command,
            tab_title=tab_title,
            force_tty=force_tty,
            pty_prompt=pty_prompt,
            pty_response=pty_response,
            _secret_unlock_attempted=_secret_unlock_attempted,
        )

    def _open_external_ssh(self, connection, *, _secret_unlock_attempted: bool = False):
        """External-process-owned SSH — no internal tab, no daemon session."""

        window = self.window

        def _launch():
            window._open_connection_in_external_terminal(connection)

        # External terminals still use GTK-side askpass/secret policy when a
        # session-backed vault must be unlocked before the external spawn.
        if not _secret_unlock_attempted and self._maybe_unlock_secrets_then(
            lambda: self._open_external_ssh(connection, _secret_unlock_attempted=True)
        ):
            return
        _launch()

    def _open_daemon_ssh(
        self,
        connection,
        *,
        remote_command: Optional[str] = None,
        tab_title: Optional[str] = None,
        force_tty: bool = False,
        pty_prompt: Optional[str] = None,
        pty_response: Optional[str] = None,
        _secret_unlock_attempted: bool = False,
    ):
        """Daemon-owned SSH — no native_connect, VTE SSH spawn, or local askpass.

        GTK may unlock a session-backed *vault* after readiness succeeds so the
        daemon broker can read stored credentials; SSH secrets themselves stay
        daemon-owned and are never retrieved into GTK for this route.
        """

        from .daemon_terminal_policy import (
            daemon_readiness_user_message,
            resolve_daemon_terminal_readiness,
        )

        window = self.window
        readiness = self._ensure_daemon_terminal_ready()
        if not readiness.ready:
            self._show_daemon_error_dialog(
                window,
                daemon_readiness_user_message(readiness),
            )
            return

        # Backend unlock only — not SSH password/passphrase retrieval into GTK.
        # After unlock, the daemon interaction broker remains authoritative.
        if not _secret_unlock_attempted and self._maybe_unlock_secrets_then(
            lambda: self._open_daemon_ssh(
                connection,
                remote_command=remote_command,
                tab_title=tab_title,
                force_tty=force_tty,
                pty_prompt=pty_prompt,
                pty_response=pty_response,
                _secret_unlock_attempted=True,
            )
        ):
            return

        # Re-check after unlock: session may have closed / daemon dropped.
        readiness = resolve_daemon_terminal_readiness(
            getattr(window, "client", None),
            getattr(window, "client_bridge", None),
            selection_pending=bool(getattr(window, "_api_client_selection_pending", False)),
        )
        if not readiness.ready:
            self._show_daemon_error_dialog(
                window,
                daemon_readiness_user_message(readiness),
            )
            return

        terminal, _page = self._create_internal_terminal_tab(
            connection,
            tab_title=tab_title,
            pty_prompt=pty_prompt,
            pty_response=pty_response,
        )

        try:
            connection_id = connection_id_for(connection)
            terminal.start_daemon_session(
                window.client,
                window.client_bridge,
                connection_id,
                remote_command=remote_command,
                force_tty=force_tty,
            )
        except Exception as exc:
            logger.error(
                "Failed to initialize daemon session type=%s",
                type(exc).__name__,
            )
            self._unregister_terminal(connection, terminal)
            try:
                page = window._page_for_child(terminal)
                if page is not None:
                    window.tab_view.close_page(page)
            except Exception:
                pass
            self._show_daemon_error_dialog(
                window,
                _(
                    "The daemon-backed SSH session could not be started.\n"
                    "Check the daemon status and try again."
                ),
            )
            return

        def _apply_theme():
            if getattr(window, "_is_quitting", False):
                return
            try:
                terminal.apply_theme()
                terminal.queue_terminal_draw()
            except Exception as exc:
                logger.error("Error applying daemon terminal theme: %s", exc)

        GLib.idle_add(_apply_theme)

    def _ensure_daemon_terminal_ready(self):
        """Evaluate readiness; attempt one bounded on-demand daemon start if needed."""

        from .daemon_terminal_policy import (
            DaemonTerminalReadinessReason,
            resolve_daemon_terminal_readiness,
        )

        window = self.window
        pending = bool(getattr(window, "_api_client_selection_pending", False))
        readiness = resolve_daemon_terminal_readiness(
            getattr(window, "client", None),
            getattr(window, "client_bridge", None),
            selection_pending=pending,
        )
        if readiness.ready:
            return readiness
        if readiness.reason is DaemonTerminalReadinessReason.SELECTION_PENDING:
            return readiness
        if readiness.reason not in {
            DaemonTerminalReadinessReason.CLIENT_UNAVAILABLE,
            DaemonTerminalReadinessReason.HANDSHAKE_INCOMPLETE,
            DaemonTerminalReadinessReason.SERVER_INSTANCE_MISSING,
        }:
            return readiness

        startup_failure = self._try_start_daemon_client()
        return resolve_daemon_terminal_readiness(
            getattr(window, "client", None),
            getattr(window, "client_bridge", None),
            selection_pending=bool(getattr(window, "_api_client_selection_pending", False)),
            startup_failure=startup_failure,
        )

    def _try_start_daemon_client(self) -> Optional[object]:
        """Bounded reconnect/start. Returns a readiness reason on failure, else None."""

        from .daemon_terminal_policy import DaemonTerminalReadinessReason
        from .daemon.launcher import DaemonLaunchError, DaemonLauncher, DaemonStartupFailure
        from .gtk_client_bridge import GtkClientBridge

        window = self.window
        app = None
        try:
            app = window.get_application()
        except Exception:
            app = None

        bridge = getattr(window, "client_bridge", None)
        if bridge is None:
            bridge = getattr(app, "_api_client_bridge", None) if app is not None else None
        if bridge is None:
            try:
                bridge = GtkClientBridge()
            except Exception:
                return DaemonTerminalReadinessReason.BRIDGE_UNAVAILABLE
            window.client_bridge = bridge
            if app is not None:
                app._api_client_bridge = bridge

        launcher = getattr(app, "_api_daemon_launcher", None) if app else None
        if launcher is None:
            verbose = bool(getattr(app, "verbose_override", False)) if app else False
            quiet = bool(getattr(app, "quiet_override", False)) if app else False
            launcher = DaemonLauncher(verbose=verbose, quiet=quiet)
            if app is not None:
                app._api_daemon_launcher = launcher

        try:
            launched = launcher.connect_or_start()
        except DaemonLaunchError as error:
            if error.reason is DaemonStartupFailure.STARTUP_TIMEOUT:
                return DaemonTerminalReadinessReason.DAEMON_START_TIMEOUT
            return DaemonTerminalReadinessReason.DAEMON_START_FAILED
        except Exception:
            logger.error("On-demand daemon start failed", exc_info=True)
            return DaemonTerminalReadinessReason.DAEMON_START_FAILED

        previous = getattr(window, "client", None)
        window.client = launched.client
        if app is not None:
            from .api.client_factory import ClientSelection

            app._api_client_selection = ClientSelection(
                client=launched.client,
                daemon_process=launched.process,
            )
            if hasattr(app, "install_api_event_subscription"):
                try:
                    app.install_api_event_subscription(launched.client)
                except Exception:
                    logger.debug(
                        "Failed to install API event subscription after daemon start",
                        exc_info=True,
                    )
        if previous is not None and previous is not launched.client:
            try:
                previous.close()
            except Exception:
                pass
        return None

    def _create_internal_terminal_tab(
        self,
        connection,
        *,
        tab_title: Optional[str] = None,
        pty_prompt: Optional[str] = None,
        pty_response: Optional[str] = None,
    ):
        window = self.window
        group_color, group_name = self._resolve_group_color_and_name(connection)
        terminal = TerminalWidget(
            connection,
            window.config,
            window.connection_manager,
            group_color=group_color,
        )
        terminal._reconnect_handler = self.reconnect_terminal
        terminal.connect("connection-established", self.on_terminal_connected)
        terminal.connect(
            "connection-failed",
            lambda _w, error: logger.error(f"Connection failed: {error}"),
        )
        terminal.connect("connection-lost", self.on_terminal_disconnected)
        terminal.connect("title-changed", self.on_terminal_title_changed)

        if pty_prompt and pty_response is not None:
            terminal._pty_autofill = (pty_prompt, pty_response)

        from sshpilot import icon_utils

        page = window.tab_view.append(terminal)
        page.set_title(
            tab_title
            or getattr(connection, "display_name", None)
            or connection.nickname
        )
        page.set_icon(icon_utils.new_gicon_from_icon_name("utilities-terminal-symbolic"))
        if group_name:
            setattr(terminal, "group_name", group_name)
        self._apply_tab_group_color(page, group_color, tooltip=group_name)

        window.connection_to_terminals.setdefault(connection, []).append(terminal)
        window.terminal_to_connection[terminal] = connection
        window.active_terminals[connection] = terminal
        window.show_tab_view()
        window.tab_view.set_selected_page(page)
        return terminal, page

    def _unregister_terminal(self, connection, terminal) -> None:
        window = self.window
        try:
            if (
                connection in window.active_terminals
                and window.active_terminals[connection] is terminal
            ):
                del window.active_terminals[connection]
            window.terminal_to_connection.pop(terminal, None)
            terms = window.connection_to_terminals.get(connection)
            if terms and terminal in terms:
                terms.remove(terminal)
                if not terms:
                    del window.connection_to_terminals[connection]
        except Exception:
            pass

    def create_terminal_for_pane(self, connection, on_connected=None, on_disconnected=None):
        """
        Create and connect a TerminalWidget for use inside a SplitPane.

        Unlike connect_to_host(), this does NOT append the terminal to tab_view.
        The caller (SplitPane) embeds the returned widget in its own layout.

        Route is resolved once. Daemon readiness failure never falls through to
        local SSH spawn for a daemon-selected route.
        """
        from .daemon_terminal_policy import (
            daemon_readiness_user_message,
        )

        window = self.window
        # Split panes are internal renderers, so external preference still uses
        # the daemon rather than creating a second process-owning path.
        use_daemon = True
        if use_daemon:
            readiness = self._ensure_daemon_terminal_ready()
            if not readiness.ready:
                self._show_daemon_error_dialog(
                    window,
                    daemon_readiness_user_message(readiness),
                )
                # Return an inert widget rather than spawning local SSH.
                group_color, group_name = self._resolve_group_color_and_name(connection)
                terminal = TerminalWidget(
                    connection,
                    window.config,
                    window.connection_manager,
                    group_color=group_color,
                )
                terminal._reconnect_handler = self.reconnect_terminal
                if group_name:
                    setattr(terminal, "group_name", group_name)
                return terminal

        group_color, group_name = self._resolve_group_color_and_name(connection)

        terminal = TerminalWidget(
            connection,
            window.config,
            window.connection_manager,
            group_color=group_color,
        )
        terminal._reconnect_handler = self.reconnect_terminal
        terminal.connect("connection-established", self.on_terminal_connected)
        terminal.connect("connection-failed", lambda w, e: logger.error(f"Connection failed: {e}"))
        terminal.connect("connection-lost", self.on_terminal_disconnected)
        terminal.connect("title-changed", self._on_pane_terminal_title_changed)

        if on_connected:
            terminal.connect("connection-established", on_connected)
        if on_disconnected:
            terminal.connect("connection-lost", on_disconnected)

        if group_name:
            setattr(terminal, "group_name", group_name)

        window.connection_to_terminals.setdefault(connection, []).append(terminal)
        window.terminal_to_connection[terminal] = connection
        window.active_terminals[connection] = terminal

        def _set_terminal_colors():
            try:
                if use_daemon:
                    try:
                        connection_id = connection_id_for(connection)
                        terminal.start_daemon_session(
                            window.client,
                            window.client_bridge,
                            connection_id,
                        )
                        terminal.apply_theme()
                        terminal.queue_terminal_draw()
                    except Exception as exc:
                        logger.error(
                            "Failed to initialize daemon session for pane type=%s",
                            type(exc).__name__,
                        )
                        self._unregister_terminal(connection, terminal)
                    return

            except Exception as exc:
                logger.error(f"Error initialising pane terminal: {exc}")

        def _start_pane_connect():
            GLib.idle_add(_set_terminal_colors)

        _start_pane_connect()
        return terminal

    def _on_pane_terminal_title_changed(self, terminal, title):
        """Title-changed handler for terminals embedded in split panes."""
        # Title updates are reflected in the tab title via SplitViewTab._update_tab_title

    def _resolve_group_color(self, connection):
        color_value, _ = self._resolve_group_color_and_name(connection)
        return color_value

    def _resolve_group_color_and_name(self, connection):
        manager = getattr(self.window, "group_manager", None)
        if not manager:
            return None, None

        nickname = getattr(connection, "nickname", None)
        if not nickname:
            return None, None

        try:
            group_id = manager.get_connection_group(nickname)
        except Exception:
            group_id = None

        visited = set()
        while group_id:
            if group_id in visited:
                break
            visited.add(group_id)

            try:
                group_info = getattr(manager, "groups", {}).get(group_id)
            except Exception:
                group_info = None

            if not isinstance(group_info, dict):
                break

            color = group_info.get("color")
            if color:
                return color, group_info.get("name")

            group_id = group_info.get("parent_id")

        return None, None

    def _create_tab_color_icon(self, rgba: Gdk.RGBA):
        # Use a simple themed icon instead of creating custom icons
        # This avoids the pixbuf save issues and works reliably
        return Gio.ThemedIcon.new("tag-symbolic")

    def _apply_tab_group_color(self, page, color_value, tooltip=None):
        use_pref = False
        try:
            use_pref = bool(self.window.config.get_setting("ui.use_group_color_in_tab", False))
        except Exception:
            use_pref = False

        if not use_pref or not color_value:
            self._clear_tab_group_color(page)
            return

        tooltip_text = tooltip or _("Group color")

        rgba = Gdk.RGBA()
        try:
            if not rgba.parse(str(color_value)):
                self._clear_tab_group_color(page)
                return
        except Exception:
            self._clear_tab_group_color(page)
            return

        # Create a themed icon and apply color via CSS
        icon = self._create_tab_color_icon(rgba)
        if icon is None:
            self._clear_tab_group_color(page)
            return

        if hasattr(page, "set_indicator_icon"):
            try:
                page.set_indicator_icon(icon)
                if hasattr(page, "set_indicator_tooltip"):
                    page.set_indicator_tooltip(tooltip_text)

                # Apply color to the icon using CSS
                self._apply_tab_icon_color(page, rgba, tooltip_text)
                setattr(page, "_sshpilot_group_indicator_icon", icon)
                setattr(page, "_sshpilot_group_color", rgba.to_string())
            except Exception as exc:
                logger.debug("Failed to set tab indicator icon: %s", exc)
        elif hasattr(page, "set_icon") and hasattr(page, "get_icon"):
            try:
                if not hasattr(page, "_sshpilot_original_icon"):
                    setattr(page, "_sshpilot_original_icon", page.get_icon())
                page.set_icon(icon)

                if hasattr(page, "set_indicator_tooltip"):
                    page.set_indicator_tooltip(tooltip_text)

                # Apply color to the icon using CSS
                self._apply_tab_icon_color(page, rgba, tooltip_text)
                setattr(page, "_sshpilot_group_indicator_icon", icon)
                setattr(page, "_sshpilot_group_color", rgba.to_string())
            except Exception as exc:
                logger.debug("Failed to set tab icon for group color: %s", exc)

    def _clear_tab_group_color(self, page):
        if hasattr(page, "set_indicator_icon"):
            try:
                page.set_indicator_icon(None)
                if hasattr(page, "set_indicator_tooltip"):
                    page.set_indicator_tooltip(None)
            except Exception:
                pass
        elif hasattr(page, "set_icon") and hasattr(page, "_sshpilot_original_icon"):
            try:
                original = getattr(page, "_sshpilot_original_icon")
                if original is not None:
                    page.set_icon(original)
            except Exception:
                pass

        # Clear the custom icon from the tab page
        if hasattr(page, "set_indicator_icon"):
            try:
                page.set_indicator_icon(None)
                if hasattr(page, "set_indicator_tooltip"):
                    page.set_indicator_tooltip(None)
            except Exception:
                pass

        # No CSS provider to remove since we're using direct colored icons

        setattr(page, "_sshpilot_group_indicator_icon", None)
        if hasattr(page, "_sshpilot_group_color"):
            delattr(page, "_sshpilot_group_color")

    def _create_colored_tab_icon(self, rgba: Gdk.RGBA):
        """Create a simple colored icon for the tab indicator"""
        try:
            # Use a simple approach - create a basic colored icon
            # Convert RGBA to hex for CSS
            hex_color = (
                f"#{int(rgba.red * 255):02x}{int(rgba.green * 255):02x}{int(rgba.blue * 255):02x}"
            )

            # Create a tag SVG icon with the color
            # Based on tag-symbolic.svg but with the group color
            svg_data = f"""<svg xmlns="http://www.w3.org/2000/svg" height="16px" viewBox="0 0 16 16" width="16px">
                <path d="m 1 2 v 6 l 6.691406 6.691406 c 0.1875 0.1875 0.414063 0.203125 0.597656 0.019532 l 6.382813 -6.382813 c 0.222656 -0.222656 0.207031 -0.449219 0.019531 -0.636719 l -6.691406 -6.691406 h -6 c -0.464844 0 -1 0.492188 -1 1 z m 3 0.960938 c 0.589844 0 1.070312 0.480468 1.070312 1.070312 s -0.480468 1.070312 -1.070312 1.070312 s -1.070312 -0.480468 -1.070312 -1.070312 s 0.480468 -1.070312 1.070312 -1.070312 z m 0 0" fill="{hex_color}"/>
            </svg>"""

            # Create a bytes icon from the SVG
            svg_bytes = svg_data.encode("utf-8")
            return Gio.BytesIcon.new(GLib.Bytes.new(svg_bytes))
        except Exception as exc:
            logger.debug(f"Failed to create colored tab icon: {exc}")
            # Fallback to themed icon
            return Gio.ThemedIcon.new("tag-symbolic")

    def _apply_tab_css_color(self, page, rgba: Gdk.RGBA):
        """Apply CSS color to the tab view to color indicator icons"""
        try:
            # Get the tab view
            tab_view = None
            try:
                if hasattr(self.window, "tab_view"):
                    tab_view = self.window.tab_view
            except Exception:
                pass

            if not tab_view:
                return

            # Remove any existing CSS provider
            if hasattr(tab_view, "_sshpilot_tab_css_provider"):
                tab_view.get_style_context().remove_provider(tab_view._sshpilot_tab_css_provider)

            # Create new CSS provider with the group color
            provider = Gtk.CssProvider()
            color_value = rgba.to_string()

            # Use a simple CSS rule to color indicator icons
            css_data = f"tab page indicator-icon {{ color: {color_value}; }}"
            provider.load_from_data(css_data.encode())
            tab_view.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
            tab_view._sshpilot_tab_css_provider = provider

        except Exception as exc:
            logger.debug(f"Failed to apply tab CSS color: {exc}")

    def _apply_tab_icon_color(self, page, rgba: Gdk.RGBA, tooltip_text=None):
        """Apply color to tab icon by setting a colored icon"""
        try:
            # Create a colored icon
            icon = self._create_colored_tab_icon(rgba)
            if icon is None:
                self._clear_tab_group_color(page)
                return

            # Set the colored icon on the tab page
            page.set_indicator_icon(icon)
            page.set_indicator_activatable(True)
            if hasattr(page, "set_indicator_tooltip"):
                page.set_indicator_tooltip(tooltip_text or _("Group color"))

            # Store reference to the icon and color for cleanup
            setattr(page, "_sshpilot_group_indicator_icon", icon)
            setattr(page, "_sshpilot_group_color", rgba)
        except Exception as exc:
            logger.debug("Failed to apply tab icon color: %s", exc)
            self._clear_tab_group_color(page)

    def restyle_open_terminals(self):
        window = self.window
        n_pages = window.tab_view.get_n_pages() if hasattr(window.tab_view, "get_n_pages") else 0
        for index in range(n_pages):
            try:
                page = window.tab_view.get_nth_page(index)
            except Exception:
                continue
            if not page:
                continue

            terminal = page.get_child() if hasattr(page, "get_child") else None
            if terminal is None:
                continue

            connection = window.terminal_to_connection.get(terminal)
            if connection:
                color_value, group_name = self._resolve_group_color_and_name(connection)
                if group_name:
                    setattr(terminal, "group_name", group_name)
            else:
                color_value, group_name = None, None

            if hasattr(terminal, "set_group_color"):
                try:
                    terminal.set_group_color(color_value, force=True)
                except Exception as exc:
                    logger.debug("Failed to update terminal group color: %s", exc)

            tooltip = getattr(terminal, "group_name", None)
            self._apply_tab_group_color(page, color_value, tooltip=tooltip)

    def _on_disconnect_confirmed(self, dialog, response_id, connection):
        dialog.destroy()
        window = self.window
        if response_id == "disconnect" and connection in window.active_terminals:
            terminal = window.active_terminals[connection]
            terminal.disconnect()

    def disconnect_from_host(self, connection):
        window = self.window
        if connection not in window.active_terminals:
            return
        confirm_disconnect = window.config.get_setting("confirm-disconnect", True)
        if confirm_disconnect:
            host_value = getattr(
                connection,
                "hostname",
                getattr(connection, "host", getattr(connection, "nickname", "")),
            )
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=_("Disconnect from {host}").format(host=connection.nickname or host_value),
                body=_("Are you sure you want to disconnect from this host?"),
            )
            dialog.add_response("cancel", _("Cancel"))
            dialog.add_response("disconnect", _("Disconnect"))
            dialog.set_response_appearance("disconnect", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("close")
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_disconnect_confirmed, connection)
            dialog.present()
        else:
            terminal = window.active_terminals[connection]
            terminal.disconnect()

    # Tab management and local terminals
    def _add_terminal_tab(self, terminal_widget, title):
        try:
            from sshpilot import icon_utils

            page = self.window.tab_view.append(terminal_widget)
            page.set_title(title)
            page.set_icon(icon_utils.new_gicon_from_icon_name("utilities-terminal-symbolic"))
            self.window.show_tab_view()
            self.window.tab_view.set_selected_page(page)
            logger.info(f"Added terminal tab: {title}")
        except Exception as e:
            logger.error(f"Failed to add terminal tab: {e}")

    def show_local_terminal(
        self,
        *,
        title: Optional[str] = None,
        command: Optional[str] = None,
        pty_prompt: Optional[str] = None,
        pty_response: Optional[str] = None,
    ) -> bool:
        logger.info("Show local terminal tab")
        try:
            effective_title = title or _("Terminal")

            class LocalConnection:
                def __init__(self):
                    self.nickname = effective_title
                    self.hostname = "localhost"
                    self.host = self.hostname
                    self.username = os.getenv("USER", "user")
                    self.port = 22
                    self.is_connected = True

            local_connection = LocalConnection()
            terminal_widget = TerminalWidget(
                local_connection, self.window.config, self.window.connection_manager
            )
            if pty_prompt and pty_response is not None:
                terminal_widget._pty_autofill = (pty_prompt, pty_response)
            if command and str(command).strip():
                command_text = str(command).strip()

                def _run_command(*_args):
                    data = (command_text + "\n").encode("utf-8")
                    terminal_widget.feed_child_data(data)

                terminal_widget.connect("connection-established", _run_command)
            terminal_widget.setup_local_shell()
            self._add_terminal_tab(terminal_widget, effective_title)

            # Register terminal so theme/font updates affect existing local tabs
            window = self.window
            window.connection_to_terminals.setdefault(local_connection, []).append(terminal_widget)
            window.terminal_to_connection[terminal_widget] = local_connection
            window.active_terminals[local_connection] = terminal_widget

            GLib.idle_add(terminal_widget.show)
            # Show the terminal widget (backend-agnostic)
            GLib.idle_add(terminal_widget.show_terminal)

            def _focus_local_terminal():
                try:
                    if hasattr(terminal_widget, "get_mapped") and not terminal_widget.get_mapped():
                        return
                    terminal_widget.grab_terminal_focus()
                except Exception as focus_error:
                    logger.debug(f"Failed to focus local terminal: {focus_error}")

            # Use window's focus coordinator to avoid race conditions
            if hasattr(self.window, "_queue_focus_operation"):
                self.window._queue_focus_operation(_focus_local_terminal)
            else:
                # Fallback for older versions
                GLib.timeout_add(100, _focus_local_terminal)
            logger.info("Local terminal tab created successfully")
            return True
        except Exception as e:
            logger.error(f"Failed to show local terminal: {e}")
            try:
                dialog = Adw.MessageDialog(
                    transient_for=self.window,
                    modal=True,
                    heading=_("Error"),
                    body=_("Could not open local terminal.\n\n{error}").format(error=e),
                )
                dialog.add_response("ok", _("OK"))
                dialog.present()
            except Exception:
                pass
            return False

    # Terminal discovery (regular tabs + split-view panes)
    def _is_broadcastable_ssh_terminal(self, terminal) -> bool:
        if terminal is None:
            return False
        if not (hasattr(terminal, "backend") or hasattr(terminal, "vte")):
            return False
        connection = getattr(terminal, "connection", None)
        if connection is None:
            return False
        try:
            if terminal._is_local_terminal():
                return False
        except Exception:
            pass
        return hasattr(connection, "hostname")

    def iter_terminals(self):
        """Yield TerminalWidgets from regular tabs and split-view panes."""
        from .split_view import SplitViewTab

        tab_view = getattr(self.window, "tab_view", None)
        if tab_view is None:
            return

        try:
            n_pages = tab_view.get_n_pages()
        except Exception:
            return

        for i in range(n_pages):
            try:
                page = tab_view.get_nth_page(i)
            except Exception:
                continue
            if page is None:
                continue
            try:
                child = page.get_child()
            except Exception:
                child = None
            if child is None:
                continue

            if isinstance(child, TerminalWidget):
                yield child
            elif isinstance(child, SplitViewTab):
                yield from child.get_all_terminals()

    def iter_ssh_terminals(self):
        """Yield SSH TerminalWidgets from regular tabs and split-view panes."""
        for terminal in self.iter_terminals():
            if self._is_broadcastable_ssh_terminal(terminal):
                yield terminal

    def get_focused_terminal(self):
        """Return the focused TerminalWidget from the selected main tab, if any."""
        from .split_view import SplitViewTab

        tab_view = getattr(self.window, "tab_view", None)
        if tab_view is None:
            return None

        try:
            page = tab_view.get_selected_page()
        except Exception:
            return None
        if page is None:
            return None

        try:
            child = page.get_child()
        except Exception:
            return None

        if isinstance(child, TerminalWidget):
            return child
        if isinstance(child, SplitViewTab):
            return child.get_focused_terminal()
        return None

    # Broadcast commands
    def broadcast_connection_ids(self):
        """Return unique saved SSH targets in visible display order."""
        result = []
        seen = set()
        for terminal_widget in self.iter_ssh_terminals():
            connection = terminal_widget.connection
            try:
                connection_id = connection_id_for(connection)
            except Exception:
                continue
            if connection_id not in seen:
                seen.add(connection_id)
                result.append(connection_id)
        return tuple(result)

    def broadcast_session_ids(self):
        """Return unique daemon session IDs from open SSH terminals."""
        result = []
        seen = set()
        for terminal_widget in self.iter_ssh_terminals():
            if not getattr(terminal_widget, "is_daemon_backed", False):
                continue
            tab_state = getattr(terminal_widget, "daemon_tab_state", None)
            session_id = getattr(tab_state, "session_id", None)
            if session_id is not None and session_id not in seen:
                seen.add(session_id)
                result.append(session_id)
        return tuple(result)

    def broadcast_command(self, command: str, *, on_success=None, on_error=None):
        """Write a command to the existing daemon-backed terminal sessions."""
        from .api.models.terminal import BroadcastTerminalInputRequest

        session_ids = self.broadcast_session_ids()
        if not command.strip() or not session_ids:
            return None
        client = getattr(self.window, "client", None)
        bridge = getattr(self.window, "client_bridge", None)
        if client is None or bridge is None:
            if on_error:
                on_error(RuntimeError("Daemon broadcast capability unavailable"))
            return None
        request = BroadcastTerminalInputRequest(session_ids, command)
        return bridge.submit(
            lambda: client.broadcast_terminal_input(request),
            on_success=on_success or (lambda _result: None),
            on_error=on_error
            or (lambda error: logger.error("Broadcast request failed: %s", error)),
        )

    def run_command_on_connections(self, command: str, *, on_success=None, on_error=None):
        """Submit one-shot command execution for saved connection targets."""
        from .api.models.broadcast import BroadcastCommandRequest

        connection_ids = self.broadcast_connection_ids()
        if not command.strip() or not connection_ids:
            return None
        client = getattr(self.window, "client", None)
        bridge = getattr(self.window, "client_bridge", None)
        if client is None or bridge is None:
            if on_error:
                on_error(RuntimeError("Daemon broadcast capability unavailable"))
            return None
        request = BroadcastCommandRequest(connection_ids, command)
        return bridge.submit(
            lambda: client.start_broadcast_command(request),
            on_success=on_success or (lambda _summary: None),
            on_error=on_error
            or (lambda error: logger.error("Broadcast command request failed: %s", error)),
        )

    def _show_daemon_error_dialog(self, window, message):
        """Show error dialog for daemon terminal failures (no automatic fallback)."""
        try:
            dialog = Adw.AlertDialog.new(
                _("Daemon Terminal Unavailable"),
                message,
            )
            dialog.add_response("ok", _("OK"))
            dialog.add_response("prefs", _("Open Preferences"))
            dialog.set_response_appearance("prefs", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")

            def _respond(_dialog, response):
                if response != "prefs":
                    return
                try:
                    app = window.get_application()
                    if app is not None and hasattr(app, "on_preferences_action"):
                        app.on_preferences_action(None, None)
                    elif hasattr(window, "on_preferences_action"):
                        window.on_preferences_action(None, None)
                except Exception:
                    logger.debug(
                        "Failed to open preferences from daemon error dialog",
                        exc_info=True,
                    )

            dialog.connect("response", _respond)
            dialog.present(window)
        except Exception as e:
            logger.error(f"Failed to show daemon error dialog: {e}")

    # Terminal signal handlers
    def on_terminal_connected(self, terminal):
        # Aggregate across all terminals of this connection (OR-logic) and set
        # the authoritative state in the reporting layer instead of writing the
        # boolean here.
        self.window._recompute_connection_state(terminal.connection)

        # Stamp last-used time so the start page can order Recent connections.
        try:
            import time

            nickname = getattr(terminal.connection, "nickname", None)
            if nickname:
                self.window.client.update_connection_metadata(nickname, {"last_used": time.time()})
                welcome = getattr(self.window, "welcome_view", None)
                if welcome is not None and hasattr(welcome, "refresh_recent"):
                    welcome.refresh_recent()
        except Exception:
            logger.debug("Failed to record last-used time", exc_info=True)
        for row in self.window._rows_for_connection(terminal.connection):
            row.update_status()
            row.queue_draw()

        if hasattr(self.window, "_hide_reconnecting_message"):
            GLib.idle_add(self.window._hide_reconnecting_message)

        self.window._is_controlled_reconnect = False
        if not getattr(self.window, "_is_controlled_reconnect", False):
            host_value = getattr(
                terminal.connection,
                "hostname",
                getattr(terminal.connection, "host", getattr(terminal.connection, "nickname", "")),
            )
            logger.info(
                f"Terminal connected: {terminal.connection.nickname} ({terminal.connection.username}@{host_value})"
            )
        else:
            logger.debug(
                f"Terminal reconnected after settings update: {terminal.connection.nickname}"
            )

        # Notify plugins (session_opened). The host dedupes reconnects by
        # terminal identity, so this is safe to call on every connect.
        try:
            host = getattr(self.window, "plugin_host", None)
            if host is not None:
                host.dispatch_session_opened(terminal)
        except Exception:
            logger.exception("Plugin session_opened dispatch failed")

        self._maybe_offer_save_connection(terminal)

    def _maybe_offer_save_connection(self, terminal):
        """After a successful CLI/ad-hoc connect, offer to save if the host is unsaved."""
        try:
            connection = getattr(terminal, "connection", None)
            if connection is None:
                return
            data = getattr(connection, "data", None) or {}
            from .cli_connect import CLI_CONNECT_FLAG

            if not data.get(CLI_CONNECT_FLAG):
                return

            from .api.connection_identity import connection_id_for
            from .api.models.connections import UnsavedHostCheckRequest
            from .unsaved_host import SavePromptDismissals, connection_destination

            window = self.window
            app = window.get_application() if hasattr(window, "get_application") else None
            dismissals = getattr(app, "save_prompt_dismissals", None) if app else None
            if dismissals is None:
                dismissals = SavePromptDismissals()
                if app is not None:
                    app.save_prompt_dismissals = dismissals

            hostname, username = connection_destination(connection)
            if dismissals.is_dismissed(hostname, username):
                return
            if (getattr(connection, "protocol", "ssh") or "ssh") != "ssh":
                return

            bridge = getattr(window, "client_bridge", None)
            client = getattr(window, "client", None)
            if bridge is None or client is None:
                window._show_daemon_unavailable_dialog()
                return
            # A CLI-created projection receives ``id == nickname`` for UI
            # compatibility, but that is not a durable daemon identity.  Do
            # not let a hostname-shaped nickname short-circuit the
            # authoritative host/user comparison.
            if data.get(CLI_CONNECT_FLAG):
                stored_id = None
            else:
                try:
                    stored_id = connection_id_for(connection)
                except Exception:
                    stored_id = None

            request = UnsavedHostCheckRequest(
                hostname=hostname,
                username=username,
                connection_id=stored_id,
                port=(
                    int(getattr(connection, "port", 22) or 22)
                    if bool(data.get("port_explicit"))
                    else None
                ),
                protocol=str(getattr(connection, "protocol", "ssh") or "ssh"),
                proxy_jump=tuple(getattr(connection, "proxy_jump", ()) or ()),
            )

            def _after_check(result):
                if result.saved:
                    return

                def _on_save(term):
                    try:
                        window.show_connection_dialog(term.connection, as_new=True)
                    except Exception:
                        logger.debug("Failed to open save connection dialog", exc_info=True)

                def _on_dismiss(term):
                    try:
                        dismissals.dismiss_connection(term.connection)
                    except Exception:
                        logger.debug("Failed to record save-prompt dismissal", exc_info=True)

                if hasattr(terminal, "show_save_connection_prompt"):
                    # Delay the reveal so the revealer is mapped by the time it
                    # fires; otherwise GTK skips the slide-up animation.
                    from gi.repository import GLib

                    def _reveal():
                        terminal.show_save_connection_prompt(
                            on_save=_on_save, on_dismiss=_on_dismiss
                        )
                        return False

                    GLib.timeout_add(600, _reveal)

            bridge.submit(
                lambda: client.check_unsaved_host(request),
                on_success=_after_check,
                on_error=lambda error: logger.info(
                    "Optional unsaved-host check skipped after daemon error: %s", error
                ),
            )
        except Exception:
            logger.debug("Save-connection offer skipped", exc_info=True)

    def on_terminal_disconnected(self, terminal):
        # The just-disconnected terminal has already flipped its own
        # is_connected flag; recompute the connection's aggregate state so a
        # connection with other live terminals stays CONNECTED.
        self.window._recompute_connection_state(terminal.connection)
        for row in self.window._rows_for_connection(terminal.connection):
            row.update_status()
            row.queue_draw()
        host_value = getattr(
            terminal.connection,
            "hostname",
            getattr(terminal.connection, "host", getattr(terminal.connection, "nickname", "")),
        )
        logger.info(
            f"Terminal disconnected: {terminal.connection.nickname} ({terminal.connection.username}@{host_value})"
        )

        # Notify plugins (session_closed).
        try:
            host = getattr(self.window, "plugin_host", None)
            if host is not None:
                host.dispatch_session_closed(terminal)
        except Exception:
            logger.exception("Plugin session_closed dispatch failed")

    def on_terminal_title_changed(self, terminal, title):
        page = self.window._page_for_child(terminal)
        if page:
            if getattr(page, "custom_tab_title", None):
                return
            if title and title != terminal.connection.nickname:
                page.set_title(
                    _("{nickname} - {title}").format(
                        nickname=terminal.connection.nickname, title=title
                    )
                )
            else:
                page.set_title(terminal.connection.nickname)

    def get_terminal_job_status(self, terminal):
        """
        Get the job status of a terminal.
        Only works for local terminals.

        Args:
            terminal: TerminalWidget instance

        Returns:
            dict: Job status information including idle state and status string
        """
        if not hasattr(terminal, "is_terminal_idle"):
            return {
                "is_idle": False,
                "has_active_job": False,
                "status": "UNKNOWN",
                "error": "Terminal does not support job detection",
            }

        try:
            is_idle = terminal.is_terminal_idle()
            has_active_job = terminal.has_active_job()
            status = terminal.get_job_status()

            # Check if this is an SSH terminal
            is_ssh = status == "SSH_TERMINAL"

            return {
                "is_idle": is_idle,
                "has_active_job": has_active_job,
                "status": status,
                "is_ssh_terminal": is_ssh,
                "error": None,
            }
        except Exception as e:
            return {
                "is_idle": False,
                "has_active_job": False,
                "status": "ERROR",
                "is_ssh_terminal": False,
                "error": str(e),
            }

    def get_all_terminal_job_statuses(self):
        """
        Get job status for all active terminals.
        Only local terminals will have meaningful job detection.

        Returns:
            list: List of dicts with terminal info and job status
        """
        statuses = []

        for terminal_widget in self.iter_terminals():
            if not (hasattr(terminal_widget, "backend") or hasattr(terminal_widget, "vte")):
                continue

            connection_info = (
                terminal_widget.get_connection_info()
                if hasattr(terminal_widget, "get_connection_info")
                else None
            )
            job_status = self.get_terminal_job_status(terminal_widget)
            connection = getattr(terminal_widget, "connection", None)
            page_title = getattr(connection, "nickname", None) or _("Terminal")

            statuses.append(
                {
                    "page_title": page_title,
                    "connection_info": connection_info,
                    "job_status": job_status,
                }
            )

        return statuses

    # --- Controlled reconnect after a connection edit ----------------------

    def prompt_reconnect(self, connection):
        """Ask whether to reconnect *connection* after a connection save."""
        dialog = Adw.AlertDialog(
            heading=_("Reconnect?"),
            body=_("If you changed settings, you can reconnect to apply them."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("reconnect", _("Reconnect"))
        dialog.set_response_appearance("reconnect", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("reconnect")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_reconnect_response, connection)
        dialog.present(self.window)

    def _on_reconnect_response(self, dialog, response, connection):
        """Handle response from the reconnect prompt."""
        window = self.window
        # Only proceed if the user confirmed and the connection is still active
        if response != "reconnect" or connection not in window.active_terminals:
            return

        terminal = window.active_terminals.get(connection)
        if not terminal:
            logger.warning("No terminal instance found for reconnection")
            return

        # Ensure the tab for this connection is focused so the user can
        # observe the reconnection process even if another tab was
        # previously active.
        try:
            window._focus_most_recent_tab(connection)
        except Exception:
            pass

        # Set controlled reconnect flag (read by terminal.py via the root window)
        window._is_controlled_reconnect = True

        try:
            # The reconnect helper terminates the old daemon controller and
            # opens its replacement.  Do not call TerminalWidget.disconnect()
            # first: its normal tab-close policy may detach or prompt, neither
            # of which is appropriate after the user explicitly confirmed a
            # settings-change reconnect.
            logger.debug("Scheduling daemon terminal reconnection")

            def _safe_reconnect():
                try:
                    self._reconnect_terminal(connection)
                except Exception as e:
                    logger.error(f"Error during daemon reconnect: {e}")
                    GLib.idle_add(self._show_reconnect_error, connection, str(e))
                return False

            GLib.idle_add(_safe_reconnect)

        except Exception as e:
            logger.error(f"Error during reconnection: {e}")
            # Remove from active terminals if reconnection fails
            if connection in window.active_terminals:
                del window.active_terminals[connection]
            window._error_dialog(
                _("Reconnection Failed"),
                _(
                    "Failed to reconnect with the new settings. Please try connecting again manually."
                ),
            )

        finally:
            # Reset the flag after a delay to ensure it's not set during normal operations
            GLib.timeout_add(1000, self._reset_controlled_reconnect)

    def _reset_controlled_reconnect(self):
        """Reset the controlled reconnect flag"""
        self.window._is_controlled_reconnect = False

    def _reconnect_terminal(self, connection):
        """Reconnect a terminal with updated connection settings"""
        window = self.window
        if connection not in window.active_terminals:
            logger.warning(f"Connection {connection.nickname} not found in active terminals")
            return False  # Don't repeat the timeout

        terminal = window.active_terminals[connection]

        try:
            logger.debug(f"Attempting to reconnect terminal for {connection.nickname}")

            if not self.reconnect_terminal(terminal):
                logger.error("Failed to reconnect with new settings")
                GLib.idle_add(self._show_reconnect_error, connection)
                return False

            logger.info(f"Successfully reconnected terminal for {connection.nickname}")

        except Exception as e:
            logger.error(f"Error reconnecting terminal: {e}", exc_info=True)
            GLib.idle_add(self._show_reconnect_error, connection, str(e))

        return False  # Don't repeat the timeout

    def reconnect_terminal(self, terminal) -> bool:
        """Open a fresh daemon session in an existing terminal widget."""
        from .daemon_terminal_policy import (
            daemon_readiness_user_message,
        )

        window = self.window
        connection = getattr(terminal, "connection", None)
        if connection is None:
            logger.error("Cannot reconnect terminal without a connection")
            return False

        readiness = self._ensure_daemon_terminal_ready()
        if not readiness.ready:
            message = daemon_readiness_user_message(readiness)
            logger.error("Daemon terminal reconnect unavailable: %s", message)
            return False

        old_controller = getattr(terminal, "_daemon_controller", None)
        if old_controller is not None:
            try:
                # Retire widget callbacks before the asynchronous close.  A
                # transport error while closing the old session must not mark
                # its freshly opened replacement as failed.
                old_controller._on_output = None
                old_controller._on_continuity_lost = None
                old_controller._on_state_changed = None
                old_controller._on_error = lambda error: logger.debug(
                    "Retired daemon controller close error: %s", error
                )
                old_controller.close()
            except Exception:
                logger.debug(
                    "Failed to close previous daemon session during reconnect",
                    exc_info=True,
                )

        terminal._uninstall_daemon_backend_io()
        terminal._daemon_mode = False
        terminal._daemon_controller = None
        terminal._daemon_tab_state = None
        terminal._hide_view_only_indicator()
        terminal.last_error_message = None
        terminal.connection_state_reason = ""
        terminal._set_disconnected_banner_visible(False)
        terminal._set_connecting_overlay_visible(True)

        # The widget (and its emulator) is reused for the replacement session,
        # so DEC private modes the previous remote left set would be inherited:
        # mouse tracking, bracketed paste, the alternate screen.  A stuck
        # mouse-tracking mode routes drags to the remote instead of selecting,
        # which makes copy silently do nothing for the rest of the tab's life
        # (GH #1178).  Reset without clearing history so the tab keeps its
        # scrollback on backends that can preserve it.
        try:
            backend = getattr(terminal, "backend", None)
            if backend is not None:
                backend.reset(False, False)
        except Exception:
            logger.debug(
                "Failed to reset emulator state before reconnect", exc_info=True
            )

        try:
            # The save flow re-keys every terminal from the pre-save snapshot
            # to the authoritative post-save ConnectionSummary, so the id
            # derived here is exactly the alias the daemon holds after a
            # rename.
            started = terminal.start_daemon_session(
                window.client,
                window.client_bridge,
                connection_id_for(connection),
            )
        except Exception:
            logger.exception("Failed to start replacement daemon terminal session")
            started = False

        if not started:
            terminal._set_connecting_overlay_visible(False)
            return False

        try:
            terminal.apply_theme()
            terminal.queue_terminal_draw()
        except Exception:
            logger.debug("Failed to refresh reconnected terminal theme", exc_info=True)
        return True

    def _show_reconnect_error(self, connection, error_message=None):
        """Show an error message when reconnection fails"""
        window = self.window
        # Remove from active terminals if reconnection fails
        if connection in window.active_terminals:
            del window.active_terminals[connection]

        # Update UI to show disconnected state
        for row in window._rows_for_connection(connection):
            row.update_status()

        window._error_dialog(
            _("Reconnection Failed"),
            error_message
            or _(
                "Failed to reconnect with the new settings. Please try connecting again manually."
            ),
        )
