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
MAX_WORKERS = 8


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

        ctx.ui.register_page(
            "health", "Health", "network-transmit-receive-symbolic",
            self._build_page)
        ctx.events.subscribe(Events.APP_SHUTDOWN, self._on_app_shutdown)

    def deactivate(self) -> None:
        self._shutdown()

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
        from gi.repository import Gtk

        self._Gtk = Gtk

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
        hint = Gtk.Label(label=f"Auto-refresh every {int(REFRESH_INTERVAL)}s")
        hint.add_css_class("dim-label")
        controls.append(hint)
        box.append(controls)

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
        self._executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self._monitor_thread = threading.Thread(
            target=self._monitor, name="health-monitor", daemon=True)
        self._monitor_thread.start()

    def _monitor(self) -> None:
        # Sleep first via wait(); the initial paint already happened in _tick().
        while not self._stop.wait(REFRESH_INTERVAL):
            self.ctx.run_on_ui_thread(self._tick)

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
        up = tcp_check(host, port, CHECK_TIMEOUT)
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
