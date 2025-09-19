"""Simple built-in file manager window used when GVFS is unavailable."""

from __future__ import annotations

import logging
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw

logger = logging.getLogger(__name__)


class FileManagerWindow(Adw.Window):
    """Placeholder in-app file manager window."""

    def __init__(
        self,
        *,
        user: str,
        host: str,
        port: Optional[int] = None,
        parent: Optional[Adw.Window] = None,
        nickname: Optional[str] = None,
    ):
        super().__init__()

        self.user = user
        self.host = host
        self.port = port
        self.nickname = nickname or host

        self._configure_window(parent)
        self._build_placeholder_ui()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
    def _configure_window(self, parent: Optional[Adw.Window]) -> None:
        """Apply basic window configuration, guarding for stubbed widgets."""

        title = f"{self.nickname or self.host}" if (self.nickname or self.host) else "Remote Files"
        for setter, value in (
            ("set_title", f"Remote Files – {title}"),
            ("set_default_size", (720, 480)),
            ("set_transient_for", parent),
            ("set_modal", True),
        ):
            if hasattr(self, setter):
                try:
                    attr = getattr(self, setter)
                    if setter == "set_default_size" and isinstance(value, tuple):
                        attr(*value)
                    else:
                        attr(value)
                except Exception:  # pragma: no cover - defensive
                    logger.debug("Unable to call %s on FileManagerWindow", setter)

    def _build_placeholder_ui(self) -> None:
        """Render a simple placeholder UI until the full manager ships."""

        if not hasattr(Gtk, "Box") or not hasattr(self, "set_content"):
            return

        main_box = Gtk.Box(orientation=getattr(Gtk.Orientation, "VERTICAL", 1), spacing=12)
        for setter in ("set_margin_top", "set_margin_bottom", "set_margin_start", "set_margin_end"):
            if hasattr(main_box, setter):
                try:
                    getattr(main_box, setter)(24)
                except Exception:  # pragma: no cover - defensive
                    continue

        if hasattr(Gtk, "Label"):
            header = Gtk.Label()
            header.set_markup(
                "<b>Built-in File Manager</b>\n"
                "This preview allows browsing remote servers even without GVFS."
            )
            header.set_wrap(True)
            header.set_halign(getattr(Gtk.Align, "START", 1))
            main_box.append(header)

            subtitle = Gtk.Label()
            subtitle.set_text(
                f"Connected to {self.user}@{self.host}:{self.port or 22}. "
                "A richer file browser will be provided in a future update."
            )
            subtitle.set_wrap(True)
            subtitle.add_css_class("dim-label")
            subtitle.set_halign(getattr(Gtk.Align, "START", 1))
            main_box.append(subtitle)

        if hasattr(Gtk, "Button"):
            close_button = Gtk.Button.new_with_label("Close")
            close_button.set_halign(getattr(Gtk.Align, "END", 1))
            close_button.connect("clicked", lambda *_: self.close())
            main_box.append(close_button)

        try:
            self.set_content(main_box)
        except Exception:  # pragma: no cover - defensive
            logger.debug("Unable to assign placeholder content to FileManagerWindow")
