"""Advanced Configuration, Raw view: the text of the file on screen.

A tab for each file of the configuration (the root and what its
``Include`` lines read); the file on screen is the one edited everywhere
else too. The selected block's lines stand out, and putting the cursor in a
block selects it.

Text typed here waits to be applied: while it does, the other views are not
edited beside it (the two would overwrite each other) and nothing here is
marked as a block's, since the lines are no longer the blocks'.

Line numbers come from GtkSourceView when it is there, as in the SSH Config
Editor; the colouring is the view's own (GtkSourceView has no ssh_config
language): Host and Match lines, keywords and comments.
"""
from __future__ import annotations

from gettext import gettext as _, ngettext
from typing import Optional, Sequence

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("GtkSource", "5")
    from gi.repository import GtkSource  # noqa: E402
    _unused = GtkSource.View  # noqa: F841 - load the library now
except (ValueError, ImportError, AttributeError):
    GtkSource = None

from .ssh_config_blocks import DISABLED_PREFIX, keyword_of  # noqa: E402

_MARGIN = 10


class RawView(Gtk.Box):
    """*controller* supplies ``open_file``, ``raw_text_changed``,
    ``raw_cursor_moved``, ``apply_raw`` and ``discard_raw``."""

    def __init__(self, controller) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.controller = controller
        for side in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{side}")(16)
        self._text = ""
        self._syncing = False
        self._block = (0, 0)
        self._stale = True
        self._shown = False

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.tabs.add_css_class("linked")
        self.tabs.set_hexpand(True)
        self.tabs.set_halign(Gtk.Align.START)
        self._tab_files: list = []
        self._tab_syncing = False
        top.append(self.tabs)
        self.revert_button = Gtk.Button(label=_("Discard"))
        self.revert_button.set_tooltip_text(_("Drop the text typed here"))
        self.revert_button.set_sensitive(False)
        self.revert_button.connect("clicked", lambda *_a: controller.discard_raw())
        top.append(self.revert_button)
        self.apply_button = Gtk.Button(label=_("Apply"))
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.set_tooltip_text(_("Read the text typed here into the other views"))
        self.apply_button.set_sensitive(False)
        self.apply_button.connect("clicked", lambda *_a: controller.apply_raw())
        top.append(self.apply_button)
        self.append(top)

        if GtkSource is not None:
            self.view = GtkSource.View(monospace=True)
            self.view.set_show_line_numbers(True)
            self.view.set_highlight_current_line(False)
            buffer = GtkSource.Buffer()
            buffer.set_highlight_syntax(False)
            self.view.set_buffer(buffer)
            manager = GtkSource.StyleSchemeManager.get_default()
            self._schemes = (manager.get_scheme("Adwaita"), manager.get_scheme("Adwaita-dark"))
            try:
                from gi.repository import Adw

                style = Adw.StyleManager.get_default()
                style.connect("notify::dark", lambda *_a: self._apply_scheme())
                self._style = style
            except Exception:
                self._style = None
            self._apply_scheme()
        else:
            self.view = Gtk.TextView(monospace=True)
        for side in ("top", "bottom", "left", "right"):
            getattr(self.view, f"set_{side}_margin")(_MARGIN)
        self.view.set_wrap_mode(Gtk.WrapMode.NONE)
        buffer = self.view.get_buffer()
        buffer.connect("changed", self._on_changed)
        buffer.connect("notify::cursor-position", self._on_cursor_moved)
        self._block_tag = buffer.create_tag(
            "advcfg-selected-block",
            paragraph_background_rgba=Gdk.RGBA(0.21, 0.52, 0.89, 0.16),
        )
        self._header_tag = buffer.create_tag("advcfg-header", weight=700)
        self._keyword_tag = buffer.create_tag("advcfg-keyword")
        self._comment_tag = buffer.create_tag("advcfg-comment", style=Pango.Style.ITALIC)
        self._sync_tag_colours()
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_child(self.view)
        self.scroller.set_vexpand(True)
        frame = Gtk.Frame()
        frame.add_css_class("view")
        frame.set_child(self.scroller)
        self.append(frame)

        self.footer = Gtk.Label(xalign=0)
        self.footer.add_css_class("caption")
        self.footer.add_css_class("dim-label")
        self.append(self.footer)

    # -- look ----------------------------------------------------------------

    def _apply_scheme(self) -> None:
        if GtkSource is None:
            return
        dark = bool(self._style and self._style.get_dark())
        scheme = self._schemes[1 if dark else 0]
        if scheme is not None:
            self.view.get_buffer().set_style_scheme(scheme)
        self._sync_tag_colours()

    def _sync_tag_colours(self) -> None:
        dark = False
        try:
            from gi.repository import Adw

            dark = Adw.StyleManager.get_default().get_dark()
        except Exception:
            pass
        accent = Gdk.RGBA()
        accent.parse("#78aeed" if dark else "#1c71d8")
        keyword = Gdk.RGBA()
        keyword.parse("#8ff0a4" if dark else "#26a269")
        comment = Gdk.RGBA()
        comment.parse("#9a9996" if dark else "#77767b")
        if getattr(self, "_header_tag", None) is None:
            return
        self._header_tag.set_property("foreground-rgba", accent)
        self._keyword_tag.set_property("foreground-rgba", keyword)
        self._comment_tag.set_property("foreground-rgba", comment)

    def _paint_syntax(self) -> None:
        buffer = self.view.get_buffer()
        start, end = buffer.get_bounds()
        for tag in (self._header_tag, self._keyword_tag, self._comment_tag):
            buffer.remove_tag(tag, start, end)
        for number in range(buffer.get_line_count()):
            found, line_start = buffer.get_iter_at_line(number)
            if not found:
                continue
            line_end = line_start.copy()
            if not line_end.ends_line():
                line_end.forward_to_line_end()
            text = buffer.get_text(line_start, line_end, False)
            stripped = text.lstrip()
            if not stripped:
                continue
            indent = len(text) - len(stripped)
            word_start = line_start.copy()
            word_start.forward_chars(indent)
            if stripped.startswith("#") and not stripped.startswith(DISABLED_PREFIX):
                buffer.apply_tag(self._comment_tag, word_start, line_end)
                continue
            if stripped.startswith(DISABLED_PREFIX):
                buffer.apply_tag(self._comment_tag, word_start, line_end)
                continue
            keyword = keyword_of(text)
            word_end = word_start.copy()
            length = len(stripped.split(None, 1)[0].split("=", 1)[0])
            word_end.forward_chars(length)
            if keyword in ("host", "match"):
                buffer.apply_tag(self._header_tag, word_start, line_end)
            elif keyword:
                buffer.apply_tag(self._keyword_tag, word_start, word_end)

    # -- the files -----------------------------------------------------------

    def set_files(self, files: Sequence, current: str) -> None:
        """A tab per file of the configuration; none when there is one."""
        wanted = [(ref.file, ref.display_name) for ref in files]
        if wanted != self._tab_files:
            self._tab_files = wanted
            while (child := self.tabs.get_first_child()) is not None:
                self.tabs.remove(child)
            group = None
            for file, name in wanted:
                tab = Gtk.ToggleButton(label=name, group=group)
                tab.add_css_class("monospace")
                tab._file = file
                tab.connect("toggled", self._on_tab)
                group = group or tab
                self.tabs.append(tab)
        self.tabs.set_visible(len(wanted) > 1)
        self._tab_syncing = True
        try:
            child = self.tabs.get_first_child()
            while child is not None:
                if child._file == current and not child.get_active():
                    child.set_active(True)
                child = child.get_next_sibling()
        finally:
            self._tab_syncing = False

    def _on_tab(self, tab) -> None:
        if self._tab_syncing or not tab.get_active():
            return
        self.controller.open_file(tab._file)

    # -- the text ------------------------------------------------------------

    @property
    def pending(self) -> bool:
        """Text was typed here and not applied."""
        return self.apply_button.get_sensitive()

    def text(self) -> str:
        buffer = self.view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def set_text(self, text: str) -> None:
        """The draft's text changed; the view follows unless it is being
        edited itself."""
        self._text = text
        self._stale = True
        if self._shown:
            self.fill()

    def set_shown(self, shown: bool) -> None:
        self._shown = shown
        if shown and self._stale:
            self.fill()

    def fill(self) -> None:
        self._stale = False
        if self.pending:
            return
        if self.text() != self._text:
            self._syncing = True
            try:
                self.view.get_buffer().set_text(self._text)
            finally:
                self._syncing = False
            self._paint_syntax()
        self._paint_block()
        self._sync_footer()

    def discard(self) -> None:
        self.apply_button.set_sensitive(False)
        self.revert_button.set_sensitive(False)
        self._stale = True
        self.fill()

    def _on_changed(self, _buffer) -> None:
        if self._syncing:
            return
        pending = self.text() != self._text
        self.apply_button.set_sensitive(pending)
        self.revert_button.set_sensitive(pending)
        self._paint_syntax()
        self._paint_block()         # typed-over text is not the blocks' lines
        self._sync_footer()
        self.controller.raw_text_changed()

    def _sync_footer(self) -> None:
        count = self.view.get_buffer().get_line_count()
        lines = ngettext("{count} line", "{count} lines", count).format(count=count)
        if self.pending:
            text = _("{lines} · Not applied: the other views wait until this text is "
                     "applied or discarded").format(lines=lines)
        else:
            text = lines
        self.footer.set_label(text)

    # -- the selected block ------------------------------------------------------

    def mark_block(self, first: int, last: int, *, scroll: bool = False) -> None:
        """Make lines *first* to *last* (1-based) stand out as the selected
        block's; ``(0, 0)``: none. *scroll* brings them into view."""
        self._block = (int(first or 0), int(last or 0))
        if self.pending:
            return
        if self._shown and not self._stale:
            self._paint_block()
            if scroll and first:
                self._scroll_to_line(first)

    def _paint_block(self) -> None:
        buffer = self.view.get_buffer()
        buffer.remove_tag(self._block_tag, buffer.get_start_iter(), buffer.get_end_iter())
        first, last = self._block
        if not first or self.pending:
            return
        found, start = buffer.get_iter_at_line(first - 1)
        if not found:
            return
        found, end = buffer.get_iter_at_line(max(first, last) - 1)
        if not found:
            end = buffer.get_end_iter()
        elif not end.ends_line():
            end.forward_to_line_end()
        buffer.apply_tag(self._block_tag, start, end)

    def _scroll_to_line(self, line: int) -> None:
        buffer = self.view.get_buffer()
        found, start = buffer.get_iter_at_line(max(0, line - 1))
        if not found:
            return
        mark = buffer.create_mark(None, start, True)

        def scroll() -> bool:
            # Once laid out: a view just shown has no line heights yet.
            self.view.scroll_to_mark(mark, 0.0, True, 0.0, 0.2)
            buffer.delete_mark(mark)
            return False

        GLib.idle_add(scroll)

    def _on_cursor_moved(self, buffer, _pspec) -> None:
        # The user's cursor only: text put here by the view moves it too.
        # The focus of its window, not has_focus(): that one also needs the
        # window active, which a window just presented may not be yet.
        if self._syncing or self.pending or not self.view.is_focus():
            return
        position = buffer.get_iter_at_mark(buffer.get_insert())
        self.controller.raw_cursor_moved(position.get_line() + 1)

    def show_line(self, line: int) -> None:
        """Select line *line* (1-based) and bring it into view."""
        self.fill()
        buffer = self.view.get_buffer()
        found, start = buffer.get_iter_at_line(max(0, line - 1))
        if not found:
            start = buffer.get_end_iter()
        end = start.copy()
        if not end.ends_line():
            end.forward_to_line_end()
        buffer.select_range(start, end)
        self._scroll_to_line(line)
        self.view.grab_focus()

    def owns_focus(self, widget: Optional[Gtk.Widget]) -> bool:
        return widget is not None and (widget is self.view or widget.is_ancestor(self.view))
