"""Docker Console plugin.

A management dashboard for the local Docker/Podman daemon or one reached over
SSH. It does NOT talk to a daemon socket or use the docker SDK — every operation
runs the ``docker``/``podman`` CLI through the plugin command APIs and parses
``--format '{{json .}}'`` output with stdlib json. Remote targets retain the
app's single native SSH/auth path.

Sibling of the ``docker`` *protocol* plugin (which is a per-container connection
type); this one is the management surface: lifecycle, logs, exec, stats, and
image/volume cleanup.
"""

from __future__ import annotations

import logging

from ...api import PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

_ICON = "brand-docker-symbolic"
_LOCAL_TARGET = "__local__"
_LOCAL_TITLE = "Local"


class Plugin(SshPilotPlugin):
    # activate() is registration only — the window UI does not exist yet, so each
    # per-host page is built lazily by its factory when first opened.
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._host_pages: set[str] = set()  # nicknames with a registered page
        # ctx.ui is None in headless contexts (no main window / UI host); this is
        # a UI-only plugin, so there is simply nothing to register there.
        if getattr(ctx, "ui", None) is None:
            return
        # Tools-menu "Docker Console" → sidebar selection, else Local. The page
        # itself is never opened directly; on_activate handles the click.
        ctx.ui.register_page("manager", "Docker Console", _ICON, lambda: None,
                             on_activate=self._open_from_tools)
        # Right-click a connection → a per-host Docker Console tab (API >= 1.7).
        register_action = getattr(ctx.ui, "register_connection_action", None)
        if callable(register_action):
            register_action("open", "Docker Console", _ICON, self._open_host_page)

    # --- one tab per host (reused if already open) ---------------------
    def _open_host_page(self, nickname: str) -> None:
        nickname = nickname or _LOCAL_TARGET
        page_id = f"host-{nickname}"
        if page_id not in self._host_pages:
            try:
                display_name = _LOCAL_TITLE if nickname == _LOCAL_TARGET else nickname
                self.ctx.ui.register_page(
                    page_id, f"Docker — {display_name}", _ICON,
                    lambda nk=nickname: self._build_host_page(nk),
                    add_menu_item=False,
                )
                self._host_pages.add(page_id)
            except Exception:
                logger.debug("register per-host docker page for %r failed",
                             nickname, exc_info=True)
        self.ctx.ui.open_page(page_id)

    def _build_host_page(self, nickname: str):
        from .page import DockerConsolePage

        return DockerConsolePage(self.ctx, initial_host=nickname)

    def _sidebar_ssh_nickname(self):
        """Nickname of the sidebar's selected SSH connection, if any."""
        host = getattr(self.ctx, "_host", None)
        window = getattr(host, "_window", None) if host is not None else None
        if window is None:
            return None
        try:
            row = window.connection_list.get_selected_row()
            conn = getattr(row, "connection", None) if row else None
            if conn is None:
                return None
            if getattr(conn, "protocol", "ssh") not in ("ssh", "", None):
                return None
            return getattr(conn, "nickname", None) or None
        except Exception:
            return None

    def _open_from_tools(self) -> None:
        # Tools / welcome have no explicit host: use the sidebar selection when
        # one is selected, otherwise start on the local Docker daemon.
        self._open_host_page(self._sidebar_ssh_nickname() or _LOCAL_TARGET)
