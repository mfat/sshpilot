"""GTK unlock prompt for session-backed secret backends (Bitwarden/Vaultwarden).

The core ``secret_storage`` module is GTK-free and never prompts; this is the GTK
layer that asks the user for the master password and hands it to the selected
backend's :meth:`SecretManager.unlock_selected`. The unlock itself (``bw unlock``)
can block, so it runs off the main thread and reports back via ``GLib.idle_add``.
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


def prompt_unlock(parent, *, on_done=None):
    """Prompt for the master password and unlock the selected session backend.

    ``on_done(success: bool)`` is invoked on the GLib main loop when finished. If
    the selected backend is not session-backed or is already unlocked, this is a
    no-op that reports success immediately.
    """
    manager = get_secret_manager()

    def _report(success: bool):
        if on_done:
            try:
                on_done(bool(success))
            except Exception:
                logger.debug("unlock on_done callback failed", exc_info=True)
        return False  # so it can be used directly with GLib.idle_add

    if not manager.selected_needs_unlock():
        _report(True)
        return

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

    def _show_login_required():
        # `bw unlock` can't succeed without an authenticated account; guide the
        # user instead of failing silently.
        msg = _("You are not logged in to {backend}.\n\n"
                "Run `bw login` (or `bw login --apikey`) in a terminal, "
                "then try unlocking again.").format(backend=label)
        if use_alert:
            info = Adw.AlertDialog(heading=_("Login required"), body=msg)
        else:
            info = Adw.MessageDialog(
                transient_for=parent, modal=True,
                heading=_("Login required"), body=msg,
            )
        info.add_response('ok', _("OK"))
        info.set_default_response('ok')
        info.set_close_response('ok')
        if use_alert:
            info.present(parent)
        else:
            info.present()

    def _finish(success: bool, needs_login: bool):
        if not success and needs_login:
            _show_login_required()
        _report(success)
        return False

    def _worker(password: str):
        ok = False
        needs_login = False
        try:
            ok = bool(manager.unlock_selected(password))
            if not ok:
                needs_login = bool(manager.selected_needs_login())
        except Exception as exc:
            logger.error("Secret backend unlock failed: %s", exc)
        GLib.idle_add(_finish, ok, needs_login)

    def _on_response(_d, response):
        if response != 'unlock':
            _report(False)
            return
        password = entry.get_text() or ''
        threading.Thread(target=_worker, args=(password,), daemon=True).start()

    dialog.connect('response', _on_response)
    # Enter in the field triggers the default response.
    entry.connect('activate', lambda *_a: dialog.response('unlock'))

    if use_alert:
        dialog.present(parent)
    else:
        dialog.present()
