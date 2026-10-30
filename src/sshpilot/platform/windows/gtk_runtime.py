"""Pin the bundled GTK/GI DLLs before ``gi.repository`` is imported on Windows.

Issue #1357 crashed at ``from gi.repository import Adw, Gtk, Gio, ...`` with
``TypeError: must be an interface`` while wrapping ``Gio.ActionMap``. That
error is what PyGObject raises when the Gio typelib is present but
``gio-2.0-0.dll`` failed to load (or a different copy registered the GType).

The frozen 6.3.6 bundle ships ``girepository-2.0-0.dll``. gvsbuild's PyGObject
3.56.3 patch only calls ``os.add_dll_directory`` if it finds
``girepository-1.0-1.dll`` on PATH, and even then it uses a ``with`` block that
removes the directory again after ``_gi`` imports. Python 3.8+ does not search
PATH for extension-module dependencies; GLib's ``g_module_open`` still uses
``LoadLibrary``, which newer Windows builds have been tightening.

This module, called before any ``gi`` import:

* permanently ``add_dll_directory``s the bundle (or ``vendor\\gtk\\bin``);
* prepends those directories to ``PATH`` and ``PYGI_DLL_PATH``;
* points ``GI_TYPELIB_PATH`` at every bundled typelib directory;
* preloads the GI/GTK DLLs so ``g_module_open`` finds them already mapped;
* writes ``startup-gtk.log`` so a windowed crash is not silent (``--verbose``
  only starts after GTK is imported).
"""

from __future__ import annotations

import os
import sys
import traceback
from dataclasses import dataclass, field
from typing import Iterable, List, MutableMapping, Optional, Sequence

# Markers used to recognise a GTK/GI bindir. The frozen 6.3.6 bundle has
# girepository-2.0; older gvsbuild zips had girepository-1.0-1.
_GI_DLL_MARKERS = (
    "girepository-2.0-0.dll",
    "girepository-1.0-1.dll",
    "gio-2.0-0.dll",
    "gtk-4-1.dll",
)

# Load order: GLib stack first, then GTK. Missing names are skipped.
_PRELOAD_DLLS = (
    "intl-8.dll",
    "pcre2-8-0.dll",
    "ffi-8.dll",
    "zlib1.dll",
    "glib-2.0-0.dll",
    "gobject-2.0-0.dll",
    "gmodule-2.0-0.dll",
    "gio-2.0-0.dll",
    "girepository-2.0-0.dll",
    "girepository-1.0-1.dll",
    "cairo-2.dll",
    "graphene-1.0-0.dll",
    "harfbuzz.dll",
    "pango-1.0-0.dll",
    "pangocairo-1.0-0.dll",
    "gdk_pixbuf-2.0-0.dll",
    "epoxy-0.dll",
    "gtk-4-1.dll",
    "adwaita-1-0.dll",
)

_TYPELIB_SUBDIRS = (
    "gi_typelibs",
    os.path.join("lib", "girepository-1.0"),
    os.path.join("lib", "girepository-2.0"),
    "girepository-1.0",
    "girepository-2.0",
)

# Keep add_dll_directory cookies alive: closing/GC removes the directory from
# the search path (the gvsbuild PyGObject patch used `with` and dropped them).
_DLL_DIRECTORY_COOKIES: List[object] = []
_CONFIGURED = False
_LAST_SETUP: Optional["GtkRuntimeSetup"] = None


@dataclass
class GtkRuntimeSetup:
    """What :func:`configure` did; useful for tests and the startup log."""

    dll_dirs: List[str] = field(default_factory=list)
    typelib_dirs: List[str] = field(default_factory=list)
    preloaded: List[str] = field(default_factory=list)
    log_path: Optional[str] = None
    skipped: Optional[str] = None


def _source_checkout_root() -> str:
    # src/sshpilot/platform/windows/gtk_runtime.py -> checkout root
    return os.path.abspath(os.path.join(os.path.dirname(__file__), *[os.pardir] * 4))


def _is_gi_dir(directory: str) -> bool:
    return any(os.path.isfile(os.path.join(directory, name)) for name in _GI_DLL_MARKERS)


def discover_dll_dirs(
    *,
    frozen: Optional[bool] = None,
    meipass: Optional[str] = None,
    executable: Optional[str] = None,
    environ: Optional[MutableMapping[str, str]] = None,
) -> List[str]:
    """GTK/GI directories, bundled copies first so a foreign copy on PATH loses."""

    environ = os.environ if environ is None else environ
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if meipass is None:
        meipass = getattr(sys, "_MEIPASS", "") or ""
    if executable is None:
        executable = sys.executable

    candidates: List[str] = []
    if frozen:
        for directory in (meipass, os.path.dirname(executable)):
            if directory:
                candidates.append(directory)
                internal = os.path.join(directory, "_internal")
                if internal != directory:
                    candidates.append(internal)
    else:
        vendor_bin = os.path.join(_source_checkout_root(), "vendor", "gtk", "bin")
        candidates.append(vendor_bin)

    for entry in environ.get("PATH", "").split(os.pathsep):
        if entry:
            candidates.append(entry)

    found: List[str] = []
    seen = set()
    for directory in candidates:
        normalized = os.path.normcase(os.path.normpath(directory))
        if not directory or normalized in seen:
            continue
        if not _is_gi_dir(directory):
            continue
        seen.add(normalized)
        found.append(os.path.normpath(directory))
    return found


def discover_typelib_dirs(
    dll_dirs: Sequence[str],
    *,
    meipass: Optional[str] = None,
) -> List[str]:
    """Every directory next to the GI DLLs that actually holds ``*.typelib``."""

    roots: List[str] = []
    if meipass is None:
        meipass = getattr(sys, "_MEIPASS", "") or ""
    if meipass:
        roots.append(meipass)
    for directory in dll_dirs:
        roots.append(directory)
        parent = os.path.dirname(directory)
        if os.path.basename(directory).lower() == "bin" and parent:
            roots.append(parent)

    found: List[str] = []
    seen = set()
    for root in roots:
        for subdir in _TYPELIB_SUBDIRS:
            path = os.path.normpath(os.path.join(root, subdir))
            key = os.path.normcase(path)
            if key in seen:
                continue
            seen.add(key)
            try:
                names = os.listdir(path)
            except OSError:
                continue
            if any(name.endswith(".typelib") for name in names):
                found.append(path)
    return found


def _prepend_path_env(
    environ: MutableMapping[str, str], name: str, directories: Sequence[str]
) -> None:
    existing = [entry for entry in environ.get(name, "").split(os.pathsep) if entry]
    seen = {os.path.normcase(os.path.normpath(entry)) for entry in directories}
    rest = [
        entry
        for entry in existing
        if os.path.normcase(os.path.normpath(entry)) not in seen
    ]
    environ[name] = os.pathsep.join([*directories, *rest])


def _add_dll_directories(directories: Sequence[str]) -> List[str]:
    added: List[str] = []
    adder = getattr(os, "add_dll_directory", None)
    if adder is None:
        return added
    for directory in directories:
        try:
            cookie = adder(directory)
        except (OSError, FileNotFoundError):
            continue
        _DLL_DIRECTORY_COOKIES.append(cookie)
        added.append(directory)
    return added


def _preload_dlls(directories: Sequence[str]) -> List[str]:
    if sys.platform != "win32":
        return []
    import ctypes

    loaded: List[str] = []
    for name in _PRELOAD_DLLS:
        for directory in directories:
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                ctypes.WinDLL(path)
            except OSError:
                continue
            loaded.append(path)
            break
    return loaded


def startup_log_path() -> str:
    from sshpilot.platform.paths import get_state_dir

    return str(get_state_dir() / "startup-gtk.log")


def _write_log(path: str, lines: Iterable[str]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def configure(
    *,
    environ: Optional[MutableMapping[str, str]] = None,
    frozen: Optional[bool] = None,
    meipass: Optional[str] = None,
    executable: Optional[str] = None,
    write_log: bool = True,
) -> GtkRuntimeSetup:
    """Make the bundled GTK/GI runtime findable. Idempotent, Windows-only."""

    global _CONFIGURED, _LAST_SETUP
    if sys.platform != "win32":
        setup = GtkRuntimeSetup(skipped="not-windows")
        _LAST_SETUP = setup
        return setup
    if _CONFIGURED and _LAST_SETUP is not None and environ is None:
        return _LAST_SETUP

    environ = os.environ if environ is None else environ
    dll_dirs = discover_dll_dirs(
        frozen=frozen, meipass=meipass, executable=executable, environ=environ
    )
    typelib_dirs = discover_typelib_dirs(dll_dirs, meipass=meipass)
    if dll_dirs:
        _add_dll_directories(dll_dirs)
        _prepend_path_env(environ, "PATH", dll_dirs)
        _prepend_path_env(environ, "PYGI_DLL_PATH", dll_dirs)
    if typelib_dirs:
        _prepend_path_env(environ, "GI_TYPELIB_PATH", typelib_dirs)

    preloaded = _preload_dlls(dll_dirs) if environ is os.environ else []

    log_path = None
    if write_log and environ is os.environ:
        try:
            log_path = startup_log_path()
            lines = [
                "sshPilot Windows GTK/GI runtime",
                f"frozen={bool(getattr(sys, 'frozen', False) if frozen is None else frozen)}",
                f"executable={executable or sys.executable}",
                f"meipass={meipass if meipass is not None else getattr(sys, '_MEIPASS', '')}",
                "dll_dirs:",
            ]
            lines.extend(f"  {directory}" for directory in dll_dirs or ["(none)"])
            lines.append("typelib_dirs:")
            lines.extend(f"  {directory}" for directory in typelib_dirs or ["(none)"])
            lines.extend(
                [
                    f"PATH prefixes={os.pathsep.join(dll_dirs)}",
                    f"PYGI_DLL_PATH={environ.get('PYGI_DLL_PATH', '')}",
                    f"GI_TYPELIB_PATH={environ.get('GI_TYPELIB_PATH', '')}",
                    "preloaded:",
                ]
            )
            lines.extend(f"  {path}" for path in preloaded or ["(none)"])
            _write_log(log_path, lines)
        except OSError:
            log_path = None

    setup = GtkRuntimeSetup(
        dll_dirs=dll_dirs,
        typelib_dirs=typelib_dirs,
        preloaded=preloaded,
        log_path=log_path,
    )
    if environ is os.environ:
        _CONFIGURED = True
        _LAST_SETUP = setup
    return setup


def log_import_failure(exc: Optional[BaseException] = None) -> Optional[str]:
    """Append a GI import failure to ``startup-gtk.log`` (best-effort)."""

    if sys.platform != "win32":
        return None
    try:
        path = (_LAST_SETUP.log_path if _LAST_SETUP else None) or startup_log_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\nGI import failed\n")
            if exc is not None:
                handle.write(f"{type(exc).__name__}: {exc}\n")
            handle.write(traceback.format_exc())
            if "must be an interface" in f"{exc}":
                handle.write(
                    "\nThis usually means Gio's typelib loaded but gio-2.0-0.dll "
                    "did not (or a different copy did). "
                    "See dll_dirs / preloaded above.\n"
                )
        return path
    except OSError:
        return None


def reset_for_tests() -> None:
    """Drop process-wide configure state (unit tests only)."""

    global _CONFIGURED, _LAST_SETUP
    _CONFIGURED = False
    _LAST_SETUP = None
    _DLL_DIRECTORY_COOKIES.clear()
