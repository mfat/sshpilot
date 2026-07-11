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
        # Tools-menu "Docker Console" → open the last-used host's tab. The page
        # itself is never opened directly; on_activate handles the click.
        ctx.ui.register_page("manager", "Docker Console", _ICON, lambda: None,
                             on_activate=self._open_last_host)
        # Right-click a connection → a per-host Docker Console tab (API >= 1.7).
        register_action = getattr(ctx.ui, "register_connection_action", None)
        if callable(register_action):
            register_action("open", "Docker Console", _ICON, self._open_host_page)

    # --- one tab per host (reused if already open) ---------------------
    def _open_host_page(self, nickname: str) -> None:
        if not nickname:
            return
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

    def _open_last_host(self) -> None:
        # No host context from the Tools menu → use the last-used target, with
        # Local always available even when there are no saved SSH connections.
        nick = self.ctx.settings.get("last_host", None)
        if nick and nick != _LOCAL_TARGET:
            try:
                known = {
                    connection.nickname for connection in self.ctx.list_connections()
                    if getattr(connection, "protocol", "ssh") in ("ssh", "", None)
                }
            except Exception:
                known = set()
            if nick not in known:
                nick = None
        self._open_host_page(nick or _LOCAL_TARGET)
