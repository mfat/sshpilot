"""GTK-free logging policy shared by the frontend, daemon, and helpers.

This module owns policy, not application lifecycle.  In particular, it never
imports GTK/GI and it never makes two processes write the same rotating file.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Iterator, Mapping, Optional

LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
LOG_CONSOLE_FORMAT = "%(asctime)s %(levelname)-5s %(short_name)s: %(message)s"
DAEMON_FORWARD_MAX_BYTES = 64 * 1024

# Keep the policy module independent from ``sshpilot.api`` as well as GTK/GI.
# The public enum uses these same wire spellings, but importing the API package
# here would run its client exports and create a configuration-time cycle.
_LEVEL_WARNING = "warning"
_LEVEL_INFO = "info"
_LEVEL_DEBUG = "debug"


_LEVELS = {
    _LEVEL_WARNING: logging.WARNING,
    _LEVEL_INFO: logging.INFO,
    _LEVEL_DEBUG: logging.DEBUG,
}


def normalize_log_level(value: object, default: str = _LEVEL_INFO) -> int:
    """Return a strict Python level for a public/configured value."""

    text = str(value).strip().lower() if value is not None else ""
    return _LEVELS.get(text, _LEVELS.get(default, logging.INFO))


def log_level_name(level: int) -> str:
    """Return the public level spelling used by configuration and the API."""

    if level <= logging.DEBUG:
        return _LEVEL_DEBUG
    if level <= logging.INFO:
        return _LEVEL_INFO
    return _LEVEL_WARNING


_SSH_CATEGORY_PREFIXES = (
    "sshpilot.connection_manager",
    "sshpilot.terminal",
    "sshpilot.terminal_manager",
    "sshpilot.terminal_backends",
    "sshpilot.ssh_utils",
    "sshpilot.ssh_config_utils",
    "sshpilot.ssh_connection_builder",
    "sshpilot.sshcopyid_window",
    "sshpilot.sshpilot_agent",
    "sshpilot.scp_utils",
    "sshpilot.sftp_utils",
    "sshpilot.known_hosts_editor",
)


def logger_in_category(name: str, prefixes: Iterable[str]) -> bool:
    return any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)


def is_ssh_category(name: str) -> bool:
    return logger_in_category(name or "", _SSH_CATEGORY_PREFIXES)


def is_frontend_app_category(name: str) -> bool:
    """Classify the historical frontend App convenience log."""

    name = name or ""
    if name.startswith("daemon.forwarded."):
        return False
    if not (name == "sshpilot" or name.startswith("sshpilot.") or name == "root"):
        return False
    return not is_ssh_category(name)


_SENSITIVE_MAPPING = re.compile(
    r"(?i)([\"'](?:password|passphrase|passwd|secret|credential|otp|mfa|pin|"
    r"api[_-]?key|token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"askpass[_-]?token|kdbx[_-]?(?:password|key)|private[_-]?key)[\"']\s*:\s*)"
    r"(?:\"[^\"]*\"|'[^']*')"
)
_SENSITIVE_KEY = re.compile(
    r"(?i)((?:password|passphrase|passwd|secret|credential|otp|mfa|pin|"
    r"api[_-]?key|token|access[_-]?token|refresh[_-]?token|session[_-]?token|"
    r"askpass[_-]?token|kdbx[_-]?(?:password|key)|private[_-]?key)"
    r"\s*[:=]\s*)"
    r"(?:'(?:[^']*)'|\"(?:[^\"]*)\"|[^\s,;\]}]+)"
)
_AUTHORIZATION = re.compile(
    r"(?i)(\b(?:authorization|proxy-authorization)\s*:\s*(?:bearer|basic)\s+)\S+"
)
_TOKEN_ENV = re.compile(
    r"(?i)(\bSSHPILOT_[A-Z0-9_]*TOKEN[A-Z0-9_]*\s*=\s*)"
    r"('(?:[^']*)'|\"(?:[^\"]*)\"|\S+)"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.DOTALL,
)
_SECRET_FRAME = re.compile(
    r"(?i)(\b(?:secret[-_ ]?frame|secret[-_ ]?payload|credential[-_ ]?payload)\s*"
    r"[=:]\s*)\S+"
)


def sanitize_log_text(text: str) -> str:
    """Redact targeted secret material from text and exception messages.

    This is defense in depth. Sensitive DTOs and terminal/secret frames must
    still never be handed to a logger by their callers.
    """

    value = str(text)
    value = _PRIVATE_KEY.sub("***REDACTED***", value)
    value = _AUTHORIZATION.sub(r"\1***REDACTED***", value)
    value = _TOKEN_ENV.sub(r"\1***REDACTED***", value)
    value = _SECRET_FRAME.sub(r"\1***REDACTED***", value)
    value = _SENSITIVE_MAPPING.sub(r'\1"***REDACTED***"', value)
    return _SENSITIVE_KEY.sub(r"\1***REDACTED***", value)


_log_context: contextvars.ContextVar[Mapping[str, object]] = contextvars.ContextVar(
    "sshpilot_log_context", default={}
)
_CONTEXT_KEYS = (
    "request",
    "client",
    "method",
    "connection",
    "session",
    "operation",
    "transfer",
    "sftp_service",
    "forward",
    "interaction",
)


@contextlib.contextmanager
def log_context(**values: object) -> Iterator[None]:
    """Temporarily add safe stable IDs to records emitted in this context."""

    current = dict(_log_context.get())
    for key, value in values.items():
        if key in _CONTEXT_KEYS and value is not None and str(value):
            current[key] = str(value)
    token = _log_context.set(current)
    try:
        yield
    finally:
        _log_context.reset(token)


def copy_log_context() -> contextvars.Context:
    """Copy the current context for a worker boundary."""

    return contextvars.copy_context()


class SanitizingFormatter(logging.Formatter):
    """Formatter that sanitizes the final message, including tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        context = _log_context.get()
        suffix = " ".join(
            f"{key}={context[key]}" for key in _CONTEXT_KEYS if key in context
        )
        if suffix:
            rendered = f"{rendered} [{suffix}]"
        return sanitize_log_text(rendered)


class _ShortNameFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        record.short_name = name[len("sshpilot."):] if name.startswith("sshpilot.") else name or "-"
        return True


class _AppCategoryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return is_frontend_app_category(record.name or "")


class _SshCategoryFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return is_ssh_category(record.name or "")


class ManagedRotatingFileHandler(RotatingFileHandler):
    """A normal rotating handler with explicit SSH Pilot ownership metadata."""

    def __init__(self, filename: str, *, role: str) -> None:
        super().__init__(
            filename,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        self._sshpilot_managed = True
        self._sshpilot_log_role = role


def close_managed_handlers(
    root: Optional[logging.Logger] = None, *, roles: Optional[Iterable[str]] = None
) -> None:
    """Remove/flush/close only handlers owned by this application."""

    root = root or logging.getLogger()
    allowed = set(roles) if roles is not None else None
    for handler in list(root.handlers):
        if not getattr(handler, "_sshpilot_managed", False):
            continue
        if allowed is not None and getattr(handler, "_sshpilot_log_role", None) not in allowed:
            continue
        root.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()


def set_managed_handler_level(level: object, *, root: Optional[logging.Logger] = None) -> int:
    """Update all managed handlers and the root logger without duplication."""

    numeric = normalize_log_level(level)
    root = root or logging.getLogger()
    root.setLevel(numeric)
    for handler in root.handlers:
        if getattr(handler, "_sshpilot_managed", False):
            handler.setLevel(numeric)
    return numeric


def _add_file_handler(root: logging.Logger, path: Path, role: str, level: int) -> ManagedRotatingFileHandler:
    handler = ManagedRotatingFileHandler(str(path), role=role)
    handler.setLevel(level)
    handler.setFormatter(SanitizingFormatter(LOG_FILE_FORMAT, datefmt=LOG_DATE_FORMAT))
    root.addHandler(handler)
    return handler


def configure_frontend_logging(log_dir: os.PathLike[str] | str, level: object) -> tuple[logging.Handler, ...]:
    """Configure frontend master/App/SSH files and its canonical console."""

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    numeric = normalize_log_level(level)
    root = logging.getLogger()
    close_managed_handlers(root, roles=("frontend.master", "frontend.app", "frontend.ssh", "frontend.console"))
    root.setLevel(numeric)
    handlers = [
        _add_file_handler(root, directory / "sshpilot.log", "frontend.master", numeric),
        _add_file_handler(root, directory / "app.log", "frontend.app", numeric),
        _add_file_handler(root, directory / "ssh.log", "frontend.ssh", numeric),
    ]
    handlers[1].addFilter(_AppCategoryFilter())
    handlers[2].addFilter(_SshCategoryFilter())
    console = logging.StreamHandler()
    console._sshpilot_managed = True  # type: ignore[attr-defined]
    console._sshpilot_log_role = "frontend.console"  # type: ignore[attr-defined]
    console.setLevel(numeric)
    console.setFormatter(SanitizingFormatter(LOG_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    console.addFilter(_ShortNameFilter())
    root.addHandler(console)
    return tuple(handlers + [console])


def configure_daemon_logging(log_dir: os.PathLike[str] | str, level: object) -> tuple[logging.Handler, ...]:
    """Configure daemon.log and the daemon's optional terminal console."""

    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    numeric = normalize_log_level(level)
    root = logging.getLogger()
    close_managed_handlers(root, roles=("daemon.file", "daemon.console"))
    root.setLevel(numeric)
    file_handler = _add_file_handler(root, directory / "daemon.log", "daemon.file", numeric)
    console = logging.StreamHandler()
    console._sshpilot_managed = True  # type: ignore[attr-defined]
    console._sshpilot_log_role = "daemon.console"  # type: ignore[attr-defined]
    console.setLevel(numeric)
    console.setFormatter(SanitizingFormatter(LOG_CONSOLE_FORMAT, datefmt="%H:%M:%S"))
    console.addFilter(_ShortNameFilter())
    root.addHandler(console)
    return (file_handler, console)


class DaemonLogTailForwarder:
    """One bounded, rotation-aware tailer for daemon.log."""

    def __init__(self, path: os.PathLike[str] | str, *, poll_interval: float = 0.25) -> None:
        self.path = Path(path)
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._offset = 0
        self._inode: Optional[tuple[int, int]] = None
        self._buffer = ""
        self._waiting_for_file = False
        self._logger = logging.getLogger("daemon.forwarded")

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            stat = self.path.stat()
            self._offset = stat.st_size
            self._inode = (stat.st_dev, stat.st_ino)
        except OSError:
            self._offset = 0
            self._inode = None
            self._waiting_for_file = True
        else:
            self._waiting_for_file = False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="DaemonLogForwarder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.poll_interval * 4))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval):
            self._read_new()

    def _read_new(self) -> None:
        try:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            if self._waiting_for_file:
                # A verbose launcher may start the tailer before daemon.log
                # exists. The first file is new startup output, not history.
                self._offset = 0
                self._buffer = ""
                self._waiting_for_file = False
            elif self._inode is not None and identity != self._inode:
                self._offset = 0
                self._buffer = ""
            if stat.st_size < self._offset:
                self._offset = 0
                self._buffer = ""
            self._inode = identity
            if stat.st_size == self._offset:
                return
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                if stat.st_size - self._offset > DAEMON_FORWARD_MAX_BYTES:
                    stream.seek(stat.st_size - DAEMON_FORWARD_MAX_BYTES)
                    stream.readline()
                else:
                    stream.seek(self._offset)
                data = stream.read()
                self._offset = stream.tell()
        except OSError:
            self._waiting_for_file = True
            return
        self._buffer += data
        lines = self._buffer.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._buffer = lines.pop()
        else:
            self._buffer = ""
        for line in lines:
            self._forward(line.rstrip("\r\n"))

    def _forward(self, line: str) -> None:
        parts = line.split(" - ", 3)
        if len(parts) >= 4:
            source_name = parts[1].strip()
            level_name = parts[2].strip().upper()
            message = parts[3]
            level = getattr(logging, level_name, logging.INFO)
            target = logging.getLogger(f"daemon.forwarded.{source_name}")
            target.log(level, "%s", message)
        elif line:
            self._logger.debug("%s", line)


_forwarder_lock = threading.Lock()
_daemon_forwarder: Optional[DaemonLogTailForwarder] = None


def ensure_daemon_log_forwarder(
    path: os.PathLike[str] | str, *, enabled: bool
) -> Optional[DaemonLogTailForwarder]:
    """Start/reuse the process-wide daemon tailer only in explicit verbose mode."""

    global _daemon_forwarder
    with _forwarder_lock:
        if not enabled:
            if _daemon_forwarder is not None:
                _daemon_forwarder.stop()
                _daemon_forwarder = None
            return None
        if _daemon_forwarder is None or _daemon_forwarder.path != Path(path):
            if _daemon_forwarder is not None:
                _daemon_forwarder.stop()
            _daemon_forwarder = DaemonLogTailForwarder(path)
            _daemon_forwarder.start()
        else:
            _daemon_forwarder.start()
        return _daemon_forwarder


def stop_daemon_log_forwarder() -> None:
    global _daemon_forwarder
    with _forwarder_lock:
        if _daemon_forwarder is not None:
            _daemon_forwarder.stop()
            _daemon_forwarder = None
