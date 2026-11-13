"""The notice about saved hosts whose own settings are not used.

SSH Pilot launches a saved host exactly as ``ssh <alias>``, so OpenSSH's rule
applies: the first value it finds for a setting wins. A host whose own block
sets something that a ``Host *`` or ``Match`` block placed before it already
sets never uses its own value. That happens after an upgrade from a version
that repeated the block on the command line, and after any edit to the file.

The daemon lists those values (``connections.list_config_conflicts``) and
sends no sentences; the wording is here. The banner shows while a conflict
exists that the user has not dismissed. Dismissing remembers the conflicts
shown then, so the banner returns only for a new one. The connection editor
marks such fields regardless.
"""

from __future__ import annotations

import logging
import os
import threading
from gettext import gettext as _, ngettext
from typing import Dict, List, Sequence

from gi.repository import Adw, GLib, Gtk

logger = logging.getLogger(__name__)

#: ``ui.<key>`` in config.json: the conflicts the user dismissed, as
#: :func:`conflict_fingerprint` strings, per configuration scope
#: (``{"default": [...], "isolated": [...]}``). Default and isolated mode read
#: different SSH configs, so what one dismissed says nothing about the other.
SETTING_KEY = "ui.config_conflicts_dismissed"


def conflict_fingerprint(conflict) -> str:
    """What identifies one conflict across launches.

    The host, the option, its unused value and the value used. Not the file
    or line: editing an unrelated line above must not bring a dismissed
    conflict back.
    """
    return "\x1f".join(
        (conflict.host, conflict.key, conflict.own_value, conflict.earlier.value)
    )


def config_scope(config) -> str:
    """Which SSH config the current conflicts come from."""
    try:
        isolated = bool(config.get_setting("ssh.use_isolated_config", False))
    except Exception:
        isolated = False
    return "isolated" if isolated else "default"


def _stored(config) -> Dict[str, List[str]]:
    try:
        stored = config.get_setting(SETTING_KEY, {})
    except Exception:
        return {}
    if isinstance(stored, list):
        # Written before dismissals were kept per scope: default mode.
        stored = {"default": stored}
    if not isinstance(stored, dict):
        return {}
    return {
        str(scope): [item for item in items if isinstance(item, str)]
        for scope, items in stored.items()
        if isinstance(items, list)
    }


def dismissed_fingerprints(config, scope: str) -> frozenset:
    return frozenset(_stored(config).get(scope, ()))


def store_dismissed(config, scope: str, fingerprints) -> None:
    stored = _stored(config)
    stored[scope] = sorted(fingerprints)
    try:
        config.set_setting(SETTING_KEY, stored)
    except Exception:
        logger.debug("Could not record the dismissed conflicts", exc_info=True)


def _fingerprint_host(fingerprint: str) -> str:
    return fingerprint.split("\x1f", 1)[0]


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
        "Some values set by these hosts are not used because an earlier "
        "block in your SSH config already sets them. To use them, remove the "
        "line from the earlier block, or move that block below the host."
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
        client = getattr(self._window, "client", None)
        if not callable(getattr(client, "list_config_conflicts", None)):
            return
        if self._checking:
            self._recheck = True
            return
        self._checking = True
        started = self._scan_state()

        def worker():
            conflicts = None
            try:
                conflicts = tuple(client.list_config_conflicts())
            except Exception:
                logger.debug("SSH config conflicts unavailable", exc_info=True)
            GLib.idle_add(self._on_checked, conflicts, started)

        threading.Thread(target=worker, daemon=True).start()

    def _scan_state(self) -> tuple:
        """What a scan's answer belongs to: the mode and the store generation.

        A mode switch or a store change while a scan runs makes its answer
        about a configuration that is no longer the current one; applying it
        would show stale conflicts and could forget dismissals of the other
        mode.
        """
        manager = getattr(self._window, "connection_manager", None)
        try:
            generation = getattr(manager.snapshot(), "generation", None)
        except Exception:
            generation = None
        return (config_scope(getattr(self._window, "config", None)), generation)

    def _on_checked(self, conflicts, started=None) -> bool:
        self._checking = False
        stale = self._recheck or (
            started is not None and started != self._scan_state()
        )
        if stale:
            # A newer scan is due; this answer is not applied at all.
            self._recheck = False
            self.check()
            return False
        if conflicts is not None:
            self._apply(conflicts)
        return False

    def _config_unreadable(self) -> bool:
        manager = getattr(self._window, "connection_manager", None)
        try:
            snapshot = manager.snapshot()
        except Exception:
            return False
        return getattr(snapshot, "ssh_config_unavailable", None) is not None

    def _loaded_hosts(self) -> frozenset:
        manager = getattr(self._window, "connection_manager", None)
        try:
            return frozenset(
                str(getattr(item, "nickname", "") or "")
                for item in (manager.connections or ())
            )
        except Exception:
            return frozenset()

    def _apply(self, conflicts: tuple) -> None:
        if self._config_unreadable():
            # An empty answer here says nothing about the file: keep every
            # dismissal and show nothing until it can be read again.
            self._conflicts = ()
            self._hide()
            self._refresh_review()
            return
        self._conflicts = conflicts
        config = getattr(self._window, "config", None)
        scope = config_scope(config)
        current = {conflict_fingerprint(item) for item in conflicts}
        dismissed = dismissed_fingerprints(config, scope)
        # A dismissed conflict that is gone is forgotten, so its return is
        # news again -- but only for a host that is loaded now. A host
        # missing from the list (a mode switch still settling) proves nothing.
        loaded = self._loaded_hosts()
        gone = {
            item for item in dismissed - current
            if _fingerprint_host(item) in loaded
        }
        if gone:
            dismissed = dismissed - gone
            store_dismissed(config, scope, dismissed)
        new = [item for item in conflicts if conflict_fingerprint(item) not in dismissed]
        if new:
            self._show(len(conflicts_by_host(new)))
        else:
            self._hide()
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
            "Hide this notice for these hosts. It returns only if another "
            "setting stops being used. The connection editor still marks each "
            "setting that is not used."
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
        config = getattr(self._window, "config", None)
        store_dismissed(
            config,
            config_scope(config),
            dismissed_fingerprints(config, config_scope(config))
            | {conflict_fingerprint(item) for item in self._conflicts},
        )
        self._hide()
        if self._review is not None:
            self._review.close()
