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
_ICON = "brand-docker-symbolic"


class Plugin(SshPilotPlugin):
    # activate() is registration only — the window UI does not exist yet, so the
    # page is built lazily by the factory when first opened.
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._page = None
        self._target_host = None  # host to focus when the page is next built
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

        # Pass the target host so a freshly built page targets it from the start
        # (its single map-time load uses it) — no default-host load racing it.
        self._page = DockerManagerPage(self.ctx, initial_host=self._target_host)
        return self._page

    def _on_connection_action(self, nickname: str) -> None:
        # Open (or focus) the Docker Manager targeting the right-clicked host.
        self._target_host = nickname
        page_before = getattr(self, "_page", None)
        try:
            self.ctx.ui.open_page(_PAGE_ID)
            page = getattr(self, "_page", None)
            # Fresh page → the factory built it with initial_host and its map-time
            # load handles it. Reused (already-open) page → the factory didn't
            # run, so re-point + reload it now.
            if page is not None and page is page_before:
                page.switch_host(nickname)
        except Exception:
            logger.debug("open Docker Manager for %r failed", nickname, exc_info=True)
        finally:
            self._target_host = None
