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

from .secret_storage import get_secret_manager, selected_master_spec

logger = logging.getLogger(__name__)

# Guards against duplicate prompts: while one unlock dialog is open, further
# prompt_unlock() calls (e.g. an impatient second double-click) ride the current
# one instead of opening another dialog. All their on_done callbacks fire when the
# in-flight unlock resolves.
_unlock_in_progress = False
_pending_callbacks = []


def _should_finish_cancel(outcome, has_closed, closed_fired) -> bool:
    """Whether the password prompt should finish as *cancel*.

    True only when the outcome is KNOWN and is not ``'unlock'`` — and the dialog is gone
    (``closed_fired``) or has no ``closed`` signal (legacy ``has_closed`` False). An
    unknown outcome (``None``) never finishes: GTK may emit ``closed`` before ``response``
    (e.g. pressing Enter), and finishing then would abort an in-progress unlock and start
    the connection early. The ``unlock`` outcome is owned by the spinner/worker path."""
    if outcome is None or outcome == 'unlock':
        return False
    return bool(closed_fired or not has_closed)


def _friendly_backend_name(backend):
    """A human label for the unlock heading — never the raw describe() (which for
    Vaultwarden includes the server URL, e.g. ``vaultwarden:https://…``)."""
    name = (getattr(backend, "name", "") or "").strip().lower()
    friendly = {"bitwarden": "Bitwarden", "vaultwarden": "Vaultwarden",
                "keepassxc": "KeePassXC"}.get(name)
    if friendly:
        return friendly
    return name.replace("-", " ").title() if name else _("vault")


def _message(parent, heading, body, on_closed=None):
    """Show a simple informational dialog (AlertDialog, MessageDialog fallback).

    ``on_closed`` (if given) runs once the dialog has fully closed, so callers can
    sequence follow-up work (e.g. opening a terminal) only after it disappears."""
    if hasattr(Adw, 'AlertDialog'):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response('ok', _("OK"))
    else:
        dialog = Adw.MessageDialog(
            transient_for=parent, modal=True, heading=heading, body=body,
        )
        dialog.add_response('ok', _("OK"))

    if on_closed is not None:
        try:
            dialog.connect('closed', lambda *_a: on_closed())
        except Exception:
            # Legacy dialogs without a 'closed' signal: best-effort, run on idle.
            GLib.idle_add(lambda: (on_closed(), False)[1])

    if hasattr(Adw, 'AlertDialog'):
        dialog.present(parent)
    else:
        dialog.present()


def _spinner_dialog(parent, heading, body):
    """A non-dismissable 'please wait' dialog with a spinner + status label.

    Returns ``(set_status, close, dialog)``: ``set_status(text)`` updates the label (call
    on the GTK main thread); ``close()`` dismisses it; ``dialog`` is exposed so the caller
    can connect its ``closed`` signal to sequence follow-up work. No buttons, and
    ``can-close`` is disabled so it blocks the UI until the operation finishes."""
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

    return status.set_text, _close, dialog


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

    Returns ``True`` when this call **owns** the interaction (it showed the dialog, or no
    unlock was needed), and ``False`` when it merely **rode** an already-open prompt. The
    connect flow uses this to avoid silently proceeding on a *ridden* prompt that resolves
    still-locked (e.g. a startup unlock the user cancelled).
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
        return True

    # A prompt is already open: ride it instead of stacking a second dialog.
    if _unlock_in_progress:
        if on_done:
            _pending_callbacks.append(on_done)
        return False
    _unlock_in_progress = True
    _finished = [False]

    def _finish(success):
        # Idempotent: several dialog 'closed' hooks may reach here, but on_done /
        # retry() (which opens the terminal) must run exactly once.
        if _finished[0]:
            return False
        _finished[0] = True
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
    label = _friendly_backend_name(backend)

    def _master_spec():
        # The master password is keyed by backend + account/profile and lives in the OS
        # keyring (never the vault it unlocks). Shared with Preferences' "Forget" button.
        return selected_master_spec(manager)

    # Holds the current spinner's (close_fn, dialog) so _after_unlock can dismiss it and
    # sequence _finish off its 'closed' signal. Shared across re-prompts (one at a time).
    _spinner = [None]

    # -- run the unlock (spinner + worker) for a given password -----------
    def _run_unlock(password, *, source, remember):
        # ``source`` is 'manual' (typed in the entry dialog, ``remember`` = checkbox) or
        # 'saved' (a master password read from the keyring → auto-unlock). The spinner is
        # always shown, so startup still displays the "Unlocking…" alert when auto-unlocking.
        def _worker(set_status):
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
            # Sequence everything off the spinner's close so the terminal the caller opens
            # (via on_done -> retry()) never appears behind a closing dialog.
            close, spin = _spinner[0] if _spinner[0] is not None else (lambda: None, None)

            if ok:
                # Persist the "remember" choice on a manual unlock; a saved password that
                # just worked stays as-is.
                if source == 'manual':
                    try:
                        if remember:
                            manager.store_in_keyring(_master_spec(), password)
                        else:
                            manager.delete_in_keyring(_master_spec())
                    except Exception:
                        logger.debug("persisting master password failed", exc_info=True)
                on_spinner_closed = lambda *_a: _finish(True)
            elif source == 'saved':
                # The saved password is stale (e.g. the master password changed). Forget it
                # and fall back to a manual entry dialog — no error popup.
                try:
                    manager.delete_in_keyring(_master_spec())
                except Exception:
                    pass
                on_spinner_closed = lambda *_a: _show_password_dialog()
            elif needs_login:
                # sshPilot only runs `bw unlock`; signing in (and, for a self-hosted
                # Vaultwarden, pointing the CLI at the server) is a one-time terminal step.
                login_body = _(
                    "Your vault has no signed-in account yet. In a terminal, run “bw "
                    "login”, then try again. For a self-hosted Vaultwarden, run “bw config "
                    "server your-server-url” first.")
                on_spinner_closed = lambda *_a, _body=login_body: _message(
                    parent,
                    _("Not signed in"),
                    _body,
                    on_closed=lambda: _finish(False),
                )
            else:
                on_spinner_closed = lambda *_a: _message(
                    parent,
                    _("Incorrect master password"),
                    _("SSH Pilot could not unlock the secret store. Check your master "
                      "password and try again."),
                    on_closed=lambda: _finish(False),
                )

            if spin is not None:
                connected = False
                try:
                    spin.connect('closed', on_spinner_closed)
                    connected = True
                except Exception:
                    connected = False
                close()
                if not connected:
                    # Legacy dialog without a 'closed' signal: sequence directly.
                    on_spinner_closed()
            else:
                on_spinner_closed()
            return False

        def _present_spinner():
            set_status, close, spin = _spinner_dialog(
                parent,
                _("Unlocking {backend}").format(backend=label),
                _("Unlocking your vault…"),
            )
            _spinner[0] = (close, spin)
            threading.Thread(target=_worker, args=(set_status,), daemon=True).start()
            return False

        GLib.idle_add(_present_spinner)

    # -- the master-password entry dialog ---------------------------------
    def _show_password_dialog():
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
        remember_check = Gtk.CheckButton(label=_("Remember master password"))
        remember_check.set_active(False)   # opt-in
        caption = Gtk.Label(label=_("Stored in your system keyring."))
        caption.set_xalign(0)
        caption.set_wrap(True)
        caption.set_margin_start(28)
        for css in ("dim-label", "caption"):
            try:
                caption.add_css_class(css)
            except Exception:
                pass
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.append(entry)
        box.append(remember_check)
        box.append(caption)
        dialog.set_extra_child(box)

        dialog.add_response('cancel', _("Cancel"))
        dialog.add_response('unlock', _("Unlock"))
        dialog.set_default_response('unlock')
        dialog.set_close_response('cancel')
        try:
            dialog.set_response_appearance('unlock', Adw.ResponseAppearance.SUGGESTED)
        except Exception:
            pass

        # Per-dialog cancel-sequencing state (fresh each time the dialog is shown).
        # ``_outcome`` starts None ("unknown"): GTK does NOT guarantee 'response' fires
        # before 'closed' (Enter / activates-default can emit 'closed' first), so the
        # default must never be treated as a cancel — that would prematurely finish the
        # unlock and start the connection while the worker is still unlocking.
        _outcome = [None]
        _pw_closed_fired = [False]
        _pw_has_closed = [False]

        def _cancel_finish_if_ready():
            if _should_finish_cancel(_outcome[0], _pw_has_closed[0], _pw_closed_fired[0]):
                _finish(False)

        def _on_response(_d, response):
            _outcome[0] = response
            if response != 'unlock':
                _cancel_finish_if_ready()
                return
            password = entry.get_text() or ''
            remember = bool(remember_check.get_active())
            # _run_unlock shows the spinner once this dialog has closed.
            _run_unlock(password, source='manual', remember=remember)

        def _on_pw_closed(_d):
            _pw_closed_fired[0] = True
            _cancel_finish_if_ready()

        dialog.connect('response', _on_response)
        try:
            dialog.connect('closed', _on_pw_closed)
            _pw_has_closed[0] = True
        except Exception:
            _pw_has_closed[0] = False
        if use_alert:
            dialog.present(parent)
        else:
            dialog.present()

        def _focus_entry():
            try:
                entry.grab_focus()
            except Exception:
                pass
            return False

        GLib.idle_add(_focus_entry)

    # Auto-unlock with a saved master password (still shows the spinner), else prompt.
    saved = None
    try:
        saved = manager.lookup_in_keyring(_master_spec())
    except Exception:
        logger.debug("saved master password lookup failed", exc_info=True)
    if saved:
        _run_unlock(saved, source='saved', remember=True)
    else:
        _show_password_dialog()
    return True   # this call owns the (newly shown) prompt
