import os
import logging
from gettext import gettext as _
from gi.repository import Gtk, Adw

from .key_manager import KeyManager, SSHKey
from .connection_manager import ConnectionManager, Connection

logger = logging.getLogger(__name__)

class SshCopyIdWindow(Gtk.Window):
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

    def __init__(self, parent, connection, key_manager, connection_manager):
        logger.info("SshCopyIdWindow: Initializing window")
        logger.debug(f"SshCopyIdWindow: Constructor called with connection: {getattr(connection, 'nickname', 'unknown')}")
        logger.debug(f"SshCopyIdWindow: Connection object type: {type(connection)}")
        logger.debug(f"SshCopyIdWindow: Key manager type: {type(key_manager)}")
        logger.debug(f"SshCopyIdWindow: Connection manager type: {type(connection_manager)}")
        
        try:
            super().__init__()
            self.set_transient_for(parent)
            self.set_modal(True)
            self.set_title("Install Public Key on Server")
            self.set_resizable(False)
            self.set_default_size(500, 400)
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
            self.set_child(tv)
            
            # ---------- Header Bar ----------
            logger.info("SshCopyIdWindow: Creating header bar")
            hb = Adw.HeaderBar()
            tv.add_top_bar(hb)

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

            # ---------- Intro text ----------
            server_name = getattr(self._conn, "nickname", None) or \
                          f"{getattr(self._conn, 'username', 'user')}@{getattr(self._conn, 'host', 'host')}"
            
            # Create a simple label instead of StatusPage for normal font size
            intro_label = Gtk.Label()
            intro_label.set_markup(f'Copy your public key to "{server_name}".')
            intro_label.set_halign(Gtk.Align.CENTER)
            intro_label.set_margin_bottom(12)
            content.append(intro_label)
            logger.info(f"SshCopyIdWindow: Intro text created for server: {server_name}")
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
            self.dropdown_existing = Gtk.DropDown()
            self.dropdown_existing.set_can_focus(True)  # Make it focusable for tab navigation
            existing_box.append(Gtk.Label(label="Select key:", xalign=0))
            existing_box.append(self.dropdown_existing)

            # Fill dropdown with discovered keys
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
            logger.debug(f"SshCopyIdWindow: Key discovery returned {len(keys)} keys")
            
            # Log details of each discovered key
            for i, key in enumerate(keys):
                logger.debug(f"SshCopyIdWindow: Key {i+1}: private_path='{key.private_path}', "
                           f"public_path='{key.public_path}', exists={os.path.exists(key.private_path)}")
            
            names = [os.path.basename(k.private_path) for k in keys] or ["No keys found"]
            logger.debug(f"SshCopyIdWindow: Key names for dropdown: {names}")
            
            dd = Gtk.DropDown.new_from_strings(names)
            if keys:
                dd.set_selected(0)
                logger.debug(f"SshCopyIdWindow: Selected first key in dropdown")
            
            self.dropdown_existing.set_model(dd.get_model())
            self.dropdown_existing.set_selected(dd.get_selected())
            # keep a cached list to resolve on OK
            self._existing_keys_cache = keys
            logger.info(f"SshCopyIdWindow: Dropdown populated with {len(names)} items")
            logger.debug(f"SshCopyIdWindow: Cached {len(keys)} keys for later use")
        except Exception as e:
            logger.error(f"SshCopyIdWindow: Failed to load existing keys: {e}")
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {str(e)}")
            self._existing_keys_cache = []
            dd = Gtk.DropDown.new_from_strings(["Error loading keys"])
            self.dropdown_existing.set_model(dd.get_model())
            self.dropdown_existing.set_selected(0)

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
