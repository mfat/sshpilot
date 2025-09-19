from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Dict, List, Optional

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gdk, Gtk

logger = logging.getLogger(__name__)


ACTION_LABELS: Dict[str, str] = {
    'quit': _('Quit'),
    'new-connection': _('New Connection'),
    'open-new-connection-tab': _('Open New Connection Tab'),
    'toggle-list': _('Focus Connection List'),
    'search': _('Search Connections'),
    'new-key': _('Copy Key to Server'),
    'edit-ssh-config': _('SSH Config Editor'),
    'local-terminal': _('Local Terminal'),
    'preferences': _('Preferences'),
    'tab-close': _('Close Tab'),
    'broadcast-command': _('Broadcast Command'),
    'help': _('Documentation'),
    'shortcuts': _('Keyboard Shortcuts'),
    'tab-next': _('Next Tab'),
    'tab-prev': _('Previous Tab'),
    'tab-overview': _('Tab Overview'),
    'quick-connect': _('Quick Connect'),
}


def _get_action_label(name: str) -> str:
    label = ACTION_LABELS.get(name)
    if label:
        return label
    return name.replace('-', ' ').title()


class _ShortcutCaptureDialog(Adw.Window):
    """Modal dialog that captures a shortcut press."""

    def __init__(self, parent: Adw.Window, on_selected):
        super().__init__(transient_for=parent, modal=True)
        self.set_title(_('Assign Shortcut'))
        self.set_default_size(360, 160)
        self._on_selected = on_selected
        self._handled = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        title = Gtk.Label(label=_('Press the new shortcut'))
        title.add_css_class('title-3')
        title.set_halign(Gtk.Align.CENTER)

        subtitle = Gtk.Label(
            label=_('Press Esc to cancel. Shortcuts must include a key (modifiers optional).')
        )
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)

        box.append(title)
        box.append(subtitle)

        self.set_content(box)

        controller = Gtk.EventControllerKey()
        controller.connect('key-pressed', self._on_key_pressed)
        self.add_controller(controller)

    def _on_key_pressed(self, _controller, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        if self._handled:
            return True

        if keyval == Gdk.KEY_Escape:
            self.close()
            return True

        # Mask modifiers to GTK's default accelerator mask
        modifiers = state & Gtk.accelerator_get_default_mod_mask()

        if not Gtk.accelerator_valid(keyval, modifiers):
            return True

        accelerator = Gtk.accelerator_name_with_keycode(None, keyval, keycode, modifiers)
        if not accelerator:
            accelerator = Gtk.accelerator_name(keyval, modifiers)

        if accelerator:
            self._handled = True
            try:
                self._on_selected(accelerator)
            finally:
                self.close()
        return True


class ShortcutEditorWindow(Adw.Window):
    """Window that allows editing of application keyboard shortcuts."""

    def __init__(self, parent_window):
        super().__init__(transient_for=parent_window, modal=True)
        self.set_title(_('Shortcut Editor'))
        self.set_default_size(600, 600)

        self._parent_window = parent_window
        self._app = parent_window.get_application()
        self._config = getattr(self._app, 'config', None)
        self._rows: Dict[str, Dict[str, Gtk.Widget]] = {}
        self._pending_overrides: Dict[str, List[str]] = {}
        self._default_shortcuts: Dict[str, Optional[List[str]]] = {}

        if self._config is not None:
            try:
                stored = self._config.get_shortcut_overrides()
                if isinstance(stored, dict):
                    self._pending_overrides = {
                        name: value for name, value in stored.items() if isinstance(value, list)
                    }
            except Exception as exc:
                logger.error('Failed to load shortcut overrides: %s', exc)

        try:
            defaults = self._app.get_registered_shortcut_defaults()
            if isinstance(defaults, dict):
                self._default_shortcuts = defaults
        except Exception as exc:
            logger.error('Failed to load default shortcuts: %s', exc)

        self._action_names = self._collect_actions()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle.new(_('Shortcut Editor'), _('Customize keyboard shortcuts')))
        toolbar_view.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self._build_list_box())

        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

        self.connect('close-request', self._on_close_request)

    def _collect_actions(self) -> List[str]:
        names: List[str] = []
        try:
            order = self._app.get_registered_action_order()
        except Exception:
            order = []

        for name in order:
            default = self._default_shortcuts.get(name)
            override = self._pending_overrides.get(name)
            if default is None and override is None:
                continue
            names.append(name)
        return names

    def _build_list_box(self) -> Gtk.Widget:
        # Create preferences page for better organization
        preferences_page = Adw.PreferencesPage()
        
        # Group shortcuts by category for better UX
        general_group = Adw.PreferencesGroup()
        general_group.set_title(_('General'))
        
        connection_group = Adw.PreferencesGroup()
        connection_group.set_title(_('Connection Management'))
        
        terminal_group = Adw.PreferencesGroup()
        terminal_group.set_title(_('Terminal'))
        
        tab_group = Adw.PreferencesGroup()
        tab_group.set_title(_('Tab Management'))

        # Categorize actions
        general_actions = ['quit', 'preferences', 'help', 'shortcuts']
        connection_actions = ['new-connection', 'open-new-connection-tab', 'toggle-list', 'search', 
                            'new-key', 'edit-ssh-config', 'quick-connect']
        terminal_actions = ['local-terminal', 'broadcast-command']
        tab_actions = ['tab-next', 'tab-prev', 'tab-close', 'tab-overview']

        for name in self._action_names:
            # Create ActionRow with switch for enable/disable
            row = Adw.ActionRow()
            row.set_title(_get_action_label(name))
            
            # Get current shortcuts for subtitle
            current_shortcuts = self._get_effective_shortcuts(name)
            if current_shortcuts is None or len(current_shortcuts) == 0:
                subtitle = _('No shortcut assigned')
                is_enabled = False
            else:
                subtitle = self._format_accelerators(current_shortcuts)
                is_enabled = True
            
            row.set_subtitle(subtitle)

            # Add enable/disable switch
            enable_switch = Gtk.Switch()
            enable_switch.set_active(is_enabled)
            enable_switch.set_valign(Gtk.Align.CENTER)
            enable_switch.connect('notify::active', self._on_switch_toggled, name)
            row.add_suffix(enable_switch)

            # Add assign button
            assign_button = Gtk.Button()
            assign_button.set_icon_name('input-keyboard-symbolic')
            assign_button.set_tooltip_text(_('Assign new shortcut'))
            assign_button.add_css_class('flat')
            assign_button.set_valign(Gtk.Align.CENTER)
            assign_button.connect('clicked', self._on_assign_clicked, name)
            row.add_suffix(assign_button)

            # Add reset button (only show if different from default)
            default_shortcuts = self._default_shortcuts.get(name)
            if current_shortcuts != default_shortcuts:
                reset_button = Gtk.Button()
                reset_button.set_icon_name('edit-undo-symbolic')
                reset_button.set_tooltip_text(_('Reset to default'))
                reset_button.add_css_class('flat')
                reset_button.set_valign(Gtk.Align.CENTER)
                reset_button.connect('clicked', self._on_reset_clicked, name)
                row.add_suffix(reset_button)

            # Store references for updates
            self._rows[name] = {
                'row': row,
                'switch': enable_switch,
                'assign_button': assign_button
            }

            # Add to appropriate group
            if name in general_actions:
                general_group.add(row)
            elif name in connection_actions:
                connection_group.add(row)
            elif name in terminal_actions:
                terminal_group.add(row)
            elif name in tab_actions:
                tab_group.add(row)
            else:
                # Fallback to general group
                general_group.add(row)

        # Add groups to preferences page
        preferences_page.add(general_group)
        preferences_page.add(connection_group)
        preferences_page.add(terminal_group)
        preferences_page.add(tab_group)

        return preferences_page


    def _format_accelerators(self, accelerators: Optional[List[str]]) -> str:
        if accelerators is None:
            return _('None')
        if len(accelerators) == 0:
            return _('Disabled')

        labels: List[str] = []
        for accel in accelerators:
            success, keyval, modifiers = Gtk.accelerator_parse(accel)
            if not success or (keyval == 0 and modifiers == 0):
                labels.append(accel)
            else:
                labels.append(Gtk.accelerator_get_label(keyval, modifiers))
        return ', '.join(labels) if labels else _('None')

    def _get_effective_shortcuts(self, action_name: str) -> Optional[List[str]]:
        if action_name in self._pending_overrides:
            return self._pending_overrides[action_name]
        return self._default_shortcuts.get(action_name)

    def _on_switch_toggled(self, switch: Gtk.Switch, _pspec, action_name: str):
        """Handle enable/disable switch toggle"""
        if switch.get_active():
            # Switch turned on - restore default or prompt for new shortcut
            default = self._default_shortcuts.get(action_name)
            if default:
                self._attempt_set_override(action_name, default)
            else:
                # No default, prompt for assignment
                self._on_assign_clicked(None, action_name)
        else:
            # Switch turned off - disable shortcut
            self._attempt_set_override(action_name, [])

    def _on_assign_clicked(self, _button, action_name: str):
        dialog = _ShortcutCaptureDialog(self, lambda accel: self._attempt_set_override(action_name, [accel]))
        dialog.present()

    def _on_reset_clicked(self, _button, action_name: str):
        self._attempt_set_override(action_name, None)

    def _attempt_set_override(self, action_name: str, accelerators: Optional[List[str]]):
        if accelerators and len(accelerators) > 1:
            accelerators = accelerators[:1]

        if accelerators and len(accelerators) == 1:
            conflict = self._find_conflict(action_name, accelerators[0])
            if conflict:
                self._show_conflict_dialog(conflict)
                return

        default = self._default_shortcuts.get(action_name)
        normalized: Optional[List[str]]
        if accelerators is None:
            normalized = None
        else:
            normalized = list(accelerators)
            if default is not None and normalized == default:
                normalized = None

        if normalized is None:
            self._pending_overrides.pop(action_name, None)
        else:
            self._pending_overrides[action_name] = normalized

        if self._config is not None:
            try:
                self._config.set_shortcut_override(action_name, normalized)
            except Exception as exc:
                logger.error('Failed to persist shortcut override for %s: %s', action_name, exc)

        self._update_row_display(action_name)
        self._apply_shortcuts()

    def _update_row_display(self, action_name: str):
        row_data = self._rows.get(action_name)
        if row_data is not None:
            row = row_data['row']
            switch = row_data['switch']
            
            # Update subtitle and switch state
            current_shortcuts = self._get_effective_shortcuts(action_name)
            if current_shortcuts is None or len(current_shortcuts) == 0:
                subtitle = _('No shortcut assigned')
                is_enabled = False
            else:
                subtitle = self._format_accelerators(current_shortcuts)
                is_enabled = True
            
            row.set_subtitle(subtitle)
            
            # Update switch without triggering the callback
            switch.handler_block_by_func(self._on_switch_toggled)
            switch.set_active(is_enabled)
            switch.handler_unblock_by_func(self._on_switch_toggled)

    def _find_conflict(self, action_name: str, accelerator: str) -> Optional[str]:
        for other in self._action_names:
            if other == action_name:
                continue
            current = self._get_effective_shortcuts(other)
            if not current:
                continue
            if accelerator in current:
                return other
        return None

    def _show_conflict_dialog(self, conflict_action: str):
        dialog = Adw.MessageDialog(
            transient_for=self,
            modal=True,
            heading=_('Shortcut Already In Use'),
            body=_('The selected shortcut is already assigned to “{action}”.').format(
                action=_get_action_label(conflict_action)
            ),
        )
        dialog.add_response('ok', _('OK'))
        dialog.connect('response', lambda d, _r: d.close())
        dialog.present()

    def _apply_shortcuts(self):
        try:
            self._app.apply_shortcut_overrides()
        except Exception as exc:
            logger.error('Failed to reapply shortcuts: %s', exc)

    def _on_close_request(self, *_args):
        self._apply_shortcuts()
        return False


__all__ = ['ShortcutEditorWindow']
