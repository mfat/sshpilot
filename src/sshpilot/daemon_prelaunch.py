"""Locate or start the daemon while the GUI is still loading GTK.

Before this, the first ``DaemonLauncher.connect_or_start()`` ran at the end of
``MainWindow.__init__``: after GTK/Adwaita, the window module, the UI build and
plugin loading. On a cold start the daemon's own imports and repository load
then came on top of all of that. :func:`start` runs the launcher on a
background thread from the entry point instead, so the daemon starts in
parallel with the GUI, and :func:`take` hands the result to the first client
selection.

The early attempt never evicts a resident daemon (see
``DaemonLauncher.connect_or_start_without_eviction``): this process may turn
out to be a secondary instance that only hands its command line to a running
window. When eviction would be needed, :func:`take` returns ``None`` and the
window's ordinary ``connect_or_start`` does it, exactly as before. Any other
failure is re-raised from :func:`take`, so daemon-unavailable handling is the
same as without the early start. Two launches that both start the daemon are
settled by the launcher's ready-wait, which waits for whichever daemon won the
socket. A secondary instance closes the client it opened through
:func:`discard` when ``main()`` returns.

Before the worker starts, this process exports the environment the daemon
inherits: verbosity, and the interface language, which reads the config
directory through GLib (except on Windows) and therefore comes after the gi
version pins.

With no daemon endpoint the worker spawns the daemon at once, so the daemon's
own start overlaps all of the GUI's (~300 ms sooner than when the spawn waited
for gi). With one, it waits for :func:`release_connect`, which ``sshpilot.main``
calls as it starts importing GTK: the connection loads the client stack
(DaemonClient, codec, API models), and that Python work slows whatever the GUI
thread imports beside it; next to the GTK import it costs the least.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
import threading
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

#: Set to ``1`` to skip the early start (the window then starts the daemon).
DISABLE_ENV = "SSHPILOT_NO_DAEMON_PRELAUNCH"

_lock = threading.Lock()
_state: Optional["_Prelaunch"] = None
_connect_released = threading.Event()
#: Longest a worker holds back its connection to a running daemon.
_CONNECT_RELEASE_WAIT_SECONDS = 3.0


class _Prelaunch:
    def __init__(self, verbose: bool, quiet: bool) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self.launcher = None
        self.result = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        #: Claimed by :func:`take` (handed over) or :func:`discard` (closed).
        self.taken = False
        self.thread = threading.Thread(
            target=self._run, name="sshpilot-daemon-prelaunch", daemon=True
        )

    def _run(self) -> None:
        try:
            from .daemon.launcher import DaemonLauncher

            self.launcher = DaemonLauncher(verbose=self.verbose, quiet=self.quiet)
            discard_dead_endpoint = getattr(
                self.launcher, "discard_dead_endpoint", None
            )
            if callable(discard_dead_endpoint):
                # A crashed daemon's endpoint is nothing to wait for.
                discard_dead_endpoint()
            socket_path = getattr(self.launcher, "socket_path", None)
            if socket_path is not None and os.path.lexists(socket_path):
                _connect_released.wait(_CONNECT_RELEASE_WAIT_SECONDS)
            self.result = self.launcher.connect_or_start_without_eviction()
        except BaseException as error:  # handed to the selection that takes it
            self.error = error
        finally:
            with _lock:
                self.done.set()
                abandoned = self.taken
                result = self.result if abandoned else None
                if abandoned:
                    self.result = None
            # discard() ran while this attempt was still in flight.
            _close_result(result)


def _close_result(result) -> None:
    if result is None:
        return
    try:
        result.client.close()
    except Exception:
        logger.debug("Could not close the unused early daemon client", exc_info=True)


def apply_verbosity_environment(verbose: bool, quiet: bool) -> None:
    """Export the verbosity a launched daemon inherits.

    Mirrors ``SshPilotApplication.__init__``, which sets the same variables
    again once the application exists.
    """
    verbose = verbose and not quiet
    quiet = quiet and not verbose
    if verbose:
        os.environ["SSHPILOT_DAEMON_VERBOSE"] = "1"
    else:
        os.environ.pop("SSHPILOT_DAEMON_VERBOSE", None)
    if quiet:
        os.environ["SSHPILOT_DAEMON_QUIET"] = "1"
    else:
        os.environ.pop("SSHPILOT_DAEMON_QUIET", None)


def _export_language() -> None:
    """Export the interface language, as importing ``sshpilot.main`` does.

    Outside Windows ``i18n`` reads the config directory through
    ``platform_utils`` (GLib; on Linux and Flatpak that is where the directory
    really comes from), so the gi version pins go first: nothing on that chain
    may load GTK unpinned. On Windows it uses the headless resolver and gi is
    not imported here at all.
    """
    try:
        if sys.platform != "win32":
            # Windows reads it without GLib (i18n.get_config_dir): no gi yet.
            from .gi_versions import require_gui_versions

            require_gui_versions()
        from .i18n import apply_language

        apply_language()
    except Exception:
        logger.debug("Could not apply the language preference", exc_info=True)


def release_connect() -> None:
    """Let the worker connect to a running daemon (see the module doc)."""
    _connect_released.set()


def start(*, verbose: bool = False, quiet: bool = False) -> bool:
    """Begin locating/starting the daemon in the background (idempotent)."""
    global _state
    if os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes"}:
        return False
    with _lock:
        if _state is not None:
            return True
        verbose = bool(verbose) and not quiet
        quiet = bool(quiet) and not verbose
        apply_verbosity_environment(verbose, quiet)
        state = _state = _Prelaunch(verbose, quiet)
    # Outside the lock (and, except on Windows, pins gi and imports GLib). Runs
    # on this thread, before the worker exists, so the environment is complete
    # before any spawn and is never written while another thread reads it.
    _export_language()
    state.thread.start()
    return True


def start_from_argv(argv: Sequence[str]) -> bool:
    """:func:`start` with the verbosity from a GUI command line.

    Does nothing for a command line that will not open the GUI (``--help``,
    ``--version`` or an invalid one); ``main()`` reports those itself.
    """
    try:
        from .cli_connect import parse_sshpilot_cli

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            opts = parse_sshpilot_cli(list(argv))
    except SystemExit:
        return False
    except Exception:
        logger.debug("Could not pre-parse the command line", exc_info=True)
        return False
    # The GUI will open: let the GL driver load alongside the imports, too.
    if sys.platform == "win32":
        from .platform.windows import gl_warmup

        gl_warmup.start()
    return start(verbose=opts.verbose or opts.diagnostics, quiet=opts.quiet)


def launcher():
    """The launcher of a finished early start, else ``None`` (never blocks).

    Reusing it keeps one launcher per process for reconnects.
    """
    state = _state
    if state is None or not state.done.is_set():
        return None
    return state.launcher


def ready(launcher=None) -> bool:
    """Whether :func:`take` would hand over a connected client without blocking.

    True only for a finished, successful early start that nobody has taken
    and that belongs to ``launcher`` (when given), so the caller can attach
    the client on the GTK thread before the window's first frame.
    """
    state = _state
    if state is None or not state.done.is_set():
        return False
    with _lock:
        return (
            not state.taken
            and state.error is None
            and state.result is not None
            and (launcher is None or launcher is state.launcher)
        )


def take(launcher=None):
    """Hand over the early result once: a ``DaemonLaunchResult`` or ``None``.

    ``None`` means the caller must run ``connect_or_start`` itself: nothing was
    started early, the result was already taken or discarded, the caller uses
    a different launcher, or a resident daemon needs replacing. An error from
    the early attempt is raised here, as ``connect_or_start`` would have
    raised it. Blocks until the early attempt finishes (call it off the GTK
    thread unless :func:`ready` said otherwise).
    """
    state = _state
    if state is None:
        return None
    state.done.wait()
    with _lock:
        if state.taken:
            return None
        if launcher is not None and launcher is not state.launcher:
            return None
        state.taken = True
        error, state.error = state.error, None
        result, state.result = state.result, None
    if error is not None:
        raise error
    return result


def discard() -> None:
    """Close an early client nobody took; never blocks.

    ``main()`` calls this when the application returns, which is how a
    secondary instance (it handed its command line to a running window)
    releases the connection it opened. An attempt still in flight closes its
    own client when it finishes.
    """
    state = _state
    if state is None:
        return
    with _lock:
        if state.taken:
            return
        state.taken = True
        if not state.done.is_set():
            return
        result, state.result = state.result, None
    _close_result(result)
