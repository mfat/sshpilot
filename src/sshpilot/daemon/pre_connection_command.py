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
import os
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional

from sshpilot.api.events import EventPublisher, EventType
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.pre_command import (
    PreCommandLaunchKind,
    PreCommandSettings,
    PreCommandStage,
    PreCommandTestResult,
    PreCommandPhase,
    PreCommandReason,
    PreConnectionCommandNotice,
    parse_knock_sequence,
)
from sshpilot.logging_support import log_context

from .port_knock import KnockResolutionError, knock as send_knock
from .bootstrap_settings import (
    DEFAULT_PRE_COMMAND_COALESCE_SECONDS,
    DEFAULT_PRE_COMMAND_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

#: Cap on how much captured output is written to the debug log. Output is
#: content, so it is never logged above DEBUG.
_STDERR_LOG_LIMIT = 800

#: How much of each stream is ever read into memory. The notice and the
#: Test result are both bounded at this, so reading more would only be
#: thrown away -- and a looping command can write without limit.
_CAPTURE_READ_LIMIT = PreConnectionCommandNotice.MAX_OUTPUT_CHARS


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
        steps = settings.knock_steps
        if not command and not steps:
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

            if steps:
                proceed = self._knock_stage(
                    settings,
                    steps,
                    connection_id=connection_id,
                    scope_id=scope_id,
                    kind=kind,
                    has_command=bool(command),
                )
                if proceed is not None:
                    # The knock either failed, or was the whole step. Either
                    # way it has published its own finished notice.
                    if proceed:
                        self._last_run[connection_id] = self._clock()
                    return proceed

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
            reason, exit_code, duration_ms, output = self._execute(
                command, timeout, kind
            )
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
                output="" if succeeded else output,
            )
            return not aborted
        finally:
            lock.release()

    # -- knocking --------------------------------------------------------------

    def _knock_stage(
        self,
        settings: PreCommandSettings,
        steps,
        *,
        connection_id: ConnectionId,
        scope_id: str,
        kind: PreCommandLaunchKind,
        has_command: bool,
    ) -> Optional[bool]:
        """Send the knock sequence. Returns ``None`` to carry on to the command.

        A return value means the step is over and a finished notice has been
        published: ``True`` when the knock was the whole step and worked,
        ``False`` when it failed and the connection asked to be gated on it.
        Returning ``None`` is the "knock succeeded, now run the command" path,
        which keeps exactly one finished notice per run and so leaves the
        frontend's state machine as simple as it was -- show on running, clear
        on finished.
        """
        logger.info(
            "port knock starting kind=%s ports=%d",
            kind.value,
            len(steps),
        )
        # The sequence is the secret here, in the same way a door code is:
        # anyone who reads it can open the firewall. Ports are content, so
        # DEBUG only, exactly as the command text is.
        logger.debug(
            "port knock sequence: %s",
            " ".join(str(step) for step in steps),
        )
        self._publish(
            connection_id,
            scope_id=scope_id,
            kind=kind,
            phase=PreCommandPhase.RUNNING,
            stage=PreCommandStage.KNOCK,
        )
        started = self._clock()
        try:
            outcome = send_knock(settings.hostname, steps)
        except KnockResolutionError as error:
            duration_ms = _elapsed_ms(started, self._clock())
            logger.warning("port knock could not resolve the host kind=%s", kind.value)
            logger.debug("port knock resolution failure: %s", error)
            return self._knock_failed(
                connection_id,
                scope_id=scope_id,
                kind=kind,
                settings=settings,
                duration_ms=duration_ms,
                detail=str(error),
            )
        except Exception:
            # Never strand a launch on the optional half of an optional step.
            duration_ms = _elapsed_ms(started, self._clock())
            logger.exception("port knock failed unexpectedly")
            return self._knock_failed(
                connection_id,
                scope_id=scope_id,
                kind=kind,
                settings=settings,
                duration_ms=duration_ms,
                detail="",
            )

        if not outcome.succeeded:
            logger.warning(
                "port knock incomplete sent=%d failed=%d duration_ms=%d kind=%s",
                outcome.sent,
                len(outcome.failed),
                outcome.duration_ms,
                kind.value,
            )
            # Which host and which reason -- content, so DEBUG only, but it is
            # the line that actually identifies the fault.
            logger.debug("port knock: %s", outcome.log_summary)
            return self._knock_failed(
                connection_id,
                scope_id=scope_id,
                kind=kind,
                settings=settings,
                duration_ms=outcome.duration_ms,
                detail=outcome.failure_reason,
            )

        logger.info(
            "port knock finished ports=%d duration_ms=%d kind=%s",
            outcome.sent,
            outcome.duration_ms,
            kind.value,
        )
        # The address is content -- it is the user's own host -- so it goes no
        # higher than DEBUG, alongside the ports.
        logger.debug("port knock sent to %s", outcome.address)
        if has_command:
            return None
        self._publish(
            connection_id,
            scope_id=scope_id,
            kind=kind,
            phase=PreCommandPhase.FINISHED,
            stage=PreCommandStage.KNOCK,
            duration_ms=outcome.duration_ms,
        )
        return True

    def _knock_failed(
        self,
        connection_id: ConnectionId,
        *,
        scope_id: str,
        kind: PreCommandLaunchKind,
        settings: PreCommandSettings,
        duration_ms: int,
        detail: str,
    ) -> bool:
        """Publish the failure and answer whether the launch may still go on.

        A knock that could not be sent is the same class of fault as a command
        that could not be started -- nothing ran -- so it reuses that reason
        rather than adding one the frontend would have to learn.
        """
        aborted = settings.abort_on_failure
        if aborted:
            logger.warning("port knock refused the launch kind=%s", kind.value)
        self._publish(
            connection_id,
            scope_id=scope_id,
            kind=kind,
            phase=PreCommandPhase.FINISHED,
            reason=PreCommandReason.START_FAILED,
            stage=PreCommandStage.KNOCK,
            duration_ms=duration_ms,
            aborted=aborted,
            output=detail,
        )
        return not aborted

    @contextmanager
    def _capture_files(self):
        """Temporary files to capture the command's output into.

        Files, not pipes, and that choice is load-bearing. Capturing through a
        pipe means waiting for the pipe to reach end-of-file, and a command the
        user deliberately backgrounded -- ``openvpn --config x.ovpn &`` -- hands
        that same pipe to a process that holds it open for its whole life. The
        shell exits immediately, but the read does not finish, so the step
        blocked for the full timeout and then reported a timeout for a command
        that had in fact started fine. With the hard gate on, that turned a
        working VPN into a host that could never be connected to.

        A shell does not capture output, which is why ``&`` behaves as expected
        in a terminal; this restores that. Writing to a file means the wait is
        for the shell to exit, which is the thing we actually asked for.

        The files are unlinked on creation, so the output never appears in the
        filesystem for another user to read -- it can quote whatever the
        command printed, including a token.

        One residual: a backgrounded grandchild keeps its inherited handle and
        goes on writing into a file nothing will read, holding that space until
        it exits. Bounded by how long and how loudly it runs, and strictly
        better than blocking every connection for the timeout.
        """
        with tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as out, tempfile.TemporaryFile(
            mode="w+", encoding="utf-8", errors="replace"
        ) as err:
            yield out, err

    @staticmethod
    def _read_capture(handle, limit: int = _CAPTURE_READ_LIMIT) -> str:
        """A bounded prefix of what the command wrote.

        Bounded at the source rather than after the fact: a misconfigured
        command can loop printing until its timeout, and reading the whole
        file to then keep 4000 characters of it would size our memory by how
        badly the user's command misbehaves.
        """
        try:
            handle.seek(0)
            return handle.read(limit)
        except Exception:
            logger.debug("pre-connection command output was unreadable", exc_info=True)
            return ""

    @staticmethod
    def _capture_bytes(handle) -> int:
        """How much the command actually wrote, without reading any of it.

        ``fstat`` on the handle answers for the whole file even though the
        read above stops early, so the logged counts stay true to what
        happened rather than to what we chose to look at.
        """
        try:
            return os.fstat(handle.fileno()).st_size
        except Exception:
            logger.debug("pre-connection command output size unknown", exc_info=True)
            return 0

    @staticmethod
    def _shell_argv(command: str) -> list:
        """The argv that runs *command* where the user's tools actually are.

        Under Flatpak the daemon lives in the sandbox, and a port knock is the
        one thing that is certainly *not* in there: ``knock``, ``fwknop`` and
        ``openvpn`` are host tools, and this app bundles none of them. Running
        the command in the sandbox therefore fails with "command not found",
        the knock never happens, and -- with the hard gate on -- the host
        becomes unreachable for a reason that looks like the user's mistake.

        ``flatpak-spawn --host`` is how Remmina solves the same problem, and
        the permission it needs (``--talk-name=org.freedesktop.Flatpak``) is
        already in our manifest. The shell is left unqualified in that case so
        the *host* resolves it, which is also what makes ``-lc`` source the
        host's profile rather than the sandbox's.
        """
        if os.path.exists("/.flatpak-info"):
            spawner = shutil.which("flatpak-spawn")
            if spawner:
                return [spawner, "--host", "sh", "-lc", command]
            # Sandboxed with no way out. Running it here will almost certainly
            # fail, but failing loudly beats not running it at all: the
            # outcome is reported either way.
            logger.warning(
                "flatpak-spawn is unavailable; the pre-connection command "
                "will run inside the sandbox"
            )
        # ``sh -lc`` rather than a bare ``sh -c``: the string is user-authored
        # and legitimately uses substitutions such as
        # ``fwknop -n host --wget-cmd "$(which wget)"``, and a *login* shell
        # also sources the user's profile. That matters in the packaged app --
        # a bundle launched from Finder inherits a minimal PATH that does not
        # include Homebrew, so a non-login shell would not find the knock
        # helper at all.
        return [shutil.which("sh") or "/bin/sh", "-lc", command]

    def _execute(
        self,
        command: str,
        timeout: int,
        kind: PreCommandLaunchKind,
    ) -> tuple:
        """Run *command*. Returns (reason, exit, ms, output).

        ``output`` is non-empty only on failure: a shell shows you a failing
        knock's own words and stays quiet on success, and the terminal tab
        mirrors that.
        """

        argv = self._shell_argv(command)
        started = self._clock()
        with self._capture_files() as (out_file, err_file):
            try:
                result = self._runner(
                    argv,
                    timeout=timeout,
                    stdout=out_file,
                    stderr=err_file,
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
                # Whatever it managed to say before it was killed is often
                # the only clue why it hung. Both streams: knock helpers are
                # not consistent about which one they complain on, and a
                # stdout-only complaint used to vanish here while showing up
                # under Test, which reads as the two disagreeing.
                partial = self._merged_capture(out_file, err_file)
                if partial:
                    logger.debug(
                        "pre-connection command output before the timeout: %.*s",
                        _STDERR_LOG_LIMIT,
                        partial,
                    )
                return (
                    PreCommandReason.TIMED_OUT,
                    None,
                    duration_ms,
                    partial,
                )
            except Exception as exc:
                duration_ms = _elapsed_ms(started, self._clock())
                # The exception *text* is content -- it can quote the command
                # line -- so only its type is logged above DEBUG.
                logger.warning(
                    "pre-connection command could not start type=%s kind=%s",
                    type(exc).__name__,
                    kind.value,
                )
                logger.debug("pre-connection command start failure", exc_info=True)
                return (
                    PreCommandReason.START_FAILED,
                    None,
                    duration_ms,
                    type(exc).__name__,
                )

            duration_ms = _elapsed_ms(started, self._clock())
            exit_code = getattr(result, "returncode", None)
            if type(exit_code) is not int:
                exit_code = None
            stdout_bytes = self._capture_bytes(out_file)
            stderr_bytes = self._capture_bytes(err_file)
            merged = self._merged_capture(out_file, err_file)
        if exit_code == 0:
            logger.info(
                "pre-connection command finished exit=0 duration_ms=%d "
                "stdout_bytes=%d stderr_bytes=%d kind=%s",
                duration_ms,
                stdout_bytes,
                stderr_bytes,
                kind.value,
            )
            return PreCommandReason.OK, exit_code, duration_ms, ""

        logger.warning(
            "pre-connection command failed exit=%s duration_ms=%d kind=%s",
            exit_code,
            duration_ms,
            kind.value,
        )
        if merged:
            # Content, so DEBUG only -- but the same text the terminal tab and
            # the Test result show, rather than a narrower slice of it.
            logger.debug(
                "pre-connection command output: %.*s",
                _STDERR_LOG_LIMIT,
                merged,
            )
        return PreCommandReason.NONZERO_EXIT, exit_code, duration_ms, merged

    # -- trying it out ---------------------------------------------------------

    def test(
        self,
        command: str,
        timeout: int = 0,
        *,
        knock_sequence: str = "",
        hostname: str = "",
    ) -> PreCommandTestResult:
        """Run *command* once and report what happened, for the editor's Test.

        Deliberately outside the serialize/coalesce machinery: the user asked
        for this exact command to run now, and reusing a recent result or
        queueing behind a launch would answer a different question than the
        one they asked. It also publishes no notice -- nothing is connecting,
        so no surface should show a connection doing anything.

        A configured knock is sent first and, if it fails, reported on its
        own: a user testing a knock that cannot even be sent is not helped by
        also being told about the command that followed it.
        """

        command = command.strip() if isinstance(command, str) else ""
        knock_sequence = (
            knock_sequence.strip() if isinstance(knock_sequence, str) else ""
        )
        if knock_sequence:
            result = self._test_knock(knock_sequence, hostname)
            if not result.succeeded or not command:
                return result
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

    def _test_knock(self, knock_sequence: str, hostname: str) -> PreCommandTestResult:
        """Send a sequence for the editor's Test button.

        Strict about the syntax where a launch is forgiving, because this is
        the one moment the user can see the mistake and fix it. Telling them
        "8OOO is not a port number" here is worth far more than silently
        knocking two of three ports at connect time.
        """
        started = self._clock()

        def failed(detail: str) -> PreCommandTestResult:
            return PreCommandTestResult(
                reason=PreCommandReason.START_FAILED,
                stage=PreCommandStage.KNOCK,
                duration_ms=_elapsed_ms(started, self._clock()),
                output=detail[: PreCommandTestResult.MAX_OUTPUT_CHARS],
            )

        try:
            steps = parse_knock_sequence(knock_sequence)
        except ValueError as error:
            return failed(str(error))
        if not steps:
            return PreCommandTestResult(
                reason=PreCommandReason.OK, stage=PreCommandStage.KNOCK
            )
        hostname = hostname.strip() if isinstance(hostname, str) else ""
        if not hostname:
            # Testing against nothing would report a success that proves
            # nothing; the connection has no host yet and the user needs to
            # know that is why.
            return failed("this connection has no hostname to knock yet")
        logger.info("port knock test starting ports=%d", len(steps))
        try:
            outcome = send_knock(hostname, steps)
        except KnockResolutionError as error:
            return failed(str(error))
        except Exception as exc:
            logger.debug("port knock test failed", exc_info=True)
            return failed(type(exc).__name__)
        logger.info(
            "port knock test finished sent=%d failed=%d duration_ms=%d",
            outcome.sent,
            len(outcome.failed),
            outcome.duration_ms,
        )
        logger.debug("port knock test sent to %s", outcome.address)
        if not outcome.succeeded:
            return failed(outcome.failure_reason)
        # No output on success. The frontend's own "Sent in 0.6s." says it,
        # in the user's language; a sentence composed here could not be
        # translated. What was sent and where is in the debug log.
        return PreCommandTestResult(
            reason=PreCommandReason.OK,
            stage=PreCommandStage.KNOCK,
            duration_ms=outcome.duration_ms,
        )

    def _execute_captured(self, command: str, timeout: int) -> tuple:
        """Like :meth:`_execute`, but keeps the output for the caller."""

        argv = self._shell_argv(command)
        started = self._clock()
        with self._capture_files() as (out_file, err_file):
            try:
                result = self._runner(
                    argv,
                    timeout=timeout,
                    stdout=out_file,
                    stderr=err_file,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                # Show what it printed before it was killed: for a command
                # that hangs, that is usually the only clue.
                return (
                    PreCommandReason.TIMED_OUT,
                    None,
                    _elapsed_ms(started, self._clock()),
                    self._merged_capture(out_file, err_file),
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
            merged = self._merged_capture(out_file, err_file)
        reason = (
            PreCommandReason.OK if exit_code == 0 else PreCommandReason.NONZERO_EXIT
        )
        return reason, exit_code, duration_ms, merged

    def _merged_capture(self, out_file, err_file) -> str:
        """Both streams in one blob, which is what the Test result shows."""
        return "".join(
            (self._read_capture(out_file), self._read_capture(err_file))
        ).strip()

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
        stage: PreCommandStage = PreCommandStage.COMMAND,
        exit_code: Optional[int] = None,
        duration_ms: int = 0,
        aborted: bool = False,
        output: str = "",
    ) -> None:
        notice = PreConnectionCommandNotice(
            connection_id=connection_id,
            scope_id=scope_id,
            kind=kind,
            phase=phase,
            reason=reason,
            stage=stage,
            exit_code=exit_code,
            duration_ms=duration_ms,
            aborted=aborted,
            output=output[: PreConnectionCommandNotice.MAX_OUTPUT_CHARS],
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


__all__ = ["PreConnectionCommandRunner"]
