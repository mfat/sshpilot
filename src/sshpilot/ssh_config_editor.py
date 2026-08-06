"""GTK/Adwaita text editor for the daemon-selected SSH config file.

The window restores the original SSH config editor UI, but file resolution
and writing are owned by the daemon: GTK never decides the config path, never
reads or writes the config file directly, and installs no filesystem watcher.
Loading and saving go through the typed daemon API
(``get_ssh_config_text`` / ``save_ssh_config_text``).
"""

from __future__ import annotations

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from gettext import gettext as _
from typing import Optional

from gi.repository import Adw, GLib, Gtk, Pango

from sshpilot.api.models.operations import SaveSshConfigTextRequest

logger = logging.getLogger(__name__)


class DaemonSshConfigEditorService:
    """GTK adapter over daemon-owned SSH config text operations.

    Returns futures so callers dispatch back to the GTK main thread via the
    existing pattern. The active revision is tracked here so saves echo the
    revision the editor loaded, letting the daemon reject stale saves.
    """

    def __init__(self, client) -> None:
        if client is None:
            raise ValueError("a daemon client is required")
        self._client = client
        self._revision: Optional[str] = None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sshpilot-ssh-config-editor"
        )

    def load(self) -> Future:
        def _do():
            snapshot = self._client.get_ssh_config_text()
            self._revision = snapshot.revision
            return snapshot

        return self._executor.submit(_do)

    def save_text(self, content: str) -> Future:
        request = SaveSshConfigTextRequest(
            text=content,
            expected_revision=self._revision or "",
        )

        def _do():
            snapshot = self._client.save_ssh_config_text(request)
            self._revision = snapshot.revision
            return snapshot

        return self._executor.submit(_do)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SSHConfigEditorWindow(Adw.Window):
    """Restored SSH config text editor, backed by the daemon client."""

    def __init__(self, parent, client, on_saved: Optional[callable] = None):
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(700, 500)
        self.set_title(_("Edit SSH Config"))

        self._service = DaemonSshConfigEditorService(client)
        self._on_saved = on_saved
        self._revision: Optional[str] = None
        self._writable = True
        self._display_name = _("SSH config")

        tv = Adw.ToolbarView()
        self.set_content(tv)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=_("Edit SSH Config")))
        tv.add_top_bar(header)

        cancel_btn = Gtk.Button(label=_("Cancel"))
        cancel_btn.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel_btn)

        self._save_btn = Gtk.Button(label=_("Save"))
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(self._save_btn)

        self.textview = Gtk.TextView()
        self.textview.set_monospace(True)
        self.textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.textview.set_editable(self._writable)

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

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(self.textview)
        tv.set_content(scrolled)

        GLib.idle_add(self._load)
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def _load(self) -> bool:
        self._save_btn.set_sensitive(False)
        future = self._service.load()

        def _done(fut):
            try:
                snapshot = fut.result()
            except Exception as exc:
                logger.error("Failed to load SSH config: %s", exc)
                GLib.idle_add(self._show_error, _("Failed to load SSH config"))
                return
            GLib.idle_add(self._apply_loaded, snapshot)

        future.add_done_callback(_done)
        return False

    def _apply_loaded(self, snapshot) -> bool:
        self._revision = snapshot.revision
        self._display_name = snapshot.display_name
        self._writable = snapshot.writable
        self.set_title(_("Edit SSH Config — {name}").format(name=self._display_name))
        buffer = self.textview.get_buffer()
        buffer.handler_block(self._highlight_handler_id)
        buffer.set_text(snapshot.text)
        buffer.handler_unblock(self._highlight_handler_id)
        self.textview.set_editable(self._writable)
        self._save_btn.set_sensitive(self._writable)
        self._apply_highlighting()
        return False

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save_clicked(self, _btn):
        buffer = self.textview.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
        self._save_btn.set_sensitive(False)
        future = self._service.save_text(text)

        def _done(fut):
            try:
                snapshot = fut.result()
            except Exception as exc:
                logger.error("Failed to save SSH config: %s", exc)
                GLib.idle_add(self._show_error, _("Failed to save SSH config"))
                GLib.idle_add(self._save_btn.set_sensitive, self._writable)
                return
            GLib.idle_add(self._after_save, snapshot)

        future.add_done_callback(_done)

    def _after_save(self, snapshot) -> bool:
        self._revision = snapshot.revision
        if self._on_saved:
            try:
                self._on_saved()
            except Exception as exc:
                logger.debug("on_saved callback failed: %s", exc)
        self.close()
        return False

    def _on_close_request(self, *_args):
        self._service.close()
        return False

    def _show_error(self, message: str) -> bool:
        try:
            self.show_toast(Adw.Toast.new(message))
        except Exception as exc:
            logger.debug("toast failed: %s", exc)
        return False

    # ------------------------------------------------------------------
    # Syntax highlighting
    # ------------------------------------------------------------------

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
