"""Welcome page widget for sshPilot."""

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk, Gdk
from gettext import gettext as _


from .connection_manager import Connection
from .search_utils import connection_matches


class WelcomePage(Gtk.Box):
    """Welcome page shown when no tabs are open."""

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        self.window = window
        self.connection_manager = window.connection_manager
        self.set_valign(Gtk.Align.START)
        self.set_halign(Gtk.Align.FILL)
        self.set_hexpand(True)
        self.set_margin_start(24)
        self.set_margin_end(24)
        self.set_margin_top(24)
        self.set_margin_bottom(24)


        # Welcome icon
        try:
            texture = Gdk.Texture.new_from_resource('/io/github/mfat/sshpilot/sshpilot.svg')
            icon = Gtk.Image.new_from_paintable(texture)
            icon.set_pixel_size(128)
        except Exception:
            icon = Gtk.Image.new_from_icon_name('network-workgroup-symbolic')
            icon.set_icon_size(Gtk.IconSize.LARGE)
            icon.set_pixel_size(128)
        icon.set_halign(Gtk.Align.CENTER)
        self.append(icon)

        # Quick connect box
        quick_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        quick_box.set_halign(Gtk.Align.FILL)
        quick_box.set_hexpand(True)
        self.quick_entry = Gtk.Entry()
        self.quick_entry.set_hexpand(True)

        self.quick_entry.set_placeholder_text(_('user@host'))
        self.quick_entry.connect('activate', self.on_quick_connect)
        connect_button = Gtk.Button(label=_('Connect'))
        connect_button.connect('clicked', self.on_quick_connect)
        quick_box.append(self.quick_entry)
        quick_box.append(connect_button)
        self.append(quick_box)

        # Search box and results
        search_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        search_container.set_halign(Gtk.Align.FILL)
        search_container.set_hexpand(True)
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_hexpand(True)

        self.search_entry.set_placeholder_text(_('Search connections'))
        self.search_entry.connect('activate', self.on_search_activate)
        self.search_entry.connect('search-changed', self.on_search_changed)
        search_container.append(self.search_entry)

        self.results_list = Gtk.ListBox()
        self.results_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.results_list.set_hexpand(True)

        self.results_list.connect('row-activated', self.on_result_activated)
        search_container.append(self.results_list)
        self.append(search_container)

        # Action buttons
        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons_box.set_halign(Gtk.Align.START)

        local_button = Gtk.Button(label=_('Local Terminal'))
        local_button.connect('clicked', lambda *_: window.terminal_manager.show_local_terminal())
        prefs_button = Gtk.Button(label=_('Preferences'))
        prefs_button.connect('clicked', lambda *_: window.show_preferences())
        buttons_box.append(local_button)
        buttons_box.append(prefs_button)
        self.append(buttons_box)

        # Shortcuts box
        shortcuts_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        shortcuts_container.set_halign(Gtk.Align.FILL)
        shortcuts_title = Gtk.Label(label=_('Keyboard Shortcuts'))
        shortcuts_title.set_halign(Gtk.Align.START)
        shortcuts_container.append(shortcuts_title)

        shortcuts_scroller = Gtk.ScrolledWindow()
        shortcuts_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        shortcuts_scroller.set_min_content_height(120)
        shortcuts_scroller.set_max_content_height(200)
        shortcuts_scroller.set_hexpand(True)
        shortcuts_scroller.set_vexpand(False)

        grid = Gtk.Grid(column_spacing=12, row_spacing=6)
        grid.set_halign(Gtk.Align.START)


        shortcuts = [
            ("<Primary>N", _('New Connection')),
            ("<Primary><Alt>N", _('Open Selected Host in a New Tab')),
            ("F9", _('Toggle Sidebar')),
            ("<Primary>L", _('Focus connection list to select server')),
            ("<Primary><Shift>K", _('Copy SSH Key to Server')),
            ("<Alt>Right", _('Next Tab')),
            ("<Alt>Left", _('Previous Tab')),
            ("<Primary>F4", _('Close Tab')),
            ("<Primary><Shift>T", _('New Local Terminal')),
            ("<Primary>plus", _('Zoom In')),
            ("<Primary>minus", _('Zoom Out')),
            ("<Primary>0", _('Reset Zoom')),
            ("<Primary>comma", _('Preferences')),
        ]

        for row, (shortcut, description) in enumerate(shortcuts):
            key_label = Gtk.ShortcutLabel.new(shortcut)
            key_label.set_halign(Gtk.Align.START)
            desc_label = Gtk.Label(label=description, xalign=0)
            grid.attach(key_label, 0, row, 1, 1)
            grid.attach(desc_label, 1, row, 1, 1)

        shortcuts_scroller.set_child(grid)
        shortcuts_container.append(shortcuts_scroller)
        self.append(shortcuts_container)


    # Quick connect handlers
    def on_quick_connect(self, *_args):
        text = self.quick_entry.get_text().strip()
        if not text:
            return
        username = ''
        host = text
        if '@' in text:
            username, host = text.split('@', 1)
        data = {"nickname": host, "host": host, "username": username}
        connection = Connection(data)
        self.window.terminal_manager.connect_to_host(connection, force_new=False)

    # Search handlers
    def _search_results(self, query: str):
        connections = self.connection_manager.get_connections()
        matches = [c for c in connections if connection_matches(c, query)]
        return matches

    def on_search_changed(self, entry):
        query = entry.get_text().strip().lower()
        for child in list(self.results_list):
            self.results_list.remove(child)
        if not query:
            return
        for conn in self._search_results(query):
            row = Gtk.ListBoxRow()
            row.connection = conn
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            label = Gtk.Label(label=f"{conn.nickname} ({conn.host})", xalign=0)
            box.append(label)
            row.set_child(box)
            self.results_list.append(row)
        self.results_list.show()

    def on_search_activate(self, entry):
        query = entry.get_text().strip().lower()
        matches = self._search_results(query)
        if matches:
            self.window.terminal_manager.connect_to_host(matches[0], force_new=False)

    def on_result_activated(self, listbox, row):
        if hasattr(row, 'connection'):
            self.window.terminal_manager.connect_to_host(row.connection, force_new=False)
