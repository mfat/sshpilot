import os
import logging
from typing import Callable, Optional

from gettext import gettext as _
from gi.repository import Gtk, Adw, GLib


logger = logging.getLogger(__name__)


class KnownHostsEditorWindow(Adw.Window):
    """Simple window for viewing and removing entries from known_hosts."""

    def __init__(self, parent, connection_manager, on_saved: Optional[Callable] = None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(700, 500)
        self.set_title(_("Known Hosts Editor"))

        self._cm = connection_manager
        self._on_saved = on_saved
        self._known_hosts_path = getattr(
            connection_manager,
            'known_hosts_path',
            os.path.expanduser('~/.ssh/known_hosts'),
        )
        self._all_entries = []  # Store all entries for filtering

        tv = Adw.ToolbarView()
        self.set_content(tv)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Known Hosts Editor")))
        tv.add_top_bar(header)

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label=_("Save"))
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(save_btn)

        # Create search entry with minimal margins
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search known hosts..."))
        self.search_entry.set_margin_start(12)
        self.search_entry.set_margin_end(12)
        self.search_entry.set_margin_top(6)
        self.search_entry.set_margin_bottom(3)
        self.search_entry.connect('search-changed', self._on_search_changed)
        
        # Create main content box
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        main_box.append(self.search_entry)
        
        # Add thin separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        main_box.append(separator)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        # Create scrolled window that expands to fill available space
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.listbox)
        scrolled.set_vexpand(True)  # Make it expand vertically
        main_box.append(scrolled)
        
        tv.set_content(main_box)

        self._load_entries()

    def _load_entries(self):
        """Load known_hosts entries into the listbox."""
        try:
            with open(self._known_hosts_path, 'r') as f:
                lines = [line.rstrip('\n') for line in f]
        except Exception as e:
            logger.error(f"Failed to load known_hosts: {e}")
            lines = []

        self._all_entries = []
        for line in lines:
            if not line.strip():
                continue
            self._all_entries.append(line)
        
        self._display_entries(self._all_entries)

    def _display_entries(self, entries):
        """Display the given entries in the listbox."""
        # Clear existing entries
        while True:
            child = self.listbox.get_first_child()
            if child is None:
                break
            self.listbox.remove(child)

        for line in entries:
            # Create a ListBoxRow to properly contain our content
            list_row = Gtk.ListBoxRow()
            list_row.set_margin_start(12)
            list_row.set_margin_end(12)
            list_row.set_margin_top(6)
            list_row.set_margin_bottom(6)
            # Store the original line for saving
            list_row._original_line = line
            
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_start(6)
            row.set_margin_end(6)
            
            remove_btn = Gtk.Button.new_from_icon_name('user-trash-symbolic')
            remove_btn.set_valign(Gtk.Align.START)
            remove_btn.set_tooltip_text(_("Remove this entry"))
            remove_btn.connect('clicked', self._on_remove_clicked, list_row)
            
            # Create a vertical box for the host information
            info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            info_box.set_hexpand(True)
            
            # Parse the known_hosts line to make it more readable
            parts = line.split()
            if len(parts) >= 3:
                hostname = parts[0]
                key_type = parts[1]
                key_data = parts[2]
                
                # Hostname label
                host_label = Gtk.Label(label=hostname)
                host_label.set_xalign(0)
                host_label.add_css_class("heading")
                host_label.set_selectable(True)
                info_box.append(host_label)
                
                # Key type and truncated key data
                key_info = f"{key_type} • {key_data[:50]}{'...' if len(key_data) > 50 else ''}"
                key_label = Gtk.Label(label=key_info)
                key_label.set_xalign(0)
                key_label.add_css_class("dim-label")
                key_label.set_selectable(True)
                info_box.append(key_label)
            else:
                # Fallback for malformed lines
                label = Gtk.Label(label=line)
                label.set_xalign(0)
                label.set_wrap(True)
                label.set_selectable(True)
                info_box.append(label)
            
            row.append(remove_btn)
            row.append(info_box)
            list_row.set_child(row)
            self.listbox.append(list_row)

    def _on_remove_clicked(self, _btn, row):
        try:
            # Add visual feedback before removal
            _btn.set_sensitive(False)  # Disable button to prevent double-clicks
            _btn.set_icon_name('process-working-symbolic')  # Show working icon
            
            # Create a smooth fade-out animation
            def animate_removal():
                # Start with full opacity
                current_opacity = 1.0
                
                def fade_step():
                    nonlocal current_opacity
                    current_opacity -= 0.1  # Reduce opacity by 10% each step
                    row.set_opacity(current_opacity)
                    
                    if current_opacity <= 0.1:
                        # Animation complete, remove the row
                        try:
                            # Remove from the listbox
                            self.listbox.remove(row)
                            # Remove from the all_entries list
                            original_line = getattr(row, '_original_line', None)
                            if original_line and original_line in self._all_entries:
                                self._all_entries.remove(original_line)
                        except Exception as e:
                            logger.error(f"Failed to remove known_host entry: {e}")
                        return False  # Stop the animation
                    
                    return True  # Continue animation
                
                # Start the animation with 50ms intervals
                GLib.timeout_add(50, fade_step)
            
            # Start the animation
            animate_removal()
            
        except Exception as e:
            logger.error(f"Failed to remove known_host entry: {e}")
            # Restore button state on error
            _btn.set_sensitive(True)
            _btn.set_icon_name('user-trash-symbolic')

    def _on_search_changed(self, search_entry):
        """Handle search text changes."""
        search_text = search_entry.get_text().lower().strip()
        
        if not search_text:
            # Show all entries if search is empty
            self._display_entries(self._all_entries)
        else:
            # Filter entries based on search text
            filtered_entries = []
            for line in self._all_entries:
                if search_text in line.lower():
                    filtered_entries.append(line)
            self._display_entries(filtered_entries)

    def _on_save_clicked(self, _btn):
        # Save all remaining entries from the _all_entries list
        lines = self._all_entries.copy()

        try:
            os.makedirs(os.path.dirname(self._known_hosts_path), exist_ok=True)
            with open(self._known_hosts_path, 'w') as f:
                if lines:
                    f.write('\n'.join(lines) + '\n')
                else:
                    f.write('')
            if self._on_saved:
                self._on_saved()
            self.close()
        except Exception as e:
            logger.error(f"Failed to save known_hosts: {e}")

