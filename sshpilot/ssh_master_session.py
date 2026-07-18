"""PTY-backed SSH ControlMaster session for the file manager.

The SFTP file manager runs ``ssh -s <host> sftp`` over pipes with no PTY, so it
cannot answer interactive prompts (2FA codes, YubiKey PINs, host-key
confirmations, unstored secrets). This module establishes the connection
*first*, as an OpenSSH ControlMaster spawned on a PTY the app owns:

* known prompts with stored secrets are answered by askpass (login password
  and key passphrase) via ``resolve_native_auth()`` env, or by writing to the
  PTY as a backup when the prompt still appears on the master PTY;
* anything else (OTP, PIN, touch, yes/no) is surfaced via callback so the UI
  can reveal the PTY in a terminal dialog for the user to answer natively;
* once ``ssh -O check`` confirms the master socket, the PTY-less SFTP worker
  connects through it instantly with no authentication of its own.

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
from typing import Callable, Dict, List, Optional

from . import ssh_multiplex
from .askpass_utils import _extract_key_path, classify_prompt, lookup_passphrase
from .ssh_connection_builder import (
    ConnectionContext,
    _get_stored_password,
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
    on_needs_interaction: Callable[[int, str], None],
    on_need_password: Callable[[str], None],
    on_failed: Callable[[str], None],
) -> Optional["MasterSession"]:
    """Start a PTY-backed ControlMaster when none is live.

    Returns the new ``MasterSession`` when one was started, or ``None`` when a
    live master already exists (``on_ready`` is invoked immediately). Callers
    (file manager, and any future PTY-less tool) marshal callbacks to the UI
    loop themselves — this helper stays GTK-free.
    """
    if check_master_alive(connection, connection_manager, config):
        on_ready()
        return None
    session = MasterSession(
        connection,
        connection_manager,
        config,
        on_ready=on_ready,
        on_needs_interaction=on_needs_interaction,
        on_need_password=on_need_password,
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
    * ``on_needs_interaction(master_fd, transcript)`` — an unanswerable prompt
      appeared. The raw reader has stopped; the caller owns ``master_fd`` now
      (attach it to a VTE) and should show ``transcript`` so the pending
      prompt is visible.
    * ``on_need_password(prompt_line)`` — a password prompt with no stored (or
      a rejected stored) password. Answer with :meth:`send_secret` or
      :meth:`cancel`.
    * ``on_failed(transcript_tail)`` — ssh exited before the socket went live.
    """

    def __init__(self, connection, connection_manager=None, config=None, *,
                 on_ready: Callable[[], None],
                 on_needs_interaction: Callable[[int, str], None],
                 on_need_password: Callable[[str], None],
                 on_failed: Callable[[str], None]):
        self._connection = connection
        self._connection_manager = connection_manager
        self._config = config
        self._on_ready = on_ready
        self._on_needs_interaction = on_needs_interaction
        self._on_need_password = on_need_password
        self._on_failed = on_failed

        self._proc: Optional[subprocess.Popen] = None
        self._master_fd: Optional[int] = None
        self._transcript = ''
        self._lock = threading.Lock()
        self._finished = threading.Event()   # ready/failed/cancelled: no more callbacks
        self._handed_off = False             # reader stopped; fd belongs to the UI
        self._answered: Dict[str, int] = {}  # prompt key -> auto-answers sent
        # Only transcript text after this offset is classified, so an answered
        # prompt (whose text remains the last line until the server responds)
        # cannot re-trigger, and a genuine re-prompt (which arrives after the
        # mark) still does.
        self._answer_mark = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._preload_agent_keys()
        ctx = _build_master_context(
            self._connection, self._connection_manager, self._config,
            ['-N'] + ssh_multiplex.controlmaster_args())
        prepared = build_ssh_connection(ctx)
        env = {**os.environ, **(prepared.env or {})}
        # Honor the auth resolver's deletions: a plain merge would resurrect a
        # desktop SSH_ASKPASS (ksshaskpass etc.) from os.environ and let it
        # intercept prompts meant for our PTY.
        for key in ('SSH_ASKPASS', 'SSH_ASKPASS_REQUIRE', 'SSH_AUTH_SOCK'):
            if key not in (prepared.env or {}):
                env.pop(key, None)
        self._stored_password = prepared.password or _get_stored_password(
            self._connection, self._connection_manager)

        master_fd, slave_fd = os.openpty()
        try:
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
        except OSError:
            pass
        try:
            # setsid() via start_new_session, then adopt the PTY slave as the
            # controlling terminal so ssh's /dev/tty prompts land on our PTY.
            self._proc = subprocess.Popen(
                list(prepared.command),
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                env=env, start_new_session=True,
                preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
            )
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        logger.debug("MasterSession spawned: %s", ' '.join(prepared.command))

        threading.Thread(target=self._read_loop, daemon=True,
                         name='ssh-master-pty-reader').start()
        threading.Thread(target=self._check_loop, daemon=True,
                         name='ssh-master-check').start()

    def send_secret(self, secret: str) -> None:
        """Write a secret + newline to the PTY (echo is off during secret
        prompts, so nothing is displayed or retained by the terminal)."""
        fd = self._master_fd
        if fd is None or self._finished.is_set():
            return
        try:
            os.write(fd, secret.encode('utf-8', errors='replace') + b'\n')
            with self._lock:
                self._answer_mark = len(self._transcript)
        except OSError:
            logger.debug("MasterSession: PTY write failed", exc_info=True)

    def cancel(self) -> None:
        """Abort the session: kill the foreground ssh and close the PTY. A
        ControlPersist daemon that already detached is left to expire."""
        self._finished.set()
        self._teardown()

    # -- internals ---------------------------------------------------------
    def _preload_agent_keys(self) -> None:
        """Silently load stored-passphrase keys into ssh-agent (existing
        keyring-only behaviour) so most key hosts never prompt at all."""
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
                self._answer_mark = max(0, self._answer_mark - dropped)
                transcript = self._transcript
            if self._handle_prompt(transcript):
                return  # handed off to the UI; stop reading
        # EOF/error: ssh exited. The check loop reports failure (or already
        # reported ready); nothing to do here.

    def _handle_prompt(self, transcript: str) -> bool:
        """React to a trailing prompt. Returns True when the reader must stop
        because the fd was handed to the UI."""
        with self._lock:
            pending = transcript[self._answer_mark:]
        kind = classify_prompt(pending)
        if kind is None or self._finished.is_set():
            return False

        lines = [l.strip() for l in pending.splitlines() if l.strip()]
        prompt_line = lines[-1] if lines else ''

        if kind == 'password':
            if self._stored_password and self._answered.get('password', 0) < 1:
                self._answered['password'] = 1
                logger.debug("MasterSession: auto-answering password prompt")
                self.send_secret(self._stored_password)
                return False
            # No stored password, or the stored one was rejected (re-prompt).
            with self._lock:
                self._answer_mark = len(self._transcript)
            self._on_need_password(prompt_line)
            return False

        if kind == 'passphrase':
            key_path = _extract_key_path(prompt_line)
            answer_key = f'passphrase:{key_path}'
            passphrase = lookup_passphrase(key_path) if key_path else ''
            if passphrase and self._answered.get(answer_key, 0) < 1:
                self._answered[answer_key] = 1
                logger.debug("MasterSession: auto-answering passphrase for %s", key_path)
                self.send_secret(passphrase)
                return False
            # Unstored (or rejected) passphrase -> let the user type it.

        # 'interactive' (or unanswerable passphrase): hand the PTY to the UI.
        self._handed_off = True
        logger.info("MasterSession: interactive prompt, handing PTY to UI: %r",
                    prompt_line)
        self._on_needs_interaction(self._master_fd, transcript)
        return True

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
        if fd is not None and not self._handed_off:
            try:
                os.close(fd)
            except OSError:
                pass
