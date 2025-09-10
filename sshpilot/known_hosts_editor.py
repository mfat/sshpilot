import os
import logging
from gettext import gettext as _

from gi.repository import Gtk, Adw

logger = logging.getLogger(__name__)


class KnownHostsEditorWindow(Adw.Window):
    """Simple editor for the user's known_hosts file."""

    def __init__(self, parent, connection_manager):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(600, 400)
        self.set_title(_("Edit Known Hosts"))

        self._cm = connection_manager
        self._known_hosts_path = getattr(
            connection_manager,
            "known_hosts_path",
            os.path.expanduser("~/.ssh/known_hosts"),
        )
        self._lines: list[str | None] = []

        tv = Adw.ToolbarView()
        self.set_content(tv)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Edit Known Hosts")))
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
        scrolled.set_child(self.listbox)
        tv.set_content(scrolled)

        self._load_entries()

    def _load_entries(self) -> None:
        """Load known_hosts lines into the listbox."""
        try:
            if not os.path.exists(self._known_hosts_path):
                return
            with open(self._known_hosts_path, "r", encoding="utf-8") as f:
                for idx, line in enumerate(f):
                    line = line.rstrip("\n")
                    self._lines.append(line)
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    parts = stripped.split()
                    host_field = parts[0]
                    key_type = parts[1] if len(parts) > 1 else ""

                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                    row.set_margin_top(6)
                    row.set_margin_bottom(6)

                    host_label = Gtk.Label(label=host_field, xalign=0)
                    host_label.set_hexpand(True)
                    row.append(host_label)

                    type_label = Gtk.Label(label=key_type, xalign=0)
                    type_label.set_hexpand(True)
                    row.append(type_label)

                    remove_btn = Gtk.Button.new_from_icon_name("list-remove-symbolic")
                    remove_btn.set_tooltip_text(_("Remove"))
                    remove_btn.connect("clicked", self._on_remove_clicked, row)
                    row._line_index = idx  # type: ignore[attr-defined]
                    row.append(remove_btn)

                    self.listbox.append(row)
        except Exception as e:
            logger.error(f"Failed to load known hosts: {e}")

    def _on_remove_clicked(self, _btn: Gtk.Button, row: Gtk.Widget) -> None:
        idx = getattr(row, "_line_index", None)
        if idx is not None:
            self._lines[idx] = None
        self.listbox.remove(row)

    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        try:
            os.makedirs(os.path.dirname(self._known_hosts_path), exist_ok=True)
            with open(self._known_hosts_path, "w", encoding="utf-8") as f:
                for line in self._lines:
                    if line is not None:
                        f.write(line + "\n")
            if getattr(self._cm, "load_known_hosts", None):
                try:
                    self._cm.load_known_hosts()  # type: ignore[attr-defined]
                except Exception:
                    pass
            self.close()
        except Exception as e:
            logger.error(f"Failed to save known hosts: {e}")
