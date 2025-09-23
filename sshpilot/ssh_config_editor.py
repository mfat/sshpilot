import os
import logging
from typing import Callable, Optional
from gettext import gettext as _

from gi.repository import Gtk, Adw, Pango

from .platform_utils import get_ssh_dir

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
        self._config_path = getattr(connection_manager, 'ssh_config_path', os.path.join(get_ssh_dir(), 'config'))

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

        tag_table = Gtk.TextTagTable()
        self._directive_tag = Gtk.TextTag.new("directive")
        self._directive_tag.props.weight = Pango.Weight.BOLD
        self._directive_tag.props.foreground = "#1c71d8"
        tag_table.add(self._directive_tag)

        self._comment_tag = Gtk.TextTag.new("comment")
        self._comment_tag.props.foreground = "#48733c"
        self._comment_tag.props.style = Pango.Style.ITALIC
        self._comment_tag.props.weight = Pango.Weight.LIGHT
        tag_table.add(self._comment_tag)

        buffer = Gtk.TextBuffer.new(tag_table)
        self.textview.set_buffer(buffer)
        self._highlight_handler_id = buffer.connect("changed", self._on_buffer_changed)


        try:
            with open(self._config_path, 'r') as f:
                buffer.set_text(f.read())
        except Exception as e:
            logger.error(f"Failed to load SSH config: {e}")
            buffer.set_text("")

        self._apply_highlighting()

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

    def _on_buffer_changed(self, _buffer):
        self._apply_highlighting()

    def _apply_highlighting(self):
        buffer = self.textview.get_buffer()
        start_iter = buffer.get_start_iter()
        end_iter = buffer.get_end_iter()

        if self._highlight_handler_id is not None:
            buffer.handler_block(self._highlight_handler_id)

        try:
            buffer.remove_tag(self._directive_tag, start_iter, end_iter)
            buffer.remove_tag(self._comment_tag, start_iter, end_iter)

            line_start = start_iter.copy()
            while True:
                line_end = line_start.copy()
                if not line_end.forward_to_line_end():
                    line_end = buffer.get_end_iter()

                line_text = buffer.get_text(line_start, line_end, False)

                stripped = line_text.lstrip()
                if stripped and not stripped.startswith('#'):
                    first_token = stripped.split(None, 1)[0]
                    leading_ws = len(line_text) - len(stripped)
                    token_start = line_start.copy()
                    token_start.forward_chars(leading_ws)
                    token_end = token_start.copy()
                    token_end.forward_chars(len(first_token))
                    buffer.apply_tag(self._directive_tag, token_start, token_end)

                comment_index = line_text.find('#')
                if comment_index != -1:
                    comment_start = line_start.copy()
                    comment_start.forward_chars(comment_index)
                    buffer.apply_tag(self._comment_tag, comment_start, line_end)

                if line_end.equal(buffer.get_end_iter()):
                    break

                line_start = line_end.copy()
                if not line_start.forward_char():
                    break
                if line_start.is_end():
                    break
        finally:
            if self._highlight_handler_id is not None:
                buffer.handler_unblock(self._highlight_handler_id)
