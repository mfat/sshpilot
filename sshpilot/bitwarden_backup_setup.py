"""Get the Bitwarden CLI ready to use as a backup destination: present, signed in, unlocked.

``ensure_bitwarden_ready(window, on_ready)`` runs the checks and calls ``on_ready(True)`` once the
vault is usable, or ``on_ready(False)`` if the user cancels / it can't be set up. Unlike the
Preferences *secrets* backend, this drives Bitwarden regardless of which backend is selected.

No CLI auto-download — if ``bw`` is missing we tell the user to install it (same posture as the
secrets backend). Sign-in is a one-time terminal step (``bw login``); we open a local terminal
tab and poll ``bw status`` until the user is signed in, then unlock via the shared unlock dialog.
"""

import logging
import threading
import time

from gettext import gettext as _
from gi.repository import Adw, GLib

logger = logging.getLogger(__name__)


def _dialog(window, heading, body, *, responses=(("ok", "OK"),)):
    dlg = Adw.MessageDialog(transient_for=window, modal=True, heading=heading, body=body)
    for rid, label in responses:
        dlg.add_response(rid, label)
    return dlg


def _bitwarden_backend():
    from .secret_storage import get_secret_manager
    try:
        return get_secret_manager().get_backend("bitwarden")
    except Exception:
        logger.debug("Could not resolve the bitwarden backend", exc_info=True)
        return None


def ensure_bitwarden_ready(window, on_ready):
    """Ensure `bw` is installed, signed in, and unlocked; then call ``on_ready(bool)``.

    Every `bw` probe/operation spawns a slow Node subprocess, so they run on a worker thread and
    marshal back via ``GLib.idle_add`` — otherwise the GTK main loop freezes (GNOME "not
    responding")."""
    bw = _bitwarden_backend()
    if bw is None:
        _no_cli_dialog(window).present()
        on_ready(False)
        return

    def probe():
        available = _safe(bw.is_available)
        needs_login = _safe(bw.needs_login, default=False) if available else False
        GLib.idle_add(lambda: (_after_probe(available, needs_login), False)[1])

    def _after_probe(available, needs_login):
        if not available:
            _no_cli_dialog(window).present()
            on_ready(False)
            return
        if needs_login:
            try:
                window.terminal_manager.show_local_terminal(title=_("Bitwarden Login"))
            except Exception:
                logger.debug("Could not open a local terminal for bw login", exc_info=True)
            _wait_for_login(window, bw, on_ready)
            return
        _unlock_then_ready(window, bw, on_ready)

    threading.Thread(target=probe, daemon=True).start()


def _no_cli_dialog(window):
    return _dialog(
        window, _("Bitwarden CLI not found"),
        _("Install the Bitwarden CLI (the “bw” command) and try again — e.g. "
          "“snap install bw”, your package manager, or bitwarden.com/help/cli. "
          "For a self-hosted Vaultwarden also run “bw config server <url>” once."))


def _wait_for_login(window, bw, on_ready):
    state = {"resolved": False}

    dialog = _dialog(
        window, _("Waiting for Bitwarden sign-in"),
        _("In the terminal that just opened, run “bw login” (for a self-hosted Vaultwarden, run "
          "“bw config server <url>” first). This will continue automatically once you are signed "
          "in."),
        responses=(("cancel", _("Cancel")),),
    )
    dialog.set_close_response("cancel")

    def resolve(ok, *, proceed_unlock=False):
        if state["resolved"]:
            return
        state["resolved"] = True
        try:
            dialog.close()
        except Exception:
            pass
        if proceed_unlock:
            _unlock_then_ready(window, bw, on_ready)
        else:
            on_ready(ok)

    dialog.connect("response", lambda _d, _r: resolve(False))
    dialog.present()

    def poll():
        while not state["resolved"]:
            signed_in = not _safe(bw.needs_login, default=True)
            if signed_in:
                GLib.idle_add(lambda: (resolve(True, proceed_unlock=True), False)[1])
                return
            time.sleep(2)

    threading.Thread(target=poll, daemon=True).start()


def _unlock_then_ready(window, bw, on_ready):
    if _safe(bw.is_unlocked):
        on_ready(True)
        return
    from .secret_unlock_dialog import prompt_unlock
    prompt_unlock(window, backend=bw, on_done=lambda ok: on_ready(bool(ok)))


def _safe(fn, default=False):
    try:
        return bool(fn())
    except Exception:
        return default
