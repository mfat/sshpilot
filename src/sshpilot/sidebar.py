"""Sidebar components and drag-and-drop helpers for sshPilot."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Gio, GObject, GLib, Graphene, Gsk, Pango, Adw

from gettext import gettext as _

from .dnd_payload import (
    content_provider_for_payload,
    decode_dnd_payload,
    new_internal_drop_target,
    set_internal_drag_icon,
)
from .platform_utils import is_macos
from .connection_manager import Connection
from .connection_display import (
    get_connection_alias as _get_connection_alias,
    get_connection_host as _get_connection_host,
    format_connection_host_display as _format_connection_host_display,
)
from .context_menu import IconContextMenu
from .file_manager_integration import (
    should_hide_external_terminal_options,
    should_hide_file_manager_options,
)
from .groups import GroupManager
from .plugins.api import Capability
from .plugins.registry import capabilities_for
from .shortcut_utils import get_primary_modifier_label
from .tag_groups import compute_tag_groups

# Feature detection for libadwaita versions across distros
HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')
HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')

# oklab(from ... var(--standalone-color-oklab)) needs GTK >= 4.16 relative-colour
# syntax and libadwaita >= 1.6 (which defines the variable). On older stacks the
# rule would be dropped, so fall back to the plain group colour there.
try:
    _SUPPORTS_STANDALONE_OKLAB = (
        (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16)
        and (Adw.MAJOR_VERSION, Adw.MINOR_VERSION) >= (1, 6)
    )
except Exception:
    _SUPPORTS_STANDALONE_OKLAB = False

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Styling helpers
# ---------------------------------------------------------------------------


_COLOR_CSS_INSTALLED = False
_DEFAULT_ROW_MARGIN_START = 0
_DEFAULT_ROW_WIDGET_MARGIN_START = -1
_GROUP_DISPLAY_OPTIONS = {"fullwidth", "nested"}
_GROUP_ROW_INDENT_WIDTH = 20
_MIN_VALID_MARGIN = 0

# Drop indicator (between-rows reorder bar) drawing parameters.
_DROP_BAR_HEIGHT = 10        # widget height reserved when shown
_DROP_BAR_THICKNESS = 2      # thickness of the pill bar
_DROP_BAR_INSET_LEFT = 6     # left inset (kept small so the bar spans nearly full width)
_DROP_BAR_INSET_RIGHT = 8    # right inset
_DROP_BAR_CAP_RADIUS = 4     # radius of the leading round cap (caret node)
_DROP_BAR_FALLBACK_ACCENT = "#3584e4"  # Adwaita blue when no theme accent
# Generous hit band for seams between sibling group subtrees (reorder targets).
_GROUP_SEAM_HIT_PX = 16

def _install_sidebar_color_css():
    global _COLOR_CSS_INSTALLED
    if _COLOR_CSS_INSTALLED:
        return

    try:
        display = Gdk.Display.get_default()
        if not display:
            return

        provider = Gtk.CssProvider()
        css = """
        .accent-red { background-color: #ff5c57; }
        .accent-blue { background-color: #51a1ff; }
        .accent-green { background-color: #5fff8d; }
        .accent-orange { background-color: #ffb347; }
        .accent-purple { background-color: #d6a2ff; }
        .accent-cyan { background-color: #5be7ff; }
        .accent-gray { background-color: #d3d7db; }

        /* Accent-bar group colour: a straight vertical bar on the leading edge.
           The transparent border is reserved on every bar-mode row so coloured
           and uncoloured rows stay aligned. The bar itself is painted by the
           per-row provider as a background-image strip, vertically inset and
           centered, so it keeps square ends while the row's rounded corners
           stay intact (a coloured border-left would either curve with the
           corner or force it square). */
        .color-bar { border-left: 3px solid transparent; }

        /* Virtual tag group rows use the osd style; the default row hover
           replaces the background with a near-transparent overlay, leaving
           osd's white text unreadable in light mode. Keep it dark. */
        row.osd:hover:not(:selected) { background-color: rgba(0, 0, 0, 0.8); }
        row.osd:active:not(:selected) { background-color: rgba(0, 0, 0, 0.85); }
        """
        provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        _COLOR_CSS_INSTALLED = True
    except Exception:
        logger.debug("Failed to install sidebar color CSS", exc_info=True)


def install_sidebar_css():
    """Install sidebar focus CSS"""
    try:
        # Install CSS for sidebar focus highlighting once per display
        display = Gdk.Display.get_default()
        if not display:
            logger.warning("No display available for CSS installation")
            return
        # Use an attribute on the display to avoid re-adding provider
        if getattr(display, '_sidebar_css_installed', False):
            return
        provider = Gtk.CssProvider()
        css = """

        /* optional: a subtle focus ring while the list is focused */
        row:selected:focus-within {
          /* box-shadow: 0 0 8px 2px @accent_bg_color inset; */
          /* border: 2px solid @accent_bg_color;  Adds a solid border of 2px thickness */
          border-radius: 8px;
        }
        
        /* Group styling */
        .group-expand-button {
          min-width: 16px;
          min-height: 16px;
          padding: 2px;
          border-radius: 4px;
        }
        
        .group-expand-button:hover {
          background: alpha(@accent_bg_color, 0.1);
        }
        
        /* Smooth drag indicator transitions */
        .drag-indicator {
          opacity: 0;
          transition: opacity 0.15s ease-in-out;
        }
        
        .drag-indicator.visible {
          opacity: 1;
        }
        
        /* Smooth transitions for connection rows during drag */
        .navigation-sidebar {
          transition: transform 0.1s ease-out, opacity 0.1s ease-out;
        }
        
        .navigation-sidebar.dragging {
          opacity: 0.7;
          transform: scale(0.98);
        }

        /* Gap between sidebar list rows (GtkListBox has no spacing property) */
        .navigation-sidebar row {
          margin: 4px 8px;
        }

        /* Selected sidebar row: always use the accent so selection is
           visible in dark mode by default. libadwaita's default
           navigation-sidebar selection is a neutral shade that is nearly
           invisible against the dark card background; this rule (which the
           accent-override path already emits, but only when an override is
           set) makes selection clear unconditionally.
           @accent_bg_color / @accent_fg_color follow the system accent and
           any user override. The more specific .tinted:selected rule below
           keeps the identical color for grouped rows. */
        .navigation-sidebar row:selected {
          background-color: @accent_bg_color;
          color: @accent_fg_color;
        }

        .navigation-sidebar row.tinted {
          margin: 4px 8px;
          border-radius: 10px;
          transition: background-color 0s ease;
        }

        .navigation-sidebar row.tinted:not(:selected) {
          background-color: alpha(@accent_bg_color, 0.18);
        }

        .navigation-sidebar row.tinted:hover:not(:selected) {
          background-color: alpha(@accent_bg_color, 0.24);
        }

        .navigation-sidebar row.tinted:active:not(:selected) {
          background-color: alpha(@accent_bg_color, 0.30);
        }

        .navigation-sidebar row.tinted:selected {
          background-color: @accent_bg_color;
          color: @accent_fg_color;
          box-shadow: inset 0 0 0 1px @accent_bg_color;
        }

        .navigation-sidebar row.tinted:selected:hover {
          background-color: shade(@accent_bg_color, 0.95);
        }

        .navigation-sidebar row.tinted:selected:active {
          background-color: shade(@accent_bg_color, 0.90);
        }

        /* Accent-bar mode: the coloured left bar is the highlight, so a
           selected row uses a neutral overlay instead of the accent fill
           (which would swamp the bar). Every bar-mode row carries
           .color-bar, and this out-specifies the accent `row:selected`
           rule above at the same provider priority. */
        .navigation-sidebar row.color-bar:selected {
          background-color: alpha(@window_fg_color, 0.10);
          color: @window_fg_color;
          box-shadow: none;
        }

        .navigation-sidebar row.color-bar:selected:hover {
          background-color: alpha(@window_fg_color, 0.13);
        }

        .navigation-sidebar row.color-bar:selected:active {
          background-color: alpha(@window_fg_color, 0.16);
        }

        /* Minimal strip only (.sidebar-minimal is set on the sidebar box while
           collapsed): selection uses the plain libadwaita-style neutral shade
           for every row — no accent fill, no forced fg text — regardless of the
           group colour mode. Higher specificity than the accent/color-bar rules
           above so it wins; the full sidebar is untouched. */
        .sidebar-minimal .navigation-sidebar row:selected,
        .sidebar-minimal .navigation-sidebar row.tinted:selected,
        .sidebar-minimal .navigation-sidebar row.color-bar:selected {
          background-color: alpha(currentColor, 0.10);
          color: inherit;
          box-shadow: none;
        }

        .sidebar-minimal .navigation-sidebar row:selected:hover,
        .sidebar-minimal .navigation-sidebar row.tinted:selected:hover,
        .sidebar-minimal .navigation-sidebar row.color-bar:selected:hover {
          background-color: alpha(currentColor, 0.13);
        }

        .sidebar-minimal .navigation-sidebar row:selected:active,
        .sidebar-minimal .navigation-sidebar row.tinted:selected:active,
        .sidebar-minimal .navigation-sidebar row.color-bar:selected:active {
          background-color: alpha(currentColor, 0.16);
        }

        /* Reorder placeholder: a slim transparent gap row whose child
           DragIndicator draws the accent bar; the list parts around it. */
        .drop-placeholder-row {
          background: transparent;
          min-height: 0;
          padding: 0;
        }

        /* Group drop target highlight */
        .drop-target-group {
          background: alpha(@accent_bg_color, 0.25);
          border-radius: 8px;
          box-shadow: 0 0 0 2px @accent_bg_color inset,
                      0 2px 8px alpha(@accent_bg_color, 0.4);
          transition: background-color 0.15s ease-in-out,
                      box-shadow 0.15s ease-in-out;
        }
        
        /* Drop target indicator styling */
        .drop-target-indicator {
          background: alpha(@accent_bg_color, 0.9);
          color: white;
          border-radius: 12px;
          padding: 4px 12px;
          margin: 4px 8px;
          font-weight: bold;
          font-size: 0.9em;
          animation: drop-indicator-bounce 0.6s ease-in-out;
        }

        @keyframes drop-indicator-bounce {
          0% {
            transform: translateY(-10px) scale(0.8);
            opacity: 0;
          }
          60% {
            transform: translateY(2px) scale(1.05);
            opacity: 1;
          }
          100% {
            transform: translateY(0) scale(1);
            opacity: 1;
          }
        }

        /* Minimal (icon-only) sidebar avatars: a plain neutral circle. The
           circle fill stays neutral for every row; group rows tint only their
           glyph (folder icon / initials) via a per-widget provider (see
           _set_avatar_color). */
        .sidebar-avatar {
          min-width: 28px;
          min-height: 28px;
          border-radius: 9999px;
          background-color: alpha(@window_fg_color, 0.15);
          color: @window_fg_color;
          font-weight: bold;
        }

        /* Ring around a connected connection's avatar. */
        .sidebar-avatar.sidebar-avatar-online {
          box-shadow: 0 0 0 2px @success_color;
        }

        /* Detachable sidebar popup: an opaque panel floating over the work area
           (see search_popup.SearchPopup). The shadow lifts it off the
           content; the scrim is transparent and only captures click-outside. */
        .sidebar-popup {
          background-color: @window_bg_color;
          box-shadow: 2px 0 12px rgba(0, 0, 0, 0.35);
        }

        /* Programmatic-only subtle transparency (see
           SearchPopup.set_transparent) — the terminal shows faintly
           through the panel while the rows stay readable. */
        .sidebar-popup.sidebar-popup-transparent {
          background-color: alpha(@window_bg_color, 0.86);
        }

        .sidebar-popup-scrim {
          background-color: transparent;
        }

        /* Dim backdrop for the centered/spotlight popup modes (see
           SearchPopup.set_backdrop) — pushes the work area back. */
        .sidebar-popup-scrim.sidebar-popup-scrim-dim {
          background-color: rgba(0, 0, 0, 0.35);
        }

        """
        provider.load_from_data(css.encode('utf-8'))
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        setattr(display, '_sidebar_css_installed', True)
        logger.debug("Sidebar CSS installed successfully")
    except Exception as e:
        logger.error(f"Failed to install sidebar CSS: {e}")
        import traceback
        logger.debug(f"CSS installation traceback: {traceback.format_exc()}")


def _use_flat_sidebar_rows(config) -> bool:
    if config is None:
        return False
    try:
        return bool(config.get_setting('ui.sidebar_flat_rows', False))
    except Exception:
        return False


def _apply_sidebar_row_style(
    row: Gtk.ListBoxRow,
    config,
    *,
    in_tag_section: bool = False,
    flat: bool | None = None,
) -> None:
    """Apply card or flat navigation-sidebar styling to a sidebar row."""
    if in_tag_section:
        row.remove_css_class('card')
        row.remove_css_class('navigation-sidebar')
        if not row.has_css_class('osd'):
            row.add_css_class('osd')
        return

    use_flat = flat if flat is not None else _use_flat_sidebar_rows(config)
    row.remove_css_class('osd')
    if use_flat:
        row.remove_css_class('card')
        if not row.has_css_class('navigation-sidebar'):
            row.add_css_class('navigation-sidebar')
    else:
        row.remove_css_class('navigation-sidebar')
        if not row.has_css_class('card'):
            row.add_css_class('card')


def _parse_color(value: Optional[str]) -> Optional[Gdk.RGBA]:
    if not value:
        return None

    rgba = Gdk.RGBA()
    try:
        if rgba.parse(str(value)):
            return rgba
    except Exception:
        logger.debug("Failed to parse color value '%s'", value, exc_info=True)
    return None


def _resolve_group_color_by_id(manager, group_id) -> Optional[Gdk.RGBA]:
    """Walk the group's parent chain and return the first colour found.

    A group keeps its own colour when set; otherwise it inherits the nearest
    coloured ancestor's colour. Returns ``None`` when no ancestor has a colour.
    """
    if not manager:
        return None

    visited = set()
    while group_id:
        if group_id in visited:
            break
        visited.add(group_id)

        try:
            group_info = manager.groups.get(group_id)
        except Exception:
            group_info = None

        if not group_info:
            break

        color = _parse_color(group_info.get('color'))
        if color:
            return color

        group_id = group_info.get('parent_id')

    return None


def _get_color_display_mode(config) -> str:
    try:
        mode = str(config.get_setting('ui.group_color_display', 'fill')).lower()
    except Exception:
        return 'fill'

    if mode not in {'fill', 'badge', 'bar', 'dot'}:
        return 'fill'
    return mode


def _fill_rgba(rgba: Optional[Gdk.RGBA]) -> Optional[Gdk.RGBA]:
    if rgba is None:
        return None

    fill = Gdk.RGBA()
    fill.red = rgba.red
    fill.green = rgba.green
    fill.blue = rgba.blue
    fill.alpha = 0.6 if rgba.alpha >= 1.0 else max(0.5, min(rgba.alpha, 0.7))
    return fill


def _get_color_class(rgba: Optional[Gdk.RGBA]) -> Optional[str]:
    if not rgba:
        return None

    import colorsys

    h, s, _v = colorsys.rgb_to_hsv(rgba.red, rgba.green, rgba.blue)

    if s < 0.3:
        return "accent-gray"
    if h < 0.1 or h > 0.9:
        return "accent-red"
    if h < 0.2:
        return "accent-orange"
    if h < 0.4:
        return "accent-green"
    if h < 0.6:
        return "accent-cyan"
    if h < 0.8:
        return "accent-blue"
    return "accent-purple"


def _set_tint_card_color(row: Gtk.Widget, rgba: Gdk.RGBA):
    try:
        base_color = rgba.to_string()

        hover_rgba = Gdk.RGBA()
        hover_rgba.red = rgba.red
        hover_rgba.green = rgba.green
        hover_rgba.blue = rgba.blue
        hover_rgba.alpha = min(1.0, rgba.alpha + 0.12)
        hover_color = hover_rgba.to_string()

        active_rgba = Gdk.RGBA()
        active_rgba.red = rgba.red
        active_rgba.green = rgba.green
        active_rgba.blue = rgba.blue
        active_rgba.alpha = min(1.0, rgba.alpha + 0.18)
        active_color = active_rgba.to_string()
    except Exception:
        logger.debug("Failed to convert RGBA to string", exc_info=True)
        return

    try:
        provider = Gtk.CssProvider()
        css_data = f"""
        .tinted {{
            transition: background-color 0s ease;
        }}

        .tinted:not(:selected) {{
            background-color: {base_color};
        }}

        .tinted:hover:not(:selected) {{
            background-color: {hover_color};
        }}

        .tinted:active:not(:selected) {{
            background-color: {active_color};
        }}

        /* Selected state intentionally omitted: selection uses the uniform
           accent style from the sidebar CSS (see window.py
           `.navigation-sidebar row.tinted:selected`) so every selected row
           looks the same regardless of its own color. */
        """
        provider.load_from_data(css_data.encode('utf-8'))

        if hasattr(row, '_tint_provider') and getattr(row, '_tint_provider'):
            try:
                row.get_style_context().remove_provider(row._tint_provider)
            except Exception:
                pass

        row._tint_provider = provider  # type: ignore[attr-defined]
        row.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
    except Exception:
        logger.debug("Failed to apply tinted color", exc_info=True)


def _clear_tint(row: Gtk.Widget):
    row.remove_css_class("tinted")
    provider = getattr(row, '_tint_provider', None)
    if provider:
        try:
            row.get_style_context().remove_provider(provider)
        except Exception:
            pass
        row._tint_provider = None  # type: ignore[attr-defined]


def _set_bar_card_color(row: Gtk.Widget, rgba: Gdk.RGBA):
    """Colour the row's leading accent bar.

    Painted as a no-repeat background strip (not a coloured border) so the
    bar keeps straight, square ends while the row's rounded corners stay
    intact. Vertically inset to 90% so it clears the corner curves. The
    provider is scoped to this widget's style context, same as ``.tinted``.
    """
    try:
        color = rgba.to_string()
    except Exception:
        return
    try:
        provider = Gtk.CssProvider()
        css_data = f"""
        .color-bar-colored {{
            background-image: linear-gradient({color}, {color});
            background-size: 3px 95%;
            background-position: left center;
            background-repeat: no-repeat;
        }}
        """
        provider.load_from_data(css_data.encode('utf-8'))

        old = getattr(row, '_bar_provider', None)
        if old:
            try:
                row.get_style_context().remove_provider(old)
            except Exception:
                pass

        row._bar_provider = provider  # type: ignore[attr-defined]
        row.get_style_context().add_provider(
            provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
    except Exception:
        logger.debug("Failed to apply accent bar color", exc_info=True)


def _clear_bar(row: Gtk.Widget):
    row.remove_css_class("color-bar")
    row.remove_css_class("color-bar-colored")
    provider = getattr(row, '_bar_provider', None)
    if provider:
        try:
            row.get_style_context().remove_provider(provider)
        except Exception:
            pass
        row._bar_provider = None  # type: ignore[attr-defined]


def _create_color_dot(icon_name: str = "dot-symbolic") -> Gtk.Image:
    """Colour dot shown before the row icon ('dot' mode; 'bar'-mode member rows)."""
    from sshpilot import icon_utils
    dot = icon_utils.new_image_from_icon_name(icon_name)
    dot.add_css_class("sidebar-color-dot")
    dot.set_pixel_size(16)
    dot.set_valign(Gtk.Align.CENTER)
    dot.set_visible(False)
    return dot


def _update_color_dot(row: Gtk.Widget, rgba: Optional[Gdk.RGBA]):
    """Colour and show the row's ``color_dot``, or hide it when no colour."""
    if not rgba:
        row.color_dot.set_visible(False)
        return
    css_data = f"""
    image.sidebar-color-dot {{
      color: {rgba.to_string()};
    }}
    """
    old = getattr(row, '_color_dot_provider', None)
    if old:
        try:
            row.color_dot.get_style_context().remove_provider(old)
        except Exception:
            pass
    row._color_dot_provider = Gtk.CssProvider()
    row._color_dot_provider.load_from_data(css_data.encode('utf-8'))
    row.color_dot.get_style_context().add_provider(
        row._color_dot_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )
    row.color_dot.set_visible(True)


def _avatar_initials(name: Optional[str]) -> str:
    """Two uppercase initials from a nickname ('Prod Web' -> 'PW', 'prod' -> 'PR')."""
    text = (name or '').strip()
    if not text:
        return '?'
    parts = text.split()
    if len(parts) >= 2:
        return (parts[0][:1] + parts[1][:1]).upper()
    return parts[0][:2].upper()


def _make_avatar(*, initials: Optional[str] = None, icon_name: Optional[str] = None) -> Gtk.Widget:
    """A round avatar we style ourselves: a Label of initials or a folder icon."""
    if icon_name is not None:
        from sshpilot import icon_utils
        widget = icon_utils.new_image_from_icon_name(icon_name)
        widget.set_pixel_size(16)
    else:
        widget = Gtk.Label(label=initials or '?')
    widget.add_css_class('sidebar-avatar')
    widget.set_halign(Gtk.Align.CENTER)
    widget.set_valign(Gtk.Align.CENTER)
    return widget


def _set_avatar_color(avatar: Gtk.Widget, rgba: Optional[Gdk.RGBA]):
    """Tint a group avatar's glyph (folder icon / initials) with ``rgba``.

    A provider on the widget's own style context at USER priority overrides the
    default ``.sidebar-avatar`` foreground; the circle keeps its neutral fill so
    only the glyph carries the group colour.
    """
    old = getattr(avatar, '_color_provider', None)
    if old is not None:
        try:
            avatar.get_style_context().remove_provider(old)
        except Exception:
            pass
        avatar._color_provider = None  # type: ignore[attr-defined]
    if rgba is None:
        return
    try:
        color = rgba.to_string()
    except Exception:
        return
    provider = Gtk.CssProvider()
    # Adwaita's own standalone-color derivation: clamp the group colour's Oklab
    # lightness (theme-aware) so the glyph stays legible on the neutral circle in
    # both light and dark, keeping its hue. Older GTK/libadwaita lack this, so
    # fall back to the plain group colour there (see _SUPPORTS_STANDALONE_OKLAB).
    if _SUPPORTS_STANDALONE_OKLAB:
        glyph_color = f"oklab(from {color} var(--standalone-color-oklab))"
    else:
        glyph_color = color
    provider.load_from_data(
        (
            ".sidebar-avatar {"
            f"  color: {glyph_color};"
            "}"
        ).encode("utf-8")
    )
    avatar._color_provider = provider  # type: ignore[attr-defined]
    avatar.get_style_context().add_provider(
        provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
    )


def _apply_row_color(row: Gtk.Widget, mode: str, rgba: Optional[Gdk.RGBA]):
    """Apply the selected group-color treatment to a sidebar row.

    Modes: ``fill`` (tinted card), ``badge`` (tag icon), ``bar`` (leading
    accent bar), ``dot`` (dot before the row icon). Mutually exclusive, so we
    always clear the others first. ``row`` must expose ``color_badge``,
    ``_update_color_badge`` and ``color_dot``.
    """
    _clear_tint(row)
    _clear_bar(row)
    row.color_badge.set_visible(False)
    row.color_dot.set_visible(False)

    if mode == 'dot':
        _update_color_dot(row, rgba)
        return

    if mode == 'bar':
        # Reserve the (transparent) bar on every row so coloured and uncoloured
        # rows keep the same content offset; colour it only when set. The
        # ``color-bar-colored`` marker (only on rows with an actual colour) is
        # what pins the sharp leading edge — uncoloured rows round both sides.
        row.add_css_class("color-bar")
        if rgba:
            row.add_css_class("color-bar-colored")
            _set_bar_card_color(row, rgba)
        return

    if not rgba:
        return

    if mode == 'badge':
        row._update_color_badge(rgba)
        row.color_badge.set_visible(True)
    else:  # fill
        row.add_css_class("tinted")
        _set_tint_card_color(row, _fill_rgba(rgba) or rgba)

# ---------------------------------------------------------------------------
# Row widgets
# ---------------------------------------------------------------------------


def _drag_bar_geometry(width, height):
    """Geometry for the drop bar + leading cap, given the widget size.

    Returns ``(bar_x, bar_y, bar_w, bar_h, cap_cx, cap_cy, cap_r)``. The bar is a
    horizontal pill inset from both edges; the cap is a filled node at its
    leading (left) end. Degenerate widths clamp to a non-negative bar.
    """
    bar_h = min(_DROP_BAR_THICKNESS, height)
    bar_y = max(0, (height - bar_h) / 2)
    bar_x = _DROP_BAR_INSET_LEFT
    bar_w = max(0, width - _DROP_BAR_INSET_LEFT - _DROP_BAR_INSET_RIGHT)
    cap_r = min(_DROP_BAR_CAP_RADIUS, height / 2)
    cap_cx = bar_x
    cap_cy = height / 2
    return (bar_x, bar_y, bar_w, bar_h, cap_cx, cap_cy, cap_r)


class DragIndicator(Gtk.Widget):
    """Custom widget showing the between-rows drop position.

    Draws a thick rounded accent bar with a soft glow and a round leading cap
    (the insertion caret), rather than a faint hairline. Shared by GroupRow and
    ConnectionRow; it only renders space when made visible during a drag.
    """

    def __init__(self):
        super().__init__()
        self.set_size_request(-1, _DROP_BAR_HEIGHT)
        self.set_visible(False)

    def _accent_color(self):
        """Theme accent (accent_bg_color), falling back to Adwaita blue."""
        rgba = None
        try:
            found, looked = self.get_style_context().lookup_color("accent_bg_color")
            if found:
                rgba = looked
        except Exception:
            rgba = None
        if rgba is None:
            rgba = Gdk.RGBA()
            rgba.parse(_DROP_BAR_FALLBACK_ACCENT)
        return rgba

    def do_snapshot(self, snapshot):
        width = self.get_width()
        height = self.get_height()
        if width <= 0 or height <= 0:
            return

        (bar_x, bar_y, bar_w, bar_h,
         cap_cx, cap_cy, cap_r) = _drag_bar_geometry(width, height)
        if bar_w <= 0:
            return

        accent = self._accent_color()

        bar_rect = Graphene.Rect()
        bar_rect.init(bar_x, bar_y, bar_w, bar_h)
        bar_rounded = Gsk.RoundedRect()
        bar_rounded.init_from_rect(bar_rect, bar_h / 2)

        # Pill-shaped accent bar.
        snapshot.push_rounded_clip(bar_rounded)
        snapshot.append_color(accent, bar_rect)
        snapshot.pop()

        # Round leading cap (the insertion caret node).
        if cap_r > 0:
            cap_rect = Graphene.Rect()
            cap_rect.init(cap_cx - cap_r, cap_cy - cap_r, cap_r * 2, cap_r * 2)
            cap_rounded = Gsk.RoundedRect()
            cap_rounded.init_from_rect(cap_rect, cap_r)
            snapshot.push_rounded_clip(cap_rounded)
            snapshot.append_color(accent, cap_rect)
            snapshot.pop()


class GroupRow(Gtk.ListBoxRow):
    """Row widget for group headers."""

    __gsignals__ = {
        "group-toggled": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),
    }

    def __init__(self, group_info: Dict, group_manager: GroupManager, connections_dict: Dict | None = None):
        super().__init__()
        _install_sidebar_color_css()
        config = getattr(group_manager, 'config', None)
        _apply_sidebar_row_style(self, config)
        self.group_info = group_info
        self.group_manager = group_manager
        self.group_id = group_info["id"]
        self.connections_dict = connections_dict or {}
        self._tint_provider = None
        self._color_badge_provider = None
        self._tint_provider = None
        self._color_badge_provider = None
        self._avatar = None
        self._compact = False
        self._member_rows = []
        self._child_group_rows = []

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)

        # Main content
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        # Kept so set_indentation() can offset nested group headers and honor
        # the fullwidth/nested Group Layout preference.
        self._content = content
        self._content_margin_base = 12
        self._indent_level = 0
        self._group_display_mode = None

        from sshpilot import icon_utils
        self.color_dot = _create_color_dot("big-dot-symbolic")
        content.append(self.color_dot)

        icon = icon_utils.new_image_from_icon_name("folder-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        icon.set_valign(Gtk.Align.CENTER)  # Center vertically relative to text
        config = getattr(self.group_manager, 'config', None)
        show_group_icon = config.get_setting('ui.sidebar_show_group_icon', True) if config else True
        icon.set_visible(show_group_icon)
        content.append(icon)
        self.icon = icon

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)  # Center vertically relative to icon
        self._info_box = info_box

        self.name_label = Gtk.Label()
        self.name_label.set_halign(Gtk.Align.START)
        self.name_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.name_label.set_valign(Gtk.Align.CENTER)  # Center vertically when count label is hidden
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.name_label.set_hexpand(True)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_width_chars(10)  # Minimum width
        self.name_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        info_box.append(self.name_label)

        self.count_label = Gtk.Label()
        self.count_label.set_halign(Gtk.Align.START)
        self.count_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.count_label.add_css_class("dim-label")
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.count_label.set_hexpand(True)
        self.count_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.count_label.set_width_chars(10)  # Minimum width
        self.count_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        # Set initial visibility based on preference
        config = getattr(self.group_manager, 'config', None)
        show_group_count = config.get_setting('ui.sidebar_show_group_count', True) if config else True
        self.count_label.set_visible(show_group_count)
        info_box.append(self.count_label)

        content.append(info_box)

        # Split-view button — only visible on hover
        self.split_view_button = icon_utils.new_button_from_icon_name("view-grid-symbolic")
        self.split_view_button.add_css_class("flat")
        self.split_view_button.set_tooltip_text(_("Open in Split View"))
        self.split_view_button.set_valign(Gtk.Align.CENTER)
        self.split_view_button.set_opacity(0.0)
        self.split_view_button.connect("clicked", self._on_split_view_clicked)
        content.append(self.split_view_button)

        # Edit button - only visible on hover
        # Use opacity instead of visibility to reserve space and prevent row resizing
        self.edit_button = icon_utils.new_button_from_icon_name("document-edit-symbolic")
        self.edit_button.add_css_class("flat")
        self.edit_button.add_css_class("group-edit-button")
        self.edit_button.set_tooltip_text(_("Edit Group"))
        self.edit_button.set_valign(Gtk.Align.CENTER)
        self.edit_button.set_opacity(0.0)  # Hidden by default but reserves space
        self.edit_button.connect("clicked", self._on_edit_clicked)
        content.append(self.edit_button)

        # Set up hover events to show/hide buttons
        self._setup_hover_buttons()

        self.color_badge = icon_utils.new_image_from_icon_name("tag-symbolic")
        self.color_badge.add_css_class("sidebar-color-badge")
        self.color_badge.set_icon_size(Gtk.IconSize.NORMAL)
        self.color_badge.set_valign(Gtk.Align.CENTER)
        self.color_badge.set_visible(False)
        content.append(self.color_badge)

        self.expand_button = Gtk.Button()
        icon_utils.set_button_icon(self.expand_button, "pan-end-symbolic")
        self.expand_button.add_css_class("flat")
        self.expand_button.add_css_class("group-expand-button")
        self.expand_button.set_can_focus(False)
        self.expand_button.connect("clicked", self._on_expand_clicked)
        content.append(self.expand_button)

        # Add drop target indicator (initially hidden)
        self.drop_target_indicator = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.drop_target_indicator.set_halign(Gtk.Align.CENTER)
        self.drop_target_indicator.set_margin_top(4)
        self.drop_target_indicator.set_margin_bottom(4)
        self.drop_target_indicator.add_css_class("drop-target-indicator")
        
        drop_icon = icon_utils.new_image_from_icon_name("list-add-symbolic")
        drop_icon.set_icon_size(Gtk.IconSize.NORMAL)
        self.drop_target_indicator.append(drop_icon)
        
        drop_label = Gtk.Label()
        drop_label.set_markup(_("<b>Add to Group</b>"))
        self.drop_target_indicator.append(drop_label)
        
        self.drop_target_indicator.set_visible(False)
        self.drop_target_indicator.set_can_target(False)

        main_box.append(content)
        main_box.append(self.drop_target_indicator)

        # Drop indicator (bottom)
        self.drop_indicator_bottom = DragIndicator()
        main_box.append(self.drop_indicator_bottom)

        self.set_child(main_box)
        self.set_selectable(True)
        self.set_can_focus(True)

        self._update_display()
        self._setup_drag_source()
        self._setup_double_click_gesture()

    # -- internal helpers -------------------------------------------------

    def _update_display(self):
        from sshpilot import icon_utils
        if self.group_info.get("expanded", True):
            icon_utils.set_button_icon(self.expand_button, "pan-down-symbolic")
        else:
            icon_utils.set_button_icon(self.expand_button, "pan-end-symbolic")

        actual_connections = [
            c
            for c in self.group_info.get("connections", [])
            if c in self.connections_dict
        ]
        count = len(actual_connections)
        group_name = GLib.markup_escape_text(str(self.group_info['name']))
        self.name_label.set_markup(f"<b>{group_name}</b>")
        self.count_label.set_text(_("{count} connections").format(count=count))
        self._apply_group_color_style()


    def _on_expand_clicked(self, button):
        self._toggle_expand()

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_source)
        # Store reference for cleanup
        self._drag_source = drag_source

    def _on_drag_prepare(self, source, x, y):
        # JSON string — TYPE_PYOBJECT crashes macOS pasteboard (issues #704/#876).
        return content_provider_for_payload(
            {"type": "group", "group_id": self.group_id}
        )

    def _on_drag_begin(self, source, drag):
        # macOS string payloads render as drag text unless we set an icon.
        set_internal_drag_icon(
            source, self, drag=drag, icon_name="folder-symbolic"
        )
        try:
            window = self.get_root()
            if window:
                if hasattr(window, "_dragged_connections"):
                    delattr(window, "_dragged_connections")
                # Track which group is being dragged
                window._dragged_group_id = self.group_id
                if hasattr(window, "begin_sidebar_drag_expand"):
                    window.begin_sidebar_drag_expand()
        except Exception as e:
            logger.error(f"Error in group drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
                # Clear the dragged group tracking
                if hasattr(window, "_dragged_group_id"):
                    delattr(window, "_dragged_group_id")
                _hide_ungrouped_area(window)
                # Drop/leave normally clean these; cover cancel-without-leave.
                if getattr(window, "_drag_in_progress", False):
                    _clear_drop_indicator(window)
                    window._drag_in_progress = False
                    if hasattr(window, "connection_list"):
                        window.connection_list.set_selection_mode(
                            Gtk.SelectionMode.MULTIPLE
                        )
                if hasattr(window, "end_sidebar_drag_expand"):
                    window.end_sidebar_drag_expand()
        except Exception as e:
            logger.error(f"Error in group drag end: {e}")

    def _setup_double_click_gesture(self):
        # Double-click is handled centrally by the ListBox row-activated path
        # (see MainWindow.on_connection_activated). Keeping a second row-local
        # gesture here causes duplicate toggle paths.
        pass

    def _toggle_expand(self):
        expanded = not self.group_info.get("expanded", True)
        self.group_info["expanded"] = expanded
        self.group_manager.set_group_expanded(self.group_id, expanded)
        self._update_display()
        self.emit("group-toggled", self.group_id, expanded)

    def set_indentation(self, level: int) -> None:
        """Indent a nested group header to match its depth in the tree."""
        try:
            self._indent_level = max(0, int(level or 0))
        except (TypeError, ValueError):
            self._indent_level = 0
        self._apply_group_display_mode()

    def refresh_group_display_mode(self, new_mode: Optional[str] = None) -> None:
        """Re-apply indentation when the Group Layout preference changes."""
        if new_mode:
            normalized = str(new_mode).lower()
            if normalized in _GROUP_DISPLAY_OPTIONS:
                self._group_display_mode = normalized
        else:
            self._group_display_mode = None
        self._apply_group_display_mode()

    def _get_group_display_mode(self) -> str:
        if self._group_display_mode in _GROUP_DISPLAY_OPTIONS:
            return self._group_display_mode

        mode = 'nested'
        config = getattr(self.group_manager, 'config', None)
        if config:
            try:
                value = str(config.get_setting('ui.group_row_display', mode)).lower()
                if value in _GROUP_DISPLAY_OPTIONS:
                    mode = value
            except Exception:
                pass

        self._group_display_mode = mode
        return mode

    def _apply_group_display_mode(self) -> None:
        content = getattr(self, '_content', None)
        if content is None:
            return

        base = getattr(self, '_content_margin_base', 12)
        indent_px = max(0, getattr(self, '_indent_level', 0)) * _GROUP_ROW_INDENT_WIDTH

        if indent_px <= 0:
            self.set_margin_start(0)
            content.set_margin_start(base)
            return

        if self._get_group_display_mode() == 'fullwidth':
            # Row spans full width; only the header content is indented.
            self.set_margin_start(0)
            content.set_margin_start(base + indent_px)
        else:  # nested: the whole header card shifts right
            self.set_margin_start(indent_px)
            content.set_margin_start(base)

    def add_member_row(self, row: Gtk.ListBoxRow) -> None:
        """Track a direct member row for in-place expand/collapse."""
        self._member_rows.append(row)

    def add_child_group_row(self, row: "GroupRow") -> None:
        """Track a direct child group row for in-place expand/collapse."""
        self._child_group_rows.append(row)

    def apply_descendant_visibility(self, parent_visible: bool = True) -> None:
        """Show or hide child rows without rebuilding the whole sidebar."""
        expanded = bool(self.group_info.get("expanded", True))
        descendants_visible = parent_visible and expanded

        for row in getattr(self, "_member_rows", None) or []:
            row.set_visible(descendants_visible)

        for row in getattr(self, "_child_group_rows", None) or []:
            row.set_visible(descendants_visible)
            if hasattr(row, "apply_descendant_visibility"):
                row.apply_descendant_visibility(descendants_visible)

    def _on_edit_clicked(self, button):
        """Handle edit button click"""
        try:
            window = self.get_root()
            if window and hasattr(window, 'on_edit_group_action'):
                window._context_menu_group_row = self
                window.on_edit_group_action(None, None)
        except Exception as e:
            logger.error(f"Error editing group {self.group_id}: {e}")

    def _on_split_view_clicked(self, button):
        """Open all connections in this group as a split-view tab."""
        try:
            window = self.get_root()
            if window and hasattr(window, 'on_open_group_in_split_view_action'):
                window._context_menu_group_row = self
                window.on_open_group_in_split_view_action(None, None)
        except Exception as e:
            logger.error(f"Error opening group in split view {self.group_id}: {e}")

    def _setup_hover_buttons(self):
        """Set up hover events to show/hide the split-view and edit buttons."""
        self._is_hovering_edit = False

        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect("enter", self._on_row_enter_edit)
        motion_controller.connect("leave", self._on_row_leave_edit)
        self.add_controller(motion_controller)

        for btn in (self.split_view_button, self.edit_button):
            if btn:
                mc = Gtk.EventControllerMotion()
                mc.connect("enter", self._on_button_enter_edit)
                mc.connect("leave", self._on_button_leave_edit)
                btn.add_controller(mc)

    # Keep old name as alias so existing callers don't break
    def _setup_edit_button_hover(self):
        self._setup_hover_buttons()

    def _on_row_enter_edit(self, controller, x, y):
        self._is_hovering_edit = True
        self._set_hover_buttons_opacity(1.0)

    def _on_row_leave_edit(self, controller):
        self._is_hovering_edit = False
        GLib.timeout_add(100, self._maybe_hide_edit_button)

    def _on_button_enter_edit(self, controller, x, y):
        self._is_hovering_edit = True
        self._set_hover_buttons_opacity(1.0)

    def _on_button_leave_edit(self, controller):
        self._is_hovering_edit = False
        GLib.timeout_add(100, self._maybe_hide_edit_button)

    def _set_hover_buttons_opacity(self, opacity: float) -> None:
        for btn in (self.split_view_button, self.edit_button):
            if btn:
                btn.set_opacity(opacity)

    def _maybe_hide_edit_button(self):
        if not self._is_hovering_edit:
            self._set_hover_buttons_opacity(0.0)
        return False

    def _apply_group_color_style(self):
        # Keep our own colour when set; otherwise inherit the nearest coloured
        # ancestor so nested groups read as part of their parent.
        rgba = _resolve_group_color_by_id(self.group_manager, self.group_id)
        # In the minimal strip the colour is strictly a fill on the avatar,
        # never a row treatment — expand/collapse re-runs this via
        # _update_display, which would otherwise bring the accent bar back.
        if getattr(self, '_compact', False):
            _apply_row_color(self, 'fill', None)
            if self._avatar is not None:
                _set_avatar_color(self._avatar, rgba)
            return
        config = getattr(self.group_manager, 'config', None)
        mode = _get_color_display_mode(config) if config else 'fill'
        _apply_row_color(self, mode, rgba)

    def _update_color_badge(self, rgba: Gdk.RGBA):
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        color_hex = f"#{r:02x}{g:02x}{b:02x}"

        css_data = f"""
        image.sidebar-color-badge {{
          color: {color_hex};
        }}
        """

        if self._color_badge_provider:
            try:
                self.color_badge.get_style_context().remove_provider(self._color_badge_provider)
            except Exception:
                pass

        self._color_badge_provider = Gtk.CssProvider()
        self._color_badge_provider.load_from_data(css_data.encode('utf-8'))
        self.color_badge.get_style_context().add_provider(
            self._color_badge_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # Remove any accent classes that might add background color
        for cls in ("accent-red", "accent-blue", "accent-green", "accent-orange", "accent-purple", "accent-cyan", "accent-gray"):
            self.color_badge.remove_css_class(cls)

    def show_drop_indicator(self, top: bool):
        """Show drop indicator line"""
        self.hide_drop_indicators()
        
        if top:
            self.drop_indicator_top.set_visible(True)
        else:
            self.drop_indicator_bottom.set_visible(True)
    
    def hide_drop_indicators(self):
        """Hide all drop indicator lines"""
        self.drop_indicator_top.set_visible(False)
        self.drop_indicator_bottom.set_visible(False)
        self.show_group_highlight(False)

    def show_group_highlight(self, show: bool):
        """Show/hide group highlight for 'add to group' drop indication."""
        if show:
            self.add_css_class("drop-target-group")
            self.drop_target_indicator.set_visible(True)
        else:
            self.remove_css_class("drop-target-group")
            self.drop_target_indicator.set_visible(False)

    def apply_row_style(self, flat: bool | None = None) -> None:
        config = getattr(self.group_manager, 'config', None)
        _apply_sidebar_row_style(self, config, flat=flat)

    def set_compact(self, compact: bool) -> None:
        """Collapse the group header to a folder-icon avatar, or restore it."""
        compact = bool(compact)
        self._compact = compact
        content = self._content
        if compact:
            content.set_halign(Gtk.Align.CENTER)
            # Zero both side margins so the avatar centers like connection rows
            # (an asymmetric margin shifts it off-center to the left).
            content.set_margin_start(0)
            content.set_margin_end(0)
            self.set_margin_start(0)  # flatten nested-group indentation in the strip
            self._info_box.set_visible(False)
            self.color_dot.set_visible(False)
            self.color_badge.set_visible(False)
            self.split_view_button.set_visible(False)
            self.edit_button.set_visible(False)
            self.expand_button.set_visible(False)
            self.icon.set_visible(False)
            # Round folder-icon avatar, matching the connection avatars but with
            # an icon instead of initials.
            if self._avatar is None:
                icon_name = 'folder-symbolic'
                try:
                    if isinstance(self.group_info.get('icon'), str) and self.group_info['icon']:
                        icon_name = self.group_info['icon']
                except Exception:
                    pass
                self._avatar = _make_avatar(icon_name=icon_name)
                content.prepend(self._avatar)
            self._avatar.set_visible(True)
            self.set_tooltip_text(str(self.group_info.get('name', '')))
            # The group's color goes on the avatar circle, not the row: clear
            # every row-level treatment (bar/tint/badge/dot — the bar in
            # particular reads as a full-width row) and paint the avatar.
            rgba = _resolve_group_color_by_id(self.group_manager, self.group_id)
            _apply_row_color(self, 'fill', None)
            _set_avatar_color(self._avatar, rgba)
        else:
            content.set_halign(Gtk.Align.FILL)
            content.set_margin_end(12)  # restore the base margin zeroed in compact
            if self._avatar is not None:
                self._avatar.set_visible(False)
            self._info_box.set_visible(True)
            self.split_view_button.set_visible(True)
            self.edit_button.set_visible(True)
            self.expand_button.set_visible(True)
            self.set_tooltip_text(None)
            config = getattr(self.group_manager, 'config', None)
            show_icon = config.get_setting('ui.sidebar_show_group_icon', True) if config else True
            self.icon.set_visible(show_icon)
            self._apply_group_display_mode()  # restore nested indentation
            self._update_display()  # restores count label visibility + colors


class TagGroupRow(GroupRow):
    """Virtual, read-only group row derived from connection tags.

    Synthesized from a ``make_tag_group_info`` dict at sidebar rebuild time;
    supports expand/collapse only and never mutates GroupManager.
    """

    EXPANDED_SETTING = "ui.tag_groups_expanded"

    def __init__(self, group_info: Dict, group_manager: GroupManager, connections_dict=None):
        super().__init__(group_info, group_manager, connections_dict)
        self.is_tag_group = True
        # Tag group headers always use osd style (never card/flat).
        self.remove_css_class("card")
        self.remove_css_class("navigation-sidebar")
        self.add_css_class("osd")
        self.icon.set_from_icon_name("tag-symbolic")
        # The edit button renames the tag (across all tagged connections);
        # the split-view button works as inherited — the action only reads
        # group_info['connections'] / ['name'], so a synthetic group is fine.
        self.edit_button.set_tooltip_text(_("Rename Tag"))
        if group_info.get("untagged"):
            # The Untagged section is not a real tag — nothing to rename.
            self.edit_button.set_visible(False)

    def apply_row_style(self, flat: bool | None = None) -> None:
        # Tag group headers always use osd styling.
        return

    def _setup_drag_source(self):
        # Tag groups cannot be dragged or reordered.
        self._drag_source = None

    def _setup_double_click_gesture(self):
        # Double-click already toggles via the ListBox row-activated path
        # (on_connection_activated -> _toggle_expand). The base class's own
        # click gesture would make it toggle twice — for real groups that
        # second toggle is masked by the full rebuild destroying the row, but
        # tag rows survive their in-place toggle, so the gesture must go.
        pass

    def _on_edit_clicked(self, button):
        # Rename the tag itself, not a GroupManager group (the base handler
        # routes to on_edit_group_action, which bails on synthetic ids).
        try:
            window = self.get_root()
            if window and hasattr(window, 'on_rename_tag_action'):
                window.on_rename_tag_action(self)
        except Exception as e:
            logger.error(f"Error renaming tag {self.group_id}: {e}")

    def _update_display(self):
        super()._update_display()
        name = GLib.markup_escape_text(str(self.group_info.get("name", "")))
        prefix = GLib.markup_escape_text(str(self.group_info.get("prefix", "#")))
        self.name_label.set_markup(f"<b>{prefix}{name}</b>")

    def _toggle_expand(self):
        # Persist to config, not GroupManager — the group only exists here.
        expanded = not self.group_info.get("expanded", True)
        self.group_info["expanded"] = expanded
        config = getattr(self.group_manager, "config", None)
        if config is not None:
            try:
                state = dict(config.get_setting(self.EXPANDED_SETTING, {}) or {})
                state[self.group_info.get("tag_key", "")] = expanded
                config.set_setting(self.EXPANDED_SETTING, state)
            except Exception:
                logger.debug("Failed to persist tag group state", exc_info=True)
        self._update_display()
        # Show/hide member rows in place instead of emitting group-toggled:
        # a full sidebar rebuild resets the scroll position for a frame, which
        # reads as flicker (tag groups sit near the bottom of the list).
        for row in getattr(self, "_member_rows", None) or []:
            row.set_visible(expanded)


class ConnectionRow(Gtk.ListBoxRow):
    """Row widget for connection list."""

    def __init__(
        self,
        connection: Connection,
        group_manager: GroupManager,
        config,
        file_manager_callback=None,
        display_group_id: Optional[str] = None,
        in_tag_section: bool = False,
    ):
        super().__init__()
        _install_sidebar_color_css()
        self.connection = connection
        self.group_manager = group_manager
        self.config = config
        self._group_id = display_group_id
        self._in_tag_section = in_tag_section
        _apply_sidebar_row_style(self, config, in_tag_section=in_tag_section)
        self._file_manager_callback = file_manager_callback
        self._tint_provider = None
        self._color_badge_provider = None
        self._color_dot_provider = None
        self._avatar = None
        self._compact = False
        self._indent_level = 0
        self._group_display_mode = None
        self._row_margin_base = None
        self._content_margin_base = None

        # Main container with drop indicators
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        
        # Drop indicator (top)
        self.drop_indicator_top = DragIndicator()
        main_box.append(self.drop_indicator_top)
        
        # Content container
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self._content_box = content
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        from sshpilot import icon_utils
        self.color_dot = _create_color_dot()
        content.append(self.color_dot)

        self.connection_icon = icon_utils.new_image_from_icon_name("computer-symbolic")
        self.connection_icon.set_icon_size(Gtk.IconSize.NORMAL)
        self.connection_icon.set_valign(Gtk.Align.CENTER)  # Center vertically relative to text
        # Set initial visibility based on preference
        show_connection_icon = self.config.get_setting('ui.sidebar_show_connection_icon', True)
        self.connection_icon.set_visible(show_connection_icon)
        content.append(self.connection_icon)

        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info_box.set_hexpand(True)
        info_box.set_valign(Gtk.Align.CENTER)  # Center vertically relative to icon
        self._info_box = info_box

        self.nickname_label = Gtk.Label()
        self.nickname_label.set_markup(f"<b>{connection.nickname}</b>")
        self.nickname_label.set_halign(Gtk.Align.START)
        self.nickname_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.nickname_label.set_valign(Gtk.Align.CENTER)  # Center vertically when host label is hidden
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.nickname_label.set_hexpand(True)
        self.nickname_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.nickname_label.set_width_chars(10)  # Minimum width
        self.nickname_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        self.nickname_label.set_tooltip_text(connection.nickname)
        info_box.append(self.nickname_label)

        self.host_label = Gtk.Label()
        self.host_label.set_halign(Gtk.Align.START)
        self.host_label.set_xalign(0.0)  # Left-align text within label (default is 0.5/center)
        self.host_label.add_css_class("dim-label")
        # Ellipsize when text exceeds available width
        # Per GTK4 docs: For ellipsizing labels, width-chars sets minimum width,
        # max-width-chars limits natural width. Both help control size allocation.
        # Labels with ellipsize need hexpand to fill available space and ellipsize properly.
        self.host_label.set_hexpand(True)
        self.host_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.host_label.set_width_chars(10)  # Minimum width
        self.host_label.set_max_width_chars(25)  # Maximum natural width (prevents expansion)
        self._apply_host_label_text()
        # Set initial visibility based on preference
        show_user_hostname = self.config.get_setting('ui.sidebar_show_user_hostname', True)
        self.host_label.set_visible(show_user_hostname)
        info_box.append(self.host_label)

        content.append(info_box)

        self.indicator_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.indicator_box.set_halign(Gtk.Align.CENTER)
        self.indicator_box.set_valign(Gtk.Align.CENTER)
        content.append(self.indicator_box)

        self.color_badge = icon_utils.new_image_from_icon_name("tag-symbolic")
        self.color_badge.add_css_class("sidebar-color-badge")
        self.color_badge.set_icon_size(Gtk.IconSize.NORMAL)
        self.color_badge.set_valign(Gtk.Align.CENTER)
        self.color_badge.set_visible(False)
        content.append(self.color_badge)

        # File manager button (before status icon) - only visible on hover
        # Use opacity instead of visibility to reserve space and prevent row resizing
        from sshpilot import icon_utils
        self.file_manager_button = icon_utils.new_button_from_icon_name("folder-symbolic")
        self.file_manager_button.add_css_class("flat")
        self.file_manager_button.add_css_class("file-manager-button")
        self.file_manager_button.set_tooltip_text(_("Manage Files"))
        self.file_manager_button.set_valign(Gtk.Align.CENTER)
        self.file_manager_button.set_opacity(0.0)  # Hidden by default but reserves space
        if file_manager_callback:
            self.file_manager_button.connect("clicked", self._on_file_manager_clicked)
        content.append(self.file_manager_button)
        
        # Set up hover events to show/hide button
        self._setup_file_manager_button_hover()

        from sshpilot import icon_utils
        self.status_icon = icon_utils.new_image_from_icon_name("wired-lock-none-symbolic")
        self.status_icon.set_pixel_size(16)
        # A fresh row is UNKNOWN (idle), which shows no indicator; update_status()
        # reveals and styles it once the connection has a real state.
        self.status_icon.set_visible(False)
        content.append(self.status_icon)
        
        # Now add the content to main_box
        main_box.append(content)
        
        # Drop indicator (bottom)
        self.drop_indicator_bottom = DragIndicator()
        main_box.append(self.drop_indicator_bottom)

        # Set the main box as the child directly (no pulse overlay)
        self.set_child(main_box)

        self.set_selectable(True)

        self.update_status()
        self._update_forwarding_indicators()
        self._setup_drag_source()
        self._apply_group_color_style()

    def set_display_group_id(self, group_id: Optional[str]) -> None:
        """Set which group this row is listed under and refresh its color."""
        self._group_id = group_id
        self._apply_group_color_style()

    def _on_file_manager_clicked(self, button):
        """Handle file manager button click"""
        if self._file_manager_callback:
            try:
                self._file_manager_callback(self.connection)
            except Exception as e:
                logger.error(f"Error opening file manager for {self.connection.nickname}: {e}")

    def _setup_file_manager_button_hover(self):
        """Set up hover events to show/hide file manager button"""
        # Track hover state
        self._is_hovering = False
        
        # Motion controller for the row
        motion_controller = Gtk.EventControllerMotion()
        motion_controller.connect("enter", self._on_row_enter)
        motion_controller.connect("leave", self._on_row_leave)
        self.add_controller(motion_controller)
        
        # Motion controller for the button itself (to keep it visible when hovering over button)
        if self.file_manager_button:
            button_motion_controller = Gtk.EventControllerMotion()
            button_motion_controller.connect("enter", self._on_button_enter)
            button_motion_controller.connect("leave", self._on_button_leave)
            self.file_manager_button.add_controller(button_motion_controller)

    def _on_row_enter(self, controller, x, y):
        """Show file manager button when mouse enters row"""
        self._is_hovering = True
        if self.file_manager_button and self._file_manager_callback:
            self.file_manager_button.set_opacity(1.0)

    def _on_row_leave(self, controller):
        """Hide file manager button when mouse leaves row"""
        self._is_hovering = False
        # Use a small delay to allow moving to the button
        GLib.timeout_add(100, self._maybe_hide_button)

    def _on_button_enter(self, controller, x, y):
        """Keep button visible when hovering over it"""
        self._is_hovering = True
        if self.file_manager_button:
            self.file_manager_button.set_opacity(1.0)

    def _on_button_leave(self, controller):
        """Handle mouse leaving the button"""
        self._is_hovering = False
        GLib.timeout_add(100, self._maybe_hide_button)

    def _maybe_hide_button(self):
        """Hide button if not hovering"""
        if not self._is_hovering and self.file_manager_button:
            self.file_manager_button.set_opacity(0.0)
        return False  # Don't repeat

    def show_drop_indicator(self, top: bool):
        """Show drop indicator line"""
        self.hide_drop_indicators()
        
        if top:
            self.drop_indicator_top.set_visible(True)
        else:
            self.drop_indicator_bottom.set_visible(True)
    
    def hide_drop_indicators(self):
        """Hide all drop indicator lines"""
        self.drop_indicator_top.set_visible(False)
        self.drop_indicator_bottom.set_visible(False)
    
    def set_indentation(self, level: int):
        """Set indentation level for grouped connections"""
        try:
            self._indent_level = max(0, int(level or 0))
        except Exception:
            self._indent_level = 0

        content = getattr(self, '_content_box', None)
        if not content:
            main_box = self.get_child()
            if not main_box:
                return

            top_indicator = main_box.get_first_child()
            content = top_indicator.get_next_sibling() if top_indicator else None
            if not content:
                return
            self._content_box = content

        global _DEFAULT_ROW_MARGIN_START, _DEFAULT_ROW_WIDGET_MARGIN_START

        if self._content_margin_base is None or _DEFAULT_ROW_MARGIN_START <= _MIN_VALID_MARGIN:
            _DEFAULT_ROW_MARGIN_START = content.get_margin_start()
            self._content_margin_base = _DEFAULT_ROW_MARGIN_START
        else:
            self._content_margin_base = _DEFAULT_ROW_MARGIN_START

        if self._row_margin_base is None or _DEFAULT_ROW_WIDGET_MARGIN_START <= _MIN_VALID_MARGIN:
            _DEFAULT_ROW_WIDGET_MARGIN_START = self.get_margin_start()
            if _DEFAULT_ROW_WIDGET_MARGIN_START < _MIN_VALID_MARGIN:
                _DEFAULT_ROW_WIDGET_MARGIN_START = 0
            self._row_margin_base = _DEFAULT_ROW_WIDGET_MARGIN_START
        else:
            self._row_margin_base = _DEFAULT_ROW_WIDGET_MARGIN_START

        self._apply_group_display_mode()

    def refresh_group_display_mode(self, new_mode: Optional[str] = None):
        """Refresh indentation styling when the preference changes."""
        if new_mode:
            normalized = str(new_mode).lower()
            if normalized in _GROUP_DISPLAY_OPTIONS:
                self._group_display_mode = normalized
        else:
            # Force new lookup from config
            self._group_display_mode = None

        self._apply_group_display_mode()

    def _get_group_display_mode(self) -> str:
        if self._group_display_mode in _GROUP_DISPLAY_OPTIONS:
            return self._group_display_mode

        mode = 'nested'
        config = getattr(self, 'config', None)
        if config:
            try:
                value = str(config.get_setting('ui.group_row_display', mode)).lower()
                if value in _GROUP_DISPLAY_OPTIONS:
                    mode = value
            except Exception:
                pass

        self._group_display_mode = mode
        return mode

    def _apply_group_display_mode(self):
        content = getattr(self, '_content_box', None)
        if not content:
            return

        if self._content_margin_base is None:
            self._content_margin_base = content.get_margin_start()

        if self._row_margin_base is None:
            self._row_margin_base = max(self.get_margin_start(), 0)

        indent_level = getattr(self, '_indent_level', 0)
        indent_px = max(0, indent_level) * _GROUP_ROW_INDENT_WIDTH

        mode = self._get_group_display_mode()

        if indent_px <= 0:
            self.set_margin_start(self._row_margin_base)
            content.set_margin_start(self._content_margin_base)
            return

        if mode == 'fullwidth':
            self.set_margin_start(self._row_margin_base)
            content.set_margin_start(self._content_margin_base + indent_px)
        else:  # nested mode
            self.set_margin_start(self._row_margin_base + indent_px)
            content.set_margin_start(self._content_margin_base)

    def _resolve_group_color(self) -> Optional[Gdk.RGBA]:
        # Rows listed under a virtual tag group are colorless: the color
        # belongs to the real-group context, not the tag listing.
        if getattr(self, '_in_tag_section', False):
            return None
        manager = getattr(self, 'group_manager', None)
        if not manager:
            return None

        try:
            group_id = manager.resolve_display_group_id(
                self.connection.nickname,
                getattr(self, '_group_id', None),
            )
        except Exception:
            group_id = None

        return _resolve_group_color_by_id(manager, group_id)

    def _apply_group_color_style(self):
        config = getattr(self, 'config', None)
        mode = _get_color_display_mode(config)
        try:
            color_children = bool(config.get_setting('ui.group_color_child_rows', False))
        except Exception:
            color_children = True
        rgba = self._resolve_group_color() if color_children else None
        if mode == 'bar':
            # Bar marks only group headers; member rows show a colour dot
            # before the title instead.
            _apply_row_color(self, mode, None)
            _update_color_dot(self, rgba)
            return
        _apply_row_color(self, mode, rgba)

    def _update_color_badge(self, rgba: Gdk.RGBA):
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        color_hex = f"#{r:02x}{g:02x}{b:02x}"

        css_data = f"""
        image.sidebar-color-badge {{
          color: {color_hex};
        }}
        """

        if self._color_badge_provider:
            try:
                self.color_badge.get_style_context().remove_provider(self._color_badge_provider)
            except Exception:
                pass

        self._color_badge_provider = Gtk.CssProvider()
        self._color_badge_provider.load_from_data(css_data.encode('utf-8'))
        self.color_badge.get_style_context().add_provider(
            self._color_badge_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # Remove any accent classes that might add background colors
        for cls in ("accent-red", "accent-blue", "accent-green", "accent-orange", "accent-purple", "accent-cyan", "accent-gray"):
            self.color_badge.remove_css_class(cls)

    # -- drag source ------------------------------------------------------

    def _setup_drag_source(self):
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        self.add_controller(drag_source)
        # Store reference for cleanup
        self._drag_source = drag_source

    def _on_drag_prepare(self, source, x, y):
        window = self.get_root()

        connections_payload: List[Dict[str, Optional[int | str]]] = []
        selection_order = 0

        if window and hasattr(window, "connection_list"):
            try:
                selected_rows = list(window.connection_list.get_selected_rows())
            except Exception:
                selected_rows = []

            # Only a deliberate multi-selection that includes the dragged row
            # carries the whole set. A single/incidental selection (e.g. the
            # active connection's still-highlighted row) must not tag along — so
            # dragging one row drags exactly that row.
            if not (len(selected_rows) > 1 and self in selected_rows):
                selected_rows = [self]

            seen_nicknames = set()
            for row in selected_rows:
                connection_obj = getattr(row, "connection", None)
                nickname = getattr(connection_obj, "nickname", None)
                if not nickname or nickname in seen_nicknames:
                    continue

                seen_nicknames.add(nickname)

                row_index = None
                try:
                    idx = row.get_index()
                    if isinstance(idx, int) and idx >= 0:
                        row_index = idx
                except Exception:
                    row_index = None

                connections_payload.append(
                    {
                        "nickname": nickname,
                        "index": row_index,
                        "order": selection_order,
                    }
                )
                selection_order += 1

        if not connections_payload:
            row_index = None
            try:
                idx = self.get_index()
                if isinstance(idx, int) and idx >= 0:
                    row_index = idx
            except Exception:
                row_index = None

            connections_payload.append(
                {
                    "nickname": self.connection.nickname,
                    "index": row_index,
                    "order": 0,
                }
            )

        connections_payload.sort(
            key=lambda item: (
                item.get("index") is None,
                item.get("index") if isinstance(item.get("index"), int) else item.get("order", 0),
            )
        )

        ordered_nicknames: List[str] = []
        for item in connections_payload:
            nickname = item.get("nickname")
            if isinstance(nickname, str) and nickname not in ordered_nicknames:
                ordered_nicknames.append(nickname)
            item.pop("order", None)

        data = {
            "type": "connection",
            "connection_nickname": ordered_nicknames[0] if ordered_nicknames else self.connection.nickname,
            "connection_nicknames": ordered_nicknames,
            "connections": connections_payload,
        }

        if window:
            window._dragged_connections = ordered_nicknames

        # JSON string — TYPE_PYOBJECT crashes macOS pasteboard (issues #704/#876).
        return content_provider_for_payload(data)

    def _on_drag_begin(self, source, drag):
        # macOS string payloads render as drag text unless we set an icon.
        set_internal_drag_icon(
            source, self, drag=drag, icon_name="computer-symbolic"
        )
        try:
            window = self.get_root()
            if window:
                if hasattr(window, "_dragged_group_id"):
                    delattr(window, "_dragged_group_id")
                if not hasattr(window, "_dragged_connections"):
                    window._dragged_connections = [self.connection.nickname]
                window._drag_in_progress = True
                if hasattr(window, "begin_sidebar_drag_expand"):
                    window.begin_sidebar_drag_expand()
                _show_ungrouped_area(window)
        except Exception as e:
            logger.error(f"Error in drag begin: {e}")

    def _on_drag_end(self, source, drag, delete_data):
        try:
            window = self.get_root()
            if window:
                if hasattr(window, "_dragged_connections"):
                    delattr(window, "_dragged_connections")
                window._drag_in_progress = False
                _hide_ungrouped_area(window)
                if hasattr(window, "end_sidebar_drag_expand"):
                    window.end_sidebar_drag_expand()
        except Exception as e:
            logger.error(f"Error in drag end: {e}")

    # -- display updates --------------------------------------------------

    @staticmethod
    def _install_pf_css():
        # The .pf-* indicator styles now live in the bundled style.css (loaded
        # once at startup); nothing to install here.
        return

    @staticmethod
    def _install_status_css():
        """Custom color for the failed/disconnected status icon. Uses an explicit
        red (#DC2626) instead of libadwaita's .error so it stays the same red in
        dark mode (where .error becomes a coral/orange-ish tone)."""
        try:
            display = Gdk.Display.get_default()
            if not display:
                return
            if getattr(display, "_status_css_installed", False):
                return
            provider = Gtk.CssProvider()
            css = (
                "image.conn-status-up { color: #16A34A; }"
                "image.conn-status-down { color: #DC2626; }"
            )
            provider.load_from_data(css.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
            )
            setattr(display, "_status_css_installed", True)
        except Exception:
            pass

    def _update_forwarding_indicators(self):
        self._install_pf_css()
        try:
            while self.indicator_box.get_first_child():
                self.indicator_box.remove(self.indicator_box.get_first_child())
        except Exception:
            return

        # Check preference for showing port forwarding indicators
        show_port_forwarding = self.config.get_setting('ui.sidebar_show_port_forwarding', True)
        if not show_port_forwarding:
            return

        # Forwarding badges only make sense for protocols that support it.
        from .plugins.api import Capability
        from .plugins.registry import capabilities_for
        if Capability.PORT_FORWARDING not in capabilities_for(self.connection):
            return

        # Group the connection's forwarding rules by type. The rule schema and
        # the formatting/grouping helpers live in port_utils so they can be
        # reused (e.g. a future port-mapping viewer) without pulling in GTK.
        from sshpilot import port_utils
        grouped = port_utils.group_forwarding_rules(
            getattr(self.connection, "forwarding_rules", None)
        )

        def make_badge(letter: str, cls: str, type_rules):
            from sshpilot import icon_utils
            img = icon_utils.new_image_from_icon_name(letter)  # 'L' / 'R' / 'D'
            img.set_pixel_size(16)
            img.set_halign(Gtk.Align.CENTER)
            img.set_valign(Gtk.Align.CENTER)
            # Tooltip lists each mapping of this type, capped so a connection
            # with many rules doesn't produce an unreadably tall tooltip.
            tooltip = "\n".join(port_utils.format_forwarding_rules(type_rules, max_lines=8))
            if tooltip:
                img.set_tooltip_text(tooltip)
            return img

        if grouped["local"]:
            self.indicator_box.append(make_badge("L", "pf-local", grouped["local"]))
        if grouped["remote"]:
            self.indicator_box.append(make_badge("R", "pf-remote", grouped["remote"]))
        if grouped["dynamic"]:
            self.indicator_box.append(make_badge("D", "pf-dynamic", grouped["dynamic"]))

    def _apply_host_label_text(self, include_port: bool | None = None):
        try:
            window = self.get_root()
            hide = bool(getattr(window, "_hide_hosts", False)) if window else False
        except Exception:
            hide = False

        if hide:
            self.host_label.set_text("••••••••••")
            self.host_label.set_tooltip_text('')
            return

        format_kwargs = {}
        if include_port is not None:
            format_kwargs["include_port"] = include_port

        display = _format_connection_host_display(self.connection, **format_kwargs)
        self.host_label.set_text(display or '')
        self.host_label.set_tooltip_text(display or '')

    def apply_row_style(self, flat: bool | None = None) -> None:
        _apply_sidebar_row_style(
            self, self.config, in_tag_section=self._in_tag_section, flat=flat
        )

    def apply_hide_hosts(self, hide: bool):
        self._apply_host_label_text()

    def update_status(self):
        """Render the status icon from the connection's authoritative state.

        This is render-only: it never computes or writes back connection state.
        Aggregation across multiple terminals lives in the reporting layer
        (``window._recompute_connection_state``) which sets the state via
        ``ConnectionManager.update_connection_state``.
        """
        try:
            from sshpilot import icon_utils
            from .connection_manager import ConnectionState

            try:
                state = self.connection.get_status()
            except Exception:
                # Older/foreign connection objects without the status API: if we
                # can't tell, stay neutral (UNKNOWN) rather than alarming in red.
                state = (
                    ConnectionState.CONNECTED
                    if getattr(self.connection, "is_connected", False)
                    else ConnectionState.UNKNOWN
                )

            host_value = _get_connection_host(self.connection) or _get_connection_alias(self.connection)

            # Clear any previously-applied semantic color classes before re-styling.
            self._install_status_css()
            for _cls in ("success", "warning", "error", "dim-label",
                         "conn-status-up", "conn-status-down"):
                self.status_icon.remove_css_class(_cls)

            # Idle / never connected this session: show no indicator at all.
            if state == ConnectionState.UNKNOWN:
                self.status_icon.set_visible(False)
                self.status_icon.set_tooltip_text("")
                self.status_icon.queue_draw()
                self._apply_group_color_style()
                return

            # Other states render an icon, subject to the global visibility pref.
            try:
                show_status = bool(self.config.get_setting('ui.sidebar_show_connection_status', True))
            except Exception:
                show_status = True
            self.status_icon.set_visible(show_status)

            if state == ConnectionState.CONNECTED:
                icon_utils.set_icon_from_name(self.status_icon, "wired-lock-closed-symbolic")
                self.status_icon.add_css_class("conn-status-up")
                self.status_icon.set_tooltip_text(_("Connected to {host}").format(host=host_value))
            elif state == ConnectionState.CONNECTING:
                icon_utils.set_icon_from_name(self.status_icon, "wired-lock-dots-symbolic")
                self.status_icon.add_css_class("warning")
                self.status_icon.set_tooltip_text(_("Connecting to {host}…").format(host=host_value))
            elif state == ConnectionState.FAILED:
                icon_utils.set_icon_from_name(self.status_icon, "wired-lock-none-symbolic")
                self.status_icon.add_css_class("conn-status-down")
                reason = ''
                try:
                    reason = self.connection.get_status_reason() or ''
                except Exception:
                    reason = ''
                self.status_icon.set_tooltip_text(
                    _("Connection failed: {reason}").format(reason=reason) if reason
                    else _("Connection failed")
                )
            else:  # DISCONNECTED — a previously-live session that went down.
                icon_utils.set_icon_from_name(self.status_icon, "wired-lock-none-symbolic")
                self.status_icon.add_css_class("conn-status-down")
                self.status_icon.set_tooltip_text(_("Disconnected"))

            self.status_icon.queue_draw()
        except Exception as e:
            logger.error(
                f"Error updating status for {getattr(self.connection, 'nickname', 'connection')}: {e}"
            )

        self._apply_group_color_style()
        if getattr(self, '_compact', False):
            self._refresh_compact_status()

    def _is_online(self) -> bool:
        from .connection_manager import ConnectionState
        try:
            return self.connection.get_status() == ConnectionState.CONNECTED
        except Exception:
            return bool(getattr(self.connection, 'is_connected', False))

    def _refresh_compact_status(self) -> None:
        """Restyle the compact avatar/icon for the current connection state and
        keep the group-color widgets suppressed (they don't fit the strip)."""
        online = self._is_online()
        if self._avatar is not None:
            if online:
                self._avatar.add_css_class('sidebar-avatar-online')
            else:
                self._avatar.remove_css_class('sidebar-avatar-online')
        self.connection_icon.remove_css_class('conn-status-up')
        if online:
            self.connection_icon.add_css_class('conn-status-up')
        # update_status() re-shows the status icon and colour widgets; keep them
        # hidden in the strip (this runs at the end of update_status when compact).
        self.status_icon.set_visible(False)
        self.color_dot.set_visible(False)
        self.color_badge.set_visible(False)

    def set_compact(self, compact: bool) -> None:
        """Collapse the row to a single avatar/icon (minimal sidebar) or restore.

        Idempotent for restore; when already compact, re-runs so a changed
        ``ui.sidebar_minimal_row_style`` takes effect immediately.
        """
        compact = bool(compact)
        if not compact and not getattr(self, '_compact', False):
            return
        self._compact = compact
        content = self._content_box

        if not compact:
            content.set_halign(Gtk.Align.FILL)
            content.set_margin_start(12)
            content.set_margin_end(12)
            if self._avatar is not None:
                self._avatar.set_visible(False)
            self._info_box.set_visible(True)
            self.indicator_box.set_visible(True)
            self.file_manager_button.set_visible(True)
            self.connection_icon.set_icon_size(Gtk.IconSize.NORMAL)
            self.connection_icon.remove_css_class('conn-status-up')
            try:
                self.connection_icon.set_visible(
                    bool(self.config.get_setting('ui.sidebar_show_connection_icon', True)))
            except Exception:
                self.connection_icon.set_visible(True)
            self.set_tooltip_text(None)
            self._apply_group_display_mode()  # restore nested indentation
            self.update_status()  # restores status_icon + group-color widgets
            return

        style = 'initials'
        try:
            style = str(self.config.get_setting('ui.sidebar_minimal_row_style', 'initials')).lower()
        except Exception:
            pass
        if style not in ('initials', 'icon'):
            style = 'initials'

        content.set_halign(Gtk.Align.CENTER)
        content.set_margin_start(0)
        content.set_margin_end(0)
        self.set_margin_start(0)  # flatten nested-group indentation in the strip
        self._info_box.set_visible(False)
        self.indicator_box.set_visible(False)
        self.color_badge.set_visible(False)
        self.color_dot.set_visible(False)
        self.file_manager_button.set_visible(False)
        self.status_icon.set_visible(False)
        self.set_tooltip_text(self.connection.nickname)

        if style == 'initials':
            if self._avatar is None:
                self._avatar = _make_avatar(
                    initials=_avatar_initials(self.connection.nickname))
                content.prepend(self._avatar)
            self._avatar.set_visible(True)
            self.connection_icon.set_visible(False)
            # The strip always shows the group color as a fill on the avatar,
            # regardless of the group color display mode (bar/badge/dot/fill).
            _set_avatar_color(self._avatar, self._resolve_group_color())
        else:  # icon
            if self._avatar is not None:
                self._avatar.set_visible(False)
            self.connection_icon.set_icon_size(Gtk.IconSize.LARGE)
            self.connection_icon.set_visible(True)

        self._refresh_compact_status()

    def update_display(self):
        if hasattr(self.connection, "nickname") and hasattr(self, "nickname_label"):
            self.nickname_label.set_markup(f"<b>{self.connection.nickname}</b>")
            self.nickname_label.set_tooltip_text(self.connection.nickname)

        if hasattr(self.connection, "username") and hasattr(self, "host_label"):
            self._apply_host_label_text(include_port=True)
        self._update_forwarding_indicators()
        self.update_status()
        # The above repopulate labels/indicators that the strip hides; re-apply
        # the compact layout so an edit doesn't leave the row half-expanded.
        if getattr(self, "_compact", False):
            self.set_compact(True)


# ---------------------------------------------------------------------------
# Drag-and-drop helpers
# ---------------------------------------------------------------------------


def reset_connection_list_drag_session(window) -> None:
    """Clear transient sidebar drag-and-drop state on ``window``.

    ``rebuild_connection_list()`` removes row widgets that may still be active
    drag sources, so their ``drag-end`` handlers might not run.
    """
    _clear_drop_indicator(window)
    _stop_connection_autoscroll(window)

    if getattr(window, "_ungrouped_area_visible", False):
        row = getattr(window, "_ungrouped_area_row", None)
        if row is not None and row.get_parent() is not None:
            try:
                window.connection_list.remove(row)
            except Exception:
                pass
        window._ungrouped_area_visible = False

    window._ungrouped_area_row = None

    if hasattr(window, "_dragged_group_id"):
        delattr(window, "_dragged_group_id")
    if hasattr(window, "_dragged_connections"):
        delattr(window, "_dragged_connections")

    window._drag_in_progress = False

    connection_list = getattr(window, "connection_list", None)
    if connection_list is not None:
        try:
            connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
        except Exception:
            pass

    # A drop rebuilds the list, destroying the dragged connection row before its
    # drag-end fires; collapse the drag-expanded strip here so it isn't missed
    # (no-op unless we auto-expanded). Cancels without a rebuild are handled by
    # the rows' own drag-end handlers.
    if hasattr(window, "end_sidebar_drag_expand"):
        window.end_sidebar_drag_expand()


def setup_connection_list_dnd(window):
    """Set up drag and drop for the window's connection list."""

    drop_target = new_internal_drop_target()
    drop_target.connect("drop", lambda t, v, x, y: _on_connection_list_drop(window, t, v, x, y))
    drop_target.connect("motion", lambda t, x, y: _on_connection_list_motion(window, t, x, y))
    drop_target.connect("leave", lambda t: _on_connection_list_leave(window, t))
    window.connection_list.add_controller(drop_target)

    window._drop_indicator_row = None
    window._drop_indicator_position = None
    window._drop_group_parent_id = None
    window._drop_group_index = None
    window._drop_group_tree_target_set = False
    window._drop_placeholder_row = None
    window._ungrouped_area_row = None
    window._ungrouped_area_visible = False
    window._connection_autoscroll_timeout_id = 0
    window._connection_autoscroll_velocity = 0.0
    if not hasattr(window, "_connection_autoscroll_margin"):
        window._connection_autoscroll_margin = 48.0
    if not hasattr(window, "_connection_autoscroll_max_velocity"):
        window._connection_autoscroll_max_velocity = 28.0
    if not hasattr(window, "_connection_autoscroll_interval_ms"):
        window._connection_autoscroll_interval_ms = 16


def _on_connection_list_motion(window, target, x, y):
    try:
        # Prevent row selection during drag by temporarily disabling selection
        if not hasattr(window, '_drag_in_progress'):
            window._drag_in_progress = True
            window.connection_list.set_selection_mode(Gtk.SelectionMode.NONE)

        # Throttle motion events to improve performance
        current_time = GLib.get_monotonic_time()
        if hasattr(window, '_last_motion_time'):
            if current_time - window._last_motion_time < 16000:  # ~16ms = 60fps
                return Gdk.DragAction.MOVE
        window._last_motion_time = current_time

        if getattr(window, "_dragged_connections", None):
            _show_ungrouped_area(window)
        _update_connection_autoscroll(window, y)

        row = _row_at_y_or_nearest(window, y)
        if not row:
            _clear_drop_indicator(window)
            return Gdk.DragAction.MOVE

        if getattr(row, "drop_placeholder", False):
            # Hovering the gap itself — leave it where it is (rows shift under
            # the cursor as the placeholder inserts; recomputing would thrash).
            return Gdk.DragAction.MOVE

        if getattr(row, "ungrouped_area", False):
            if hasattr(window, "_dragged_group_id"):
                _show_group_end_drop(window)
                return Gdk.DragAction.MOVE
            _clear_drop_indicator(window)
            window._drop_indicator_row = row
            window._drop_indicator_position = "ungrouped"
            return Gdk.DragAction.MOVE

        # Show indicators for valid drop targets
        if hasattr(row, "show_drop_indicator"):
            row_y = row.get_allocation().y
            row_height = row.get_allocation().height
            relative_y = y - row_y
            position = "above" if relative_y < row_height / 2 else "below"
            
            # Handle connection rows
            if hasattr(row, "connection"):
                if hasattr(window, "_dragged_group_id"):
                    # Groups reorder relative to group rows only. Root-level
                    # connections mark the end of the group section.
                    if (getattr(row, "_group_id", None) is None
                            and not getattr(row, "_in_tag_section", False)):
                        _show_group_end_drop(window)
                    else:
                        _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE
                # Member rows under virtual tag groups are not drop targets.
                if getattr(row, "_in_tag_section", False):
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE
                dragged = set(getattr(window, "_dragged_connections", []) or [])
                nickname = getattr(getattr(row, "connection", None), "nickname", None)
                if dragged and nickname in dragged:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                _show_drop_indicator(window, row, position)

            # Handle group rows
            elif (hasattr(row, "group_id")
                  and not getattr(row, "is_tag_group", False)
                  and hasattr(window, "_dragged_group_id")):
                if row.group_id == window._dragged_group_id:
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                decision = _group_into_decision(
                    window.group_manager.groups,
                    window._dragged_group_id,
                    row.group_id,
                )
                if decision == "invalid":
                    _clear_drop_indicator(window)
                    return Gdk.DragAction.MOVE

                listbox = window.connection_list
                direct_row = _listbox_row_at_y(listbox, y)
                _dragged_parent = window.group_manager.groups.get(
                    window._dragged_group_id, {}
                ).get("parent_id")

                seam = _group_reorder_seam_at_y(
                    window, y, window._dragged_group_id
                )

                if seam is None and window._drop_indicator_row is row:
                    pos = window._drop_indicator_position
                    if pos == "on_group" and direct_row is row:
                        return Gdk.DragAction.MOVE
                    # Only short-circuit an "above"/"below" indicator when the
                    # pointer is in a gap (direct_row=None). When on-row, let
                    # the display logic run so the indicator can transition from
                    # seam ("above") to nest ("on_group") as pointer moves deeper.
                    if pos in ("above", "below") and direct_row is None:
                        return Gdk.DragAction.MOVE

                # Seam has priority in gaps and during unnesting; nest wins on-row.
                if seam is not None:
                    seam_row, seam_zone = seam
                    _apply_group_reorder_indicator(window, seam_row, seam_zone)
                elif direct_row is row and decision == "nest":
                    _show_drop_indicator_on_group(window, row)
                elif direct_row is None:
                    _clear_drop_indicator(window)
                elif decision == "reorder":
                    # seam is None and pointer is on the parent row but NOT
                    # near any unnesting seam → nothing sensible to show.
                    if row.group_id == _dragged_parent:
                        _clear_drop_indicator(window)
                    else:
                        zone = _group_reorder_position_from_y(row, y, listbox)
                        _apply_group_reorder_indicator(window, row, zone)
                else:
                    zone = _group_reorder_position_from_y(row, y, listbox)
                    _apply_group_reorder_indicator(window, row, zone)
            
            # Dragging a connection onto a group row adds it to that group.
            elif (hasattr(row, "group_id")
                  and getattr(window, "_dragged_connections", None)):
                if _pointer_over_group_row(row, y, window.connection_list):
                    _show_drop_indicator_on_group(window, row)
                else:
                    _clear_drop_indicator(window)
            else:
                _clear_drop_indicator(window)
        else:
            _clear_drop_indicator(window)
        return Gdk.DragAction.MOVE
    except Exception as e:
        logger.error(f"Error handling motion: {e}")
        return Gdk.DragAction.MOVE


def _on_connection_list_leave(window, target):
    _clear_drop_indicator(window)
    _hide_ungrouped_area(window)
    _stop_connection_autoscroll(window)

    # Restore selection mode after drag
    if hasattr(window, '_drag_in_progress'):
        window._drag_in_progress = False
        window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
    
    return True


def _show_drop_indicator(window, row, position):
    try:
        # Between-rows reorder: open a real gap with the placeholder row (the
        # list parts around it — no overlap), instead of painting a line inside
        # the target row. Clear any lingering group 'into' highlight first.
        # Also when it IS the target row: a lingering "on_group" highlight on
        # the same row must drop before the gap appears.
        if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
            window._drop_indicator_row.hide_drop_indicators()
        _position_drop_placeholder(window, row, position)
    except Exception as e:
        logger.error(f"Error showing drop indicator: {e}")


def _row_at_y_or_nearest(window, y):
    """get_row_at_y, but bridge the inter-row margin gaps that return None.

    Sidebar rows carry a small vertical margin, so a cursor landing in the gap
    between two rows yields no row. Probe a few pixels either side so targeting
    stays reliable right up to the row edges.
    """
    lb = window.connection_list
    row = lb.get_row_at_y(int(y))
    if row is not None:
        return row
    for dy in (-4, 4, -8, 8):
        row = lb.get_row_at_y(int(y) + dy)
        if row is not None:
            return row
    return None


def _group_header_bounds_in_listbox(row, listbox) -> tuple:
    """Return ``(y0, y1)`` of the folder header in ``listbox`` coordinates."""
    try:
        content = getattr(row, "_content", None)
        if content is not None and listbox is not None:
            ok, _x0, y0 = content.translate_coordinates(listbox, 0, 0)
            if ok:
                h = content.get_allocation().height
                if h > 0:
                    return float(y0), float(y0 + h)
        if content is not None:
            ok, _x0, y_off = content.translate_coordinates(row, 0, 0)
            if ok:
                h = content.get_allocation().height
                if h > 0:
                    row_y = row.get_allocation().y
                    top = row_y + y_off
                    return float(top), float(top + h)
        alloc = row.get_allocation()
        return float(alloc.y), float(alloc.y + alloc.height)
    except Exception:
        alloc = row.get_allocation()
        return float(alloc.y), float(alloc.y + alloc.height)


def _row_allocation_contains_y(row, y, listbox, margin: int = 0) -> bool:
    """True when ``y`` falls inside the row allocation (± ``margin``) in listbox coords."""
    try:
        ok, _x0, y0 = row.translate_coordinates(listbox, 0, 0)
        h = row.get_allocation().height
        if not ok or h <= 0:
            alloc = row.get_allocation()
            y0, h = alloc.y, alloc.height
        return (y0 - margin) <= y < (y0 + h + margin)
    except Exception:
        return False


def _pointer_over_group_row(row, y, listbox=None) -> bool:
    """True when the pointer is on ``row`` (Gtk hit-test or allocation slack)."""
    listbox = listbox or row.get_parent()
    if listbox is None:
        return False
    try:
        if listbox.get_row_at_y(int(y)) is row:
            return True
        # CSS row margins sit outside the allocation; allow a little slack.
        return _row_allocation_contains_y(row, y, listbox, margin=6)
    except Exception:
        return False


def _listbox_row_at_y(listbox, y):
    """Row under ``y`` without gap-bridging probes."""
    try:
        return listbox.get_row_at_y(int(y))
    except Exception:
        return None


def _pointer_over_group_header(row, y, listbox=None) -> bool:
    """True when ``y`` is over the folder header content (legacy helper)."""
    listbox = listbox or row.get_parent()
    if listbox is None:
        return False
    try:
        y0, y1 = _group_header_bounds_in_listbox(row, listbox)
        return y0 <= y < y1
    except Exception:
        return False


def _group_reorder_position_from_y(row, y, listbox=None) -> str:
    """'above' / 'below' for gap reorder from list y (uses header midpoint)."""
    listbox = listbox or row.get_parent()
    try:
        y0, y1 = _group_header_bounds_in_listbox(row, listbox)
        mid = (y0 + y1) / 2
        return "above" if y < mid else "below"
    except Exception:
        return "above"


def _set_group_tree_drop_target(window, parent_id, index: int) -> None:
    window._drop_group_parent_id = parent_id
    window._drop_group_index = index
    window._drop_group_tree_target_set = True


def _tree_target_insert_before(manager, group_id: str) -> tuple:
    parent_id, idx = manager.sibling_index(group_id)
    return parent_id, idx


def _tree_target_insert_after(manager, group_id: str) -> tuple:
    parent_id, idx = manager.sibling_index(group_id)
    return parent_id, idx + 1


def _tree_target_nest_into(manager, group_id: str) -> tuple:
    return group_id, len(manager.get_ordered_siblings(group_id))


def _record_group_reorder_tree_target(window, ref_group_id: str, position: str) -> None:
    """Map a visual above/below highlight to ``(parent_id, child_index)``."""
    if not hasattr(window, "_dragged_group_id"):
        return
    manager = window.group_manager
    try:
        if position == "above":
            parent_id, index = _tree_target_insert_before(manager, ref_group_id)
        else:
            parent_id, index = _tree_target_insert_after(manager, ref_group_id)
        _set_group_tree_drop_target(window, parent_id, index)
    except ValueError:
        pass


def _sibling_group_rows(window, parent_id):
    """Group header rows sharing ``parent_id``, in sidebar list order."""
    rows = []
    for row in _iter_host_group_rows(window):
        if getattr(row, "is_tag_group", False):
            continue
        group = window.group_manager.groups.get(row.group_id)
        if group and group.get("parent_id") == parent_id:
            rows.append(row)
    return rows


def _subtree_bottom_y(row) -> float:
    """Bottom edge of a group subtree in listbox coordinates."""
    rows = _collect_group_subtree_rows(row)
    last = rows[-1] if rows else row
    alloc = last.get_allocation()
    return float(alloc.y + alloc.height)


def _group_reorder_seam_at_y(window, y, dragged_group_id):
    """If ``y`` is near a sibling seam, return ``(target_row, zone)`` for reorder.

    Checks seams at the dragged group's own level first, then walks up ancestor
    levels so dragging a nested group to a gap between its ancestor's siblings
    shows the unnesting indicator with the same generous ±16 px hit area.
    """
    dragged = window.group_manager.groups.get(dragged_group_id)
    if not dragged:
        return None

    listbox = window.connection_list
    hit = _GROUP_SEAM_HIT_PX

    def _scan(parent_id):
        siblings = _sibling_group_rows(window, parent_id)
        if len(siblings) < 2:
            return None
        for i, row in enumerate(siblings):
            if row.group_id == dragged_group_id:
                continue
            y0, _y1 = _group_header_bounds_in_listbox(row, listbox)
            if i == 0:
                if abs(y - y0) <= hit:
                    return row, "above"
            else:
                prev = siblings[i - 1]
                seam = (_subtree_bottom_y(prev) + y0) / 2.0
                if abs(y - seam) <= hit:
                    return row, "above"
            if i < len(siblings) - 1:
                nxt = siblings[i + 1]
                bottom = _subtree_bottom_y(row)
                if nxt.group_id == dragged_group_id:
                    drag_row = _find_group_row_by_id(window, dragged_group_id)
                    if drag_row is not None:
                        dy0, _ = _group_header_bounds_in_listbox(drag_row, listbox)
                        seam = (bottom + dy0) / 2.0
                        if abs(y - seam) <= hit:
                            return row, "below"
                else:
                    ny0, _ = _group_header_bounds_in_listbox(nxt, listbox)
                    seam = (bottom + ny0) / 2.0
                    if abs(y - seam) <= hit:
                        return nxt, "above"
        return None

    own_parent = dragged.get("parent_id")
    result = _scan(own_parent)
    if result is not None:
        return result

    current = own_parent
    while current is not None:
        parent_group = window.group_manager.groups.get(current)
        if not parent_group:
            break
        ancestor_parent = parent_group.get("parent_id")
        result = _scan(ancestor_parent)
        if result is not None:
            return result
        current = ancestor_parent

    return None


def _group_has_visible_children(row) -> bool:
    """True if the group row is followed by a deeper (descendant) row."""
    try:
        base = getattr(row, "_indent_level", 0)
        nxt = row.get_next_sibling()
        while nxt is not None and getattr(nxt, "drop_placeholder", False):
            nxt = nxt.get_next_sibling()
        if nxt is None or getattr(nxt, "ungrouped_area", False):
            return False
        return getattr(nxt, "_indent_level", 0) > base
    except Exception:
        return False


def _next_sibling_group_row(window, row):
    """Next group row at the same indent level, skipping descendants."""
    try:
        base = getattr(row, "_indent_level", 0)
        nxt = row.get_next_sibling()
        while nxt is not None:
            if getattr(nxt, "drop_placeholder", False):
                nxt = nxt.get_next_sibling()
                continue
            if getattr(nxt, "ungrouped_area", False):
                return None
            level = getattr(nxt, "_indent_level", 0)
            if level < base:
                return None
            if (level == base
                    and hasattr(nxt, "group_id")
                    and not getattr(nxt, "is_tag_group", False)):
                return nxt
            nxt = nxt.get_next_sibling()
    except Exception:
        pass
    return None


def _last_root_group_row(window):
    """Last top-level (indent 0) group row in the list, or None."""
    last = None
    child = window.connection_list.get_first_child()
    while child is not None:
        if (hasattr(child, "group_id")
                and not getattr(child, "is_tag_group", False)
                and not getattr(child, "drop_placeholder", False)
                and getattr(child, "_indent_level", 0) == 0):
            last = child
        child = child.get_next_sibling()
    return last


def _is_last_root_group(window, row):
    """True if row is the last top-level (indent 0) group in the list."""
    return (getattr(row, "_indent_level", 0) == 0
            and _last_root_group_row(window) is row)


def _group_section_end_index(window):
    """List index where the group section ends — first root connection or ungrouped row."""
    idx = 0
    child = window.connection_list.get_first_child()
    while child is not None:
        if getattr(child, "drop_placeholder", False):
            child = child.get_next_sibling()
            continue
        if getattr(child, "ungrouped_area", False):
            return idx
        if (hasattr(child, "connection")
                and getattr(child, "_group_id", None) is None
                and not getattr(child, "_in_tag_section", False)):
            return idx
        idx += 1
        child = child.get_next_sibling()
    return idx


def _show_group_end_drop(window):
    """Place the gap at the end of the group section."""
    last = _last_root_group_row(window)
    if last is None or last.group_id == getattr(window, "_dragged_group_id", None):
        _clear_drop_indicator(window)
        return
    placeholder = _create_drop_placeholder(window)
    if (placeholder.get_parent() is not None
            and window._drop_indicator_row is last
            and window._drop_indicator_position == "below"):
        return
    if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
        window._drop_indicator_row.hide_drop_indicators()
    if placeholder.get_parent() is not None:
        window.connection_list.remove(placeholder)
    window.connection_list.insert(placeholder, _group_section_end_index(window))
    window._drop_indicator_row = last
    window._drop_indicator_position = "below"
    if hasattr(window, "_dragged_group_id"):
        manager = window.group_manager
        parent_id, index = _tree_target_insert_after(manager, last.group_id)
        _set_group_tree_drop_target(window, parent_id, index)


def _apply_group_reorder_indicator(window, row, zone: str) -> None:
    """Show a reorder gap for ``zone`` ('above' / 'below'), mapping expanded-group seams."""
    if zone == "below" and _group_has_visible_children(row):
        if _is_last_root_group(window, row):
            _show_group_end_drop(window)
            return
        nxt = _next_sibling_group_row(window, row)
        if nxt is not None:
            _show_drop_indicator(window, nxt, "above")
        else:
            _clear_drop_indicator(window)
    else:
        _show_drop_indicator(window, row, zone)


def _group_into_decision(groups, dragged_id, target_id) -> str:
    """How to treat an 'into' hover when dragging ``dragged_id`` over ``target_id``.

    Returns ``"invalid"`` (target is the dragged group or a descendant — would
    create a cycle), ``"reorder"`` (dragged is already a direct child of target,
    so nesting is a no-op — reorder instead), or ``"nest"`` (a real nest).
    """
    if not dragged_id or not target_id or target_id == dragged_id:
        return "invalid"
    # Walk target's ancestry: if we reach the dragged group, target is a
    # descendant and nesting would loop.
    cur = target_id
    seen = set()
    while cur is not None and cur not in seen:
        if cur == dragged_id:
            return "invalid"
        seen.add(cur)
        cur = groups.get(cur, {}).get("parent_id")
    if groups.get(dragged_id, {}).get("parent_id") == target_id:
        return "reorder"
    return "nest"


def _show_drop_indicator_on_group(window, row):
    """Show a special indicator when dropping a connection onto a group (adds to group)"""
    try:
        # Only update if the indicator has changed
        if (window._drop_indicator_row != row or
            window._drop_indicator_position != "on_group"):

            # A gap and the 'Add to Group' highlight must never show together.
            _remove_drop_placeholder(window)

            # Clear any existing indicators
            if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
                window._drop_indicator_row.hide_drop_indicators()

            # Show group highlight indicator instead of line indicators
            if hasattr(row, 'show_group_highlight'):
                row.show_group_highlight(True)
            elif hasattr(row, 'show_drop_indicator'):
                # Fallback: show bottom indicator if group highlight not available
                row.show_drop_indicator(False)

            window._drop_indicator_row = row
            window._drop_indicator_position = "on_group"
            if hasattr(window, "_dragged_group_id"):
                manager = window.group_manager
                parent_id, index = _tree_target_nest_into(manager, row.group_id)
                _set_group_tree_drop_target(window, parent_id, index)
    except Exception as e:
        logger.error(f"Error showing group drop indicator: {e}")


def _create_ungrouped_area(window):
    if window._ungrouped_area_row:
        return window._ungrouped_area_row

    ungrouped_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)

    from sshpilot import icon_utils
    icon = icon_utils.new_image_from_icon_name("folder-open-symbolic")
    icon.set_pixel_size(24)
    icon.add_css_class("dim-label")

    label = Gtk.Label(label=_("Drop connections here to ungroup them"))
    label.add_css_class("dim-label")
    label.add_css_class("caption")

    ungrouped_area.append(icon)
    ungrouped_area.append(label)

    ungrouped_row = Gtk.ListBoxRow()
    ungrouped_row.set_child(ungrouped_area)
    ungrouped_row.set_selectable(False)
    ungrouped_row.set_activatable(False)
    ungrouped_row.ungrouped_area = True

    window._ungrouped_area_row = ungrouped_row
    return ungrouped_row


def _placeholder_insert_index(target_index, position):
    """List index where the insertion placeholder goes for a given target.

    'above' lands at the target's own slot; 'below' one past it.
    """
    return target_index if position == "above" else target_index + 1


def _create_drop_placeholder(window):
    """Cached slim placeholder row that opens a gap between rows during a drag.

    Mirrors Nautilus's reorder placeholder: a real, non-interactive list row
    inserted at the drop position so the list parts around it (clean gap, no
    overlap). Its visible content is the accent-bar DragIndicator.
    """
    if getattr(window, "_drop_placeholder_row", None) is not None:
        return window._drop_placeholder_row

    bar = DragIndicator()
    bar.set_visible(True)  # only ever in the tree mid-drag

    placeholder = Gtk.ListBoxRow()
    placeholder.set_child(bar)
    placeholder.set_selectable(False)
    placeholder.set_activatable(False)
    placeholder.add_css_class("drop-placeholder-row")
    placeholder.set_size_request(-1, _DROP_BAR_HEIGHT + 2)
    placeholder.drop_placeholder = True

    window._drop_placeholder_row = placeholder
    return placeholder


def _position_drop_placeholder(window, target_row, position):
    """Insert/move the gap placeholder relative to ``target_row``.

    Records the real target row + position for the WYSIWYG drop; the placeholder
    itself is purely visual.
    """
    try:
        placeholder = _create_drop_placeholder(window)

        # Already showing the same gap → nothing to do (avoids churn/flicker).
        if (placeholder.get_parent() is not None
                and window._drop_indicator_row is target_row
                and window._drop_indicator_position == position):
            return

        # Remove first so target_row.get_index() reflects the natural layout
        # (the placeholder may currently sit above the target). Same as Nautilus.
        if placeholder.get_parent() is not None:
            window.connection_list.remove(placeholder)

        target_index = target_row.get_index()
        if target_index < 0:
            return
        insert_index = _placeholder_insert_index(target_index, position)

        window.connection_list.insert(placeholder, insert_index)
        window._drop_indicator_row = target_row
        window._drop_indicator_position = position
        if (hasattr(window, "_dragged_group_id")
                and hasattr(target_row, "group_id")
                and position in ("above", "below")):
            _record_group_reorder_tree_target(
                window, target_row.group_id, position
            )
    except Exception as e:
        logger.error(f"Error positioning drop placeholder: {e}")


def _remove_drop_placeholder(window):
    """Remove the gap placeholder from the list (keeping the cached widget)."""
    try:
        placeholder = getattr(window, "_drop_placeholder_row", None)
        if placeholder is not None and placeholder.get_parent() is not None:
            window.connection_list.remove(placeholder)
    except Exception as e:
        logger.error(f"Error removing drop placeholder: {e}")


def _show_ungrouped_area(window):
    try:
        if window._ungrouped_area_visible:
            return

        hierarchy = window.group_manager.get_group_hierarchy()
        if not hierarchy:
            return

        ungrouped_row = _create_ungrouped_area(window)
        window.connection_list.append(ungrouped_row)
        window._ungrouped_area_visible = True
    except Exception as e:
        logger.error(f"Error showing ungrouped area: {e}")


def _hide_ungrouped_area(window):
    try:
        if not getattr(window, "_ungrouped_area_visible", False):
            return

        row = getattr(window, "_ungrouped_area_row", None)
        if row is not None and row.get_parent() is not None:
            window.connection_list.remove(row)
        window._ungrouped_area_visible = False
    except Exception as e:
        logger.error(f"Error hiding ungrouped area: {e}")


def _clear_drop_indicator(window):
    try:
        _remove_drop_placeholder(window)
        if window._drop_indicator_row and hasattr(window._drop_indicator_row, 'hide_drop_indicators'):
            window._drop_indicator_row.hide_drop_indicators()

        window._drop_indicator_row = None
        window._drop_indicator_position = None
        window._drop_group_parent_id = None
        window._drop_group_index = None
        window._drop_group_tree_target_set = False
    except Exception as e:
        logger.error(f"Error clearing drop indicator: {e}")
        _remove_drop_placeholder(window)
        window._drop_indicator_row = None
        window._drop_indicator_position = None
        window._drop_group_parent_id = None
        window._drop_group_index = None
        window._drop_group_tree_target_set = False


def _sidebar_allows_inplace_dnd(window) -> bool:
    """True when the full hierarchy is shown without an active search/tag filter."""
    if getattr(window, "_tag_filter", None):
        return False
    search_entry = getattr(window, "search_entry", None)
    if search_entry is not None:
        try:
            if search_entry.get_text().strip():
                return False
        except Exception:
            pass
    return True


def _iter_host_group_rows(window):
    """Yield real (non-tag) group rows in the connection list."""
    connection_list = getattr(window, "connection_list", None)
    if connection_list is None:
        return
    child = connection_list.get_first_child()
    while child:
        if hasattr(child, "group_id") and not getattr(child, "is_tag_group", False):
            yield child
        child = child.get_next_sibling()


def _find_group_row_by_id(window, group_id: Optional[str]):
    if not group_id:
        return None
    for row in _iter_host_group_rows(window):
        if row.group_id == group_id:
            return row
    return None


def _collect_group_subtree_rows(group_row) -> List[Gtk.ListBoxRow]:
    """Return a group header and every descendant row in list order."""
    rows: List[Gtk.ListBoxRow] = [group_row]
    for member in getattr(group_row, "_member_rows", None) or []:
        rows.append(member)
    for child in getattr(group_row, "_child_group_rows", None) or []:
        rows.extend(_collect_group_subtree_rows(child))
    return rows


def _listbox_reposition_row(listbox, row, insert_index: int) -> None:
    if row.get_parent() is not None:
        listbox.remove(row)
    listbox.insert(row, insert_index)


def _detach_connection_member_rows(window, nickname: str, *, except_group_id=None) -> None:
    """Remove ``nickname`` from every group's tracked member rows."""
    for group_row in _iter_host_group_rows(window):
        if except_group_id is not None and group_row.group_id == except_group_id:
            continue
        members = getattr(group_row, "_member_rows", None) or []
        group_row._member_rows = [
            row for row in members
            if getattr(getattr(row, "connection", None), "nickname", None) != nickname
        ]


def _connection_has_single_row(window, nickname: str) -> bool:
    manager = getattr(window, "connection_manager", None)
    if manager is None or not hasattr(manager, "find_connection_by_nickname"):
        return False
    connection = manager.find_connection_by_nickname(nickname)
    if connection is None:
        return False
    rows = window._rows_for_connection(connection) if hasattr(window, "_rows_for_connection") else []
    return len(rows) == 1


def _sync_group_member_rows(window, group_id: str) -> bool:
    """Reorder / re-home member rows for one group without a full rebuild."""
    group_row = _find_group_row_by_id(window, group_id)
    if group_row is None:
        return False

    group = window.group_manager.groups.get(group_id)
    if not group:
        return False

    desired_nicks = list(group.get("connections", []))
    indent_level = getattr(group_row, "_indent_level", 0) + 1
    row_by_nick: Dict[str, Gtk.ListBoxRow] = {}

    for nick in desired_nicks:
        if not _connection_has_single_row(window, nick):
            return False
        connection = window.connection_manager.find_connection_by_nickname(nick)
        row = window._rows_for_connection(connection)[0]
        row_by_nick[nick] = row
        if hasattr(row, "set_display_group_id"):
            row.set_display_group_id(group_id)
        if hasattr(row, "set_indentation"):
            row.set_indentation(indent_level)

    for nick in desired_nicks:
        _detach_connection_member_rows(window, nick, except_group_id=group_id)

    insert_at = group_row.get_index() + 1
    ordered_rows = []
    for nick in desired_nicks:
        row = row_by_nick[nick]
        ordered_rows.append(row)
        if row.get_index() != insert_at:
            current_idx = row.get_index()
            # Removing the row shifts all subsequent indices down by 1, so
            # when the row sits before insert_at the effective target is T-1.
            effective = insert_at - 1 if current_idx < insert_at else insert_at
            _listbox_reposition_row(window.connection_list, row, effective)
            insert_at = effective + 1
        else:
            insert_at += 1

    group_row._member_rows = ordered_rows
    return True


def _root_connections_start_index(window) -> int:
    """List index where ungrouped (root) connection rows begin."""
    insert_at = 0
    root_group_ids = sorted(
        (
            gid
            for gid, group in window.group_manager.groups.items()
            if group.get("parent_id") is None
        ),
        key=lambda gid: window.group_manager.groups[gid].get("order", 0),
    )
    for gid in root_group_ids:
        group_row = _find_group_row_by_id(window, gid)
        if group_row is None:
            return -1
        for subtree_row in _collect_group_subtree_rows(group_row):
            insert_at = max(insert_at, subtree_row.get_index() + 1)
    return insert_at


def _sync_root_connection_rows(window) -> bool:
    """Reorder ungrouped connection rows at the end of the hosts list."""
    nicknames = list(window.group_manager.root_connections)
    start_index = _root_connections_start_index(window)
    if start_index < 0:
        return False

    ordered_rows = []
    for nick in nicknames:
        if not _connection_has_single_row(window, nick):
            return False
        connection = window.connection_manager.find_connection_by_nickname(nick)
        row = window._rows_for_connection(connection)[0]
        _detach_connection_member_rows(window, nick)
        if hasattr(row, "set_display_group_id"):
            row.set_display_group_id(None)
        if hasattr(row, "set_indentation"):
            row.set_indentation(0)
        ordered_rows.append(row)

    insert_at = start_index
    for row in ordered_rows:
        if row.get_index() != insert_at:
            _listbox_reposition_row(window.connection_list, row, insert_at)
        insert_at += 1
    return True


def _apply_child_group_order(window, parent_id: str) -> bool:
    """Reorder child group subtrees under ``parent_id`` to match GroupManager."""
    parent_row = _find_group_row_by_id(window, parent_id)
    if parent_row is None:
        return False

    parent_group = window.group_manager.groups.get(parent_id)
    if not parent_group:
        return False

    child_ids = list(parent_group.get("children", []))
    insert_at = parent_row.get_index() + 1
    for member in getattr(parent_row, "_member_rows", None) or []:
        insert_at = max(insert_at, member.get_index() + 1)

    ordered_child_rows = []
    for child_id in child_ids:
        child_row = _find_group_row_by_id(window, child_id)
        if child_row is None:
            return False
        ordered_child_rows.append(child_row)
        for subtree_row in _collect_group_subtree_rows(child_row):
            if subtree_row.get_index() != insert_at:
                _listbox_reposition_row(window.connection_list, subtree_row, insert_at)
            insert_at += 1

    parent_row._child_group_rows = ordered_child_rows
    return True


def _apply_root_group_order(window) -> bool:
    """Reorder top-level group subtrees to match GroupManager."""
    root_ids = sorted(
        (
            gid
            for gid, group in window.group_manager.groups.items()
            if group.get("parent_id") is None
        ),
        key=lambda gid: window.group_manager.groups[gid].get("order", 0),
    )

    insert_at = 0
    for gid in root_ids:
        group_row = _find_group_row_by_id(window, gid)
        if group_row is None:
            return False
        for subtree_row in _collect_group_subtree_rows(group_row):
            if subtree_row.get_index() != insert_at:
                _listbox_reposition_row(window.connection_list, subtree_row, insert_at)
            insert_at += 1
    return True


def _groups_needing_member_resync(window, nicknames: List[str]) -> set:
    """Return group ids whose member rows may be stale after a connection move."""
    needed = set()
    nick_set = set(nicknames)
    for group_row in _iter_host_group_rows(window):
        group_id = group_row.group_id
        group = window.group_manager.groups.get(group_id, {})
        group_nicks = set(group.get("connections", []))
        member_nicks = {
            getattr(getattr(row, "connection", None), "nickname", None)
            for row in (getattr(group_row, "_member_rows", None) or [])
        }
        if group_nicks & nick_set or member_nicks & nick_set:
            needed.add(group_id)
    return needed


def _apply_connection_dnd_in_place(window, connection_nicknames: List[str]) -> bool:
    if not _sidebar_allows_inplace_dnd(window):
        return False
    if not connection_nicknames:
        return False

    for nick in connection_nicknames:
        if not _connection_has_single_row(window, nick):
            return False

    groups_to_sync = _groups_needing_member_resync(window, connection_nicknames)
    needs_root_sync = any(
        window.group_manager.get_connection_group(nick) is None
        for nick in connection_nicknames
    )

    for group_id in groups_to_sync:
        if not _sync_group_member_rows(window, group_id):
            return False
    if needs_root_sync and not _sync_root_connection_rows(window):
        return False
    return True


def _apply_group_dnd_in_place(
    window,
    source_group_id: str,
    *,
    nested: bool,
    reparented: bool,
) -> bool:
    if nested or reparented:
        return False
    if not _sidebar_allows_inplace_dnd(window):
        return False

    source = window.group_manager.groups.get(source_group_id)
    if not source:
        return False

    parent_id = source.get("parent_id")
    if parent_id:
        return _apply_child_group_order(window, parent_id)
    return _apply_root_group_order(window)


def _on_connection_list_drop(window, target, value, x, y):
    try:
        # Capture what motion last highlighted before clearing it, so the drop
        # performs exactly the action the user saw (WYSIWYG). Re-deriving the
        # target/zone from y here is unreliable: autoscroll and the mid-drag
        # ungrouped-area row shift allocations between the last motion and drop.
        indicator_row = getattr(window, "_drop_indicator_row", None)
        indicator_pos = getattr(window, "_drop_indicator_position", None)
        drop_parent_id = getattr(window, "_drop_group_parent_id", None)
        drop_index = getattr(window, "_drop_group_index", None)
        drop_tree_target_set = getattr(window, "_drop_group_tree_target_set", False)
        _clear_drop_indicator(window)
        _hide_ungrouped_area(window)
        _stop_connection_autoscroll(window)

        # Restore selection mode after drag
        if hasattr(window, '_drag_in_progress'):
            window._drag_in_progress = False
            window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)

        # Pasteboard-safe JSON string (or bare dict in tests).
        value = decode_dnd_payload(value)
        if not isinstance(value, dict):
            return False

        drop_type = value.get("type")
        changes_made = False
        group_nested = False
        group_reparented = False
        connection_nicknames_applied: List[str] = []
        tag_drop = False

        if drop_type == "connection":
            connection_nicknames: List[str] = []

            payload = value.get("connections")
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        nickname = item.get("nickname")
                        if isinstance(nickname, str) and nickname not in connection_nicknames:
                            connection_nicknames.append(nickname)

            if not connection_nicknames:
                raw_list = value.get("connection_nicknames")
                if isinstance(raw_list, list):
                    for nickname in raw_list:
                        if isinstance(nickname, str) and nickname not in connection_nicknames:
                            connection_nicknames.append(nickname)

            if not connection_nicknames:
                nickname = value.get("connection_nickname")
                if isinstance(nickname, str):
                    connection_nicknames.append(nickname)

            if connection_nicknames:
                # Act on what motion highlighted (the placeholder gap / 'Add to
                # Group' highlight), not a fresh y recompute — keeps the drop
                # consistent with what the user saw. Fall back to a hit-test only
                # when there was no prior motion.
                target_row = indicator_row
                if target_row is None or target_row.get_parent() is None:
                    target_row = window.connection_list.get_row_at_y(int(y))
                # Reorder position for connection targets; group/tag targets use
                # 'on_group' (move into / add tag), which is not above/below.
                position = indicator_pos if indicator_pos in ("above", "below") else "below"

                if not target_row:
                    for nickname in connection_nicknames:
                        window.group_manager.move_connection(nickname, None)
                        changes_made = True
                    connection_nicknames_applied = list(connection_nicknames)
                elif getattr(target_row, "ungrouped_area", False) or indicator_pos == "ungrouped":
                    for nickname in connection_nicknames:
                        window.group_manager.move_connection(nickname, None)
                        changes_made = True
                    connection_nicknames_applied = list(connection_nicknames)
                else:
                    if getattr(target_row, "is_tag_group", False):
                        # Dropping onto a tag group adds the tag to the dragged
                        # connections (copy semantics — GroupManager untouched;
                        # its synthetic id must never reach move_connection).
                        if target_row.group_info.get("untagged"):
                            # The Untagged section is not a tag to apply.
                            return False
                        from .tag_groups import add_tag_to_list
                        tag_name = str(target_row.group_info.get("name", ""))
                        cfg = getattr(window, "config", None)
                        if not tag_name or cfg is None:
                            return False
                        for nickname in connection_nicknames:
                            tags, changed = add_tag_to_list(
                                cfg.get_connection_tags(nickname), tag_name
                            )
                            if changed:
                                cfg.set_connection_tags(nickname, tags)
                                changes_made = True
                        if changes_made:
                            tag_drop = True
                    elif hasattr(target_row, "group_id"):
                        target_group_id = target_row.group_id

                        if position == "above":
                            first_connection = None
                            child = window.connection_list.get_first_child()
                            while child:
                                if hasattr(child, 'connection'):
                                    connection_group = window.group_manager.get_connection_group(child.connection.nickname)
                                    if connection_group == target_group_id:
                                        first_connection = child.connection.nickname
                                        break
                                child = child.get_next_sibling()

                            if first_connection:
                                for nickname in connection_nicknames:
                                    current_group_id = window.group_manager.get_connection_group(nickname)
                                    if current_group_id != target_group_id:
                                        window.group_manager.move_connection(nickname, target_group_id)
                                        changes_made = True
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, first_connection, "above"
                                    )
                                    first_connection = nickname
                                    changes_made = True
                            else:
                                for nickname in connection_nicknames:
                                    if window.group_manager.get_connection_group(nickname) != target_group_id:
                                        window.group_manager.move_connection(nickname, target_group_id)
                                        changes_made = True
                        else:
                            for nickname in connection_nicknames:
                                if window.group_manager.get_connection_group(nickname) != target_group_id:
                                    window.group_manager.move_connection(nickname, target_group_id)
                                    changes_made = True
                        if changes_made:
                            connection_nicknames_applied = list(connection_nicknames)
                    else:
                        # Member rows under virtual tag groups are not drop
                        # targets (a drop here would move the connection into
                        # the reference row's real group, which reads as
                        # "dropped into the tag group").
                        if getattr(target_row, "_in_tag_section", False):
                            return False
                        target_connection = getattr(target_row, "connection", None)
                        if target_connection:
                            reference_nickname = target_connection.nickname
                            target_group_id = window.group_manager.get_connection_group(reference_nickname)

                            for nickname in connection_nicknames:
                                current_group_id = window.group_manager.get_connection_group(nickname)
                                if current_group_id != target_group_id:
                                    window.group_manager.move_connection(nickname, target_group_id)
                                    changes_made = True

                            if position == "above":
                                reference = reference_nickname
                                for nickname in reversed(connection_nicknames):
                                    if nickname == reference:
                                        continue
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, reference, "above"
                                    )
                                    reference = nickname
                                    changes_made = True
                            else:
                                reference = reference_nickname
                                for nickname in connection_nicknames:
                                    if nickname == reference:
                                        continue
                                    window.group_manager.reorder_connection_in_group(
                                        nickname, reference, "below"
                                    )
                                    reference = nickname
                                    changes_made = True
                            if changes_made:
                                connection_nicknames_applied = list(connection_nicknames)

        elif drop_type == "group":
            group_id = value.get("group_id")
            group_id_applied = None
            if group_id:
                # Prefer the row motion last highlighted; only fall back to a
                # fresh hit-test when there was no prior motion (rare).
                if (indicator_row is not None
                        and hasattr(indicator_row, "group_id")
                        and not getattr(indicator_row, "is_tag_group", False)):
                    target_row = indicator_row
                else:
                    target_row = _row_at_y_or_nearest(window, y)
                    indicator_pos = None  # stale relative to this row; recompute below

                if (target_row and hasattr(target_row, "group_id")
                        and not getattr(target_row, "is_tag_group", False)):
                    target_group_id = target_row.group_id
                    if target_group_id != group_id:
                        if target_group_id in window.group_manager.groups:
                            manager = window.group_manager
                            source_group = manager.groups.get(group_id)

                            if indicator_pos == "on_group":
                                if (source_group
                                        and source_group.get("parent_id") == target_group_id):
                                    pass
                                else:
                                    parent_id, index = _tree_target_nest_into(
                                        manager, target_group_id
                                    )
                                    old_parent = (
                                        source_group.get("parent_id")
                                        if source_group else None
                                    )
                                    if manager.place_group(group_id, parent_id, index):
                                        changes_made = True
                                        group_nested = True
                                        group_reparented = old_parent != parent_id
                            elif drop_tree_target_set:
                                old_parent = (
                                    source_group.get("parent_id")
                                    if source_group else None
                                )
                                if manager.place_group(
                                    group_id, drop_parent_id, drop_index
                                ):
                                    changes_made = True
                                    if old_parent != drop_parent_id:
                                        group_reparented = True
                                    else:
                                        group_id_applied = group_id
                            elif indicator_pos in ("above", "below"):
                                if indicator_pos == "above":
                                    parent_id, index = _tree_target_insert_before(
                                        manager, target_group_id
                                    )
                                else:
                                    parent_id, index = _tree_target_insert_after(
                                        manager, target_group_id
                                    )
                                old_parent = (
                                    source_group.get("parent_id")
                                    if source_group else None
                                )
                                if manager.place_group(group_id, parent_id, index):
                                    changes_made = True
                                    if old_parent != parent_id:
                                        group_reparented = True
                                    else:
                                        group_id_applied = group_id
                        else:
                            logger.warning(f"Target group '{target_group_id}' does not exist")

        # Reflect model changes in the list without a full rebuild when possible.
        if changes_made:
            applied = False
            try:
                if drop_type == "connection" and connection_nicknames_applied and not tag_drop:
                    applied = _apply_connection_dnd_in_place(
                        window, connection_nicknames_applied
                    )
                elif drop_type == "group" and group_id_applied:
                    applied = _apply_group_dnd_in_place(
                        window,
                        group_id_applied,
                        nested=group_nested,
                        reparented=group_reparented,
                    )
            except Exception:
                logger.debug("In-place DnD update failed; rebuilding list", exc_info=True)
                applied = False
            if not applied:
                window.rebuild_connection_list()
            return True

        return False
    except Exception as e:
        logger.error(f"Error handling drop: {e}")
        return False


def _get_target_group_at_position(window, x, y):
    try:
        row = window.connection_list.get_row_at_y(int(y))
        if row and hasattr(row, "group_id"):
            return row.group_id
        elif row and hasattr(row, "connection"):
            connection = row.connection
            return window.group_manager.get_connection_group(connection.nickname)
        return None
    except Exception:
        return None


def _update_connection_autoscroll(window, y):
    """Update autoscroll velocity based on pointer position within the viewport."""
    scrolled = getattr(window, "connection_scrolled", None)
    if not scrolled:
        _stop_connection_autoscroll(window)
        return

    allocation = scrolled.get_allocation()
    height = allocation.height
    if height <= 0:
        _stop_connection_autoscroll(window)
        return

    margin = max(1.0, min(getattr(window, "_connection_autoscroll_margin", 48.0), height / 2))
    max_velocity = max(1.0, getattr(window, "_connection_autoscroll_max_velocity", 28.0))

    vadjustment = scrolled.get_vadjustment()
    adjustment_value = vadjustment.get_value() if vadjustment else 0.0
    viewport_y = max(0.0, min(height, y - adjustment_value))

    top_threshold = margin
    bottom_threshold = height - margin

    velocity = 0.0
    if viewport_y < top_threshold:
        distance = top_threshold - viewport_y
        velocity = -_calculate_autoscroll_velocity(distance, margin, max_velocity)
    elif viewport_y > bottom_threshold:
        distance = viewport_y - bottom_threshold
        velocity = _calculate_autoscroll_velocity(distance, margin, max_velocity)

    if velocity:
        _start_connection_autoscroll(window, velocity)
    else:
        _stop_connection_autoscroll(window)


def _calculate_autoscroll_velocity(distance, margin, max_velocity):
    """Scale the autoscroll velocity based on how deep the pointer is in the margin."""
    ratio = min(1.0, max(0.0, distance) / margin)
    return max_velocity * ratio


def _start_connection_autoscroll(window, velocity):
    """Ensure an autoscroll timeout is active with the requested velocity."""
    window._connection_autoscroll_velocity = float(velocity)

    timeout_id = getattr(window, "_connection_autoscroll_timeout_id", 0)
    if timeout_id:
        return

    interval = max(10, int(getattr(window, "_connection_autoscroll_interval_ms", 16)))

    def _step():
        return _connection_autoscroll_step(window)

    window._connection_autoscroll_timeout_id = GLib.timeout_add(interval, _step)


def _stop_connection_autoscroll(window):
    """Cancel any active autoscroll timeout and reset state."""
    timeout_id = getattr(window, "_connection_autoscroll_timeout_id", 0)
    if timeout_id:
        GLib.source_remove(timeout_id)
    window._connection_autoscroll_timeout_id = 0
    window._connection_autoscroll_velocity = 0.0


def _connection_autoscroll_step(window):
    scrolled = getattr(window, "connection_scrolled", None)
    if not scrolled:
        window._connection_autoscroll_timeout_id = 0
        window._connection_autoscroll_velocity = 0.0
        return False

    velocity = getattr(window, "_connection_autoscroll_velocity", 0.0)
    if not velocity:
        window._connection_autoscroll_timeout_id = 0
        return False

    adjustment = scrolled.get_vadjustment()
    if not adjustment:
        window._connection_autoscroll_timeout_id = 0
        window._connection_autoscroll_velocity = 0.0
        return False

    lower = adjustment.get_lower()
    upper = adjustment.get_upper() - adjustment.get_page_size()
    current = adjustment.get_value()

    if upper < lower:
        upper = lower

    new_value = max(lower, min(upper, current + velocity))

    if new_value != current:
        adjustment.set_value(new_value)

    # Keep the timeout running as long as velocity remains set
    if getattr(window, "_connection_autoscroll_velocity", 0.0):
        return True

    window._connection_autoscroll_timeout_id = 0
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _expand_toolbar_button(button: Gtk.Widget) -> Gtk.Widget:
    """Give a sidebar toolbar control an equal share of the row width."""
    button.set_hexpand(True)
    button.set_halign(Gtk.Align.FILL)
    return button


def _horizontal_clip(child: Gtk.Widget) -> Gtk.ScrolledWindow:
    """Wrap ``child`` so it can be allocated narrower than its content width.

    The header/toolbar strips are fixed-height rows of buttons whose min width
    would otherwise force the sidebar wide, defeating the collapse-to-strip
    animation. A ScrolledWindow with EXTERNAL horizontal policy and
    ``min_content_width == 0`` requests no minimum width and clips its child
    (no scrollbar), while NEVER vertical policy keeps the row's natural height.
    """
    clip = Gtk.ScrolledWindow()
    # NEVER at rest so the strip fills the sidebar width (homogeneous buttons
    # spread). The window flips this to EXTERNAL during the expand animation
    # (see _set_sidebar_clipping) so the buttons clip instead of forcing width.
    clip.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
    clip.set_min_content_width(0)
    clip.set_propagate_natural_height(True)
    clip.set_hexpand(True)
    clip.set_child(child)
    return clip


def _build_sidebar_header(window, sidebar_box):
    """Build the sidebar action header (add/search/filter/sort/menu)."""
    # Sidebar header
    header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    header.set_hexpand(True)
    header.set_homogeneous(True)
    header.set_margin_start(12)
    header.set_margin_end(12)
    header.set_margin_top(12)
    header.set_margin_bottom(6)
    
    # # Title
    # title_label = Gtk.Label()
    # title_label.set_markup('<b>Connections</b>')
    # title_label.set_halign(Gtk.Align.START)
    # title_label.set_hexpand(True)
    # header.append(title_label)
    
    # Add connection button
    from sshpilot import icon_utils
    add_button = icon_utils.new_button_from_icon_name('list-add-symbolic')
    add_button.add_css_class('flat')
    _expand_toolbar_button(add_button)
    add_button.set_tooltip_text(
        _('Add Connection ({shortcut}+Shift+N)').format(
            shortcut=get_primary_modifier_label())
    )
    add_button.connect('clicked', window.on_add_connection_clicked)
    try:
        add_button.set_can_focus(False)
    except Exception:
        pass
    header.append(add_button)

    # Search button
    window.search_button = icon_utils.new_button_from_icon_name('system-search-symbolic')
    window.search_button.add_css_class('flat')
    _expand_toolbar_button(window.search_button)
    # Platform-aware shortcut in tooltip
    shortcut = 'Cmd+F' if is_macos() else 'Ctrl+F'
    window.search_button.set_tooltip_text(
        _('Search Connections ({shortcut})').format(shortcut=shortcut))
    window.search_button.connect('clicked', lambda *_: window.focus_search_entry())
    try:
        window.search_button.set_can_focus(False)
    except Exception:
        pass
    header.append(window.search_button)

    # Hide/Show hostnames button (eye icon)
    def _update_eye_icon(btn):
        try:
            icon = 'view-conceal-symbolic' if window._hide_hosts else 'view-reveal-symbolic'
            icon_utils.set_button_icon(btn, icon)
            btn.set_tooltip_text(_('Show hostnames') if window._hide_hosts else _('Hide hostnames'))
        except Exception:
            pass

    hide_button = icon_utils.new_button_from_icon_name('view-reveal-symbolic')
    hide_button.add_css_class('flat')
    _expand_toolbar_button(hide_button)
    _update_eye_icon(hide_button)
    def _on_toggle_hide(btn):
        try:
            window._hide_hosts = not window._hide_hosts
            # Persist setting
            try:
                window.config.set_setting('ui.hide_hosts', window._hide_hosts)
            except Exception:
                pass
            # Update all rows
            for rows in window.connection_rows.values():
                for row in (rows if isinstance(rows, list) else [rows]):
                    if hasattr(row, 'apply_hide_hosts'):
                        row.apply_hide_hosts(window._hide_hosts)
            # Update icon/tooltip
            _update_eye_icon(btn)
        except Exception:
            pass
    hide_button.connect('clicked', _on_toggle_hide)
    try:
        hide_button.set_can_focus(False)
    except Exception:
        pass
    header.append(hide_button)

    # Tag filter dropdown: pick a tag to show only connections carrying it.
    tag_button = Gtk.MenuButton()
    tag_button.add_css_class('flat')
    _expand_toolbar_button(tag_button)
    tag_button.set_icon_name('tag-symbolic')
    tag_button.set_tooltip_text(_('Filter by tag'))

    filter_action = Gio.SimpleAction.new_stateful(
        'filter-tag', GLib.VariantType.new('s'), GLib.Variant('s', '')
    )

    def _on_filter_tag(action, param):
        try:
            action.set_state(param)
            window._tag_filter = param.get_string() or None
            window.rebuild_connection_list()
        except Exception:
            logger.error("Failed to apply tag filter", exc_info=True)

    filter_action.connect('activate', _on_filter_tag)
    window.add_action(filter_action)

    def _build_tag_menu(btn):
        # Rebuilt on every popup so new/renamed tags always show.
        try:
            menu = Gio.Menu()
            all_item = Gio.MenuItem.new(_('All Connections'), None)
            all_item.set_action_and_target_value(
                'win.filter-tag', GLib.Variant('s', '')
            )
            menu.append_item(all_item)

            tag_map = {}
            for conn in window.connection_manager.get_connections():
                try:
                    tag_map[conn.nickname] = window.config.get_connection_tags(conn.nickname)
                except Exception:
                    pass
            tags_section = Gio.Menu()
            for display_tag, nicknames in compute_tag_groups(tag_map):
                item = Gio.MenuItem.new(
                    f'{display_tag} ({len(nicknames)})', None
                )
                item.set_action_and_target_value(
                    'win.filter-tag', GLib.Variant('s', display_tag.casefold())
                )
                tags_section.append_item(item)
            menu.append_section(None, tags_section)
            btn.set_menu_model(menu)
        except Exception:
            logger.error("Failed to build tag filter menu", exc_info=True)

    tag_button.set_create_popup_func(_build_tag_menu)
    try:
        tag_button.set_can_focus(False)
    except Exception:
        pass
    header.append(tag_button)

    sort_button = window._build_sort_button()
    _expand_toolbar_button(sort_button)
    header.append(sort_button)

    preferences_button = window._build_preferences_button()
    _expand_toolbar_button(preferences_button)
    header.append(preferences_button)

    # Menu button (packed on content header bar in setup_content_area)
    window.menu_button = Gtk.MenuButton()
    window.menu_button.add_css_class('flat')
    window.menu_button.set_can_focus(False)
    # MenuButton uses set_icon_name() which goes through icon theme
    # We'll use set_icon_name() - the icon theme should find our bundled icon
    window.menu_button.set_icon_name('open-menu-symbolic')
    window.menu_button.set_tooltip_text(_('Menu'))
    window.menu_button.set_menu_model(window.create_menu())

    header_handle = Gtk.WindowHandle()
    header_handle.set_hexpand(True)
    header_handle.set_child(header)
    window._sidebar_header_handle = header_handle
    # Clip so the button row can't force the sidebar wider than the strip.
    window._sidebar_header_clip = _horizontal_clip(header_handle)
    sidebar_box.append(window._sidebar_header_clip)

def _build_sidebar_search(window, sidebar_box):
    """Build the collapsible connection search entry."""
    # Search container
    search_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    search_container.add_css_class('search-container')
    search_container.set_margin_start(2)
    search_container.set_margin_end(2)
    search_container.set_margin_bottom(6)
    
    # Search entry for filtering connections
    window.search_entry = Gtk.SearchEntry()
    window.search_entry.set_placeholder_text(_('Search connections'))
    window.search_entry.connect('search-changed', window.on_search_changed)
    window.search_entry.connect('stop-search', window.on_search_stopped)
    search_key = Gtk.EventControllerKey()
    # Use the capture phase so Down/Up/Enter are handled before the
    # SearchEntry's internal text widget consumes them (otherwise arrow
    # keys move the cursor and Enter triggers default activation).
    search_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    search_key.connect('key-pressed', window._on_search_entry_key_pressed)
    window.search_entry.add_controller(search_key)
    # Prevent search entry from being the default focus widget
    window.search_entry.set_can_focus(True)
    window.search_entry.set_focus_on_click(False)
    search_container.append(window.search_entry)
    
    # Store reference to search container for showing/hiding
    window.search_container = search_container
    
    # Hide search container by default
    search_container.set_visible(False)
    
    sidebar_box.append(search_container)

def _create_sidebar_connection_list(window, sidebar_box):
    """Create the connection ListBox and wire selection/DnD."""
    # Connection list
    window.connection_scrolled = Gtk.ScrolledWindow()
    # NEVER horizontally at rest so rows fit and ellipsize to the sidebar width.
    # During the expand animation the window flips this to EXTERNAL (see
    # _set_sidebar_clipping) so full-width rows can be clipped instead of
    # forcing the sidebar wide; it flips back when the animation finishes.
    window.connection_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    window.connection_scrolled.set_min_content_width(0)
    window.connection_scrolled.set_vexpand(True)
    window.connection_scrolled.set_hexpand(True)
    
    window.connection_list = Gtk.ListBox()
    window.connection_list.add_css_class("navigation-sidebar")
    window.connection_list.set_hexpand(True)
    window.connection_list.set_selection_mode(Gtk.SelectionMode.MULTIPLE)
    try:
        window.connection_list.set_can_focus(True)
    except Exception:
        pass
    
    
    # Connect signals
    window.connection_list.connect('row-selected', window.on_connection_selected)  # For button sensitivity
    window.connection_list.connect('row-activated', window.on_connection_activated)  # For Enter key/double-click

    # GTK auto-hides the window's focus-visible state after a few seconds of
    # keyboard inactivity, which makes a keyboard-selected connection row
    # look deselected (the focus ring vanishes) even though it still holds
    # focus and selection. Re-assert it while focus stays in the list.
    window.connect('notify::focus-visible', window._on_focus_visible_changed)

    # Arrow Up from the first row hops back to the search entry (capture
    # phase so we intercept before the ListBox's own boundary handling).
    nav_key = Gtk.EventControllerKey()
    nav_key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    nav_key.connect('key-pressed', window._on_connection_list_nav_key)
    window.connection_list.add_controller(nav_key)
    
    # Make sure the connection list is focusable and can receive key events
    window.connection_list.set_focusable(True)
    window.connection_list.set_can_focus(True)
    # Manage focus manually so double-click activation can hand control to the terminal
    window.connection_list.set_focus_on_click(False)
    window.connection_list.set_activate_on_single_click(False)  # Require double-click to activate
    
    # Set connection list as the default focus widget for the sidebar
    # Queue this operation to avoid race conditions during startup
    def _set_sidebar_focus():
        if window.connection_list.get_parent() == sidebar_box:
            sidebar_box.set_focus_child(window.connection_list)
    
    window._queue_focus_operation(_set_sidebar_focus)
    
    # Set up drag and drop for reordering
    setup_connection_list_dnd(window)

def _attach_connection_list_context_menu(window):
    """Attach right-click context menu and middle-click open handlers."""
    # Right-click context menu using simple gesture without coordinate detection
    try:
        # Use a simple gesture but avoid all coordinate-based operations
        context_click = Gtk.GestureClick()
        context_click.set_button(Gdk.BUTTON_SECONDARY)  # Only handle right-click
        # Capture phase so this gesture handles the right-click before the
        # ListBox's own row handling.
        context_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _build_and_show_menu(row):
            # Build a Gtk.PopoverMenu from the shared sidebar context menu helper.
            # Reset any batch-target snapshot from a previous menu.
            window._context_menu_connections = None
            menu = IconContextMenu()

            def _on_popover_closed(popover, *_):
                # Only clear context state if this is still the active
                # popover — a newer right-click may have already replaced
                # it. Always unparent so popovers don't accumulate.
                if getattr(window, '_context_menu_popover', None) is popover:
                    window._context_menu_popover = None
                    window._context_menu_row = None
                    window._context_menu_connection = None
                    window._context_menu_connections = None
                try:
                    popover.unparent()
                except Exception:
                    pass

            if getattr(row, 'is_tag_group', False):
                # Virtual tag groups: rename the tag or open members in
                # split view — no edit/delete/run (nothing to mutate).
                # The Untagged section is not a real tag: no rename.
                untagged = bool(getattr(row, 'group_info', {}).get('untagged'))
                menu.add_section(
                    None if untagged else menu.add_item('document-edit-symbolic', _('Rename Tag…'), lambda: window.on_rename_tag_action(row)),
                    menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: window._open_tag_group_split(row)),
                )
            elif hasattr(row, 'group_id'):
                menu.add_section(
                    menu.add_item('document-edit-symbolic', _('Edit Group'), lambda: window.on_edit_group_action(None, None)),
                    menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: window.on_open_group_in_split_view_action(None, None)),
                    menu.add_item('utilities-terminal-symbolic', _('Run Command…'), lambda: window.on_run_command_action()),
                    menu.add_item('user-trash-symbolic', _('Delete Group'), lambda: window.on_delete_group_action(None, None)),
                )
            else:
                conn = getattr(row, 'connection', None)
                # The right-click gesture has already collapsed the
                # selection to the clicked row unless it was part of a
                # multi-selection, so the selection reflects intent here.
                # Dedupe by connection: the same connection may be selected
                # through several rows (real group + tag group).
                try:
                    selected_conns = window._connections_from_rows(
                        window._get_selected_connection_rows()
                    )
                except Exception:
                    selected_conns = [conn] if conn else []
                multi = len(selected_conns) > 1
                # Snapshot the targets for the lifetime of this menu so
                # batch actions operate on exactly what was selected when
                # the menu opened, even if the selection changes before
                # the item callback runs.
                window._context_menu_connections = list(selected_conns) if multi else None

                # Protocol capabilities decide which per-host actions make
                # sense (all-capable for SSH, narrower for plugin protocols).
                conn_caps = capabilities_for(conn) if conn else frozenset()
                all_remote_command = bool(selected_conns) and all(
                    Capability.REMOTE_COMMAND in capabilities_for(c)
                    for c in selected_conns
                )

                if multi:
                    # Multi-selection: only actions that operate on all
                    # selected connections; per-host dialogs are hidden.
                    menu.add_section(
                        menu.add_item('list-add-symbolic', _('Open New Connections'), lambda: window.on_open_new_connection_action(None, None)),
                        menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: window.on_open_in_split_view_action(None, None)),
                        menu.add_item('utilities-terminal-symbolic', _('Run Command on Hosts…'), lambda: window.on_run_command_action()) if all_remote_command else None,
                    )
                else:
                    menu.add_section(
                        menu.add_item('list-add-symbolic', _('Open New Connection'), lambda: window.on_open_new_connection_action(None, None)),
                        menu.add_item('document-edit-symbolic', _('Edit Connection'), lambda: window.on_edit_connection_action(None, None)),
                        menu.add_item('view-grid-symbolic', _('Open in Split View'), lambda: window.on_open_in_split_view_action(None, None)),
                        menu.add_item('utilities-terminal-symbolic', _('Run Command on Host…'), lambda: window.on_run_command_action()) if Capability.REMOTE_COMMAND in conn_caps else None,
                        menu.add_item('edit-copy-symbolic', _('Duplicate Connection'), lambda: window.on_duplicate_connection_action(None, None)),
                        menu.add_item('edit-copy-symbolic', _('Copy Address'), lambda: window._copy_connection_address()),
                    )

                def _has_wol_mac(c):
                    try:
                        meta = window.config.get_connection_meta(c.nickname) if c else {}
                        return bool((meta or {}).get('wol_mac', '').strip())
                    except Exception:
                        return False

                wol_item = None
                if any(_has_wol_mac(c) for c in (selected_conns or [conn])):
                    wol_item = menu.add_item('network-wireless-symbolic', _('Wake on LAN'), lambda: window.on_wake_on_lan_action(None, None))
                if multi:
                    menu.add_section(wol_item)
                else:
                    menu.add_section(
                        menu.add_item('folder-symbolic', _('Manage Files'), lambda: window.on_manage_files_action(None, None)) if (Capability.FILE_TRANSFER in conn_caps and not should_hide_file_manager_options()) else None,
                        menu.add_item('dialog-password-symbolic', _('Copy Key to Server'), lambda: window.on_copy_key_to_server_action(None, None)) if Capability.KEY_DEPLOYMENT in conn_caps else None,
                        menu.add_item('dialog-password-symbolic', _('Manage authorized_keys…'), lambda: window.on_manage_authorized_keys_action(None, None)) if Capability.KEY_DEPLOYMENT in conn_caps else None,
                        wol_item,
                        # System terminal rides build_native_command(), an SSH-only path.
                        menu.add_item('utilities-terminal-symbolic', _('Open in System Terminal'), lambda: window.on_open_in_system_terminal_action(None, None)) if (getattr(conn, 'protocol', 'ssh') == 'ssh' and not should_hide_external_terminal_options()) else None,
                    )

                def _conn_groups(c):
                    try:
                        return window.group_manager.get_connection_groups(c.nickname) if c else []
                    except Exception:
                        return []

                current_groups = _conn_groups(conn)
                any_grouped = any(_conn_groups(c) for c in selected_conns) if multi else bool(current_groups)
                row_group_id = getattr(row, '_group_id', None)
                ungroup_label = _('Remove from Group') if (not multi and row_group_id and len(current_groups) > 1) else _('Ungroup')
                menu.add_section(
                    menu.add_item('folder-symbolic', _('Move to Group'), lambda: window.on_move_to_group_action(None, None)),
                    menu.add_item('list-add-symbolic', _('Copy to Group'), lambda: window.on_copy_to_group_action(None, None)),
                    menu.add_item('edit-undo-symbolic', ungroup_label, lambda: window.on_move_to_ungrouped_action(None, None)) if any_grouped else None,
                )

                try:
                    pin_targets = selected_conns if multi else ([conn] if conn else [])
                    all_pinned = bool(pin_targets) and all(
                        window.config.is_pinned(c.nickname) for c in pin_targets
                    )
                    if all_pinned:
                        menu.add_section(
                            menu.add_item('starred-symbolic', _('Unpin from Start Page'), lambda: window._toggle_pin_connections(pin_targets)),
                        )
                    else:
                        menu.add_section(
                            menu.add_item('non-starred-symbolic', _('Pin to Start Page'), lambda: window._toggle_pin_connections(pin_targets)),
                        )
                except Exception:
                    pass

                # Plugin-contributed connection actions (single SSH host),
                # e.g. "Docker Console". The menu is rebuilt per right-click,
                # so actions registered at activate time appear here.
                try:
                    if not multi and conn and getattr(conn, 'protocol', 'ssh') == 'ssh':
                        ph = getattr(window, 'plugin_host', None)
                        actions = ph.ui.connection_actions() if ph is not None else []
                        if actions:
                            nick = getattr(conn, 'nickname', '')
                            menu.add_section(*[
                                menu.add_item(
                                    a.icon_name or 'application-x-executable-symbolic',
                                    a.label,
                                    lambda cb=a.callback, nk=nick: cb(nk),
                                )
                                for a in actions
                            ])
                except Exception:
                    logger.debug("Failed to add plugin connection actions", exc_info=True)

                menu.add_section(
                    menu.add_item('user-trash-symbolic', _('Delete'), lambda: window.on_delete_connection_action(None, None)),
                )

            popover = menu.show(row, on_closed=_on_popover_closed)
            window._context_menu_popover = popover

            # Disable the autohide modal grab. An autohide popover grabs all
            # input while open, so the next right-click on another row is
            # swallowed to dismiss this popover and never reaches our gesture
            # (a "dead click"). Without the grab, every right-click reaches the
            # handler, which closes this menu and opens the next one in a
            # single click. We handle dismissal ourselves (see below).
            try:
                popover.set_autohide(False)
            except Exception:
                pass

            # Escape closes the menu (autohide normally provides this).
            try:
                key_ctrl = Gtk.EventControllerKey()

                def _on_menu_key(_c, keyval, _code, _state):
                    if keyval == Gdk.KEY_Escape:
                        popover.popdown()
                        return True
                    return False

                key_ctrl.connect('key-pressed', _on_menu_key)
                popover.add_controller(key_ctrl)
            except Exception:
                pass

        def _on_right_click(gesture, n_press, x, y):
            try:
                logger.debug("Simple right-click detected - showing context menu for selected row")

                # Try to detect the clicked row, but fall back to selected row if detection fails
                row = window._pick_connection_list_row(x, y)
                if row is not None:
                    logger.debug("Using clicked row for context menu")

                # Fallback to selected row if click detection failed
                if not row:
                    try:
                        row = window.connection_list.get_selected_row()
                        if row:
                            logger.debug("Using currently selected row for context menu (fallback)")
                        else:
                            # If no selection, use first row
                            first_visible = window.connection_list.get_row_at_index(0)
                            if first_visible:
                                row = first_visible
                                logger.debug("Using first row for context menu (no selection)")
                    except Exception as e:
                        logger.debug(f"Failed to get selected row: {e}")

                if not row:
                    logger.debug("No row available for context menu")
                    return

                # Claim the event sequence so the right-click stops here and
                # is not also processed by the ListBox's own handling.
                try:
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                except Exception:
                    pass

                # Dismiss and detach any context menu still open from a
                # previous right-click before showing the new one. The menu is
                # non-autohide, so this right-click reaches us instead of being
                # swallowed to close the old popover, letting us replace it in a
                # single click.
                prev_popover = getattr(window, '_context_menu_popover', None)
                if prev_popover is not None:
                    window._context_menu_popover = None
                    try:
                        prev_popover.popdown()
                    except Exception:
                        pass
                    try:
                        if prev_popover.get_parent() is not None:
                            prev_popover.unparent()
                    except Exception:
                        pass

                # Highlight the right-clicked row so the UI reflects which
                # connection the context menu applies to. Mirror standard
                # file-manager behavior: right-clicking a row that isn't part
                # of the current selection selects just that row; right-
                # clicking within an existing multi-selection preserves it.
                try:
                    already_selected = row in window.connection_list.get_selected_rows()
                except Exception:
                    already_selected = False
                if not already_selected:
                    window._select_only_row(row)

                # Move the keyboard focus (focus ring) to the right-clicked row
                # too, otherwise it stays on a previously keyboard-focused row.
                try:
                    row.grab_focus()
                except Exception:
                    pass

                # Set context menu data
                window._context_menu_row = row
                window._context_menu_connection = getattr(row, 'connection', None)
                # Safe for tag rows too: the only consumer that acts on a
                # synthetic tag id is split view (which we want); edit/
                # delete/run all bail on groups.get('tag::…') -> None.
                window._context_menu_group_row = row if hasattr(row, 'group_id') else None

                _build_and_show_menu(row)

            except Exception as e:
                logger.error(f"Failed to create context menu: {e}")
        
        context_click.connect('pressed', _on_right_click)
        window.connection_list.add_controller(context_click)

        # Because the context menu is non-autohide (see _build_and_show_menu),
        # it no longer dismisses itself when the user clicks away. Close it on
        # any primary/middle press elsewhere in the window so it behaves like a
        # normal context menu. Presses on the popover's own menu items land on
        # a separate surface and do not reach this controller, so selecting an
        # item still works. Runs in the capture phase but never claims the
        # event, so normal click handling proceeds.
        def _dismiss_context_menu_on_press(gesture, n_press, x, y):
            pop = getattr(window, '_context_menu_popover', None)
            if pop is None:
                return
            window._context_menu_popover = None
            try:
                pop.popdown()
            except Exception:
                pass
            try:
                if pop.get_parent() is not None:
                    pop.unparent()
            except Exception:
                pass

        dismiss_click = Gtk.GestureClick()
        dismiss_click.set_button(0)  # any button
        dismiss_click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def _on_dismiss_press(gesture, n_press, x, y):
            # The right-click handler manages replacing the menu itself; only
            # dismiss here for non-secondary buttons.
            try:
                if gesture.get_current_button() == Gdk.BUTTON_SECONDARY:
                    return
            except Exception:
                pass
            _dismiss_context_menu_on_press(gesture, n_press, x, y)

        dismiss_click.connect('pressed', _on_dismiss_press)
        window.add_controller(dismiss_click)

        middle_click = Gtk.GestureClick()
        middle_click.set_button(Gdk.BUTTON_MIDDLE)

        def _on_middle_click(gesture, n_press, x, y):
            if n_press != 1:
                return


            row = window._pick_connection_list_row(x, y)

            if not row:
                try:
                    row = window.connection_list.get_selected_row()
                except Exception:
                    row = None

            if not row or not hasattr(row, 'connection'):
                return

            previous_row = getattr(window, '_context_menu_row', None)
            previous_connection = getattr(window, '_context_menu_connection', None)
            previous_connections = getattr(window, '_context_menu_connections', None)

            try:
                window._context_menu_row = row
                window._context_menu_connection = row.connection
                # Middle-click targets only the clicked row; a multi-select
                # snapshot from a still-open context menu must not win.
                window._context_menu_connections = None
                window.on_open_new_connection_action(None, None)
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            finally:
                window._context_menu_row = previous_row
                window._context_menu_connection = previous_connection
                window._context_menu_connections = previous_connections

        middle_click.connect('pressed', _on_middle_click)
        window.connection_list.add_controller(middle_click)
    except Exception:
        pass

def _attach_connection_list_shortcuts(window):
    """Attach Ctrl/Cmd+Enter shortcut to open selected connections."""
    # Add keyboard controller for Ctrl/⌘+Enter to open new connection
    try:
        key_controller = Gtk.ShortcutController()
        key_controller.set_scope(Gtk.ShortcutScope.LOCAL)
        
        def _on_ctrl_enter(widget, *args):
            try:
                window._open_new_connection_tabs(
                    window._connections_from_rows(
                        window._get_selected_connection_rows()
                    )
                )
            except Exception as e:
                logger.error(
                    f"Failed to open new connection with {get_primary_modifier_label()}+Enter: {e}"
                )
            return True
        
        trigger = '<Meta>Return' if is_macos() else '<Primary>Return'
        
        key_controller.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string(trigger),
            Gtk.CallbackAction.new(_on_ctrl_enter)
        ))
        
        window.connection_list.add_controller(key_controller)
    except Exception as e:
        logger.debug(
            f"Failed to add {get_primary_modifier_label()}+Enter shortcut: {e}"
        )

def _build_sidebar_toolbar(window, sidebar_box):
    """Build connection and group toolbars at the bottom of the sidebar."""
    # Sidebar toolbar
    toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    toolbar.set_hexpand(True)
    toolbar.set_margin_start(6)
    toolbar.set_margin_end(6)
    toolbar.set_margin_top(6)
    toolbar.set_margin_bottom(6)
    toolbar.add_css_class('toolbar')
    try:
        # Expose the computed visual height so terminal banners can match
        min_h, nat_h, min_baseline, nat_baseline = toolbar.measure(Gtk.Orientation.VERTICAL, -1)
        window._toolbar_row_height = max(min_h, nat_h)
        # Also track the real allocated height dynamically
        def _on_toolbar_alloc(widget, allocation):
            try:
                window._toolbar_row_height = allocation.height
            except Exception:
                pass
        toolbar.connect('size-allocate', _on_toolbar_alloc)
    except Exception:
        window._toolbar_row_height = 36
    
    # Import icon_utils for toolbar buttons
    from sshpilot import icon_utils
    
    # Connection toolbar buttons
    window.connection_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    window.connection_toolbar.set_hexpand(True)
    window.connection_toolbar.set_homogeneous(True)

    # Edit button
    window.edit_button = icon_utils.new_button_from_icon_name('document-edit-symbolic')
    window.edit_button.add_css_class('flat')
    _expand_toolbar_button(window.edit_button)
    window.edit_button.set_tooltip_text(_('Edit Connection'))
    window.edit_button.set_sensitive(False)
    window.edit_button.connect('clicked', window.on_edit_connection_clicked)
    window.connection_toolbar.append(window.edit_button)

    # Copy key to server button (ssh-copy-id)
    window.copy_key_button = icon_utils.new_button_from_icon_name('dialog-password-symbolic')
    window.copy_key_button.add_css_class('flat')
    _expand_toolbar_button(window.copy_key_button)
    window.copy_key_button.set_tooltip_text(
        _('Copy public key to server for passwordless login ({shortcut}+Shift+K)').format(
            shortcut=get_primary_modifier_label())
    )
    window.copy_key_button.set_sensitive(False)
    window.copy_key_button.connect('clicked', window.on_copy_key_to_server_clicked)
    window.connection_toolbar.append(window.copy_key_button)

    # SCP transfer button
    window.scp_button = icon_utils.new_button_from_icon_name('vertical-arrows-long-symbolic')
    window.scp_button.add_css_class('flat')
    _expand_toolbar_button(window.scp_button)
    window.scp_button.set_tooltip_text(_('Transfer files with scp'))
    window.scp_button.set_sensitive(False)
    window.scp_button.connect('clicked', window.on_scp_button_clicked)
    window.connection_toolbar.append(window.scp_button)

    # Manage files button (visibility controlled dynamically)
    window.manage_files_button = icon_utils.new_button_from_icon_name('folder-symbolic')
    window.manage_files_button.add_css_class('flat')
    _expand_toolbar_button(window.manage_files_button)
    primary_label = get_primary_modifier_label()
    window.manage_files_button.set_tooltip_text(
        _("Open file manager for remote server ({shortcut}+Shift+O)").format(
            shortcut=primary_label)
    )
    window.manage_files_button.set_sensitive(False)
    window.manage_files_button.connect('clicked', window.on_manage_files_button_clicked)
    window.manage_files_button.set_visible(not should_hide_file_manager_options())
    window.connection_toolbar.append(window.manage_files_button)
    
    # System terminal button (only when external terminals are available)
    if not should_hide_external_terminal_options():
        window.system_terminal_button = icon_utils.new_button_from_icon_name('utilities-terminal-symbolic')
        window.system_terminal_button.add_css_class('flat')
        _expand_toolbar_button(window.system_terminal_button)
        window.system_terminal_button.set_tooltip_text(_('Open connection in system terminal'))
        window.system_terminal_button.set_sensitive(False)
        window.system_terminal_button.connect('clicked', window.on_system_terminal_button_clicked)
        window.connection_toolbar.append(window.system_terminal_button)
    
    # Delete button
    window.delete_button = icon_utils.new_button_from_icon_name('user-trash-symbolic')
    window.delete_button.add_css_class('flat')
    _expand_toolbar_button(window.delete_button)
    window.delete_button.set_tooltip_text(_('Delete Connection'))
    window.delete_button.set_sensitive(False)
    window.delete_button.connect('clicked', window.on_delete_connection_clicked)
    window.connection_toolbar.append(window.delete_button)
    
    # Group toolbar buttons
    window.group_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    window.group_toolbar.set_hexpand(True)
    window.group_toolbar.set_homogeneous(True)

    # Rename group button
    window.rename_group_button = icon_utils.new_button_from_icon_name('document-edit-symbolic')
    window.rename_group_button.add_css_class('flat')
    _expand_toolbar_button(window.rename_group_button)
    window.rename_group_button.set_tooltip_text(_('Rename Group'))
    window.rename_group_button.set_sensitive(False)
    window.rename_group_button.connect('clicked', window.on_rename_group_clicked)
    window.group_toolbar.append(window.rename_group_button)
    
    # Delete group button
    window.delete_group_button = icon_utils.new_button_from_icon_name('user-trash-symbolic')
    window.delete_group_button.add_css_class('flat')
    _expand_toolbar_button(window.delete_group_button)
    window.delete_group_button.set_tooltip_text(_('Delete Group'))
    window.delete_group_button.set_sensitive(False)
    window.delete_group_button.connect('clicked', window.on_delete_group_clicked)
    window.group_toolbar.append(window.delete_group_button)
    
    # Minimize-to-strip chevron: lives at the start of the bottom toolbar (full
    # mode only; the whole toolbar is hidden in the strip). Collapses to icons.
    minimize_button = icon_utils.new_button_from_icon_name('go-previous-symbolic')
    minimize_button.add_css_class('flat')
    # Natural (unstretched) height so it matches the lone expand chevron rather
    # than growing to the taller action-button row height.
    minimize_button.set_valign(Gtk.Align.CENTER)
    minimize_button.set_tooltip_text(_('Minimize sidebar to icons'))
    minimize_button.connect('clicked', lambda *_a: window.set_sidebar_minimal(True))
    try:
        minimize_button.set_can_focus(False)
    except Exception:
        pass
    window._sidebar_minimize_button = minimize_button

    # Add both toolbars to main toolbar
    toolbar.append(minimize_button)
    toolbar.append(window.connection_toolbar)
    toolbar.append(window.group_toolbar)

    window._sidebar_toolbar_box = toolbar
    # Clip so the toolbar's button row can't force the sidebar wider than the strip.
    window._sidebar_toolbar_clip = _horizontal_clip(toolbar)
    sidebar_box.append(window._sidebar_toolbar_clip)

    # Expand chevron: the strip-mode counterpart, pinned to the very bottom of
    # the sidebar (the vexpanding list above pushes it down). Only shown while
    # minimal; visibility is toggled in _apply_sidebar_minimal_chrome.
    expand_button = Gtk.Button()
    # go-next (not pan-end): matches the collapse chevron's go-previous arrow
    # weight; pan-* icons are smaller glyphs and look undersized beside it.
    icon_utils.set_button_icon(expand_button, 'go-next-symbolic')
    expand_button.set_tooltip_text(_('Expand sidebar'))
    expand_button.add_css_class('flat')
    expand_button.connect('clicked', lambda *_a: window.set_sidebar_minimal(False))
    # Wrap in a .toolbar bar so the button gets the same compact Adwaita metrics
    # as the collapse chevron (which lives in the bottom .toolbar box); a bare
    # flat button uses larger default padding and looks a different size. Centre
    # the bar so the natural-width chevron sits mid-strip, not left-aligned.
    expand_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
    expand_bar.add_css_class('toolbar')
    expand_bar.set_halign(Gtk.Align.CENTER)
    expand_bar.set_margin_top(6)
    expand_bar.set_margin_bottom(6)
    expand_bar.append(expand_button)
    expand_bar.set_visible(False)
    # Toggled by _apply_sidebar_minimal_chrome; the click lives on the inner button.
    window._sidebar_expand_button = expand_bar
    sidebar_box.append(expand_bar)

def _assemble_sidebar_shell(window, sidebar_box):
    """Wrap the sidebar content in HeaderBar + ToolbarView and attach it."""
    # Sidebar header: title + window controls (GNOME split-view pattern)
    window.sidebar_header_bar = Adw.HeaderBar()
    window.sidebar_header_bar.add_css_class('flat')
    if HAS_NAV_SPLIT or HAS_OVERLAY_SPLIT:
        window.sidebar_header_bar.set_show_start_title_buttons(True)
        window.sidebar_header_bar.set_show_end_title_buttons(True)

    sidebar_title_label = Gtk.Label(label='SSH Pilot')
    sidebar_title_label.add_css_class('title')
    sidebar_title_label.set_xalign(0.0)
    window._sidebar_title_label = sidebar_title_label
    window.sidebar_header_bar.set_title_widget(sidebar_title_label)

    sidebar_toolbar_view = Adw.ToolbarView()
    sidebar_toolbar_view.add_css_class('sidebar')
    sidebar_toolbar_view.add_top_bar(window.sidebar_header_bar)
    sidebar_toolbar_view.set_content(sidebar_box)
    # Kept so the detachable sidebar popup can reparent sidebar_box out of here
    # and back (see search_popup.SearchPopup).
    window._sidebar_toolbar_view = sidebar_toolbar_view

    window._set_sidebar_widget(sidebar_toolbar_view)
    logger.debug("Set sidebar widget")

def build_sidebar(window):
    """Set up the sidebar with connection list"""
    sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    # Ensure sidebar box expands to use full allocated width from NavigationSplitView
    sidebar_box.set_hexpand(True)
    sidebar_box.set_vexpand(True)
    window._sidebar_box = sidebar_box

    _build_sidebar_header(window, sidebar_box)
    _build_sidebar_search(window, sidebar_box)
    _create_sidebar_connection_list(window, sidebar_box)
    _attach_connection_list_context_menu(window)
    _attach_connection_list_shortcuts(window)

    window.connection_scrolled.set_child(window.connection_list)
    sidebar_box.append(window.connection_scrolled)

    _build_sidebar_toolbar(window, sidebar_box)
    _assemble_sidebar_shell(window, sidebar_box)


__all__ = ["ConnectionRow", "GroupRow", "build_sidebar"]
