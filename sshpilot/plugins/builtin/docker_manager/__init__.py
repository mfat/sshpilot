"""Docker Console plugin.

A management dashboard for the Docker/Podman daemon running on a host you reach
over SSH. It does NOT talk to a daemon socket or use the docker SDK — every
operation runs the ``docker``/``podman`` CLI on the host through the app's single
native SSH path (``ctx.run_command``) and parses ``--format '{{json .}}'`` output
with stdlib json. Streamed/interactive output (live logs, exec shell) opens a
terminal tab via ``ctx.open_command_terminal`` (API >= 1.6).

Sibling of the ``docker`` *protocol* plugin (which is a per-container connection
type); this one is the management surface: lifecycle, logs, exec, stats, and
image/volume cleanup.
"""

from __future__ import annotations

import logging

from ...api import PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

_ICON = "brand-docker-symbolic"


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
                self.ctx.ui.register_page(
                    page_id, f"Docker — {nickname}", _ICON,
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
        # No host context from the Tools menu → use the last-used host, else the
        # first SSH connection.
        nick = self.ctx.settings.get("last_host", None)
        if not nick:
            try:
                conns = [c for c in self.ctx.list_connections()
                         if getattr(c, "protocol", "ssh") in ("ssh", "", None)]
            except Exception:
                conns = []
            nick = conns[0].nickname if conns else None
        if nick:
            self._open_host_page(nick)
        else:
            self.ctx.ui.notify("No SSH connections to manage Docker on")
