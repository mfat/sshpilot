import os
import logging
from gettext import gettext as _
from gi.repository import Gtk, Adw, GLib, Gio

from .key_manager import KeyManager, SSHKey
from .platform_utils import get_ssh_dir
from .connection_manager import ConnectionManager, Connection
from typing import Optional, Tuple
import shutil
import gi
try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
except Exception:
    Vte = None
from .terminal import TerminalWidget
from .config import Config
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
)
from .ssh_utils import ensure_writable_ssh_home

logger = logging.getLogger(__name__)


def _ssh_key_from_public_path(path: str) -> SSHKey:
    """Build an SSHKey for a user-chosen public key file.

    ssh-copy-id only needs the public key (``-i <pub>``), so we set
    ``public_path`` to exactly the chosen file regardless of extension. The
    private path is the conventional sibling (the ``.pub`` suffix stripped) and
    is only used for the dropdown label, mirroring discovered keys.
    """
    priv = path[:-4] if path.endswith('.pub') else path
    key = SSHKey(priv)
    key.public_path = path
    return key


class SshCopyIdWindow(Adw.Window):
    """
    Full Adwaita-styled window for installing a public key on a server.
    - Shows selected server nickname
    - Two modes:
        1) Use existing key (DropDown)
        2) Generate new key (embedded key-generator form)
    - Pressing OK triggers:
        - Either copy selected existing key
        - Or generate a new key, then copy it
    - Uses your existing terminal flow:
        parent._show_ssh_copy_id_terminal_using_main_widget(connection, ssh_key)
    """

    #: Trailing dropdown entry that opens the file chooser instead of selecting a key.
    _BROWSE_LABEL = _("Browse for a key file…")

    def __init__(self, parent, connection, key_manager, connection_manager):
        logger.info("SshCopyIdWindow: Initializing window")
        logger.debug(f"SshCopyIdWindow: Constructor called with connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"SshCopyIdWindow: Connection object type: {type(connection)}")
        logger.debug(f"SshCopyIdWindow: Key manager type: {type(key_manager)}")
        logger.debug(f"SshCopyIdWindow: Connection manager type: {type(connection_manager)}")

        # Title for the window and header bar
        title = _("Copy key to Server")

        try:
            super().__init__()
            self.set_transient_for(parent)
            self.set_modal(True)
            self.set_resizable(False)
            self.set_default_size(500, 400)
            # Set window title so desktop environments show it
            self.set_title(title)
            logger.debug("SshCopyIdWindow: Base window initialized")

            self._parent = parent
            self._conn = connection
            self._km = key_manager
            self._cm = connection_manager
            logger.debug("SshCopyIdWindow: Instance variables set")
            
            logger.info(f"SshCopyIdWindow: Window initialized for connection {getattr(connection, 'nickname', 'unknown')}")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to initialize window: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            raise

        # ---------- Outer layout ----------
        logger.info("SshCopyIdWindow: Creating outer layout")
        try:
            tv = Adw.ToolbarView()
            self.set_content(tv)
            
            # ---------- Header Bar ----------
            logger.info("SshCopyIdWindow: Creating header bar")
            hb = Adw.HeaderBar()
            tv.add_top_bar(hb)
            # Show title in the header bar
            hb.set_title_widget(Gtk.Label(label=title))

            # Cancel button
            btn_cancel = Gtk.Button(label="Cancel")
            btn_cancel.connect("clicked", self._on_close_clicked)
            hb.pack_start(btn_cancel)

            self.btn_ok = Gtk.Button(label="OK")
            self.btn_ok.add_css_class("suggested-action")
            self.btn_ok.connect("clicked", self._on_ok_clicked)
            hb.pack_end(self.btn_ok)
            logger.info("SshCopyIdWindow: Header bar created successfully")

            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            content.set_margin_top(18); content.set_margin_bottom(18)
            content.set_margin_start(18); content.set_margin_end(18)
            tv.set_content(content)

            try:
                illustration = Gtk.Image.new_from_resource(
                    '/io/github/mfat/sshpilot/keychain.png'
                )
                illustration.set_pixel_size(160)
                illustration.set_halign(Gtk.Align.CENTER)
                illustration.set_vexpand(False)
                illustration.set_margin_bottom(12)
                content.append(illustration)
            except GLib.Error as exc:
                logger.debug(
                    "ssh-copy-id dialog illustration unavailable: %s", exc
                )
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to create outer layout: {e}")
            raise

        # ---------- Options group ----------
        logger.info("SshCopyIdWindow: Creating options group")
        try:
            group = Adw.PreferencesGroup(title="")

            # Radio option 1: Use existing key (using CheckButton with group for radio behavior)
            self.radio_existing = Gtk.CheckButton(label="Copy existing key")
            self.radio_existing.set_can_focus(True)  # Make it focusable for tab navigation
            self.radio_generate = Gtk.CheckButton(label="Generate new key")
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
            existing_box.append(Gtk.Label(label="Select key:", xalign=0))
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
            self.row_key_name.set_title("Key file name")
            self.row_key_name.set_text("id_ed25519")
            self.row_key_name.set_can_focus(True)  # Make it focusable for tab navigation
            gen_box.append(self.row_key_name)

            # Key type
            key_type_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            key_type_label = Gtk.Label(label="Key type:", xalign=0)
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
                self.row_pass_toggle.set_title("Encrypt with passphrase")
                self.row_pass_toggle.set_activatable(True)  # Make the entire row clickable
                
                passphrase_group.add(self.row_pass_toggle)
                
                # Passphrase entries (outside revealer)
                self.pass1 = Gtk.PasswordEntry()
                self.pass1.set_property("placeholder-text", "Passphrase")
                self.pass1.set_can_focus(True)  # Make it focusable for tab navigation
                self.pass2 = Gtk.PasswordEntry()
                self.pass2.set_property("placeholder-text", "Confirm passphrase")
                self.pass2.set_can_focus(True)  # Make it focusable for tab navigation
                
                # Create a box for passphrase entries
                self.pass_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
                self.pass_box.set_margin_start(12)
                self.pass_box.set_margin_end(12)
                self.pass_box.set_margin_top(6)
                self.pass_box.set_margin_bottom(6)
                self.pass_box.append(self.pass1)
                self.pass_box.append(self.pass2)
                self.pass_box.set_visible(False)
                
                passphrase_group.add(self.pass_box)
                
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
                self.force_toggle.set_title("Force key transfer")
                self.force_toggle.set_subtitle("Overwrite existing keys on the server")
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

        logger.info("SshCopyIdWindow: Window construction completed, presenting")
        self.present()

    # ---------- Helpers ----------

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
        logger.info("SshCopyIdWindow: Reloading existing keys")
        logger.debug("SshCopyIdWindow: Calling key_manager.discover_keys()")
        try:
            keys = self._km.discover_keys()
            logger.info(f"SshCopyIdWindow: Discovered {len(keys)} keys")
            self._existing_keys_cache = list(keys)
            names = [os.path.basename(k.private_path) for k in keys] or [_("No keys found")]
            self._last_real_selection = 0
            self._rebuild_existing_dropdown(names, 0)
            logger.info(f"SshCopyIdWindow: Dropdown populated with {len(names)} key item(s)")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to load existing keys: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._existing_keys_cache = []
            self._last_real_selection = 0
            self._rebuild_existing_dropdown([_("Error loading keys")], 0)

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
        try:
            dlg = Gtk.FileChooserNative(
                title=_("Select Public Key"),
                action=Gtk.FileChooserAction.OPEN,
                transient_for=self,
                modal=True,
            )
            try:
                ssh_dir = get_ssh_dir()
                if os.path.isdir(ssh_dir):
                    dlg.set_current_folder(Gio.File.new_for_path(ssh_dir))
            except Exception:
                pass
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
        cache = list(getattr(self, "_existing_keys_cache", None) or [])

        # De-dupe: if this exact public key is already listed, just select it.
        for i, key in enumerate(cache):
            if getattr(key, "public_path", None) == path:
                self._last_real_selection = i
                self._rebuild_existing_dropdown(
                    [os.path.basename(k.private_path) for k in cache], i
                )
                self.radio_existing.set_active(True)
                return

        cache.append(_ssh_key_from_public_path(path))
        self._existing_keys_cache = cache
        new_idx = len(cache) - 1
        self._last_real_selection = new_idx
        self._rebuild_existing_dropdown(
            [os.path.basename(k.private_path) for k in cache], new_idx
        )
        # Browsing implies copying an existing key.
        self.radio_existing.set_active(True)

    def _info(self, title, body):
        try:
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=body)
            md.add_response("ok", "OK")
            md.set_default_response("ok")
            md.set_close_response("ok")
            md.present()
        except Exception:
            pass

    def _error(self, title, body, detail=""):
        try:
            text = body + (f"\n\n{detail}" if detail else "")
            md = Adw.MessageDialog(transient_for=self, modal=True, heading=title, body=text)
            md.add_response("close", "Close")
            md.set_default_response("close")
            md.set_close_response("close")
            md.present()
        except Exception:
            logger.error("%s: %s | %s", title, body, detail)

    def _on_close_clicked(self, *_):
        logger.info("SshCopyIdWindow: Close button clicked")
        self.close()

    # ---------- OK (main action) ----------
    def _on_ok_clicked(self, *_):
        logger.info("SshCopyIdWindow: OK button clicked")
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
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
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
            logger.info(f"SshCopyIdWindow: Selected key: {ssh_key.private_path}")
            logger.debug(f"SshCopyIdWindow: Selected key details - private_path='{ssh_key.private_path}', "
                       f"public_path='{ssh_key.public_path}', exists={os.path.exists(ssh_key.private_path)}")
            
            # Launch your existing terminal ssh-copy-id flow
            logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
            force_enabled = self.force_toggle.get_active()
            logger.debug(f"SshCopyIdWindow: Force option enabled: {force_enabled}")
            self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, ssh_key, force_enabled)
            logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
            self.close()
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Copy existing failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._error("Copy failed", "Could not copy the selected key to the server.", str(e))

    # ---------- Mode: generate ----------
    def _do_generate_and_copy(self):
        logger.info("SshCopyIdWindow: Starting generate and copy operation")
        logger.debug("SshCopyIdWindow: Processing key generation request")
        
        try:
            key_name = (self.row_key_name.get_text() or "").strip()
            logger.info(f"SshCopyIdWindow: Key name: '{key_name}'")
            logger.debug(f"SshCopyIdWindow: Raw key name from UI: '{self.row_key_name.get_text()}'")
            
            if not key_name:
                logger.debug("SshCopyIdWindow: Empty key name provided")
                raise ValueError("Enter a key file name (e.g. id_ed25519)")
            if "/" in key_name or key_name.startswith("."):
                logger.debug(f"SshCopyIdWindow: Invalid key name '{key_name}' - contains '/' or starts with '.'")
                raise ValueError("Key file name must not contain '/' or start with '.'")

            # Key type
            type_selection = self.type_dropdown.get_selected()
            kt = "ed25519" if type_selection == 0 else "rsa"
            logger.info(f"SshCopyIdWindow: Key type: {kt}")
            logger.debug(f"SshCopyIdWindow: Type selection index: {type_selection}, resolved to: {kt}")

            passphrase = None
            passphrase_enabled = self.row_pass_toggle.get_active()
            logger.debug(f"SshCopyIdWindow: Passphrase toggle state: {passphrase_enabled}")
            
            if passphrase_enabled:
                p1 = self.pass1.get_text() or ""
                p2 = self.pass2.get_text() or ""
                logger.debug(f"SshCopyIdWindow: Passphrase lengths - p1: {len(p1)}, p2: {len(p2)}")
                if p1 != p2:
                    logger.debug("SshCopyIdWindow: Passphrases do not match")
                    raise ValueError("Passphrases do not match")
                passphrase = p1
                logger.info("SshCopyIdWindow: Passphrase enabled")
                logger.debug("SshCopyIdWindow: Passphrase validation successful")

            logger.info(f"SshCopyIdWindow: Calling key_manager.generate_key with name='{key_name}', type='{kt}'")
            logger.debug(f"SshCopyIdWindow: Key generation parameters - name='{key_name}', type='{kt}', "
                       f"size={3072 if kt == 'rsa' else 0}, passphrase={'<set>' if passphrase else 'None'}")
            
            new_key = self._km.generate_key(
                key_name=key_name,
                key_type=kt,
                key_size=3072 if kt == "rsa" else 0,
                comment=None,
                passphrase=passphrase,
            )
            
            if not new_key:
                logger.debug("SshCopyIdWindow: Key generation returned None")
                raise RuntimeError("Key generation failed. See logs for details.")

            logger.info(f"SshCopyIdWindow: Key generated successfully: {new_key.private_path}")
            logger.debug(f"SshCopyIdWindow: Generated key details - private_path='{new_key.private_path}', "
                       f"public_path='{new_key.public_path}'")
            
            # Ensure the key files are properly written and accessible
            import time
            logger.debug("SshCopyIdWindow: Waiting 0.5s for files to be written")
            time.sleep(0.5)  # Small delay to ensure files are written
            
            # Verify the key files exist and are accessible
            private_exists = os.path.exists(new_key.private_path)
            public_exists = os.path.exists(new_key.public_path)
            logger.debug(f"SshCopyIdWindow: File existence check - private: {private_exists}, public: {public_exists}")
            
            if not private_exists:
                logger.debug(f"SshCopyIdWindow: Private key file missing: {new_key.private_path}")
                raise RuntimeError(f"Private key file not found: {new_key.private_path}")
            if not public_exists:
                logger.debug(f"SshCopyIdWindow: Public key file missing: {new_key.public_path}")
                raise RuntimeError(f"Public key file not found: {new_key.public_path}")
            
            logger.info(f"SshCopyIdWindow: Key files verified, starting ssh-copy-id")
            logger.debug("SshCopyIdWindow: All key files verified successfully")
            
            # Run your terminal ssh-copy-id flow
            logger.debug("SshCopyIdWindow: Calling _show_ssh_copy_id_terminal_using_main_widget()")
            force_enabled = self.force_toggle.get_active()
            logger.debug(f"SshCopyIdWindow: Force option enabled: {force_enabled}")
            self._parent._show_ssh_copy_id_terminal_using_main_widget(self._conn, new_key, force_enabled)
            logger.debug("SshCopyIdWindow: Terminal window launched, closing dialog")
            self.close()

        except Exception as e:
            logger.error(f"SshCopyIdWindow: Generate and copy failed: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")


class SshCopyIdRunner:
    """Run ssh-copy-id in an embedded terminal window for a connection.

    Collaborator of MainWindow (terminal_manager-style): borrows config,
    connection_manager and _error_dialog from ``self.window``.
    """

    def __init__(self, window):
        self.window = window

    @staticmethod
    def _find_ssh_copy_id_helper(binary_name: str) -> Optional[str]:
        """Return the preferred path for a helper used by ssh-copy-id."""
        flatpak_path = f"/app/bin/{binary_name}"
        if os.path.exists(flatpak_path) and os.access(flatpak_path, os.X_OK):
            return flatpak_path
        return shutil.which(binary_name)

    def _preflight(self, connection, ssh_key) -> Optional[Tuple[str, str]]:
        """Validate local prerequisites before opening the ssh-copy-id terminal."""
        if self._find_ssh_copy_id_helper('ssh-copy-id') is None:
            return (
                _('ssh-copy-id is not installed'),
                _('Install ssh-copy-id, then try copying the public key again.'),
            )

        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        if not host_value:
            return (
                _('Connection host is missing'),
                _('Set a host name or SSH config alias for this connection before copying a key.'),
            )

        try:
            port = getattr(connection, 'port', 22)
            if port not in (None, ''):
                port_num = int(port)
                if port_num <= 0 or port_num > 65535:
                    raise ValueError
        except (TypeError, ValueError):
            return (
                _('Invalid SSH port'),
                _('Set the connection port to a number between 1 and 65535.'),
            )

        public_path = getattr(ssh_key, 'public_path', '') or ''
        if not public_path:
            return (
                _('Public key is missing'),
                _('Select or generate a key with a public key file before copying it to the server.'),
            )

        expanded_public_path = os.path.expanduser(public_path)
        if not os.path.isfile(expanded_public_path):
            return (
                _('Public key file not found'),
                _('The selected public key file does not exist: {}').format(expanded_public_path),
            )
        if not os.access(expanded_public_path, os.R_OK):
            return (
                _('Public key file is not readable'),
                _('sshPilot cannot read the selected public key file: {}').format(expanded_public_path),
            )

        try:
            env = os.environ.copy()
            ensure_writable_ssh_home(env)
        except Exception as exc:
            logger.error('ssh-copy-id preflight failed while preparing SSH home: %s', exc)
            return (
                _('Could not prepare SSH environment'),
                _('sshPilot could not prepare a writable SSH home for ssh-copy-id: {}').format(exc),
            )

        try:
            auth_method = int(getattr(connection, 'auth_method', 0) or 0)
            username = getattr(connection, 'username', '')
            manager = getattr(self.window, 'connection_manager', None)
            has_saved_password = bool(manager.get_password(host_value, username)) if manager else False
            if (auth_method == 1 or (auth_method == 0 and has_saved_password)) and has_saved_password:
                if self._find_ssh_copy_id_helper('sshpass') is None:
                    logger.warning(
                        'ssh-copy-id preflight: sshpass unavailable; falling back to terminal password prompt',
                    )
        except Exception as exc:
            logger.debug('ssh-copy-id preflight skipped optional auth-helper check: %s', exc)

        return None

    def run(self, connection, ssh_key, force=False):
        """Show a window with header bar and embedded terminal running ssh-copy-id.

        Requirements:
        - Terminal expands horizontally, no borders around it
        - Header bar contains Cancel and Close buttons
        """
        logger.info("Main window: Starting ssh-copy-id terminal window creation")
        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        logger.debug(f"Main window: Connection details - host: {host_value}, "
                    f"username: {getattr(connection, 'username', 'unknown')}, "
                    f"port: {getattr(connection, 'port', 22)}")
        logger.debug(f"Main window: SSH key details - private_path: {getattr(ssh_key, 'private_path', 'unknown')}, "
                    f"public_path: {getattr(ssh_key, 'public_path', 'unknown')}")

        try:
            preflight_error = self._preflight(connection, ssh_key)
            if preflight_error:
                heading, body = preflight_error
                self.window._error_dialog(_("SSH Key Copy Error"), heading, body)
                return

            target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value
            pub_name = os.path.basename(getattr(ssh_key, 'public_path', '') or '')
            body_text = _('This will add your public key to the server\'s ~/.ssh/authorized_keys so future logins can use SSH keys.')
            logger.debug(f"Main window: Target: {target}, public key name: {pub_name}")

            dlg = Adw.Window()
            dlg.set_transient_for(self.window)
            dlg.set_modal(True)
            logger.debug("Main window: Created modal window")
            try:
                dlg.set_title(_('ssh-copy-id'))
            except Exception:
                pass
            try:
                dlg.set_default_size(920, 520)
            except Exception:
                pass

            # Header bar with Cancel
            header = Adw.HeaderBar()
            title_widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            title_label = Gtk.Label(label=_('ssh-copy-id'))
            title_label.set_halign(Gtk.Align.START)
            subtitle_label = Gtk.Label(label=_('Copying {key} to {target}').format(key=pub_name or _('selected key'), target=target))
            subtitle_label.set_halign(Gtk.Align.START)
            try:
                title_label.add_css_class('title-2')
                subtitle_label.add_css_class('dim-label')
            except Exception:
                pass
            title_widget.append(title_label)
            title_widget.append(subtitle_label)
            header.set_title_widget(title_widget)

            # Close button is omitted; window has native close (X)

            # Content: TerminalWidget without connecting spinner/banner
            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            try:
                content_box.set_margin_top(12)
                content_box.set_margin_bottom(12)
                content_box.set_margin_start(6)
                content_box.set_margin_end(6)
            except Exception:
                pass
            # Optional info text under header bar
            info_lbl = Gtk.Label(label=body_text)
            info_lbl.set_halign(Gtk.Align.START)
            try:
                info_lbl.add_css_class('dim-label')
                info_lbl.set_wrap(True)
            except Exception:
                pass
            content_box.append(info_lbl)

            term_widget = TerminalWidget(connection, self.window.config, self.window.connection_manager)
            # Hide connecting overlay and suppress disconnect banner for this non-SSH task
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            term_widget.set_hexpand(True)
            term_widget.set_vexpand(True)
            # No frame: avoid borders around the terminal
            content_box.append(term_widget)

            # Bottom button area with Close button
            button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            button_box.set_halign(Gtk.Align.END)
            button_box.set_margin_top(12)

            cancel_btn = Gtk.Button(label=_('Close'))
            try:
                cancel_btn.add_css_class('suggested-action')
            except Exception:
                pass
            button_box.append(cancel_btn)

            content_box.append(button_box)

            # Root container combines header and content
            root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            root_box.append(header)
            root_box.append(content_box)
            dlg.set_content(root_box)

            def _on_cancel(btn):
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass
                dlg.close()
            cancel_btn.connect('clicked', _on_cancel)
            # No explicit close button; use window close (X)

            # Resolve auth once via the single shared resolver so ssh-copy-id
            # authenticates exactly like the terminal and SCP.
            from .ssh_connection_builder import resolve_native_auth
            # For combined auth (publickey AND password) the key must be in
            # ssh-agent so its passphrase isn't prompted while sshpass owns the
            # pty for the password. resolve_native_auth now loads it as part of
            # committing to the combined path, so no separate preload is needed.
            auth = resolve_native_auth(
                connection,
                getattr(self.window, 'connection_manager', None),
                getattr(self.window, 'config', None),
            )

            # Build ssh-copy-id command with options derived from connection settings
            logger.debug("Main window: Building ssh-copy-id command arguments")
            argv = self._build_argv(
                connection,
                ssh_key,
                force,
                known_hosts_path=self.window.connection_manager.known_hosts_path,
                auth=auth,
            )
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.info("Full command line: %s", cmdline)
            logger.debug(f"Main window: Command argv: {argv}")
            logger.debug(f"Main window: Shell-quoted command: {cmdline}")

            # Helper to write colored lines into the terminal
            def _feed_colored_line(text: str, color: str):
                colors = {
                    'red': '\x1b[31m',
                    'green': '\x1b[32m',
                    'yellow': '\x1b[33m',
                    'blue': '\x1b[34m',
                }
                prefix = colors.get(color, '')
                try:
                    if hasattr(term_widget, 'backend') and term_widget.backend:
                        term_widget.backend.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.feed(("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8'))
                except Exception:
                    pass

            # Initial info line
            _feed_colored_line(_('Running ssh-copy-id…'), 'yellow')

            # Apply the resolved auth: env (askpass/keyring or none), any extra
            # opts (before the target), and sshpass for a stored password. This is
            # the single shared auth path (same as terminal + SCP).
            logger.debug("Main window: Applying resolved auth to ssh-copy-id env/argv")
            from .scp_utils import _apply_native_auth_env
            from .ssh_password_exec import wrap_argv_with_sshpass

            env = os.environ.copy()
            _apply_native_auth_env(env, auth)
            if auth.extra_opts:
                # ssh-copy-id requires -o options before the target.
                argv[-1:-1] = auth.extra_opts
            if auth.use_sshpass and auth.password:
                argv, _sshpass_cleanup = wrap_argv_with_sshpass(argv, auth.password, env=env)
                import atexit
                atexit.register(_sshpass_cleanup)
            logger.debug(
                "Main window: ssh-copy-id auth (askpass=%s, sshpass=%s)",
                auth.use_askpass, auth.use_sshpass,
            )

            ensure_writable_ssh_home(env)

            # Ensure /app/bin is first in PATH for Flatpak compatibility
            logger.debug("Main window: Setting up PATH for Flatpak compatibility")
            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                logger.debug(f"Main window: Current PATH: {current_path}")
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"
                    logger.debug(f"Main window: Updated PATH: {env['PATH']}")
                else:
                    logger.debug("Main window: /app/bin already in PATH")
            else:
                logger.debug("Main window: /app/bin does not exist, skipping PATH modification")

            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.debug(f"Main window: Final command line: {cmdline}")
            envv = [f"{k}={v}" for k, v in env.items()]
            logger.debug(f"Main window: Environment variables count: {len(envv)}")

            try:
                logger.debug("Main window: Spawning ssh-copy-id process in terminal")
                logger.debug(f"Main window: Working directory: {os.path.expanduser('~') or '/'}")
                logger.debug(f"Main window: Command: ['bash', '-lc', '{cmdline}']")

                # Convert envv to dict for backend
                env_dict = {}
                if envv:
                    for env_item in envv:
                        if '=' in env_item:
                            key, value = env_item.split('=', 1)
                            env_dict[key] = value

                if hasattr(term_widget, 'backend') and term_widget.backend:
                    term_widget.backend.spawn_async(
                        argv=['bash', '-lc', cmdline],
                        env=env_dict if env_dict else None,
                        cwd=os.path.expanduser('~') or '/',
                        flags=0,
                        child_setup=None,
                        callback=None,
                        user_data=None
                    )
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    term_widget.vte.spawn_async(
                        Vte.PtyFlags.DEFAULT,
                        os.path.expanduser('~') or '/',
                        ['bash', '-lc', cmdline],
                        envv,  # <— use merged env
                        GLib.SpawnFlags.DEFAULT,
                        None,
                        None,
                        -1,
                        None,
                        None
                    )
                logger.debug("Main window: ssh-copy-id process spawned successfully")

                # Show result modal when the command finishes
                def _on_copyid_exited(widget, status):
                    logger.debug(f"Main window: ssh-copy-id process exited with raw status: {status}")
                    # Normalize exit code
                    exit_code = None
                    try:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                            logger.debug(f"Main window: Process exited normally, exit code: {exit_code}")
                        else:
                            exit_code = status if 0 <= int(status) < 256 else ((int(status) >> 8) & 0xFF)
                            logger.debug(f"Main window: Process did not exit normally, normalized exit code: {exit_code}")
                    except Exception as e:
                        logger.debug(f"Main window: Error normalizing exit status: {e}")
                        try:
                            exit_code = int(status)
                            logger.debug(f"Main window: Converted status to int: {exit_code}")
                        except Exception as e2:
                            logger.debug(f"Main window: Failed to convert status to int: {e2}")
                            exit_code = status

                    logger.info(f"ssh-copy-id exited with status: {status}, normalized exit_code: {exit_code}")

                    # Simple verification: just check exit code like default ssh-copy-id
                    ok = (exit_code == 0)

                    # Get error details from output if failed
                    error_details = None
                    if not ok:
                        try:
                            content = None
                            backend = getattr(term_widget, 'backend', None)
                            if backend and hasattr(backend, 'get_content'):
                                content = backend.get_content()
                            if content is None and hasattr(term_widget, 'vte') and term_widget.vte:
                                content_result = term_widget.vte.get_text_range(
                                    0,
                                    0,
                                    -1,
                                    -1,
                                    lambda *args: True,
                                )
                                content = content_result[0] if content_result else None
                            if content:
                                # Look for common error patterns in the output
                                content_lower = content.lower()
                                if 'permission denied' in content_lower:
                                    error_details = 'Permission denied - check user credentials and server permissions'
                                elif 'connection refused' in content_lower:
                                    error_details = 'Connection refused - check server address and SSH service'
                                elif 'authentication failed' in content_lower:
                                    error_details = 'Authentication failed - check username and password/key'
                                elif 'no such file or directory' in content_lower:
                                    error_details = 'File not found - check if SSH directory exists on server'
                                elif 'operation not permitted' in content_lower:
                                    error_details = 'Operation not permitted - check server permissions'
                                else:
                                    # Extract the last few lines of output for context
                                    stripped_content = content.strip() if content else ''
                                    lines = stripped_content.split('\n') if stripped_content else []
                                    if lines:
                                        error_details = f"Error details: {lines[-1]}"
                        except Exception as e:
                            logger.debug(f"Main window: Error extracting error details: {e}")

                    if ok:
                        logger.info("ssh-copy-id completed successfully")
                        logger.debug("Main window: ssh-copy-id succeeded, showing success message")
                        _feed_colored_line(_('Public key was installed successfully.'), 'green')
                    else:
                        logger.error(f"ssh-copy-id failed with exit code: {exit_code}")
                        logger.debug(f"Main window: ssh-copy-id failed with exit code {exit_code}")
                        _feed_colored_line(_('Failed to install the public key.'), 'red')
                        if error_details:
                            _feed_colored_line(error_details, 'red')

                    def _present_result_dialog():
                        logger.debug(f"Main window: Presenting result dialog - success: {ok}")
                        msg = Adw.MessageDialog(
                            transient_for=dlg,
                            modal=True,
                            heading=_('Success') if ok else _('Error'),
                            body=(_('Public key copied to {}@{}').format(connection.username, host_value)
                                  if ok else _('Failed to copy the public key. Check logs for details.')),
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                        logger.debug("Main window: Result dialog presented")
                        return False

                    GLib.idle_add(_present_result_dialog)

                try:
                    # Connect child-exited signal using backend
                    if hasattr(term_widget, 'backend') and term_widget.backend:
                        term_widget.backend.connect_child_exited(_on_copyid_exited)
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.connect('child-exited', _on_copyid_exited)
                except Exception:
                    pass
            except Exception as e:
                logger.error(f'Failed to spawn ssh-copy-id in TerminalWidget: {e}')
                logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
                dlg.close()
                # No fallback method available
                logger.error(f'Terminal ssh-copy-id failed: {e}')
                self.window._error_dialog(_("SSH Key Copy Error"),
                                  _("Failed to copy SSH key to server."), 
                                  f"Terminal error: {str(e)}\n\nPlease check:\n• Network connectivity\n• SSH server configuration\n• User permissions")
                return

            dlg.present()
            logger.debug("Main window: ssh-copy-id terminal window presented successfully")
        except Exception as e:
            logger.error(f'VTE ssh-copy-id window failed: {e}')
            logger.debug(f'Main window: Exception details: {type(e).__name__}: {str(e)}')
            self.window._error_dialog(_("SSH Key Copy Error"),
                              _("Failed to create ssh-copy-id terminal window."), 
                              f"Error: {str(e)}\n\nThis could be due to:\n• Missing VTE terminal widget\n• Display/GTK issues\n• System resource limitations")


    def _build_argv(
        self,
        connection,
        ssh_key,
        force: bool = False,
        known_hosts_path: Optional[str] = None,
        auth=None,
    ):
        """Construct argv for ssh-copy-id honoring saved UI auth preferences.

        When ``auth`` (a resolved ``NativeAuth``) is supplied, the
        PreferredAuthentications choice is driven off it (single shared auth
        decision) instead of recomputing saved-password state here.
        """
        logger.info(f"Building ssh-copy-id argv for key: {getattr(ssh_key, 'public_path', 'unknown')}")
        logger.debug(f"Main window: Building ssh-copy-id command arguments")
        logger.debug(f"Main window: Connection object: {type(connection)}")
        logger.debug(f"Main window: SSH key object: {type(ssh_key)}")
        logger.debug(f"Main window: Force option: {force}")
        logger.info(f"Key object attributes: private_path={getattr(ssh_key, 'private_path', 'unknown')}, public_path={getattr(ssh_key, 'public_path', 'unknown')}")
        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        
        # Verify the public key file exists
        logger.debug(f"Main window: Checking if public key file exists: {ssh_key.public_path}")
        if not os.path.exists(ssh_key.public_path):
            logger.error(f"Public key file does not exist: {ssh_key.public_path}")
            logger.debug(f"Main window: Public key file missing: {ssh_key.public_path}")
            raise RuntimeError(f"Public key file not found: {ssh_key.public_path}")
        
        logger.debug(f"Main window: Public key file verified: {ssh_key.public_path}")
        argv = ['ssh-copy-id']
        
        # Add force option if enabled
        if force:
            argv.append('-f')
            logger.debug("Main window: Added force option (-f) to ssh-copy-id")
        
        argv.extend(['-i', ssh_key.public_path])
        logger.debug(f"Main window: Base command: {argv}")
        try:
            port = getattr(connection, 'port', 22)
            logger.debug(f"Main window: Connection port: {port}")
            if port and port != 22:
                argv += ['-p', str(connection.port)]
                logger.debug(f"Main window: Added port option: -p {connection.port}")
        except Exception as e:
            logger.debug(f"Main window: Error getting port: {e}")
            pass
        # Honor app SSH settings: strict host key checking / auto-add
        logger.debug("Main window: Loading SSH configuration")
        try:
            cfg = Config()
            ssh_cfg = cfg.get_ssh_config() if hasattr(cfg, 'get_ssh_config') else {}
            logger.debug(f"Main window: SSH config: {ssh_cfg}")
            strict_val = str(ssh_cfg.get('strict_host_key_checking', '') or '').strip()
            auto_add = bool(ssh_cfg.get('auto_add_host_keys', True))
            logger.debug(f"Main window: SSH settings - strict_val='{strict_val}', auto_add={auto_add}")
            if strict_val:
                argv += ['-o', f'StrictHostKeyChecking={strict_val}']
                logger.debug(f"Main window: Added strict host key checking: {strict_val}")
            elif auto_add:
                argv += ['-o', 'StrictHostKeyChecking=accept-new']
                logger.debug("Main window: Added auto-accept new host keys")
        except Exception as e:
            logger.debug(f"Main window: Error loading SSH config: {e}")
            argv += ['-o', 'StrictHostKeyChecking=accept-new']
            logger.debug("Main window: Using default strict host key checking: accept-new")

        if known_hosts_path:
            argv += ['-o', f'UserKnownHostsFile={known_hosts_path}']

        # Port forwards are useless for key-copying and can cause failure when
        # ExitOnForwardFailure=yes is set and a port is already in use.
        argv += ['-o', 'ClearAllForwardings=yes']

        # Derive auth prefs. Prefer the resolved NativeAuth (single shared auth
        # decision); fall back to recomputing from the connection when not given.
        logger.debug("Main window: Determining authentication preferences")
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        logger.debug(f"Main window: Connection keyfile: '{keyfile}'")

        if auth is not None:
            prefer_password = bool(getattr(auth, 'password_mode', False))
            combined_auth = bool(getattr(auth, 'use_sshpass', False)) and not prefer_password
        else:
            try:
                auth_method = int(getattr(connection, 'auth_method', 0) or 0)
            except Exception as e2:
                logger.debug(f"Main window: Error getting auth method from connection object: {e2}")
                auth_method = 0
            prefer_password = (auth_method == 1)
            has_saved_password = bool(self.window.connection_manager.get_password(host_value, connection.username))
            combined_auth = (auth_method == 0 and has_saved_password)

        try:
            # key_select_mode is saved in ssh config, our connection object should have it post-load
            key_mode = int(getattr(connection, 'key_select_mode', 0) or 0)
            logger.debug(f"Main window: Key select mode: {key_mode}")
        except Exception as e:
            logger.debug(f"Main window: Error getting key select mode: {e}")
            key_mode = 0

        # Validate keyfile path
        try:
            keyfile_ok = bool(keyfile) and os.path.isfile(keyfile)
            logger.debug(f"Main window: Keyfile validation - keyfile='{keyfile}', exists={keyfile_ok}")
        except Exception as e:
            logger.debug(f"Main window: Error validating keyfile: {e}")
            keyfile_ok = False

        # Priority: if UI selected a specific key and it exists, use it; otherwise fall back to password prefs/try-all
        logger.debug(f"Main window: Applying authentication options - key_mode={key_mode}, keyfile_ok={keyfile_ok}, prefer_password={prefer_password}, combined_auth={combined_auth}")
        
        # For ssh-copy-id, we should NOT add IdentityFile options because:
        # 1. ssh-copy-id should use the same key for authentication that it's copying
        # 2. The -i parameter already specifies which key to copy
        # 3. Adding IdentityFile would cause ssh-copy-id to use a different key for auth
        
        if key_mode == 1 and keyfile_ok:
            # Don't add IdentityFile for ssh-copy-id - it should use the key being copied
            logger.debug(f"Main window: Skipping IdentityFile for ssh-copy-id - using key being copied for authentication")
        else:
            # Apply authentication preferences
            if prefer_password:
                argv += ['-o', 'PreferredAuthentications=password']
                if getattr(connection, 'pubkey_auth_no', False):
                    argv += ['-o', 'PubkeyAuthentication=no']
                    logger.debug("Main window: Added password authentication options - PubkeyAuthentication=no, PreferredAuthentications=password")
                else:
                    logger.debug("Main window: Added password authentication option - PreferredAuthentications=password")
            elif combined_auth:
                argv += [
                    '-o',
                    'PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password'
                ]
                logger.debug(
                    "Main window: Added combined authentication options - "
                    "PreferredAuthentications=gssapi-with-mic,hostbased,publickey,keyboard-interactive,password"
                )
        
        # Target
        target = f"{connection.username}@{host_value}" if getattr(connection, 'username', '') else host_value
        argv.append(target)
        logger.debug(f"Main window: Added target: {target}")
        logger.debug(f"Main window: Final argv: {argv}")
        return argv

