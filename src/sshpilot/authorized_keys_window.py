"""GTK/Adwaita editor window for a remote ``~/.ssh/authorized_keys`` file."""

from __future__ import annotations

import logging
import threading
from gettext import gettext as _
from typing import List

from gi.repository import Adw, GLib, Gtk

from sshpilot.api.errors import ErrorCode, SshPilotError
from .authorized_keys_parser import (
    AuthorizedKeyEntry,
    Item,
    compute_fingerprint,
    parse_file,
)
from .authorized_keys_service import DaemonAuthorizedKeysService
from .shortcut_utils import install_esc_to_close

logger = logging.getLogger(__name__)


# Restriction-related flag options the user can toggle from the dialog.
# Stored as (name, label) so we can render uniformly.
_RESTRICT_OPT_INS = (
    ("pty", _("Allow PTY")),
    ("agent-forwarding", _("Allow agent forwarding")),
    ("port-forwarding", _("Allow port forwarding")),
    ("user-rc", _("Run ~/.ssh/rc")),
    ("X11-forwarding", _("Allow X11 forwarding")),
)

_NO_OPT_OUTS = (
    ("no-pty", _("Disable PTY")),
    ("no-agent-forwarding", _("Disable agent forwarding")),
    ("no-port-forwarding", _("Disable port forwarding")),
    ("no-user-rc", _("Skip ~/.ssh/rc")),
    ("no-X11-forwarding", _("Disable X11 forwarding")),
)


def _short(s: str, n: int = 28) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _summary(entry: AuthorizedKeyEntry) -> str:
    chips: List[str] = []
    if entry.get_option("command"):
        chips.append(_("forced cmd"))
    src = entry.get_option("from")
    if isinstance(src, str) and src:
        chips.append(f"from={_short(src, 24)}")
    if entry.get_option("restrict") is True:
        chips.append("restrict")
    if entry.get_option("cert-authority") is True:
        chips.append("CA")
    if entry.get_option("expiry-time"):
        chips.append("expiry")
    if entry.get_options("permitopen"):
        chips.append("permitopen")
    if entry.unknown_options:
        chips.append(_("+unknown"))
    return " · ".join(chips) or _("no options")


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/authorized_keys_window.ui")
class AuthorizedKeysWindow(Adw.Window):
    """List + edit ``~/.ssh/authorized_keys`` over SFTP."""

    __gtype_name__ = "SshPilotAuthorizedKeysWindow"

    toast_overlay = Gtk.Template.Child()
    title_label = Gtk.Template.Child()
    add_button = Gtk.Template.Child()
    reload_button = Gtk.Template.Child()
    raw_button = Gtk.Template.Child()
    save_button = Gtk.Template.Child()
    status_label = Gtk.Template.Child()
    list_box = Gtk.Template.Child()

    def __init__(
        self,
        parent,
        *,
        client,
        service_id=None,
        connection=None,
        local: bool = False,
        key_manager=None,
        interaction_dialogs=None,
    ) -> None:
        super().__init__()
        self._parent = parent
        self._connection = connection
        self._key_manager = key_manager
        self._interaction_dialogs = interaction_dialogs
        self._items: List[Item] = []
        self._dirty = False
        self._loaded = False
        self._closing = False
        self._listing_keys = False
        self._reading_public = False
        self._open_raw_editors: List[Gtk.Window] = []
        self._service = DaemonAuthorizedKeysService(
            client, service_id=service_id, local=local
        )
        self._local = local
        self._ready = local
        if local:
            title = _("Authorized keys — this computer")
        else:
            user = getattr(connection, "username", "") or ""
            host = (
                getattr(connection, "hostname", None)
                or getattr(connection, "host", None)
                or getattr(connection, "nickname", "")
            )
            title = _("Authorized keys — {who}").format(
                who=f"{user}@{host}" if user else host
            )

        self.set_title(title)
        self.set_transient_for(parent)
        self.set_modal(False)
        self.set_default_size(720, 560)
        self._build_ui(title)
        GLib.idle_add(self._reload)
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self, title: str) -> None:
        # Static chrome lives in the template; here we bind the template-child
        # aliases (keeping the class's original ``self._x`` names), set the
        # dynamic title, wire the add-menu popover, and connect handlers.
        self._toast_overlay = self.toast_overlay
        self.title_label.set_label(title)

        self._add_button = self.add_button
        self._add_button.set_popover(
            Gtk.PopoverMenu.new_from_model(self._build_add_menu_model())
        )

        self._reload_button = self.reload_button
        self._reload_button.connect("clicked", lambda *_: self._reload())

        self._raw_button = self.raw_button
        self._raw_button.connect("clicked", self._on_raw_edit_clicked)

        self._save_button = self.save_button
        self._save_button.connect("clicked", self._on_save_clicked)

        self._status_label = self.status_label
        self._list_box = self.list_box

        # Add an action set on the window for Ctrl+S.
        self._install_shortcuts()

    def _build_add_menu_model(self):
        from gi.repository import Gio
        menu = Gio.Menu()
        menu.append(_("Add from local keys…"), "ak.add-local")
        menu.append(_("Paste public key…"), "ak.add-paste")

        group = Gio.SimpleActionGroup()
        action_local = Gio.SimpleAction.new("add-local", None)
        action_local.connect("activate", lambda *_: self._on_add_from_local())
        group.add_action(action_local)
        action_paste = Gio.SimpleAction.new("add-paste", None)
        action_paste.connect("activate", lambda *_: self._on_add_from_paste())
        group.add_action(action_paste)
        self.insert_action_group("ak", group)
        return menu

    def _install_shortcuts(self) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)
        save_shortcut = Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>s"),
            Gtk.CallbackAction.new(lambda *_: (self._on_save_clicked(None), True)[1]),
        )
        controller.add_shortcut(save_shortcut)
        self.add_controller(controller)
        install_esc_to_close(self)

    # ------------------------------------------------------------------
    # Toast / status
    # ------------------------------------------------------------------

    def _toast(self, message: str) -> bool:
        try:
            self._toast_overlay.add_toast(Adw.Toast.new(message))
        except Exception as exc:
            logger.debug("toast failed: %s", exc)
        return False

    def _set_status(self, text: str) -> None:
        self._status_label.set_text(text)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._save_button.set_sensitive(dirty)

    # ------------------------------------------------------------------
    # Load / save
    # ------------------------------------------------------------------

    def _reload(self) -> bool:
        if not self._local and not self._ready:
            return self._wait_for_service_ready()
        self._set_status(_("Loading…"))
        future = self._service.load()

        def _done(fut):
            try:
                items = fut.result()
            except Exception as exc:
                logger.error("Failed to load authorized_keys: %s", exc)
                GLib.idle_add(self._toast, _("Failed to load: {error}").format(error=exc))
                GLib.idle_add(self._set_status, _("Error loading authorized_keys"))
                return
            GLib.idle_add(self._apply_loaded, items)

        future.add_done_callback(_done)
        return False

    def _wait_for_service_ready(self) -> bool:
        self._set_status(_("Connecting…"))
        future = self._service.await_ready()

        def _done(fut):
            if self._closing:
                return
            try:
                fut.result()
            except Exception as exc:
                logger.error("Failed to connect authorized_keys editor: %s", exc)
                GLib.idle_add(
                    self._toast,
                    _("Failed to connect: {error}").format(error=exc),
                )
                GLib.idle_add(self._set_status, _("Connection failed"))
                return
            self._ready = True
            GLib.idle_add(self._reload)

        future.add_done_callback(_done)
        return False

    def _apply_loaded(self, items: List[Item]) -> bool:
        self._items = items
        self._loaded = True
        self._set_dirty(False)
        self._refresh_list()
        entries = [it for it in items if isinstance(it, AuthorizedKeyEntry)]
        self._set_status(
            _("Loaded {n} entries").format(n=len(entries))
            if entries
            else _("No keys yet — use “Add key” to install one.")
        )
        return False

    def _on_save_clicked(self, _btn) -> None:
        if not self._dirty:
            return
        # Recompute fingerprints / mark dirty entries before serializing.
        for it in self._items:
            if isinstance(it, AuthorizedKeyEntry) and it.dirty:
                it.fingerprint_sha256 = compute_fingerprint(it.keytype, it.key_b64)
        self._set_status(_("Saving…"))
        self._save_button.set_sensitive(False)
        future = self._service.save(self._items, make_backup=True)

        def _done(fut):
            try:
                fut.result()
            except Exception as exc:
                logger.error("Failed to save authorized_keys: %s", exc)
                GLib.idle_add(self._toast, _("Save failed: {error}").format(error=exc))
                GLib.idle_add(self._save_button.set_sensitive, True)
                GLib.idle_add(self._set_status, _("Save failed"))
                return
            GLib.idle_add(self._after_save)

        future.add_done_callback(_done)

    def _after_save(self) -> bool:
        self._toast(_("Saved"))
        self._set_dirty(False)
        # Refresh from disk so we display exactly what was written.
        self._reload()
        return False

    # ------------------------------------------------------------------
    # List rendering
    # ------------------------------------------------------------------

    def _refresh_list(self) -> None:
        child = self._list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt

        for idx, item in enumerate(self._items):
            if isinstance(item, AuthorizedKeyEntry):
                self._list_box.append(self._build_row(idx, item))

    def _build_row(self, _index: int, entry: AuthorizedKeyEntry) -> Gtk.Widget:
        row = Adw.ActionRow()
        title = entry.comment or _("(no comment)")
        row.set_title(GLib.markup_escape_text(title))
        fp = entry.fingerprint_sha256 or compute_fingerprint(entry.keytype, entry.key_b64)
        subtitle_parts = [entry.keytype, fp, _summary(entry)]
        row.set_subtitle(" · ".join(p for p in subtitle_parts if p))

        switch = Gtk.Switch()
        switch.set_valign(Gtk.Align.CENTER)
        switch.set_active(not entry.disabled)
        switch.connect("notify::active", self._on_enable_toggled, entry)
        row.add_prefix(switch)

        edit_btn = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        edit_btn.set_tooltip_text(_("Edit entry"))
        edit_btn.add_css_class("flat")
        edit_btn.set_valign(Gtk.Align.CENTER)
        edit_btn.connect("clicked", self._on_edit_clicked, entry)
        row.add_suffix(edit_btn)

        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.set_tooltip_text(_("Delete entry"))
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", self._on_delete_clicked, entry)
        row.add_suffix(del_btn)

        return row

    # ------------------------------------------------------------------
    # Row callbacks
    # ------------------------------------------------------------------

    def _on_enable_toggled(self, switch, _pspec, entry: AuthorizedKeyEntry) -> None:
        new_disabled = not switch.get_active()
        if new_disabled == entry.disabled:
            return
        entry.disabled = new_disabled
        entry.mark_dirty()
        self._set_dirty(True)

    def _on_delete_clicked(self, _btn, entry: AuthorizedKeyEntry) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Delete this key?"),
            body=entry.comment or entry.fingerprint_sha256 or _("This authorized_keys entry will be removed."),
        )
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("delete", _("Delete"))
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def _on_response(_d, resp):
            if resp == "delete":
                try:
                    self._items.remove(entry)
                except ValueError:
                    return
                self._set_dirty(True)
                self._refresh_list()

        dlg.connect("response", _on_response)
        dlg.present()

    def _on_edit_clicked(self, _btn, entry: AuthorizedKeyEntry) -> None:
        AuthorizedKeyEntryDialog(self, entry, on_saved=self._on_entry_edited).present()

    def _on_entry_edited(self, _entry: AuthorizedKeyEntry) -> None:
        self._set_dirty(True)
        self._refresh_list()

    # ------------------------------------------------------------------
    # Add key
    # ------------------------------------------------------------------

    def _on_add_from_local(self) -> None:
        """List daemon-discovered keys off the GTK thread, then let the user pick.

        The injected key manager is the only source; when no manager is
        available (daemon down) there is no local fallback and no scanning.
        """
        if self._closing:
            return
        if self._key_manager is None:
            self._toast(
                _("SSH keys require the background service. Start it and try again.")
            )
            return
        if self._listing_keys or self._reading_public:
            return
        self._listing_keys = True
        self._set_local_key_busy(True)
        km = self._key_manager
        try:
            thread = threading.Thread(
                target=self._list_keys_worker,
                args=(km,),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            logger.error("Could not start local key listing: %s", type(exc).__name__)
            self._listing_keys = False
            self._set_local_key_busy(False)
            self._toast(_("Could not start listing local keys."))

    def _list_keys_worker(self, km):
        """Daemon/client call off the GTK thread; result delivered via idle."""
        try:
            keys = km.discover_keys() or []
        except BaseException as exc:  # noqa: BLE001 - marshalled to the GTK thread
            GLib.idle_add(self._on_local_keys_loaded, None, exc)
            return
        GLib.idle_add(self._on_local_keys_loaded, keys, None)

    def _on_local_keys_loaded(self, keys, exc):
        # Reset the raw flag first, then bail out when closing: no widget or
        # toast work may run from a callback after teardown.
        self._listing_keys = False
        if self._closing:
            return
        self._set_local_key_busy(False)
        if exc is not None:
            logger.error("Failed to read local keys: %s", type(exc).__name__)
            self._toast(_("Failed to read local keys."))
            return
        keys = list(keys or [])
        if not keys:
            self._toast(_("No local SSH keys found"))
            return
        self._prompt_local_key_pick(keys)

    def _prompt_local_key_pick(self, keys):
        names = [k.name for k in keys]
        dlg = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Add public key"),
            body=_("Pick a local key whose public counterpart to install."),
        )
        dropdown = Gtk.DropDown.new_from_strings(names)
        dropdown.set_selected(0)
        dlg.set_extra_child(dropdown)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("add", _("Add"))
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("add")
        dlg.set_close_response("cancel")

        def _on_response(_d, resp):
            # A response after teardown must not start a daemon read.
            if self._closing:
                return
            if resp != "add":
                return
            idx = dropdown.get_selected()
            if idx < 0 or idx >= len(keys):
                return
            self._start_public_key_read(keys[idx])

        dlg.connect("response", _on_response)
        dlg.present()

    def _start_public_key_read(self, key) -> None:
        """Read a daemon-discovered key's public text off the GTK thread."""
        if self._closing or self._listing_keys or self._reading_public:
            return
        self._reading_public = True
        self._set_local_key_busy(True)
        km = self._key_manager
        try:
            thread = threading.Thread(
                target=self._read_public_worker,
                args=(km, key),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            logger.error("Could not start public-key read: %s", type(exc).__name__)
            self._reading_public = False
            self._set_local_key_busy(False)
            self._toast(_("Could not start reading the public key."))

    def _read_public_worker(self, km, key):
        """Daemon/client call off the GTK thread; result delivered via idle."""
        try:
            text = km.read_public_key(key)
        except SshPilotError as exc:
            GLib.idle_add(self._on_public_key_read_failed, exc.code)
            return
        except BaseException as exc:  # noqa: BLE001 - marshalled to the GTK thread
            logger.debug("public-key read failed: %s", type(exc).__name__)
            GLib.idle_add(self._on_public_key_read_failed, None)
            return
        GLib.idle_add(self._on_public_key_read_ok, text)

    def _on_public_key_read_ok(self, text):
        self._reading_public = False
        if self._closing:
            return
        self._set_local_key_busy(False)
        self._append_pubkey_text(text)

    def _on_public_key_read_failed(self, code):
        self._reading_public = False
        if self._closing:
            return
        self._set_local_key_busy(False)
        if code == ErrorCode.KEY_PUBLIC_UNAVAILABLE:
            self._toast(
                _("The public key for the selected key is not available. "
                  "Generate it with ssh-keygen and try again.")
            )
        else:
            self._toast(_("Could not read the public key."))

    def _set_local_key_busy(self, busy: bool) -> None:
        """Disable the add-menu button while a daemon key operation runs."""
        try:
            self._add_button.set_sensitive(not busy)
        except Exception:
            pass

    def _on_add_from_paste(self) -> None:
        dlg = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Paste public key"),
            body=_("Paste a single OpenSSH public key line."),
        )
        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_size_request(480, 100)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(text_view)
        scrolled.set_size_request(480, 100)
        dlg.set_extra_child(scrolled)

        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("add", _("Add"))
        dlg.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("add")
        dlg.set_close_response("cancel")

        def _on_response(_d, resp):
            if resp != "add":
                return
            buf = text_view.get_buffer()
            start, end = buf.get_bounds()
            text = buf.get_text(start, end, False).strip()
            if not text:
                return
            self._append_pubkey_text(text)

        dlg.connect("response", _on_response)
        dlg.present()

    def _append_pubkey_text(self, text: str) -> None:
        parsed = parse_file(text + "\n")
        added = 0
        for it in parsed:
            if isinstance(it, AuthorizedKeyEntry):
                it.mark_dirty()
                self._items.append(it)
                added += 1
        if added == 0:
            self._toast(_("Could not parse a public key from input"))
            return
        self._set_dirty(True)
        self._refresh_list()
        self._toast(_("Added {n} key").format(n=added))

    # ------------------------------------------------------------------
    # Raw edit fallback
    # ------------------------------------------------------------------

    def _on_raw_edit_clicked(self, _btn) -> None:
        try:
            from .text_editor import RemoteFileEditorWindow
            editor = RemoteFileEditorWindow(
                parent=self,
                file_path=("~/.ssh/authorized_keys" if self._service._local else ".ssh/authorized_keys"),
                file_name="authorized_keys",
                is_local=False,
                daemon_file_service=self._service,
            )
            self._open_raw_editors.append(editor)
            editor.present()
        except Exception as exc:
            logger.error("Failed to open daemon raw editor: %s", type(exc).__name__)
            self._toast(_("Raw editor unavailable"))

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def _on_close_request(self, _window) -> bool:
        if not self._dirty:
            self._teardown()
            return False

        dlg = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_("Unsaved changes"),
            body=_("Discard changes to authorized_keys?"),
        )
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("discard", _("Discard"))
        dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def _on_response(_d, resp):
            if resp == "discard":
                self._dirty = False
                self._teardown()
                self.destroy()

        dlg.connect("response", _on_response)
        dlg.present()
        return True  # block default close while we ask

    def _teardown(self) -> None:
        self._closing = True
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None
        close = getattr(self._service, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# Per-entry edit dialog
# ---------------------------------------------------------------------------


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/authorized_key_entry_dialog.ui")
class AuthorizedKeyEntryDialog(Adw.Window):
    """Modal dialog for editing one AuthorizedKeyEntry's options."""

    __gtype_name__ = "SshPilotAuthorizedKeyEntryDialog"

    cancel_button = Gtk.Template.Child()
    apply_button = Gtk.Template.Child()
    body = Gtk.Template.Child()

    def __init__(self, parent: AuthorizedKeysWindow, entry: AuthorizedKeyEntry, *, on_saved):
        super().__init__()
        self._entry = entry
        self._on_saved = on_saved
        self.set_transient_for(parent)

        self.cancel_button.connect("clicked", lambda *_: self.close())
        self.apply_button.connect("clicked", self._on_save_clicked)

        # Esc closes
        key_ctrl = Gtk.EventControllerKey()

        def _on_key(_c, keyval, _code, _state):
            from gi.repository import Gdk
            if keyval == Gdk.KEY_Escape:
                self.close()
                return True
            return False

        key_ctrl.connect("key-pressed", _on_key)
        self.add_controller(key_ctrl)

        # The data-driven form is appended to the template's body box.
        body = self.body

        # Identity / comment
        info = Adw.PreferencesGroup()
        info.set_title(_("Key"))
        info_row = Adw.ActionRow()
        info_row.set_title(entry.keytype)
        info_row.set_subtitle(entry.fingerprint_sha256 or compute_fingerprint(entry.keytype, entry.key_b64))
        info.add(info_row)
        self._comment_row = Adw.EntryRow()
        self._comment_row.set_title(_("Comment"))
        self._comment_row.set_text(entry.comment or "")
        info.add(self._comment_row)
        body.append(info)

        # Restrictions group
        restrict_group = Adw.PreferencesGroup()
        restrict_group.set_title(_("Restrictions"))
        restrict_group.set_description(
            _("If ‘restrict’ is on, all forwardings/PTY/user-rc are denied unless re-enabled here.")
        )
        self._restrict_switch = Adw.SwitchRow()
        self._restrict_switch.set_title(_("Apply ‘restrict’"))
        self._restrict_switch.set_active(entry.get_option("restrict") is True)
        self._restrict_switch.connect("notify::active", self._on_restrict_toggled)
        restrict_group.add(self._restrict_switch)

        self._cert_authority_switch = Adw.SwitchRow()
        self._cert_authority_switch.set_title(_("cert-authority"))
        self._cert_authority_switch.set_subtitle(_("Treat this entry as a CA that signs user certificates."))
        self._cert_authority_switch.set_active(entry.get_option("cert-authority") is True)
        restrict_group.add(self._cert_authority_switch)

        # opt-in switches (only meaningful when restrict is on)
        self._opt_in_switches = {}
        for name, label in _RESTRICT_OPT_INS:
            sw = Adw.SwitchRow()
            sw.set_title(label)
            sw.set_active(entry.get_option(name) is True)
            restrict_group.add(sw)
            self._opt_in_switches[name] = sw

        # opt-out switches (no-*) for non-restrict mode
        self._opt_out_switches = {}
        for name, label in _NO_OPT_OUTS:
            sw = Adw.SwitchRow()
            sw.set_title(label)
            active = entry.get_option(name) is True
            # Treat the case-variant duplicates uniformly
            if not active and name.lower() != name:
                active = entry.get_option(name.lower()) is True
            sw.set_active(active)
            restrict_group.add(sw)
            self._opt_out_switches[name] = sw

        body.append(restrict_group)

        # Source / identity
        src_group = Adw.PreferencesGroup()
        src_group.set_title(_("Source / identity"))
        self._from_row = Adw.EntryRow()
        self._from_row.set_title(_("from= (pattern list, e.g. 10.0.0.0/8,!10.0.0.5)"))
        v = entry.get_option("from")
        self._from_row.set_text(v if isinstance(v, str) else "")
        src_group.add(self._from_row)

        self._principals_row = Adw.EntryRow()
        self._principals_row.set_title(_("principals= (comma-separated)"))
        v = entry.get_option("principals")
        self._principals_row.set_text(v if isinstance(v, str) else "")
        src_group.add(self._principals_row)

        body.append(src_group)

        # Command / env / expiry
        cmd_group = Adw.PreferencesGroup()
        cmd_group.set_title(_("Command, environment, expiry"))

        cmd_label = Gtk.Label(label=_("Forced command (command=)"), xalign=0)
        cmd_label.set_margin_top(4)
        cmd_group.add(cmd_label)
        self._command_buf = Gtk.TextBuffer()
        v = entry.get_option("command")
        self._command_buf.set_text(v if isinstance(v, str) else "", -1)
        cmd_view = Gtk.TextView(buffer=self._command_buf)
        cmd_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        cmd_view.set_monospace(True)
        cmd_view.add_css_class("card")
        cmd_view.set_size_request(-1, 80)
        cmd_group.add(cmd_view)

        self._expiry_row = Adw.EntryRow()
        self._expiry_row.set_title(_("expiry-time (e.g. YYYYMMDDHHMM or 20260101)"))
        v = entry.get_option("expiry-time")
        self._expiry_row.set_text(v if isinstance(v, str) else "")
        cmd_group.add(self._expiry_row)

        env_label = Gtk.Label(
            label=_("environment= entries (NAME=value, one per line). Often ignored unless sshd's PermitUserEnvironment is on."),
            xalign=0,
            wrap=True,
        )
        env_label.set_margin_top(4)
        cmd_group.add(env_label)
        self._env_buf = Gtk.TextBuffer()
        existing_envs = [v for v in entry.get_options("environment") if isinstance(v, str)]
        self._env_buf.set_text("\n".join(existing_envs), -1)
        env_view = Gtk.TextView(buffer=self._env_buf)
        env_view.set_monospace(True)
        env_view.add_css_class("card")
        env_view.set_size_request(-1, 80)
        cmd_group.add(env_view)

        body.append(cmd_group)

        # Tunneling
        tun_group = Adw.PreferencesGroup()
        tun_group.set_title(_("Tunneling"))

        po_label = Gtk.Label(label=_("permitopen= entries (host:port, one per line)"), xalign=0)
        tun_group.add(po_label)
        self._permitopen_buf = Gtk.TextBuffer()
        self._permitopen_buf.set_text(
            "\n".join(v for v in entry.get_options("permitopen") if isinstance(v, str)), -1
        )
        po_view = Gtk.TextView(buffer=self._permitopen_buf)
        po_view.set_monospace(True)
        po_view.add_css_class("card")
        po_view.set_size_request(-1, 60)
        tun_group.add(po_view)

        pl_label = Gtk.Label(label=_("permitlisten= entries (port or host:port, one per line)"), xalign=0)
        tun_group.add(pl_label)
        self._permitlisten_buf = Gtk.TextBuffer()
        self._permitlisten_buf.set_text(
            "\n".join(v for v in entry.get_options("permitlisten") if isinstance(v, str)), -1
        )
        pl_view = Gtk.TextView(buffer=self._permitlisten_buf)
        pl_view.set_monospace(True)
        pl_view.add_css_class("card")
        pl_view.set_size_request(-1, 60)
        tun_group.add(pl_view)

        self._tunnel_row = Adw.EntryRow()
        self._tunnel_row.set_title(_("tunnel= (device number)"))
        v = entry.get_option("tunnel")
        self._tunnel_row.set_text(v if isinstance(v, str) else "")
        tun_group.add(self._tunnel_row)

        body.append(tun_group)

        # FIDO group, only when keytype indicates sk-*
        if entry.keytype.startswith("sk-"):
            fido_group = Adw.PreferencesGroup()
            fido_group.set_title(_("FIDO security key"))
            self._verify_required = Adw.SwitchRow()
            self._verify_required.set_title(_("verify-required"))
            self._verify_required.set_subtitle(_("Require PIN/biometric verification on the security key."))
            self._verify_required.set_active(entry.get_option("verify-required") is True)
            fido_group.add(self._verify_required)
            self._no_touch_required = Adw.SwitchRow()
            self._no_touch_required.set_title(_("no-touch-required"))
            self._no_touch_required.set_subtitle(_("Skip the touch step (less secure)."))
            self._no_touch_required.set_active(entry.get_option("no-touch-required") is True)
            fido_group.add(self._no_touch_required)
            body.append(fido_group)
        else:
            self._verify_required = None
            self._no_touch_required = None

        # Unknown options (preserved)
        if entry.unknown_options:
            unk_group = Adw.PreferencesGroup()
            unk_group.set_title(_("Preserved unknown options"))
            unk_group.set_description(_("These options are not modelled by this editor but will be re-emitted verbatim."))
            self._unknown_check_rows = []
            for token in entry.unknown_options:
                row = Adw.ActionRow()
                row.set_title(GLib.markup_escape_text(token))
                keep = Gtk.CheckButton()
                keep.set_active(True)
                keep.set_valign(Gtk.Align.CENTER)
                row.add_suffix(keep)
                unk_group.add(row)
                self._unknown_check_rows.append((token, keep))
            body.append(unk_group)
        else:
            self._unknown_check_rows = []

        self._on_restrict_toggled(self._restrict_switch, None)

    def _on_restrict_toggled(self, switch, _pspec) -> None:
        restrict_on = switch.get_active()
        for sw in self._opt_in_switches.values():
            sw.set_sensitive(restrict_on)
        for sw in self._opt_out_switches.values():
            sw.set_sensitive(not restrict_on)

    def _on_save_clicked(self, _btn) -> None:
        entry = self._entry

        new_comment = (self._comment_row.get_text() or "").strip()
        if new_comment != entry.comment:
            entry.comment = new_comment
            entry.mark_dirty()

        # Drop everything we manage, then re-apply from form state.
        managed = {
            "command",
            "expiry-time",
            "from",
            "principals",
            "tunnel",
            "restrict",
            "cert-authority",
            "verify-required",
            "no-touch-required",
            "environment",
            "permitopen",
            "permitlisten",
        }
        for name, _label in _RESTRICT_OPT_INS:
            managed.add(name)
        for name, _label in _NO_OPT_OUTS:
            managed.add(name)

        new_opts = [(n, v) for n, v in entry.options if n not in managed]

        # Emit BOTH groups according to their switch state, not just the
        # currently-visible one. The opt-out switches stay set to their
        # last value when greyed, and silently dropping them on save would
        # erase the user's no-* preferences across a restrict on/off toggle.
        restrict_on = self._restrict_switch.get_active()
        if restrict_on:
            new_opts.append(("restrict", True))
        for name, sw in self._opt_in_switches.items():
            if sw.get_active():
                new_opts.append((name, True))
        for name, sw in self._opt_out_switches.items():
            if sw.get_active():
                new_opts.append((name, True))

        if self._cert_authority_switch.get_active():
            new_opts.append(("cert-authority", True))

        src = (self._from_row.get_text() or "").strip()
        if src:
            new_opts.append(("from", src))
        principals = (self._principals_row.get_text() or "").strip()
        if principals:
            new_opts.append(("principals", principals))

        cmd_text = self._command_buf.get_text(
            self._command_buf.get_start_iter(), self._command_buf.get_end_iter(), False
        ).strip()
        if cmd_text:
            new_opts.append(("command", cmd_text))

        expiry = (self._expiry_row.get_text() or "").strip()
        if expiry:
            new_opts.append(("expiry-time", expiry))

        env_text = self._env_buf.get_text(
            self._env_buf.get_start_iter(), self._env_buf.get_end_iter(), False
        )
        for line in env_text.splitlines():
            v = line.strip()
            if v:
                new_opts.append(("environment", v))

        po_text = self._permitopen_buf.get_text(
            self._permitopen_buf.get_start_iter(), self._permitopen_buf.get_end_iter(), False
        )
        for line in po_text.splitlines():
            v = line.strip()
            if v:
                new_opts.append(("permitopen", v))

        pl_text = self._permitlisten_buf.get_text(
            self._permitlisten_buf.get_start_iter(), self._permitlisten_buf.get_end_iter(), False
        )
        for line in pl_text.splitlines():
            v = line.strip()
            if v:
                new_opts.append(("permitlisten", v))

        tun = (self._tunnel_row.get_text() or "").strip()
        if tun:
            new_opts.append(("tunnel", tun))

        if self._verify_required is not None and self._verify_required.get_active():
            new_opts.append(("verify-required", True))
        if self._no_touch_required is not None and self._no_touch_required.get_active():
            new_opts.append(("no-touch-required", True))

        # Unknown options that user did not uncheck.
        new_unknown = [tok for tok, chk in self._unknown_check_rows if chk.get_active()]

        if (
            new_opts != entry.options
            or new_unknown != entry.unknown_options
        ):
            entry.options = new_opts
            entry.unknown_options = new_unknown
            entry.mark_dirty()

        if self._on_saved is not None:
            self._on_saved(entry)
        self.close()

