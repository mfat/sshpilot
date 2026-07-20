"""Setup flow for the ``rbw`` secret-storage backend (https://github.com/doy/rbw).

The mirror of :mod:`bitwarden_setup`, but much smaller because ``rbw`` does most
of the work itself:

- We do **not** auto-download ``rbw`` — it ships from distro packages / cargo /
  the AUR, so a missing CLI gets install guidance, not a binary fetch.
- We do **not** run a sign-in/2FA/unlock wizard — ``rbw`` owns that through its
  ``rbw-agent`` + pinentry. sshPilot only supplies the account **config** (email
  + optional self-hosted server) and then kicks off ``rbw unlock`` / ``rbw sync``;
  pinentry prompts for the master password and any 2FA.

Public API mirrors bitwarden_setup so Preferences can drive both the same way:
:func:`probe_rbw_status`, :func:`run_rbw_setup`, :func:`ensure_rbw_ready`.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from .window_dialogs import parent_window

logger = logging.getLogger(__name__)

RBW_HELP_URL = "https://github.com/doy/rbw"
_LOGIN_TIMEOUT = 300  # seconds — generous, master-password + 2FA entry via pinentry


# ---------------------------------------------------------------------------
# CLI plumbing (Flatpak-aware, shared resolver with the backend)
# ---------------------------------------------------------------------------


def _rbw_argv(*args: str) -> Optional[List[str]]:
    """``rbw`` argv prefix + *args* (host path or ``flatpak-spawn --host rbw``),
    or ``None`` when ``rbw`` is not installed."""
    from .platform_utils import resolve_host_binary

    prefix = resolve_host_binary("rbw")
    return (prefix + list(args)) if prefix else None


def _run(*args: str, input_text: Optional[str] = None, timeout: int = 120):
    argv = _rbw_argv(*args)
    if not argv:
        raise RuntimeError("rbw is not available")
    return subprocess.run(
        argv,
        input=(input_text.encode() if input_text is not None else None),
        capture_output=True,
        env=os.environ.copy(),
        check=False,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class RbwStatus:
    """Snapshot of ``rbw`` readiness."""

    cli_installed: bool = False
    configured: bool = False  # an account email is set in `rbw config`
    unlocked: bool = False
    email: str = ""

    @property
    def is_ready(self) -> bool:
        return self.cli_installed and self.configured and self.unlocked


def probe_rbw_status() -> RbwStatus:
    """Check CLI presence, account config (``rbw config show``) and unlock state
    (``rbw unlocked``). Never prompts — ``rbw unlocked`` only reads agent state."""
    if _rbw_argv() is None:
        return RbwStatus()

    email = ""
    try:
        res = _run("config", "show")
        if res.returncode == 0:
            cfg = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
            email = str(cfg.get("email") or "").strip()
    except Exception:
        logger.debug("rbw config show failed", exc_info=True)

    unlocked = False
    try:
        unlocked = _run("unlocked").returncode == 0
    except Exception:
        logger.debug("rbw unlocked check failed", exc_info=True)

    return RbwStatus(cli_installed=True, configured=bool(email),
                     unlocked=unlocked, email=email)


# ---------------------------------------------------------------------------
# Dialogs
# ---------------------------------------------------------------------------


def _install_dialog(window) -> None:
    """Compact notice that rbw — the selected secret backend — isn't installed, with a
    clickable link to the project. Uses the same alert style as the app's other
    backend-unavailable notices (not a full-page status view). No install commands:
    packaging differs per distro and the repo documents it."""
    heading = _("rbw not found")
    body = _(
        "SSH Pilot is set to use rbw — an unofficial Bitwarden client — for secret "
        "storage, but the “rbw” command was not found. Install it, then restart "
        "SSH Pilot or retry from Preferences ▸ Secret Storage."
    )
    body += "\n\n<a href=\"{url}\">{label}</a>".format(
        url=RBW_HELP_URL, label=_("View rbw on GitHub"))

    if hasattr(Adw, "AlertDialog"):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.set_body_use_markup(True)
        dialog.add_response("ok", _("OK"))
        dialog.present(window)
    else:
        dialog = Adw.MessageDialog(transient_for=parent_window(window), modal=True,
                                   heading=heading, body=body)
        dialog.set_body_use_markup(True)
        dialog.add_response("ok", _("OK"))
        dialog.present()


def _ready_dialog(window, status: RbwStatus, on_done: Callable[[bool], None]) -> None:
    from .bitwarden_setup import _message_dialog

    dlg = _message_dialog(
        window, _("rbw is ready"),
        _("Signed in and unlocked as {email}.").format(email=status.email or _("your account")),
    )
    dlg.connect("response", lambda *_a: on_done(True))
    dlg.present()


def _error_dialog(window, detail: str, on_done: Callable[[bool], None]) -> None:
    from .bitwarden_setup import _message_dialog

    body = _("rbw could not be unlocked.\n\nCheck that a pinentry program is "
             "configured (`rbw config set pinentry …`) and try again, or run "
             "`rbw login` in a terminal.")
    if detail:
        body += "\n\n" + detail
    dlg = _message_dialog(window, _("rbw setup failed"), body)
    dlg.connect("response", lambda *_a: on_done(False))
    dlg.present()


def _prompt_config(window, status: RbwStatus, on_done: Callable[[bool], None]) -> None:
    """Collect the account email + optional self-hosted server, write them with
    ``rbw config set``, then continue. ``on_done(True)`` to proceed to login."""
    from .bitwarden_setup import _message_dialog

    dlg = _message_dialog(
        window, _("Configure rbw"),
        _("Enter your Bitwarden account email. For a self-hosted Vaultwarden, also "
          "set the server URL (leave empty for the official bitwarden.com)."),
        responses=(("cancel", _("Cancel")), ("save", _("Save"))),
    )
    dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("save")
    dlg.set_close_response("cancel")

    form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    form.set_margin_top(8)
    email_row = Adw.EntryRow(title=_("Account email"))
    email_row.set_text(status.email or "")
    server_row = Adw.EntryRow(title=_("Server URL (optional)"))
    server_row.set_text(_current_base_url())
    group = Adw.PreferencesGroup()
    group.add(email_row)
    group.add(server_row)
    form.append(group)
    dlg.set_extra_child(form)

    def _respond(_d, resp):
        if resp != "save":
            on_done(False)
            return
        email = (email_row.get_text() or "").strip()
        server = (server_row.get_text() or "").strip()
        if not email:
            on_done(False)
            return
        _apply_config_async(window, email, server, on_done)

    dlg.connect("response", _respond)
    dlg.present()


def _current_base_url() -> str:
    try:
        res = _run("config", "show")
        if res.returncode == 0:
            cfg = json.loads(res.stdout.decode("utf-8", "replace") or "{}")
            return str(cfg.get("base_url") or "").strip()
    except Exception:
        pass
    return ""


def _apply_config_async(window, email: str, server: str,
                        on_done: Callable[[bool], None]) -> None:
    from .bitwarden_setup import progress_dialog

    _set, close = progress_dialog(window, _("rbw"), _("Saving rbw configuration…"))

    def worker():
        ok = True
        try:
            ok = _run("config", "set", "email", email).returncode == 0
            if server:
                _run("config", "set", "base_url", server)
            else:
                _run("config", "unset", "base_url")
        except Exception:
            logger.debug("rbw config set failed", exc_info=True)
            ok = False
        GLib.idle_add(lambda: (close(), on_done(ok), False)[-1])

    _thread(worker)


# ---------------------------------------------------------------------------
# Login / unlock (pinentry-driven)
# ---------------------------------------------------------------------------


def _login_async(window, on_done: Callable[[bool], None]) -> None:
    """Run ``rbw unlock`` + ``rbw sync`` off the main thread. rbw's pinentry
    prompts for the master password (and 2FA / login as needed). Success is
    decided by the end state (``rbw unlocked``), not intermediate return codes."""
    from .bitwarden_setup import progress_dialog

    _set, close = progress_dialog(
        window, _("rbw"),
        _("Unlocking rbw… follow the pinentry prompt for your master password."),
    )

    def worker():
        detail = ""
        try:
            res = _run("unlock", timeout=_LOGIN_TIMEOUT)
            if res.returncode != 0:
                detail = res.stderr.decode("utf-8", "replace").strip()
            _run("sync", timeout=_LOGIN_TIMEOUT)
        except Exception as exc:
            detail = str(exc)
        status = probe_rbw_status()
        GLib.idle_add(lambda: (_after_login(status, detail), False)[1])

    def _after_login(status: RbwStatus, detail: str):
        close()
        if status.unlocked:
            _ready_dialog(window, status, on_done)
        else:
            _error_dialog(window, detail, on_done)

    _thread(worker)


def _thread(target) -> None:
    import threading

    threading.Thread(target=target, daemon=True).start()


# ---------------------------------------------------------------------------
# Public entry points (mirror bitwarden_setup)
# ---------------------------------------------------------------------------


def ensure_rbw_ready(window, on_ready: Callable[[bool], None]) -> None:
    """Make ``rbw`` ready (installed + configured + unlocked), then ``on_ready(bool)``.
    Silent when already ready — used by the backend-selection path so switching to
    rbw only prompts when something is actually missing."""
    from .bitwarden_setup import progress_dialog

    _set, close = progress_dialog(window, _("rbw"), _("Checking rbw…"))

    def worker():
        status = probe_rbw_status()
        GLib.idle_add(lambda: (_after(status), False)[1])

    def _after(status: RbwStatus):
        close()
        if status.is_ready:
            on_ready(True)
            return
        if not status.cli_installed:
            _install_dialog(window)
            on_ready(False)
            return
        if not status.configured:
            _prompt_config(window, status,
                           lambda ok: _login_async(window, on_ready) if ok else on_ready(False))
            return
        _login_async(window, on_ready)

    _thread(worker)


def run_rbw_setup(window, on_done: Optional[Callable[[bool], None]] = None) -> None:
    """Preferences “Set up…” entry: configure + log in, confirming when already ready."""
    cb = on_done or (lambda _ok: None)
    from .bitwarden_setup import progress_dialog

    _set, close = progress_dialog(window, _("rbw"), _("Checking rbw…"))

    def worker():
        status = probe_rbw_status()
        GLib.idle_add(lambda: (_after(status), False)[1])

    def _after(status: RbwStatus):
        close()
        if not status.cli_installed:
            _install_dialog(window)
            cb(False)
            return
        if status.is_ready:
            _ready_dialog(window, status, cb)
            return
        if not status.configured:
            _prompt_config(window, status,
                           lambda ok: _login_async(window, cb) if ok else cb(False))
            return
        _login_async(window, cb)

    _thread(worker)
