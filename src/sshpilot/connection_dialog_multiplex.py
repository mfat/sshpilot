"""Connection multiplexing (OpenSSH ControlMaster) for one host, in its Host block.

Like every other field of the editor, the choice is written as directives of
the host's own block, so plain ``ssh`` outside the app shares the connection
too. The row is a view over the Extra Options list, as the ssh -N and
ExitOnForwardFailure switches are:

* Default writes nothing: Settings ▸ SSH Options, or an earlier block, decide;
* On writes ``ControlMaster auto`` with a ``ControlPath`` and a
  ``ControlPersist``, unless the block already sets them;
* Off writes ``ControlMaster no`` and ``ControlPath none``. ``no`` alone still
  rides a master another session left on the socket.

Settings ▸ SSH Options always win: with multiplexing on there, the app's own
``-o`` options come first on the command line, and the row is marked
Overridden like any other option they force (``connection_dialog_overrides``).

The sockets live in ``~/.ssh/sockets``, not in the app's private directory:
plain ``ssh`` reads the same block, and the daemon retires only the masters
it started there (``daemon.control_masters``). A master this block opens
outlives the app by its ``ControlPersist``, as one opened by ``ssh`` does.

OpenSSH for Windows cannot multiplex, so there is no row there.
"""

from __future__ import annotations

import logging
import os
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .platform.fs import makedirs_private
from .platform_utils import is_windows

logger = logging.getLogger(__name__)

DEFAULT = 0
ON = 1
OFF = 2

#: OpenSSH for Windows cannot multiplex: a ControlMaster line fails its launch.
MULTIPLEXING_SUPPORTED = not is_windows()

#: How long a master On opens stays up after its last session: the app's own
#: masters linger as long.
CONTROL_PERSIST = "60"

#: Where On points a host's sockets. ``%C`` is ssh's per-connection hash, so
#: the path stays short and unique per host.
SOCKET_DIR = "~/.ssh/sockets"
CONTROL_PATH = SOCKET_DIR + "/%C"

_KEYWORDS = ("ControlMaster", "ControlPath", "ControlPersist")


def mode_of(control_master) -> int:
    """The row's choice for the block's ``ControlMaster`` value (None: unset).
    Every value but ``no`` opens or rides a master, so it reads as On."""
    if control_master is None or not str(control_master).strip():
        return DEFAULT
    return OFF if str(control_master).strip().lower() == "no" else ON


def ensure_socket_dir(path: str = SOCKET_DIR) -> None:
    """Create the sockets directory, private as ssh wants it. Best effort: a
    missing directory only makes ssh connect without sharing."""
    try:
        makedirs_private(os.path.expanduser(path))
    except OSError:
        logger.debug("Could not create %s", path, exc_info=True)


class ConnectionDialogMultiplexMixin:
    """The Multiplexing group of the connection editor: its Share connection row."""

    def _build_multiplex_group(self):
        """The group holding the row, or None where ssh cannot multiplex."""
        self._multiplex_syncing = False
        if not MULTIPLEXING_SUPPORTED:
            self._multiplex_group = None
            return None
        model = Gtk.StringList()
        for label in (_("Default"), _("On"), _("Off")):
            model.append(label)
        self.multiplex_row = Adw.ComboRow(title=_("Share connection"))
        self.multiplex_row.set_subtitle(
            _("Terminals and file transfers to this host reuse one connection")
        )
        self.multiplex_row.set_model(model)
        # Found by every directive it writes, not only ControlMaster.
        self.multiplex_row._sshpilot_search_keywords = "ControlPath ControlPersist"
        self.multiplex_row.set_selected(DEFAULT)
        group = Adw.PreferencesGroup(title=_("Multiplexing"))
        group.add(self.multiplex_row)
        self._multiplex_group = group
        return group

    def _wire_multiplex_row(self):
        """Connect the row to the Extra Options list it writes to."""
        row = getattr(self, "multiplex_row", None)
        if row is None:
            return
        row.connect("notify::selected", self._on_multiplex_row_changed)
        self._sync_multiplex_row_from_advanced()

    def _sync_multiplex_row_from_advanced(self):
        """Show the block's ``ControlMaster`` on the row."""
        if getattr(self, "_multiplex_syncing", False):
            return
        advanced_tab = getattr(self, "advanced_tab", None)
        row = getattr(self, "multiplex_row", None)
        if advanced_tab is None or row is None:
            return
        mode = mode_of(advanced_tab.get_option("ControlMaster"))
        self._multiplex_syncing = True
        try:
            if row.get_selected() != mode:
                row.set_selected(mode)
        finally:
            self._multiplex_syncing = False

    def _on_multiplex_row_changed(self, row, _pspec=None):
        """Rewrite the block's sharing directives for the choice."""
        if getattr(self, "_multiplex_syncing", False):
            return
        advanced_tab = getattr(self, "advanced_tab", None)
        if advanced_tab is None:
            return
        mode = row.get_selected()
        self._multiplex_syncing = True
        try:
            if mode == ON:
                advanced_tab.set_option("ControlMaster", "auto")
                path = advanced_tab.get_option("ControlPath")
                if path is None or path.strip().lower() == "none":
                    advanced_tab.set_option("ControlPath", CONTROL_PATH)
                if advanced_tab.get_option("ControlPersist") is None:
                    advanced_tab.set_option("ControlPersist", CONTROL_PERSIST)
            elif mode == OFF:
                advanced_tab.set_option("ControlMaster", "no")
                advanced_tab.set_option("ControlPath", "none")
                advanced_tab.remove_option("ControlPersist")
            else:
                for keyword in _KEYWORDS:
                    if advanced_tab.get_option(keyword) is not None:
                        advanced_tab.remove_option(keyword)
        finally:
            self._multiplex_syncing = False

    def _prepare_multiplex_save(self):
        """Before a save: the directory the block's sockets go in exists."""
        get_option = getattr(getattr(self, "advanced_tab", None), "get_option", None)
        if not callable(get_option) or not MULTIPLEXING_SUPPORTED:
            return
        if mode_of(get_option("ControlMaster")) != ON:
            return
        path = get_option("ControlPath") or ""
        if path.strip() == CONTROL_PATH:
            ensure_socket_dir()
