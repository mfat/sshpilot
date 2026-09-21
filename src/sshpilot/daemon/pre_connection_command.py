"""The connection's pre-connection command, run by the daemon before a launch.

A connection may carry a local shell string that has to complete before SSH
dials out -- almost always a port knock (``fwknop``/``knock``) or a VPN dial-up
that authorises a short access window.

This used to live in the GTK frontend and run for terminal tabs only
(``terminal.run_pre_connection_command`` plus
``TerminalManager._maybe_run_pre_command_then``). That was a bug, not a layering
choice: of the six kinds in :data:`sshpilot.daemon.ssh_launch._POLICIES`, only
``TERMINAL`` ever ran it, so on a knock-gated host SFTP browsing, port
forwards, SCP transfers, ``ssh-copy-id`` and Host Info probes all dialled a
closed port. The fix only exists where the six kinds converge, which is the
launcher.

Three properties this module owns that a frontend structurally cannot:

*Serialization.* A knock is a stateful, rate-limited sequence. Opening three
tabs at once used to fire three knocks concurrently and interleaved, which some
``knockd`` configurations score as a *failed* sequence.

*Coalescing.* Within a short window a recent run is reused rather than
repeated, which is what keeps a multi-tab burst -- or daemon session restore
reopening a whole set -- down to one knock.

*A voice.* A failing pre-command used to produce a frontend log line and
nothing else. Every outcome here is published as a
:class:`~sshpilot.api.models.pre_command.PreConnectionCommandNotice` so the user
is told what happened.

By default this module does **not** fail a launch: SSH's own error tells the
user far more than a pre-step veto, which is the behaviour this feature shipped
with. A connection can opt into the opposite, and
:meth:`PreConnectionCommandRunner.run` then answers ``False`` so the launcher
refuses. It never raises either way -- a fault in here must not be able to lock
someone out of their own host.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Any, Callable, Dict, Optional

from sshpilot.api.events import EventPublisher, EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandSettings,
    PreCommandTestResult,
    PreCommandPhase,
    PreCommandReason,
    PreConnectionCommandNotice,
)
from sshpilot.logging_support import log_context

from .bootstrap_settings import (
    DEFAULT_PRE_COMMAND_COALESCE_SECONDS,
    DEFAULT_PRE_COMMAND_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

#: Cap on how much captured stderr is written to the debug log. Output is
#: content, so it is never logged above DEBUG and never crosses the wire.
_STDERR_LOG_LIMIT = 800


class PreConnectionCommandRunner:
    """Run, serialize, coalesce and narrate one connection's pre-command.

    One instance per daemon. :class:`~sshpilot.daemon.ssh_launch.SshLauncher`
    is constructed per call, so the runner cannot live there: the coalescing
    state has to outlive a single launch to be worth anything.
    """

    def __init__(
        self,
        *,
        settings: Any,
        lookup: Callable[[ConnectionId], PreCommandSettings],
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._lookup = lookup
        self._runner = runner
        self._clock = clock
        self._publisher = EventPublisher()
        self._state_lock = threading.Lock()
        self._connection_locks: Dict[str, threading.Lock] = {}
        self._last_run: Dict[str, float] = {}

    # -- events --------------------------------------------------------------

    def subscribe_events(self, callback) -> Any:
        return self._publisher.subscribe(callback)

    def close(self) -> None:
        self._publisher.close()

    # -- execution -----------------------------------------------------------

    def run(
        self,
        connection_id: Optional[ConnectionId],
        *,
        scope_id: str,
        kind: PreCommandLaunchKind,
    ) -> bool:
        """Run the connection's pre-command, if it has one. Never raises.

        Returns whether the launch may proceed. That is ``True`` unless the
        connection asked for a hard gate and the command did not succeed --
        an internal fault here always answers ``True``, because a bug in this
        module must not be able to lock someone out of their own host.

        Blocking by design: the caller is a launch preparation already running
        on a :class:`~sshpilot.daemon.command_executor.BoundedCommandExecutor`
        worker, and the whole point is that the knock lands before OpenSSH
        dials. The daemon's dispatch thread is not involved.
        """

        if not connection_id or not scope_id:
            return True
        with log_context(connection=connection_id, session=scope_id):
            try:
                return self._run_guarded(connection_id, scope_id=scope_id, kind=kind)
            except Exception:
                # A connection must never be stranded by an optional pre-step.
                # The helper below already handles every expected failure, so
                # reaching here means a bug in this module -- log it with a
                # traceback and let the launch continue regardless.
                logger.exception("pre-connection command handling failed")
                return True

    def _run_guarded(
        self,
        connection_id: ConnectionId,
        *,
        scope_id: str,
        kind: PreCommandLaunchKind,
    ) -> bool:
        settings = self._read_settings(connection_id)
        command = settings.command
        if not command:
            logger.debug("no pre-connection command configured kind=%s", kind.value)
            return True

        # A per-connection timeout wins; 0 means "follow the app default", so
        # a connection that never set one moves with the preference.
        timeout = settings.timeout or self._timeout_seconds()
        coalesce = self._coalesce_seconds()
        lock = self._lock_for(connection_id)

        # Take the lock *before* the window check: a burst of tabs then
        # produces exactly one execution plus N cheap skips, rather than N
        # racing threads all deciding the window was empty.
        waited_ms = 0
        if not lock.acquire(blocking=False):
            logger.debug("pre-connection command waiting for connection lock")
            started_wait = self._clock()
            lock.acquire()
            waited_ms = _elapsed_ms(started_wait, self._clock())
        try:
            if waited_ms:
                logger.info("pre-connection command lock wait ms=%d", waited_ms)
            age_ms = self._coalesce_age_ms(connection_id, coalesce)
            if age_ms is not None:
                logger.info(
                    "pre-connection command reused age_ms=%d window_s=%s kind=%s",
                    age_ms,
                    coalesce,
                    kind.value,
                )
                self._publish(
                    connection_id,
                    scope_id=scope_id,
                    kind=kind,
                    phase=PreCommandPhase.FINISHED,
                    reason=PreCommandReason.COALESCED,
                )
                return True

            logger.info(
                "pre-connection command starting kind=%s timeout_s=%s coalesce_s=%s",
                kind.value,
                timeout,
                coalesce,
            )
            logger.debug("pre-connection command text: %s", command)
            self._publish(
                connection_id,
                scope_id=scope_id,
                kind=kind,
                phase=PreCommandPhase.RUNNING,
            )
            reason, exit_code, duration_ms = self._execute(command, timeout, kind)
            succeeded = reason is PreCommandReason.OK
            if succeeded:
                self._last_run[connection_id] = self._clock()
            aborted = not succeeded and settings.abort_on_failure
            if aborted:
                logger.warning(
                    "pre-connection command refused the launch kind=%s reason=%s",
                    kind.value,
                    reason.value,
                )
            self._publish(
                connection_id,
                scope_id=scope_id,
                kind=kind,
                phase=PreCommandPhase.FINISHED,
                reason=reason,
                exit_code=exit_code,
                duration_ms=duration_ms,
                aborted=aborted,
            )
            return not aborted
        finally:
            lock.release()

    def _execute(
        self,
        command: str,
        timeout: int,
        kind: PreCommandLaunchKind,
    ) -> tuple:
        """Run *command* and classify the outcome. Returns (reason, exit, ms)."""

        # ``sh -lc`` rather than a bare ``sh -c``: the string is user-authored
        # and legitimately uses substitutions such as
        # ``fwknop -n host --wget-cmd "$(which wget)"``, and a *login* shell
        # also sources the user's profile. That matters in the packaged app --
        # a bundle launched from Finder inherits a minimal PATH that does not
        # include Homebrew, so a non-login shell would not find the knock
        # helper at all. The daemon inherits the same minimal environment, so
        # the reason holds here exactly as it did in the frontend.
        shell = shutil.which("sh") or "/bin/sh"
        started = self._clock()
        try:
            result = self._runner(
                [shell, "-lc", command],
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            duration_ms = _elapsed_ms(started, self._clock())
            logger.warning(
                "pre-connection command timed out after_s=%s kind=%s; "
                "descendants of the shell may still be running",
                timeout,
                kind.value,
            )
            logger.debug("pre-connection command timed out: %s", command)
            return PreCommandReason.TIMED_OUT, None, duration_ms
        except Exception as exc:
            duration_ms = _elapsed_ms(started, self._clock())
            # The exception *text* is content -- it can quote the command line
            # -- so only its type is logged above DEBUG.
            logger.warning(
                "pre-connection command could not start type=%s kind=%s",
                type(exc).__name__,
                kind.value,
            )
            logger.debug("pre-connection command start failure", exc_info=True)
            return PreCommandReason.START_FAILED, None, duration_ms

        duration_ms = _elapsed_ms(started, self._clock())
        exit_code = getattr(result, "returncode", None)
        if type(exit_code) is not int:
            exit_code = None
        stdout_bytes = _text_length(getattr(result, "stdout", ""))
        stderr_text = getattr(result, "stderr", "") or ""
        if exit_code == 0:
            logger.info(
                "pre-connection command finished exit=0 duration_ms=%d "
                "stdout_bytes=%d stderr_bytes=%d kind=%s",
                duration_ms,
                stdout_bytes,
                _text_length(stderr_text),
                kind.value,
            )
            return PreCommandReason.OK, exit_code, duration_ms

        logger.warning(
            "pre-connection command failed exit=%s duration_ms=%d kind=%s",
            exit_code,
            duration_ms,
            kind.value,
        )
        trimmed = stderr_text.strip()
        if trimmed:
            # Output is content: DEBUG only, truncated, and never published.
            logger.debug(
                "pre-connection command stderr: %.*s",
                _STDERR_LOG_LIMIT,
                trimmed,
            )
        return PreCommandReason.NONZERO_EXIT, exit_code, duration_ms

    # -- trying it out ---------------------------------------------------------

    def test(self, command: str, timeout: int = 0) -> PreCommandTestResult:
        """Run *command* once and report what happened, for the editor's Test.

        Deliberately outside the serialize/coalesce machinery: the user asked
        for this exact command to run now, and reusing a recent result or
        queueing behind a launch would answer a different question than the
        one they asked. It also publishes no notice -- nothing is connecting,
        so no surface should show a connection doing anything.
        """

        command = command.strip() if isinstance(command, str) else ""
        if not command:
            return PreCommandTestResult(reason=PreCommandReason.OK)
        timeout = timeout if type(timeout) is int and timeout > 0 else (
            self._timeout_seconds()
        )
        logger.info("pre-connection command test starting timeout_s=%s", timeout)
        logger.debug("pre-connection command test text: %s", command)
        reason, exit_code, duration_ms, output = self._execute_captured(
            command, timeout
        )
        logger.info(
            "pre-connection command test finished reason=%s exit=%s duration_ms=%d",
            reason.value,
            exit_code,
            duration_ms,
        )
        limit = PreCommandTestResult.MAX_OUTPUT_CHARS
        return PreCommandTestResult(
            reason=reason,
            exit_code=exit_code,
            duration_ms=duration_ms,
            output=output[:limit],
        )

    def _execute_captured(self, command: str, timeout: int) -> tuple:
        """Like :meth:`_execute`, but keeps the output for the caller."""

        shell = shutil.which("sh") or "/bin/sh"
        started = self._clock()
        try:
            result = self._runner(
                [shell, "-lc", command],
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return (
                PreCommandReason.TIMED_OUT,
                None,
                _elapsed_ms(started, self._clock()),
                "",
            )
        except Exception as exc:
            return (
                PreCommandReason.START_FAILED,
                None,
                _elapsed_ms(started, self._clock()),
                type(exc).__name__,
            )
        duration_ms = _elapsed_ms(started, self._clock())
        exit_code = getattr(result, "returncode", None)
        if type(exit_code) is not int:
            exit_code = None
        merged = "".join(
            part for part in (
                getattr(result, "stdout", "") or "",
                getattr(result, "stderr", "") or "",
            )
        ).strip()
        reason = (
            PreCommandReason.OK if exit_code == 0 else PreCommandReason.NONZERO_EXIT
        )
        return reason, exit_code, duration_ms, merged

    # -- helpers -------------------------------------------------------------

    def _read_settings(self, connection_id: ConnectionId) -> PreCommandSettings:
        try:
            value = self._lookup(connection_id)
        except Exception as exc:
            # A lookup that fails must not strand an otherwise valid launch.
            logger.warning(
                "pre-connection command lookup failed type=%s",
                type(exc).__name__,
            )
            return PreCommandSettings()
        return value if type(value) is PreCommandSettings else PreCommandSettings()

    def _lock_for(self, connection_id: ConnectionId) -> threading.Lock:
        with self._state_lock:
            lock = self._connection_locks.get(connection_id)
            if lock is None:
                lock = threading.Lock()
                self._connection_locks[connection_id] = lock
            return lock

    def _coalesce_age_ms(
        self, connection_id: ConnectionId, coalesce: int
    ) -> Optional[int]:
        """Milliseconds since the last successful run, when inside the window."""

        if coalesce <= 0:
            return None
        previous = self._last_run.get(connection_id)
        if previous is None:
            return None
        age = self._clock() - previous
        if age < 0 or age >= coalesce:
            return None
        return int(age * 1000)

    def _timeout_seconds(self) -> int:
        value = getattr(
            self._settings,
            "pre_command_timeout_seconds",
            DEFAULT_PRE_COMMAND_TIMEOUT_SECONDS,
        )
        return value if type(value) is int and value > 0 else (
            DEFAULT_PRE_COMMAND_TIMEOUT_SECONDS
        )

    def _coalesce_seconds(self) -> int:
        value = getattr(
            self._settings,
            "pre_command_coalesce_seconds",
            DEFAULT_PRE_COMMAND_COALESCE_SECONDS,
        )
        return value if type(value) is int and value >= 0 else (
            DEFAULT_PRE_COMMAND_COALESCE_SECONDS
        )

    def _publish(
        self,
        connection_id: ConnectionId,
        *,
        scope_id: str,
        kind: PreCommandLaunchKind,
        phase: PreCommandPhase,
        reason: PreCommandReason = PreCommandReason.OK,
        exit_code: Optional[int] = None,
        duration_ms: int = 0,
        aborted: bool = False,
    ) -> None:
        notice = PreConnectionCommandNotice(
            connection_id=connection_id,
            scope_id=scope_id,
            kind=kind,
            phase=phase,
            reason=reason,
            exit_code=exit_code,
            duration_ms=duration_ms,
            aborted=aborted,
        )
        try:
            self._publisher.publish(
                EventType.PRE_CONNECTION_COMMAND,
                notice,
                connection_id=connection_id,
                session_id=scope_id,
            )
        except RuntimeError:
            # The publisher closes during shutdown; a launch in flight then
            # simply loses its narration rather than its connection.
            logger.debug("pre-connection command notice was not published")


def _elapsed_ms(started: float, now: float) -> int:
    elapsed = now - started
    return int(elapsed * 1000) if elapsed > 0 else 0


def _text_length(value: Any) -> int:
    """Byte count of captured output -- never the output itself."""

    if isinstance(value, str):
        return len(value.encode("utf-8", "replace"))
    if isinstance(value, (bytes, bytearray)):
        return len(value)
    return 0


__all__ = ["PreConnectionCommandRunner"]
