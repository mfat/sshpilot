"""The one-time notice about saved hosts whose own settings are not used.

Until SSH Pilot launched hosts exactly as ``ssh <alias>``, it repeated each
Host block's own settings on the command line, so they beat a ``Host *`` or
``Match`` block placed before them. Now OpenSSH's own rule applies: the first
value it finds wins. A host whose block sets something an earlier block
already decides therefore connects differently after the upgrade.

The daemon lists those values (``connections.list_config_conflicts``) and
sends no sentences; the wording is here. The notice is shown to installs that
predate the change only -- a fresh install never saw the old behaviour -- and
once dismissed, or once nothing is left to report, it never comes back. The
connection editor keeps marking such fields regardless.
"""

from __future__ import annotations

import logging
import os
import threading
from gettext import gettext as _, ngettext
from typing import Dict, List, Sequence

from gi.repository import Adw, GLib, Gtk

logger = logging.getLogger(__name__)

#: ``ui.<key>`` in config.json. A fresh install's defaults set it to False;
#: a settings file from before it existed lacks it, and that means pending.
SETTING_KEY = "ui.config_precedence_notice"


def notice_pending(config) -> bool:
    """Whether this install still has the notice to show."""
    try:
        return bool(config.get_setting(SETTING_KEY, True))
    except Exception:
        return False


def retire_notice(config) -> None:
    """Never show the notice again."""
    try:
        config.set_setting(SETTING_KEY, False)
    except Exception:
        logger.debug("Could not record the precedence notice as done", exc_info=True)


# -- wording ----------------------------------------------------------------


def _display_path(path: str) -> str:
    home = os.path.expanduser("~")
    if home and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


def banner_title(host_count: int) -> str:
    return ngettext(
        "{count} saved host does not use some of its own settings, because an "
        "earlier block in your SSH config sets them first.",
        "{count} saved hosts do not use some of their own settings, because an "
        "earlier block in your SSH config sets them first.",
        host_count,
    ).format(count=host_count)


def review_button_label() -> str:
    return _("Review")


def review_heading() -> str:
    return _("Settings Not Used")


def review_body() -> str:
    return _(
        "SSH Pilot now connects exactly as “ssh <host>” does in a terminal: "
        "the first value OpenSSH finds for a setting is the one it uses. These "
        "hosts set a value that an earlier block in your SSH config already "
        "decides, so their own value is not used. To use it, remove the line "
        "from the earlier block, or move that block below the host."
    )


def _keyword(key: str) -> str:
    """The keyword as OpenSSH documents it (``User``), from the lower-case key."""
    try:
        from ..ssh_option_list import OPENSSH_KEYWORDS
    except Exception:
        return key
    for name in OPENSSH_KEYWORDS:
        if name.lower() == key:
            return name
    return key


def conflict_text(conflict) -> str:
    """One line per unused value: what is used, where from, and what is not."""
    earlier = conflict.earlier
    keyword = _keyword(conflict.key)
    if earlier.block:
        template = _(
            "{keyword}: uses “{value}” from “{block}” ({file}, line {line}), "
            "not “{own}”"
        )
    else:
        template = _(
            "{keyword}: uses “{value}” from {file}, line {line}, above every "
            "Host block, not “{own}”"
        )
    return template.format(
        keyword=keyword,
        value=earlier.value,
        block=earlier.block,
        file=_display_path(earlier.file),
        line=earlier.line,
        own=conflict.own_value,
    )


def nothing_left_text() -> str:
    return _("Every saved host uses its own settings now.")


def edit_host_tooltip() -> str:
    return _("Edit this connection")


# -- grouping ---------------------------------------------------------------


def conflicts_by_host(conflicts: Sequence) -> Dict[str, List]:
    """Conflicts grouped by host, hosts in the order the daemon listed them."""
    grouped: Dict[str, List] = {}
    for conflict in conflicts:
        grouped.setdefault(conflict.host, []).append(conflict)
    return grouped


# -- the notice -------------------------------------------------------------


class ConfigPrecedenceNotice:
    """Banner in the main window, and the Review window it opens.

    *window* is the main window: it supplies ``client``, ``config``,
    ``connection_manager``, ``show_connection_dialog`` and the container the
    banner goes in.
    """

    def __init__(self, window, container: Gtk.Box) -> None:
        self._window = window
        self._container = container
        self._banner = None
        self._conflicts: tuple = ()
        self._checking = False
        self._recheck = False
        self._review = None

    # The daemon walks every saved host's files, so the check runs off the
    # GTK thread; a burst of store refreshes collapses into one more check.
    def check(self) -> None:
        if not notice_pending(getattr(self._window, "config", None)):
            self._hide()
            return
        client = getattr(self._window, "client", None)
        if not callable(getattr(client, "list_config_conflicts", None)):
            return
        if self._checking:
            self._recheck = True
            return
        self._checking = True

        def worker():
            conflicts = None
            try:
                conflicts = tuple(client.list_config_conflicts())
            except Exception:
                logger.debug("SSH config conflicts unavailable", exc_info=True)
            GLib.idle_add(self._on_checked, conflicts)

        threading.Thread(target=worker, daemon=True).start()

    def _on_checked(self, conflicts) -> bool:
        self._checking = False
        if conflicts is not None:
            self._apply(conflicts)
        if self._recheck:
            self._recheck = False
            self.check()
        return False

    def _apply(self, conflicts: tuple) -> None:
        self._conflicts = conflicts
        if not conflicts:
            # Nothing to report: an upgrade that needed no notice, or every
            # conflict fixed. Either way, never ask again.
            if self._banner is not None or self._review is not None:
                self._hide()
            retire_notice(getattr(self._window, "config", None))
            self._refresh_review()
            return
        self._show(len(conflicts_by_host(conflicts)))
        self._refresh_review()

    def _show(self, host_count: int) -> None:
        banner = self._banner
        if banner is None:
            banner = Adw.Banner()
            try:
                banner.set_use_markup(False)
            except Exception:
                pass
            banner.set_button_label(review_button_label())
            banner.connect("button-clicked", self._on_review)
            self._container.append(banner)
            self._banner = banner
        banner.set_title(banner_title(host_count))
        self._container.set_visible(True)
        banner.set_revealed(True)

    def _hide(self) -> None:
        if self._banner is not None:
            self._banner.set_revealed(False)
        self._container.set_visible(False)

    # -- review window ---------------------------------------------------

    def _on_review(self, *_args) -> None:
        if self._review is not None:
            self._review.present()
            return
        review = Adw.Window()
        review.set_title(review_heading())
        review.set_transient_for(self._window)
        review.set_modal(False)
        review.set_default_size(620, 520)
        review.connect("close-request", self._on_review_closed)
        try:
            from ..shortcut_utils import install_esc_to_close

            install_esc_to_close(review)
        except Exception:
            pass

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        footer.set_margin_start(24)
        footer.set_margin_end(24)
        footer.set_margin_top(12)
        footer.set_margin_bottom(12)
        footer.set_halign(Gtk.Align.END)
        dismiss = Gtk.Button(label=_("Don't Show Again"))
        dismiss.set_tooltip_text(_(
            "Hide this notice for good. The connection editor still marks "
            "each setting that is not used."
        ))
        dismiss.connect("clicked", self._on_dismiss)
        footer.append(dismiss)
        toolbar.add_bottom_bar(footer)

        review.set_content(toolbar)
        self._review = review
        self._review_page = page
        self._review_groups: List = []
        self._refresh_review()
        review.present()

    def _on_review_closed(self, *_args) -> bool:
        self._review = None
        return False

    def _refresh_review(self) -> None:
        page = getattr(self, "_review_page", None)
        if self._review is None or page is None:
            return
        for group in self._review_groups:
            page.remove(group)
        self._review_groups = []

        grouped = conflicts_by_host(self._conflicts)
        if not grouped:
            done = Adw.PreferencesGroup()
            row = Adw.ActionRow()
            row.set_title(nothing_left_text())
            done.add(row)
            page.add(done)
            self._review_groups.append(done)
            return

        group = Adw.PreferencesGroup()
        # A group description is Pango markup, and the text holds “<host>”.
        group.set_description(GLib.markup_escape_text(review_body()))
        for host, items in grouped.items():
            row = Adw.ActionRow()
            row.set_title(host)
            try:
                row.set_title_selectable(False)
                row.set_use_markup(False)
            except Exception:
                pass
            row.set_subtitle("\n".join(conflict_text(item) for item in items))
            row.set_activatable(True)
            row.set_tooltip_text(edit_host_tooltip())
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.connect("activated", self._on_host_activated, items[0])
            group.add(row)
        page.add(group)
        self._review_groups.append(group)

    def _on_host_activated(self, _row, conflict) -> None:
        manager = getattr(self._window, "connection_manager", None)
        connection = None
        for finder, key in (
            ("get_connection_by_id", conflict.connection_id),
            ("find_connection_by_nickname", conflict.host),
        ):
            lookup = getattr(manager, finder, None)
            if callable(lookup):
                try:
                    connection = lookup(key)
                except Exception:
                    connection = None
            if connection is not None:
                break
        if connection is None:
            logger.debug("Saved host %s is no longer listed", conflict.host)
            self.check()
            return
        try:
            self._window.show_connection_dialog(connection)
        except Exception:
            logger.debug("Could not open the connection editor", exc_info=True)

    def _on_dismiss(self, *_args) -> None:
        retire_notice(getattr(self._window, "config", None))
        self._hide()
        if self._review is not None:
            self._review.close()
