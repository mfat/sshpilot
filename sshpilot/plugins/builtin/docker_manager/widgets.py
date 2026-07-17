"""Shared GTK helpers for the Docker Console plugin UI."""

from __future__ import annotations

import re
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
/* No bold anywhere in the placeholder — the StatusPage title is bold by
   default. */
statuspage.docker-console-placeholder label.title {
  font-weight: normal;
}
/* Failure state: colour only the summary (title), not the whole page —
   the generic Adwaita .error class would tint the detail log too. */
statuspage.docker-console-placeholder.docker-error label.title {
  color: @error_color;
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


# SSH chatter that must never surface as an "error summary" (verbose mode
# prints debugN:/mux lines on stderr ahead of the real failure).
_SSH_NOISE_PREFIXES = (
    "debug1:", "debug2:", "debug3:",
    "warning: permanently added",
    "authenticated to ",
    "transferred:",
    "shared connection to",
)


def strip_ssh_noise(text: str) -> str:
    """Drop SSH debug/mux chatter lines, keeping the real output."""
    lines = [ln for ln in (text or "").splitlines()
             if ln.strip()
             and not ln.strip().lower().startswith(_SSH_NOISE_PREFIXES)]
    return "\n".join(lines)


# ECMA-48 / VT100 control sequences that container apps often emit (colors,
# bold, OSC titles). Gtk.TextView cannot interpret them, so the Logs tab
# strips them before display / filter / copy / save.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:"
    r"\[[0-?]*[ -/]*[@-~]"           # CSI  (e.g. ESC[36m, ESC[0m)
    r"|\][^\x07\x1B]*(?:\x07|\x1B\\)"  # OSC (ESC]…BEL or ESC]…ESC\)
    r"|[@-Z\\-_]"                    # 2-byte Fe (ESC c, ESC M, …)
    r")"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences from *text* (plain readable remainder)."""
    if not text:
        return ""
    return _ANSI_ESCAPE_RE.sub("", text)


def describe_docker_failure(text: str) -> str:
    """One human-readable line for a failed docker/SSH command, parsed from
    the real stderr/stdout (falls back to the output's first meaningful line)."""
    text = strip_ssh_noise(text)
    low = text.lower()
    if ("cannot connect to the docker daemon" in low
            or "is the docker daemon running" in low):
        return "Docker daemon isn't running on this host"
    runtime_missing = re.search(r"\b(docker|podman)\b[^\n]*not found", low)
    if (runtime_missing or "command not found" in low
            or "executable file not found" in low):
        name = runtime_missing.group(1).capitalize() if runtime_missing else "Docker"
        return f"{name} isn't installed on this host"
    if "permission denied" in low and ("docker.sock" in low or "dial unix" in low):
        return "Permission denied talking to Docker — needs sudo or the docker group"
    if "could not resolve hostname" in low:
        return "Could not resolve the host name"
    if "connection refused" in low:
        return "Connection refused by the host"
    if "timed out" in low:
        return "Connection timed out"
    if "permission denied" in low:
        return "Permission denied"
    first = text.strip().splitlines()
    return first[0].strip() if first else "Command failed"


def truncate_toast(message: str, max_chars: int = _TOAST_MAX_CHARS) -> str:
    display, truncated = truncate_message(message, max_chars)
    if truncated:
        # Toasts are single-line-ish; collapse newlines for the summary.
        return " ".join(display.split())
    return message


def wrap_with_overlay(content: Gtk.Widget, placeholder: Gtk.Widget) -> Gtk.Overlay:
    """Overlay *placeholder* on *content* (ListBox placeholder workaround).

    The placeholder fills the overlay so its opaque background covers the list
    area; ``can_target`` starts False (clicks fall through) and is raised by
    the loading/error states, whose text is mouse-selectable. It sits inside a
    crossfade Gtk.Revealer so hiding it fades the freshly loaded list in
    instead of swapping abruptly."""
    ensure_placeholder_css()
    overlay = Gtk.Overlay()
    overlay.set_vexpand(True)
    overlay.set_child(content)
    placeholder.set_halign(Gtk.Align.FILL)
    placeholder.set_valign(Gtk.Align.FILL)
    placeholder.set_hexpand(True)
    placeholder.set_vexpand(True)
    placeholder.set_can_target(False)
    revealer = Gtk.Revealer()
    revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
    revealer.set_transition_duration(250)
    revealer.set_reveal_child(True)
    # The revealer spans the whole overlay even when unrevealed — it must not
    # swallow clicks/scrolling meant for the list underneath.
    revealer.set_can_target(False)
    revealer.set_child(placeholder)
    placeholder._revealer = revealer  # type: ignore[attr-defined]
    overlay.add_overlay(revealer)
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
    # Errors show the parsed human-readable summary (a plain selectable label,
    # no framed log box); the raw output is what the caller toasts/logs.
    if error:
        text = describe_docker_failure(text)
    display, _ = truncate_message(text, _PLACEHOLDER_MAX_CHARS)
    lbl = Gtk.Label(label=display, wrap=True, xalign=0)
    lbl.set_max_width_chars(72)
    lbl.set_selectable(True)
    lbl.add_css_class("error" if error else "dim-label")
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
