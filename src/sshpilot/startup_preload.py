"""Import the main window's GTK-free dependencies on a background thread.

Between importing GTK and building the window, the GTK thread spends much of
its time in C (Adwaita's ``do_startup``, ~190 ms), with the GIL released. The
window module's pure-Python dependencies -- the API models (hundreds of
dataclasses), settings, plugins, config -- are imported on a worker in that
gap, so the window import that follows finds them already loaded. So is the
client factory the first daemon client hand-over imports right after the first
frame (``MainWindow._apply_ready_early_client``).

:func:`start` runs as ``main()`` begins; :func:`wait` is called before the
window module is imported, which keeps the two threads from importing the same
modules at the same time. Only modules that import without GTK are listed
(GLib/Gio are already loaded), so the worker never runs GTK code off the GTK
thread; nor does it register GObject types: ``sshpilot.config`` (``Config``)
is imported on the GTK thread before the worker starts, and a test keeps the
rest of the list free of GObject subclasses. A failed import is logged and
skipped: the importing code then raises it as usual.
"""
from __future__ import annotations

import importlib
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

#: Set to ``1`` to import everything on the GTK thread, as before.
DISABLE_ENV = "SSHPILOT_NO_STARTUP_PRELOAD"
#: Longest the GTK thread waits for the worker; after that it imports the rest
#: itself (the per-module import locks keep that correct).
_WAIT_SECONDS = 10.0

# In the order the window import loads them.
MODULES = (
    "sshpilot.api",
    "sshpilot.api._safe_values",
    "sshpilot.api.models",
    "sshpilot.api.models.common",
    "sshpilot.api.errors",
    "sshpilot.api.connection_identity",
    "sshpilot.accessibility",
    "sshpilot.core.interaction.policy",
    "sshpilot.core.interaction",
    "sshpilot.gtk.interaction",
    "sshpilot.api.events",
    "sshpilot.api.models.connection_store",
    "sshpilot.api.models.pre_command",
    "sshpilot.api.models.connections",
    "sshpilot.gtk.connection_store",
    "sshpilot.gtk.daemon_connection_services",
    "sshpilot.api.models.operations",
    "sshpilot.api.models.terminal",
    "sshpilot.api.models.sessions",
    "sshpilot.gtk.plugin_session_failure_messages",
    "sshpilot.gtk.session_failure_messages",
    "sshpilot.gtk.sftp_failure_messages",
    "sshpilot.gtk.connection_runtime_status",
    "sshpilot.gtk",
    "sshpilot.platform.locking",
    "sshpilot.custom_terminal_themes",
    "sshpilot.core.settings.defaults",
    "sshpilot.core.settings.identity",
    "sshpilot.core.settings.migration",
    "sshpilot.core.settings.ssh_overrides",
    "sshpilot.core.settings.store",
    "sshpilot.core.settings",
    "sshpilot.api.models.keys",
    "sshpilot.api.models.daemon",
    "sshpilot.connection_display",
    "sshpilot.connection_sort",
    "sshpilot.groups",
    "sshpilot.session_manager",
    "sshpilot.core.plugins.contracts",
    "sshpilot.core.plugins",
    "sshpilot.plugins.host",
    "sshpilot.identity",
    "sshpilot.plugins.api",
    "sshpilot.plugins",
    "sshpilot.plugins.registry",
    "sshpilot.accent_override_css",
    "sshpilot.tag_groups",
    "sshpilot.wol",
    "sshpilot.terminal_cursor",
    "sshpilot.terminal_highlight",
    "sshpilot.ssh_process_manager",
    "sshpilot.core.connection_evidence",
    "sshpilot.terminal_input",
    "sshpilot.terminal_osc52",
    "sshpilot.terminal_display_pause",
    "sshpilot.gtk.asbru_import_messages",
    "sshpilot.core.secrets.policy",
    "sshpilot.core.secrets.protocols",
    "sshpilot.core.secrets",
    "sshpilot.core.secrets.management",
    "sshpilot.api.models.secrets",
    "sshpilot.gtk.secret_transfer_messages",
    "sshpilot.search_utils",
    "sshpilot.system_terminal",
    "sshpilot.api.capabilities",
    # Not imported by the window module, but by the first client hand-over.
    "sshpilot.api.client",
    "sshpilot.api.client_factory",
)

_thread: Optional[threading.Thread] = None


def _run() -> None:
    for name in MODULES:
        try:
            importlib.import_module(name)
        except Exception:
            logger.warning("Preloading %s failed", name, exc_info=True)


def disabled() -> bool:
    return os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes"}


def start() -> None:
    """Start the worker once (no-op when disabled or already started)."""
    global _thread
    if _thread is not None or disabled():
        return
    # Registers the Config GObject type here, on the GTK thread; the
    # application needs it straight after this anyway.
    importlib.import_module("sshpilot.config")
    _thread = threading.Thread(target=_run, name="sshpilot-preload", daemon=True)
    _thread.start()


def wait() -> None:
    """Block until the worker is done (immediately if it never started)."""
    thread = _thread
    if thread is None:
        return
    thread.join(_WAIT_SECONDS)
    if thread.is_alive():
        logger.warning(
            "Startup preload still running after %.0f s; continuing without it",
            _WAIT_SECONDS,
        )
