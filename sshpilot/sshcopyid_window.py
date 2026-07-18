import atexit
import logging
import os
import shutil
from gettext import gettext as _
from typing import Callable, Optional, Tuple

import gi
from gi.repository import Adw, GLib, Gio, Gtk

try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
except Exception:
    Vte = None

from .command_progress_dialog import (
    build_progress_status_row,
    build_terminal_disclosure,
    normalize_child_exit_status as _normalize_child_exit_status,
    read_terminal_text as _read_ssh_copyid_terminal_text,
    terminal_awaiting_input as _terminal_awaiting_input,
    wrap_dialog_terminal,
)
from .config import Config
from .key_manager import SSHKey
from .platform_utils import get_ssh_dir
from .terminal import TerminalWidget
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
)
from .ssh_utils import ensure_writable_ssh_home

logger = logging.getLogger(__name__)

_COPYID_FAILURE_MARKERS = (
    'permission denied',
    'authentication failed',
    'connection refused',
    'operation not permitted',
    'no such file or directory',
    'host key verification failed',
    'failed to install',
    'failed to copy',
)

# ssh-copy-id prints these (unlocalized) on a successful run; failure markers
# can also appear in successful runs (e.g. a mistyped password the user
# retried), so a success marker outranks them when the exit code is zero.
_COPYID_SUCCESS_MARKERS = (
    'number of key(s) added',
    'all keys were skipped because they already exist',
)


def _terminal_indicates_copy_failure(text: str) -> bool:
    lowered = (text or '').lower()
    return any(marker in lowered for marker in _COPYID_FAILURE_MARKERS)


def _terminal_indicates_copy_success(text: str) -> bool:
    lowered = (text or '').lower()
    return any(marker in lowered for marker in _COPYID_SUCCESS_MARKERS)


def _copyid_run_succeeded(exit_code: int, content: str) -> bool:
    if exit_code != 0:
        return False
    if _terminal_indicates_copy_success(content):
        return True
    return not _terminal_indicates_copy_failure(content)


def _wrap_sshcopyid_terminal(term_widget: TerminalWidget) -> Gtk.Widget:
    """Thin wrapper so tests can monkeypatch the dialog terminal card."""
    return wrap_dialog_terminal(term_widget)


def _build_terminal_disclosure(
    terminal_card: Gtk.Widget,
    on_expanded_changed: Callable[[bool], None],
) -> Tuple[Gtk.Widget, Callable[[bool], None], Callable[[], bool]]:
    """Thin wrapper so tests can monkeypatch the terminal disclosure."""
    return build_terminal_disclosure(terminal_card, on_expanded_changed)


def _build_copy_progress_row(
    pub_name: str,
    target: str,
) -> Tuple[
    Gtk.Widget,
    Callable[[], bool],
    Callable[[], None],
    Callable[[], None],
    Callable[[], None],
]:
    """Thin wrapper: key-copy status strings over the shared progress row."""
    key_name = pub_name or _('selected')
    return build_progress_status_row(
        _('Copying key {name} to {target}').format(name=key_name, target=target),
        _('Copied key {name} to {target}').format(name=key_name, target=target),
        _('Failed to copy key {name} to {target}').format(
            name=key_name, target=target,
        ),
    )


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
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")
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
        self.btn_ok.set_sensitive(conn is not None)

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
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")
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
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")
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
            logger.debug(f"SshCopyIdWindow: Exception details: {type(e).__name__}: {e!s}")


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
            # Password delivery is via askpass (REQUIRE=force); graphical prompts.
            if auth_method == 1 and has_saved_password:
                logger.debug(
                    'ssh-copy-id preflight: password-method with saved password '
                    '(askpass will autofill; MFA via askpass)',
                )
        except Exception as exc:
            logger.debug('ssh-copy-id preflight skipped optional auth-helper check: %s', exc)

        return None

    def run(self, connection, ssh_key, force=False):
        """Show an Adw window with embedded terminal running ssh-copy-id."""
        logger.info("Main window: Starting ssh-copy-id terminal window creation")
        host_value = _get_connection_host(connection) or _get_connection_alias(connection)
        logger.debug(
            "Main window: Connection details - host: %s, username: %s, port: %s",
            host_value,
            getattr(connection, 'username', 'unknown'),
            getattr(connection, 'port', 22),
        )
        logger.debug(
            "Main window: SSH key details - private_path: %s, public_path: %s",
            getattr(ssh_key, 'private_path', 'unknown'),
            getattr(ssh_key, 'public_path', 'unknown'),
        )

        try:
            preflight_error = self._preflight(connection, ssh_key)
            if preflight_error:
                heading, body = preflight_error
                self.window._error_dialog(_("SSH Key Copy Error"), heading, body)
                return

            target = (
                f"{connection.username}@{host_value}"
                if getattr(connection, 'username', '')
                else host_value
            )
            pub_name = os.path.basename(getattr(ssh_key, 'public_path', '') or '')
            logger.debug("Main window: Target: %s, public key name: %s", target, pub_name)

            dlg = Adw.Dialog.new()
            dlg.set_title(_('ssh-copy-id'))
            # Track the content's natural size so the dialog grows/shrinks
            # with the terminal revealer animation.
            dlg.set_follows_content_size(True)

            toolbar = Adw.ToolbarView()
            dlg.set_child(toolbar)

            header = Adw.HeaderBar()
            header.set_show_end_title_buttons(False)
            header.set_title_widget(Gtk.Label(label=_('ssh-copy-id')))

            copyid_exit_state = {
                'finished': False,
                'handler_id': None,
                'prompt_poll_id': None,
            }

            def _stop_prompt_poller() -> None:
                poll_id = copyid_exit_state.get('prompt_poll_id')
                if poll_id is None:
                    return
                copyid_exit_state['prompt_poll_id'] = None
                try:
                    GLib.source_remove(poll_id)
                except Exception:
                    pass

            def _on_dialog_closed(*_args):
                # Closing the dialog (Cancel/Close button or Esc) kills the
                # child below, which still fires child-exited; mark the run
                # finished first so cancellation isn't reported as a failure.
                copyid_exit_state['finished'] = True
                _stop_prompt_poller()
                stop_copy_spinner()
                try:
                    if hasattr(term_widget, 'disconnect'):
                        term_widget.disconnect()
                except Exception:
                    pass

            dlg.connect('closed', _on_dialog_closed)

            def _close_window(*_args):
                dlg.close()

            cancel_btn = Gtk.Button(label=_('Cancel'))
            cancel_btn.connect('clicked', _close_window)
            header.pack_start(cancel_btn)

            close_btn = Gtk.Button(label=_('Close'))
            close_btn.add_css_class('suggested-action')
            close_btn.connect('clicked', _close_window)
            header.pack_end(close_btn)

            toolbar.add_top_bar(header)

            content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            content_box.set_hexpand(True)
            content_box.set_vexpand(True)
            # Constant natural width so toggling the terminal only changes
            # height; VTE reflows its columns to whatever width it gets.
            content_box.set_size_request(560, -1)
            content_box.set_margin_top(12)
            content_box.set_margin_bottom(12)
            content_box.set_margin_start(12)
            content_box.set_margin_end(12)

            (
                progress_row,
                start_copy_spinner,
                stop_copy_spinner,
                mark_copy_success,
                mark_copy_failure,
            ) = _build_copy_progress_row(pub_name, target)
            content_box.append(progress_row)

            term_widget = TerminalWidget(
                connection,
                self.window.config,
                self.window.connection_manager,
            )
            try:
                term_widget._set_connecting_overlay_visible(False)
                setattr(term_widget, '_suppress_disconnect_banner', True)
                setattr(term_widget, '_suppress_connection_exit_handling', True)
                term_widget._set_disconnected_banner_visible(False)
            except Exception:
                pass
            terminal_card = _wrap_sshcopyid_terminal(term_widget)
            # VTE's natural height is tiny; give the expanded card a real one.
            terminal_card.set_size_request(-1, 260)

            def _focus_terminal_input() -> bool:
                try:
                    if hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.grab_focus()
                    else:
                        term_widget.grab_focus()
                except Exception:
                    pass
                return False

            def _on_terminal_expanded_changed(expanded: bool) -> None:
                if not expanded:
                    return
                # First expansion (manual or auto) ends prompt watching.
                _stop_prompt_poller()
                if not copyid_exit_state['finished']:
                    GLib.idle_add(_focus_terminal_input)

            (
                terminal_disclosure,
                set_terminal_expanded,
                terminal_is_expanded,
            ) = _build_terminal_disclosure(terminal_card, _on_terminal_expanded_changed)
            content_box.append(terminal_disclosure)

            toolbar.set_content(content_box)

            from .ssh_connection_builder import (
                apply_forced_askpass_env,
                resolve_native_auth,
            )

            auth = resolve_native_auth(
                connection,
                getattr(self.window, 'connection_manager', None),
                getattr(self.window, 'config', None),
            )

            known_hosts_path = None
            manager = getattr(self.window, 'connection_manager', None)
            if manager is not None:
                known_hosts_path = getattr(manager, 'known_hosts_path', None)

            argv = self._build_argv(
                connection,
                ssh_key,
                force,
                known_hosts_path=known_hosts_path,
                auth=auth,
            )
            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            logger.info("Starting ssh-copy-id: %s", ' '.join(argv))
            logger.debug("Main window: Shell-quoted command: %s", cmdline)

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
                        term_widget.backend.feed(
                            ("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8')
                        )
                    elif hasattr(term_widget, 'vte') and term_widget.vte:
                        term_widget.vte.feed(
                            ("\r\n" + prefix + text + "\x1b[0m\r\n").encode('utf-8')
                        )
                except Exception:
                    pass

            _feed_colored_line(_('Running ssh-copy-id…'), 'yellow')

            # REQUIRE=force: graphical askpass for passphrase/password/MFA even
            # though ssh-copy-id runs inside a VTE (which has a real TTY).
            env = apply_forced_askpass_env(
                auth.env,
                connection,
                session_password=getattr(auth, 'password', None),
            )
            if auth.extra_opts:
                argv[-1:-1] = auth.extra_opts
            logger.debug(
                "Main window: ssh-copy-id auth (askpass force, resolver_askpass=%s)",
                auth.use_askpass,
            )

            ensure_writable_ssh_home(env)

            if os.path.exists('/app/bin'):
                current_path = env.get('PATH', '')
                if '/app/bin' not in current_path:
                    env['PATH'] = f"/app/bin:{current_path}"

            cmdline = ' '.join([GLib.shell_quote(a) for a in argv])
            envv = [f"{k}={v}" for k, v in env.items()]
            env_dict = {}
            for env_item in envv:
                if '=' in env_item:
                    key, value = env_item.split('=', 1)
                    env_dict[key] = value

            try:
                if hasattr(term_widget, 'backend') and term_widget.backend:
                    term_widget.backend.spawn_async(
                        argv=['bash', '-lc', cmdline],
                        env=env_dict if env_dict else None,
                        cwd=os.path.expanduser('~') or '/',
                        flags=0,
                        child_setup=None,
                        callback=None,
                        user_data=None,
                    )
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    term_widget.vte.spawn_async(
                        Vte.PtyFlags.DEFAULT,
                        os.path.expanduser('~') or '/',
                        ['bash', '-lc', cmdline],
                        envv,
                        GLib.SpawnFlags.DEFAULT,
                        None,
                        None,
                        -1,
                        None,
                        None,
                    )
                logger.debug("Main window: ssh-copy-id process spawned successfully")
                try:
                    term_widget._install_pty_autofill()
                except Exception:
                    logger.debug("ssh-copy-id: could not arm PTY auto-fill", exc_info=True)

                def _disconnect_copyid_exit_handler() -> None:
                    handler_id = copyid_exit_state.get('handler_id')
                    if handler_id is None:
                        return
                    try:
                        if hasattr(term_widget, 'backend') and term_widget.backend:
                            term_widget.backend.disconnect(handler_id)
                        elif hasattr(term_widget, 'vte') and term_widget.vte:
                            term_widget.vte.disconnect(handler_id)
                    except Exception:
                        pass
                    copyid_exit_state['handler_id'] = None

                def _finish_ssh_copy_id(status) -> bool:
                    if copyid_exit_state['finished']:
                        return False
                    copyid_exit_state['finished'] = True
                    _stop_prompt_poller()
                    _disconnect_copyid_exit_handler()

                    exit_code = _normalize_child_exit_status(status)
                    content = _read_ssh_copyid_terminal_text(term_widget)
                    content_failed = _terminal_indicates_copy_failure(content)
                    content_succeeded = _terminal_indicates_copy_success(content)
                    ok = _copyid_run_succeeded(exit_code, content)

                    logger.info(
                        "ssh-copy-id exited with status: %s, normalized exit_code: %s, "
                        "content_failure=%s, content_success=%s, ok=%s",
                        status,
                        exit_code,
                        content_failed,
                        content_succeeded,
                        ok,
                    )

                    error_details = None
                    if not ok:
                        content_lower = content.lower()
                        if 'permission denied' in content_lower:
                            error_details = (
                                'Permission denied - check user credentials '
                                'and server permissions'
                            )
                        elif 'connection refused' in content_lower:
                            error_details = (
                                'Connection refused - check server address '
                                'and SSH service'
                            )
                        elif 'authentication failed' in content_lower:
                            error_details = (
                                'Authentication failed - check username and password/key'
                            )
                        elif 'no such file or directory' in content_lower:
                            error_details = (
                                'File not found - check if SSH directory exists on server'
                            )
                        elif 'operation not permitted' in content_lower:
                            error_details = (
                                'Operation not permitted - check server permissions'
                            )
                        else:
                            lines = [
                                line for line in content.strip().split('\n') if line
                            ]
                            if lines:
                                error_details = f"Error details: {lines[-1]}"

                    if ok:
                        mark_copy_success()
                        _feed_colored_line(_('Public key was installed successfully.'), 'green')
                    else:
                        mark_copy_failure()
                        _feed_colored_line(_('Failed to install the public key.'), 'red')
                        if error_details:
                            _feed_colored_line(error_details, 'red')
                        # Reveal the error output behind the alert dialog.
                        set_terminal_expanded(True)

                    if ok:
                        # The progress row and terminal already show success;
                        # an alert on top would be redundant.
                        return False

                    heading = _('Error')
                    body = _(
                        'Failed to copy the public key. '
                        'Check logs for details.'
                    )
                    if hasattr(Adw, 'AlertDialog'):
                        msg = Adw.AlertDialog(heading=heading, body=body)
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present(dlg)
                    else:
                        msg = Adw.MessageDialog(
                            transient_for=self.window,
                            modal=True,
                            heading=heading,
                            body=body,
                        )
                        msg.add_response('ok', _('OK'))
                        msg.set_default_response('ok')
                        msg.set_close_response('ok')
                        msg.present()
                    return False

                def _on_copyid_exited(widget, status):
                    GLib.idle_add(_finish_ssh_copy_id, status)

                if hasattr(term_widget, 'backend') and term_widget.backend:
                    copyid_exit_state['handler_id'] = (
                        term_widget.backend.connect_child_exited(_on_copyid_exited)
                    )
                elif hasattr(term_widget, 'vte') and term_widget.vte:
                    copyid_exit_state['handler_id'] = term_widget.vte.connect(
                        'child-exited', _on_copyid_exited,
                    )

                def _poll_for_prompt() -> bool:
                    if copyid_exit_state['finished'] or terminal_is_expanded():
                        copyid_exit_state['prompt_poll_id'] = None
                        return GLib.SOURCE_REMOVE
                    content = _read_ssh_copyid_terminal_text(term_widget)
                    if _terminal_awaiting_input(content):
                        copyid_exit_state['prompt_poll_id'] = None
                        set_terminal_expanded(True)
                        return GLib.SOURCE_REMOVE
                    return GLib.SOURCE_CONTINUE

                copyid_exit_state['prompt_poll_id'] = GLib.timeout_add(
                    400, _poll_for_prompt,
                )
            except Exception as e:
                logger.error('Failed to spawn ssh-copy-id in TerminalWidget: %s', e)
                dlg.close()
                self.window._error_dialog(
                    _("SSH Key Copy Error"),
                    _("Failed to copy SSH key to server."),
                    (
                        f"Terminal error: {e!s}\n\nPlease check:\n"
                        "• Network connectivity\n"
                        "• SSH server configuration\n"
                        "• User permissions"
                    ),
                )
                return

            dlg.present(self.window)
            GLib.idle_add(start_copy_spinner)
            logger.debug("Main window: ssh-copy-id dialog presented successfully")
        except Exception as e:
            logger.error('VTE ssh-copy-id window failed: %s', e)
            self.window._error_dialog(
                _("SSH Key Copy Error"),
                _("Failed to create ssh-copy-id terminal window."),
                (
                    f"Error: {e!s}\n\nThis could be due to:\n"
                    "• Missing VTE terminal widget\n"
                    "• Display/GTK issues\n"
                    "• System resource limitations"
                ),
            )


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

        # Shared command prefix via the single option builder (same one the SCP
        # paths use): app-level -o options, strict-host policy, port and
        # ClearAllForwardings. The builder skips flags ssh-copy-id can't take
        # (-v/-C/-A/BatchMode) and never injects IdentityFile for it, so the
        # operation authenticates with the key being copied.
        from .ssh_connection_builder import _build_base_ssh_command
        from .ssh_config_utils import get_effective_ssh_config

        try:
            effective_config = get_effective_ssh_config(host_value) if host_value else {}
        except Exception:
            effective_config = {}
        app_cfg = getattr(self.window, 'config', None)
        if app_cfg is None:
            app_cfg = Config()
        argv = _build_base_ssh_command(connection, effective_config, app_cfg, 'ssh-copy-id')

        # Add force option if enabled
        if force:
            argv.append('-f')
            logger.debug("Main window: Added force option (-f) to ssh-copy-id")

        argv.extend(['-i', ssh_key.public_path])
        logger.debug(f"Main window: Base command: {argv}")

        if known_hosts_path:
            argv += ['-o', f'UserKnownHostsFile={known_hosts_path}']

        # Derive auth prefs. Prefer the resolved NativeAuth (single shared auth
        # decision); fall back to recomputing from the connection when not given.
        logger.debug("Main window: Determining authentication preferences")
        key_mode = 0
        keyfile = getattr(connection, 'keyfile', '') or ''
        logger.debug(f"Main window: Connection keyfile: '{keyfile}'")

        if auth is not None:
            prefer_password = bool(getattr(auth, 'password_mode', False))
            # Key-based + stored password (askpass delivers both; MFA via force).
            combined_auth = bool(getattr(auth, 'password', None)) and not prefer_password
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
                argv += ['-o', 'PreferredAuthentications=keyboard-interactive,password']
                if getattr(connection, 'pubkey_auth_no', False):
                    argv += ['-o', 'PubkeyAuthentication=no']
                    logger.debug(
                        "Main window: Added password authentication options - "
                        "PubkeyAuthentication=no, PreferredAuthentications="
                        "keyboard-interactive,password"
                    )
                else:
                    logger.debug(
                        "Main window: Added password authentication option - "
                        "PreferredAuthentications=keyboard-interactive,password"
                    )
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

