"""OLED / Pure black theme CSS and display provider management.

This module provides an OLED pitch-black (#000000) style for SSH Pilot
without modifying the user's terminal color schemes.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

OLED_THEME_CSS = """
/* OLED / Pure Black Theme for SSH Pilot */
:root, window, dialog {
  --window-bg-color: #000000;
  --window-fg-color: #f6f6f6;
  --view-bg-color: #000000;
  --view-fg-color: #f6f6f6;
  --headerbar-bg-color: #000000;
  --headerbar-fg-color: #f6f6f6;
  --headerbar-backdrop-color: #000000;
  --headerbar-border-color: rgba(255, 255, 255, 0.08);
  --card-bg-color: #080808;
  --card-fg-color: #f6f6f6;
  --card-border-color: rgba(255, 255, 255, 0.08);
  --dialog-bg-color: #000000;
  --dialog-fg-color: #f6f6f6;
  --popover-bg-color: #0d0d0d;
  --popover-fg-color: #f6f6f6;
  --secondary-sidebar-bg-color: #000000;
  --sidebar-bg-color: #000000;
  --sidebar-backdrop-color: #000000;
}

@define-color window_bg_color #000000;
@define-color window_fg_color #f6f6f6;
@define-color view_bg_color #000000;
@define-color view_fg_color #f6f6f6;
@define-color headerbar_bg_color #000000;
@define-color headerbar_fg_color #f6f6f6;
@define-color headerbar_backdrop_color #000000;
@define-color card_bg_color #080808;
@define-color card_fg_color #f6f6f6;
@define-color dialog_bg_color #000000;
@define-color dialog_fg_color #f6f6f6;
@define-color popover_bg_color #0d0d0d;
@define-color popover_fg_color #f6f6f6;
@define-color secondary_sidebar_bg_color #000000;
@define-color sidebar_bg_color #000000;
@define-color sidebar_backdrop_color #000000;

window.background,
window {
  background-color: #000000;
}

headerbar {
  background-color: #000000;
  box-shadow: inset 0 -1px rgba(255, 255, 255, 0.08);
}

.navigation-sidebar,
navigation-sidebar,
sidebar {
  background-color: #000000;
}

.card,
list.boxed-list {
  background-color: #080808;
  border: 1px solid rgba(255, 255, 255, 0.08);
}

popover,
popover contents {
  background-color: #0d0d0d;
  border: 1px solid rgba(255, 255, 255, 0.12);
}

/* Dialogs, about windows, sheets, statuspages */
dialog,
dialog > *,
dialog.about,
dialog.about > *,
dialog.about .content,
dialog.about statuspage,
dialog.about subpage,
dialog.about navigation-view,
dialog.about navigation-view > *,
window.dialog,
window.about,
adwaboutdialog,
adwaboutdialog > *,
adwstatuspage,
statuspage,
.sheet,
.dialog,
dialog.background {
  background-color: #000000;
  color: #f6f6f6;
}

dialog {
  border: 1px solid rgba(255, 255, 255, 0.12);
}

/* Bottom action bars, split view strips, file manager footers */
actionbar,
actionbar > revealer > box,
.add-pane-strip,
.add-pane-strip > revealer > box,
actionbar.fm-bottom-card,
actionbar.fm-bottom-card > revealer > box {
  background-color: #000000;
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.08);
}

preferencespage,
preferencesgroup,
adwpreferenceswindow {
  background-color: #000000;
}

searchbar,
searchbar > revealer > box {
  background-color: #000000;
  box-shadow: inset 0 -1px rgba(255, 255, 255, 0.08);
}

tabbar,
tabbox {
  background-color: #000000;
}

scrolledwindow,
viewport {
  background-color: transparent;
}
"""


def apply_oled_theme_override(enabled: bool) -> None:
    """Add or remove the OLED pure black CSS provider on default display."""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
    except Exception:
        pass

    try:
        from gi.repository import Gdk, Gtk
    except (ImportError, ValueError):
        return

    display = Gdk.Display.get_default()
    if display is None:
        return

    existing: Optional[Gtk.CssProvider] = getattr(display, "_oled_theme_provider", None)
    if not enabled:
        if existing is not None:
            try:
                Gtk.StyleContext.remove_provider_for_display(display, existing)
            except Exception:
                pass
            try:
                delattr(display, "_oled_theme_provider")
            except Exception:
                pass
        return

    if existing is not None:
        return

    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(OLED_THEME_CSS.encode("utf-8"))
        # High user priority to override built-in actionbar / dialog gray styles
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
        display._oled_theme_provider = provider
        logger.info("Applied OLED pure black theme override")
    except Exception as exc:
        logger.error("Failed to apply OLED theme override: %s", exc)
