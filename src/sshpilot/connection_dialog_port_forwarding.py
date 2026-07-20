"""Port-forwarding rule management for ConnectionDialog.

Extracted verbatim from connection_dialog.py as a mixin to shrink that
god-object. ConnectionDialog inherits this mixin, so ``self`` stays identical and
the move is a pure cut-and-paste with no logic changes. These methods build and
manage the port-forwarding rule list and its editor/info windows (load/add/edit/
delete rules, the rule editor, the per-type defaults, and the listening-ports
info dialog). They never open an SSH connection; ``get_port_checker`` only
inspects local listening ports.
"""

import logging

try:
    from gi.repository import Gtk, Adw
except (ImportError, AttributeError):  # pragma: no cover - used in tests without GTK
    class _DummyGIMeta(type):
        def __getattr__(cls, name):
            value = _DummyGIMeta(name, (object,), {})
            setattr(cls, name, value)
            return value

        def __call__(cls, *args, **kwargs):
            return object()

    class Gtk(metaclass=_DummyGIMeta):
        pass

    class Adw(metaclass=_DummyGIMeta):
        pass

from gettext import gettext as _

from .port_utils import get_port_checker

logger = logging.getLogger(__name__)


class ConnectionDialogPortForwardingMixin:
    def load_port_forwarding_rules(self):
        """Load port forwarding rules from the connection and update UI"""
        if not hasattr(self, 'rules_list') or not hasattr(self, 'forwarding_rules'):
            return

        # Clear existing rules UI
        while self.rules_list.get_first_child():
            self.rules_list.remove(self.rules_list.get_first_child())

        # Show placeholder if no rules
        if not self.forwarding_rules:
            self.rules_list.append(self.placeholder)
            return

        # Hide placeholder since we have rules
        if self.placeholder.get_parent():
            self.placeholder.unparent()

        # Process each forwarding rule
        for rule in self.forwarding_rules:
            if not rule.get('enabled', True):
                continue

            rule_type = rule.get('type', '')

            # Create a row for the rule
            row = Adw.ActionRow()
            row.set_selectable(False)

            # Set appropriate icon and title based on rule type
            from sshpilot import icon_utils
            if rule_type == 'local':
                row.set_title(_("Local Port Forwarding"))
                row.add_prefix(icon_utils.new_image_from_icon_name("network-transmit-receive-symbolic"))
                description = _("Local {local_port} → {remote_host}:{remote_port}").format(
                    local_port=rule.get('listen_port', ''),
                    remote_host=rule.get('remote_host', ''),
                    remote_port=rule.get('remote_port', '')
                )
            elif rule_type == 'remote':
                row.set_title(_("Remote Port Forwarding"))
                row.add_prefix(icon_utils.new_image_from_icon_name("network-receive-symbolic"))
                listen_addr = rule.get('listen_addr') or ''
                listen_port = rule.get('listen_port', '')
                remote_src = f"{listen_addr}:{listen_port}" if listen_addr else f"{listen_port}"
                dest_host = rule.get('local_host') or rule.get('remote_host') or ''
                dest_port = rule.get('local_port') or rule.get('remote_port') or ''
                if rule.get('socks') or not (dest_host or dest_port):
                    description = _("Remote {src} → SOCKS").format(src=remote_src)
                else:
                    description = _("Remote {src} → {dest_host}:{dest_port}").format(
                        src=remote_src, dest_host=dest_host, dest_port=dest_port
                    )
            elif rule_type == 'dynamic':
                row.set_title(_("Dynamic Port Forwarding (SOCKS)"))
                row.add_prefix(icon_utils.new_image_from_icon_name("network-workgroup-symbolic"))
                description = _("SOCKS proxy on port {port}").format(
                    port=rule.get('listen_port', '')
                )
            else:
                continue

            # Add description
            row.set_subtitle(description)

            # Add delete button
            delete_button = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat", "error"]
            )
            delete_button.connect("clicked", self.on_delete_forwarding_rule_clicked, rule)
            row.add_suffix(delete_button)

            # Add edit button
            edit_button = Gtk.Button(
                icon_name="document-edit-symbolic",
                valign=Gtk.Align.CENTER,
                css_classes=["flat"]
            )
            edit_button.connect("clicked", self.on_edit_forwarding_rule_clicked, rule)
            row.add_suffix(edit_button)

            # Add the row to the list
            self.rules_list.append(row)

        # Show the rules list
        self.rules_list.show()

    def on_delete_forwarding_rule_clicked(self, button, rule):
        """Handle delete port forwarding rule button click"""
        if not hasattr(self, 'forwarding_rules'):
            return

        # Remove the rule from the list
        self.forwarding_rules = [r for r in self.forwarding_rules if r != rule]

        # Reload the rules UI
        self.load_port_forwarding_rules()

        logger.info(f"Deleted port forwarding rule: {rule}")

    def on_edit_forwarding_rule_clicked(self, button, rule):
        """Handle edit port forwarding rule button click"""
        logger.info(f"Edit port forwarding rule clicked: {rule}")
        self._open_rule_editor(existing_rule=rule)

    def on_add_forwarding_rule_clicked(self, button):
        """Handle add port forwarding rule button click"""
        logger.info("Add port forwarding rule clicked")
        self._open_rule_editor(existing_rule=None)

    def on_view_port_info_clicked(self, button):
        """Handle view port info button click"""
        self._show_port_info_dialog()

    def _open_rule_editor(self, existing_rule=None):
        """Open an Adw.Window to add/edit a forwarding rule."""
        # Create Adw.Window
        dialog = Adw.Window()
        dialog.set_title(_("Port Forwarding Rule Editor"))
        dialog.set_default_size(500, -1)  # 500px width, auto height
        dialog.set_modal(True)
        dialog.set_transient_for(self)

        # Create content box
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

        # Type selector
        type_model = Gtk.StringList()
        type_model.append(_("Local"))
        type_model.append(_("Remote"))
        type_model.append(_("Dynamic"))
        type_row = Adw.ComboRow()
        type_row.set_title(_("Type"))
        type_row.set_model(type_model)

        listen_addr_row = Adw.EntryRow(title=_("Bind address (optional)"))
        listen_port_row = Adw.EntryRow()
        listen_port_row.set_title(_("Local port"))
        try:
            lpe2 = listen_port_row.get_child()
            if lpe2 and hasattr(lpe2, 'set_input_purpose'):
                lpe2.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if lpe2 and hasattr(lpe2, 'set_max_length'):
                lpe2.set_max_length(5)
        except Exception:
            pass

        remote_host_row = Adw.EntryRow(title=_("Host"))
        remote_port_row = Adw.EntryRow()
        remote_port_row.set_title(_("Port"))
        try:
            rpe2 = remote_port_row.get_child()
            if rpe2 and hasattr(rpe2, 'set_input_purpose'):
                rpe2.set_input_purpose(Gtk.InputPurpose.DIGITS)
            if rpe2 and hasattr(rpe2, 'set_max_length'):
                rpe2.set_max_length(5)
        except Exception:
            pass

        # Pack rows
        group = Adw.PreferencesGroup()
        group.add(type_row)
        group.add(listen_addr_row)
        group.add(listen_port_row)
        group.add(remote_host_row)
        group.add(remote_port_row)
        box.append(group)

        # Create header bar with buttons
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)

        # Add buttons to header bar
        cancel_button = Gtk.Button(label=_("Cancel"))
        cancel_button.add_css_class("flat")
        header_bar.pack_start(cancel_button)

        save_button = Gtk.Button(label=_("Save"))
        save_button.add_css_class("suggested-action")
        header_bar.pack_end(save_button)

        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(box)

        # Set content for Adw.Window
        dialog.set_content(main_box)

        # Populate when editing
        if existing_rule:
            t = existing_rule.get('type', 'local')
            type_row.set_selected({'local':0,'remote':1,'dynamic':2}.get(t,0))
            listen_addr_row.set_text(str(existing_rule.get('listen_addr', 'localhost')))
            try:
                listen_port_row.set_text(str(int(existing_rule.get('listen_port', 0) or 0)))
            except Exception:
                listen_port_row.set_text(str(existing_rule.get('listen_port', '')))
            if t == 'remote':
                # For remote rules, the destination is local_host/local_port.
                # A single-argument (SOCKS) rule has no destination — leave the
                # fields blank so it round-trips as SOCKS instead of being forced
                # into a localhost destination on save.
                if existing_rule.get('socks') or not (
                    existing_rule.get('local_host') or existing_rule.get('local_port')
                ):
                    remote_host_row.set_text('')
                    remote_port_row.set_text('')
                else:
                    remote_host_row.set_text(str(existing_rule.get('local_host', 'localhost')))
                    try:
                        remote_port_row.set_text(str(int(existing_rule.get('local_port', 0) or 0)))
                    except Exception:
                        remote_port_row.set_text(str(existing_rule.get('local_port', '')))
            else:
                remote_host_row.set_text(str(existing_rule.get('remote_host', 'localhost')))
                try:
                    remote_port_row.set_text(str(int(existing_rule.get('remote_port', 0) or 0)))
                except Exception:
                    remote_port_row.set_text(str(existing_rule.get('remote_port', '')))
        else:
            type_row.set_selected(0)
            # Sane defaults for a new rule
            listen_addr_row.set_text('localhost')
            listen_port_row.set_text('8080')
            remote_host_row.set_text('localhost')
            remote_port_row.set_text('22')

        # Avoid shadowing translation function '_' by using a local alias
        t = _
        previous_type_idx = type_row.get_selected()
        def _sync_visibility(*args):
            nonlocal previous_type_idx
            idx = type_row.get_selected()
            # Apply label set per type
            if idx == 0:
                # Local
                listen_addr_row.set_visible(False)
                listen_port_row.set_title(t("Local Port"))
                remote_host_row.set_visible(True)
                remote_host_row.set_title(t("Target Host"))
                remote_port_row.set_visible(True)
                remote_port_row.set_title(t("Target Port"))
            elif idx == 1:
                # Remote
                listen_addr_row.set_visible(True)
                listen_addr_row.set_title(t("Remote host (optional)"))
                listen_port_row.set_title(t("Remote port"))
                remote_host_row.set_visible(True)
                remote_host_row.set_title(t("Destination host"))
                remote_port_row.set_visible(True)
                remote_port_row.set_title(t("Destination port"))
            else:
                # Dynamic
                listen_addr_row.set_visible(True)
                listen_addr_row.set_title(t("Bind address (optional)"))
                listen_port_row.set_title(t("Local port"))
                remote_host_row.set_visible(False)
                remote_port_row.set_visible(False)
            self._apply_rule_editor_defaults_for_type(
                idx,
                listen_addr_row,
                listen_port_row,
                remote_host_row,
                remote_port_row,
                previous_type_idx,
            )
            previous_type_idx = idx
        type_row.connect('notify::selected', _sync_visibility)
        _sync_visibility()

        # Handle button clicks
        def _on_cancel_clicked(button):
            dialog.destroy()

        def _on_save_clicked(button):
            self._save_rule_from_editor(existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row)
            dialog.destroy()

        cancel_button.connect('clicked', _on_cancel_clicked)
        save_button.connect('clicked', _on_save_clicked)

        # Show the window
        dialog.present()

    def _save_rule_from_editor(self, existing_rule, type_row, listen_addr_row, listen_port_row, remote_host_row, remote_port_row):
        idx = type_row.get_selected()
        rtype = 'local' if idx == 0 else ('remote' if idx == 1 else 'dynamic')
        listen_addr = listen_addr_row.get_text().strip()
        try:
            listen_port = int((listen_port_row.get_text() or '0').strip() or '0')
        except Exception:
            listen_port = 0
        if listen_port <= 0 or listen_port > 65535:
            self.show_error(_("Please enter a valid listen port (1–65535)"))
            return

        # Check for port conflicts (for local and dynamic forwarding)
        if rtype in ['local', 'dynamic']:
            try:
                port_checker = get_port_checker()
                conflicts = port_checker.get_port_conflicts([listen_port], listen_addr or 'localhost')

                if conflicts:
                    port, port_info = conflicts[0]
                    conflict_msg = _("Port {port} is already in use").format(port=port)
                    if port_info.process_name:
                        conflict_msg += _(" by {process} (PID: {pid})").format(
                            process=port_info.process_name,
                            pid=port_info.pid
                        )

                    # Suggest alternative port
                    alt_port = port_checker.find_available_port(listen_port, listen_addr or 'localhost')
                    if alt_port:
                        conflict_msg += _("\n\nSuggested alternative: port {alt_port}").format(alt_port=alt_port)

                    # Show error dialog with conflict information
                    self.show_error(conflict_msg)
                    return

            except Exception as e:
                logger.debug(f"Could not check port conflict for {listen_port}: {e}")
                # Continue without port checking if there's an error
        rule = {
            'type': rtype,
            'enabled': True,
            # RemoteForward binds on the REMOTE host: an empty bind address means
            # "let the remote/GatewayPorts decide" (loopback by default), so keep
            # it empty rather than pinning localhost. Local/dynamic bind locally
            # and keep defaulting to localhost.
            'listen_addr': listen_addr if rtype == 'remote' else (listen_addr or 'localhost'),
            'listen_port': listen_port,
        }
        if rtype == 'local':
            # LocalForward: [listen_addr:]listen_port remote_host:remote_port
            # The destination is mandatory and must be a valid port.
            try:
                remote_port = int((remote_port_row.get_text() or '0').strip() or '0')
            except Exception:
                remote_port = 0
            if remote_port <= 0 or remote_port > 65535:
                self.show_error(_("Please enter a valid destination port (1–65535)"))
                return
            rule['remote_host'] = remote_host_row.get_text().strip() or 'localhost'
            rule['remote_port'] = remote_port
        elif rtype == 'remote':
            # RemoteForward: [listen_addr:]listen_port [local_host:local_port]
            # With no destination it is a reverse SOCKS proxy (single-argument
            # form, ssh_config(5)). An empty destination is therefore valid and
            # means SOCKS rather than a malformed forward.
            dest_host = remote_host_row.get_text().strip()
            try:
                dest_port = int((remote_port_row.get_text() or '0').strip() or '0')
            except Exception:
                dest_port = 0
            if not dest_host and dest_port <= 0:
                rule['socks'] = True
            else:
                if dest_port <= 0 or dest_port > 65535:
                    self.show_error(_("Please enter a valid destination port (1–65535)"))
                    return
                rule['local_host'] = dest_host or 'localhost'
                rule['local_port'] = dest_port

        if not hasattr(self, 'forwarding_rules') or self.forwarding_rules is None:
            self.forwarding_rules = []

        if existing_rule and existing_rule in self.forwarding_rules:
            idx_existing = self.forwarding_rules.index(existing_rule)
            self.forwarding_rules[idx_existing] = rule
        else:
            self.forwarding_rules.append(rule)

        self.load_port_forwarding_rules()

    def _apply_rule_editor_defaults_for_type(
        self,
        idx,
        listen_addr_row,
        listen_port_row,
        remote_host_row,
        remote_port_row,
        previous_idx=None,
    ):
        """Apply defaults for rule editor fields based on selected forwarding type."""
        try:
            if idx == 0:  # Local
                if not listen_addr_row.get_text().strip():
                    listen_addr_row.set_text('localhost')
                try:
                    if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                        listen_port_row.set_text('8080')
                except Exception:
                    listen_port_row.set_text('8080')

                # When switching from Remote to Local, always reset the local
                # target host to localhost instead of carrying remote destination.
                if previous_idx == 1:
                    remote_host_row.set_text('localhost')
                elif not remote_host_row.get_text().strip():
                    remote_host_row.set_text('localhost')

                try:
                    if int((remote_port_row.get_text() or '0').strip() or '0') == 0:
                        remote_port_row.set_text('22')
                except Exception:
                    remote_port_row.set_text('22')
            elif idx == 1:  # Remote
                # The bind address is on the REMOTE host and is optional — keep it
                # empty by default. On a real switch into Remote, clear whatever a
                # previous type seeded so it starts blank; on the initial load of
                # an existing rule leave the populated value untouched.
                if previous_idx is not None and previous_idx != idx:
                    listen_addr_row.set_text('')
                try:
                    if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                        listen_port_row.set_text('8080')
                except Exception:
                    listen_port_row.set_text('8080')
                # Only seed a destination when the user actively switches into
                # Remote from another type; on the initial load we leave it as-is
                # so an existing single-argument (SOCKS) rule keeps its blank
                # destination instead of being silently turned into a forward.
                if previous_idx is not None and previous_idx != idx:
                    if not remote_host_row.get_text().strip():
                        remote_host_row.set_text('localhost')
                    try:
                        if int((remote_port_row.get_text() or '0').strip() or '0') == 0:
                            remote_port_row.set_text('22')
                    except Exception:
                        remote_port_row.set_text('22')
            else:  # Dynamic
                if not listen_addr_row.get_text().strip():
                    listen_addr_row.set_text('localhost')
                try:
                    if int((listen_port_row.get_text() or '0').strip() or '0') == 0:
                        listen_port_row.set_text('1080')
                except Exception:
                    listen_port_row.set_text('1080')
        except Exception:
            pass

    def _show_port_info_dialog(self):
        """Show a window with current port information"""
        # Create Adw.Window
        parent_win = self.get_transient_for() if hasattr(self, 'get_transient_for') else None
        dialog = Adw.Window()
        dialog.set_title(_("Port Information"))
        dialog.set_default_size(600, 400)
        dialog.set_modal(True)
        if parent_win:
            dialog.set_transient_for(parent_win)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12
        )

        # Header with refresh button
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_label = Gtk.Label()
        header_label.set_markup(f"<b>{_('Currently Listening Ports')}</b>")
        header_label.set_halign(Gtk.Align.START)
        header_label.set_hexpand(True)
        header_box.append(header_label)

        from sshpilot import icon_utils
        refresh_button = Gtk.Button()
        icon_utils.set_button_icon(refresh_button, "view-refresh-symbolic")
        refresh_button.set_tooltip_text(_("Refresh port information"))
        header_box.append(refresh_button)

        box.append(header_box)

        # Scrolled window for port list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        # Port list
        port_list = Gtk.ListBox()
        port_list.set_selection_mode(Gtk.SelectionMode.NONE)
        port_list.add_css_class("boxed-list")
        scrolled.set_child(port_list)

        box.append(scrolled)

        def refresh_port_info():
            """Refresh the port information display"""
            # Clear existing items
            while row := port_list.get_first_child():
                port_list.remove(row)

            try:
                port_checker = get_port_checker()
                ports = port_checker.get_listening_ports(refresh=True)

                if not ports:
                    # Show empty state
                    empty_row = Adw.ActionRow()
                    empty_row.set_title(_("No listening ports found"))
                    empty_row.set_subtitle(_("All ports appear to be available"))
                    port_list.append(empty_row)
                    return

                # Sort ports by port number
                ports.sort(key=lambda p: p.port)

                for port_info in ports:
                    row = Adw.ActionRow()

                    # Title: Port and protocol
                    title = f"{_('Port')} {port_info.port}/{port_info.protocol.upper()}"
                    if port_info.address != "0.0.0.0":
                        title += f" ({port_info.address})"
                    row.set_title(title)

                    # Subtitle: Process information
                    if port_info.process_name and port_info.pid:
                        subtitle = f"{port_info.process_name} (PID: {port_info.pid})"
                    elif port_info.process_name:
                        subtitle = port_info.process_name
                    elif port_info.pid:
                        subtitle = f"PID: {port_info.pid}"
                    else:
                        subtitle = _("Unknown process")

                    row.set_subtitle(subtitle)

                    # Add icon based on port type
                    from sshpilot import icon_utils
                    if port_info.port < 1024:
                        icon = icon_utils.new_image_from_icon_name("security-high-symbolic")
                        icon.set_tooltip_text(_("System port (requires root)"))
                    else:
                        icon = icon_utils.new_image_from_icon_name("network-transmit-receive-symbolic")

                    row.add_prefix(icon)
                    port_list.append(row)

            except Exception as e:
                logger.error(f"Error refreshing port info: {e}")
                error_row = Adw.ActionRow()
                error_row.set_title(_("Error loading port information"))
                error_row.set_subtitle(str(e))
                port_list.append(error_row)

        # Connect refresh button
        refresh_button.connect("clicked", lambda *_: refresh_port_info())

        # Create header bar with window controls
        header_bar = Adw.HeaderBar()
        header_bar.set_show_end_title_buttons(True)

        # Create main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header_bar)
        main_box.append(box)

        # Set content for Adw.Window
        dialog.set_content(main_box)

        # Load initial data
        refresh_port_info()

        # Ensure dialog closes when the window controller is activated
        dialog.connect("close-request", lambda *_: dialog.destroy())

        # Show the window
        dialog.present()

    def _autosave_forwarding_changes(self):
        """Disabled autosave to avoid log floods; saving occurs on dialog Save."""
        return
