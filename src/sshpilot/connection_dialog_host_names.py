"""What is typed where a connection's SSH alias goes, read as a ``Host`` line.

A regular ssh_config user types there what they would write after ``Host``:

* several plain names (``web1 web2``) are several connections that share one
  block. A new connection is saved that way; each name appears in the list.
  The row under the field says so as soon as a space is typed.
* a pattern (``*``, ``*.example.com``, ``!bastion``) is a rule for every host
  it matches and not a connection. It is not saved from here: a row under
  the field says so and hands what was typed to Advanced Configuration, where
  rules are authored and ordered.

A saved connection keeps one name; more hosts for its block are added on its
Host line in Advanced Configuration, which the same row opens.

``read_host_names`` is the reading itself, free of widgets.
"""

from __future__ import annotations

import logging
from gettext import gettext as _, ngettext
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
        if getattr(self, "is_editing", False):
            return _("SSH Alias (no whitespace allowed)")
        return _("SSH Alias (spaces separate several hosts)")

    def _build_host_line_offer_row(self):
        """The row under the alias that sends a Host line this dialog does
        not save to Advanced Configuration. Hidden until there is one."""
        row = Adw.ActionRow()
        try:
            row.set_use_markup(False)
        except Exception:
            pass
        button = Gtk.Button(label=_("Open Advanced Configuration"))
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", self._on_host_line_offer_clicked)
        row.add_suffix(button)
        row.set_activatable_widget(button)
        row.set_visible(False)
        self.host_line_offer_row = row
        self.host_line_offer_button = button
        return row

    def _typed_host_names(self) -> Tuple[str, List[str]]:
        row = getattr(self, "nickname_row", None)
        return read_host_names(row.get_text() if row is not None else "")

    def _typed_primary_alias(self) -> str:
        """The one host a new connection's questions to OpenSSH are about:
        the first name typed, or none while a pattern is there."""
        kind, names = self._typed_host_names()
        return names[0] if kind in (SINGLE, SEVERAL) else ""

    def _typed_shared_names(self) -> List[str]:
        """The other names a new connection's block is to declare."""
        if getattr(self, "is_editing", False):
            return []
        kind, names = self._typed_host_names()
        return names[1:] if kind == SEVERAL else []

    def _host_line_applies(self) -> bool:
        try:
            return self._selected_protocol_id() == "ssh"
        except Exception:
            return True

    def _validate_host_names(self, text: str):
        """Validation of the alias field as a Host line, or None to let the
        ordinary one-name validation run."""
        kind, names = read_host_names(text)
        if not self._host_line_applies() or kind in (EMPTY, SINGLE):
            return None
        if kind == PATTERN:
            return ValidationResult(False, _("A pattern is not a connection"), "error")
        if getattr(self, "is_editing", False):
            return ValidationResult(False, _("A saved connection has one name"), "error")
        if len(set(names)) != len(names):
            return ValidationResult(False, _("A name is given more than once"), "error")
        for name in names:
            result = self.validator.validate_connection_name(name)
            if not result.is_valid:
                return result
        return ValidationResult(
            True,
            ngettext(
                "{count} connection sharing one Host block",
                "{count} connections sharing one Host block",
                len(names),
            ).format(count=len(names)),
        )

    def _sync_host_line_ui(self) -> None:
        """Show the offer row for a Host line this dialog does not save, and
        take the Name field away while several names are typed."""
        kind, names = self._typed_host_names()
        applies = self._host_line_applies()
        editing = bool(getattr(self, "is_editing", False))
        row = getattr(self, "host_line_offer_row", None)
        if row is not None:
            # Saved from here: said plainly, so that a space is never typed
            # into several connections unnoticed. Otherwise a warning with
            # the way to Advanced Configuration.
            saved_here = applies and kind == SEVERAL and not editing
            self.host_line_offer_button.set_visible(not saved_here)
            if saved_here:
                row.remove_css_class("warning")
            else:
                row.add_css_class("warning")
            if saved_here:
                row.set_title(ngettext(
                    "This creates {count} connection: {names}",
                    "This creates {count} connections: {names}",
                    len(names),
                ).format(count=len(names), names=", ".join(names)))
                row.set_subtitle(_(
                    "A space separates hosts. They share one Host block, so "
                    "a change to one applies to all of them."
                ))
                row.set_visible(True)
            elif applies and kind == PATTERN:
                row.set_title(_("A pattern is not a connection"))
                row.set_subtitle(_(
                    "It sets options for every host it matches. Create it as "
                    "a block in Advanced Configuration."
                ))
                row.set_visible(True)
            elif applies and kind == SEVERAL and editing:
                row.set_title(_("A saved connection has one name"))
                row.set_subtitle(_(
                    "To give this block more hosts, edit its Host line in "
                    "Advanced Configuration."
                ))
                row.set_visible(True)
            else:
                row.set_visible(False)
        name_row = getattr(self, "display_name_row", None)
        if name_row is not None and not editing:
            several = applies and kind == SEVERAL
            if several != bool(getattr(self, "_name_off_for_several", False)):
                self._name_off_for_several = several
                name_row.set_sensitive(not several)
                if several:
                    name_row.set_tooltip_text(
                        _("A name is for one connection. Name each one after saving.")
                    )
                elif applies:
                    name_row.set_tooltip_text(
                        _("Shown in the connection list. Leave empty to use the SSH alias.")
                    )

    def _on_host_line_offer_clicked(self, _button) -> None:
        """Take what was typed to Advanced Configuration.

        A pattern becomes a new, unsaved block there, with the host, user
        and port typed here; a new connection's dialog is then closed, its
        Host line having moved. For a saved connection the window opens at
        its block, as the sidebar's Advanced Configuration does.
        """
        kind, names = self._typed_host_names()
        editing = bool(getattr(self, "is_editing", False))
        if kind != PATTERN:
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
            on_saved=self._on_ssh_config_saved if editing else None,
        )
        if window is not None and not editing:
            self.close()
