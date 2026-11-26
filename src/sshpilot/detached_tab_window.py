"""A tab moved out of the main window into a window of its own.

The tab's page is transferred between Adw.TabViews (``transfer_page``), so
its widget tree moves as it is: an RDP session, or a file manager with its
connections, transfers and the terminals below it. Nothing reconnects.
"Move Back to Tab" transfers it back into the main window. Closing this
window ends the tab, as closing the tab would.
"""

from __future__ import annotations

from gettext import gettext as _
from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402


class DetachedTabWindow(Adw.ApplicationWindow):
    """Holds one tab moved out of the main window."""

    def __init__(
        self,
        main_window: Any,
        title: str,
        *,
        on_move_back: Callable[["DetachedTabWindow"], None],
        on_close: Callable[["DetachedTabWindow"], None],
    ) -> None:
        super().__init__(application=main_window.get_application(), title=title)
        self._on_move_back = on_move_back
        self._on_close = on_close
        self._moving_back = False

        # A tab view without a tab bar: transfer_page needs one on both sides,
        # and it shows the single page as the window's content.
        self.tab_view = Adw.TabView()

        header = Adw.HeaderBar()
        back = Gtk.Button(label=_("Move Back to Tab"))
        back.connect("clicked", lambda *_a: self.move_back())
        header.pack_start(back)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.tab_view)
        self.set_content(toolbar)

        self.connect("close-request", self._on_close_request)

    @property
    def tab(self):
        page = self.tab_view.get_selected_page()
        return page.get_child() if page is not None else None

    def move_back(self) -> None:
        """Return the tab to the main window; it carries on as it is."""

        self._moving_back = True
        self._on_move_back(self)
        self.destroy()

    def _on_close_request(self, *_args) -> bool:
        if not self._moving_back:
            self._on_close(self)
        return False
