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

Under the row, Socket path and Keep open edit the block's ``ControlPath`` and
``ControlPersist``: shown while sharing is On, or while the block sets them
with no ``ControlMaster`` of its own, so no line the row owns is out of sight.
Values OpenSSH would refuse only once a connection is tried -- a
``ControlPersist`` it cannot read, a socket path too long to listen on -- are
marked there and refuse the save, and the save creates the socket directory
(``_prepare_multiplex_save``): ssh does not, and exits after authenticating
when it is missing.

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

from .core.ssh.control_path import (
    MAX_CONTROL_PATH_LENGTH,
    control_path_directory,
    control_path_too_long,
    control_persist_valid,
)
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


def detail_problem(keyword: str, value: str) -> str:
    """Why OpenSSH would refuse *value* for the Socket path or Keep open row
    (``""`` when it would not). Both fail only once a connection is tried:
    a bad ``ControlPersist`` refuses the launch, and a socket path too long
    to listen on makes ssh exit after authenticating."""
    value = (value or "").strip()
    if not value:
        return ""
    if keyword == "ControlPersist" and not control_persist_valid(value):
        return _("Use seconds, a time such as 10m or 1h30m, yes, or no")
    if keyword == "ControlPath" and control_path_too_long(value):
        return _(
            "Too long for a socket: at most {count} characters once %C "
            "becomes the 40-character host hash"
        ).format(count=MAX_CONTROL_PATH_LENGTH)
    return ""


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

        # Adw.EntryRow has no subtitle: the hint lives in the tooltip.
        self.multiplex_path_row = Adw.EntryRow(title=_("Socket path"))
        self.multiplex_path_row.set_tooltip_text(
            _("Where the shared connection's socket is kept (ControlPath). "
              "%C names it after the host, user and port.")
        )
        self.multiplex_path_row._sshpilot_search_keywords = "socket"
        self.multiplex_persist_row = Adw.EntryRow(
            title=_("Keep open after last session")
        )
        self.multiplex_persist_row.set_tooltip_text(
            _("How long the shared connection stays up once its last terminal "
              "or transfer closes (ControlPersist): seconds, a time such as "
              "10m, or yes to keep it until stopped.")
        )
        self.multiplex_persist_row._sshpilot_search_keywords = "persist timeout"
        for row, keyword in (
            (self.multiplex_path_row, "ControlPath"),
            (self.multiplex_persist_row, "ControlPersist"),
        ):
            row.connect("changed", self._on_multiplex_detail_changed, keyword)
            row._sshpilot_multiplex_keyword = keyword
            group.add(row)
        self._sync_multiplex_detail_rows()
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
        self._sync_multiplex_detail_rows()

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
        self._sync_multiplex_detail_rows()
        self._multiplex_changed()

    def _on_multiplex_detail_changed(self, row, keyword):
        """Socket path or Keep open edited: its line follows (none if empty)."""
        if getattr(self, "_multiplex_syncing", False):
            return
        value = row.get_text().strip()
        if value:
            self._multiplex_set(keyword, value)
        else:
            self._multiplex_remove(keyword)
        self._validate_multiplex_rows()
        self._multiplex_changed()

    def _multiplex_changed(self):
        """The lines changed: ask OpenSSH what the host uses with them."""
        refresh = getattr(self, "_schedule_effective_refresh", None)
        if callable(refresh):
            refresh()

    def _sync_multiplex_detail_rows(self):
        """Show the block's path and persist time under the row, and only
        where they mean something: while sharing is On, or while the block
        sets them without a ControlMaster (Off writes ``ControlPath none``,
        which says nothing to show)."""
        mode = mode_of(self._multiplex_get("ControlMaster"))
        self._multiplex_syncing = True
        try:
            for name, keyword in (
                ("multiplex_path_row", "ControlPath"),
                ("multiplex_persist_row", "ControlPersist"),
            ):
                row = getattr(self, name, None)
                if row is None:
                    continue
                value = self._multiplex_get(keyword) or ""
                if row.get_text() != value:
                    row.set_text(value)
                row.set_visible(mode == ON or (mode == DEFAULT and bool(value)))
        finally:
            self._multiplex_syncing = False
        self._validate_multiplex_rows()

    def _validate_multiplex_rows(self):
        """Mark Socket path and Keep open when OpenSSH would refuse them;
        the first such row in sight, or None (the save is refused on it)."""
        first = None
        mark = getattr(self, "_row_set_message", None)
        for name in ("multiplex_path_row", "multiplex_persist_row"):
            row = getattr(self, name, None)
            if row is None:
                continue
            problem = ""
            if row.get_visible():
                problem = detail_problem(
                    row._sshpilot_multiplex_keyword, row.get_text()
                )
            if callable(mark):
                mark(row, problem, is_error=bool(problem))
            if problem and first is None:
                first = row
        return first

    def _prepare_multiplex_save(self) -> bool:
        """Before a save: the directory the block's sockets go in exists.

        OpenSSH does not create it, and a master that cannot listen there
        makes ssh exit after authenticating, on every connection to the
        host. So it is created here, for any path whose directory is known
        before connecting; False, once the user is told, when it cannot be.
        """
        if not MULTIPLEXING_SUPPORTED:
            return True
        if mode_of(self._multiplex_get("ControlMaster")) == OFF:
            return True
        directory = control_path_directory(self._multiplex_get("ControlPath") or "")
        if directory is None or os.path.isdir(directory):
            return True
        try:
            makedirs_private(directory)
        except OSError as error:
            logger.warning("Could not create the socket directory %s: %s",
                           directory, error)
            self.show_error(
                _("The socket directory {path} could not be created ({reason}). "
                  "Connections to this host fail while they share one, so "
                  "choose another socket path.").format(
                      path=directory, reason=error.strerror or error),
                heading=_("Cannot Share Connection"),
            )
            return False
        return True
