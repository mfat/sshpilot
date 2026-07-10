"""Platform-related utility functions."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

from gi.repository import GLib

APP_ID = "io.github.mfat.sshpilot"
APP_NAME = "sshpilot"


def is_macos() -> bool:
    """Return True if running on macOS."""
    return platform.system() == "Darwin"


def is_windows() -> bool:
    """Return True if running on Windows."""
    return platform.system() == "Windows"


_BITWARDEN_DESKTOP_APP = "com.bitwarden.desktop"
_BW_VERIFY_TIMEOUT = 20
_bw_binding_cache: Optional[Tuple["BwCliBinding", float]] = None
_BW_CACHE_TTL = 30.0
_host_env_cache: dict[str, Optional[str]] = {}


@dataclass(frozen=True)
class BwCliBinding:
    """A verified (or candidate) Bitwarden CLI invocation."""

    argv_prefix: Tuple[str, ...]
    source: str


def _verify_bw_argv(argv_prefix: List[str]) -> bool:
    """True when ``bw --version`` succeeds for this argv prefix."""
    try:
        result = subprocess.run(
            argv_prefix + ["--version"],
            capture_output=True, text=True, timeout=_BW_VERIFY_TIMEOUT, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def _flatpak_bitwarden_cli_prefix(flatpak_argv: List[str]) -> Optional[List[str]]:
    """Return argv prefix for ``bw`` bundled in the Bitwarden desktop Flatpak, if installed."""
    try:
        result = subprocess.run(
            flatpak_argv + ["info", _BITWARDEN_DESKTOP_APP],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return flatpak_argv + ["run", "--command=bw", _BITWARDEN_DESKTOP_APP]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _host_env(name: str) -> Optional[str]:
    """Read an environment variable from the Flatpak host, cached."""
    if name in _host_env_cache:
        return _host_env_cache[name]
    value: Optional[str] = None
    if is_flatpak():
        spawn = shutil.which("flatpak-spawn")
        if spawn:
            try:
                result = subprocess.run(
                    [spawn, "--host", "printenv", name],
                    capture_output=True, text=True, timeout=10, check=False,
                )
                if result.returncode == 0 and (result.stdout or "").strip():
                    value = (result.stdout or "").strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
    else:
        value = os.environ.get(name) or None
    _host_env_cache[name] = value
    return value


def get_managed_bw_cli_dir() -> str:
    """Directory where sshPilot installs the Bitwarden CLI (``$XDG_DATA_HOME/sshpilot/bin``)."""
    if is_flatpak():
        home = _host_env("HOME")
        if home:
            data_home = _host_env("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
            return os.path.join(data_home, APP_NAME, "bin")
    return os.path.join(get_data_dir(), "bin")


def _legacy_managed_bw_cli_path() -> str:
    """Previous install location (``$XDG_STATE_HOME/sshpilot/bin/bw``), for discovery only."""
    if is_flatpak():
        home = _host_env("HOME")
        if home:
            state_home = _host_env("XDG_STATE_HOME") or os.path.join(home, ".local", "state")
            return os.path.join(state_home, APP_NAME, "bin", "bw")
    return os.path.join(get_state_dir(), "bin", "bw")


def get_managed_bw_cli_path() -> str:
    """Absolute path where sshPilot installs the Bitwarden CLI binary."""
    return os.path.join(get_managed_bw_cli_dir(), "bw")


def _managed_bw_cli_argv(path: str) -> Optional[List[str]]:
    """Return an argv prefix for a managed ``bw`` install at ``path``, if executable."""
    if is_flatpak():
        spawn = shutil.which("flatpak-spawn")
        if not spawn:
            return None
        try:
            result = subprocess.run(
                [spawn, "--host", "test", "-x", path],
                capture_output=True, text=True, timeout=10, check=False,
            )
            if result.returncode != 0:
                return None
            return [spawn, "--host", path]
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return [path]
    return None


def _discover_bw_cli_bindings(*, path_only: bool = True) -> List[BwCliBinding]:
    """Candidate ``bw`` sources without running them.

    ``path_only`` (default): only ``bw`` on PATH (incl. Flatpak host spawn).
    When False, also includes the Bitwarden desktop Flatpak CLI (not on PATH).
    """
    out: List[BwCliBinding] = []
    found = resolve_host_binary("bw")
    if found:
        if found[0] == shutil.which("flatpak-spawn") or found[0].endswith("flatpak-spawn"):
            out.append(BwCliBinding(tuple(found), "Host PATH (flatpak-spawn --host bw)"))
        elif len(found) == 1 and os.path.isabs(found[0]):
            out.append(BwCliBinding(tuple(found), f"PATH ({found[0]})"))
        else:
            out.append(BwCliBinding(tuple(found), "PATH (bw)"))
    managed = get_managed_bw_cli_path()
    managed_argv = _managed_bw_cli_argv(managed)
    if managed_argv and not any(list(c.argv_prefix) == managed_argv for c in out):
        out.append(BwCliBinding(tuple(managed_argv), f"sshPilot install ({managed})"))
    elif not managed_argv:
        legacy = _legacy_managed_bw_cli_path()
        if legacy != managed:
            legacy_argv = _managed_bw_cli_argv(legacy)
            if legacy_argv and not any(list(c.argv_prefix) == legacy_argv for c in out):
                out.append(BwCliBinding(tuple(legacy_argv), f"sshPilot install ({legacy})"))
    if path_only:
        return out
    flatpak = shutil.which("flatpak")
    if flatpak:
        prefix = _flatpak_bitwarden_cli_prefix([flatpak])
        if prefix:
            out.append(BwCliBinding(
                tuple(prefix),
                f"Bitwarden desktop Flatpak ({_BITWARDEN_DESKTOP_APP})",
            ))
    if is_flatpak():
        spawn = shutil.which("flatpak-spawn")
        if spawn:
            prefix = _flatpak_bitwarden_cli_prefix([spawn, "--host", "flatpak"])
            if prefix:
                out.append(BwCliBinding(
                    tuple(prefix),
                    f"Bitwarden desktop Flatpak on host ({_BITWARDEN_DESKTOP_APP})",
                ))
    return out


def resolve_bw_cli_binding(
    *,
    verify: bool = True,
    force_refresh: bool = False,
    path_only: bool = True,
) -> Optional[BwCliBinding]:
    """Return the first usable Bitwarden CLI binding, verified with ``bw --version``.

    By default only considers ``bw`` on PATH (``path_only=True``). The Bitwarden
    desktop Flatpak bundles a ``bw`` command but it is not on PATH — use
    ``path_only=False`` only when explicitly probing that fallback.
    """
    global _bw_binding_cache
    now = time.monotonic()
    if (
        path_only
        and not force_refresh
        and _bw_binding_cache is not None
        and now - _bw_binding_cache[1] < _BW_CACHE_TTL
    ):
        return _bw_binding_cache[0]

    binding: Optional[BwCliBinding] = None
    for candidate in _discover_bw_cli_bindings(path_only=path_only):
        verified = (not verify) or _verify_bw_argv(list(candidate.argv_prefix))
        if verified:
            binding = candidate
            break
    if path_only:
        _bw_binding_cache = (binding, now) if binding else None
    return binding


def invalidate_bw_cli_cache() -> None:
    """Drop cached ``bw`` discovery (e.g. after install/uninstall)."""
    global _bw_binding_cache
    _bw_binding_cache = None


def resolve_bw_cli(*, force_refresh: bool = False) -> Optional[List[str]]:
    """Return an argv prefix for the Bitwarden CLI (``bw``), verified when possible."""
    binding = resolve_bw_cli_binding(force_refresh=force_refresh)
    if binding is None:
        return None
    return list(binding.argv_prefix)


def describe_bw_cli_source(*, force_refresh: bool = False) -> Optional[str]:
    """Human-readable label for the active ``bw`` source, if any."""
    binding = resolve_bw_cli_binding(force_refresh=force_refresh)
    return binding.source if binding else None


def resolve_bw_cli_path(*, force_refresh: bool = False) -> Optional[str]:
    """Absolute path to the ``bw`` executable sshPilot will run, if any."""
    binding = resolve_bw_cli_binding(force_refresh=force_refresh)
    if binding is None:
        return None
    argv = list(binding.argv_prefix)
    if not argv:
        return None
    if argv[0].endswith("flatpak-spawn") and len(argv) >= 3:
        candidate = argv[-1]
        return candidate if os.path.isabs(candidate) else None
    if len(argv) == 1 and os.path.isabs(argv[0]):
        return argv[0]
    return None


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

