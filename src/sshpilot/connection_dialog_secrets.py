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
            "A password is saved. Type over it to replace it, or erase it to "
            "remove it."
        ),
        SecretStatus.LOCKED: _(
            "Secure storage is locked, so it is not known whether a password "
            "is saved. A password typed here replaces it."
        ),
        SecretStatus.UNAVAILABLE: _("Secure storage could not be checked."),
    }.get(status, "")


def _button_texts(kind: str) -> Tuple[str, str]:
    """Tooltips of the Reveal and Undo buttons."""
    if kind == "passphrase":
        return _("Show the saved passphrase"), _("Keep the saved passphrase")
    return _("Show the saved password"), _("Keep the saved password")


#: Shown, masked, in the field of a saved secret that was not read: the
#: field looks filled, as a saved password does, without holding it.
PLACEHOLDER = "\u2022" * 8


def inner_text(entry):
    """The Gtk.Text inside a password entry or row, or None."""
    if Gtk is None:
        return None
    try:
        pending = [entry.get_first_child()]
        while pending:
            widget = pending.pop()
            if widget is None:
                continue
            if isinstance(widget, Gtk.Text):
                return widget
            pending.append(widget.get_next_sibling())
            pending.append(widget.get_first_child())
    except Exception:
        logger.debug("Could not find the entry's text", exc_info=True)
    return None


def show_entry_text(entry) -> None:
    """Unmask a password entry (neither GTK widget has an API for it)."""
    text = inner_text(entry)
    if text is not None:
        text.set_visibility(True)


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
        #: The field holds :data:`PLACEHOLDER`, not a value.
        self.placeholder_active = False
        self._setting = False
        try:
            entry.connect("changed", self._on_entry_changed)
        except Exception:
            logger.debug("Cannot watch the secret entry", exc_info=True)
        # The entry's own eye, pressed on the dots, asks for the saved
        # secret: showing the dots themselves would say nothing.
        text = inner_text(entry)
        if text is not None:
            text.connect("notify::visibility", self._on_peek)

    def _on_peek(self, text, _param) -> None:
        if not self.placeholder_active or not text.get_visibility():
            return
        text.set_visibility(False)
        self.reveal()

    # -- widgets -----------------------------------------------------------

    def build_buttons(self, *, labels: bool = False) -> Tuple[object, object]:
        """The two buttons a field can need; both start hidden.

        A saved secret shows as dots: the entry's own eye reveals it and
        erasing the dots removes it, so no button is needed for either.
        What that leaves is **Show saved**, for a locked storage (no dots:
        it is not known whether anything is saved), and **Undo**, after a
        removal was asked for. ``labels``: worded buttons, for a place with
        room; else icons (a key row).
        """
        show, keep = _button_texts(self.kind)
        if labels:
            self.reveal_button = Gtk.Button(label=_("Show saved"))
            self.remove_button = Gtk.Button(label=_("Undo"))
        else:
            self.reveal_button = Gtk.Button(icon_name="dialog-password-symbolic")
            self.remove_button = Gtk.Button(icon_name="edit-undo-symbolic")
        self.reveal_button.set_tooltip_text(show)
        self.reveal_button.connect("clicked", lambda *_a: self.reveal())
        self.remove_button.set_tooltip_text(keep)
        self.remove_button.connect("clicked", lambda *_a: self.toggle_remove())
        for button in (self.reveal_button, self.remove_button):
            if not labels:
                button.add_css_class("flat")
            button.set_valign(Gtk.Align.CENTER)
            button.set_visible(False)
        return self.reveal_button, self.remove_button

    def text(self) -> str:
        """The line to show for this field now."""
        return status_text(self.status, self.remove_pending, self.kind)

    def _set_entry(self, text: str) -> None:
        self._setting = True
        try:
            self.entry.set_text(text)
        except Exception:
            logger.debug("Could not set the secret entry", exc_info=True)
        finally:
            self._setting = False

    def value(self) -> str:
        """What the user typed (or revealed); empty for the placeholder."""
        if self.placeholder_active:
            return ""
        try:
            return self.entry.get_text() or ""
        except Exception:
            return ""

    def baseline(self) -> str:
        """The entry text that means "unchanged"."""
        return PLACEHOLDER if self.placeholder_active else (self.revealed or "")

    def _sync_placeholder(self) -> None:
        """Fill an untouched field of a saved secret with dots."""
        wanted = (
            self.status == SecretStatus.SAVED
            and self.revealed is None
            and not self.remove_pending
        )
        if wanted and not self.placeholder_active and not self.value():
            self.placeholder_active = True
            self._set_entry(PLACEHOLDER)
        elif not wanted and self.placeholder_active:
            self.placeholder_active = False
            self._set_entry("")

    def _on_entry_changed(self, *_args) -> None:
        """The user edited the dots: they stand for no text of their own.

        Typing replaces them with what was typed; erasing them asks for
        the saved secret to be removed, as clearing a filled field does.
        """
        if self._setting or not self.placeholder_active:
            return
        try:
            text = self.entry.get_text() or ""
        except Exception:
            return
        if text == PLACEHOLDER:
            return
        self.placeholder_active = False
        typed = text.replace("\u2022", "")
        self._set_entry(typed)
        try:
            self.entry.set_position(-1)
        except Exception:
            pass
        if not typed:
            self.remove_pending = True
        self._refresh()

    def _refresh(self) -> None:
        self._sync_placeholder()
        if self.reveal_button is not None:
            self.reveal_button.set_visible(
                self.status == SecretStatus.LOCKED
                and self.revealed is None
                and not self.remove_pending
            )
            self.reveal_button.set_sensitive(not self._busy)
        if self.remove_button is not None:
            self.remove_button.set_visible(self.remove_pending)
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
                    if not self.value():
                        self.placeholder_active = False
                        self._set_entry(value)
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
            self.placeholder_active = False
            self._set_entry("")
        self._refresh()

    def operation(self) -> Optional[Tuple[str, str]]:
        """What Save does: see :func:`planned_operation`."""
        return planned_operation(self.value(), self.revealed, self.remove_pending)
