"""Mock VPS provider — the worked example for the sshPilot plugin SDK.

It exercises *every* public capability a provider plugin needs, end to end,
against a fake provider (no real network calls), so you can read one file and
see the whole flow:

* a custom "Deploy VPS" page (``ctx.ui.register_page`` → opens as a tab)
* reacting to application events (``ctx.events.subscribe``)
* per-plugin settings and secrets (``ctx.settings`` / ``ctx.secrets``)
* background provisioning off the UI thread + marshalling back
  (``threading`` + ``ctx.run_on_ui_thread``)
* generating an SSH key (``ctx.generate_key``)
* creating a connection (``ctx.add_connection``) and opening a terminal for it
  (``ctx.open_connection``)
* transient notifications (``ctx.ui.notify``)

The ONLY sshPilot import is ``sshpilot.plugins.api`` — the public surface.

To try it: copy this directory to ``~/.local/share/sshpilot/plugins/mock-vps``
(Flatpak: ``~/.var/app/io.github.mfat.sshpilot/data/sshpilot/plugins/mock-vps``),
enable "Mock VPS Provider" in Preferences ▸ Plugins, and restart sshPilot.
"""

from __future__ import annotations

import logging
import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

from sshpilot.plugins.api import (  # noqa: E402
    Events,
    PluginContext,
    SshPilotPlugin,
)

logger = logging.getLogger(__name__)

REGIONS = [("fra1", "Frankfurt"), ("nyc1", "New York"), ("sgp1", "Singapore")]


class Plugin(SshPilotPlugin):
    # ------------------------------------------------------------------
    # activate() is REGISTRATION ONLY: no live UI here, because the window
    # UI does not exist yet. Live work happens from event/UI callbacks,
    # which only fire after app_started.
    # ------------------------------------------------------------------
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._status_label = None

        ctx.ui.register_page(
            "deploy", "Deploy VPS", "network-server-symbolic", self._build_page
        )

        ctx.events.subscribe(Events.APP_STARTED, self._on_app_started)
        ctx.events.subscribe(Events.CONNECTION_CREATED, self._on_connection_created)
        ctx.events.subscribe(Events.SESSION_OPENED, self._on_session_opened)
        ctx.events.subscribe(Events.APP_SHUTDOWN, self._on_app_shutdown)

        # Last-used region, persisted per-plugin (namespaced in app config).
        self._region = ctx.settings.get("region", "fra1")

    def deactivate(self) -> None:
        logger.info("mock-vps: deactivate")

    # --- event handlers (main thread) ---------------------------------
    def _on_app_started(self, _payload) -> None:
        self.ctx.ui.notify("Mock VPS provider ready")

    def _on_connection_created(self, info) -> None:
        # info is a read-only ConnectionInfo snapshot.
        logger.info("mock-vps: connection created: %s (%s)", info.nickname, info.host)

    def _on_session_opened(self, info) -> None:
        logger.info("mock-vps: session opened for %s", info.connection.nickname)

    def _on_app_shutdown(self, _payload) -> None:
        logger.info("mock-vps: app shutting down")

    # --- the page widget (built on first open) ------------------------
    def _build_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)

        title = Gtk.Label(label="Deploy a VPS")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        region_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        region_row.append(Gtk.Label(label="Region:"))
        self._region_combo = Gtk.DropDown.new_from_strings(
            [label for _value, label in REGIONS]
        )
        # Restore the persisted region.
        for index, (value, _label) in enumerate(REGIONS):
            if value == self._region:
                self._region_combo.set_selected(index)
                break
        region_row.append(self._region_combo)
        box.append(region_row)

        self._deploy_btn = Gtk.Button(label="Deploy")
        self._deploy_btn.add_css_class("suggested-action")
        self._deploy_btn.connect("clicked", self._on_deploy_clicked)
        box.append(self._deploy_btn)

        self._status_label = Gtk.Label(label="Idle")
        self._status_label.set_halign(Gtk.Align.START)
        box.append(self._status_label)

        return box

    # --- deploy flow --------------------------------------------------
    def _on_deploy_clicked(self, _btn) -> None:
        index = self._region_combo.get_selected()
        region = REGIONS[index][0] if 0 <= index < len(REGIONS) else "fra1"
        self.ctx.settings.set("region", region)  # remember choice

        self._deploy_btn.set_sensitive(False)
        self._set_status("Provisioning…")

        # Provider/network work must NOT run on the UI thread.
        threading.Thread(
            target=self._provision_worker, args=(region,), daemon=True
        ).start()

    def _provision_worker(self, region: str) -> None:
        """Runs on a background thread. Pretend to call a provider API."""
        try:
            # … the partner's real HTTP provisioning would go here …
            ip = "203.0.113.10"  # mock result

            # Generate a key for the new host (public API → app KeyManager).
            key_path = self.ctx.generate_key(f"mock-vps-{region}")

            # Stash a provider credential, namespaced & keyring-backed.
            self.ctx.secrets.set("api_token", "mock-token-123")

            # Back to the UI thread for any UI / connection work.
            self.ctx.run_on_ui_thread(self._finish_deploy, ip, key_path)
        except Exception:
            logger.exception("mock-vps: provisioning failed")
            self.ctx.run_on_ui_thread(self._fail_deploy)

    def _finish_deploy(self, ip: str, key_path) -> None:
        data = {
            "nickname": f"vps-{ip}",
            "host": ip,
            "hostname": ip,
            "username": "root",
            "port": 22,
            "protocol": "ssh",
        }
        if key_path:
            data["keyfile"] = key_path

        try:
            info = self.ctx.add_connection(data)
        except ValueError as exc:
            # e.g. duplicate nickname — surface it and stop.
            self._set_status(f"Failed: {exc}")
            self.ctx.ui.notify(f"Deploy failed: {exc}")
            self._deploy_btn.set_sensitive(True)
            return

        self._set_status(f"Deployed {info.nickname} ({info.host})")
        self.ctx.ui.notify(f"VPS {info.nickname} is ready")
        self.ctx.open_connection(info.nickname)  # opens a terminal tab
        self._deploy_btn.set_sensitive(True)

    def _fail_deploy(self) -> None:
        self._set_status("Provisioning failed")
        self.ctx.ui.notify("VPS provisioning failed")
        self._deploy_btn.set_sensitive(True)

    def _set_status(self, text: str) -> None:
        if self._status_label is not None:
            self._status_label.set_text(text)
