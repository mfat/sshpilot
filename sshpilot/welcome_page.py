"""Welcome page widget for sshPilot."""

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk, Gdk

from .shortcut_utils import get_primary_modifier_label


class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.set_valign(Gtk.Align.CENTER)
        self.set_halign(Gtk.Align.CENTER)
        self.set_margin_start(48)
        self.set_margin_end(48)
        self.set_margin_top(48)
        self.set_margin_bottom(48)

        # Welcome icon
        try:
            texture = Gdk.Texture.new_from_resource('/io/github/mfat/sshpilot/sshpilot.svg')
            icon = Gtk.Image.new_from_paintable(texture)
            icon.set_pixel_size(128)
        except Exception:
            icon = Gtk.Image.new_from_icon_name('network-workgroup-symbolic')
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(128)
        self.append(icon)

        # Welcome message
        message = Gtk.Label()
        message.set_text('Select a host from the list, double-click or press Enter to connect')
        message.set_halign(Gtk.Align.CENTER)
        message.add_css_class('dim-label')
        self.append(message)

        # Shortcuts box
        shortcuts_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_box.set_halign(Gtk.Align.CENTER)

        shortcuts_title = Gtk.Label()
        shortcuts_title.set_markup('<b>Keyboard Shortcuts</b>')
        shortcuts_box.append(shortcuts_title)

        primary = get_primary_modifier_label()
        shortcuts = [
            (f'{primary}+N', 'New Connection'),
            (f'{primary}+Alt+N', 'Open  Selected Host in a New Tab'),
            ('F9', 'Toggle Sidebar'),
            (f'{primary}+L', 'Focus connection list to select server'),
            (f'{primary}+Shift+K', 'Copy SSH Key to Server'),
            ('Alt+Right', 'Next Tab'),
            ('Alt+Left', 'Previous Tab'),
            (f'{primary}+F4', 'Close Tab'),
            (f'{primary}+Shift+T', 'New Local Terminal'),
            (f'{primary}+Shift+=', 'Zoom In'),
            (f'{primary}+-', 'Zoom Out'),
            (f'{primary}+0', 'Reset Zoom'),
            (f'{primary}+,', 'Preferences'),
        ]

        for shortcut, description in shortcuts:
            shortcut_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

            key_label = Gtk.Label()
            key_label.set_markup(f'<tt>{shortcut}</tt>')
            key_label.set_width_chars(15)
            key_label.set_halign(Gtk.Align.START)
            shortcut_box.append(key_label)

            desc_label = Gtk.Label()
            desc_label.set_text(description)
            desc_label.set_halign(Gtk.Align.START)
            shortcut_box.append(desc_label)

            shortcuts_box.append(shortcut_box)

        self.append(shortcuts_box)
