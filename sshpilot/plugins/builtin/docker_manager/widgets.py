"""Shared GTK helpers for the Docker Console plugin UI."""

from __future__ import annotations

from typing import Callable, Optional, Tuple

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Adw, Gdk  # noqa: E402

from .client import DockerClient, DockerError  # noqa: E402

# Prefer Adw.AlertDialog (libadwaita ≥ 1.5, May 2024) — Adw.MessageDialog is
# deprecated since 1.6. The app's minimum is 1.4, so fall back to MessageDialog
# where AlertDialog isn't available (matches sshpilot/file_manager/progress_dialog.py).
_HAS_ALERT_DIALOG = hasattr(Adw, "AlertDialog")

# Opaque fill for list/grid placeholders. Without this, failure text is drawn on
# a transparent Gtk.Overlay child and looks like a log dumped over the window.
_PLACEHOLDER_CSS = """
.docker-console-placeholder {
  background-color: @window_bg_color;
}
"""
_PLACEHOLDER_CSS_LOADED = False

# Keep in-content failure text readable; full stderr goes to a details dialog.
_PLACEHOLDER_MAX_CHARS = 480
_TOAST_MAX_CHARS = 160


def ensure_placeholder_css() -> None:
    """Install placeholder CSS once (safe to call repeatedly)."""
    global _PLACEHOLDER_CSS_LOADED
    if _PLACEHOLDER_CSS_LOADED:
        return
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(_PLACEHOLDER_CSS.encode("utf-8"))
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        _PLACEHOLDER_CSS_LOADED = True
    except Exception:
        # Styling is best-effort; placeholders still work without it.
        pass


def build_alert(heading: str, body: str) -> Adw.Window:
    """A response-based dialog (AlertDialog when available, else MessageDialog).
    Both share the response API used here: ``add_response``,
    ``set_response_appearance``, ``set_default_response``, ``set_close_response``,
    ``set_response_enabled``, ``set_extra_child`` and the ``response`` signal."""
    if _HAS_ALERT_DIALOG:
        return Adw.AlertDialog(heading=heading, body=body)
    return Adw.MessageDialog(modal=True, heading=heading, body=body)


def present_alert(dialog: Adw.Window, parent: Optional[Gtk.Widget]) -> None:
    """Present a :func:`build_alert` dialog. AlertDialog takes the parent at
    present-time; MessageDialog needs it set as transient-for first."""
    if _HAS_ALERT_DIALOG and isinstance(dialog, Adw.AlertDialog):
        dialog.present(parent)
    else:
        if parent is not None:
            dialog.set_transient_for(parent)
        dialog.present()


def field(d: dict, *keys: str, default: str = "") -> str:
    """First present non-empty value among ``keys`` (Docker/Podman differ)."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (list, tuple)):
            v = ", ".join(str(x) for x in v)
        if v not in (None, ""):
            return str(v)
    return default


def health_of(status: str) -> Optional[str]:
    s = (status or "").lower()
    if "unhealthy" in s:
        return "unhealthy"
    if "health: starting" in s or "(starting)" in s:
        return "starting"
    if "healthy" in s:
        return "healthy"
    return None


def truncate_message(text: str, max_chars: int) -> Tuple[str, bool]:
    """Return ``(display_text, was_truncated)`` for placeholder/toast copy."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars].rsplit("\n", 1)[0].rstrip()
    if not cut or len(cut) < max_chars // 3:
        cut = text[:max_chars].rstrip()
    return cut + "\n\n…", True


def truncate_toast(message: str, max_chars: int = _TOAST_MAX_CHARS) -> str:
    display, truncated = truncate_message(message, max_chars)
    if truncated:
        # Toasts are single-line-ish; collapse newlines for the summary.
        return " ".join(display.split())
    return message


def wrap_with_overlay(content: Gtk.Widget, placeholder: Gtk.Widget) -> Gtk.Overlay:
    """Overlay *placeholder* on *content* (ListBox placeholder workaround).

    The placeholder fills the overlay so its opaque background covers the list
    area; clicks pass through while ``can_target`` is False (loading over an
    existing list)."""
    ensure_placeholder_css()
    overlay = Gtk.Overlay()
    overlay.set_vexpand(True)
    overlay.set_child(content)
    placeholder.set_halign(Gtk.Align.FILL)
    placeholder.set_valign(Gtk.Align.FILL)
    placeholder.set_hexpand(True)
    placeholder.set_vexpand(True)
    placeholder.set_can_target(False)
    overlay.add_overlay(placeholder)
    return overlay


def clear_listbox(listbox: Gtk.ListBox) -> None:
    child = listbox.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt


def clear_grid(grid: Gtk.Grid) -> None:
    child = grid.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        grid.remove(child)
        child = nxt


def listbox_wrap(widget: Gtk.Widget) -> Gtk.Widget:
    row = Gtk.ListBoxRow()
    row.set_activatable(False)
    row.set_child(widget)
    return row


def grid_message(text: str, *, error: bool = False) -> Gtk.Widget:
    display, _ = truncate_message(text, _PLACEHOLDER_MAX_CHARS)
    if error:
        # TextView so stats failures are mouse-selectable / copyable like list errors.
        view = Gtk.TextView()
        view.set_editable(False)
        view.set_cursor_visible(True)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.set_top_margin(8)
        view.set_bottom_margin(8)
        view.set_left_margin(4)
        view.set_right_margin(4)
        view.add_css_class("error")
        view.get_buffer().set_text(display, -1)
        view.set_hexpand(True)
        return view
    lbl = Gtk.Label(label=display, wrap=True, xalign=0)
    lbl.set_max_width_chars(72)
    lbl.set_selectable(True)
    lbl.add_css_class("dim-label")
    lbl.set_margin_top(12)
    lbl.set_margin_bottom(12)
    return lbl


def error_text(err: Exception) -> str:
    msg = str(err) if isinstance(err, DockerError) else f"Error: {err}"
    if DockerClient.is_permission_error(str(err)):
        msg += (
            "\n\nDocker needs elevated access. Enable the “sudo” toggle "
            "above (you may be prompted for your sudo password), or add your "
            "user to the “docker” group on the host."
        )
    return msg


def named_row(title: str, subtitle: str) -> Gtk.Box:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.set_margin_top(6)
    row.set_margin_bottom(6)
    row.set_margin_start(8)
    row.set_margin_end(8)
    info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
    t = Gtk.Label(label=title or "?", xalign=0)
    t.add_css_class("heading")
    info.append(t)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0)
        s.add_css_class("dim-label")
        s.add_css_class("caption")
        info.append(s)
    row.append(info)
    return row


def add_row_action(row: Gtk.Box, icon: str, tip: str, cb: Callable[[], None], *,
                   refreshes: bool = True) -> None:
    """Append a flat icon button with double-click guard."""
    btn = Gtk.Button(icon_name=icon)
    btn.set_tooltip_text(tip)
    btn.add_css_class("flat")

    def _clicked(_b: Gtk.Button) -> None:
        btn.set_sensitive(False)
        if not refreshes:
            GLib.timeout_add(
                800,
                lambda: (btn.get_parent() is not None and btn.set_sensitive(True)) or False,
            )
        cb()

    btn.connect("clicked", _clicked)
    row.append(btn)
