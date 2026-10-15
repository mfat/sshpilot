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
same as without the early start.

GTK-free; the launcher itself is imported on the worker thread.
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import threading
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

#: Set to ``1`` to skip the early start (the window then starts the daemon).
DISABLE_ENV = "SSHPILOT_NO_DAEMON_PRELAUNCH"

_lock = threading.Lock()
_state: Optional["_Prelaunch"] = None


class _Prelaunch:
    def __init__(self, verbose: bool, quiet: bool) -> None:
        self.verbose = verbose
        self.quiet = quiet
        self.launcher = None
        self.result = None
        self.error: Optional[BaseException] = None
        self.done = threading.Event()
        self.taken = False
        self.thread = threading.Thread(
            target=self._run, name="sshpilot-daemon-prelaunch", daemon=True
        )

    def _run(self) -> None:
        try:
            from .daemon.launcher import DaemonLauncher

            self.launcher = DaemonLauncher(verbose=self.verbose, quiet=self.quiet)
            self.result = self.launcher.connect_or_start_without_eviction()
        except BaseException as error:  # handed to the selection that takes it
            self.error = error
        finally:
            self.done.set()


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


def start(*, verbose: bool = False, quiet: bool = False) -> bool:
    """Begin locating/starting the daemon in the background (idempotent)."""
    global _state
    if os.environ.get(DISABLE_ENV, "").strip() in {"1", "true", "yes"}:
        return False
    with _lock:
        if _state is not None:
            return True
        verbose = bool(verbose) and not quiet
        quiet = bool(quiet) and not verbose
        apply_verbosity_environment(verbose, quiet)
        # A daemon started from here inherits this environment, so export the
        # interface language now, as importing ``sshpilot.main`` would have
        # done before the window-time launch (``_init_gettext``).
        try:
            from .i18n import apply_language

            apply_language()
        except Exception:
            logger.debug("Could not apply the language preference", exc_info=True)
        _state = _Prelaunch(verbose, quiet)
        _state.thread.start()
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
    return start(verbose=opts.verbose or opts.diagnostics, quiet=opts.quiet)


def launcher():
    """The launcher of a finished early start, else ``None`` (never blocks).

    Reusing it keeps one launcher per process for reconnects.
    """
    state = _state
    if state is None or not state.done.is_set():
        return None
    return state.launcher


def take(launcher=None):
    """Hand over the early result once: a ``DaemonLaunchResult`` or ``None``.

    ``None`` means the caller must run ``connect_or_start`` itself: nothing was
    started early, the result was already taken, the caller uses a different
    launcher, or a resident daemon needs replacing. An error from the early
    attempt is raised here, as ``connect_or_start`` would have raised it.
    """
    state = _state
    if state is None:
        return None
    with _lock:
        if state.taken:
            return None
        state.done.wait()
        if launcher is not None and launcher is not state.launcher:
            return None
        state.taken = True
    if state.error is not None:
        error, state.error = state.error, None
        raise error
    result, state.result = state.result, None
    return result


def discard() -> None:
    """Close an early client nobody took (tests, or an aborted startup)."""
    global _state
    with _lock:
        state, _state = _state, None
    if state is None:
        return
    state.done.wait()
    if not state.taken and state.result is not None:
        try:
            state.result.client.close()
        except Exception:
            pass
