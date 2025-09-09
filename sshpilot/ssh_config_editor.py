import os
import logging
from typing import Callable, Optional
from gettext import gettext as _

from gi.repository import Gtk, Adw

logger = logging.getLogger(__name__)

class SSHConfigEditorWindow(Adw.Window):
    """Simple window for editing the user's ~/.ssh/config file."""

    def __init__(self, parent, connection_manager, on_saved: Optional[Callable] = None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(700, 500)
        self.set_title(_("Edit SSH Config"))

        self._cm = connection_manager
        self._on_saved = on_saved
        self._config_path = getattr(connection_manager, 'ssh_config_path', os.path.expanduser('~/.ssh/config'))

        # Toolbar view with header bar
        tv = Adw.ToolbarView()
        self.set_content(tv)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Edit SSH Config")))
        tv.add_top_bar(header)

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label=_("Save"))
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(save_btn)

        # Text view for editing
        self.textview = Gtk.TextView()
        self.textview.set_monospace(True)
        buffer = self.textview.get_buffer()

        try:
            with open(self._config_path, 'r') as f:
                buffer.set_text(f.read())
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}")
            buffer.set_text("")

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.textview)
        tv.set_content(scrolled)

    def _on_save_clicked(self, _btn):
        buffer = self.textview.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, 'w') as f:
                f.write(text)
            if self._cm:
                self._cm.load_ssh_config()
            if self._on_saved:
                self._on_saved()
            self.close()
        except Exception as e:
            logger.error(f"Failed to save SSH config: {e}")
