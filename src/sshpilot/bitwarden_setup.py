"""Bitwarden CLI setup: detect/install ``bw``, sign in, and unlock.

All lifecycle operations (status, server configuration, login, unlock, sync,
lock, logout) are daemon-owned and are driven exclusively through the
daemon-backed :class:`SecretBackendsController`. This module never calls
``get_secret_manager()``, never obtains a Bitwarden backend, never invokes
``bw`` for lifecycle work and never reads or writes ``secrets.*`` configuration
directly.

What stays frontend-owned (deliberately narrow presentation):

- the optional Bitwarden CLI **installation/download** flow
  (``detect_install_plan`` / ``run_install`` / ``download_and_install_bw_binary``
  and the associated dialogs), which runs in this process by design;
- the GTK dialogs (progress spinner, message dialogs, the sign-in wizard pages,
  the server picker, the ready/locked/signed-out notices).

Secrets never travel through the wizard: master passwords, two-step codes, API
client secrets and auth-challenge client secrets are collected by the daemon
through protected interactions and presented app-wide by the secrets interaction
presenter. The wizard only collects non-secret inputs (method, email, 2FA
method, API client id, SSO organization identifier).
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import shlex
import shutil
import ssl
import subprocess
import threading
import time
import zipfile
from dataclasses import dataclass
from gettext import gettext as _
from typing import Callable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gi.repository import Adw, GLib, Gtk

from .window_dialogs import parent_window

logger = logging.getLogger(__name__)

_INSTALL_TIMEOUT = 600
BW_CLI_DOWNLOAD = "https://bitwarden.com/download/?app=cli&platform=linux"
BW_EU_SERVER = "https://vault.bitwarden.eu"
# Bitwarden CLI two-step login ``--method`` values (Authenticator=0, Email=1, YubiKey=3).
_BW_TWOFAMETHOD_VALUES = ("0", "1", "3")
_GITHUB_CLI_RELEASES = (
    "https://api.github.com/repos/bitwarden/clients/releases?per_page=100"
)
_bw_status_cache: Optional[Tuple["BitwardenStatus", float]] = None
_BW_STATUS_CACHE_TTL = 30.0


@dataclass(frozen=True)
class InstallPlan:
    """How to install the Bitwarden CLI on this system."""

    argv: Tuple[str, ...]
    description: str
    automated: bool
    terminal_command: str
    note: str = ""


@dataclass
class BitwardenStatus:
    """Snapshot of Bitwarden CLI readiness (presentation only)."""

    cli_installed: bool = False
    needs_login: bool = True
    unlocked: bool = False

    @property
    def is_ready(self) -> bool:
        return self.cli_installed and not self.needs_login and self.unlocked


def is_bw_installed(*, force_refresh: bool = False) -> bool:
    from .platform_utils import resolve_bw_cli

    return resolve_bw_cli(force_refresh=force_refresh) is not None


def _bw_source_line(*, force_refresh: bool = False) -> str:
    from .platform_utils import describe_bw_cli_source

    src = describe_bw_cli_source(force_refresh=force_refresh)
    if not src:
        return ""
    return "\n\n" + _("CLI source: {source}").format(source=src)


def _host_argv(*parts: str) -> List[str]:
    from .platform_utils import is_flatpak

    if is_flatpak() and shutil.which("flatpak-spawn"):
        return ["flatpak-spawn", "--host", *parts]
    return list(parts)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _fetch_url(url: str, *, timeout: int = 120) -> bytes:
    req = Request(url, headers={"User-Agent": "sshpilot-bitwarden-setup"})
    with urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
        return resp.read()


def _latest_cli_release() -> Tuple[str, str]:
    """Return ``(version, zip_asset_name)`` for this platform's native CLI build."""
    payload = json.loads(_fetch_url(_GITHUB_CLI_RELEASES).decode("utf-8"))
    machine = platform.machine().lower()
    system = platform.system().lower()
    for release in payload:
        tag = release.get("tag_name") or ""
        if not tag.startswith("cli-v"):
            continue
        version = tag.removeprefix("cli-v")
        if system == "linux":
            asset = (
                f"bw-linux-arm64-{version}.zip"
                if machine in ("aarch64", "arm64")
                else f"bw-linux-{version}.zip"
            )
        elif system == "darwin":
            asset = (
                f"bw-macos-arm64-{version}.zip"
                if machine in ("aarch64", "arm64")
                else f"bw-macos-{version}.zip"
            )
        elif system == "windows":
            asset = f"bw-windows-{version}.zip"
        else:
            continue
        names = {a.get("name") for a in release.get("assets") or []}
        if asset in names:
            return version, asset
    raise RuntimeError(_("Could not find a Bitwarden CLI release for this platform."))


def _cli_download_url(version: str, asset_name: str) -> str:
    return (
        f"https://github.com/bitwarden/clients/releases/download/"
        f"cli-v{version}/{asset_name}"
    )


def _install_bw_binary_native(url: str, dest: str) -> None:
    from .platform_utils import get_managed_bw_cli_path, is_flatpak

    dest = dest or get_managed_bw_cli_path()
    if is_flatpak():
        _install_bw_binary_on_host(url, dest)
        return
    data = _fetch_url(url, timeout=_INSTALL_TIMEOUT)
    install_dir = os.path.dirname(dest)
    os.makedirs(install_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = next((n for n in zf.namelist() if os.path.basename(n) in ("bw", "bw.exe")), None)
        if member is None:
            raise RuntimeError(_("Downloaded archive did not contain a bw executable."))
        with zf.open(member) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
    os.chmod(dest, 0o755)


def _install_bw_binary_on_host(url: str, dest: str) -> None:
    script = f"""
set -euo pipefail
dest={shlex.quote(dest)}
url={shlex.quote(url)}
mkdir -p "$(dirname "$dest")"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
if command -v curl >/dev/null 2>&1; then
  curl -fsSL -o "$tmp/bw.zip" "$url"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$tmp/bw.zip" "$url"
else
  echo "Need curl or wget on the host." >&2
  exit 1
fi
if command -v unzip >/dev/null 2>&1; then
  unzip -q -o "$tmp/bw.zip" -d "$tmp"
else
  python3 -c 'import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/bw.zip" "$tmp"
fi
bin=$(find "$tmp" -maxdepth 2 -type f \\( -name bw -o -name bw.exe \\) | head -n1)
if [ -z "$bin" ]; then
  echo "Archive did not contain bw." >&2
  exit 1
fi
install -m755 "$bin" "$dest"
"""
    result = subprocess.run(
        _host_argv("bash", "-c", script),
        capture_output=True, text=True, timeout=_INSTALL_TIMEOUT, check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or _("Host install failed."))


def download_and_install_bw_binary() -> Tuple[bool, str]:
    """Download the official native ``bw`` binary and install it for sshPilot."""
    from .platform_utils import get_managed_bw_cli_path, invalidate_bw_cli_cache

    try:
        version, asset = _latest_cli_release()
        url = _cli_download_url(version, asset)
        dest = get_managed_bw_cli_path()
        _install_bw_binary_native(url, dest)
    except (HTTPError, URLError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        logger.debug("Bitwarden CLI download/install failed", exc_info=True)
        return False, str(exc) or _("Could not download the Bitwarden CLI.")
    invalidate_bw_cli_cache()
    invalidate_bitwarden_status_cache()
    if not is_bw_installed(force_refresh=True):
        return False, _(
            "The Bitwarden CLI was installed but could not be verified. "
            "Try restarting sshPilot."
        )
    return True, ""


def detect_install_plan() -> Optional[InstallPlan]:
    """Return an install plan when ``bw`` is missing, else ``None``."""
    if is_bw_installed():
        return None

    from .platform_utils import get_managed_bw_cli_path, is_flatpak

    dest = get_managed_bw_cli_path()
    if is_flatpak():
        note = _(
            "The CLI will be downloaded on your system (outside the Flatpak sandbox) "
            "and installed to:\n\n    {path}"
        ).format(path=dest)
    else:
        note = _("Will be installed to:\n\n    {path}").format(path=dest)
    return InstallPlan(
        argv=(),
        description=_("Download the official Bitwarden CLI binary"),
        automated=True,
        terminal_command=_("Download from {url}").format(url=BW_CLI_DOWNLOAD),
        note=note,
    )


def run_install(plan: InstallPlan) -> Tuple[bool, str]:
    """Run ``plan`` synchronously. Returns ``(success, detail)``."""
    if not plan.automated:
        return False, plan.terminal_command
    if plan.argv:
        try:
            result = subprocess.run(
                list(plan.argv),
                capture_output=True, text=True, timeout=_INSTALL_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired:
            return False, _("Installation timed out.")
        except OSError as exc:
            return False, str(exc)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return False, detail or _("Installation failed.")
    else:
        return download_and_install_bw_binary()
    if not is_bw_installed():
        return False, _(
            "The install command finished but “bw” is still not available. "
            "Try restarting sshPilot."
        )
    from .platform_utils import invalidate_bw_cli_cache
    invalidate_bw_cli_cache()
    invalidate_bitwarden_status_cache()
    return True, ""


def invalidate_bitwarden_status_cache() -> None:
    """Drop cached Bitwarden readiness (e.g. after install/unlock)."""
    global _bw_status_cache
    _bw_status_cache = None


def _resolve_controller(window):
    """The daemon-backed secrets controller reachable from ``window``, or ``None``.

    Prefers the window's own ``secrets_controller``, then unwraps dialogs/owned
    windows, and finally consults the widget root. Returns ``None`` when no daemon
    controller is reachable — GTK then owns nothing and must not fall back to
    ``secret_storage``.
    """
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


def probe_bitwarden_status(controller=None, *, force_refresh: bool = False) -> BitwardenStatus:
    """Check CLI presence locally and query the daemon for sign-in/unlock state.

    Results are cached briefly for UI refresh paths (Preferences). The daemon
    owns all backend state: this only adapts the daemon's ``BitwardenStatus``
    (needs_login / unlocked) into the presentation dataclass, combining it with
    the local CLI-presence check used by the install presentation.
    """
    global _bw_status_cache
    now = time.monotonic()
    if (
        not force_refresh
        and _bw_status_cache is not None
        and now - _bw_status_cache[1] < _BW_STATUS_CACHE_TTL
    ):
        return _bw_status_cache[0]

    installed = is_bw_installed(force_refresh=force_refresh)
    if not installed:
        status = BitwardenStatus()
    elif controller is None:
        status = BitwardenStatus(cli_installed=True, needs_login=True, unlocked=False)
    else:
        try:
            api_status = controller.bitwarden_status(force_refresh=force_refresh)
            status = BitwardenStatus(
                cli_installed=True,
                needs_login=bool(getattr(api_status, "needs_login", True)),
                unlocked=bool(getattr(api_status, "unlocked", False)),
            )
        except Exception:
            logger.debug("Bitwarden daemon status probe failed", exc_info=True)
            status = BitwardenStatus(cli_installed=True, needs_login=True, unlocked=False)
    _bw_status_cache = (status, now)
    return status


def progress_dialog(parent, heading, message, *, on_cancel=None):
    """Spinner dialog; returns ``(set_status, close)``."""
    win = Adw.Window()
    win.set_modal(True)
    if parent is not None:
        win.set_transient_for(_modal_parent(parent))
        # Adw.Window instances are not necessarily registered with the
        # Gtk.Application just because they have a transient parent.  Register
        # this modal so routed daemon prompts (notably the backup passphrase)
        # can discover it and parent themselves above the spinner instead of
        # appearing behind it.
        try:
            from .window_dialogs import associate_window_with_parent_application

            associate_window_with_parent_application(win, _modal_parent(parent))
        except Exception:
            logger.debug("Could not register progress dialog with application", exc_info=True)
    win.set_title(heading)
    win.set_resizable(False)
    win.set_default_size(420, 170)

    toolbar = Adw.ToolbarView()
    toolbar.add_top_bar(Adw.HeaderBar())
    content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
    content.set_valign(Gtk.Align.CENTER)
    content.set_vexpand(True)
    content.set_margin_top(18)
    content.set_margin_bottom(24)
    content.set_margin_start(24)
    content.set_margin_end(24)
    spinner = Gtk.Spinner()
    spinner.set_size_request(32, 32)
    spinner.set_halign(Gtk.Align.CENTER)
    spinner.start()
    label = Gtk.Label(label=message)
    label.set_wrap(True)
    label.set_justify(Gtk.Justification.CENTER)
    label.set_halign(Gtk.Align.CENTER)
    content.append(spinner)
    content.append(label)
    toolbar.set_content(content)
    win.set_content(toolbar)

    state = {"closed": False}

    def _on_close_request(_w):
        if not state["closed"]:
            state["closed"] = True
            spinner.stop()
            if on_cancel:
                on_cancel()
        return False

    win.connect("close-request", _on_close_request)
    win.present()

    def close():
        if state["closed"]:
            return
        state["closed"] = True
        spinner.stop()
        try:
            win.close()
        except Exception:
            pass

    return label.set_text, close


def _message_dialog(
    window,
    heading,
    body,
    *,
    responses=(("ok", "OK"),),
    modal: bool = True,
    parent=None,
):
    dlg = Adw.MessageDialog(
        transient_for=_modal_parent(parent or window),
        modal=modal,
        heading=heading,
        body=body,
    )
    for rid, label in responses:
        dlg.add_response(rid, label)
    return dlg


def _terminal_host_candidates(window):
    """Windows that may own ``terminal_manager`` (Preferences → MainWindow, etc.)."""
    if window is None:
        return []
    seen = set()
    out = []

    def add(w):
        if w is None or id(w) in seen:
            return
        seen.add(id(w))
        out.append(w)

    add(window)
    add(getattr(window, "parent_window", None))
    try:
        add(window.get_transient_for())
    except Exception:
        pass
    try:
        # Settings is a NavigationPage inside MainWindow; climb to the root.
        add(window.get_root())
    except Exception:
        pass
    try:
        app = window.get_application()
        if app is not None:
            add(getattr(app, "window", None))
            for w in app.get_windows():
                if w.__class__.__name__ == "MainWindow":
                    add(w)
    except Exception:
        pass
    return out


def _resolve_main_window(window):
    for host in _terminal_host_candidates(window):
        if host.__class__.__name__ == "MainWindow":
            return host
    manager = _resolve_terminal_manager(window)
    if manager is not None:
        return getattr(manager, "window", None)
    return None


def _hide_preferences_windows(window) -> list:
    """Leave Settings mode so the work UI (tabs) is visible.

    Returns a list of main windows that were in Settings mode (so callers can
    re-enter via ``_restore_preferences_windows``).
    """
    left = []
    main = _resolve_main_window(window)
    candidates = [main] if main is not None else []
    for host in _terminal_host_candidates(window):
        if host not in candidates:
            candidates.append(host)
    for host in candidates:
        if host is None:
            continue
        leave = getattr(host, 'leave_preferences', None)
        if callable(leave):
            try:
                if leave():
                    left.append(host)
            except Exception:
                pass
        elif host.__class__.__name__ == 'PreferencesWindow':
            # Legacy path: Preferences was a separate widget; no longer used.
            parent = getattr(host, 'parent_window', None)
            leave_parent = getattr(parent, 'leave_preferences', None) if parent else None
            if callable(leave_parent):
                try:
                    if leave_parent():
                        left.append(parent)
                except Exception:
                    pass
    return left


def _restore_preferences_windows(windows) -> None:
    """Re-enter Settings mode on windows that left it for a terminal reveal."""
    for host in windows:
        show = getattr(host, 'show_preferences', None)
        if callable(show):
            try:
                show()
            except Exception:
                pass


def _present_main_window(window) -> None:
    main = _resolve_main_window(window)
    if main is None:
        return
    try:
        main.unminimize()
        main.present()
        if hasattr(main, "show_tab_view"):
            main.show_tab_view()
    except Exception:
        logger.debug("Could not present main window for Bitwarden setup", exc_info=True)


def _toast_main(window, message: str) -> None:
    main = _resolve_main_window(window)
    if main is None:
        return
    overlay = getattr(main, "toast_overlay", None)
    if overlay is None:
        return
    try:
        overlay.add_toast(Adw.Toast.new(message))
    except Exception:
        logger.debug("Bitwarden setup toast failed", exc_info=True)


def _reveal_terminal_workspace(window) -> list:
    """Leave Settings mode and raise the main window before opening a terminal tab."""
    hidden = _hide_preferences_windows(window)
    _present_main_window(window)
    return hidden


def _wrap_on_ready(on_ready: Callable[[bool], None],
                   hidden: list) -> Callable[[bool], None]:
    def _done(ok: bool) -> None:
        _restore_preferences_windows(hidden)
        on_ready(ok)
    return _done


def _resolve_terminal_manager(window):
    for host in _terminal_host_candidates(window):
        manager = getattr(host, "terminal_manager", None)
        if manager is not None:
            return manager
    return None


def _open_local_terminal(window, *, title: str) -> Tuple[bool, list]:
    hidden = _reveal_terminal_workspace(window)
    manager = _resolve_terminal_manager(window)
    if manager is None:
        logger.debug("No terminal_manager found for Bitwarden setup (window=%s)",
                     type(window).__name__)
        _restore_preferences_windows(hidden)
        _message_dialog(
            window,
            _("Open a terminal"),
            _("Could not open a built-in terminal from Settings. "
              "Open a system terminal manually and run the commands shown in "
              "the next dialog."),
        ).present()
        return False, []
    try:
        manager.show_local_terminal(title=title)
        _present_main_window(window)
        return True, hidden
    except Exception:
        logger.debug("Could not open a local terminal", exc_info=True)
        _restore_preferences_windows(hidden)
        return False, []


def _offer_install(window, plan: InstallPlan, on_done: Callable[[bool], None]):
    """Ask to install ``bw``; ``on_done(True)`` when present, else ``False``."""
    note_block = f"\n\n{plan.note}" if plan.note else ""
    if plan.automated:
        body = _(
            "The Bitwarden CLI (“bw”) is not installed. Install it now?\n\n"
            "{method}{note}"
        ).format(method=plan.description, note=note_block)
        responses = (("cancel", _("Cancel")), ("install", _("Install")))
    else:
        body = _(
            "The Bitwarden CLI (“bw”) is not installed. Open a terminal and run:\n\n"
            "    {command}\n\n"
            "{note}"
            "Then return here and choose “Try again”."
        ).format(command=plan.terminal_command, note=(plan.note + "\n\n") if plan.note else "")
        responses = (("cancel", _("Cancel")), ("terminal", _("Open terminal")),
                     ("retry", _("Try again")))

    dlg = _message_dialog(window, _("Install Bitwarden CLI"), body, responses=responses)
    if plan.automated:
        dlg.set_default_response("install")
    else:
        dlg.set_default_response("terminal")
    dlg.set_close_response("cancel")

    def _respond(_d, resp):
        if resp == "cancel":
            on_done(False)
            return
        if resp == "terminal":
            _open_local_terminal(window, title=_("Install Bitwarden CLI"))
            _offer_install(window, plan, on_done)
            return
        if resp == "retry":
            if is_bw_installed():
                on_done(True)
            else:
                _offer_install(window, plan, on_done)
            return
        if resp != "install":
            on_done(False)
            return

        cancelled = {"v": False}

        def _cancel():
            cancelled["v"] = True
            on_done(False)

        _set_status, close = progress_dialog(
            window, _("Installing Bitwarden CLI"),
            _("Installing the Bitwarden CLI…"), on_cancel=_cancel,
        )

        def worker():
            ok, detail = run_install(plan)
            GLib.idle_add(lambda: (_after_install(ok, detail), False)[1])

        def _after_install(ok, detail):
            close()
            if cancelled["v"]:
                return
            if ok:
                on_done(True)
                return
            _message_dialog(
                window, _("Installation failed"),
                detail or _("Could not install the Bitwarden CLI."),
            ).present()
            on_done(False)

        threading.Thread(target=worker, daemon=True).start()

    dlg.connect("response", _respond)
    dlg.present()


def _server_preset_from_config(controller) -> int:
    """0 = US cloud, 1 = EU cloud, 2 = self-hosted/custom."""
    url = _server_url_from_controller(controller)
    if not url:
        return 0
    if url.rstrip("/") == BW_EU_SERVER:
        return 1
    return 2


def _server_url_from_controller(controller) -> str:
    """The daemon-staged Bitwarden server URL (read-only presentation)."""
    try:
        configuration = controller.configuration()
        if configuration is not None:
            return str(getattr(configuration, "bitwarden_server", "") or "").strip()
    except Exception:
        pass
    return ""


def _format_bitwarden_login_error(detail: str) -> str:
    """Augment ``bw login`` errors with doc-aligned guidance."""
    text = (detail or "").strip()
    lower = text.lower()
    if "trusted device" in lower:
        return _(
            "{detail}\n\n"
            "Accounts enrolled in an organization's trusted-device policy "
            "cannot access vault data through the Bitwarden CLI."
        ).format(detail=text)
    if any(token in lower for token in ("bot", "authentication challenge", "auth challenge")):
        return _(
            "{detail}\n\n"
            "If Bitwarden asked for additional authentication, enter your "
            "personal API key client secret in the auth-challenge field "
            "(Account Settings → Security → Keys)."
        ).format(detail=text)
    return text or _("Sign-in failed.")


def _prompt_server_url(window, controller, on_chosen: Callable[[str], None]):
    """Bitwarden server selection before sign-in (US / EU / self-hosted).

    The chosen URL is applied by the daemon (``bitwarden_configure_server``);
    this dialog only reports the selection back.
    """
    parent = _modal_parent(window)
    dlg = Adw.MessageDialog(
        transient_for=parent_window(parent), modal=True,
        heading=_("Bitwarden server"),
        body=_(
            "Choose which Bitwarden server to use before signing in. "
            "Self-hosted Vaultwarden URLs are supported.\n\n"
            "CAs from the system trust store are trusted automatically. For a "
            "CA outside it, set NODE_EXTRA_CA_CERTS to your bundle in the "
            "environment before signing in."
        ),
    )
    model = Gtk.StringList()
    model.append(_("Bitwarden Cloud (US — default)"))
    model.append(_("Bitwarden Cloud (EU)"))
    model.append(_("Self-hosted / Vaultwarden"))
    dropdown = Gtk.DropDown(model=model)
    dropdown.set_selected(_server_preset_from_config(controller))
    entry = Gtk.Entry()
    entry.set_placeholder_text("https://vault.example.com")
    saved = _server_url_from_controller(controller)
    preset = _server_preset_from_config(controller)
    entry.set_text(saved if preset == 2 else "")
    entry.set_visible(preset == 2)
    entry.set_hexpand(True)

    def _on_preset_changed(_dd, _pspec):
        entry.set_visible(dropdown.get_selected() == 2)

    dropdown.connect("notify::selected", _on_preset_changed)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.append(dropdown)
    box.append(entry)
    dlg.set_extra_child(box)
    dlg.add_response("cancel", _("Cancel"))
    dlg.add_response("continue", _("Continue"))
    dlg.set_default_response("continue")
    dlg.set_close_response("cancel")

    def _respond(_d, resp):
        if resp != "continue":
            on_chosen(None)  # None = cancelled; "" = US-default server
            return
        selected = dropdown.get_selected()
        if selected == 1:
            url = BW_EU_SERVER
        elif selected == 2:
            url = (entry.get_text() or "").strip()
        else:
            url = ""
        on_chosen(url)

    dlg.connect("response", _respond)
    dlg.present()


def _modal_parent(window):
    """Return the window that should own Bitwarden setup modals.

    Prefer the initiating window when it is visible, so dialogs stack above it;
    ``resolve_app_modal_parent`` always picks MainWindow. Fall back to the app
    modal parent only when the initiator is missing or not visible.
    """
    try:
        from .window import present_for_modal_dialog, resolve_app_modal_parent

        window = parent_window(window)
        if isinstance(window, Gtk.Window):
            try:
                visible = window.get_visible()
            except Exception:
                visible = True
            if visible:
                present_for_modal_dialog(window)
                return window
        parent = resolve_app_modal_parent(window)
        present_for_modal_dialog(parent)
        return parent
    except Exception:
        return _resolve_main_window(window) or window


def _show_error(parent, heading: str, body: str, *, on_closed=None):
    dlg = _message_dialog(parent, heading, body)
    if on_closed:
        dlg.connect("response", lambda *_a: on_closed())
    dlg.present()


def _signin_field_box(label: str, widget: Gtk.Widget) -> Gtk.Box:
    """Labeled form row for ``Adw.MessageDialog`` (not ``Adw.EntryRow`` — those need a list box)."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    title = Gtk.Label(label=label, xalign=0, halign=Gtk.Align.START)
    title.add_css_class("caption-heading")
    widget.set_hexpand(True)
    try:
        widget.set_property("activates-default", True)
    except Exception:
        pass
    box.append(title)
    box.append(widget)
    return box


def _signin_page(
    parent,
    *,
    heading,
    body,
    rows,
    on_next,
    on_cancel,
    on_back=None,
    next_label=None,
    error="",
):
    """One wizard page: a single ``Adw.MessageDialog`` with ``rows`` of labeled fields.

    Mirrors a single ``bw login`` CLI prompt. ``rows`` is ``[(label, widget), …]``.
    ``on_next(close_page)`` runs on Continue and owns advancing — it must call
    ``close_page()`` before showing a spinner or the next page. ``on_back`` and
    ``on_cancel`` take no arguments. Every path closes the dialog explicitly.
    """
    dlg = Adw.MessageDialog(
        transient_for=parent_window(parent), modal=True, heading=heading, body=body,
    )
    form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for label, widget in rows:
        form.append(_signin_field_box(label, widget))
    if error:
        error_label = Gtk.Label(label=error, xalign=0, halign=Gtk.Align.START)
        error_label.set_wrap(True)
        error_label.add_css_class("error")
        form.append(error_label)
    dlg.set_extra_child(form)

    if on_back is not None:
        dlg.add_response("back", _("Back"))
    dlg.add_response("cancel", _("Cancel"))
    dlg.add_response("next", next_label or _("Continue"))
    dlg.set_default_response("next")
    dlg.set_close_response("cancel")
    try:
        dlg.set_response_appearance("next", Adw.ResponseAppearance.SUGGESTED)
    except Exception:
        pass

    closed = {"v": False}

    def close_page():
        if closed["v"]:
            return
        closed["v"] = True
        try:
            dlg.close()
        except Exception:
            pass

    def _respond(_d, resp):
        if resp == "next":
            on_next(close_page)
        elif resp == "back" and on_back is not None:
            close_page()
            on_back()
        else:
            close_page()
            on_cancel()

    dlg.connect("response", _respond)
    dlg.present()
    if rows:
        try:
            rows[0][1].grab_focus()
        except Exception:
            pass


def _login_wizard(window, controller, on_done: Callable[[bool], None]):
    """Sequential graphical sign-in driven through the daemon controller.

    Only non-secret inputs are collected here (method, email, 2FA method, API
    client id, SSO organization identifier); the daemon collects the master
    password, two-step codes, API client secrets and auth-challenge client
    secrets through protected interactions presented app-wide.
    """
    parent = _modal_parent(window)
    data = {}

    def _cancel():
        on_done(False)

    def _finish_ok():
        invalidate_bitwarden_status_cache()
        _ensure_unlocked_then_ready(parent, controller, on_done)

    def _spinner(message=None):
        return progress_dialog(parent, _("Bitwarden"), message or _("Signing in…"))

    # -- method picker (bw login / --apikey / --sso) -------------------
    def _method_page(error=""):
        model = Gtk.StringList()
        model.append(_("Email and master password"))
        model.append(_("Personal API key"))
        model.append(_("Single sign-on (SSO)"))
        dropdown = Gtk.DropDown(model=model)
        dropdown.set_selected(data.get("method", 0))

        def _next(close_page):
            selected = dropdown.get_selected()
            data["method"] = selected
            close_page()
            if selected == 0:
                _email_page()
            elif selected == 1:
                _apikey_id_page()
            else:
                _sso_page()

        _signin_page(
            parent,
            heading=_("Sign in to Bitwarden"),
            body=_("Choose how to sign in to the Bitwarden CLI."),
            rows=[(_("Sign-in method"), dropdown)],
            on_next=_next, on_cancel=_cancel, error=error,
        )

    # -- email + master password (bw login) ---------------------------
    def _email_page(error=""):
        entry = Gtk.Entry()
        entry.set_input_purpose(Gtk.InputPurpose.EMAIL)
        entry.set_text(data.get("email", ""))

        def _next(close_page):
            email = (entry.get_text() or "").strip()
            data["email"] = email
            close_page()
            if not email:
                _email_page(error=_("Enter your email address."))
                return
            _attempt_login()

        _signin_page(
            parent,
            heading=_("Sign in to Bitwarden"),
            body=_("Enter your Bitwarden email address."),
            rows=[(_("Email"), entry)],
            on_next=_next, on_back=_method_page, on_cancel=_cancel, error=error,
        )

    def _twofa_method_page(error="", info=""):
        model = Gtk.StringList()
        model.append(_("Authenticator app"))
        model.append(_("Email"))
        model.append(_("YubiKey"))
        dropdown = Gtk.DropDown(model=model)
        dropdown.set_selected(data.get("twofa_idx", 0))

        def _next(close_page):
            idx = dropdown.get_selected()
            data["twofa_idx"] = idx
            method = (
                _BW_TWOFAMETHOD_VALUES[idx]
                if 0 <= idx < len(_BW_TWOFAMETHOD_VALUES)
                else "0"
            )
            close_page()
            _attempt_login(twofa_method=method)

        _signin_page(
            parent,
            heading=_("Two-step login"),
            body=info or _(
                "Choose your two-step login method. The verification code is "
                "requested securely by the daemon."
            ),
            rows=[(_("Two-step login method"), dropdown)],
            on_next=_next, on_back=_email_page, on_cancel=_cancel, error=error,
            next_label=_("Continue"),
        )

    def _attempt_login(twofa_method=None):
        _set_status, close = _spinner()

        def worker():
            try:
                api_status = controller.bitwarden_login(
                    data.get("email", ""), twofa_method=twofa_method,
                )
                ok = bool(getattr(api_status, "logged_in", False))
                detail = str(getattr(api_status, "message", "") or "")
                needs_2fa = bool(getattr(api_status, "twofa_required", False))
            except Exception as exc:
                logger.debug("Bitwarden daemon login failed", exc_info=True)
                ok, detail, needs_2fa = False, str(exc), False

            def done():
                close()
                if ok:
                    _finish_ok()
                elif needs_2fa and not twofa_method:
                    _twofa_method_page()
                elif needs_2fa and twofa_method:
                    _twofa_method_page(error=_format_bitwarden_login_error(detail))
                else:
                    _email_page(error=_format_bitwarden_login_error(detail))
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    # -- personal API key (bw login --apikey) -------------------------
    def _apikey_id_page(error=""):
        entry = Gtk.Entry()
        entry.set_text(data.get("client_id", ""))

        def _next(close_page):
            client_id = (entry.get_text() or "").strip()
            data["client_id"] = client_id
            close_page()
            if not client_id:
                _apikey_id_page(error=_("Enter your API key client ID."))
                return
            _attempt_apikey()

        _signin_page(
            parent,
            heading=_("Sign in with API key"),
            body=_("Enter your personal API key client ID. The client secret is "
                   "requested securely by the daemon."),
            rows=[(_("Client ID"), entry)],
            on_next=_next, on_back=_method_page, on_cancel=_cancel, error=error,
        )

    def _attempt_apikey():
        _set_status, close = _spinner()

        def worker():
            try:
                api_status = controller.bitwarden_api_key_login(data.get("client_id", ""))
                ok = bool(getattr(api_status, "logged_in", False))
                detail = str(getattr(api_status, "message", "") or "")
            except Exception as exc:
                logger.debug("Bitwarden API key login failed", exc_info=True)
                ok, detail = False, str(exc)

            def done():
                close()
                if ok:
                    _finish_ok()
                else:
                    _apikey_id_page(error=_format_bitwarden_login_error(detail))
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    # -- SSO (bw login --sso) -----------------------------------------
    def _sso_page(error=""):
        entry = Gtk.Entry()
        entry.set_text(data.get("sso_id", ""))

        def _next(close_page):
            data["sso_id"] = (entry.get_text() or "").strip()
            close_page()
            _attempt_sso()

        _signin_page(
            parent,
            heading=_("Sign in with SSO"),
            body=_("SSO opens in your web browser. Leave the organization "
                   "identifier empty for the default SSO flow."),
            rows=[(_("Organization ID (optional)"), entry)],
            on_next=_next, on_back=_method_page, on_cancel=_cancel, error=error,
            next_label=_("Sign in"),
        )

    def _attempt_sso():
        _set_status, close = _spinner(_("Complete SSO sign-in in your browser…"))

        def worker():
            try:
                api_status = controller.bitwarden_sso_login(data.get("sso_id") or None)
                ok = bool(getattr(api_status, "logged_in", False))
                detail = str(getattr(api_status, "message", "") or "")
                if ok:
                    for _ in range(150):
                        try:
                            probe = controller.bitwarden_status(force_refresh=True)
                            if not getattr(probe, "needs_login", True):
                                break
                        except Exception:
                            pass
                        time.sleep(2)
                    else:
                        ok = False
                        detail = _(
                            "SSO sign-in timed out. Complete sign-in in the "
                            "browser and try again."
                        )
            except Exception as exc:
                logger.debug("Bitwarden SSO login failed", exc_info=True)
                ok, detail = False, str(exc)

            def done():
                close()
                if ok:
                    _finish_ok()
                else:
                    _sso_page(error=_format_bitwarden_login_error(detail))
                return False

            GLib.idle_add(done)

        threading.Thread(target=worker, daemon=True).start()

    _method_page()


def _prompt_gui_login(window, controller, on_done: Callable[[bool], None]):
    """Configure the server if needed (daemon-side), then run the sign-in wizard."""
    def _after_server(url: str):
        if url is None:
            on_done(False)  # user cancelled the server prompt
            return
        if not url:
            _login_wizard(window, controller, on_done)
            return
        _set_status, close = progress_dialog(
            window, _("Bitwarden"), _("Configuring server…"),
        )

        def worker():
            ok = False
            detail = ""
            try:
                api_status = controller.bitwarden_configure_server(url)
                detail = str(getattr(api_status, "message", "") or "")
                ok = not bool(detail)
            except Exception as exc:
                logger.debug("Bitwarden server configuration failed", exc_info=True)
                detail = str(exc)
            GLib.idle_add(lambda: (_after_configure(ok, detail), False)[1])

        def _after_configure(ok, detail=""):
            close()
            if not ok:
                _show_error(
                    window,
                    _("Server configuration failed"),
                    detail or _(
                        "Could not set the Bitwarden server URL. Check the address and try again."
                    ),
                    on_closed=lambda: on_done(False),
                )
                return
            _login_wizard(window, controller, on_done)

        threading.Thread(target=worker, daemon=True).start()

    _prompt_server_url(window, controller, _after_server)


def _ensure_unlocked_then_ready(parent, controller, on_ready: Callable[[bool], None]):
    """Ensure the vault is unlocked after a successful sign-in, then finish.

    The daemon's ``bitwarden_login`` mirrors the CLI's "log in and unlock in one
    step"; when the vault is still locked (rare), unlock through the daemon.
    """
    def worker():
        unlocked = False
        try:
            api_status = controller.bitwarden_status(force_refresh=True)
            unlocked = bool(getattr(api_status, "unlocked", False))
        except Exception:
            unlocked = False
        if not unlocked:
            try:
                api_status = controller.bitwarden_unlock()
                unlocked = bool(getattr(api_status, "unlocked", False))
            except Exception:
                logger.debug("Bitwarden unlock after login failed", exc_info=True)
                unlocked = False
        GLib.idle_add(lambda: (on_ready(bool(unlocked)), False)[1])

    threading.Thread(target=worker, daemon=True).start()


def _unlock_then_ready(window, controller, on_ready: Callable[[bool], None]):
    if controller is None:
        on_ready(False)
        return
    _ensure_unlocked_then_ready(_modal_parent(window), controller, on_ready)


def _show_ready_dialog(window, on_done: Callable[[bool], None]):
    dlg = _message_dialog(
        window,
        _("Bitwarden is ready"),
        _("The Bitwarden CLI is installed, you are signed in, and your vault is "
          "unlocked. No further setup is needed.{source}").format(
            source=_bw_source_line(force_refresh=True),
        ),
    )
    dlg.connect("response", lambda *_a: on_done(True))
    dlg.present()


def _show_locked_dialog(window, controller, on_done: Callable[[bool], None]):
    dlg = _message_dialog(
        window,
        _("Vault locked"),
        _("You are signed in to Bitwarden but the vault is locked. Unlock now?"),
        responses=(("cancel", _("Cancel")), ("unlock", _("Unlock"))),
    )
    dlg.set_default_response("unlock")
    dlg.set_close_response("cancel")

    def _respond(_d, resp):
        if resp == "unlock":
            _unlock_then_ready(window, controller, on_done)
        else:
            on_done(False)

    dlg.connect("response", _respond)
    dlg.present()


def _show_signed_out_dialog(window, on_done: Callable[[bool], None]):
    dlg = _message_dialog(
        window,
        _("Not signed in"),
        _("The Bitwarden CLI is installed but you are not signed in. "
          "Continue to sign in?"),
        responses=(("cancel", _("Cancel")), ("continue", _("Continue"))),
    )
    dlg.set_default_response("continue")
    dlg.set_close_response("cancel")

    def _respond(_d, resp):
        on_done(resp == "continue")

    dlg.connect("response", _respond)
    dlg.present()


def ensure_bitwarden_ready(
    window,
    on_ready: Callable[[bool], None],
    *,
    install_if_missing: bool = True,
):
    """Ensure ``bw`` is installed, signed in, and unlocked; then ``on_ready(bool)``.

    All lifecycle work is routed through the daemon-backed secrets controller;
    only CLI-presence detection and the install dialogs run in this process.
    """
    controller = _resolve_controller(window)
    if controller is None:
        _message_dialog(
            window, _("Bitwarden unavailable"),
            _("The secret-backend daemon service is not available."),
        ).present()
        on_ready(False)
        return

    cancelled = {"v": False}

    def _cancel():
        cancelled["v"] = True
        on_ready(False)

    _set_status, _close_spinner = progress_dialog(
        window, _("Bitwarden"), _("Connecting to Bitwarden…"), on_cancel=_cancel,
    )

    def probe():
        status = probe_bitwarden_status(controller)
        GLib.idle_add(lambda: (_after_probe(status), False)[1])

    def _after_probe(status):
        if cancelled["v"]:
            return
        _close_spinner()
        if not status.cli_installed:
            if not install_if_missing:
                _no_cli_dialog(window).present()
                on_ready(False)
                return
            plan = detect_install_plan()
            if plan is None:
                _no_cli_dialog(window).present()
                on_ready(False)
                return
            _offer_install(window, plan, lambda ok: _after_install(ok))
            return
        _continue_signin(status.needs_login)

    def _after_install(installed):
        if not installed:
            on_ready(False)
            return
        status = probe_bitwarden_status(controller, force_refresh=True)
        _continue_signin(status.needs_login)

    def _continue_signin(needs_login):
        if not needs_login:
            _unlock_then_ready(window, controller, on_ready)
            return
        _prompt_gui_login(window, controller, on_ready)

    threading.Thread(target=probe, daemon=True).start()


def run_bitwarden_setup(
    window,
    on_done: Optional[Callable[[bool], None]] = None,
    *,
    interactive: bool = True,
):
    """Preferences entry: install, sign in, unlock.

    When ``interactive`` is True (manual “Set up…” button), shows status if Bitwarden
    is already fully ready instead of silently succeeding.
    """
    cb = on_done or (lambda _ok: None)
    controller = _resolve_controller(window)
    if controller is None:
        _message_dialog(
            window, _("Bitwarden unavailable"),
            _("The secret-backend daemon service is not available."),
        ).present()
        cb(False)
        return

    if not interactive:
        ensure_bitwarden_ready(window, cb, install_if_missing=True)
        return

    cancelled = {"v": False}

    def _cancel():
        cancelled["v"] = True
        cb(False)

    _set_status, _close_spinner = progress_dialog(
        window, _("Bitwarden"), _("Checking Bitwarden…"), on_cancel=_cancel,
    )

    def worker():
        status = probe_bitwarden_status(controller)
        GLib.idle_add(lambda: (_after_check(status), False)[1])

    def _confirm(ok):
        """Show a 'Bitwarden is ready' confirmation on success, else report failure."""
        if ok:
            _show_ready_dialog(window, cb)
        else:
            cb(False)

    def _after_check(status: BitwardenStatus):
        if cancelled["v"]:
            return
        _close_spinner()
        if status.is_ready:
            _show_ready_dialog(window, cb)
            return
        if status.cli_installed and not status.needs_login and not status.unlocked:
            _show_locked_dialog(window, controller, _confirm)
            return
        if status.cli_installed and status.needs_login:
            def _cont(ok):
                if not ok:
                    cb(False)
                    return
                ensure_bitwarden_ready(
                    window, _confirm, install_if_missing=False,
                )
            _show_signed_out_dialog(window, _cont)
            return
        ensure_bitwarden_ready(window, _confirm, install_if_missing=True)

    threading.Thread(target=worker, daemon=True).start()


def _no_cli_dialog(window):
    from .platform_utils import get_managed_bw_cli_path

    return _message_dialog(
        window, _("Bitwarden CLI not found"),
        _("Install the Bitwarden CLI (the “bw” command) and try again. "
          "Use “Set up Bitwarden…” in Preferences to download the official "
          "binary automatically, or get it from:\n\n  {url}\n\n"
          "sshPilot installs to:\n\n    {path}\n\n"
          "For self-hosted or EU cloud, set the server in the setup wizard before signing in."
        ).format(url=BW_CLI_DOWNLOAD, path=get_managed_bw_cli_path()),
    )
