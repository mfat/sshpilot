"""Docker Manager plugin.

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

_PAGE_ID = "manager"
_ICON = "package-x-generic-symbolic"


class Plugin(SshPilotPlugin):
    # activate() is registration only — the window UI does not exist yet, so the
    # page is built lazily by the factory when first opened.
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._page = None
        # ctx.ui is None in headless contexts (no main window / UI host); this is
        # a UI-only plugin, so there is simply nothing to register there.
        if getattr(ctx, "ui", None) is None:
            return
        ctx.ui.register_page(_PAGE_ID, "Docker Manager", _ICON, self._build_page)
        # Right-click a connection → open Docker Manager targeting that host
        # (API >= 1.7). Older cores simply won't have this method.
        register_action = getattr(ctx.ui, "register_connection_action", None)
        if callable(register_action):
            register_action("open", "Docker Manager", _ICON, self._on_connection_action)

    def _build_page(self):
        from .page import DockerManagerPage

        self._page = DockerManagerPage(self.ctx)
        return self._page

    def _on_connection_action(self, nickname: str) -> None:
        # Open (or focus) the page, then point it at the right-clicked host.
        self.ctx.ui.open_page(_PAGE_ID)
        page = getattr(self, "_page", None)
        if page is not None:
            try:
                page.select_host(nickname)
            except Exception:
                logger.debug("select_host(%r) failed", nickname, exc_info=True)
