"""Shared GTK helpers for the Docker Console plugin UI."""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk, Adw  # noqa: E402

from .client import DockerClient, DockerError  # noqa: E402

# Prefer Adw.AlertDialog (libadwaita ≥ 1.5, May 2024) — Adw.MessageDialog is
# deprecated since 1.6. The app's minimum is 1.4, so fall back to MessageDialog
# where AlertDialog isn't available (matches sshpilot/file_manager/progress_dialog.py).
_HAS_ALERT_DIALOG = hasattr(Adw, "AlertDialog")


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


def wrap_with_overlay(content: Gtk.Widget, placeholder: Gtk.Widget) -> Gtk.Overlay:
    """Overlay *placeholder* on *content* (ListBox placeholder workaround)."""
    overlay = Gtk.Overlay()
    overlay.set_vexpand(True)
    overlay.set_child(content)
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
    lbl = Gtk.Label(label=text, wrap=True, xalign=0)
    lbl.add_css_class("error" if error else "dim-label")
    lbl.set_margin_top(12)
    lbl.set_margin_bottom(12)
    return lbl


def error_text(err: Exception) -> str:
    msg = str(err) if isinstance(err, DockerError) else f"Error: {err}"
    if DockerClient.is_permission_error(str(err)):
        msg += (
            "\n\nDocker needs elevated access. Enable the “sudo” toggle "
            "above (requires passwordless sudo), or add your user to the "
            "“docker” group on the host."
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
                lambda: btn.get_parent() is not None and btn.set_sensitive(True) or False,
            )
        cb()

    btn.connect("clicked", _clicked)
    row.append(btn)
