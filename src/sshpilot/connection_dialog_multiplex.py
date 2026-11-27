"""Connection multiplexing (OpenSSH ControlMaster) for one host, in its Host block.

Like every other field of the editor, the choice is written as directives of
the host's own block, so plain ``ssh`` outside the app shares the connection
too. The row owns those directives, as the authentication section owns its
keywords: they are taken out of the block's extra lines on load, so the Extra
Options page (options no other field covers) never lists them, and joined
back on save and for OpenSSH's answer (``_combined_extra_ssh_config``):

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

OpenSSH for Windows cannot multiplex, so there is no row there, and the
directives stay on the Extra Options page.
"""

from __future__ import annotations

import logging
import os
import re
from gettext import gettext as _
from typing import List, Optional, Tuple

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

#: The directives the row owns (lower-case), where there is a row: kept off
#: the Extra Options page, which refuses them as having their own field.
OWN_KEYWORDS = (
    frozenset({"controlmaster", "controlpath", "controlpersist"})
    if MULTIPLEXING_SUPPORTED else frozenset()
)

_LINE = re.compile(r"\s*(\S+?)\s*(?:=\s*|\s+)(.*?)\s*$")

Line = Tuple[str, str]


def _parse(line: str) -> Optional[Line]:
    """``(keyword, value)`` of one ``Keyword value`` (or ``=value``) line."""
    match = _LINE.match(line or "")
    return (match.group(1), match.group(2)) if match else None


def split_multiplex_lines(extra: str) -> Tuple[List[Line], str]:
    """``(the row's lines, the rest)`` of an extra-options text."""
    own, other = [], []
    for line in (extra or "").splitlines():
        if not line.strip():
            continue
        parsed = _parse(line)
        if parsed is not None and parsed[0].lower() in OWN_KEYWORDS:
            own.append(parsed)
        else:
            other.append(line)
    return own, "\n".join(other)


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
        self._multiplex_lines: List[Line] = []
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
        self.multiplex_row.connect("notify::selected", self._on_multiplex_row_changed)
        group = Adw.PreferencesGroup(title=_("Multiplexing"))
        group.add(self.multiplex_row)
        self._multiplex_group = group
        return group

    # -- the lines --------------------------------------------------------

    def _multiplex_get(self, keyword: str) -> Optional[str]:
        wanted = keyword.lower()
        for key, value in getattr(self, "_multiplex_lines", ()):
            if key.lower() == wanted:
                return value
        return None

    def _multiplex_set(self, keyword: str, value: str) -> None:
        """Set *keyword*, in place when the block has it (keeping its line's
        position and spelling), else after the others."""
        lines = self._multiplex_lines
        wanted = keyword.lower()
        for index, (key, _value) in enumerate(lines):
            if key.lower() == wanted:
                lines[index] = (key, value)
                return
        lines.append((keyword, value))

    def _multiplex_remove(self, keyword: str) -> None:
        wanted = keyword.lower()
        self._multiplex_lines[:] = [
            line for line in self._multiplex_lines if line[0].lower() != wanted
        ]

    def _load_multiplex_lines(self, lines: List[Line]) -> None:
        """Take the block's own lines (see ``split_multiplex_lines``)."""
        self._multiplex_lines = list(lines)
        row = getattr(self, "multiplex_row", None)
        if row is None:
            return
        self._multiplex_syncing = True
        try:
            row.set_selected(mode_of(self._multiplex_get("ControlMaster")))
        finally:
            self._multiplex_syncing = False

    def _multiplex_extra_lines(self) -> List[str]:
        """The row's lines, to join with the other extra lines."""
        return [f"{key} {value}" for key, value in getattr(self, "_multiplex_lines", ())]

    # -- the row ----------------------------------------------------------

    def _on_multiplex_row_changed(self, row, _pspec=None):
        """Rewrite the block's sharing directives for the choice."""
        if getattr(self, "_multiplex_syncing", False):
            return
        mode = row.get_selected()
        if mode == ON:
            self._multiplex_set("ControlMaster", "auto")
            path = self._multiplex_get("ControlPath")
            if path is None or path.strip().lower() == "none":
                self._multiplex_set("ControlPath", CONTROL_PATH)
            if self._multiplex_get("ControlPersist") is None:
                self._multiplex_set("ControlPersist", CONTROL_PERSIST)
        elif mode == OFF:
            self._multiplex_set("ControlMaster", "no")
            self._multiplex_set("ControlPath", "none")
            self._multiplex_remove("ControlPersist")
        else:
            self._multiplex_lines = []
        # The lines changed: ask OpenSSH what the host uses with them.
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh()

    def _prepare_multiplex_save(self):
        """Before a save: the directory the block's sockets go in exists."""
        if not MULTIPLEXING_SUPPORTED:
            return
        if mode_of(self._multiplex_get("ControlMaster")) != ON:
            return
        if (self._multiplex_get("ControlPath") or "").strip() == CONTROL_PATH:
            ensure_socket_dir()
