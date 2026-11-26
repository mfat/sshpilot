"""What is typed where a connection's SSH alias goes, read as a ``Host`` line.

A connection has one name, with no whitespace. A regular ssh_config user may
still type there what they would write after ``Host``:

* several names (``web1 web2``), which in a configuration share one block;
* a pattern (``*``, ``*.example.com``, ``!bastion``), a rule for every host
  it matches and not a connection.

Neither is saved from the dialog. The field is marked, and a row under it
hands what was typed to Advanced Configuration, where Host lines of several
hosts and rules are authored and ordered: a new connection's line becomes a
new, unsaved block there with the host, user and port typed beside it; a
saved connection's opens at its block.

``read_host_names`` is the reading itself, free of widgets.
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import List, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from .ssh_connection_validator import ValidationResult
from .ssh_config_document import is_literal_host_token

logger = logging.getLogger(__name__)

EMPTY = "empty"
SINGLE = "single"
SEVERAL = "several"
PATTERN = "pattern"


def read_host_names(text: str) -> Tuple[str, List[str]]:
    """``(kind, names)`` of what was typed as the alias.

    ``PATTERN`` as soon as one word is a wildcard or a negation, whatever
    else is there: the line is then a rule.
    """
    names = (text or "").split()
    if not names:
        return EMPTY, []
    if not all(is_literal_host_token(name) for name in names):
        return PATTERN, names
    return (SINGLE if len(names) == 1 else SEVERAL), names


def handoff_options(hostname: str, username: str, port: str) -> List[Tuple[str, str]]:
    """The options typed beside the alias, for the block that takes its place."""
    options = []
    if hostname.strip():
        options.append(("HostName", hostname.strip()))
    if username.strip():
        options.append(("User", username.strip()))
    if port.strip() and port.strip() != "22":
        options.append(("Port", port.strip()))
    return options


class ConnectionDialogHostNamesMixin:
    """The alias field of ``ConnectionDialog`` read as a Host line."""

    def _alias_row_title(self) -> str:
        return _("SSH Alias (no whitespace allowed)")

    def _build_host_line_offer_row(self):
        """The row under the alias that sends a Host line this dialog does
        not save to Advanced Configuration. Hidden until there is one."""
        row = Adw.ActionRow()
        try:
            row.set_use_markup(False)
        except Exception:
            pass
        row.add_css_class("warning")
        button = Gtk.Button(label=_("Open Advanced Configuration"))
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._on_host_line_offer_clicked)
        row.add_suffix(button)
        row.set_visible(False)
        self.host_line_offer_row = row
        self.host_line_offer_button = button
        return row

    def _typed_host_names(self) -> Tuple[str, List[str]]:
        row = getattr(self, "nickname_row", None)
        return read_host_names(row.get_text() if row is not None else "")

    def _typed_primary_alias(self) -> str:
        """The one host the dialog's questions to OpenSSH are about: the
        name typed, or none while it is not one name."""
        kind, names = self._typed_host_names()
        return names[0] if kind == SINGLE else ""

    def _host_line_applies(self) -> bool:
        try:
            return self._selected_protocol_id() == "ssh"
        except Exception:
            return True

    def _validate_host_names(self, text: str):
        """Validation of the alias field as a Host line, or None to let the
        ordinary one-name validation run."""
        kind, _names = read_host_names(text)
        if not self._host_line_applies() or kind in (EMPTY, SINGLE):
            return None
        if kind == PATTERN:
            return ValidationResult(False, _("A pattern is not a connection"), "error")
        return ValidationResult(False, _("An alias can't contain spaces"), "error")

    def _sync_host_line_ui(self) -> None:
        """Show the offer row for a Host line this dialog does not save."""
        row = getattr(self, "host_line_offer_row", None)
        if row is None:
            return
        kind, _names = self._typed_host_names()
        if not self._host_line_applies() or kind in (EMPTY, SINGLE):
            row.set_visible(False)
            return
        if kind == PATTERN:
            row.set_title(_("A pattern is not a connection"))
            row.set_subtitle(_(
                "It sets options for every host it matches. Create it as "
                "a block in Advanced Configuration."
            ))
        else:
            row.set_title(_("An alias can't contain spaces"))
            row.set_subtitle(_(
                "For several aliases on one Host line, use Advanced "
                "Configuration."
            ))
        row.set_visible(True)

    def _on_host_line_offer_clicked(self, _button) -> None:
        """Take what was typed to Advanced Configuration.

        For a new connection it becomes a new, unsaved block there, with
        the host, user and port typed here, and the dialog is closed, its
        Host line having moved. For a saved connection the window opens at
        its block, as the sidebar's Advanced Configuration does.
        """
        kind, names = self._typed_host_names()
        editing = bool(getattr(self, "is_editing", False))
        if kind not in (SEVERAL, PATTERN):
            return
        if editing:
            self._on_advanced_config_clicked(None)
            return
        opener = getattr(getattr(self, "parent_window", None), "open_advanced_config", None)
        if not callable(opener):
            return

        def text(name):
            row = getattr(self, name, None)
            return row.get_text() if row is not None else ""

        window = opener(
            new_block=(
                " ".join(names),
                handoff_options(text("hostname_row"), text("username_row"), text("port_row")),
            ),
            over=self,
        )
        if window is not None:
            self.close()
