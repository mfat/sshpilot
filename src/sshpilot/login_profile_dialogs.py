"""Login profile UI: management window, editor, and assignment dialogs.

All daemon calls go through :class:`LoginProfileController` on a worker
thread (:func:`run_async`); results are delivered on the GTK thread. The
dialogs never touch SSH config, secrets, or connection state directly.
"""

from __future__ import annotations

import logging
import threading
from gettext import gettext as _, ngettext
from typing import Callable, Dict, List, Mapping, Optional, Sequence

from gi.repository import Adw, GLib, Gtk

from sshpilot.api.models.login_profiles import (
    LoginProfileLinkMode,
    LoginProfileSettings,
    LoginProfileSummary,
)
from sshpilot.gtk.login_profile_controller import (
    CHOICE_CUSTOM,
    CHOICE_INHERIT,
    LoginProfileController,
    ProfileChoice,
    affected_items,
    current_choice_index,
    default_group_members_to_link,
    format_preview,
    profile_choices,
    profile_summary_line,
    settings_from_values,
)

logger = logging.getLogger(__name__)

_AUTH_OPTIONS = (_("Key-based"), _("Password"))
_KEY_MODE_OPTIONS = (
    _("Automatic"),
    _("Specific keys only"),
    _("Specific keys and agent"),
)
_ADD_KEYS_VALUES = ("", "yes", "no", "ask", "confirm")
_ADD_KEYS_LABELS = (_("Default"), _("Yes"), _("No"), _("Ask"), _("Confirm"))


def error_text(error: BaseException) -> str:
    message = getattr(error, "message", None)
    return str(message or error) or _("The operation failed")


def run_async(
    operation: Callable[[], object],
    on_success: Callable[[object], None],
    on_error: Callable[[BaseException], None],
) -> None:
    """Run a blocking controller call off the GTK thread."""

    def _worker():
        try:
            result = operation()
        except BaseException as exc:  # noqa: BLE001 - delivered to the UI
            failure = exc  # ``exc`` is unbound after the except block
            GLib.idle_add(lambda: (on_error(failure), False)[1])
            return
        GLib.idle_add(lambda: (on_success(result), False)[1])

    threading.Thread(target=_worker, daemon=True, name="login-profiles").start()


def _string_list(items: Sequence[str]) -> Gtk.StringList:
    model = Gtk.StringList()
    for item in items:
        model.append(item)
    return model


def _dialog_frame(title: str, confirm_label: str) -> tuple:
    """An ``Adw.Dialog`` with Cancel/confirm header and a preferences page."""
    dialog = Adw.Dialog()
    dialog.set_title(title)
    dialog.set_content_width(520)
    dialog.set_content_height(620)
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_end_title_buttons(False)
    header.set_show_start_title_buttons(False)
    cancel = Gtk.Button(label=_("Cancel"))
    cancel.connect("clicked", lambda *_a: dialog.close())
    header.pack_start(cancel)
    confirm = Gtk.Button(label=confirm_label)
    confirm.add_css_class("suggested-action")
    header.pack_end(confirm)
    toolbar.add_top_bar(header)
    banner = Adw.Banner()
    banner.set_revealed(False)
    toolbar.add_top_bar(banner)
    page = Adw.PreferencesPage()
    toolbar.set_content(page)
    dialog.set_child(toolbar)
    return dialog, page, confirm, banner


def _show_error(banner: Adw.Banner, error: BaseException) -> None:
    banner.set_title(error_text(error))
    banner.set_revealed(True)


# ---------------------------------------------------------------------------
# Path list (identity / certificate files)
# ---------------------------------------------------------------------------

class _PathListGroup:
    """A preferences group editing an ordered list of file paths."""

    def __init__(self, title: str, description: str, paths: Sequence[str]) -> None:
        self.group = Adw.PreferencesGroup(title=title, description=description)
        add = Gtk.Button(icon_name="list-add-symbolic")
        add.add_css_class("flat")
        add.set_tooltip_text(_("Add file…"))
        add.connect("clicked", self._on_add)
        self.group.set_header_suffix(add)
        self._rows: List[Adw.ActionRow] = []
        self._paths: List[str] = []
        for path in paths:
            self._append(path)

    @property
    def paths(self) -> List[str]:
        return list(self._paths)

    def set_sensitive(self, value: bool) -> None:
        self.group.set_sensitive(value)

    def _append(self, path: str) -> None:
        path = path.strip()
        if not path or path in self._paths:
            return
        row = Adw.ActionRow(title=path)
        row.set_title_lines(1)
        remove = Gtk.Button(icon_name="user-trash-symbolic")
        remove.add_css_class("flat")
        remove.set_valign(Gtk.Align.CENTER)
        remove.set_tooltip_text(_("Remove"))
        remove.connect("clicked", lambda *_a: self._remove(row, path))
        row.add_suffix(remove)
        self.group.add(row)
        self._rows.append(row)
        self._paths.append(path)

    def _remove(self, row: Adw.ActionRow, path: str) -> None:
        self.group.remove(row)
        self._rows.remove(row)
        self._paths.remove(path)

    def _on_add(self, button: Gtk.Button) -> None:
        chooser = Gtk.FileDialog()
        chooser.set_title(_("Select a file"))
        root = button.get_root()

        def _done(dialog, result):
            try:
                file = dialog.open_finish(result)
            except GLib.Error:
                return
            if file is not None and file.get_path():
                self._append(file.get_path())

        chooser.open(root if isinstance(root, Gtk.Window) else None, None, _done)


# ---------------------------------------------------------------------------
# Profile editor
# ---------------------------------------------------------------------------

class LoginProfileEditor:
    """Form for one profile. ``on_submit(settings, password, sudo, done)``.

    ``password``/``sudo`` are secret edits: ``None`` keeps the stored secret,
    ``""`` clears it, any other value replaces it. ``done(error)`` re-enables
    the form (and shows ``error``) or closes it when ``error`` is ``None``.
    """

    def __init__(
        self,
        *,
        profile: Optional[LoginProfileSummary] = None,
        initial: Optional[LoginProfileSettings] = None,
        on_submit: Callable[..., None],
    ) -> None:
        self.profile = profile
        settings = profile.settings if profile is not None else initial
        if settings is None:
            settings = LoginProfileSettings(name=_("New profile"))
        self._on_submit = on_submit
        title = _("Edit Login Profile") if profile else _("New Login Profile")
        self.dialog, page, self._save, self._banner = _dialog_frame(title, _("Save"))
        self._save.connect("clicked", self._on_save_clicked)

        general = Adw.PreferencesGroup(title=_("Profile"))
        self.name_row = Adw.EntryRow(title=_("Name"))
        self.name_row.set_text(settings.name)
        general.add(self.name_row)
        self.username_row = Adw.EntryRow(title=_("Username"))
        self.username_row.set_text(settings.username)
        general.add(self.username_row)
        page.add(general)

        auth = Adw.PreferencesGroup(title=_("Authentication"))
        self.auth_row = Adw.ComboRow(title=_("Method"), model=_string_list(_AUTH_OPTIONS))
        self.auth_row.set_selected(settings.auth_method)
        self.auth_row.connect("notify::selected", self._sync_visibility)
        auth.add(self.auth_row)
        self.key_mode_row = Adw.ComboRow(
            title=_("Key selection"), model=_string_list(_KEY_MODE_OPTIONS)
        )
        self.key_mode_row.set_selected(settings.key_select_mode)
        self.key_mode_row.connect("notify::selected", self._sync_visibility)
        auth.add(self.key_mode_row)
        self.pubkey_no_row = Adw.SwitchRow(
            title=_("Disable public key authentication"),
            subtitle=_("PubkeyAuthentication no"),
        )
        self.pubkey_no_row.set_active(settings.pubkey_auth_no)
        auth.add(self.pubkey_no_row)
        page.add(auth)

        self.keys = _PathListGroup(
            _("Private keys"), _("IdentityFile, in order"), settings.identity_files
        )
        page.add(self.keys.group)
        self.certs = _PathListGroup(
            _("Certificates"), _("CertificateFile"), settings.certificate_files
        )
        page.add(self.certs.group)

        agent = Adw.PreferencesGroup(title=_("Agent and hardware keys"))
        self.identity_agent_row = Adw.EntryRow(title=_("IdentityAgent"))
        self.identity_agent_row.set_text(settings.identity_agent)
        agent.add(self.identity_agent_row)
        self.add_keys_row = Adw.ComboRow(
            title=_("Add keys to agent"), model=_string_list(_ADD_KEYS_LABELS)
        )
        value = settings.add_keys_to_agent
        self._add_keys_custom = value if value not in _ADD_KEYS_VALUES else ""
        self.add_keys_row.set_selected(
            _ADD_KEYS_VALUES.index(value) if value in _ADD_KEYS_VALUES else 0
        )
        agent.add(self.add_keys_row)
        self.pkcs11_row = Adw.EntryRow(title=_("PKCS#11 provider"))
        self.pkcs11_row.set_text(settings.pkcs11_provider)
        agent.add(self.pkcs11_row)
        self.sk_row = Adw.EntryRow(title=_("FIDO security key provider"))
        self.sk_row.set_text(settings.security_key_provider)
        agent.add(self.sk_row)
        self.forward_agent_row = Adw.SwitchRow(title=_("Forward agent"))
        self.forward_agent_row.set_active(settings.forward_agent)
        self.forward_agent_row.connect("notify::active", self._sync_visibility)
        agent.add(self.forward_agent_row)
        self.forward_target_row = Adw.EntryRow(title=_("Agent socket to forward (optional)"))
        self.forward_target_row.set_text(settings.forward_agent_target)
        agent.add(self.forward_target_row)
        page.add(agent)

        secrets = Adw.PreferencesGroup(
            title=_("Secrets"),
            description=_("Stored in your secure storage and shared by every "
                          "connection using this profile."),
        )
        self.password_row = Adw.PasswordEntryRow(title=_("Login password"))
        secrets.add(self.password_row)
        self.clear_password_row = Adw.SwitchRow(title=_("Remove stored login password"))
        self.clear_password_row.set_visible(bool(profile and profile.has_password))
        secrets.add(self.clear_password_row)
        self.sudo_row = Adw.PasswordEntryRow(title=_("Sudo password"))
        secrets.add(self.sudo_row)
        self.clear_sudo_row = Adw.SwitchRow(title=_("Remove stored sudo password"))
        self.clear_sudo_row.set_visible(bool(profile and profile.has_sudo_password))
        secrets.add(self.clear_sudo_row)
        if profile is not None:
            if profile.has_password:
                self.password_row.set_title(_("Login password (stored — type to replace)"))
            if profile.has_sudo_password:
                self.sudo_row.set_title(_("Sudo password (stored — type to replace)"))
        page.add(secrets)

        extra = Adw.PreferencesGroup(
            title=_("Extra SSH options"),
            description=_("One directive per line, e.g. ServerAliveInterval 30. "
                          "They replace the same directives of linked connections."),
        )
        self.extra_view = Gtk.TextView()
        self.extra_view.set_monospace(True)
        self.extra_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.extra_view.set_size_request(-1, 90)
        self.extra_view.get_buffer().set_text(settings.extra_ssh_config)
        frame = Gtk.Frame()
        frame.set_child(self.extra_view)
        extra.add(frame)
        page.add(extra)
        self._sync_visibility()

    def present(self, parent: Gtk.Widget) -> None:
        self.dialog.present(parent)

    def _sync_visibility(self, *_args) -> None:
        key_auth = self.auth_row.get_selected() == 0
        specific = self.key_mode_row.get_selected() in (1, 2)
        self.key_mode_row.set_visible(key_auth)
        self.keys.group.set_visible(key_auth and specific)
        self.certs.group.set_visible(key_auth)
        self.pubkey_no_row.set_visible(not key_auth)
        self.forward_target_row.set_visible(self.forward_agent_row.get_active())

    def values(self) -> Dict[str, object]:
        buffer = self.extra_view.get_buffer()
        extra = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        selected = self.add_keys_row.get_selected()
        add_keys = _ADD_KEYS_VALUES[selected] if selected < len(_ADD_KEYS_VALUES) else ""
        if selected == 0 and self._add_keys_custom:
            add_keys = self._add_keys_custom
        return {
            "name": self.name_row.get_text(),
            "username": self.username_row.get_text(),
            "auth_method": self.auth_row.get_selected(),
            "key_select_mode": self.key_mode_row.get_selected(),
            "identity_files": self.keys.paths,
            "certificate_files": self.certs.paths,
            "identity_agent": self.identity_agent_row.get_text(),
            "add_keys_to_agent": add_keys,
            "pkcs11_provider": self.pkcs11_row.get_text(),
            "security_key_provider": self.sk_row.get_text(),
            "pubkey_auth_no": self.pubkey_no_row.get_active(),
            "forward_agent": self.forward_agent_row.get_active(),
            "forward_agent_target": self.forward_target_row.get_text(),
            "extra_ssh_config": extra,
        }

    @staticmethod
    def _secret_edit(entry: Adw.PasswordEntryRow, clear: Adw.SwitchRow) -> Optional[str]:
        if clear.get_visible() and clear.get_active():
            return ""
        text = entry.get_text()
        return text if text else None

    def _on_save_clicked(self, *_args) -> None:
        try:
            settings = settings_from_values(self.values())
        except (TypeError, ValueError) as error:
            _show_error(self._banner, error)
            return
        self._banner.set_revealed(False)
        self._save.set_sensitive(False)

        def _done(error: Optional[BaseException] = None) -> None:
            if error is None:
                self.dialog.close()
                return
            self._save.set_sensitive(True)
            _show_error(self._banner, error)

        self._on_submit(
            settings,
            self._secret_edit(self.password_row, self.clear_password_row),
            self._secret_edit(self.sudo_row, self.clear_sudo_row),
            _done,
        )


def open_profile_editor(
    parent: Gtk.Widget,
    controller: LoginProfileController,
    *,
    profile: Optional[LoginProfileSummary] = None,
    initial: Optional[LoginProfileSettings] = None,
    on_saved: Optional[Callable[[LoginProfileSummary], None]] = None,
) -> LoginProfileEditor:
    """Create or edit a profile through the daemon."""

    # After a create, later saves from the same form must update that
    # profile (e.g. retrying a password the backend rejected).
    state = {"profile": profile}

    def _submit(settings, password, sudo, done):
        current = state["profile"]
        if current is None:
            operation = lambda: controller.create(  # noqa: E731
                settings, password=password, sudo_password=sudo
            )
        else:
            operation = lambda: controller.update(  # noqa: E731
                current.id,
                settings,
                expected_revision=None if current is not profile else current.revision,
                password=password,
                sudo_password=sudo,
            )

        def _ok(summary):
            done(None)
            if on_saved is not None:
                on_saved(summary)

        def _failed(error):
            saved = getattr(error, "summary", None)
            if saved is not None:
                state["profile"] = saved
                if on_saved is not None:
                    on_saved(saved)
            done(error)

        run_async(operation, _ok, _failed)

    editor = LoginProfileEditor(profile=profile, initial=initial, on_submit=_submit)
    editor.present(parent)
    return editor


# ---------------------------------------------------------------------------
# Confirmation helpers
# ---------------------------------------------------------------------------

def confirm_preview(
    parent: Gtk.Widget,
    previews,
    on_confirm: Callable[[], None],
    *,
    heading: Optional[str] = None,
) -> None:
    """Show the Host-block changes an assignment makes; skip when there are none."""
    text = format_preview(previews)
    if not text:
        on_confirm()
        return
    alert = Adw.AlertDialog(
        heading=heading or _("Replace connection settings?"),
        body=_("The login profile will replace these settings:") + "\n\n" + text,
    )
    alert.add_response("cancel", _("Cancel"))
    alert.add_response("apply", _("Apply Profile"))
    alert.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
    alert.set_default_response("apply")
    alert.set_close_response("cancel")

    def _response(_dialog, response):
        if response == "apply":
            on_confirm()

    alert.connect("response", _response)
    alert.present(parent)


def show_error_alert(parent: Gtk.Widget, heading: str, error: BaseException) -> None:
    alert = Adw.AlertDialog(heading=heading, body=error_text(error))
    alert.add_response("ok", _("OK"))
    alert.present(parent)


# ---------------------------------------------------------------------------
# Assignment (bulk connections) and group profile dialogs
# ---------------------------------------------------------------------------

def show_assign_dialog(
    parent: Gtk.Widget,
    controller: LoginProfileController,
    connection_ids: Sequence[str],
    *,
    on_done: Optional[Callable[[], None]] = None,
) -> None:
    """Pick a profile for one or more connections, preview, then assign."""
    snapshot = controller.snapshot
    if snapshot is None:
        run_async(
            controller.refresh,
            lambda _s: show_assign_dialog(parent, controller, connection_ids, on_done=on_done),
            lambda error: show_error_alert(parent, _("Login profiles unavailable"), error),
        )
        return
    choices = profile_choices(snapshot, include_inherit=False)
    # "Inherit from group" is offered unconditionally here: each connection
    # resolves its own primary group's profile.
    choices = choices[:1] + (
        ProfileChoice(CHOICE_INHERIT, _("Inherit from group")),
    ) + choices[1:]
    count = len(connection_ids)
    dialog, page, apply_button, banner = _dialog_frame(
        _("Assign Login Profile"), _("Apply")
    )
    dialog.set_content_height(320)
    group = Adw.PreferencesGroup(
        description=(
            ngettext(
                "Applies to {count} connection.", "Applies to {count} connections.", count
            ).format(count=count)
            if count > 1 else connection_ids[0]
        )
    )
    combo = Adw.ComboRow(title=_("Login profile"), model=_string_list([c.label for c in choices]))
    if count == 1:
        combo.set_selected(current_choice_index(choices, snapshot, connection_ids[0]))
    group.add(combo)
    page.add(group)

    def _apply(*_args):
        choice = choices[combo.get_selected()]
        apply_button.set_sensitive(False)

        def _assign():
            run_async(
                lambda: controller.assign(connection_ids, choice.mode, choice.profile_id),
                lambda _r: (dialog.close(), on_done() if on_done else None),
                lambda error: (apply_button.set_sensitive(True), _show_error(banner, error)),
            )

        if choice.kind == CHOICE_CUSTOM:
            _assign()
            return

        def _previewed(previews):
            apply_button.set_sensitive(True)
            confirm_preview(dialog, previews, lambda: (apply_button.set_sensitive(False), _assign()))

        run_async(
            lambda: controller.preview(connection_ids, choice.mode, choice.profile_id),
            _previewed,
            lambda error: (apply_button.set_sensitive(True), _show_error(banner, error)),
        )

    apply_button.connect("clicked", _apply)
    dialog.present(parent)


def show_group_profile_dialog(
    parent: Gtk.Widget,
    controller: LoginProfileController,
    group_id: str,
    group_name: str,
    member_ids: Sequence[str],
    *,
    on_done: Optional[Callable[[], None]] = None,
) -> None:
    """Assign or clear a group's profile and choose which members inherit it."""
    snapshot = controller.snapshot
    if snapshot is None:
        run_async(
            controller.refresh,
            lambda _s: show_group_profile_dialog(
                parent, controller, group_id, group_name, member_ids, on_done=on_done
            ),
            lambda error: show_error_alert(parent, _("Login profiles unavailable"), error),
        )
        return
    profiles = list(snapshot.profiles)
    labels = [_("None")] + [p.name for p in profiles]
    dialog, page, apply_button, banner = _dialog_frame(
        _("Group Login Profile"), _("Apply")
    )
    top = Adw.PreferencesGroup(
        title=group_name,
        description=_("Connections in this group, and in nested groups without "
                      "their own profile, can inherit it."),
    )
    combo = Adw.ComboRow(title=_("Login profile"), model=_string_list(labels))
    current = snapshot.group_profile_id(group_id)
    for index, profile in enumerate(profiles, start=1):
        if profile.id == current:
            combo.set_selected(index)
    top.add(combo)
    page.add(top)

    members = Adw.PreferencesGroup(
        title=_("Members that inherit the profile"),
        description=_("Members with their own explicit profile are unchecked by default."),
    )
    preselected = set(default_group_members_to_link(snapshot, member_ids))
    checks: Dict[str, Adw.ActionRow] = {}
    for cid in member_ids:
        row = Adw.ActionRow(title=cid)
        check = Gtk.CheckButton()
        check.set_active(cid in preselected)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        link = snapshot.link_for(cid)
        if link is not None and link.mode is LoginProfileLinkMode.EXPLICIT:
            explicit = snapshot.profile(link.profile_id)
            row.set_subtitle(
                _("Uses profile “{name}”").format(name=explicit.name if explicit else "?")
            )
        members.add(row)
        checks[cid] = (row, check)
    if member_ids:
        page.add(members)

    def _refresh_previews(*_args):
        index = combo.get_selected()
        if index == 0 or not member_ids:
            return
        profile = profiles[index - 1]

        def _show(previews):
            for preview in previews:
                entry = checks.get(preview.connection_id)
                if entry is None or not preview.changes:
                    continue
                link = snapshot.link_for(preview.connection_id)
                if link is not None and link.mode is LoginProfileLinkMode.EXPLICIT:
                    continue  # keeps its own profile; the note says which
                entry[0].set_subtitle(
                    "; ".join(
                        f"{c.label}: {c.before.replace(chr(10), ', ') or '—'} → "
                        f"{c.after.replace(chr(10), ', ') or '—'}"
                        for c in preview.changes
                    )
                )

        run_async(
            lambda: controller.preview(member_ids, LoginProfileLinkMode.EXPLICIT, profile.id),
            _show,
            lambda error: logger.debug("Group profile preview failed: %s", error),
        )

    combo.connect("notify::selected", _refresh_previews)
    _refresh_previews()

    def _apply(*_args):
        index = combo.get_selected()
        profile_id = profiles[index - 1].id if index > 0 else None
        link = [cid for cid, (_row, check) in checks.items() if check.get_active()]
        apply_button.set_sensitive(False)
        run_async(
            lambda: controller.set_group_profile(
                group_id, profile_id, link if profile_id else ()
            ),
            lambda _r: (dialog.close(), on_done() if on_done else None),
            lambda error: (apply_button.set_sensitive(True), _show_error(banner, error)),
        )

    apply_button.connect("clicked", _apply)
    dialog.present(parent)


# ---------------------------------------------------------------------------
# Delete with reassignment
# ---------------------------------------------------------------------------

def show_delete_dialog(
    parent: Gtk.Widget,
    controller: LoginProfileController,
    profile: LoginProfileSummary,
    group_names: Mapping[str, str],
    *,
    on_done: Optional[Callable[[object], None]] = None,
) -> None:
    snapshot = controller.snapshot
    items = affected_items(snapshot, profile.id, group_names) if snapshot else ()

    def _delete(targets):
        run_async(
            lambda: controller.delete(profile.id, targets),
            lambda result: on_done(result) if on_done else None,
            lambda error: show_error_alert(parent, _("Could not delete the profile"), error),
        )

    if not items:
        alert = Adw.AlertDialog(
            heading=_("Delete “{name}”?").format(name=profile.name),
            body=_("No connection or group uses this profile. Its stored "
                   "passwords are deleted too."),
        )
        alert.add_response("cancel", _("Cancel"))
        alert.add_response("delete", _("Delete"))
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_close_response("cancel")
        alert.connect(
            "response", lambda _d, response: _delete({}) if response == "delete" else None
        )
        alert.present(parent)
        return

    dialog, page, delete_button, banner = _dialog_frame(
        _("Delete “{name}”").format(name=profile.name), _("Delete")
    )
    delete_button.remove_css_class("suggested-action")
    delete_button.add_css_class("destructive-action")
    intro = Adw.PreferencesGroup(
        description=_("These connections and groups use the profile. Choose what "
                      "each should use instead. Detached connections keep their "
                      "current settings."),
    )
    page.add(intro)

    state = {"targets": []}  # list of (label, profile_id or None)
    all_row = Adw.ComboRow(title=_("Apply to all"))
    intro.add(all_row)
    new_row = Adw.ButtonRow(title=_("New Profile…")) if hasattr(Adw, "ButtonRow") else None
    rows_group = Adw.PreferencesGroup(title=_("Affected"))
    page.add(rows_group)
    rows: Dict[str, Adw.ComboRow] = {}
    for item in items:
        row = Adw.ComboRow(title=item.label)
        row.set_subtitle(_("Group") if item.is_group else _("Connection"))
        rows_group.add(row)
        rows[item.key] = row

    def _rebuild_targets(select_id: Optional[str] = None):
        current = controller.snapshot
        targets = [(_("Detach (keep current values)"), None)] + [
            (p.name, p.id) for p in (current.profiles if current else ())
            if p.id != profile.id
        ]
        state["targets"] = targets
        labels = [label for label, _pid in targets]
        all_row.set_model(_string_list(labels))
        for row in rows.values():
            previous = row.get_selected()
            row.set_model(_string_list(labels))
            row.set_selected(previous if previous < len(labels) else 0)
        if select_id is not None:
            for index, (_label, pid) in enumerate(targets):
                if pid == select_id:
                    all_row.set_selected(index)

    def _apply_all(*_args):
        for row in rows.values():
            row.set_selected(all_row.get_selected())

    all_row.connect("notify::selected", _apply_all)
    _rebuild_targets()

    def _new_profile(*_args):
        open_profile_editor(
            dialog,
            controller,
            on_saved=lambda summary: _rebuild_targets(select_id=summary.id),
        )

    if new_row is not None:
        new_row.connect("activated", _new_profile)
        intro.add(new_row)
    else:
        button = Gtk.Button(label=_("New Profile…"))
        button.connect("clicked", _new_profile)
        intro.set_header_suffix(button)

    def _confirm(*_args):
        targets = {
            key: state["targets"][row.get_selected()][1] for key, row in rows.items()
        }
        delete_button.set_sensitive(False)
        run_async(
            lambda: controller.delete(profile.id, targets),
            lambda result: (dialog.close(), on_done(result) if on_done else None),
            lambda error: (delete_button.set_sensitive(True), _show_error(banner, error)),
        )

    delete_button.connect("clicked", _confirm)
    dialog.present(parent)


# ---------------------------------------------------------------------------
# Management window
# ---------------------------------------------------------------------------

class LoginProfilesWindow(Adw.Window):
    """Lists login profiles with their usage; create, edit, delete."""

    def __init__(
        self,
        parent: Gtk.Window,
        controller: LoginProfileController,
        *,
        group_names: Callable[[], Mapping[str, str]] = dict,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._group_names = group_names
        self.set_title(_("Login Profiles"))
        self.set_default_size(560, 560)
        self.set_transient_for(parent)
        self.set_modal(False)
        try:
            from .shortcut_utils import install_esc_to_close

            install_esc_to_close(self)
        except Exception:
            logger.debug("Escape-to-close unavailable", exc_info=True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        new_button = Gtk.Button(icon_name="list-add-symbolic")
        new_button.set_tooltip_text(_("New Login Profile"))
        new_button.connect("clicked", self._on_new)
        header.pack_start(new_button)
        toolbar.add_top_bar(header)
        self.toast_overlay = Adw.ToastOverlay()
        self._stack = Gtk.Stack()
        self._empty = Adw.StatusPage(
            icon_name="avatar-default-symbolic",
            title=_("No Login Profiles"),
            description=_("A login profile bundles a username, keys and passwords. "
                          "Assign it to connections or groups to reuse it."),
        )
        create = Gtk.Button(label=_("New Login Profile"))
        create.add_css_class("pill")
        create.add_css_class("suggested-action")
        create.set_halign(Gtk.Align.CENTER)
        create.connect("clicked", self._on_new)
        self._empty.set_child(create)
        self._page = Adw.PreferencesPage()
        self._group = Adw.PreferencesGroup()
        self._page.add(self._group)
        self._rows: List[Gtk.Widget] = []
        self._stack.add_named(self._empty, "empty")
        self._stack.add_named(self._page, "list")
        self.toast_overlay.set_child(self._stack)
        toolbar.set_content(self.toast_overlay)
        self.set_content(toolbar)
        self.reload()

    def _toast(self, message: str) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(4)
        self.toast_overlay.add_toast(toast)

    def reload(self) -> None:
        run_async(
            self._controller.refresh,
            lambda snapshot: self._render(snapshot),
            lambda error: self._toast(error_text(error)),
        )

    def _render(self, snapshot) -> None:
        for row in self._rows:
            self._group.remove(row)
        self._rows = []
        if not snapshot.available:
            self._toast(_("Login profiles are unavailable: their file could not be read."))
        self._stack.set_visible_child_name("list" if snapshot.profiles else "empty")
        for profile in snapshot.profiles:
            row = Adw.ActionRow(title=profile.name)
            usage = "{} · {}".format(
                ngettext("{n} connection", "{n} connections", profile.connection_count).format(
                    n=profile.connection_count
                ),
                ngettext("{n} group", "{n} groups", profile.group_count).format(
                    n=profile.group_count
                ),
            )
            row.set_subtitle(f"{profile_summary_line(profile)}\n{usage}")
            row.set_subtitle_lines(2)
            edit = Gtk.Button(icon_name="document-edit-symbolic")
            edit.set_tooltip_text(_("Edit"))
            edit.add_css_class("flat")
            edit.set_valign(Gtk.Align.CENTER)
            edit.connect("clicked", lambda *_a, p=profile: self._on_edit(p))
            delete = Gtk.Button(icon_name="user-trash-symbolic")
            delete.set_tooltip_text(_("Delete"))
            delete.add_css_class("flat")
            delete.set_valign(Gtk.Align.CENTER)
            delete.connect("clicked", lambda *_a, p=profile: self._on_delete(p))
            row.add_suffix(edit)
            row.add_suffix(delete)
            row.set_activatable_widget(edit)
            self._group.add(row)
            self._rows.append(row)

    def _on_new(self, *_args) -> None:
        open_profile_editor(self, self._controller, on_saved=lambda _s: self.reload())

    def _on_edit(self, profile: LoginProfileSummary) -> None:
        open_profile_editor(
            self, self._controller, profile=profile, on_saved=lambda _s: self.reload()
        )

    def _on_delete(self, profile: LoginProfileSummary) -> None:
        def _done(result):
            self.reload()
            detached = getattr(result, "detached", ())
            if detached:
                self._toast(
                    ngettext(
                        "{count} connection was detached and kept its settings.",
                        "{count} connections were detached and kept their settings.",
                        len(detached),
                    ).format(count=len(detached))
                )

        show_delete_dialog(
            self, self._controller, profile, self._group_names(), on_done=_done
        )
