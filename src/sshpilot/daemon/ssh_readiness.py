"""Daemon-owned OpenSSH session readiness: capability probe + diagnostics.

The daemon instruments its owned SSH terminal launches with a private
``-v -E <file>`` pair so OpenSSH's own verbose stream (never shown in the
user terminal) provides authoritative local evidence of authentication.  This
module owns everything around that evidence:

* a secure per-daemon runtime directory under
  ``$XDG_RUNTIME_DIR/sshpilot/diagnostics/<instance>/`` (0700 dirs, 0600
  files, no symlinks, no ``/tmp`` escape);
* a cached per-executable capability probe (``ssh -G -F /dev/null -E file
  localhost`` — no network) plus ``ssh -V`` for logging/cache invalidation;
* per-session leases: create the file before OpenSSH execs, install the
  inotify watch, parse appended bytes with :class:`SshDiagnosticParser`, and
  deliver the decisive verdict to the session runtime (also on a bounded
  grace deadline for the PTY-evidence fallback).

Any unavailable prerequisite (no runtime dir, no inotify, a probe that fails)
disables diagnostics for the launch; the session runtime then relies on the
PTY connection-evidence path.  Nothing here touches the public API and every
secret stays out of the diagnostic stream (it carries OpenSSH debug logging
only).
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence

from sshpilot.core.ssh_diagnostics import SshDiagnosticParser, SshDiagnosticResult
from sshpilot.daemon.lifecycle import ensure_private_runtime_directory
from sshpilot.daemon.ssh_diagnostics_monitor import (
    SshDiagnosticsMonitor,
    inotify_available,
)

logger = logging.getLogger(__name__)

DEFAULT_READINESS_GRACE_SECONDS = 8.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 3.0
DEFAULT_VERSION_TIMEOUT_SECONDS = 2.0

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_VISIBLE_VERBOSITY_RE = re.compile(r"^-v+$")
# Short OpenSSH options that consume a separate argument token.  Only the
# option portion of the command line (everything before the destination) is
# ever scanned, so a ``-v`` inside a remote command stays out of scope.
_SSH_VALUE_OPTIONS = frozenset(
    {
        "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-j",
        "-J", "-K", "-L", "-l", "-m", "-O", "-o", "-P", "-p", "-Q",
        "-R", "-S", "-W", "-w", "-x",
    }
)
_ASKPASS_ENV_KEYS = (
    "SSH_ASKPASS",
    "SSH_ASKPASS_REQUIRE",
    "DISPLAY",
    "SSHPILOT_DAEMON_ASKPASS_SOCKET",
    "SSHPILOT_DAEMON_ASKPASS_TOKEN",
)

OnResultCallback = Callable[[object, SshDiagnosticResult], None]
OnGraceExpiredCallback = Callable[[object], None]


def launch_eligible_for_diagnostics(argv: Sequence[str]) -> bool:
    """Return whether *argv* may carry the private ``-v -E`` instrumentation.

    Only canonical OpenSSH ``ssh`` launches qualify.  SCP/SFTP/ssh-copy-id
    binaries, a user-supplied ``-E`` (never overwrite it), and any user-
    requested verbosity (``-v``/``-vv``/``-vvv``) exclude the launch; in the
    verbosity case the PTY stream already carries the verbose evidence.

    Only the option portion before the destination is inspected: a ``-v`` or
    ``-E`` belonging to the remote command (``ssh host some-command -v``) is
    not user OpenSSH verbosity and must not disqualify the instrumentation.
    Options that take a separate argument are skipped so their values are not
    mistaken for the destination.
    """
    if not argv:
        return False
    if os.path.basename(str(argv[0])) not in {"ssh", "ssh.exe"}:
        return False
    tokens = [str(token) for token in argv[1:]]
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            # First non-option token is the destination; the remote command
            # follows it and is out of scope.
            break
        if token.startswith("-E") or _VISIBLE_VERBOSITY_RE.match(token):
            return False
        if token in _SSH_VALUE_OPTIONS:
            index += 2
            continue
        index += 1
    return True


def insert_ssh_diagnostics_options(
    argv: Sequence[str],
    diagnostics_path: str,
) -> tuple[str, ...]:
    """Insert ``-v -E <path>`` before the destination.

    Options are inserted right after the executable / ``-F`` config so they
    are never mistaken for the remote command (which may trail the
    destination).  The destination stays ``argv[-1]`` when no remote command
    is set.
    """
    argv = tuple(argv)
    insert_at = 1
    if len(argv) >= 3 and argv[1] == "-F":
        insert_at = 3
    options = ("-v", "-E", diagnostics_path)
    return (*argv[:insert_at], *options, *argv[insert_at:])


def resolve_diagnostics_root(
    runtime_dir: Optional[str],
    instance_id: str,
) -> Optional[Path]:
    """Return the secure diagnostics root for this daemon instance.

    ``None`` when the runtime directory is unusable (unset, non-private,
    symlinked, unwritable, or Flatpak-sandboxed storage that fails the
    write probe).  No ``/tmp`` fallback is ever attempted.
    """
    if not runtime_dir:
        return None
    root = Path(runtime_dir) / "sshpilot" / "diagnostics" / instance_id
    try:
        runtime_info = Path(runtime_dir).lstat()
    except OSError:
        return None
    if stat.S_ISLNK(runtime_info.st_mode) or not stat.S_ISDIR(runtime_info.st_mode):
        return None
    try:
        ensure_private_runtime_directory(Path(runtime_dir) / "sshpilot")
        ensure_private_runtime_directory(Path(runtime_dir) / "sshpilot" / "diagnostics")
        ensure_private_runtime_directory(root)
    except OSError:
        logger.debug("diagnostics runtime directory is unusable: %s", root)
        return None
    # Prove the location is actually writable before enabling diagnostics.
    probe = root / f"write-probe-{secrets.token_hex(6)}.log"
    try:
        fd = os.open(
            str(probe),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except OSError:
        return None
    try:
        os.write(fd, b"x")
        os.fsync(fd)
    except OSError:
        os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass
        return None
    os.close(fd)
    try:
        probe.unlink()
    except OSError:
        return None
    return root


def _sanitized_probe_env(environment: Mapping[str, str]) -> dict[str, str]:
    probe_env = dict(environment or os.environ)
    for name in _ASKPASS_ENV_KEYS:
        probe_env.pop(name, None)
    for name in tuple(probe_env):
        if (
            name.startswith("SSHPILOT_ASKPASS_")
            or name.startswith("SSHPILOT_SESSION_")
            or name.startswith("SSHPILOT_DAEMON_")
        ):
            probe_env.pop(name, None)
    probe_env.setdefault("HOME", os.path.expanduser("~"))
    probe_env.setdefault("PATH", "/usr/bin:/bin")
    return probe_env


@dataclass
class _SessionDiagnostics:
    session_id: object
    path: Path
    parser: SshDiagnosticParser
    on_result: OnResultCallback
    on_grace_expired: Optional[OnGraceExpiredCallback]
    engaged: bool = False
    decisive: bool = False
    decisive_result: Optional[SshDiagnosticResult] = None
    finished: bool = False
    timer: Optional[threading.Timer] = None
    monitor_key: object = field(default=None)


class SshReadinessManager:
    """Daemon-scoped owner of OpenSSH diagnostics for owned terminal sessions."""

    def __init__(
        self,
        *,
        instance_id: str,
        grace_seconds: float = DEFAULT_READINESS_GRACE_SECONDS,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
        version_timeout: float = DEFAULT_VERSION_TIMEOUT_SECONDS,
        runtime_dir: Optional[str] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not instance_id:
            raise ValueError("readiness manager requires a daemon instance id")
        if grace_seconds <= 0 or probe_timeout <= 0 or version_timeout <= 0:
            raise ValueError("readiness timeouts must be positive")
        self._grace_seconds = float(grace_seconds)
        self._probe_timeout = float(probe_timeout)
        self._version_timeout = float(version_timeout)
        self._clock = clock
        if runtime_dir is None:
            runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
        self._root = resolve_diagnostics_root(runtime_dir, instance_id)
        self._monitor: Optional[SshDiagnosticsMonitor] = None
        if self._root is not None and inotify_available():
            try:
                self._monitor = SshDiagnosticsMonitor()
            except (OSError, RuntimeError):
                self._monitor = None
                logger.debug("inotify diagnostics monitor is unavailable")
        self._lock = threading.Lock()
        self._sessions: Dict[object, _SessionDiagnostics] = {}
        self._capability_cache: Dict[tuple, bool] = {}
        self._versions: Dict[str, str] = {}
        self._closed = False

    @property
    def available(self) -> bool:
        return self._root is not None and self._monitor is not None

    # -- session lifecycle --------------------------------------------------

    def subscribe(
        self,
        session_id: object,
        on_result: OnResultCallback,
        on_grace_expired: Optional[OnGraceExpiredCallback] = None,
    ) -> None:
        """Register the runtime callbacks before the launch executes."""
        if not callable(on_result):
            raise TypeError("diagnostics result callback must be callable")
        deliver = None
        with self._lock:
            if self._closed:
                return
            record = self._sessions.get(session_id)
            if record is None:
                record = _SessionDiagnostics(
                    session_id=session_id,
                    path=Path(),
                    parser=SshDiagnosticParser(),
                    on_result=on_result,
                    on_grace_expired=on_grace_expired,
                )
                self._sessions[session_id] = record
            else:
                record.on_result = on_result
                record.on_grace_expired = on_grace_expired
                if record.decisive:
                    deliver = record.decisive_result
        if deliver is not None:
            try:
                on_result(session_id, deliver)
            except Exception:
                logger.debug("diagnostics result callback failed", exc_info=True)

    def prepare_launch(
        self,
        session_id: object,
        argv: Sequence[str],
        environment: Mapping[str, str],
    ) -> Optional[str]:
        """Arm diagnostics for one launch; return the file path or ``None``.

        Eligible when the launch is a canonical ``ssh`` invocation, the
        executable capability probe succeeds, and the secure storage + event
        monitor are available.  Ordering is strict: the file is created, the
        watch is installed *and acknowledged*, and only then is the session
        marked engaged (which arms the grace timer).  OpenSSH execs after the
        returned path is inserted into argv, so the first write is always
        watched.  Any failure degrades this one launch to the PTY evidence
        path (``None``) — never a session startup error.
        """
        if not self.available or not launch_eligible_for_diagnostics(argv):
            return None
        if not self._capable(argv[0], environment):
            return None
        path = self._diagnostics_path(session_id)
        if path is None:
            return None
        try:
            fd = os.open(
                str(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except OSError:
            logger.debug("diagnostics file creation failed for %r", session_id)
            return None
        os.close(fd)
        # The record must exist before the watch can deliver, so the monitor
        # thread never races a brand-new session; engagement is still deferred
        # until the watch is acknowledged below.
        with self._lock:
            if self._closed:
                try:
                    path.unlink()
                except OSError:
                    pass
                return None
            record = self._sessions.get(session_id)
            if record is None:
                record = _SessionDiagnostics(
                    session_id=session_id,
                    path=Path(),
                    parser=SshDiagnosticParser(),
                    on_result=None,
                    on_grace_expired=None,
                )
                self._sessions[session_id] = record
            record.path = path
            key = ("diag", session_id)
            record.monitor_key = key
        assert self._monitor is not None
        try:
            registered = self._monitor.register(
                key, str(path), self._file_reader(session_id)
            )
        except (RuntimeError, OSError):
            registered = False
        if not registered:
            # No watch: this launch stays on the PTY evidence path.  Release
            # the owned file and the placeholder record; nothing was engaged.
            with self._lock:
                if self._sessions.get(session_id) is record:
                    self._sessions.pop(session_id, None)
            try:
                path.unlink()
            except OSError:
                pass
            logger.debug("diagnostics registration failed for %r", session_id)
            return None
        with self._lock:
            if self._closed:
                try:
                    self._monitor.unregister(key, unlink_path=str(path))
                except RuntimeError:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return None
            record = self._sessions.get(session_id)
            if record is None or record.finished:
                try:
                    self._monitor.unregister(key, unlink_path=str(path))
                except RuntimeError:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                return None
            record.engaged = True
            if not record.decisive:
                record.timer = threading.Timer(
                    self._grace_seconds,
                    self._fire_grace_expired,
                    args=(session_id,),
                )
                record.timer.daemon = True
                record.timer.start()
        return str(path)

    def is_engaged(self, session_id: object) -> bool:
        with self._lock:
            record = self._sessions.get(session_id)
            return bool(
                record is not None
                and record.engaged
                and not record.finished
            )

    def finish(self, session_id: object) -> None:
        """Drain, unregister, and clean up a session's diagnostics (idempotent)."""
        with self._lock:
            record = self._sessions.pop(session_id, None)
            if record is None or record.finished:
                return
            record.finished = True
            if record.timer is not None:
                record.timer.cancel()
            key = record.monitor_key
            path = record.path
        if self._monitor is not None and key is not None:
            self._monitor.unregister(
                key, unlink_path=str(path) if path else None
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            records = tuple(self._sessions.values())
            self._sessions.clear()
        for record in records:
            if record.timer is not None:
                record.timer.cancel()
        if self._monitor is not None:
            self._monitor.close()
            self._monitor = None
        if self._root is not None:
            try:
                self._root.rmdir()
            except OSError:
                pass

    # -- capability probe ----------------------------------------------------

    def _ssh_version(self, executable: str, environment: Mapping[str, str]) -> str:
        with self._lock:
            cached = self._versions.get(executable)
        if cached is not None:
            return cached
        try:
            result = subprocess.run(
                (executable, "-V"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._version_timeout,
                env=_sanitized_probe_env(environment),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            version = "unknown"
        else:
            version = (
                (result.stderr or result.stdout).decode("utf-8", errors="replace")
                .strip()
                or "unknown"
            )
        with self._lock:
            self._versions[executable] = version
        logger.debug("ssh readiness probe: %s is %s", executable, version)
        return version

    def _capable(self, executable: str, environment: Mapping[str, str]) -> bool:
        version = self._ssh_version(executable, environment)
        key = (executable, version)
        with self._lock:
            cached = self._capability_cache.get(key)
        if cached is not None:
            return cached
        probe_path = self._probe_path()
        if probe_path is None:
            return False
        capable = False
        try:
            result = subprocess.run(
                (
                    executable,
                    "-G",
                    "-F",
                    "/dev/null",
                    "-E",
                    str(probe_path),
                    "localhost",
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self._probe_timeout,
                env=_sanitized_probe_env(environment),
                check=False,
            )
            capable = result.returncode == 0 and probe_path.exists()
        except (OSError, subprocess.SubprocessError):
            capable = False
        finally:
            try:
                probe_path.unlink()
            except OSError:
                pass
        with self._lock:
            self._capability_cache[key] = capable
        logger.debug(
            "ssh readiness capability for %s: %s",
            executable,
            "available" if capable else "unavailable",
        )
        return capable

    # -- file plumbing ---------------------------------------------------------

    def _diagnostics_path(self, session_id: object) -> Optional[Path]:
        if self._root is None:
            return None
        name = str(session_id)
        if _SESSION_NAME_RE.match(name) is None:
            return None
        return self._root / f"{name}.log"

    def _probe_path(self) -> Optional[Path]:
        if self._root is None:
            return None
        path = self._root / f"probe-{secrets.token_hex(6)}.log"
        try:
            fd = os.open(
                str(path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except OSError:
            return None
        os.close(fd)
        return path

    def _file_reader(self, session_id: object) -> Callable[[bytes], None]:
        def _on_data(data: bytes) -> None:
            self._consume(session_id, data)

        return _on_data

    # -- event handling ---------------------------------------------------------

    def _consume(self, session_id: object, data: bytes) -> None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None or record.finished or record.decisive:
                return
            result = record.parser.feed(data)
            if result is not None:
                record.decisive = True
                record.decisive_result = result
                if record.timer is not None:
                    record.timer.cancel()
                callback = record.on_result
            else:
                callback = None
        if callback is not None:
            try:
                callback(session_id, result)
            except Exception:
                logger.debug("diagnostics result callback failed", exc_info=True)

    def _fire_grace_expired(self, session_id: object) -> None:
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None or record.finished or record.decisive:
                return
            callback = record.on_grace_expired
        if callback is not None:
            try:
                callback(session_id)
            except Exception:
                logger.debug("diagnostics grace callback failed", exc_info=True)
