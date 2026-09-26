"""Login profile picker for the connection dialog's Authentication page.

While a profile is linked (explicitly or inherited from the connection's
group) the profile decides the authentication settings: the auth rows and the
Username row are locked, and the dialog shows what the profile provides. The
chosen link is stored as ``self._login_profile_change`` — ``(mode, profile_id)``
or ``None`` when unchanged — and the window applies it around the config save
(see ``MainWindow._save_connection_via_client``). An existing connection also
gets an inline preview of the Host-block settings the new link replaces.
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

    # -- building --------------------------------------------------------------

    def _build_login_profile_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title=_("Login profile"),
            description=_("A login profile supplies the username, keys and "
                          "passwords, shared with other connections."),
        )
        self.login_profile_group = group
        self.login_profile_row = Adw.ComboRow(title=_("Profile"))
        self.login_profile_row.set_model(Gtk.StringList())
        self.login_profile_row.connect("notify::selected", self._on_login_profile_selected)
        group.add(self.login_profile_row)

        self.login_profile_info_row = Adw.ActionRow()
        self.login_profile_info_row.set_subtitle_lines(0)
        self.login_profile_info_row.set_visible(False)
        group.add(self.login_profile_info_row)

        buttons = Gtk.Box(spacing=6)
        self.login_profile_new_button = Gtk.Button(label=_("New…"))
        self.login_profile_new_button.set_tooltip_text(_("Create a login profile"))
        self.login_profile_new_button.connect("clicked", self._on_login_profile_new)
        self.login_profile_save_as_button = Gtk.Button(label=_("Save as Profile…"))
        self.login_profile_save_as_button.set_tooltip_text(
            _("Create a login profile from this connection's current settings")
        )
        self.login_profile_save_as_button.connect("clicked", self._on_login_profile_save_as)
        self.login_profile_edit_button = Gtk.Button(label=_("Edit Profile…"))
        self.login_profile_edit_button.connect("clicked", self._on_login_profile_edit)
        for button in (
            self.login_profile_new_button,
            self.login_profile_save_as_button,
            self.login_profile_edit_button,
        ):
            button.add_css_class("flat")
            buttons.append(button)
        group.set_header_suffix(buttons)
        # Hidden until the daemon answers (and entirely without the capability).
        group.set_visible(False)
        return group

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
            custom_label=_("Custom (no profile)"),
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
        self.login_profile_edit_button.set_visible(locked)
        self.login_profile_save_as_button.set_visible(not locked)
        info = self.login_profile_info_row
        if not locked:
            info.set_visible(False)
            return
        from .gtk.login_profile_controller import profile_summary_line

        info.set_title(
            _("Authentication is managed by “{name}”").format(name=profile.name)
        )
        info.set_subtitle(profile_summary_line(profile))
        info.set_visible(True)
        self._refresh_login_profile_preview()

    def _refresh_login_profile_preview(self) -> None:
        """For an existing connection, show what the new link would replace."""
        connection_id = self._login_profile_connection_id()
        choice = self._current_login_profile_choice()
        if (
            connection_id is None
            or choice is None
            or self.login_profile_row.get_selected() == self._login_profile_initial_index
        ):
            return
        controller = self._login_profile_controller()
        if controller is None:
            return
        from .gtk.login_profile_controller import format_preview
        from .login_profile_dialogs import run_async

        selected = self.login_profile_row.get_selected()

        def _show(previews):
            if self.login_profile_row.get_selected() != selected:
                return
            text = format_preview(previews)
            base = self.login_profile_info_row.get_subtitle() or ""
            if text:
                lines = text.splitlines()[1:]  # drop the "<connection>:" header
                self.login_profile_info_row.set_subtitle(
                    base + "\n" + _("On save, replaces:") + "\n"
                    + "\n".join(line.strip() for line in lines)
                )

        run_async(
            lambda: controller.preview([connection_id], choice.mode, choice.profile_id),
            _show,
            lambda error: logger.debug("Login profile preview failed: %s", error),
        )

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

    def _after_profile_saved(self, summary) -> None:
        controller = self._login_profile_controller()
        snapshot = controller.snapshot if controller is not None else None
        if snapshot is not None:
            self._on_login_profile_snapshot(snapshot, select_profile_id=summary.id)

    def _on_login_profile_new(self, *_args) -> None:
        controller = self._login_profile_controller()
        if controller is None:
            return
        from .login_profile_dialogs import open_profile_editor

        open_profile_editor(self, controller, on_saved=self._after_profile_saved)

    def _on_login_profile_edit(self, *_args) -> None:
        controller = self._login_profile_controller()
        profile = self._selected_login_profile()
        if controller is None or profile is None:
            return
        from .login_profile_dialogs import open_profile_editor

        open_profile_editor(self, controller, profile=profile, on_saved=self._after_profile_saved)

    def _login_profile_values_from_dialog(self) -> dict:
        def text(name: str) -> str:
            row = getattr(self, name, None)
            try:
                return row.get_text().strip() if row is not None else ""
            except Exception:
                return ""

        def call(name: str, default: Any) -> Any:
            method = getattr(self, name, None)
            try:
                return method() if callable(method) else default
            except Exception:
                return default

        forward = call("_selected_forward_agent_fields", {}) or {}
        nickname = text("nickname_row") or text("hostname_row")
        return {
            "name": _("{name} login").format(name=nickname) if nickname else _("New profile"),
            "username": text("username_row"),
            "auth_method": call("_selected_auth_method", 0),
            "key_select_mode": call("_selected_key_mode", 0),
            "identity_files": list(call("_collect_identity_files", []) or []),
            "certificate_files": list(call("_collect_certificate_files", []) or []),
            "identity_agent": call("_selected_identity_agent", "") or "",
            "add_keys_to_agent": call("_selected_add_keys_to_agent", "") or "",
            "pkcs11_provider": text("pkcs11_provider_row"),
            "security_key_provider": text("security_key_provider_row"),
            "pubkey_auth_no": bool(
                getattr(getattr(self, "pubkey_auth_row", None), "get_active", lambda: False)()
            ),
            "forward_agent": bool(forward.get("forward_agent")),
            "forward_agent_target": str(forward.get("forward_agent_target") or ""),
            "extra_ssh_config": "",
        }

    def _on_login_profile_save_as(self, *_args) -> None:
        controller = self._login_profile_controller()
        if controller is None:
            return
        from .gtk.login_profile_controller import settings_from_values
        from .login_profile_dialogs import open_profile_editor

        try:
            initial = settings_from_values(self._login_profile_values_from_dialog())
        except (TypeError, ValueError):
            initial = None
        open_profile_editor(self, controller, initial=initial, on_saved=self._after_profile_saved)


def login_profile_locked_widgets(dialog: Any, auth_groups: List[Any]) -> List[Any]:
    """Widgets a linked profile owns: every auth group plus the Username row."""
    return [*auth_groups, getattr(dialog, "username_row", None)]
