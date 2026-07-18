"""Dialog revealing a MasterSession's PTY when interactive auth is needed.

Shown only when the PTY-backed SSH master (``ssh_master_session.MasterSession``)
hits a prompt the app cannot answer from stored secrets — a 2FA verification
code, a hardware-key PIN or touch, a host-key confirmation. The live master
PTY is adopted into a VTE terminal (``Vte.Pty.new_foreign_sync``) so the user
answers ssh natively; the already-consumed transcript is ``feed()``-ed first so
the pending prompt is visible. The dialog closes itself once the master socket
goes live (the caller invokes :meth:`finish`).
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('Vte', '3.91')
from gi.repository import Adw, Gtk, Vte  # noqa: E402

from gettext import gettext as _  # noqa: E402

logger = logging.getLogger(__name__)


class AuthTerminalDialog(Adw.Dialog):
    """Adw.Dialog embedding the master's PTY for the user to answer ssh."""

    def __init__(self, display_name: str, master_fd: int, transcript: str,
                 on_cancelled: Callable[[], None]):
        super().__init__()
        self._on_cancelled: Optional[Callable[[], None]] = on_cancelled
        self._finished = False

        self.set_title(_('Interactive Authentication'))
        self.set_content_width(620)
        self.set_content_height(400)

        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        cancel_button = Gtk.Button(label=_('Cancel'))
        cancel_button.connect('clicked', lambda *_a: self.close())
        header.pack_start(cancel_button)

        subtitle = Gtk.Label(
            label=_('{name} is asking for additional authentication. '
                    'Answer the prompt below to connect.').format(
                        name=display_name))
        subtitle.set_wrap(True)
        subtitle.set_xalign(0.0)
        subtitle.add_css_class('dim-label')

        self._vte = Vte.Terminal()
        self._vte.set_hexpand(True)
        self._vte.set_vexpand(True)
        try:
            self._vte.set_size(80, 24)
        except Exception:
            pass
        # Replay what the hidden reader already consumed (display only), then
        # hand the PTY to VTE — from here on the user talks to ssh directly.
        if transcript:
            try:
                self._vte.feed(transcript.replace('\n', '\r\n').encode('utf-8'))
            except Exception:
                logger.debug('AuthTerminalDialog: transcript replay failed',
                             exc_info=True)
        try:
            pty = Vte.Pty.new_foreign_sync(master_fd)
            self._vte.set_pty(pty)
        except Exception:
            logger.error('AuthTerminalDialog: could not adopt master PTY',
                         exc_info=True)
            # Without the PTY the terminal is dead — say so instead of
            # presenting an empty box; Cancel remains the way out.
            subtitle.remove_css_class('dim-label')
            subtitle.add_css_class('error')
            subtitle.set_label(_(
                'Could not attach the authentication terminal. '
                'Cancel and try again, or connect from a terminal first.'))

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class('card')
        try:
            card.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:
            pass
        card.set_hexpand(True)
        card.set_vexpand(True)
        card.append(self._vte)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.set_margin_top(12)
        body.set_margin_bottom(12)
        body.set_margin_start(12)
        body.set_margin_end(12)
        body.append(subtitle)
        body.append(card)

        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_child(view)

        self.connect('closed', self._on_closed)
        self.connect('map', lambda *_a: self._vte.grab_focus())

    def finish(self) -> None:
        """Close programmatically (auth completed) without firing the cancel
        callback."""
        self._finished = True
        self.close()

    def _on_closed(self, *_args) -> None:
        callback = self._on_cancelled
        self._on_cancelled = None
        if not self._finished and callback is not None:
            callback()
