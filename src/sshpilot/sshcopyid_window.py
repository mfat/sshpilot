import logging
import threading
from gettext import gettext as _

from gi.repository import Adw, GLib, Gtk


from sshpilot.api.errors import ErrorCode, SshPilotError
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
)
from .shortcut_utils import install_esc_to_close

logger = logging.getLogger(__name__)


@Gtk.Template(resource_path="/io/github/mfat/sshpilot/ui/sshcopyid_window.ui")
class SshCopyIdWindow(Adw.Window):
    """
    Full Adwaita-styled window for installing a public key on a server.
    - Server row with searchable inventory picker (``connection`` may be
      ``None``; OK stays disabled until a server is chosen)
    - Two modes:
        1) Use existing key (DropDown)
        2) Generate new key (embedded key-generator form)
    - Pressing OK triggers:
        - Either copy selected existing key
        - Or generate a new key, then copy it
    - Uses your existing terminal flow:
        parent._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)
    """

    __gtype_name__ = "SshPilotSshCopyIdWindow"

    cancel_button = Gtk.Template.Child()
    btn_ok = Gtk.Template.Child()
    content_box = Gtk.Template.Child()

    #: Trailing dropdown entry that opens the file chooser instead of selecting a key.
    _BROWSE_LABEL = _("Browse for a key file…")

    def __init__(self, parent, connection, key_manager, connection_manager):
        logger.info("SshCopyIdWindow: Initializing window")
        logger.debug(f"SshCopyIdWindow: Constructor called with connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"SshCopyIdWindow: Connection object type: {type(connection)}")
        logger.debug(f"SshCopyIdWindow: Key manager type: {type(key_manager)}")
        logger.debug(f"SshCopyIdWindow: Connection manager type: {type(connection_manager)}")

        super().__init__()
        self.set_transient_for(parent)
        install_esc_to_close(self)

        self._parent = parent
        self._conn = connection
        self._km = key_manager
        self._cm = connection_manager

        # Async-operation guards: no background work may start new work after
        # the window closes, and repeated clicks are ignored while running.
        self._closed = False
        self._loading_keys = False
        self._generating = False
        try:
            self.connect("close-request", self._on_close_request)
        except Exception:
            logger.debug("SshCopyIdWindow: close-request hook unavailable", exc_info=True)

        # Static chrome (header Cancel/OK, keychain illustration) lives in the
        # template; the server picker, key mode, and generator form are built
        # below and appended to the template's content box.
        self.cancel_button.connect("clicked", self._on_close_clicked)
        self.btn_ok.connect("clicked", self._on_ok_clicked)
        content = self.content_box

        # ---------- Server picker ----------
        try:
            server_group = Adw.PreferencesGroup(title="")
            self._server_row = Adw.ActionRow(title=_("Server"))
            self._server_row.set_activatable(True)
            self._server_label = Gtk.Label()
            self._server_label.add_css_class('dim-label')
            self._server_label.set_valign(Gtk.Align.CENTER)
            self._server_row.add_suffix(self._server_label)
            pick_btn = Gtk.Button()
            pick_btn.set_icon_name('pan-down-symbolic')
            pick_btn.set_tooltip_text(_("Pick from inventory"))
            pick_btn.add_css_class('flat')
            pick_btn.set_valign(Gtk.Align.CENTER)
            self._server_row.add_suffix(pick_btn)
            server_group.add(self._server_row)
            content.append(server_group)

            def _open_server_picker(*_a):
                from .host_picker import show_host_picker
                popover = show_host_picker(
                    self, self._server_row, self._set_server,
                    connections=self._cm.get_connections(),
                )
                # Anchored to the row: stretch to its full width.
                if popover is not None:
                    width = self._server_row.get_width()
                    if width > 0:
                        popover.set_size_request(width, -1)

            pick_btn.connect('clicked', _open_server_picker)
            self._server_row.connect('activated', lambda _r: _open_server_picker())
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create server picker: {e}")
            raise

        # ---------- Options group ----------
        logger.info("SshCopyIdWindow: Creating options group")
        try:
            group = Adw.PreferencesGroup(title="")

            # Radio option 1: Use existing key (using CheckButton with group for radio behavior)
            self.radio_existing = Gtk.CheckButton(label=_("Copy existing key"))
            self.radio_existing.set_can_focus(True)  # Make it focusable for tab navigation
            self.radio_generate = Gtk.CheckButton(label=_("Generate new key"))
            self.radio_generate.set_can_focus(True)  # Make it focusable for tab navigation

            # Make them behave like radio buttons (GTK4)
            self.radio_generate.set_group(self.radio_existing)
            self.radio_existing.set_active(True)
            logger.info("SshCopyIdWindow: Radio buttons created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create radio buttons: {e}")
            raise

        # Existing key row with dropdown
        logger.info("SshCopyIdWindow: Creating existing key dropdown")
        try:
            existing_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            existing_box.set_margin_start(12)
            existing_box.set_margin_bottom(6)
            # Selection state guards for the dropdown (which carries a final
            # "Browse…" item that opens a portal-aware file chooser).
            self._programmatic_dd_change = False
            self._last_real_selection = 0
            self._key_chooser_native = None
            self.dropdown_existing = Gtk.DropDown()
            self.dropdown_existing.set_can_focus(True)  # Make it focusable for tab navigation
            self.dropdown_existing.connect("notify::selected", self._on_dropdown_selected)
            existing_box.append(Gtk.Label(label=_("Select key:"), xalign=0))
            existing_box.append(self.dropdown_existing)

            # Fill dropdown with discovered keys (plus the trailing Browse item)
            self._reload_existing_keys()
            logger.info("SshCopyIdWindow: Existing key dropdown created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create existing key dropdown: {e}")
            raise

        # Generate form (embedded)
        logger.info("SshCopyIdWindow: Creating key generation form")
        try:
            self.generate_revealer = Gtk.Revealer()
            self.generate_revealer.set_reveal_child(False)
            self.generate_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)

            gen_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            gen_box.set_margin_start(12)
            gen_box.set_margin_top(6)
            gen_box.set_can_focus(True)  # Make the box focusable for tab navigation

            # Key name
            self.row_key_name = Adw.EntryRow()
            self.row_key_name.set_title(_("Key file name"))
            self.row_key_name.set_text("id_ed25519")
            self.row_key_name.set_can_focus(True)  # Make it focusable for tab navigation
            gen_box.append(self.row_key_name)

            # Key type
            key_type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            key_type_label = Gtk.Label(label=_("Key type:"), xalign=0)
            self.type_dropdown = Gtk.DropDown()
            self._types_model = Gtk.StringList.new(["ed25519", "rsa"])
            self.type_dropdown.set_model(self._types_model)
            self.type_dropdown.set_selected(0)
            self.type_dropdown.set_can_focus(True)  # Make it focusable for tab navigation
            key_type_box.append(key_type_label)
            key_type_box.append(self.type_dropdown)
            gen_box.append(key_type_box)

            self.generate_revealer.set_child(gen_box)
            logger.info("SshCopyIdWindow: Key generation form created successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create key generation form: {e}")
            raise

        # Pack into PreferencesGroup
        logger.info("SshCopyIdWindow: Packing UI elements")
        try:
            # Row 1: Existing
            existing_row = Adw.ActionRow()
            existing_row.add_prefix(self.radio_existing)
            existing_row.add_suffix(existing_box)
            group.add(existing_row)

            # Row 2: Generate
            generate_row = Adw.ActionRow()
            generate_row.add_prefix(self.radio_generate)
            group.add(generate_row)
            # Embedded generator UI under row 2
            group.add(self.generate_revealer)

            content.append(group)

            # ---------- Passphrase toggle ----------
            logger.info("SshCopyIdWindow: Creating passphrase toggle")
            try:
                passphrase_group = Adw.PreferencesGroup(title="")
                
                self.row_pass_toggle = Adw.SwitchRow()
                self.row_pass_toggle.set_title(_("Encrypt with passphrase"))
                self.row_pass_toggle.set_activatable(True)  # Make the entire row clickable
                
                passphrase_group.add(self.row_pass_toggle)
                
                # Native ssh-keygen prompts through the daemon's protected
                # interaction dialog after generation starts. No secret is
                # collected into the ordinary generation request.
                self.pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                self.pass_box.set_visible(False)
                
                content.append(passphrase_group)
                logger.info("SshCopyIdWindow: Passphrase toggle created successfully")
            except Exception as e:
                logger.error(f"SshCopyIdWindow: Failed to create passphrase toggle: {e}")
                raise

            # ---------- Force key transfer toggle ----------
            logger.info("SshCopyIdWindow: Creating force key transfer toggle")
            try:
                force_group = Adw.PreferencesGroup(title="")
                
                self.force_toggle = Adw.SwitchRow()
                self.force_toggle.set_title(_("Force key transfer"))
                self.force_toggle.set_subtitle(_("Overwrite existing keys on the server"))
                self.force_toggle.set_active(True)  # Default to enabled
                force_group.add(self.force_toggle)
                
                content.append(force_group)
                logger.info("SshCopyIdWindow: Force key transfer toggle created successfully")
            except Exception as e:
                logger.error(f"SshCopyIdWindow: Failed to create force toggle: {e}")
                raise

            # Radio change behavior
            self.radio_existing.connect("toggled", self._on_mode_toggled)
            self.radio_generate.connect("toggled", self._on_mode_toggled)
            
            # Set initial state (since "Copy existing key" is selected by default)
            self.row_pass_toggle.set_sensitive(False)
            self.pass_box.set_sensitive(False)
            
            # Key type change behavior
            self.type_dropdown.connect("notify::selected", self._on_key_type_changed)
            
            # Passphrase toggle behavior
            self.row_pass_toggle.connect("notify::active", self._on_pass_toggle)

            logger.info("SshCopyIdWindow: UI elements packed successfully")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to pack UI elements: {e}")
            raise

        self._set_server(connection)

        logger.info("SshCopyIdWindow: Window construction completed, presenting")
        self.present()

    # ---------- Helpers ----------

    def _set_server(self, conn):
        """Update the target connection and the server row/OK sensitivity."""
        self._conn = conn
        if conn is None:
            self._server_label.set_label('')
            self._server_label.set_visible(False)
            self._server_row.set_subtitle(_("Choose a server…"))
        else:
            nick = getattr(conn, 'nickname', '') or _get_connection_alias(conn) or ''
            host = _get_connection_host(conn) or _get_connection_alias(conn) or ''
            user = getattr(conn, 'username', '')
            self._server_label.set_label(nick)
            self._server_label.set_visible(bool(nick))
            self._server_row.set_subtitle(f"{user}@{host}" if user and host else host)
        # Central sensitivity: an in-flight key load/generate keeps OK off even
        # when a server is chosen (fixes the constructor ordering bug where the
        # initial load disabled OK and _set_server re-enabled it).
        self._update_ok_sensitivity()

    def _on_mode_toggled(self, *_):
        # Reveal generator only when "Generate new key" is selected
        generate_active = self.radio_generate.get_active()
        logger.info(f"SshCopyIdWindow: Mode toggled, generate active: {generate_active}")
        self.generate_revealer.set_reveal_child(generate_active)
        
        # Enable/disable passphrase section based on mode
        self.row_pass_toggle.set_sensitive(generate_active)
        self.pass_box.set_sensitive(generate_active)
        
        # If switching to "Copy existing key", turn off passphrase and hide fields
        if not generate_active:
            self.row_pass_toggle.set_active(False)
            self.pass_box.set_visible(False)
        self._update_ok_sensitivity()
    
    def _on_key_type_changed(self, *_):
        # Update key name placeholder when key type changes
        type_selection = self.type_dropdown.get_selected()
        if type_selection == 1:  # RSA
            self.row_key_name.set_text("id_rsa")
        else:  # ed25519
            self.row_key_name.set_text("id_ed25519")
    

    
    def _on_pass_toggle(self, *_):
        # Show/hide passphrase entries when toggle changes
        self.pass_box.set_visible(self.row_pass_toggle.get_active())

    def _reload_existing_keys(self):
        """Start a background key listing; never block the GTK thread."""
        if self._loading_keys or self._closed:
            return
        self._loading_keys = True
        self._set_key_loading_state(True)
        km = self._km
        try:
            thread = threading.Thread(
                target=self._load_keys_worker,
                args=(km,),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            logger.error("SshCopyIdWindow: Could not start key loading: %s", type(exc).__name__)
            self._loading_keys = False
            self._set_key_loading_state(False)

    def _load_keys_worker(self, km):
        """Daemon/client call off the GTK thread; result delivered via idle."""
        try:
            keys = km.discover_keys() or []
        except BaseException as exc:  # noqa: BLE001 - marshalled to the GTK thread
            GLib.idle_add(self._on_keys_load_failed, exc)
            return
        GLib.idle_add(self._on_keys_loaded, keys)

    def _on_keys_loaded(self, keys):
        # Reset the raw flag first, then bail out when closed: never touch
        # widgets or start new work from a callback after the window is gone.
        # The cache is committed before controls are re-enabled so OK
        # sensitivity reflects the freshly loaded key list.
        self._loading_keys = False
        if self._closed:
            return
        logger.info("SshCopyIdWindow: Discovered %d keys", len(keys))
        self._existing_keys_cache = list(keys)
        self._last_real_selection = 0
        names = [k.name for k in keys] or [_("No keys found")]
        self._rebuild_existing_dropdown(names, 0)
        self._set_key_loading_state(False)

    def _on_keys_load_failed(self, exc):
        self._loading_keys = False
        if self._closed:
            return
        logger.error("SshCopyIdWindow: Failed to load existing keys: %s", type(exc).__name__)
        self._existing_keys_cache = []
        self._last_real_selection = 0
        self._rebuild_existing_dropdown([_("Error loading keys")], 0)
        self._set_key_loading_state(False)
        self._error(_("Key Loading Failed"), _("Could not load the local keys."))

    def _set_key_loading_state(self, loading):
        """Disable/re-enable the existing-key controls while listing keys."""
        try:
            self.dropdown_existing.set_sensitive(not loading)
            self._update_ok_sensitivity()
        except Exception:
            pass

    def _update_ok_sensitivity(self):
        """Single source of truth for OK-button sensitivity.

        OK is enabled only when a server is selected, no key operation is
        running, and the active mode has a usable selection: existing-key mode
        needs at least one real key in the cache; generation mode needs the
        form (its validity is checked on click).
        """
        try:
            if self._conn is None or self._loading_keys or self._generating:
                self.btn_ok.set_sensitive(False)
                return
            if self.radio_existing.get_active():
                has_real = bool(getattr(self, "_existing_keys_cache", None))
                self.btn_ok.set_sensitive(has_real)
            else:
                self.btn_ok.set_sensitive(True)
        except Exception:
            pass

    def _rebuild_existing_dropdown(self, names, select_index):
        """Set the dropdown model to *names* plus a trailing Browse item.

        Wrapped in a suppression flag so the programmatic model/selection change
        doesn't re-enter the ``notify::selected`` handler (which would otherwise
        treat it as a user picking the Browse item).
        """
        self._programmatic_dd_change = True
        try:
            items = list(names) + [self._BROWSE_LABEL]
            self.dropdown_existing.set_model(Gtk.StringList.new(items))
            self.dropdown_existing.set_selected(select_index)
        finally:
            self._programmatic_dd_change = False

    def _on_dropdown_selected(self, *_):
        """Open the file chooser when the trailing Browse item is picked."""
        if self._programmatic_dd_change:
            return
        model = self.dropdown_existing.get_model()
        if model is None:
            return
        idx = self.dropdown_existing.get_selected()
        sentinel = model.get_n_items() - 1  # the Browse item is always last
        if idx == sentinel:
            self._open_key_file_chooser()
        else:
            self._last_real_selection = idx

    def _open_key_file_chooser(self):
        """Portal-aware native file chooser for selecting a public key."""
        if self._closed:
            return
        try:
            dlg = Gtk.FileChooserNative(
                title=_("Select Public Key"),
                action=Gtk.FileChooserAction.OPEN,
                transient_for=self,
                modal=True,
            )
            try:
                pub_filter = Gtk.FileFilter()
                pub_filter.set_name(_("SSH Public Keys"))
                pub_filter.add_pattern("*.pub")
                all_filter = Gtk.FileFilter()
                all_filter.set_name(_("All Files"))
                all_filter.add_pattern("*")
                dlg.add_filter(pub_filter)
                dlg.add_filter(all_filter)
            except Exception:
                pass
            # Keep a reference so the native dialog isn't garbage-collected.
            self._key_chooser_native = dlg
            dlg.connect("response", self._on_key_file_response)
            dlg.show()
        except Exception:
            logger.warning("Failed to open public key file chooser", exc_info=True)
            self._revert_dropdown_selection()

    def _on_key_file_response(self, dlg, response):
        try:
            # A response after closure must not update the key cache.
            if self._closed:
                return
            path = None
            if response == Gtk.ResponseType.ACCEPT:
                gfile = dlg.get_file()
                path = gfile.get_path() if gfile else None
            if path:
                self._add_browsed_public_key(path)
            else:
                self._revert_dropdown_selection()
        finally:
            dlg.destroy()
            self._key_chooser_native = None

    def _revert_dropdown_selection(self):
        """Restore the last real selection after a cancelled/failed browse."""
        self._programmatic_dd_change = True
        try:
            self.dropdown_existing.set_selected(self._last_real_selection)
        finally:
            self._programmatic_dd_change = False

    def _add_browsed_public_key(self, path):
        """Add a browsed public key to the dropdown and select it.

        The cached key list is the source of truth; the model is rebuilt from it
        (plus the Browse item) so selection indices always line up, even when the
        list previously held only a placeholder ("No keys found").
        """
        if self._closed:
            return
        cache = list(getattr(self, "_existing_keys_cache", None) or [])

        # De-dupe: if this exact public key is already listed, just select it.
        for i, key in enumerate(cache):
            if getattr(key, "public_path", None) == path:
                self._last_real_selection = i
                self._rebuild_existing_dropdown([k.name for k in cache], i)
                self.radio_existing.set_active(True)
                self._update_ok_sensitivity()
                return

        self._error(
            _("Public Key Selection"),
            _("Choose a key discovered by the background service."),
        )
        self._revert_dropdown_selection()

    def _info(self, title, body):
        try:
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=body)
            md.add_response("ok", _("OK"))
            md.set_default_response("ok")
            md.set_close_response("ok")
            md.present()
        except Exception:
            pass

    def _error(self, title, body, detail=""):
        try:
            text = body + (f"\n\n{detail}" if detail else "")
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=text)
            md.add_response("close", _("Close"))
            md.set_default_response("close")
            md.set_close_response("close")
            md.present()
        except Exception:
            logger.error("%s: %s | %s", title, body, detail)

    def _on_close_request(self, *_args):
        self._closed = True
        dialogs = getattr(self, "_key_generation_interactions", None)
        if dialogs is not None:
            dialogs.close()
            self._key_generation_interactions = None
        return False

    def _on_close_clicked(self, *_):
        logger.info("SshCopyIdWindow: Close button clicked")
        self._closed = True
        self.close()

    # ---------- OK (main action) ----------
    def _on_ok_clicked(self, *_):
        logger.info("SshCopyIdWindow: OK button clicked")
        # Nothing to do after close, while a listing runs, or while a
        # generation is already in flight (OK is also disabled then, but a
        # re-entrant activation must never start a second worker).
        if self._closed or self._loading_keys or self._generating:
            return
        logger.debug("SshCopyIdWindow: Starting main action processing")
        
        # Log current UI state
        existing_active = self.radio_existing.get_active()
        generate_active = self.radio_generate.get_active()
        logger.debug(f"SshCopyIdWindow: UI state - existing_active={existing_active}, generate_active={generate_active}")
        
        try:
            if self.radio_existing.get_active():
                logger.info("SshCopyIdWindow: Copying existing key")
                logger.debug("SshCopyIdWindow: Calling _do_copy_existing()")
                self._do_copy_existing()
            else:
                logger.info("SshCopyIdWindow: Generating new key and copying")
                logger.debug("SshCopyIdWindow: Calling _do_generate_and_copy()")
                self._do_generate_and_copy()
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Operation failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")
            self._error("Operation failed", "Could not start the requested action.", str(e))

    # ---------- Mode: existing ----------
    def _do_copy_existing(self):
        logger.info("SshCopyIdWindow: Starting copy existing key operation")
        logger.debug("SshCopyIdWindow: Processing existing key selection")
        
        try:
            keys = getattr(self, "_existing_keys_cache", []) or []
            logger.info(f"SshCopyIdWindow: Found {len(keys)} cached keys")
            logger.debug(f"SshCopyIdWindow: Cached keys list length: {len(keys)}")
            
            if not keys:
                logger.debug("SshCopyIdWindow: No cached keys available")
                raise RuntimeError("No keys available in ~/.ssh")
            
            idx = self.dropdown_existing.get_selected()
            logger.info(f"SshCopyIdWindow: Selected key index: {idx}")
            logger.debug(f"SshCopyIdWindow: Dropdown selection index: {idx}")
            
            if idx < 0 or idx >= len(keys):
                logger.debug(f"SshCopyIdWindow: Invalid index {idx} for keys list of length {len(keys)}")
                raise RuntimeError("Please select a key to copy")
            
            ssh_key = keys[idx]
            logger.info("SshCopyIdWindow: Selected key: %s", ssh_key.name)
            
            # Launch your existing terminal ssh-copy-id flow
            logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
            force_enabled = self.force_toggle.get_active()
            logger.debug(f"SshCopyIdWindow: Force option enabled: {force_enabled}")
            self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, ssh_key, force_enabled)
            logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
            self.close()
        except Exception as e:
            logger.error("SshCopyIdWindow: Copy existing failed: %s", type(e).__name__)
            self._error("Copy failed", "Could not copy the selected key to the server.", str(e))

    # ---------- Mode: generate ----------
    def _do_generate_and_copy(self):
        """Validate the form, then run generation off the GTK thread.

        A second click while generation is running is ignored, and nothing
        starts after the window closes (defense in depth: ``_on_ok_clicked``
        also guards closed/loading/generating). Passphrases are collected later
        by the daemon interaction dialog and never enter this worker request.
        """
        if self._closed or self._generating:
            return
        try:
            key_name = (self.row_key_name.get_text() or "").strip()
            if not key_name:
                raise ValueError(_("Enter a key file name (e.g. id_ed25519)"))
            if "/" in key_name or key_name.startswith("."):
                raise ValueError(_("Key file name must not contain '/' or start with '.'"))

            type_selection = self.type_dropdown.get_selected()
            kt = "ed25519" if type_selection == 0 else "rsa"

            encrypted = bool(self.row_pass_toggle.get_active())
        except ValueError as exc:
            self._error(_("Key Generation Failed"), str(exc))
            return

        logger.info("SshCopyIdWindow: Starting generate and copy operation")
        self._generating = True
        self._set_generating_state(True)
        interaction_scope_id = None
        if encrypted:
            from .api.models import SessionId
            from .daemon_interaction_dialogs import DaemonInteractionDialogs
            from .runtime_identity import new_operation_id

            interaction_scope_id = SessionId(
                f"key-{new_operation_id()}"
            )
            dialogs = DaemonInteractionDialogs(
                self._parent.client,
                self._parent.client_bridge,
                self,
            )
            dialogs.set_session(interaction_scope_id)
            self._key_generation_interactions = dialogs

        request = {
            "key_name": key_name,
            "key_type": kt,
            "key_size": 3072 if kt == "rsa" else 0,
            "comment": None,
            "encrypted": encrypted,
            "interaction_scope_id": interaction_scope_id,
        }
        try:
            thread = threading.Thread(
                target=self._generate_worker,
                args=(self._km, self._parent, self._conn, request),
                daemon=True,
            )
            thread.start()
        except Exception as exc:
            logger.error(
                "SshCopyIdWindow: Could not start generation: %s", type(exc).__name__
            )
            self._generating = False
            dialogs = getattr(self, "_key_generation_interactions", None)
            if dialogs is not None:
                dialogs.close()
                self._key_generation_interactions = None
            self._set_generating_state(False)
            self._error(
                _("Key Generation Failed"),
                _("Could not start key generation."),
            )

    def _generate_worker(self, km, parent, conn, request):
        """Daemon/client call off the GTK thread; result delivered via idle."""
        try:
            new_key = km.generate_key(**request)
        except SshPilotError as exc:
            if exc.code == ErrorCode.MUTATION_AMBIGUOUS:
                GLib.idle_add(self._on_key_mutation_ambiguous, exc)
            else:
                GLib.idle_add(self._on_key_generation_failed, exc)
            return
        except BaseException as exc:  # noqa: BLE001 - marshalled to the GTK thread
            GLib.idle_add(self._on_key_generation_failed, exc)
            return
        if new_key is None:
            GLib.idle_add(
                self._on_key_generation_failed,
                RuntimeError("Key generation returned no result"),
            )
            return
        GLib.idle_add(self._on_key_generated, new_key, conn)

    def _on_key_generated(self, new_key, conn):
        self._generating = False
        dialogs = getattr(self, "_key_generation_interactions", None)
        if dialogs is not None:
            dialogs.close()
            self._key_generation_interactions = None
        if self._closed:
            return
        self._set_generating_state(False)
        logger.info("SshCopyIdWindow: Key generated successfully")
        # The daemon result is authoritative; no local file checks or delays.
        try:
            force_enabled = self.force_toggle.get_active()
            self._parent._show_ssh_copy_id_terminal_using_main_widget(
                conn, new_key, force_enabled
            )
        except Exception as exc:
            logger.error(
                "SshCopyIdWindow: Could not start ssh-copy-id: %s", type(exc).__name__
            )
            self._error(
                _("Key Generation Failed"),
                _("The key was generated, but copying it to the server could not start."),
            )
            return
        logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
        self.close()

    def _on_key_mutation_ambiguous(self, exc):
        self._generating = False
        dialogs = getattr(self, "_key_generation_interactions", None)
        if dialogs is not None:
            dialogs.close()
            self._key_generation_interactions = None
        if self._closed:
            return
        self._set_generating_state(False)
        logger.warning(
            "SshCopyIdWindow: Key generation outcome unknown: %s", type(exc).__name__
        )
        self._info(
            _("Key May Have Been Generated"),
            _(
                "The connection was lost while generating the key. The key may "
                "have been created. Check the key list; generation was not retried."
            ),
        )
        # Reload once; do not automatically resubmit the mutation.
        self._reload_existing_keys()

    def _on_key_generation_failed(self, exc):
        self._generating = False
        dialogs = getattr(self, "_key_generation_interactions", None)
        if dialogs is not None:
            dialogs.close()
            self._key_generation_interactions = None
        if self._closed:
            return
        self._set_generating_state(False)
        logger.error(
            "SshCopyIdWindow: Generate failed: %s", type(exc).__name__
        )
        if isinstance(exc, FileExistsError):
            message = _("A key with that name already exists.")
        elif isinstance(exc, ValueError):
            message = str(exc) or _("The key name is invalid.")
        else:
            message = _("Could not generate the SSH key.")
        self._error(_("Key Generation Failed"), message)

    def _set_generating_state(self, generating):
        """Disable/re-enable the form while a generation is running."""
        try:
            self.row_key_name.set_sensitive(not generating)
            self.type_dropdown.set_sensitive(not generating)
            self.row_pass_toggle.set_sensitive(
                not generating and self.radio_generate.get_active()
            )
            self.pass_box.set_sensitive(
                not generating and self.radio_generate.get_active()
            )
            self.force_toggle.set_sensitive(not generating)
            # Mode switches and server re-selection are frozen mid-generation.
            self.radio_existing.set_sensitive(not generating)
            self.radio_generate.set_sensitive(not generating)
            self._server_row.set_sensitive(not generating)
            self._update_ok_sensitivity()
        except Exception:
            pass



class SshCopyIdRunner:
    """GTK adapter for the daemon-owned public-key deployment operation."""

    def __init__(self, window):
        self.window = window
        self._operation_id = None
        self._poll_id = None
        self._interaction_dialogs = None

    def run(self, connection, ssh_key, force=False):
        client = getattr(self.window, "client", None)
        key_id = getattr(ssh_key, "key_id", None)
        if client is None or not key_id:
            self.window._error_dialog(
                _("SSH Key Copy Error"),
                _("The background service is required to deploy a key."),
                _("Reconnect to the sshPilot daemon and try again."),
            )
            return
        # Attach the interaction presenter before starting the operation so a
        # password/passphrase/host-key/FIDO prompt during deployment is never
        # missed. It starts unbound (ignoring every interaction); once the
        # daemon returns the OperationSummary it binds to the operation ID —
        # the one public scope the deployment worker scopes its interactions
        # to — and set_session() reconciles any prompt already created.
        dialogs = None
        bridge = getattr(self.window, "client_bridge", None)
        if bridge is not None:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs
            dialogs = DaemonInteractionDialogs(client, bridge, self.window)
        try:
            from .api.connection_identity import connection_id_for
            from .api.models.identity import DeployKeyRequest
            from .api.models.keys import KeyStoreScope
            summary = client.deploy_key(
                DeployKeyRequest(
                    connection_id=connection_id_for(connection),
                    key_id=key_id,
                    scope=KeyStoreScope.DEFAULT,
                    force=bool(force),
                )
            )
        except SshPilotError as exc:
            if dialogs is not None:
                dialogs.close()
            self.window._error_dialog(
                _("SSH Key Copy Error"),
                _("Could not start public-key deployment."),
                _(str(exc.message)),
            )
            return
        except Exception:
            if dialogs is not None:
                dialogs.close()
            logger.error("Could not start daemon key deployment", exc_info=True)
            self.window._error_dialog(
                _("SSH Key Copy Error"),
                _("Could not start public-key deployment."),
                _("Reconnect to the sshPilot daemon and try again."),
            )
            return
        if dialogs is not None:
            from .api.models.common import SessionId
            dialogs.set_session(SessionId(str(summary.operation_id)))
        self._interaction_dialogs = dialogs
        self._operation_id = summary.operation_id
        self._poll_operation()

    def _poll_operation(self):
        if self._operation_id is None:
            return False
        client = getattr(self.window, "client", None)
        if client is None:
            self._operation_id = None
            self._dispose_interaction_dialogs()
            return False
        try:
            summary = client.get_operation(self._operation_id)
            if summary.state.value in {"succeeded", "failed", "cancelled"}:
                self._operation_id = None
                self._poll_id = None
                # The operation's interaction scope is done: release the
                # presenter so it never claims unrelated interactions again.
                self._dispose_interaction_dialogs()
                if summary.state.value == "succeeded":
                    self.window._info_dialog(
                        _("SSH Key Copy"),
                        _("The public key was installed on the server."),
                    )
                elif summary.state.value == "cancelled":
                    self.window._info_dialog(
                        _("SSH Key Copy"),
                        _("Public-key deployment was cancelled."),
                    )
                else:
                    self.window._error_dialog(
                        _("SSH Key Copy Error"),
                        _("Public-key deployment failed."),
                        summary.message,
                    )
                return False
        except Exception:
            logger.debug("Daemon deployment status unavailable", exc_info=True)
        self._poll_id = GLib.timeout_add(250, self._poll_operation)
        return False

    def _dispose_interaction_dialogs(self):
        """Close and detach the presenter; never claim interactions after teardown."""
        if self._interaction_dialogs is not None:
            self._interaction_dialogs.close()
            self._interaction_dialogs = None

    def cancel(self):
        operation_id = self._operation_id
        client = getattr(self.window, "client", None)
        if operation_id is not None and client is not None:
            try:
                client.cancel_operation(operation_id)
            except Exception:
                logger.debug("Daemon deployment cancellation failed", exc_info=True)
        self._operation_id = None
        self._dispose_interaction_dialogs()
        if self._poll_id is not None:
            try:
                GLib.source_remove(self._poll_id)
            except Exception:
                pass
            self._poll_id = None
