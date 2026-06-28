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

# Guards against duplicate prompts: while one unlock dialog is open, further
# prompt_unlock() calls (e.g. an impatient second double-click) ride the current
# one instead of opening another dialog. All their on_done callbacks fire when the
# in-flight unlock resolves.
_unlock_in_progress = False
_pending_callbacks = []


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


def _spinner_dialog(parent, heading, body):
    """A non-dismissable 'please wait' dialog with a spinner + status label.

    Returns ``(set_status, close)``: ``set_status(text)`` updates the label (call on the
    GTK main thread); ``close()`` dismisses it. No buttons, and ``can-close`` is disabled
    so it blocks the UI until the operation finishes."""
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_top(6)
    box.set_margin_bottom(6)
    spinner = Gtk.Spinner()
    spinner.start()
    box.append(spinner)
    status = Gtk.Label(label=body)
    status.set_wrap(True)
    status.set_xalign(0)
    status.set_hexpand(True)
    box.append(status)

    if hasattr(Adw, 'AlertDialog'):
        dialog = Adw.AlertDialog(heading=heading)
        dialog.set_extra_child(box)
        try:
            dialog.set_can_close(False)   # block dismissal while the work runs
        except Exception:
            pass
        dialog.present(parent)
    else:
        dialog = Adw.MessageDialog(transient_for=parent, modal=True, heading=heading)
        dialog.set_extra_child(box)
        dialog.present()

    def _close():
        try:
            dialog.set_can_close(True)
        except Exception:
            pass
        try:
            if hasattr(dialog, 'force_close'):
                dialog.force_close()
            else:
                dialog.close()
        except Exception:
            pass

    return status.set_text, _close


def unlock_at_startup(window):
    """If the selected secret backend is session-backed (Bitwarden/Vaultwarden) and
    locked, prompt to unlock it at app startup — so the vault is ready (and warm) before
    the first connection, with the same password dialog + spinner used elsewhere.

    No-op for passive backends or an already-unlocked vault. Safe to schedule via
    ``GLib.idle_add`` from the application's activation. Returns ``False`` so it runs
    once when used as an idle source."""
    try:
        from .config import Config
        manager = get_secret_manager()
        # Apply the configured selection up front (idempotent with the connection
        # manager's deferred init) so the check below is accurate regardless of order.
        try:
            manager.set_selected(Config().get_setting('secrets.backend', 'auto'))
        except Exception:
            pass
        if manager.selected_needs_unlock():
            prompt_unlock(window)
    except Exception:
        logger.debug("startup unlock failed", exc_info=True)
    return False


def prompt_unlock(parent, *, on_done=None):
    """Prompt for the master password and unlock the selected session backend.

    ``on_done(success: bool)`` is invoked on the GLib main loop when finished. If
    the selected backend is not session-backed, already unlocked, or unavailable,
    this is a no-op that reports success immediately. The password dialog is shown
    immediately (no `bw status` pre-probe); whether a failure is "not signed in" vs
    "wrong password" is decided after the unlock attempt, off the main thread.

    Only one prompt is ever open at a time — concurrent calls ride the in-flight one
    and all their callbacks fire when it resolves.
    """
    global _unlock_in_progress
    manager = get_secret_manager()

    # Cheap, non-blocking: session-backed + available + locked. (False also covers
    # "already unlocked", "not a session backend", and "unavailable".)
    if not manager.selected_needs_unlock():
        if on_done:
            try:
                on_done(True)
            except Exception:
                logger.debug("unlock on_done callback failed", exc_info=True)
        return

    # A prompt is already open: ride it instead of stacking a second dialog.
    if _unlock_in_progress:
        if on_done:
            _pending_callbacks.append(on_done)
        return
    _unlock_in_progress = True

    def _finish(success):
        global _unlock_in_progress
        _unlock_in_progress = False
        callbacks = list(_pending_callbacks)
        _pending_callbacks.clear()
        if on_done:
            callbacks.insert(0, on_done)
        for cb in callbacks:
            try:
                cb(bool(success))
            except Exception:
                logger.debug("unlock on_done callback failed", exc_info=True)
        return False  # usable directly with GLib.idle_add

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
    # Gtk.PasswordEntry has the `activates-default` property but no
    # set_activates_default() convenience method — set it via the property so
    # Enter triggers the dialog's default response.
    entry.set_property('activates-default', True)
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

    # Holds the spinner dialog's close() so _after_unlock can dismiss it.
    _close_spinner = [lambda: None]

    def _worker(password, set_status):
        ok = False
        needs_login = False

        def _progress(stage):
            text = {
                "starting": _("Starting the vault service…"),
                "unlocking": _("Unlocking your vault…"),
                "loading": _("Loading your vault…"),
            }.get(stage)
            if text:
                GLib.idle_add(lambda: (set_status(text), False)[1])

        try:
            ok = bool(manager.unlock_selected(password, progress=_progress))
            if not ok:
                try:
                    needs_login = bool(manager.selected_needs_login())
                except Exception:
                    needs_login = False
        except Exception as exc:
            logger.error("Secret backend unlock failed: %s", exc)
        GLib.idle_add(_after_unlock, ok, needs_login)

    def _after_unlock(ok, needs_login):
        _close_spinner[0]()                 # dismiss the "Unlocking…" spinner
        if ok:
            _finish(True)
        elif needs_login:
            _message(
                parent,
                _("Not signed in"),
                _("Your secret store has no signed-in account yet. Open a terminal, "
                  "run “bw login”, then try again."),
            )
            _finish(False)
        else:
            _message(
                parent,
                _("Incorrect master password"),
                _("SSH Pilot could not unlock the secret store. Check your master "
                  "password and try again."),
            )
            _finish(False)
        return False

    def _on_response(_d, response):
        if response != 'unlock':
            _finish(False)
            return
        password = entry.get_text() or ''

        # Show a blocking spinner while the unlock runs (otherwise the user sees
        # nothing for the several seconds it takes). Present it after the password
        # dialog has finished closing.
        def _present_spinner():
            be = manager.selected_backend()
            label_be = be.describe() if be is not None else _("vault")
            set_status, close = _spinner_dialog(
                parent,
                _("Unlocking {backend}").format(backend=label_be),
                _("Unlocking your vault…"),
            )
            _close_spinner[0] = close
            threading.Thread(
                target=_worker, args=(password, set_status), daemon=True
            ).start()
            return False

        GLib.idle_add(_present_spinner)

    dialog.connect('response', _on_response)
    if use_alert:
        dialog.present(parent)
    else:
        dialog.present()

    # Start the backend's daemon now (off-thread) so its startup overlaps the user
    # typing the master password — by submit time only the unlock itself remains.
    try:
        manager.prewarm_selected()
    except Exception:
        logger.debug("prewarm_selected failed", exc_info=True)

    # Focus the password entry once the dialog is realized so the user can type
    # immediately and Enter activates the default "Unlock" response.
    def _focus_entry():
        try:
            entry.grab_focus()
        except Exception:
            pass
        return False

    GLib.idle_add(_focus_entry)
