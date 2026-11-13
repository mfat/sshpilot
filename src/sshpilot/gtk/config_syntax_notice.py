"""The notice that OpenSSH rejects the SSH configuration.

OpenSSH stops at a line it cannot parse, in any block of any file it reads,
and then connects to nothing. A session would only show OpenSSH's own error
in its terminal, for every host alike, so the main window says it up front:
which line of which file, and what OpenSSH makes of it.

The daemon names the lines (``connections.list_config_syntax_errors``) and
sends no sentences; the wording is here. The banner cannot be dismissed. It
goes away when OpenSSH accepts the configuration again.

The daemon announces a change of the files only when a connection changed
with it, and correcting a line usually changes none. So the main window asks
again after the SSH config editor saves and whenever it becomes the active
window, which is when someone returns from correcting the file elsewhere.
"""

from __future__ import annotations

import logging
import os
import threading
from gettext import gettext as _, ngettext
from typing import Sequence

from gi.repository import Adw, GLib, Gtk

logger = logging.getLogger(__name__)


# -- wording ----------------------------------------------------------------


def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def banner_title(errors: Sequence) -> str:
    """What is wrong and where, then OpenSSH's own words for the first line."""
    first = errors[0]
    more = len(errors) - 1
    if more > 0:
        text = ngettext(
            "SSH connections cannot start: line {line} of {file} and "
            "{count} more line in your SSH config are not valid.",
            "SSH connections cannot start: line {line} of {file} and "
            "{count} more lines in your SSH config are not valid.",
            more,
        ).format(line=first.line, file=_display_path(first.file), count=more)
    else:
        text = _(
            "SSH connections cannot start: line {line} of {file} in your SSH "
            "config is not valid."
        ).format(line=first.line, file=_display_path(first.file))
    if first.diagnostic:
        # OpenSSH's text, shown as it is. On the same line: a banner centres
        # each line of its title, which sets a second line adrift.
        text = "{} {}".format(text, first.diagnostic)
    return text


def edit_button_label() -> str:
    return _("Edit SSH Config")


# -- the notice -------------------------------------------------------------


class ConfigSyntaxNotice:
    """Banner in the main window while OpenSSH rejects the configuration.

    *window* is the main window: it supplies ``client`` and
    ``_open_ssh_config_editor``.
    """

    def __init__(self, window, container: Gtk.Box) -> None:
        self._window = window
        self._container = container
        self._banner = None
        self._checking = False
        self._recheck = False

    # OpenSSH is run to judge the files, so the check runs off the GTK
    # thread; a burst of store refreshes collapses into one more check.
    def check(self) -> None:
        client = getattr(self._window, "client", None)
        if not callable(getattr(client, "list_config_syntax_errors", None)):
            return
        if self._checking:
            self._recheck = True
            return
        self._checking = True

        def worker():
            errors = None
            try:
                errors = tuple(client.list_config_syntax_errors())
            except Exception:
                logger.debug("SSH config syntax errors unavailable", exc_info=True)
            GLib.idle_add(self._on_checked, errors)

        threading.Thread(target=worker, daemon=True).start()

    def _on_checked(self, errors) -> bool:
        self._checking = False
        if self._recheck:
            # The files changed while OpenSSH was reading them: ask again
            # and do not show an answer about the older text.
            self._recheck = False
            self.check()
            return False
        if errors is not None:
            self._apply(errors)
        return False

    def _apply(self, errors: tuple) -> None:
        if errors:
            self._show(errors)
        else:
            self._hide()

    def _show(self, errors: tuple) -> None:
        banner = self._banner
        if banner is None:
            banner = Adw.Banner()
            try:
                banner.set_use_markup(False)
            except Exception:
                pass
            # Solid error colours (style.css): nothing connects until this
            # is fixed, so it must not read as a passing hint.
            banner.add_css_class("error")
            banner.set_button_label(edit_button_label())
            banner.connect("button-clicked", self._on_edit)
            self._container.append(banner)
            self._banner = banner
        banner.set_title(banner_title(errors))
        self._container.set_visible(True)
        banner.set_revealed(True)

    def _hide(self) -> None:
        if self._banner is not None:
            self._banner.set_revealed(False)
        self._container.set_visible(False)

    def _on_edit(self, *_args) -> None:
        opener = getattr(self._window, "_open_ssh_config_editor", None)
        if not callable(opener):
            return
        try:
            opener()
        except Exception:
            logger.debug("Could not open the SSH config editor", exc_info=True)
