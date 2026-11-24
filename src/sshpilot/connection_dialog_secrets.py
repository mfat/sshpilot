"""Saved passwords and passphrases in the connection editor, without reading them.

Opening a connection used to fetch its saved login password and one
passphrase per key to fill the masked fields. That reads every secret (and
asks to unlock a locked vault) for an edit that may only change the port.

The editor now asks only *whether* a secret is saved, and only when secure
storage is unlocked: a locked or unreachable vault has a status of its own
and is never unlocked just to open or save the dialog. The secret itself is
fetched by one action only, **Reveal**. **Replace** is typing a new value;
**Remove** marks the saved one for deletion. Both are applied on Save and
dropped on Cancel: nothing is written or deleted before that.

:func:`check_status` and :func:`planned_operation` are the rules (no GTK);
:class:`SavedSecretField` binds them to an entry and its two buttons.
"""

from __future__ import annotations

import logging
import threading
from enum import Enum
from gettext import gettext as _
from typing import Callable, Optional, Tuple

try:
    from gi.repository import GLib, Gtk
except Exception:  # pragma: no cover - GTK-less import (headless tests)
    GLib = Gtk = None

logger = logging.getLogger(__name__)


class SecretStatus(str, Enum):
    """What is known about a saved secret, without reading it."""

    UNKNOWN = "unknown"          # not asked (yet)
    SAVED = "saved"
    NONE = "none"
    LOCKED = "locked"            # secure storage is locked or signed out
    UNAVAILABLE = "unavailable"  # secure storage could not be asked


def check_status(client, has: Callable[[], bool]) -> SecretStatus:
    """Whether a secret is saved, asking only an unlocked secure storage.

    *has* is the daemon's presence check (``has_connection_password`` or
    ``has_key_passphrase``). It is not called while storage is locked or
    signed out: a locked vault answers "nothing saved" for everything, and
    some backends would ask the user to unlock.
    """
    try:
        state = client.get_secret_state()
    except Exception:
        logger.debug("Secure storage state unavailable", exc_info=True)
        return SecretStatus.UNAVAILABLE
    if state is None:
        return SecretStatus.UNAVAILABLE
    if getattr(state, "needs_unlock", False) or getattr(state, "login_required", False):
        return SecretStatus.LOCKED
    try:
        return SecretStatus.SAVED if has() else SecretStatus.NONE
    except Exception:
        logger.debug("Saved-secret status unavailable", exc_info=True)
        return SecretStatus.UNAVAILABLE


def planned_operation(
    text: str, revealed: Optional[str], remove_pending: bool
) -> Optional[Tuple[str, str]]:
    """What Save does with a secret: ``None``, ``("store", value)`` or
    ``("delete", "")``.

    *text* is the field; *revealed* the saved value once Reveal loaded it
    (else ``None``); *remove_pending* that Remove was chosen. An empty field
    that was never revealed means "leave the saved secret alone".
    """
    text = text or ""
    if text:
        return None if text == (revealed or "") else ("store", text)
    if remove_pending or revealed:
        return ("delete", "")
    return None


def status_text(status: SecretStatus, remove_pending: bool, kind: str) -> str:
    """The line shown for a field; ``kind`` is ``password`` or ``passphrase``.

    A passphrase's text is the placeholder of a narrow entry, so it is short.
    """
    if kind == "passphrase":
        if remove_pending:
            return _("Removed on save")
        return {
            SecretStatus.SAVED: _("Saved passphrase"),
            SecretStatus.LOCKED: _("Storage locked"),
            SecretStatus.UNAVAILABLE: _("Storage unavailable"),
        }.get(status, _("Key passphrase"))
    if remove_pending:
        return _("The saved password is removed when you save.")
    return {
        SecretStatus.SAVED: _(
            "A password is saved in secure storage. Type a new one to replace it."
        ),
        SecretStatus.LOCKED: _(
            "Secure storage is locked, so it is not known whether a password "
            "is saved. A password typed here replaces it."
        ),
        SecretStatus.UNAVAILABLE: _("Secure storage could not be checked."),
    }.get(status, "")


def _button_texts(kind: str) -> Tuple[str, str, str]:
    if kind == "passphrase":
        return (
            _("Show the saved passphrase"),
            _("Remove the saved passphrase"),
            _("Keep the saved passphrase"),
        )
    return (
        _("Show the saved password"),
        _("Remove the saved password"),
        _("Keep the saved password"),
    )


def show_entry_text(entry) -> None:
    """Unmask a password entry (neither GTK widget has an API for it)."""
    if Gtk is None:
        return
    try:
        pending = [entry.get_first_child()]
        while pending:
            widget = pending.pop()
            if widget is None:
                continue
            if isinstance(widget, Gtk.Text):
                widget.set_visibility(True)
                return
            pending.append(widget.get_next_sibling())
            pending.append(widget.get_first_child())
    except Exception:
        logger.debug("Could not unmask the entry", exc_info=True)


class SavedSecretField:
    """An entry for a secret kept in secure storage, with Reveal and Remove.

    ``check()`` returns a :class:`SecretStatus` and ``reveal()`` the saved
    value (empty when there is none); both run on a worker thread.
    ``unlock(start, declined)`` runs ``start`` once secure storage is
    unlocked, asking the user if it has to. ``on_changed(field)`` is called
    on the main loop whenever the status or the pending removal changes.
    """

    def __init__(
        self,
        entry,
        *,
        kind: str,
        check: Callable[[], SecretStatus],
        reveal: Callable[[], str],
        unlock: Callable[[Callable[[], None], Callable[[], None]], None],
        on_changed: Optional[Callable[["SavedSecretField"], None]] = None,
    ) -> None:
        self.entry = entry
        self.kind = kind
        self._check = check
        self._reveal = reveal
        self._unlock = unlock
        self._on_changed = on_changed
        self.status = SecretStatus.UNKNOWN
        #: The saved value, once Reveal loaded it; the field's baseline.
        self.revealed: Optional[str] = None
        self.remove_pending = False
        self._busy = False
        self.reveal_button = None
        self.remove_button = None

    # -- widgets -----------------------------------------------------------

    def build_buttons(self) -> Tuple[object, object]:
        """The Reveal and Remove buttons (hidden until a secret is saved)."""
        show, remove, _keep = _button_texts(self.kind)
        self.reveal_button = Gtk.Button(icon_name="view-reveal-symbolic")
        self.reveal_button.set_tooltip_text(show)
        self.reveal_button.connect("clicked", lambda *_a: self.reveal())
        self.remove_button = Gtk.Button(icon_name="edit-clear-symbolic")
        self.remove_button.set_tooltip_text(remove)
        self.remove_button.connect("clicked", lambda *_a: self.toggle_remove())
        for button in (self.reveal_button, self.remove_button):
            button.add_css_class("flat")
            button.set_valign(Gtk.Align.CENTER)
            button.set_visible(False)
        return self.reveal_button, self.remove_button

    def text(self) -> str:
        """The line to show for this field now."""
        return status_text(self.status, self.remove_pending, self.kind)

    def _refresh(self) -> None:
        maybe_saved = self.status in (SecretStatus.SAVED, SecretStatus.LOCKED)
        if self.reveal_button is not None:
            self.reveal_button.set_visible(
                maybe_saved and self.revealed is None and not self.remove_pending
            )
            self.reveal_button.set_sensitive(not self._busy)
        if self.remove_button is not None:
            _show, remove, keep = _button_texts(self.kind)
            self.remove_button.set_visible(
                self.status == SecretStatus.SAVED or self.remove_pending
            )
            self.remove_button.set_icon_name(
                "edit-undo-symbolic" if self.remove_pending else "edit-clear-symbolic"
            )
            self.remove_button.set_tooltip_text(keep if self.remove_pending else remove)
        if callable(self._on_changed):
            try:
                self._on_changed(self)
            except Exception:
                logger.debug("Secret field listener failed", exc_info=True)

    # -- status (never reads the secret) -----------------------------------

    def load_status(self) -> None:
        """Ask, off the main thread, whether a secret is saved."""

        def apply(status):
            # A reveal that finished meanwhile knows more than this answer.
            if self.revealed is None:
                self.status = status
            self._refresh()
            return False

        def worker():
            try:
                status = self._check()
            except Exception:
                logger.debug("Saved-secret status check failed", exc_info=True)
                status = SecretStatus.UNAVAILABLE
            GLib.idle_add(apply, status)

        threading.Thread(target=worker, daemon=True).start()

    # -- the three actions --------------------------------------------------

    def reveal(self) -> None:
        """Fetch the saved secret into the field (the only read of it)."""
        if self._busy or self.revealed is not None:
            return
        self._busy = True
        self._refresh()

        def apply(value):
            self._busy = False
            if value is None:
                # Could not be read: nothing is concluded from that.
                self._refresh()
                return False
            if value:
                self.status = SecretStatus.SAVED
                try:
                    if not self.entry.get_text():
                        self.entry.set_text(value)
                        self.revealed = value
                        show_entry_text(self.entry)
                except Exception:
                    logger.debug("Could not show the revealed secret", exc_info=True)
            else:
                self.status = SecretStatus.NONE
            self._refresh()
            return False

        def start():
            def worker():
                try:
                    value = self._reveal() or ""
                except Exception:
                    logger.warning("Saved secret could not be revealed", exc_info=True)
                    value = None
                GLib.idle_add(apply, value)

            threading.Thread(target=worker, daemon=True).start()

        def declined():
            self._busy = False
            self._refresh()

        try:
            self._unlock(start, declined)
        except Exception:
            logger.debug("Unlock before reveal failed", exc_info=True)
            declined()

    def toggle_remove(self) -> None:
        """Mark the saved secret for removal on Save, or take that back."""
        self.remove_pending = not self.remove_pending
        if self.remove_pending:
            # The field holds no value to keep; a revealed one is forgotten.
            self.revealed = None
            try:
                self.entry.set_text("")
            except Exception:
                pass
        self._refresh()

    def operation(self) -> Optional[Tuple[str, str]]:
        """What Save does: see :func:`planned_operation`."""
        try:
            text = self.entry.get_text()
        except Exception:
            text = ""
        return planned_operation(text, self.revealed, self.remove_pending)
