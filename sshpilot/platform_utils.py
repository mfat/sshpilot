"""Platform-related utility functions."""

import os
import platform
import shutil
import subprocess
from typing import List, Optional

from gi.repository import GLib

APP_ID = "io.github.mfat.sshpilot"
APP_NAME = "sshpilot"


def is_macos() -> bool:
    """Return True if running on macOS."""
    return platform.system() == "Darwin"


def is_flatpak() -> bool:
    """Return True if running inside a Flatpak sandbox."""
    return os.environ.get("FLATPAK_ID") is not None or os.path.exists("/.flatpak-info")


def resolve_host_binary(binary: str) -> Optional[List[str]]:
    """Return an argv *prefix* that runs ``binary``:

    * ``[<abs path>]`` when it is in the sandbox ``PATH``;
    * ``[flatpak-spawn, --host, binary]`` when it exists only on the Flatpak host;
    * ``None`` when it cannot be found either way.

    Callers append their own arguments to the returned list.
    """
    found = shutil.which(binary)
    if found:
        return [found]
    if is_flatpak():
        spawn = shutil.which("flatpak-spawn")
        if spawn:
            try:
                result = subprocess.run(
                    [spawn, "--host", "which", binary],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if result.returncode == 0 and (result.stdout or "").strip():
                    return [spawn, "--host", binary]
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
    return None


def get_config_dir() -> str:
    """Return the per-user configuration directory for sshPilot."""
    return os.path.join(GLib.get_user_config_dir(), APP_NAME)


def get_data_dir() -> str:
    """Return the per-user data directory for sshPilot."""
    return os.path.join(GLib.get_user_data_dir(), APP_NAME)


def get_state_dir() -> str:
    """Return the per-user state directory for sshPilot.

    Per the XDG Base Directory specification, ``$XDG_STATE_HOME`` (default
    ``~/.local/state``) is the right home for *state* that should survive
    restarts but isn't important enough for ``$XDG_DATA_HOME`` — which the
    spec explicitly lists as covering "actions history (logs, history,
    recently used files, …)" and the current state of the application.

    Prefers :func:`GLib.get_user_state_dir` when available (GLib ≥ 2.72)
    so Flatpak and other portal-mediated environments stay consistent with
    GLib's own resolution; otherwise resolves the spec by hand.
    """
    if hasattr(GLib, "get_user_state_dir"):
        try:
            return os.path.join(GLib.get_user_state_dir(), APP_NAME)
        except Exception:
            pass
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(state_home, APP_NAME)


def _build_restart_command(executable, argv, main_spec):
    """Build the argv list to re-exec the app, preserving how it was launched.

    ``main_spec`` is ``__main__.__spec__`` (or ``None``). When the app was
    started with ``python -m <module>`` (Flatpak uses ``-m sshpilot.main``),
    ``__spec__`` is set and ``argv[0]`` is just the module's file path — running
    that path directly would execute it as a top-level script with no package
    context, breaking the module-level relative imports. In that case re-exec via
    ``-m <spec.name>`` so the package context is preserved. Otherwise (a plain
    script like ``run.py`` or a console entry point, where ``__spec__`` is None)
    re-exec the original argv unchanged.
    """
    name = getattr(main_spec, 'name', None) if main_spec is not None else None
    if name:
        return [executable, '-m', name] + list(argv[1:])
    return [executable] + list(argv)


def restart_app() -> None:
    """Replace the current process with a fresh instance of the same app.

    Works on Linux (including Flatpak) and macOS.  The setting must be
    persisted before this is called — os.execv replaces the process
    immediately with no further cleanup.
    """
    import sys
    import __main__
    # os.execv keeps the current environment (e.g. SSHPILOT_FLATPAK).
    args = _build_restart_command(
        sys.executable, sys.argv, getattr(__main__, '__spec__', None)
    )
    os.execv(sys.executable, args)


_sshpass_path_cache: str | None = None
_sshpass_checked: bool = False


def get_sshpass_path() -> str | None:
    """Return the path to the sshpass binary, or None if unavailable.

    The result is cached after the first call so repeated lookups are free.
    Checks the Flatpak-bundled location first, then falls back to PATH.
    """
    global _sshpass_path_cache, _sshpass_checked
    if _sshpass_checked:
        return _sshpass_path_cache

    flatpak_path = "/app/bin/sshpass"
    if os.path.exists(flatpak_path) and os.access(flatpak_path, os.X_OK):
        _sshpass_path_cache = flatpak_path
    else:
        _sshpass_path_cache = shutil.which("sshpass")

    _sshpass_checked = True
    return _sshpass_path_cache


def get_ssh_dir() -> str:
    """Return the user's SSH directory.

    By default this uses GLib's concept of the home directory and appends
    ``.ssh``. The location can be overridden by setting the
    ``SSHPILOT_SSH_DIR`` environment variable.
    """
    override = os.environ.get("SSHPILOT_SSH_DIR")
    if override:
        return os.path.expanduser(override)
    return os.path.join(GLib.get_home_dir(), ".ssh")

