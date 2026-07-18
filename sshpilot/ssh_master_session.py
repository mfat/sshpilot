"""PTY-backed SSH ControlMaster session for the file manager.

The SFTP file manager runs ``ssh -s <host> sftp`` over pipes with no PTY, so it
cannot answer interactive prompts itself. This module establishes the connection
*first*, as an OpenSSH ControlMaster spawned on an app-owned PTY:

* **All auth** (login password, key passphrase, OTP/MFA) is handled by askpass
  via ``resolve_native_auth()`` env with ``SSH_ASKPASS_REQUIRE=prefer``. There is
  no PTY-side prompt scraping or AuthTerminalDialog handoff.
* The PTY exists so OpenSSH can run ``ssh -N`` / ControlMaster; output is only
  retained for failure diagnostics.
* Once ``ssh -O check`` confirms the master socket, the PTY-less SFTP worker
  connects through it with no authentication of its own.

GTK-free: callbacks fire on internal threads; callers marshal to the UI loop.
"""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import struct
import subprocess
import termios
import threading
from typing import Callable, List, Optional

from . import ssh_multiplex
from .askpass_utils import classify_prompt, lookup_passphrase
from .ssh_connection_builder import (
    ConnectionContext,
    _askpass_env_for_connection,
    build_ssh_connection,
)

# Re-export for callers that historically imported classify_prompt from here.
__all__ = [
    'MasterSession',
    'check_master_alive',
    'classify_prompt',
    'ensure_authenticated_master',
    'invalidate_master',
]

logger = logging.getLogger(__name__)

_TRANSCRIPT_MAX = 16384
_CHECK_INTERVAL = 1.0
_CHECK_TIMEOUT = 10


def _build_master_context(connection, connection_manager, config,
                          extra_args: List[str]) -> ConnectionContext:
    return ConnectionContext(
        connection=connection,
        connection_manager=connection_manager,
        config=config,
        command_type='ssh',
        native_mode=True,
        extra_args=extra_args,
    )


def ensure_authenticated_master(
    connection,
    connection_manager=None,
    config=None,
    *,
    on_ready: Callable[[], None],
    on_failed: Callable[[str], None],
) -> Optional["MasterSession"]:
    """Start a PTY-backed ControlMaster when none is live.

    Auth is entirely via askpass (see ``MasterSession``). Returns the new
    ``MasterSession`` when one was started, or ``None`` when a live master
    already exists (``on_ready`` is invoked immediately). Callers marshal
    callbacks to the UI loop themselves — this helper stays GTK-free.
    """
    if check_master_alive(connection, connection_manager, config):
        on_ready()
        return None
    session = MasterSession(
        connection,
        connection_manager,
        config,
        on_ready=on_ready,
        on_failed=on_failed,
    )
    session.start()
    return session


def check_master_alive(connection, connection_manager=None, config=None) -> bool:
    """Return True when a live ControlMaster socket exists for *connection*.

    Runs ``ssh -O check`` through the single command/auth builder so isolated
    mode resolves the same ``-F`` config (and therefore the same ``%C`` socket)
    as the master and the worker.
    """
    try:
        ctx = _build_master_context(
            connection, connection_manager, config,
            ['-O', 'check', '-o', f'ControlPath={ssh_multiplex.control_path()}'])
        prepared = build_ssh_connection(ctx)
        env = {**os.environ, **(prepared.env or {})}
        result = subprocess.run(
            list(prepared.command), env=env, capture_output=True,
            timeout=_CHECK_TIMEOUT, check=False)
        return result.returncode == 0
    except Exception:
        return False


def invalidate_master(connection, connection_manager=None, config=None, *,
                      background: bool = True) -> None:
    """Gracefully retire *connection*'s ControlMaster after its SSH config
    changed, so the next connect negotiates a fresh master with the new
    settings instead of silently riding the old transport.

    Uses ``ssh -O stop`` (not ``exit``): the master stops accepting new mux
    clients and removes its socket, but live sessions keep running until they
    end on their own. Best-effort; a dead master simply has no socket to stop.
    """
    def _stop() -> None:
        try:
            ctx = _build_master_context(
                connection, connection_manager, config,
                ['-O', 'stop', '-o',
                 f'ControlPath={ssh_multiplex.control_path()}'])
            prepared = build_ssh_connection(ctx)
            env = {**os.environ, **(prepared.env or {})}
            subprocess.run(list(prepared.command), env=env, capture_output=True,
                           timeout=_CHECK_TIMEOUT, check=False)
        except Exception:
            logger.debug("invalidate_master failed (best-effort)", exc_info=True)

    if background:
        threading.Thread(target=_stop, daemon=True,
                         name='ssh-master-invalidate').start()
    else:
        _stop()


class MasterSession:
    """One PTY-backed ``ssh -N`` master establishing a shared connection.

    Callbacks (all invoked on internal threads; the caller marshals to GTK):

    * ``on_ready()`` — ``ssh -O check`` passed; the socket is live. The
      foreground ssh has been terminated (ControlPersist keeps the daemonized
      master serving the socket).
    * ``on_failed(transcript_tail)`` — ssh exited before the socket went live.
    """

    def __init__(self, connection, connection_manager=None, config=None, *,
                 on_ready: Callable[[], None],
                 on_failed: Callable[[str], None]):
        self._connection = connection
        self._connection_manager = connection_manager
        self._config = config
        self._on_ready = on_ready
        self._on_failed = on_failed

        self._proc: Optional[subprocess.Popen] = None
        self._master_fd: Optional[int] = None
        self._transcript = ''
        self._lock = threading.Lock()
        self._finished = threading.Event()   # ready/failed/cancelled: no more callbacks

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._preload_agent_keys()
        ctx = _build_master_context(
            self._connection, self._connection_manager, self._config,
            ['-N'] + ssh_multiplex.controlmaster_args())
        prepared = build_ssh_connection(ctx)
        env = {**os.environ, **(prepared.env or {})}
        # Honor the auth resolver's deletions: a plain merge would resurrect a
        # desktop SSH_ASKPASS (ksshaskpass etc.) from os.environ.
        for key in ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'SSH_AUTH_SOCK'):
            if key not in (prepared.env or {}):
                env.pop(key, None)

        # FM master always uses askpass (prefer). Auth UI is askpass dialogs —
        # never PTY-side classify/handoff. When resolve_native_auth left askpass
        # off (nothing saved / setting off), still wire it so unstored password
        # and OTP prompts can be collected graphically.
        if not env.get('SSH_ASKPASS'):
            ask_env = _askpass_env_for_connection(
                self._connection,
                session_password=getattr(prepared, 'password', None),
            )
            env.update(ask_env)
            logger.debug(
                "MasterSession: forced askpass prefer (resolver had no SSH_ASKPASS)"
            )
        elif env.get('SSH_ASKPASS_REQUIRE') != 'prefer':
            env['SSH_ASKPASS_REQUIRE'] = 'prefer'

        master_fd, slave_fd = os.openpty()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        except OSError:
            pass
        try:
            # setsid() via start_new_session, then adopt the PTY slave as the
            # controlling terminal (OpenSSH still allocates a tty for -N).
            self._proc = subprocess.Popen(
                list(prepared.command),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=env, start_new_session=True,
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        logger.debug(
            "MasterSession spawned (askpass auth): %s",
            ' '.join(prepared.command),
        )

        threading.Thread(target=self._read_loop, daemon=True,
                         name='ssh-master-pty-reader').start()
        threading.Thread(target=self._check_loop, daemon=True,
                         name='ssh-master-check').start()

    def cancel(self) -> None:
        """Abort the session: kill the foreground ssh and close the PTY. A
        ControlPersist daemon that already detached is left to expire."""
        self._finished.set()
        self._teardown()

    # -- internals ---------------------------------------------------------
    def _preload_agent_keys(self) -> None:
        """Silently load stored-passphrase keys into ssh-agent so most key hosts
        never prompt at all."""
        manager = self._connection_manager
        if manager is None or not hasattr(manager, 'prepare_key_for_connection'):
            return
        candidates = getattr(self._connection, 'resolved_identity_files', None)
        if not candidates and hasattr(self._connection, 'collect_identity_file_candidates'):
            try:
                candidates = self._connection.collect_identity_file_candidates()
            except Exception:
                candidates = None
        for path in list(candidates or []):
            try:
                if lookup_passphrase(path):
                    manager.prepare_key_for_connection(path)
            except Exception:
                logger.debug("MasterSession: agent preload failed for %s",
                             path, exc_info=True)

    def _read_loop(self) -> None:
        """Drain PTY output into a transcript for failure diagnostics only."""
        fd = self._master_fd
        while not self._finished.is_set():
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode('utf-8', errors='replace')
            with self._lock:
                combined = self._transcript + text
                dropped = max(0, len(combined) - _TRANSCRIPT_MAX)
                self._transcript = combined[dropped:]

    def _check_loop(self) -> None:
        while not self._finished.is_set():
            if check_master_alive(self._connection, self._connection_manager,
                                  self._config):
                if self._finished.is_set():
                    return
                self._finished.set()
                logger.info("MasterSession: master socket live")
                # ControlPersist already daemonized the real master; the
                # foreground ssh -N is just a client now.
                self._teardown()
                self._on_ready()
                return
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                if self._finished.is_set():
                    return
                self._finished.set()
                with self._lock:
                    tail = self._transcript[-2000:]
                logger.warning("MasterSession: ssh exited rc=%s before socket "
                               "went live", proc.returncode)
                self._teardown()
                self._on_failed(tail)
                return
            self._finished.wait(_CHECK_INTERVAL)

    def _teardown(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass
        fd = self._master_fd
        self._master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
