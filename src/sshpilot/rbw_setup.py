"""Setup flow for the ``rbw`` secret-storage backend (https://github.com/doy/rbw).

The mirror of :mod:`bitwarden_setup`, but much smaller because ``rbw`` does most
of the work itself:

- We do **not** auto-download ``rbw`` — it ships from distro packages / cargo /
  the AUR, so a missing CLI gets install guidance, not a binary fetch.
- Unlock uses the same GTK master-password dialog as Bitwarden/KeePass
  (including Remember). sshPilot supplies the account **config** (email +
  optional self-hosted server) then ``controller.unlock()`` / ``rbw sync``.

All lifecycle work is daemon-owned and driven exclusively through the
daemon-backed :class:`SecretBackendsController`. This module never executes
``rbw`` itself (no ``_run`` / ``_rbw_argv``), never imports ``secret_storage``
and never reads or writes ``secrets.*`` configuration directly — only GTK
dialogs and the CLI-presence check for the install notice stay here.

Public API mirrors bitwarden_setup so Preferences can drive both the same way:
:func:`probe_rbw_status`, :func:`run_rbw_setup`, :func:`ensure_rbw_ready`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib  # noqa: E402

from .window_dialogs import parent_window

logger = logging.getLogger(__name__)

RBW_HELP_URL = "https://github.com/doy/rbw"


# ---------------------------------------------------------------------------
# Status (daemon-owned — presentation adapter only)
# ---------------------------------------------------------------------------


@dataclass
class RbwStatus:
    """Snapshot of ``rbw`` readiness (presentation only)."""

    cli_installed: bool = False
    configured: bool = False  # an account email is set in `rbw config`
    unlocked: bool = False
    email: str = ""

    @property
    def is_ready(self) -> bool:
        return self.cli_installed and self.configured and self.unlocked


def _resolve_controller(window):
    """The daemon-backed secrets controller reachable from ``window``, or ``None``."""
    controller = getattr(window, "secrets_controller", None)
    if controller is not None:
        return controller
    owner = getattr(window, "parent_window", None)
    if owner is not None:
        controller = getattr(owner, "secrets_controller", None)
        if controller is not None:
            return controller
    try:
        root = window.get_root()
    except Exception:
        return None
    return getattr(root, "secrets_controller", None)


def probe_rbw_status(controller=None) -> RbwStatus:
    """Adapt the daemon's rbw status into the presentation dataclass.

    Never prompts — the daemon only reads agent state (``rbw unlocked`` and
    ``rbw config show``).
    """
    if controller is None:
        return RbwStatus()
    try:
        api_status = controller.rbw_status()
        installed = bool(getattr(api_status, "installed", False))
        return RbwStatus(
            cli_installed=installed,
            configured=bool(getattr(api_status, "configured", False)),
            unlocked=bool(getattr(api_status, "unlocked", False)),
            email=str(getattr(api_status, "email", "") or ""),
        )
    except Exception:
        logger.debug("rbw daemon status probe failed", exc_info=True)
        return RbwStatus(cli_installed=False, configured=False, unlocked=False)


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


def _prompt_config(window, status: RbwStatus, controller,
                   on_done: Callable[[bool], None]) -> None:
    """Collect the account email + optional self-hosted server, apply them through the
    daemon (``rbw_configure``), then continue. ``on_done(True)`` to proceed to login."""
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
    server_row.set_text(_current_base_url(controller))
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
        _apply_config_async(window, controller, email, server, on_done)

    dlg.connect("response", _respond)
    dlg.present()


def _current_base_url(controller) -> str:
    """The daemon-staged rbw base URL (read-only presentation)."""
    try:
        api_status = controller.rbw_status()
        return str(getattr(api_status, "base_url", "") or "").strip()
    except Exception:
        return ""


def _apply_config_async(window, controller, email: str, server: str,
                        on_done: Callable[[bool], None]) -> None:
    from .bitwarden_setup import progress_dialog

    _set, close = progress_dialog(window, _("rbw"), _("Saving rbw configuration…"))

    def worker():
        ok = False
        try:
            requested_email = (email or "").strip()
            result = controller.rbw_configure(requested_email, server)
            message = str(getattr(result, "message", "") or "").strip()
            configured = bool(getattr(result, "configured", False))
            returned_email = str(getattr(result, "email", "") or "").strip()
            ok = not message and configured and returned_email == requested_email
        except Exception:
            logger.debug("rbw config set failed", exc_info=True)
        GLib.idle_add(lambda: (close(), on_done(ok), False)[-1])

    _thread(worker)


# ---------------------------------------------------------------------------
# Login / unlock (daemon-owned; pinentry-driven inside the daemon)
# ---------------------------------------------------------------------------


def _login_async(window, controller, on_done: Callable[[bool], None]) -> None:
    """Unlock via the same GTK master-password dialog as Bitwarden/KeePass."""
    from .api.models.secrets import UnlockResultKind

    def worker():
        detail = ""
        try:
            result = controller.unlock()
            kind = getattr(result, "kind", None)
            if kind != UnlockResultKind.UNLOCKED:
                detail = str(getattr(result, "message", "") or "") or str(kind)
        except Exception as exc:
            detail = str(exc)
        if not detail:
            try:
                sync_status = controller.rbw_sync()
                detail = str(getattr(sync_status, "message", "") or "")
            except Exception as exc:
                detail = str(exc)
        status = probe_rbw_status(controller)
        GLib.idle_add(lambda: (_after_login(status, detail), False)[1])

    def _after_login(status: RbwStatus, detail: str):
        if status.unlocked and not detail:
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

    controller = _resolve_controller(window)
    if controller is None:
        _error_dialog(window, _("The secret-backend daemon service is not available."),
                      on_ready)
        return

    _set, close = progress_dialog(window, _("rbw"), _("Checking rbw…"))

    def worker():
        status = probe_rbw_status(controller)
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
            _prompt_config(window, status, controller,
                           lambda ok: _login_async(window, controller, on_ready)
                           if ok else on_ready(False))
            return
        _login_async(window, controller, on_ready)

    _thread(worker)


def run_rbw_setup(window, on_done: Optional[Callable[[bool], None]] = None) -> None:
    """Preferences “Set up…” entry: configure + log in, confirming when already ready."""
    cb = on_done or (lambda _ok: None)
    from .bitwarden_setup import progress_dialog

    controller = _resolve_controller(window)
    if controller is None:
        _error_dialog(window, _("The secret-backend daemon service is not available."),
                      cb)
        return

    _set, close = progress_dialog(window, _("rbw"), _("Checking rbw…"))

    def worker():
        status = probe_rbw_status(controller)
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
            _prompt_config(window, status, controller,
                           lambda ok: _login_async(window, controller, cb)
                           if ok else cb(False))
            return
        _login_async(window, controller, cb)

    _thread(worker)
