"""Non-protocol sshPilot plugin template — a page that reacts to events.

The smallest useful event+UI plugin: it adds a page showing how many connections
have been created since launch, updating live as `CONNECTION_CREATED` fires, and
persists the running total per-plugin. Copy this directory, rename it and the
`id` in `plugin.json`, and replace the page/handler with your own.

Patterns shown (the conventions the official non-protocol plugins follow):
* `activate()` is registration-only (no live UI here)
* a UI page via `ctx.ui.register_page` with a lazily-imported `gi`
* an event subscription via `ctx.events` that updates the page
* per-plugin persisted settings via `ctx.settings`
* pure logic (`next_count`) kept GTK-free so it's unit-testable

See ../writing-plugins.md ▸ "Event-driven & UI plugins" for the full guide, and
../../../plugins/ for richer worked examples (auto-group, notes, health).
"""

from __future__ import annotations

import logging

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)


# --- pure logic (no GTK; unit-testable) -------------------------------------

def next_count(current: int) -> int:
    """Increment a non-negative counter, tolerating junk read back from JSON."""
    try:
        return max(0, int(current)) + 1
    except (TypeError, ValueError):
        return 1


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._count = 0  # session-local; persisted total lives in settings
        self._label = None
        ctx.ui.register_page(
            "home", "Template", "applications-system-symbolic", self._build_page)
        ctx.events.subscribe(Events.CONNECTION_CREATED, self._on_connection_created)

    def deactivate(self) -> None:
        logger.info("ui-template: deactivate")

    def _on_connection_created(self, _info) -> None:
        self._count = next_count(self._count)
        total = next_count(self.ctx.settings.get("total_created", 0))
        self.ctx.settings.set("total_created", total)
        self._refresh()

    # --- UI (gi imported lazily; only runs inside the app) ----------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)
        title = Gtk.Label(label="UI Template")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)
        self._label = Gtk.Label(xalign=0)
        box.append(self._label)
        self._refresh()
        return box

    def _refresh(self) -> None:
        if self._label is None:
            return
        total = self.ctx.settings.get("total_created", 0)
        self._label.set_label(
            f"Connections created this session: {self._count}\n"
            f"Total ever (persisted): {total}")
