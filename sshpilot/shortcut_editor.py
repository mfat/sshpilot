from __future__ import annotations

import logging
from gettext import gettext as _
from typing import Dict, Iterable, List, Optional

import gi
gi.require_version('Adw', '1')
gi.require_version('Gtk', '4.0')
from gi.repository import Adw, Gdk, Gtk

logger = logging.getLogger(__name__)


_GtkBoxBase = getattr(Gtk, 'Box', object)

try:
    PreferencesPageBase = Adw.PreferencesPage  # type: ignore[attr-defined]
except AttributeError:

    class PreferencesPageBase(_GtkBoxBase):  # type: ignore[misc]
        """Fallback widget used in test environments without libadwaita."""

        def __init__(self, *args, **kwargs):
            try:
                super().__init__(*args, **kwargs)  # type: ignore[misc]
            except Exception:
                pass
            self._fallback_children: List[object] = []

        def add(self, child):  # type: ignore[override]
            self._fallback_children.append(child)

        def append(self, child):  # type: ignore[override]
            self._fallback_children.append(child)

        def add_css_class(self, *_args):  # type: ignore[override]
            return None

        def set_title(self, *_args):  # type: ignore[override]
            return None

        def set_icon_name(self, *_args):  # type: ignore[override]
            return None


ACTION_LABELS: Dict[str, str] = {
    'quit': _('Quit'),
    'new-connection': _('New Connection'),
    'open-new-connection-tab': _('Open New Connection Tab'),
    'toggle-list': _('Focus Connection List'),
    'search': _('Search Connections'),
    'new-key': _('Copy Key to Server'),
    'manage-files': _('Manage Files'),
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

PASS_THROUGH_NOTICE = _('Shortcuts disabled while terminal pass-through mode is active')


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


class ShortcutsPreferencesPage(PreferencesPageBase):
    """Preferences page that provides shortcut editing widgets."""

    def __init__(
        self,
        parent_widget: Optional[Gtk.Widget],
        app=None,
        config=None,
        owner_window: Optional[Gtk.Widget] = None,
    ):
        super().__init__()

        self._transient_parent = parent_widget
        self._owner_window = owner_window or parent_widget
        self._app = app
        if self._app is None and parent_widget is not None and hasattr(parent_widget, 'get_application'):
            try:
                self._app = parent_widget.get_application()
                logger.debug(f"Got application from parent widget: {self._app}")
            except Exception as e:
                logger.debug(f"Failed to get application from parent widget: {e}")
                self._app = None
        self._config = config or getattr(self._app, 'config', None)
        logger.debug(f"Shortcut editor initialized with app: {self._app}, config: {self._config}")

        self._rows: Dict[str, Dict[str, Gtk.Widget]] = {}
        self._pending_overrides: Dict[str, List[str]] = {}
        self._default_shortcuts: Dict[str, Optional[List[str]]] = {}
        self._shortcuts_container: Optional[Gtk.Box] = None
        self._editor_root: Optional[Gtk.Box] = None
        if self._config is not None:
            try:
                self._pass_through_enabled = bool(
                    self._config.get_setting('terminal.pass_through_mode', False)
                )
            except Exception:
                self._pass_through_enabled = False
        else:
            self._pass_through_enabled = False

        if self._config is not None:
            try:
                stored = self._config.get_shortcut_overrides()
                if isinstance(stored, dict):
                    self._pending_overrides = {
                        name: value for name, value in stored.items() if isinstance(value, list)
                    }
            except Exception as exc:
                logger.error('Failed to load shortcut overrides: %s', exc)

        if self._app is not None:
            try:
                defaults = self._app.get_registered_shortcut_defaults()
                if isinstance(defaults, dict):
                    self._default_shortcuts = defaults
            except Exception as exc:
                logger.error('Failed to load default shortcuts: %s', exc)

        self._action_names = self._collect_actions()
        logger.debug(f"Shortcut editor collected {len(self._action_names)} actions: {self._action_names}")
        self._groups_list: List[Adw.PreferencesGroup] = []

        self._pass_through_notice = self._create_pass_through_notice_widget()
        self._shortcuts_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._shortcuts_container.set_margin_top(12)
        self._shortcuts_container.set_margin_bottom(12)
        self._shortcuts_container.set_margin_start(12)
        self._shortcuts_container.set_margin_end(12)

        if hasattr(self, 'add_css_class'):
            try:
                self.add_css_class('shortcut-editor-page')
            except Exception:
                pass

        self._build_groups()
        logger.debug(f"Shortcut editor built {len(self._groups_list)} groups")

    def _collect_actions(self) -> List[str]:
        names: List[str] = []
        order: Iterable[str] = []
        if self._app is not None:
            try:
                order = self._app.get_registered_action_order()
                logger.debug(f"Got action order from app: {list(order)}")
            except Exception as e:
                logger.debug(f"Failed to get action order from app: {e}")
                order = []

        logger.debug(f"Default shortcuts: {self._default_shortcuts}")
        logger.debug(f"Pending overrides: {self._pending_overrides}")

        for name in order:
            default = self._default_shortcuts.get(name)
            override = self._pending_overrides.get(name)
            if default is None and override is None:
                continue
            names.append(name)
            logger.debug(f"Added action to names: {name} (default: {default}, override: {override})")
        
        logger.debug(f"Final action names: {names}")
        return names

    def _build_groups(self):
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
        connection_actions = [
            'new-connection',
            'open-new-connection-tab',
            'toggle-list',
            'search',
            'new-key',
            'manage-files',
            'edit-ssh-config',
            'quick-connect',
        ]
        terminal_actions = ['local-terminal', 'broadcast-command']
        tab_actions = ['tab-next', 'tab-prev', 'tab-close', 'tab-overview']

        for name in self._action_names:
            row = Adw.ActionRow()
            row.set_title(_get_action_label(name))

            current_shortcuts = self._get_effective_shortcuts(name)
            if current_shortcuts is None or len(current_shortcuts) == 0:
                subtitle = _('No shortcut assigned')
                is_enabled = False
            else:
                subtitle = self._format_accelerators(current_shortcuts)
                is_enabled = True

            enable_switch = Gtk.Switch()
            enable_switch.set_active(is_enabled)
            enable_switch.set_valign(Gtk.Align.CENTER)
            enable_switch.connect('notify::active', self._on_switch_toggled, name)
            row.add_suffix(enable_switch)

            assign_button = Gtk.Button()
            assign_button.set_icon_name('input-keyboard-symbolic')
            assign_button.set_tooltip_text(_('Assign new shortcut'))
            assign_button.add_css_class('flat')
            assign_button.set_valign(Gtk.Align.CENTER)
            assign_button.connect('clicked', self._on_assign_clicked, name)
            row.add_suffix(assign_button)

            default_shortcuts = self._default_shortcuts.get(name)
            reset_button: Optional[Gtk.Widget] = None
            if current_shortcuts != default_shortcuts:
                reset_button = Gtk.Button()
                reset_button.set_icon_name('edit-undo-symbolic')
                reset_button.set_tooltip_text(_('Reset to default'))
                reset_button.add_css_class('flat')
                reset_button.set_valign(Gtk.Align.CENTER)
                reset_button.connect('clicked', self._on_reset_clicked, name)
                row.add_suffix(reset_button)

            self._rows[name] = {
                'row': row,
                'switch': enable_switch,
                'assign_button': assign_button,
                'reset_button': reset_button,
                'base_subtitle': subtitle,
            }

            row.set_subtitle(subtitle)

            if name in general_actions:
                general_group.add(row)
                logger.debug(f"Added {name} to General group")
            elif name in connection_actions:
                connection_group.add(row)
                logger.debug(f"Added {name} to Connection Management group")
            elif name in terminal_actions:
                terminal_group.add(row)
                logger.debug(f"Added {name} to Terminal group")
            elif name in tab_actions:
                tab_group.add(row)
                logger.debug(f"Added {name} to Tab Management group")
            else:
                general_group.add(row)
                logger.debug(f"Added {name} to General group (fallback)")

        for group in (general_group, connection_group, terminal_group, tab_group):
            try:
                group.add_css_class('boxed-list')
            except Exception:
                pass
            self._groups_list.append(group)
            if self._shortcuts_container is not None:
                try:
                    self._shortcuts_container.append(group)
                except AttributeError:
                    self._shortcuts_container.add(group)
            # Don't add to self - the groups will be added to the preferences page separately
            logger.debug(f"Prepared group '{group.get_title()}' with {len(list(group))} children")

        self.set_pass_through_enabled(self._pass_through_enabled)

    def _create_pass_through_notice_widget(self) -> Gtk.Widget:
        message = PASS_THROUGH_NOTICE
        if hasattr(Adw, 'Banner'):
            try:
                banner = Adw.Banner.new(message)
                banner.set_revealed(False)
                banner.add_css_class('warning')
                banner.set_margin_top(12)
                banner.set_margin_start(12)
                banner.set_margin_end(12)
                return banner
            except Exception:
                pass

        label = Gtk.Label(label=message)
        label.set_wrap(True)
        label.set_visible(False)
        label.add_css_class('warning')
        label.set_margin_top(12)
        label.set_margin_bottom(0)
        label.set_margin_start(12)
        label.set_margin_end(12)
        return label

    def get_pass_through_notice_widget(self) -> Gtk.Widget:
        return self._pass_through_notice

    def get_shortcuts_container(self) -> Gtk.Widget:
        if self._shortcuts_container is None:
            self._shortcuts_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        return self._shortcuts_container

    def create_editor_widget(self) -> Gtk.Widget:
        if self._editor_root is None:
            container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            container.set_margin_bottom(12)
            notice_widget = self.get_pass_through_notice_widget()
            shortcuts_container = self.get_shortcuts_container()
            for widget in (notice_widget, shortcuts_container):
                parent = widget.get_parent()
                if parent is not None:
                    remove_method = getattr(parent, 'remove', None)
                    if callable(remove_method):
                        remove_method(widget)
            container.append(notice_widget)
            container.append(shortcuts_container)
            self._editor_root = container
        else:
            parent = self._editor_root.get_parent()
            if parent is not None:
                remove_method = getattr(parent, 'remove', None)
                if callable(remove_method):
                    remove_method(self._editor_root)
        return self._editor_root

    def iter_groups(self) -> Iterable[Adw.PreferencesGroup]:
        """Yield the preference groups managed by this page."""

        yield from self._groups_list

    def _add_group_widget(self, group: Adw.PreferencesGroup):
        add_method = getattr(self, 'add', None)
        if callable(add_method):
            add_method(group)
            return

        append_method = getattr(self, 'append', None)
        if callable(append_method):
            append_method(group)
            return

        raise AttributeError('Unable to attach preferences group to shortcuts page')

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
        if switch.get_active():
            default = self._default_shortcuts.get(action_name)
            if default:
                self._attempt_set_override(action_name, default)
            else:
                self._on_assign_clicked(None, action_name)
        else:
            self._attempt_set_override(action_name, [])

    def _on_assign_clicked(self, _button, action_name: str):
        dialog_parent = self._transient_parent or self.get_root()
        dialog = _ShortcutCaptureDialog(
            dialog_parent,
            lambda accel: self._attempt_set_override(action_name, [accel]),
        )
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

            current_shortcuts = self._get_effective_shortcuts(action_name)
            if current_shortcuts is None or len(current_shortcuts) == 0:
                subtitle = _('No shortcut assigned')
                is_enabled = False
            else:
                subtitle = self._format_accelerators(current_shortcuts)
                is_enabled = True

            row_data['base_subtitle'] = subtitle
            row.set_subtitle(subtitle)

            switch.handler_block_by_func(self._on_switch_toggled)
            switch.set_active(is_enabled)
            switch.handler_unblock_by_func(self._on_switch_toggled)

            self._apply_pass_through_state_to_row(action_name)

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
        dialog_parent = self._transient_parent or self.get_root()
        dialog = Adw.MessageDialog(
            transient_for=dialog_parent,
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
        if self._app is None:
            return

        try:
            self._app.apply_shortcut_overrides()
            owner = self._owner_window
            if owner is not None and hasattr(owner, '_shortcuts_window'):
                owner._shortcuts_window = None
        except Exception as exc:
            logger.error('Failed to reapply shortcuts: %s', exc)

    def flush_changes(self):
        """Flush pending overrides to the application."""

        self._apply_shortcuts()


    def set_pass_through_enabled(self, enabled: bool):
        self._pass_through_enabled = bool(enabled)
        notice_widget = getattr(self, '_pass_through_notice', None)
        if notice_widget is not None:
            if hasattr(notice_widget, 'set_revealed'):
                try:
                    notice_widget.set_revealed(self._pass_through_enabled)
                except Exception:
                    notice_widget.set_visible(self._pass_through_enabled)
            else:
                notice_widget.set_visible(self._pass_through_enabled)

        if self._shortcuts_container is not None:
            parent = None
            try:
                parent = self._shortcuts_container.get_parent()
            except Exception:
                parent = None
            if parent is not None:
                self._shortcuts_container.set_sensitive(not self._pass_through_enabled)

        for name in self._rows:
            self._apply_pass_through_state_to_row(name)

    def _apply_pass_through_state_to_row(self, action_name: str):
        row_data = self._rows.get(action_name)
        if not row_data:
            return

        row = row_data['row']
        try:
            row.set_sensitive(not self._pass_through_enabled)
        except Exception:
            logger.debug('Failed to update sensitivity for row %s', action_name)
        base = row_data.get('base_subtitle', row.get_subtitle() or '')
        if row.get_subtitle() != base:
            row.set_subtitle(base)

        notice = PASS_THROUGH_NOTICE

        for key in ('switch', 'assign_button', 'reset_button'):
            widget = row_data.get(key)
            if widget is not None:
                try:
                    widget.set_sensitive(not self._pass_through_enabled)
                except Exception:
                    logger.debug('Failed to update sensitivity for %s control', key)

        row.set_tooltip_text(notice if self._pass_through_enabled else None)


class ShortcutEditorWindow(Adw.Window):
    """Window that allows editing of application keyboard shortcuts."""

    def __init__(self, parent_window):
        super().__init__(transient_for=parent_window, modal=True)
        self.set_title(_('Shortcut Editor'))
        self.set_default_size(600, 600)

        self._parent_window = parent_window
        self._app = parent_window.get_application()
        self._config = getattr(self._app, 'config', None)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle.new(_('Shortcut Editor'), _('Customize keyboard shortcuts'))
        )
        pass_through_active = False
        if self._config is not None:
            try:
                pass_through_active = bool(
                    self._config.get_setting('terminal.pass_through_mode', False)
                )
            except Exception:
                pass

        toggle_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        toggle_label = Gtk.Label(label=_('Pass-through'))
        toggle_label.set_halign(Gtk.Align.END)
        toggle_label.set_valign(Gtk.Align.CENTER)
        self._pass_through_switch = Gtk.Switch()
        self._pass_through_switch.set_active(pass_through_active)
        self._pass_through_switch.set_valign(Gtk.Align.CENTER)
        self._pass_through_switch.connect('notify::active', self._on_pass_through_switch_toggled)
        toggle_box.append(toggle_label)
        toggle_box.append(self._pass_through_switch)
        try:
            header.pack_end(toggle_box)
        except AttributeError:
            header.add_suffix(toggle_box)
        toolbar_view.add_top_bar(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._preferences_page = ShortcutsPreferencesPage(
            parent_widget=self,
            app=self._app,
            config=self._config,
            owner_window=self._parent_window,
        )
        editor_widget = self._preferences_page.create_editor_widget()
        scrolled.set_child(editor_widget)

        toolbar_view.set_content(scrolled)
        self.set_content(toolbar_view)

        self._preferences_page.set_pass_through_enabled(pass_through_active)

        self.connect('close-request', self._on_close_request)

    def _on_pass_through_switch_toggled(self, switch: Gtk.Switch, _pspec):
        active = bool(switch.get_active())
        self._preferences_page.set_pass_through_enabled(active)
        if self._config is not None:
            try:
                self._config.set_setting('terminal.pass_through_mode', active)
            except Exception as exc:
                logger.error('Failed to persist pass-through mode: %s', exc)

    def _on_close_request(self, *_args):
        self._preferences_page.flush_changes()
        return False


__all__ = ['ShortcutEditorWindow', 'ShortcutsPreferencesPage']
