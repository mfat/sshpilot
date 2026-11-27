"""Keep the AppImage's own environment away from the programs it starts.

AppRun (packaging/appimage/AppRun) sets what the bundled GTK and WebKit can
only find through the environment -- typelib, GIO module, schema, pixbuf and
WebKit injected-bundle paths -- and starts the app in ``$APPDIR/usr/lib``,
where the WebKit helper path patched into the bundled library resolves. All of
that is right for the bundled interpreter and wrong for the host's programs:
a shell or ssh the user gets from SSH Pilot would otherwise load our typelibs
and GIO modules, and start inside the read-only AppImage mount.

AppRun therefore records what it changed: ``SSHPILOT_APPIMAGE_VARS`` names
every variable it set, ``SSHPILOT_APPIMAGE_HOST_<NAME>`` holds a host value it
replaced (absent when the host had none) and ``SSHPILOT_APPIMAGE_CWD`` the
directory the AppImage was started from. Outside the AppImage none of these
exist and every function here is a no-op.

Programs inside the AppImage (the daemon, the askpass helper, a restart) keep
the bundled environment; every other program gets the host's back. Local shells
go through :func:`host_environment` explicitly, because VTE spawns them from C;
Python-level spawns are covered by :func:`install_spawn_guard`.
"""

from __future__ import annotations

import functools
import inspect
import os
import shutil
import subprocess
from typing import Any, Mapping, MutableMapping, Optional

VARS_KEY = "SSHPILOT_APPIMAGE_VARS"
HOST_PREFIX = "SSHPILOT_APPIMAGE_HOST_"
CWD_KEY = "SSHPILOT_APPIMAGE_CWD"
_BOOKKEEPING_PREFIX = "SSHPILOT_APPIMAGE_"

_guard_installed = False
_webkit_prepared = False


def active(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether this process was started by the AppImage's AppRun."""
    environ = os.environ if environ is None else environ
    return bool(environ.get(VARS_KEY)) and bool(environ.get("APPDIR"))


def host_environment(env: Mapping[str, str]) -> dict:
    """Return *env* with every AppRun-made change undone.

    Variables AppRun replaced get the host's value back, the ones it added
    are dropped, and so is the bookkeeping itself, so a program started from
    the result sees the environment the AppImage was launched with (plus
    whatever SSH Pilot added on purpose, such as ``SSH_AUTH_SOCK``).
    """
    result = dict(env)
    managed = (env.get(VARS_KEY) or "").split()
    if not managed:
        return result
    for name in managed:
        saved = env.get(HOST_PREFIX + name)
        if saved is None:
            result.pop(name, None)
        else:
            result[name] = saved
    for key in list(result):
        if key.startswith(_BOOKKEEPING_PREFIX):
            del result[key]
    return result


def host_cwd() -> Optional[str]:
    """The directory the AppImage was started from, if it still exists."""
    if not active():
        return None
    cwd = os.environ.get(CWD_KEY)
    if cwd and os.path.isdir(cwd):
        return cwd
    return os.path.expanduser("~") or "/"


def _appdir() -> Optional[str]:
    appdir = os.environ.get("APPDIR")
    return os.path.realpath(appdir) if appdir else None


def is_bundled(program: Any, env: Optional[Mapping[str, str]] = None) -> bool:
    """Whether *program* (a path or a name looked up on PATH) is ours."""
    appdir = _appdir()
    if not appdir or program is None:
        return False
    program = os.fsdecode(program)
    if os.sep not in program:
        search_path = (env if env is not None else os.environ).get("PATH")
        found = shutil.which(program, path=search_path)
        if not found:
            return False
        program = found
    resolved = os.path.realpath(program)
    return resolved == appdir or resolved.startswith(appdir + os.sep)


def _environment_for(program: Any, env: Optional[Mapping[str, str]]) -> Optional[MutableMapping]:
    """The environment *program* should get: ours if bundled, else the host's."""
    source = os.environ if env is None else env
    if is_bundled(program, source):
        return None if env is None else env
    return host_environment(source)


def _program_of(args: Any, executable: Any, shell: bool) -> Any:
    if executable is not None:
        return executable
    if shell:
        return "/bin/sh"
    if isinstance(args, (str, bytes, os.PathLike)):
        return args
    try:
        return args[0]
    except (IndexError, TypeError, KeyError):
        return None


def install_spawn_guard() -> None:
    """Give host programs the host environment and starting directory.

    Wraps ``subprocess.Popen`` (and with it ``subprocess.run`` and asyncio's
    subprocesses) and the ``os.exec*`` / ``os.posix_spawn*`` calls that take
    an environment. A Popen without ``cwd`` starts in the AppImage's launch
    directory instead of the WebKit working directory. Idempotent; does
    nothing outside the AppImage.
    """
    global _guard_installed
    if _guard_installed or not active():
        return
    _guard_installed = True

    original_init = subprocess.Popen.__init__
    signature = inspect.signature(original_init)

    @functools.wraps(original_init)
    def guarded_init(self, *args, **kwargs):
        try:
            bound = signature.bind(self, *args, **kwargs)
        except TypeError:
            return original_init(self, *args, **kwargs)
        arguments = bound.arguments
        program = _program_of(
            arguments.get("args"),
            arguments.get("executable"),
            bool(arguments.get("shell", False)),
        )
        arguments["env"] = _environment_for(program, arguments.get("env"))
        if arguments.get("cwd") is None:
            cwd = host_cwd()
            if cwd:
                arguments["cwd"] = cwd
        return original_init(*bound.args, **bound.kwargs)

    subprocess.Popen.__init__ = guarded_init

    original_execve = os.execve
    original_execvpe = os.execvpe

    def execv(path, args):
        return original_execve(path, args, _environment_for(path, None) or os.environ)

    def execve(path, args, env):
        return original_execve(path, args, _environment_for(path, env))

    def execvp(file, args):
        return original_execvpe(file, args, _environment_for(file, None) or os.environ)

    def execvpe(file, args, env):
        return original_execvpe(file, args, _environment_for(file, env))

    os.execv, os.execve, os.execvp, os.execvpe = execv, execve, execvp, execvpe

    for name in ("posix_spawn", "posix_spawnp"):
        original = getattr(os, name, None)
        if original is None:
            continue

        def spawn(path, argv, env, *rest, _original=original, **kwargs):
            return _original(path, argv, _environment_for(path, env), *rest, **kwargs)

        setattr(os, name, spawn)


def vte_spawn_flags(glib_module: Any, vte_module: Any) -> Any:
    """GLib spawn flags for a VTE spawn of a host program.

    VTE adds the caller's ``envv`` to the app's own environment instead of
    replacing it, so a variable :func:`host_environment` removed (typelib,
    GIO module, WebKit paths) would come back from the parent. Inside the
    AppImage, ``SPAWN_NO_PARENT_ENVV`` makes ``envv`` the whole environment;
    the callers always pass a complete one. Elsewhere: the default flags.
    """
    flags = glib_module.SpawnFlags.DEFAULT
    no_parent = getattr(vte_module, "SPAWN_NO_PARENT_ENVV", None)
    if active() and no_parent is not None:
        flags = glib_module.SpawnFlags(int(flags) | int(no_parent))
    return flags


def prepare_webkit(webkit_module: Any) -> None:
    """Let WebKit's sandbox see the AppImage before the first web process.

    The patched helper path is relative to the working directory, which lies
    inside the AppImage mount; bubblewrap only shows the WebProcess what it is
    told to bind. Read-only, and only the AppDir. Call before creating a
    WebView; repeated calls do nothing.
    """
    global _webkit_prepared
    if _webkit_prepared or not active():
        return
    _webkit_prepared = True
    appdir = _appdir()
    try:
        context = webkit_module.WebContext.get_default()
        context.add_path_to_sandbox(appdir, True)
    except Exception:  # noqa: BLE001 - a WebView without it still works on matching hosts
        import logging

        logging.getLogger(__name__).warning(
            "Could not add the AppImage to WebKit's sandbox", exc_info=True
        )
