"""Login profile picker for the connection dialog's Authentication page.

While a profile is linked (explicitly or inherited from the connection's
group) the profile decides the authentication settings: the auth rows are
locked and the Username row is dimmed showing the profile's username. The
Username row also carries a picker button so a profile can be chosen from
the Connection page. The chosen link is stored as ``self._login_profile_change`` — ``(mode, profile_id)``
or ``None`` when unchanged — and the window applies it around the config save
(see ``MainWindow._save_connection_via_client``).
"""

from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Any, List, Optional, Sequence

from gi.repository import Adw, Gtk

logger = logging.getLogger(__name__)


class ConnectionDialogLoginProfileMixin:
    """Mixed into ``ConnectionDialog``; GTK widgets are created lazily."""

    _login_profile_choices: Sequence[Any] = ()
    _login_profile_initial_index = 0
    _login_profile_snapshot = None
    _login_profile_inherited = None
    _login_profile_controller_obj = None
    _login_profile_loading = False
    _login_profile_locked_widgets: Sequence[Any] = ()
    _username_profile_button = None
    _username_profile_list = None
    # The user's own username, kept while the row shows a profile's.
    _username_before_profile: Optional[str] = None
    _applying_inherited_value = False
    _login_profiles_window = None

    # -- building --------------------------------------------------------------

    def _build_login_profile_group(self) -> Adw.PreferencesGroup:
        """The Profile picker row."""
        self.login_profile_row = Adw.ComboRow(title=_("Profile"))
        self.login_profile_row.set_model(Gtk.StringList())
        self.login_profile_row.connect("notify::selected", self._on_login_profile_selected)
        self.login_profile_manage_button = Gtk.Button()
        self.login_profile_manage_button.set_child(_profile_icon())
        self.login_profile_manage_button.set_tooltip_text(_("Manage Profiles…"))
        self.login_profile_manage_button.connect("clicked", self._on_login_profile_manage)
        self.login_profile_manage_button.add_css_class("flat")
        self.login_profile_manage_button.add_css_class("accent")
        self.login_profile_manage_button.set_valign(Gtk.Align.CENTER)
        self.login_profile_row.add_suffix(self.login_profile_manage_button)
        group = Adw.PreferencesGroup()
        group.add(self.login_profile_row)
        self.login_profile_group = group
        # Hidden until the daemon answers (and entirely without the capability).
        group.set_visible(False)
        return group

    def _add_username_profile_button(self, row) -> None:
        """Suffix on the Username row that picks a login profile."""
        button = Gtk.MenuButton()
        button.set_child(_profile_icon())
        button.set_tooltip_text(_("Use a login profile"))
        button.add_css_class("flat")
        button.set_valign(Gtk.Align.CENTER)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("navigation-sidebar")
        listbox.connect("row-activated", self._on_username_profile_activated)
        popover = Gtk.Popover()
        popover.set_child(listbox)
        popover.connect("show", lambda *_a: self._fill_username_profile_list())
        button.set_popover(popover)
        # Hidden until the daemon answers, like the Authentication page group.
        button.set_visible(False)
        row.add_suffix(button)
        self._username_profile_button = button
        self._username_profile_list = listbox

    def _fill_username_profile_list(self) -> None:
        listbox = self._username_profile_list
        if listbox is None:
            return
        child = listbox.get_first_child()
        while child is not None:
            listbox.remove(child)
            child = listbox.get_first_child()
        selected = self.login_profile_row.get_selected()
        for index, choice in enumerate(self._login_profile_choices):
            box = Gtk.Box(spacing=12)
            label = Gtk.Label(label=choice.label, xalign=0, hexpand=True)
            box.append(label)
            check = Gtk.Image.new_from_icon_name("object-select-symbolic")
            check.set_opacity(1.0 if index == selected else 0.0)
            box.append(check)
            listbox.append(box)

    def _on_username_profile_activated(self, _listbox, row) -> None:
        index = row.get_index()
        if 0 <= index < len(self._login_profile_choices):
            self.login_profile_row.set_selected(index)
        button = self._username_profile_button
        if button is not None:
            button.popdown()

    def _set_username_row_greyed(self, row, greyed: bool) -> None:
        """Dim the row's title and text the way an inherited value is dimmed.

        ``dim-label`` goes on the editable area rather than the row so the
        profile button beside it stays bright. A row already dimmed as
        inherited is left as is; dimming twice makes it unreadable.
        """
        delegate = row.get_delegate() if hasattr(row, "get_delegate") else None
        area = delegate.get_parent() if delegate is not None else None
        if area is None:
            return
        if greyed and not row.has_css_class("dim-label"):
            area.add_css_class("dim-label")
        else:
            area.remove_css_class("dim-label")

    def _set_username_text(self, row, text: str) -> None:
        # Not a user edit: must not adopt an inherited username.
        self._applying_inherited_value = True
        try:
            row.set_text(text)
        finally:
            self._applying_inherited_value = False

    def _sync_username_row(self, profile) -> None:
        """Dim the Username row showing a linked profile's username."""
        row = getattr(self, "username_row", None)
        if row is None:
            return
        if profile is None:
            if self._username_before_profile is not None:
                self._set_username_text(row, self._username_before_profile)
                self._username_before_profile = None
            row.set_editable(True)
            row.set_title(_("Username"))
            self._set_username_row_greyed(row, False)
            return
        if self._username_before_profile is None:
            self._username_before_profile = row.get_text()
        # The profile's username replaces the connection's on save.
        self._set_username_text(row, profile.settings.username)
        row.set_editable(False)
        row.set_title(_("Username (using a login profile)"))
        self._set_username_row_greyed(row, True)

    def _register_login_profile_locked_widgets(self, widgets: Sequence[Any]) -> None:
        self._login_profile_locked_widgets = tuple(w for w in widgets if w is not None)

    # -- loading ---------------------------------------------------------------

    def _login_profile_controller(self):
        if self._login_profile_controller_obj is not None:
            return self._login_profile_controller_obj
        parent = getattr(self, "parent_window", None)
        getter = getattr(parent, "_login_profile_controller", None)
        if not callable(getter):
            return None
        try:
            controller = getter()
        except Exception:
            logger.debug("Login profile controller unavailable", exc_info=True)
            return None
        self._login_profile_controller_obj = controller
        return controller

    def _login_profile_connection_id(self) -> Optional[str]:
        if not getattr(self, "is_editing", False):
            return None
        connection = getattr(self, "connection", None)
        nickname = str(getattr(connection, "nickname", "") or "")
        return nickname or None

    def _login_profile_group_context(self):
        """(primary group id, parent map) of the edited connection."""
        manager = getattr(getattr(self, "parent_window", None), "group_manager", None)
        groups = getattr(manager, "groups", None) or {}
        parents = {gid: info.get("parent_id") for gid, info in groups.items()}
        connection_id = self._login_profile_connection_id()
        group_id = None
        if connection_id and manager is not None:
            try:
                member_of = manager.get_connection_groups(connection_id) or []
                group_id = member_of[0] if member_of else None
            except Exception:
                group_id = None
        return group_id, parents

    def start_login_profile_load(self) -> None:
        """Fetch profiles off the GTK thread; the picker appears when they arrive."""
        if getattr(self, "login_profile_group", None) is None or self._login_profile_loading:
            return
        controller = self._login_profile_controller()
        if controller is None:
            return
        self._login_profile_loading = True
        from .login_profile_dialogs import run_async

        def _loaded(snapshot):
            self._login_profile_loading = False
            self._on_login_profile_snapshot(snapshot)

        def _failed(error):
            self._login_profile_loading = False
            logger.debug("Loading login profiles failed: %s", error)

        run_async(controller.refresh, _loaded, _failed)

    def _on_login_profile_snapshot(self, snapshot, *, select_profile_id: Optional[str] = None) -> None:
        from .gtk.login_profile_controller import (
            CHOICE_PROFILE,
            current_choice_index,
            profile_choices,
            resolve_group_profile,
        )

        previous = self._current_login_profile_choice()
        self._login_profile_snapshot = snapshot
        group_id, parents = self._login_profile_group_context()
        self._login_profile_inherited = resolve_group_profile(snapshot, group_id, parents)
        choices = profile_choices(
            snapshot,
            inherited=self._login_profile_inherited,
            custom_label=_("Don't use a profile"),
            inherit_label=_("Inherit from group ({name})"),
        )
        first_load = not self._login_profile_choices
        self._login_profile_choices = choices
        model = Gtk.StringList()
        for choice in choices:
            model.append(choice.label)
        if first_load:
            self._login_profile_initial_index = current_choice_index(
                choices, snapshot, self._login_profile_connection_id()
            )
            index = self._login_profile_initial_index
        else:
            index = 0
            wanted = (CHOICE_PROFILE, select_profile_id) if select_profile_id else (
                (previous.kind, previous.profile_id) if previous else None
            )
            for i, choice in enumerate(choices):
                if wanted and (choice.kind, choice.profile_id) == wanted:
                    index = i
        self.login_profile_row.set_model(model)
        self.login_profile_row.set_selected(index)
        self.login_profile_group.set_visible(snapshot.available)
        if self._username_profile_button is not None:
            self._username_profile_button.set_visible(snapshot.available)
        self._on_login_profile_selected()

    # -- selection ---------------------------------------------------------------

    def _current_login_profile_choice(self):
        choices = self._login_profile_choices
        if not choices:
            return None
        index = self.login_profile_row.get_selected()
        return choices[index] if 0 <= index < len(choices) else None

    def _selected_login_profile(self):
        from .gtk.login_profile_controller import CHOICE_INHERIT, CHOICE_PROFILE

        choice = self._current_login_profile_choice()
        snapshot = self._login_profile_snapshot
        if choice is None or snapshot is None:
            return None
        if choice.kind == CHOICE_PROFILE:
            return snapshot.profile(choice.profile_id)
        if choice.kind == CHOICE_INHERIT:
            return self._login_profile_inherited
        return None

    def _on_login_profile_selected(self, *_args) -> None:
        profile = self._selected_login_profile()
        locked = profile is not None
        for widget in self._login_profile_locked_widgets:
            try:
                widget.set_sensitive(not locked)
            except Exception:
                pass
        self._sync_username_row(profile)

    def collect_login_profile_change(self):
        """``(mode, profile_id)`` when the link changed, else ``None``."""
        choice = self._current_login_profile_choice()
        if choice is None:
            return None
        index = self.login_profile_row.get_selected()
        if getattr(self, "is_editing", False) and index == self._login_profile_initial_index:
            return None
        if not getattr(self, "is_editing", False) and choice.mode is None:
            return None
        return (choice.mode, choice.profile_id)

    # -- profile buttons ----------------------------------------------------------

    def _on_login_profile_manage(self, *_args) -> None:
        """Open the Login Profiles window; refresh the picker when it closes."""
        parent = getattr(self, "parent_window", None)
        show = getattr(parent, "show_login_profiles_window", None)
        if not callable(show):
            return
        window = show(transient_for=self)
        if window is not None and window is not self._login_profiles_window:
            self._login_profiles_window = window
            window.connect("close-request", self._on_login_profiles_window_closed)

    def _on_login_profiles_window_closed(self, *_args) -> bool:
        self._login_profiles_window = None
        self.start_login_profile_load()
        return False


def _profile_icon() -> Gtk.Image:
    """The login profile icon, from the bundled copy rather than the theme."""
    from .icon_utils import new_image_from_icon_name

    return new_image_from_icon_name("system-users-symbolic")


def login_profile_locked_widgets(dialog: Any, auth_groups: List[Any]) -> List[Any]:
    """Widgets a linked profile locks: every auth group.

    The Username row is handled by ``_sync_username_row`` instead, so its
    profile picker button stays usable while a profile is linked.
    """
    return list(auth_groups)
