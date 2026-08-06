"""GTK unlock prompt for session-backed secret backends (Bitwarden/Vaultwarden).

The daemon (``sshpilotd``) owns the secret backends: it stages the selection, holds
the lock state, and collects master passwords / 2FA codes through **protected
interactions** presented app-wide by :class:`SecretsInteractionPresenter`. This GTK
module only *drives* the unlock through the daemon-backed
:class:`SecretBackendsController` — it never reads secret values, never touches
``SecretManager``, never imports ``secret_storage``, and never collects a master
password in a dialog.

It owns the user-facing messaging for the unlock interaction:
- if the backend has no authenticated account, the daemon reports ``login_required``
  and this module opens the Bitwarden sign-in wizard instead of a doomed prompt;
- on an unavailable backend it shows the unavailable notice.

``on_done(success: bool)`` is purely for flow control (success only when actually
unlocked). The controller call runs off the main thread (it blocks while the daemon
waits on the protected interaction), and ``on_done`` fires on the GLib main loop.
"""

import logging
import threading
from gettext import gettext as _

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw, GLib

from .window_dialogs import parent_window

logger = logging.getLogger(__name__)

# Guards against duplicate prompts: while one unlock is in flight, further
# prompt_unlock() calls (e.g. an impatient second double-click) ride the current
# one instead of opening another dialog. All their on_done callbacks fire when the
# in-flight unlock resolves.
_unlock_in_progress = False
_pending_callbacks = []


def _report_noop(on_done, success):
    """Fire ``on_done(success)`` for a call that never started an unlock."""
    if on_done:
        try:
            on_done(bool(success))
        except Exception:
            logger.debug("unlock on_done callback failed", exc_info=True)


def _backend_name(backend):
    """The raw backend name string from a descriptor, a name, or a shim object."""
    if isinstance(backend, str):
        return backend
    return getattr(backend, "name", "") or ""


def _friendly_backend_name(backend):
    """A human label for the unlock heading — never the raw describe() (which for
    Vaultwarden includes the server URL, e.g. ``vaultwarden:https://…``)."""
    name = (_backend_name(backend) or "").strip().lower()
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
            transient_for=parent_window(parent), modal=True, heading=heading, body=body,
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
        dialog = Adw.MessageDialog(transient_for=parent_window(parent), modal=True, heading=heading)
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


def _prompt_unavailable_backend(parent, backend):
    """Tell the user the selected secret backend cannot run (missing CLI, database, …).

    Shared by every backend so the startup notice is consistent; each routes to its own
    remedy (rbw → install + link, Bitwarden → set-up flow, others → a generic notice)."""
    name = (_backend_name(backend) or "").strip().lower()
    friendly = _friendly_backend_name(backend)

    if name == "rbw":
        from .rbw_setup import _install_dialog
        _install_dialog(parent)
        return

    if name == "bitwarden":
        from .bitwarden_setup import run_bitwarden_setup

        dialog = Adw.MessageDialog(
            transient_for=parent_window(parent), modal=True,
            heading=_("Bitwarden CLI not found"),
            body=_(
                "Bitwarden is selected for secret storage, but the “bw” command "
                "was not found. Passwords and passphrases will not be stored or "
                "autofilled until the CLI is installed.\n\n"
                "Open Settings ▸ Security & Credentials to change the backend, "
                "or set up Bitwarden now."
            ),
        )
        dialog.add_response("dismiss", _("Not now"))
        dialog.add_response("setup", _("Set up Bitwarden…"))
        dialog.set_default_response("setup")
        dialog.set_close_response("dismiss")

        def _respond(_dlg, resp):
            if resp == "setup":
                run_bitwarden_setup(parent, interactive=True)

        dialog.connect("response", _respond)
        dialog.present()
        return

    _message(
        parent,
        _("Secret backend unavailable"),
        _(
            "{name} is selected for secret storage but is not available on this "
            "system. Passwords and passphrases will not be stored or autofilled "
            "until it is available.\n\n"
            "Open Settings ▸ Security & Credentials to fix the setup or choose "
            "another backend."
        ).format(name=friendly),
    )


def _prompt_not_signed_in(parent, backend):
    """Tell the user the selected vault is installed but not signed in, and offer sign-in."""
    name = (_backend_name(backend) or "").strip().lower()
    friendly = _friendly_backend_name(backend)

    if name == "bitwarden":
        from .bitwarden_setup import run_bitwarden_setup

        dialog = Adw.MessageDialog(
            transient_for=parent_window(parent), modal=True,
            heading=_("Sign in to Bitwarden"),
            body=_(
                "Bitwarden is selected for secret storage, but you are not signed "
                "in. Passwords and passphrases will not be stored or autofilled "
                "until you sign in and unlock your vault.\n\n"
                "Open Settings ▸ Security & Credentials to change the backend, or "
                "sign in now."
            ),
        )
        dialog.add_response("dismiss", _("Not now"))
        dialog.add_response("setup", _("Sign in…"))
        dialog.set_default_response("setup")
        dialog.set_close_response("dismiss")

        def _respond(_dlg, resp):
            if resp == "setup":
                run_bitwarden_setup(parent, interactive=False)

        dialog.connect("response", _respond)
        dialog.present()
        return

    _message(
        parent,
        _("Not signed in"),
        _(
            "{name} is selected for secret storage but you are not signed in. "
            "Passwords and passphrases will not be stored or autofilled until you "
            "sign in.\n\nOpen Settings ▸ Security & Credentials to fix the setup."
        ).format(name=friendly),
    )


def _resolve_controller(parent):
    """The daemon-backed secrets controller reachable from ``parent``, or ``None``.

    Prefers the window's own ``secrets_controller``; dialogs that hold the owning
    window (``ConnectionDialog.parent_window``) are unwrapped; and finally a plain
    widget's root window is consulted. Returns ``None`` when no daemon controller is
    reachable — GTK then owns nothing and must not fall back to ``secret_storage``.
    """
    controller = getattr(parent, "secrets_controller", None)
    if controller is not None:
        return controller
    owner = getattr(parent, "parent_window", None)
    if owner is not None:
        controller = getattr(owner, "secrets_controller", None)
        if controller is not None:
            return controller
    try:
        root = parent.get_root()
    except Exception:
        return None
    return getattr(root, "secrets_controller", None)


def _object_needs_unlock(backend):
    """Duck-typed "would need unlocking" for an explicitly passed backend shim.

    Only the shim's own state is read (no manager, no ``secret_storage``): a
    session-backed, available, still-locked target needs an unlock."""
    if not getattr(backend, "session_backed", False):
        return False
    try:
        if not backend.is_available():
            return False
    except Exception:
        return False
    try:
        if backend.is_unlocked():
            return False
    except Exception:
        pass
    return True


def _unlock_with_controller(controller, state):
    """Run the daemon-owned unlock for the selected backend.

    When the daemon reports a stale session (locked yet not needing unlock), drop it
    with ``lock()`` first so the unlock interaction re-prompts. The result is the
    daemon's :class:`SecretUnlockResult` — never a secret value."""
    if state is not None and state.locked and not state.needs_unlock:
        lock = getattr(controller, "lock", None)
        if callable(lock):
            try:
                lock()
            except Exception:
                logger.debug("pre-unlock lock() failed", exc_info=True)
    unlock = getattr(controller, "unlock", None)
    if not callable(unlock):
        raise RuntimeError("the secrets controller exposes no unlock()")
    return unlock()


def _unlock_result_outcome(result):
    """Map a daemon ``SecretUnlockResult`` to ``(ok, notice)``.

    ``notice`` is ``"login"`` (sign-in needed), ``"unavailable"`` (backend cannot
    run), or ``None``. An ``interaction_required`` outcome is a user cancel and maps
    to a plain failure."""
    kind = getattr(getattr(result, "kind", None), "value", None)
    if kind == "unlocked":
        return True, None
    if kind == "login_required":
        return False, "login"
    if kind == "backend_unavailable":
        return False, "unavailable"
    return False, None


def unlock_at_startup(window):
    """If the selected session-backed secret backend is locked, unlock it at startup.

    The daemon owns the backend: this only reads the daemon's lock state and drives
    an unlock when needed (the app-wide secrets interaction presenter collects any
    master password). No-op when the daemon exposes no secrets controller — the app
    never falls back to GTK-owned secret code. A selected-but-unavailable backend
    gets the same notice as the manual path.

    Safe to schedule via ``GLib.idle_add`` from the application's activation. Returns
    ``False`` so it runs once when used as an idle source."""
    controller = getattr(window, "secrets_controller", None)
    if controller is None:
        return False
    try:
        state = controller.load_state()
    except Exception:
        logger.debug("startup unlock state query failed", exc_info=True)
        return False
    if state.login_required:
        _prompt_not_signed_in(window, state.selected_backend)
        return False
    if state.needs_unlock:
        prompt_unlock(window)
        return False
    # Already unlocked / passive backend. Still surface a selected-but-unavailable
    # backend once, like the manual path does (the daemon reports it as needing no
    # unlock, so without this check the user would never hear about it).
    try:
        registry = controller.load_registry()
    except Exception:
        logger.debug("startup registry query failed", exc_info=True)
        return False
    for backend in getattr(registry, "backends", ()):
        if getattr(backend, "selected", False) and not getattr(backend, "available", False):
            _prompt_unavailable_backend(window, backend)
            return False
    return False


def prompt_unlock(parent, *, backend=None, on_done=None):
    """Unlock a session backend through the daemon-owned secrets controller.

    By default this unlocks the selected backend. ``backend`` is accepted for the
    backup-destination / setup shims: a non-``None`` object is duck-typed only for its
    *state* (an already-unlocked target reports success immediately), while the actual
    unlock always goes through the daemon — GTK never unlocks a backend directly.

    ``on_done(success: bool)`` is invoked on the GLib main loop when finished. If no
    daemon controller is reachable this is a no-op that reports ``False`` (GTK must
    not fall back to a local backend); if no unlock is needed it reports ``True``.
    The master password is never collected here: the daemon raises a protected
    interaction and the app-wide secrets presenter shows the prompt.

    Only one prompt is ever open at a time — concurrent calls ride the in-flight one
    and all their callbacks fire when it resolves.

    Returns ``True`` when this call **owns** the interaction (it started the unlock, or
    no unlock was needed), and ``False`` when it merely **rode** an already-open prompt.
    The connect flow uses this to avoid silently proceeding on a *ridden* prompt that
    resolves still-locked (e.g. a startup unlock the user cancelled).
    """
    global _unlock_in_progress
    controller = _resolve_controller(parent)

    # Explicit backend object shim: only its duck-typed state is read here; the
    # unlock itself stays daemon-owned. An already-unlocked target needs no action.
    if backend is not None:
        if not _object_needs_unlock(backend):
            _report_noop(on_done, True)
            return True
        target = backend
    else:
        target = None

    # No daemon controller reachable: GTK owns nothing and must not fall back to a
    # GTK-owned or legacy backend path.
    if controller is None:
        _report_noop(on_done, False)
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

    label = _friendly_backend_name(target if target is not None else _("vault"))
    backend_name = _backend_name(target) if target is not None else ""

    # Holds the current spinner's (close_fn, dialog) so _after_unlock can dismiss it and
    # sequence _finish off its 'closed' signal.
    _spinner = [None]

    # -- run the daemon unlock (spinner + worker) -----------------------
    def _run_unlock():
        def _worker(set_status):
            ok = False
            notice = None
            state = None
            if target is None:
                # The daemon owns the decision; when it says no unlock is needed the
                # call resolves as a no-op success (already unlocked / passive /
                # unavailable), mirroring the old "cheap check" contract.
                try:
                    state = controller.load_state()
                except Exception:
                    logger.debug("unlock state query failed", exc_info=True)
                    state = None
                if state is not None and not state.needs_unlock:
                    ok = True
            if not ok:
                try:
                    ok, notice = _unlock_result_outcome(
                        _unlock_with_controller(controller, state)
                    )
                except Exception as exc:
                    logger.error("Secret backend unlock failed: %s", exc)
            GLib.idle_add(_after_unlock, ok, notice)

        def _after_unlock(ok, notice):
            # Sequence everything off the spinner's close so the terminal the caller opens
            # (via on_done -> retry()) never appears behind a closing dialog.
            close, spin = _spinner[0] if _spinner[0] is not None else (lambda: None, None)

            if ok:
                on_spinner_closed = lambda *_a: _finish(True)
            elif notice == "login":
                def _show_login_notice(*_a):
                    _prompt_not_signed_in(parent, backend_name)
                    return _finish(False)
                on_spinner_closed = _show_login_notice
            elif notice == "unavailable":
                def _show_unavailable_notice(*_a):
                    _prompt_unavailable_backend(parent, backend_name)
                    return _finish(False)
                on_spinner_closed = _show_unavailable_notice
            else:
                # Cancelled (interaction_required) or an unexpected failure.
                on_spinner_closed = lambda *_a: _finish(False)

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

    _run_unlock()
    return True   # this call owns the (newly started) unlock
