"""Shared Adw.Dialog chrome for in-app command progress (ssh-copy-id, scp).

Provides a spinner/status row, a card-wrapped VTE, and a Show/Hide Terminal
disclosure so live terminal output stays collapsed until the user expands it
(or a prompt/failure forces it open).
"""

from __future__ import annotations

import logging
import os
from gettext import gettext as _
from typing import Callable, Tuple

import gi

try:
    gi.require_version('Gtk', '4.0')
    gi.require_version('Gdk', '4.0')
except Exception:
    pass
from gi.repository import Gtk

try:
    gi.require_version('Vte', '3.91')
    from gi.repository import Vte
except Exception:
    Vte = None

from .terminal import TerminalWidget

logger = logging.getLogger(__name__)

_TERMINAL_CARD_CSS = False

# Interactive prompts ssh may print when askpass doesn't cover auth.
# Only the last non-empty line is checked, so scrollback text such as
# "Permission denied (publickey,password)." can't false-positive.
_PROMPT_COLON_MARKERS = (
    'password',
    'passphrase',
    'pin',
    'verification code',
    'otp',
)
_PROMPT_INLINE_MARKERS = (
    '(yes/no',
    'continue connecting',
    "please type 'yes'",
)


def _ensure_terminal_card_css() -> None:
    # .command-progress-terminal-card is now defined in the bundled style.css
    # (loaded once at startup); nothing to install here.
    return


def normalize_child_exit_status(status) -> int:
    try:
        value = int(status)
    except (TypeError, ValueError):
        return -1
    try:
        if os.WIFEXITED(value):
            return os.WEXITSTATUS(value)
    except Exception:
        pass
    if 0 <= value < 256:
        return value
    return (value >> 8) & 0xFF


def read_terminal_text(term_widget: TerminalWidget) -> str:
    vte = getattr(term_widget, 'vte', None)
    if vte is None:
        backend = getattr(term_widget, 'backend', None)
        vte = getattr(backend, 'vte', None) if backend else None
    # get_text_format() (used by backend.get_content) returns a fragment on
    # screens that received feed() data before the pty was attached — which
    # these dialogs do — so read an explicit range up to the cursor row.
    try:
        if vte is not None and hasattr(vte, 'get_text_range_format'):
            try:
                _col, cursor_row = vte.get_cursor_position()
                end_row = cursor_row + 1
            except Exception:
                end_row = vte.get_row_count()
            content_result = vte.get_text_range_format(
                Vte.Format.TEXT, 0, 0, end_row, -1,
            )
            if content_result and content_result[0]:
                return content_result[0]
    except Exception:
        pass
    try:
        backend = getattr(term_widget, 'backend', None)
        if backend and hasattr(backend, 'get_content'):
            content = backend.get_content()
            if content:
                return content
    except Exception:
        pass
    try:
        if vte is not None:
            content_result = vte.get_text_range(0, 0, -1, -1, lambda *args: True)
            if content_result:
                return content_result[0] or ''
    except Exception:
        pass
    return ''


def terminal_awaiting_input(text: str) -> bool:
    lines = [line.strip() for line in (text or '').splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return False
    last = lines[-1].lower()
    if any(marker in last for marker in _PROMPT_INLINE_MARKERS):
        return True
    if last.endswith(':'):
        return any(marker in last for marker in _PROMPT_COLON_MARKERS)
    return False


def wrap_dialog_terminal(term_widget: TerminalWidget) -> Gtk.Widget:
    """Wrap the dialog terminal in an Adwaita card with clipped rounded corners."""
    _ensure_terminal_card_css()
    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    frame.add_css_class('card')
    frame.add_css_class('command-progress-terminal-card')
    try:
        frame.set_overflow(Gtk.Overflow.HIDDEN)
    except Exception:
        pass
    frame.set_hexpand(True)
    frame.set_vexpand(True)
    term_widget.set_hexpand(True)
    term_widget.set_vexpand(True)
    frame.append(term_widget)
    return frame


def build_terminal_disclosure(
    terminal_card: Gtk.Widget,
    on_expanded_changed: Callable[[bool], None],
) -> Tuple[Gtk.Widget, Callable[[bool], None], Callable[[], bool]]:
    """Wrap the terminal card in a revealer with a Show/Hide Terminal toggle."""
    from . import icon_utils

    revealer = Gtk.Revealer()
    revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
    revealer.set_transition_duration(250)
    revealer.set_reveal_child(False)
    revealer.set_vexpand(True)
    revealer.set_child(terminal_card)

    chevron = icon_utils.new_image_from_icon_name('pan-end-symbolic')
    label = Gtk.Label(label=_('Show Terminal'))

    button_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    button_content.append(chevron)
    button_content.append(label)

    toggle = Gtk.ToggleButton()
    toggle.add_css_class('flat')
    toggle.set_child(button_content)
    toggle.set_halign(Gtk.Align.CENTER)

    def _on_toggled(button):
        expanded = bool(button.get_active())
        revealer.set_reveal_child(expanded)
        label.set_label(_('Hide Terminal') if expanded else _('Show Terminal'))
        try:
            icon_utils.set_icon_from_name(
                chevron,
                'pan-down-symbolic' if expanded else 'pan-end-symbolic',
            )
        except Exception:
            pass
        try:
            on_expanded_changed(expanded)
        except Exception:
            logger.debug('terminal disclosure callback failed', exc_info=True)

    toggle.connect('toggled', _on_toggled)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.append(toggle)
    box.append(revealer)

    def set_expanded(expanded: bool) -> None:
        toggle.set_active(bool(expanded))

    def is_expanded() -> bool:
        return bool(toggle.get_active())

    return box, set_expanded, is_expanded


def build_progress_status_row(
    running_text: str,
    success_text: str,
    failure_text: str,
) -> Tuple[
    Gtk.Widget,
    Callable[[], bool],
    Callable[[], None],
    Callable[[], None],
    Callable[[], None],
]:
    """Build spinner + status label row for a command progress dialog."""
    from . import icon_utils

    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_halign(Gtk.Align.CENTER)
    row.set_margin_top(4)
    row.set_margin_bottom(8)

    icon_slot = Gtk.Stack()
    icon_slot.set_halign(Gtk.Align.CENTER)
    icon_slot.set_valign(Gtk.Align.CENTER)
    icon_slot.set_size_request(20, 20)

    spinner = Gtk.Spinner()
    spinner.set_size_request(20, 20)

    success_icon = icon_utils.new_image_from_icon_name('success-small-symbolic')
    success_icon.set_pixel_size(20)
    try:
        success_icon.add_css_class('success')
    except Exception:
        pass

    error_icon = icon_utils.new_image_from_icon_name('error-outline-symbolic')
    error_icon.set_pixel_size(20)
    try:
        error_icon.add_css_class('error')
    except Exception:
        pass

    icon_slot.add_named(spinner, 'spinner')
    icon_slot.add_named(success_icon, 'success')
    icon_slot.add_named(error_icon, 'error')
    icon_slot.set_visible_child_name('spinner')

    label = Gtk.Label(label=running_text)
    label.set_halign(Gtk.Align.START)
    label.set_valign(Gtk.Align.CENTER)
    label.set_wrap(True)

    row.append(icon_slot)
    row.append(label)

    def start() -> bool:
        try:
            spinner.set_spinning(True)
            spinner.start()
        except Exception:
            pass
        return False

    def stop() -> None:
        try:
            spinner.set_spinning(False)
            spinner.stop()
            spinner.set_visible(False)
        except Exception:
            pass

    def _set_status_style(style_class: str) -> None:
        for css_class in ('dim-label', 'success', 'error'):
            try:
                label.remove_css_class(css_class)
            except Exception:
                pass
        try:
            label.add_css_class(style_class)
        except Exception:
            pass

    def mark_success() -> None:
        try:
            spinner.set_spinning(False)
            spinner.stop()
            icon_slot.set_visible_child_name('success')
            label.set_label(success_text)
            _set_status_style('success')
        except Exception:
            pass

    def mark_failure() -> None:
        try:
            spinner.set_spinning(False)
            spinner.stop()
            icon_slot.set_visible_child_name('error')
            label.set_label(failure_text)
            _set_status_style('error')
        except Exception:
            pass

    spinner.connect('map', lambda *_: spinner.start())

    return row, start, stop, mark_success, mark_failure
