"""GTK unlock prompt for session-backed secret backends (Bitwarden/Vaultwarden).

The core ``secret_storage`` module is GTK-free and never prompts; this is the GTK
layer that asks the user for the master password and hands it to the selected
backend's :meth:`SecretManager.unlock_selected`.

It owns the user-facing messaging for the unlock interaction:
- if the backend has no authenticated account, it tells the user to run ``bw login``
  instead of showing a doomed password prompt;
- on a failed unlock it reports an incorrect-password message.
``on_done(success: bool)`` is purely for flow control (success only when actually
unlocked). Backend calls that may spawn a process (``bw status``/``bw unlock``) run
off the main thread.
"""

import logging
import threading
from gettext import gettext as _

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .secret_storage import get_secret_manager

logger = logging.getLogger(__name__)


def _message(parent, heading, body):
    """Show a simple informational dialog (AlertDialog, MessageDialog fallback)."""
    if hasattr(Adw, 'AlertDialog'):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response('ok', _("OK"))
        dialog.present(parent)
    else:
        dialog = Adw.MessageDialog(
            transient_for=parent, modal=True, heading=heading, body=body,
        )
        dialog.add_response('ok', _("OK"))
        dialog.present()


def prompt_unlock(parent, *, on_done=None):
    """Prompt for the master password and unlock the selected session backend.

    ``on_done(success: bool)`` is invoked on the GLib main loop when finished. If
    the selected backend is not session-backed, already unlocked, or unavailable,
    this is a no-op that reports success immediately. If the backend has no
    authenticated account, it shows a "run ``bw login``" message and reports
    failure (no password prompt).
    """
    manager = get_secret_manager()

    def _report(success):
        if on_done:
            try:
                on_done(bool(success))
            except Exception:
                logger.debug("unlock on_done callback failed", exc_info=True)
        return False  # usable directly with GLib.idle_add

    # Cheap, non-blocking: session-backed + available + locked. (False also covers
    # "already unlocked", "not a session backend", and "unavailable".)
    if not manager.selected_needs_unlock():
        _report(True)
        return

    # "Locked" may actually mean "not logged in". needs_login() spawns `bw status`,
    # so probe off the main thread, then branch back on it.
    def _probe():
        needs_login = False
        try:
            needs_login = bool(manager.selected_needs_login())
        except Exception:
            logger.debug("needs_login probe failed", exc_info=True)
        GLib.idle_add(_branch, needs_login)

    def _branch(needs_login):
        if needs_login:
            _message(
                parent,
                _("Not signed in"),
                _("Your secret store has no signed-in account yet. Open a terminal, "
                  "run “bw login”, then select the backend again."),
            )
            _report(False)
        else:
            _show_password_prompt()
        return False

    def _show_password_prompt():
        backend = manager.selected_backend()
        label = backend.describe() if backend is not None else _("vault")
        heading = _("Unlock {backend}").format(backend=label)
        body = _("Enter your master password to unlock the secret store.")

        use_alert = hasattr(Adw, 'AlertDialog')
        if use_alert:
            dialog = Adw.AlertDialog(heading=heading, body=body)
        else:
            dialog = Adw.MessageDialog(
                transient_for=parent, modal=True, heading=heading, body=body,
            )

        entry = Gtk.PasswordEntry(show_peek_icon=True)
        entry.set_hexpand(True)
        entry.set_activates_default(True)  # Enter triggers the default response
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        dialog.set_extra_child(box)

        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('unlock', _("Unlock"))
        dialog.set_default_response('unlock')
        dialog.set_close_response('cancel')
        try:
            dialog.set_response_appearance('unlock', Adw.ResponseAppearance.SUGGESTED)
        except Exception:
            pass

        def _worker(password):
            ok = False
            try:
                ok = bool(manager.unlock_selected(password))
            except Exception as exc:
                logger.error("Secret backend unlock failed: %s", exc)
            GLib.idle_add(_after_unlock, ok)

        def _after_unlock(ok):
            if ok:
                _report(True)
            else:
                _message(
                    parent,
                    _("Incorrect master password"),
                    _("sshPilot could not unlock the secret store. Check your master "
                      "password and try again."),
                )
                _report(False)
            return False

        def _on_response(_d, response):
            if response != 'unlock':
                _report(False)
                return
            password = entry.get_text() or ''
            threading.Thread(target=_worker, args=(password,), daemon=True).start()

        dialog.connect('response', _on_response)
        if use_alert:
            dialog.present(parent)
        else:
            dialog.present()

    threading.Thread(target=_probe, daemon=True).start()
