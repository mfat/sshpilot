"""Host Health Dashboard — live up/down status for every saved connection.

A non-protocol sshPilot plugin. Opens a page that lists all saved hosts and
shows whether each one's ``host:port`` accepts a TCP connection, refreshed on a
timer in the background. A good worked example of doing **network work off the
UI thread** and **shutting threads down cleanly**.

Capabilities exercised (all from ``sshpilot.plugins.api``):
* enumerating saved hosts (``ctx.list_connections`` — needs app API >= 1.4)
* a UI page (``ctx.ui.register_page``)
* background workers + marshalling results back (``ctx.run_on_ui_thread``)
* clean shutdown via ``deactivate()`` and the ``APP_SHUTDOWN`` event so no
  worker outlives the app

Pure logic (``tcp_check``) has no GTK import and is unit-tested by monkeypatching
``socket``; ``gi`` is imported lazily inside the page factory. The threading uses
only the stdlib (``threading`` / ``concurrent.futures``), imported at module top
because neither needs a display.
"""

from __future__ import annotations

import logging
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from sshpilot.plugins.api import Events, PluginContext, SshPilotPlugin

logger = logging.getLogger(__name__)

CHECK_TIMEOUT = 3.0        # seconds per TCP probe
REFRESH_INTERVAL = 30.0    # seconds between automatic refreshes
MAX_WORKERS = 64  # cap; the pool is sized to min(this, host count) so probes
                  # run concurrently and results stream in as each returns


# --- pure logic (no GTK) ----------------------------------------------------

def tcp_check(host: str, port: int, timeout: float = CHECK_TIMEOUT) -> bool:
    """Return True if a TCP connection to ``host:port`` succeeds within
    ``timeout``. Any failure (refused, unreachable, DNS, timeout) → False."""
    if not host:
        return False
    try:
        port = int(port) or 22
    except (TypeError, ValueError):
        port = 22
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --- plugin -----------------------------------------------------------------

class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        self.ctx = ctx
        self._stop = threading.Event()
        self._executor = None
        self._monitor_thread = None
        self._rows = {}            # nickname -> status Gtk.Label
        self._list_box = None
        self._supported = hasattr(ctx, "list_connections")
        self._timeout = self._read_float("timeout", CHECK_TIMEOUT)
        self._interval = self._read_float("interval", REFRESH_INTERVAL)
        self._pause_when_hidden = bool(ctx.settings.get("pause_when_hidden", True))
        self._page_visible = True

        ctx.ui.register_page(
            "health", "Health", "network-transmit-receive-symbolic",
            self._build_page)
        ctx.events.subscribe(Events.APP_SHUTDOWN, self._on_app_shutdown)

    def deactivate(self) -> None:
        self._shutdown()

    def _read_float(self, key: str, default: float) -> float:
        try:
            return max(0.5, float(self.ctx.settings.get(key, default)))
        except (TypeError, ValueError):
            return default

    # --- lifecycle --------------------------------------------------------
    def _on_app_shutdown(self, _payload) -> None:
        self._shutdown()

    def _shutdown(self) -> None:
        """Stop the monitor loop and worker pool. Idempotent and best-effort:
        runs on both deactivate() and app_shutdown, in either order."""
        self._stop.set()
        executor = self._executor
        self._executor = None
        if executor is not None:
            executor.shutdown(wait=False)
        logger.info("health: workers stopped")

    # --- UI (gi imported lazily; only runs inside the app) ----------------
    def _build_page(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk

        self._Gtk = Gtk
        self._Adw = Adw

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for fn in (box.set_margin_top, box.set_margin_bottom,
                   box.set_margin_start, box.set_margin_end):
            fn(18)

        title = Gtk.Label(label="Host Health")
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        if not self._supported:
            warn = Gtk.Label(
                label="This plugin needs sshPilot plugin API ≥ 1.4 "
                      "(ctx.list_connections). Please update sshPilot.")
            warn.add_css_class("dim-label")
            warn.set_wrap(True)
            warn.set_xalign(0)
            box.append(warn)
            return box

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label="Refresh now")
        refresh.connect("clicked", lambda _b: self._tick())
        controls.append(refresh)
        box.append(controls)

        settings = Adw.PreferencesGroup(title="Settings")
        self._timeout_row = Adw.EntryRow(title="Probe timeout (seconds)")
        self._timeout_row.set_text(str(self._timeout))
        self._timeout_row.connect("apply", self._on_timeout_changed)
        self._timeout_row.connect("entry-activated", self._on_timeout_changed)
        self._interval_row = Adw.EntryRow(title="Auto-refresh interval (seconds)")
        self._interval_row.set_text(str(self._interval))
        self._interval_row.connect("apply", self._on_interval_changed)
        self._interval_row.connect("entry-activated", self._on_interval_changed)
        for r in (self._timeout_row, self._interval_row):
            try:
                r.set_show_apply_button(True)
            except Exception:
                pass
        self._pause_row = Adw.SwitchRow(
            title="Pause when page is hidden",
            subtitle="Stop probing while the Health tab isn't visible")
        self._pause_row.set_active(self._pause_when_hidden)
        self._pause_row.connect("notify::active", self._on_pause_toggled)
        settings.add(self._timeout_row)
        settings.add(self._interval_row)
        settings.add(self._pause_row)
        box.append(settings)

        # Track tab visibility for pause-when-hidden (best-effort).
        box.connect("map", lambda *_a: self._set_visible(True))
        box.connect("unmap", lambda *_a: self._set_visible(False))

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list_box = Gtk.ListBox()
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroller.set_child(self._list_box)
        box.append(scroller)

        # First paint + start the background monitor.
        self._tick()
        self._start_monitor()
        return box

    # --- monitor loop -----------------------------------------------------
    def _start_monitor(self) -> None:
        if self._monitor_thread is not None:
            return
        # The worker pool is created lazily in _tick (sized to the host count);
        # this only starts the periodic re-check thread.
        self._monitor_thread = threading.Thread(
            target=self._monitor, name="health-monitor", daemon=True)
        self._monitor_thread.start()

    def _monitor(self) -> None:
        # Sleep first via wait(); the initial paint already happened in _tick().
        while not self._stop.wait(self._interval):
            if self._pause_when_hidden and not self._page_visible:
                continue  # tab hidden — skip the auto-probe
            self.ctx.run_on_ui_thread(self._tick)

    # --- settings handlers ------------------------------------------------
    def _set_visible(self, visible: bool) -> None:
        self._page_visible = visible

    def _on_timeout_changed(self, *_a) -> None:
        try:
            value = max(0.5, float(self._timeout_row.get_text().strip()))
        except (TypeError, ValueError):
            self._timeout_row.set_text(str(self._timeout))
            return
        self._timeout = value
        self.ctx.settings.set("timeout", value)

    def _on_interval_changed(self, *_a) -> None:
        try:
            value = max(0.5, float(self._interval_row.get_text().strip()))
        except (TypeError, ValueError):
            self._interval_row.set_text(str(self._interval))
            return
        self._interval = value
        self.ctx.settings.set("interval", value)

    def _on_pause_toggled(self, row, _param) -> None:
        self._pause_when_hidden = row.get_active()
        self.ctx.settings.set("pause_when_hidden", self._pause_when_hidden)

    # --- UI thread work ---------------------------------------------------
    def _tick(self) -> None:
        """Rebuild the row list from the current connections and dispatch a
        probe per host. Runs on the UI thread."""
        if self._stop.is_set() or self._list_box is None:
            return
        Gtk = self._Gtk
        # Rebuild rows to reflect added/removed connections.
        child = self._list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt
        self._rows = {}

        connections = self.ctx.list_connections()
        if not connections:
            row = Gtk.ListBoxRow()
            row.set_child(Gtk.Label(label="No connections to check.",
                                    margin_top=8, margin_bottom=8))
            self._list_box.append(row)
            return

        # Create the pool lazily (sized to the work) so the very first paint
        # dispatches probes immediately — and concurrently — rather than waiting
        # for the next auto-refresh.
        if self._executor is None and not self._stop.is_set():
            self._executor = ThreadPoolExecutor(
                max_workers=min(MAX_WORKERS, max(8, len(connections))))
        executor = self._executor
        for info in connections:
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            for fn in (line.set_margin_top, line.set_margin_bottom,
                       line.set_margin_start, line.set_margin_end):
                fn(8)
            name = Gtk.Label(label=info.nickname, xalign=0)
            name.set_hexpand(True)
            addr = Gtk.Label(label=f"{info.host}:{info.port}", xalign=0)
            addr.add_css_class("dim-label")
            status = Gtk.Label(label="Checking…")
            status.add_css_class("dim-label")
            line.append(name)
            line.append(addr)
            line.append(status)
            row.set_child(line)
            self._list_box.append(row)
            self._rows[info.nickname] = status

            if executor is not None and not self._stop.is_set():
                executor.submit(self._check_one, info.nickname, info.host, info.port)

    def _check_one(self, nickname: str, host: str, port: int) -> None:
        """Runs on a worker thread."""
        up = tcp_check(host, port, self._timeout)
        if not self._stop.is_set():
            self.ctx.run_on_ui_thread(self._update_row, nickname, up)

    def _update_row(self, nickname: str, up: bool) -> None:
        status = self._rows.get(nickname)
        if status is None:
            return
        status.remove_css_class("dim-label")
        status.remove_css_class("success")
        status.remove_css_class("error")
        if up:
            status.set_label("● Up")
            status.add_css_class("success")
        else:
            status.set_label("● Down")
            status.add_css_class("error")
