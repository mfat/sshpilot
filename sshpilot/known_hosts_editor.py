import os
import logging
from typing import Callable, Optional

from gettext import gettext as _
from gi.repository import Gtk, Adw


logger = logging.getLogger(__name__)


class KnownHostsEditorWindow(Adw.Window):
    """Simple window for viewing and removing entries from known_hosts."""

    def __init__(self, parent, connection_manager, on_saved: Optional[Callable] = None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title(_("Known Hosts Editor"))

        self._cm = connection_manager
        self._on_saved = on_saved
        self._known_hosts_path = getattr(
            connection_manager,
            'known_hosts_path',
            os.path.expanduser('~/.ssh/known_hosts'),
        )

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

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.listbox)
        tv.set_content(scrolled)

        self._load_entries()

    def _load_entries(self):
        """Load known_hosts entries into the listbox."""
        try:
            with open(self._known_hosts_path, 'r') as f:
                lines = [line.rstrip('\n') for line in f]
        except Exception as e:
            logger.error(f"Failed to load known_hosts: {e}")
            lines = []

        for line in lines:
            if not line.strip():
                continue
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            label = Gtk.Label(label=line, xalign=0)
            label.set_hexpand(True)
            remove_btn = Gtk.Button.new_from_icon_name('user-trash-symbolic')
            remove_btn.set_valign(Gtk.Align.CENTER)
            remove_btn.connect('clicked', self._on_remove_clicked, row)
            row.append(label)
            row.append(remove_btn)
            self.listbox.append(row)

    def _on_remove_clicked(self, _btn, row):
        try:
            self.listbox.remove(row)
        except Exception as e:
            logger.error(f"Failed to remove known_host entry: {e}")

    def _on_save_clicked(self, _btn):
        lines = []
        child = self.listbox.get_first_child()
        while child:
            if isinstance(child, Gtk.ListBoxRow):
                row = child.get_child()
            else:
                row = child
            label = row.get_first_child()
            if isinstance(label, Gtk.Label):
                text = label.get_text()
                if text:
                    lines.append(text)
            child = child.get_next_sibling()

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

