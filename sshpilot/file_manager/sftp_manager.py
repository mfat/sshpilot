"""Asynchronous SFTP layer for the in-app file manager.

Wraps paramiko in a worker-thread executor and emits GObject signals when
significant events happen (connection state, progress, errors). All UI
work is marshalled back to the GTK main loop via ``GLib.idle_add`` so
callers never touch widgets from a worker thread.
"""

from __future__ import annotations

import dataclasses
import errno
import logging
import os
import pathlib
import posixpath
import re
import stat
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

import paramiko
from gi.repository import GLib, GObject

from .exceptions import TransferCancelledException
from .remote_walk import _sftp_path_exists, stat_isdir


logger = logging.getLogger(__name__)


def _existing_identity_files(raw: Any) -> List[str]:
    """Expand a config ``IdentityFile`` value to deduped paths that exist on disk.

    Accepts a string or list (as ``ssh -G`` / ``get_effective_ssh_config``
    returns it). Used to offer a host exactly its configured identity instead of
    spraying every agent/default key, matching native ``ssh -F config``.
    """

    if isinstance(raw, str):
        raw = [raw]
    result: List[str] = []
    for entry in raw or []:
        expanded = os.path.expanduser(os.path.expandvars(str(entry)))
        if os.path.isfile(expanded) and expanded not in result:
            result.append(expanded)
    return result


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None  # Number of items in directory (for folders only)


class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Asynchronous SFTP layer


class AsyncSFTPManager(GObject.GObject):
    """Small wrapper around :mod:`paramiko` that performs operations in
    worker threads.

    The class exposes a queue of operations and emits signals when important
    events happen.  Tests can monkeypatch :class:`paramiko.SSHClient` to avoid
    talking to a real server.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "authentication-required": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        # Carries actual byte counts so the progress dialog can compute speed
        # and ETA without back-deriving them from fraction × hard-coded total.
        # Args: (transferred_bytes, total_bytes_or_zero_if_unknown). Both are
        # Python ints (boxed via 'object') to avoid 32-bit truncation.
        "progress-bytes": (GObject.SignalFlags.RUN_FIRST, None, (object, object)),
        "operation-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "directory-loaded": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Callable[[Callable, tuple, dict], None] | None = None,
        connection: Any = None,
        connection_manager: Any = None,
        ssh_config: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._username = username
        self._password = password
        self._port = port or 22
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        # Use single worker to serialize SFTP operations - SFTP connections are not thread-safe
        # Operations will be queued and executed one at a time
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._dispatcher = dispatcher or (
            lambda cb, args=(), kwargs=None: _MainThreadDispatcher.dispatch(
                cb, *args, **(kwargs or {})
            )
        )
        self._lock = threading.Lock()
        # NOTE: ``_cancelled_operations`` is intentionally NOT protected by
        # ``_lock``. ``_lock`` is held for the entire duration of an SFTP
        # ``put()``/``get()`` call. If cancel signaling shared that lock:
        #   * the UI thread's cancel button would block until the transfer
        #     finished (no cancel possible during a transfer);
        #   * the progress callback (which runs in the worker thread already
        #     holding ``_lock``) would deadlock on its first invocation,
        #     because ``threading.Lock`` is non-reentrant — freezing the
        #     transfer too.
        # A dedicated, never-long-held lock decouples the two concerns.
        self._cancel_lock = threading.Lock()
        self._cancelled_operations = set()  # Track cancelled operation IDs
        # Monotonic counter for operation IDs. Combined with time_ns() it
        # guarantees no two transfers (within or across threads) ever share an
        # ID, even when scheduled in the same nanosecond. Bumped under _lock.
        self._operation_seq = 0
        self._connection = connection
        self._connection_manager = connection_manager
        self._ssh_config = dict(ssh_config) if ssh_config else None
        self._proxy_sock: Optional[Any] = None
        self._jump_clients: List[paramiko.SSHClient] = []
        self._keepalive_interval: int = 0
        self._keepalive_count_max: int = 0
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_stop_event: Optional[threading.Event] = None
        self._keepalive_failures: int = 0

    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"

    # -- connection -----------------------------------------------------

    def connect_to_server(self, password: Optional[str] = None) -> None:
        """Connect to server, optionally with a password for authentication."""
        if password is not None:
            self._password = password
        self._submit(
            self._connect_impl,
            on_success=lambda *_: self.emit("connected"),
            on_error=lambda exc: self.emit("connection-error", str(exc)),
        )

    def close(self) -> None:
        logger.info("AsyncSFTPManager.close() called")
        # Stop keepalive worker first
        self._stop_keepalive_worker()

        # Interrupt any in-flight blocking operation BEFORE taking ``_lock``.
        # A worker thread stuck in a no-timeout paramiko read (listdir/get/…)
        # holds ``_lock`` for the entire call, so waiting on ``_lock`` here would
        # deadlock and hang the whole app on quit. Closing the transport/socket
        # from this thread makes that blocked read raise, letting the worker
        # release ``_lock`` and finish. These are plain attribute reads (no lock)
        # used purely to force the sockets shut; closing twice is harmless, since
        # the locked cleanup below repeats it.
        interrupt_targets: List[Any] = []
        try:
            interrupt_targets = [self._sftp, self._client, self._proxy_sock]
            interrupt_targets.extend(self._jump_clients or [])
        except Exception:  # pragma: no cover - defensive
            interrupt_targets = []
        for obj in interrupt_targets:
            if obj is None:
                continue
            try:
                obj.close()
            except Exception as exc:  # best-effort interruption
                logger.debug("Error interrupting %r during close: %s", obj, exc)

        with self._lock:
            if self._sftp is not None:
                try:
                    self._sftp.close()
                except Exception as exc:
                    logger.debug(f"Error closing SFTP client: {exc}")
                finally:
                    self._sftp = None
            if self._client is not None:
                try:
                    self._client.close()
                except Exception as exc:
                    logger.debug(f"Error closing SSH client: {exc}")
                finally:
                    self._client = None
            if self._jump_clients:
                for jump_client in self._jump_clients:
                    try:
                        jump_client.close()
                    except Exception as exc:  # pragma: no cover - defensive cleanup
                        logger.debug("Error closing jump client: %s", exc)
                self._jump_clients.clear()

            if self._proxy_sock is not None:
                try:
                    self._proxy_sock.close()
                except Exception as exc:  # pragma: no cover - defensive cleanup
                    logger.debug("Error closing proxy socket: %s", exc)
                finally:
                    self._proxy_sock = None
        # Shutdown executor - use wait=False since connections are closed and will interrupt operations
        # This prevents hanging if there are stuck operations (like hanging listdir calls)
        logger.debug("Shutting down executor (connections closed, operations should be interrupted)")
        try:
            # Use wait=False to avoid hanging on stuck operations
            # The connections are already closed, so operations should fail quickly
            # Threads are daemon threads, so they'll be cleaned up when process exits
            self._executor.shutdown(wait=False)
            logger.debug("Executor shutdown initiated (non-blocking)")
        except Exception as exc:
            logger.debug(f"Error shutting down executor: {exc}")
        logger.info("AsyncSFTPManager.close() completed")

    def _reset_sftp_channel(self) -> None:
        """Recreate the SFTP channel after a botched in-flight transfer.

        paramiko's SFTPClient channel can be left in an unrecoverable state
        when ``put()``/``get()`` is interrupted mid-stream by raising from
        the progress callback. The transfer uses pipelined writes, so when
        the exception propagates there are typically packets in flight whose
        responses paramiko's reader never matches up. The channel then sits
        forever waiting for responses that never come, and every subsequent
        ``listdir``/``stat``/etc. queued on this manager hangs behind it.

        Tearing down the SFTPClient and asking the still-alive SSH client
        for a new channel via ``open_sftp()`` is cheap (no re-auth, no
        re-handshake) and unblocks the worker.
        """
        with self._lock:
            client = self._client
            old_sftp = self._sftp
            # Drop the reference first so any concurrent reader (e.g. the
            # keepalive thread) sees the channel as unavailable rather than
            # holding a stale reference to the broken one.
            self._sftp = None

        if old_sftp is not None:
            try:
                old_sftp.close()
            except Exception as exc:
                logger.debug(
                    "Old SFTP close error (expected after cancel): %s", exc
                )

        if client is None:
            logger.warning(
                "Cannot reset SFTP channel: SSH client is no longer available"
            )
            return

        try:
            new_sftp = client.open_sftp()
        except Exception as exc:
            logger.error(
                "Failed to reopen SFTP channel after cancelled transfer: %s",
                exc,
            )
            return

        with self._lock:
            self._sftp = new_sftp
        logger.info("SFTP channel reset after cancelled transfer")

    def _remove_remote_best_effort(self, path: str, *, context: str) -> None:
        """Try to delete a remote file; swallow errors and log them.

        Used to scrub partial files left by cancelled transfers. The remote
        file may or may not exist — depending on when in the put() the
        cancel landed — so a missing-file error is normal and logged at
        debug rather than reported to the user.
        """
        with self._lock:
            sftp = self._sftp
        if sftp is None:
            logger.debug(
                "Skipping %s cleanup of %s: SFTP unavailable", context, path
            )
            return
        try:
            with self._lock:
                if self._sftp is None:
                    return
                self._sftp.remove(path)
            logger.debug("Removed partial remote file (%s): %s", context, path)
        except IOError as exc:
            # FileNotFoundError or permission issues — partial may legitimately
            # never have been created, or the user can't delete it.
            logger.debug(
                "Could not remove partial remote file (%s) %s: %s",
                context, path, exc,
            )
        except Exception as exc:
            logger.debug(
                "Unexpected error removing partial remote file (%s) %s: %s",
                context, path, exc,
            )

    def _mark_cancelled(self, operation_id: str) -> None:
        """Thread-safe insert into the cancelled-operations set.

        Uses the dedicated ``_cancel_lock`` — see comment in ``__init__`` for
        why this must NOT use ``_lock``.
        """
        with self._cancel_lock:
            self._cancelled_operations.add(operation_id)

    def _discard_cancellation_flag(self, operation_id: str) -> None:
        """Thread-safe removal from the cancelled-operations set."""
        with self._cancel_lock:
            self._cancelled_operations.discard(operation_id)

    def _is_cancelled(self, operation_id: str) -> bool:
        """Thread-safe membership check for the cancelled-operations set."""
        with self._cancel_lock:
            return operation_id in self._cancelled_operations

    def _next_operation_id(self, kind: str) -> str:
        """Generate a unique operation ID for a transfer.

        Combines a monotonic counter with the current wall-clock nanoseconds
        so two transfers started in the same tick — or even the same
        nanosecond — can never share an ID. Uses ``_cancel_lock`` (the short
        bookkeeping lock) rather than the SFTP-ops ``_lock``: this method is
        called from the UI thread at the top of every ``upload``/``download``,
        and ``_lock`` is held for the entire duration of in-progress
        transfers, so contending on it would freeze the UI.
        """
        with self._cancel_lock:
            self._operation_seq += 1
            seq = self._operation_seq
        return f"{kind}_{id(self)}_{time.time_ns()}_{seq}"

    def _start_keepalive_worker(self) -> None:
        with self._lock:
            if self._keepalive_interval <= 0 or self._sftp is None:
                return
            if self._keepalive_thread is not None and self._keepalive_thread.is_alive():
                return

            stop_event = threading.Event()
            interval = self._keepalive_interval
            self._keepalive_stop_event = stop_event
            self._keepalive_failures = 0

        def _worker() -> None:
            logger.debug("SFTP keepalive worker started for %s", self._host)
            try:
                while not stop_event.wait(interval):
                    # Do the stat under the lock so close()/reset cannot pull
                    # the channel out from under us between the None-check and
                    # the call. An active transfer holds the lock, but that's
                    # fine — a transfer IS keepalive traffic.
                    stat_error: Optional[Exception] = None
                    sftp_exited = False
                    with self._lock:
                        sftp = self._sftp
                        count_max = self._keepalive_count_max
                        if sftp is None:
                            sftp_exited = True
                        else:
                            try:
                                sftp.stat(".")
                            except Exception as exc:
                                stat_error = exc
                    if sftp_exited:
                        logger.debug("Keepalive worker exiting because SFTP client is gone")
                        break

                    if stat_error is not None:
                        with self._lock:
                            self._keepalive_failures += 1
                            failures = self._keepalive_failures
                            count_max = self._keepalive_count_max
                        logger.debug(
                            "SFTP keepalive attempt failed (%s/%s): %s",
                            failures,
                            count_max,
                            stat_error,
                        )
                        if count_max >= 0 and failures > count_max:
                            message = (
                                "SFTP keepalive failed too many times; connection may be down"
                            )
                            logger.warning(message)
                            self._dispatcher(
                                self.emit,
                                ("operation-error", message),
                                {},
                            )
                            break
                    else:
                        with self._lock:
                            self._keepalive_failures = 0
            finally:
                logger.debug("SFTP keepalive worker exiting for %s", self._host)
                with self._lock:
                    if self._keepalive_thread is threading.current_thread():
                        self._keepalive_thread = None
                        self._keepalive_stop_event = None
                        self._keepalive_failures = 0

        thread = threading.Thread(
            target=_worker,
            name=f"SFTPKeepalive-{self._host}",
            daemon=True,
        )
        with self._lock:
            self._keepalive_thread = thread
        thread.start()

    def _stop_keepalive_worker(self) -> None:
        thread: Optional[threading.Thread]
        event: Optional[threading.Event]
        with self._lock:
            thread = self._keepalive_thread
            event = self._keepalive_stop_event
        if event is not None:
            event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            if thread is None or not thread.is_alive():
                self._keepalive_thread = None
                self._keepalive_stop_event = None
                self._keepalive_failures = 0

    # -- helpers --------------------------------------------------------

    def _submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Future:
        future = self._executor.submit(func)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - errors handled uniformly
                if on_error:
                    self._dispatcher(on_error, (exc,), {})
                else:
                    self._dispatcher(self.emit, ("operation-error", str(exc)), {})
            else:
                if on_success:
                    self._dispatcher(on_success, (result,), {})

        future.add_done_callback(_done)
        return future

    # -- actual work ----------------------------------------------------

    @staticmethod
    def _select_host_key_policy(strict_host: str, auto_add: bool) -> paramiko.MissingHostKeyPolicy:
        """Return an appropriate Paramiko host key policy based on settings."""

        normalized = (strict_host or "").strip().lower()
        try:
            if normalized in {"yes", "always"}:
                return paramiko.RejectPolicy()
            if normalized in {"no", "off", "accept-new", "accept_new"}:
                return paramiko.AutoAddPolicy()
            if normalized in {"ask", "accept-new-once", "ask-new"}:
                return paramiko.WarningPolicy()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to create host key policy for '%s': %s", normalized, exc)

        return paramiko.AutoAddPolicy() if auto_add else paramiko.RejectPolicy()

    @staticmethod
    def _parse_proxy_jump_entry(entry: str) -> Tuple[str, Optional[str], Optional[int]]:
        """Parse a ``ProxyJump`` token into host, optional user, and port."""

        token = entry.strip()
        if not token:
            return entry, None, None

        username: Optional[str] = None
        host_segment = token
        if "@" in token:
            username, host_segment = token.split("@", 1)

        port: Optional[int] = None
        hostname = host_segment

        if host_segment.startswith("[") and "]" in host_segment:
            bracket_end = host_segment.index("]")
            hostname = host_segment[1:bracket_end]
            remainder = host_segment[bracket_end + 1 :]
            if remainder.startswith(":"):
                try:
                    port = int(remainder[1:])
                except ValueError:
                    port = None
        elif ":" in host_segment:
            host_part, port_str = host_segment.rsplit(":", 1)
            hostname = host_part or host_segment
            try:
                port = int(port_str)
            except ValueError:
                port = None

        return hostname or entry, username, port

    def _create_proxy_jump_socket(
        self,
        jump_entries: List[str],
        *,
        config_override: Optional[str],
        policy: paramiko.MissingHostKeyPolicy,
        known_hosts_path: Optional[str],
        allow_agent: bool,
        look_for_keys: bool,
        key_filename: Optional[str],
        passphrase: Optional[str],
        resolved_host: str,
        resolved_port: int,
        base_username: str,
        connect_timeout: Optional[int] = None,
    ) -> Tuple[Any, List[paramiko.SSHClient]]:
        """Create a socket by chaining SSH connections through jump hosts."""

        from ..ssh_config_utils import get_effective_ssh_config

        def _coerce_port(value: Any, default: int) -> int:
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return default

        def _split_proxy_jump(value: Any) -> List[str]:
            if isinstance(value, (list, tuple, set)):
                tokens: List[str] = []
                for item in value:
                    tokens.extend(_split_proxy_jump(item))
                return tokens
            if isinstance(value, str):
                return [tok.strip() for tok in re.split(r"[\s,]+", value) if tok.strip()]
            return []

        resolved_hops: List[Dict[str, Any]] = []
        visited: set = set()

        def _expand_entry(raw_entry: str) -> None:
            host_token, explicit_user, explicit_port = self._parse_proxy_jump_entry(raw_entry)
            if host_token in visited:
                # Cycle protection: a hop's ProxyJump that loops back to an
                # already-resolved alias is skipped rather than recursed forever.
                logger.debug("Skipping already-resolved ProxyJump alias %s", host_token)
                return
            visited.add(host_token)
            try:
                hop_cfg = get_effective_ssh_config(host_token, config_file=config_override)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to resolve effective SSH config for ProxyJump host %s: %s",
                    host_token,
                    exc,
                )
                hop_cfg = {}

            # A jump host can itself sit behind its own ProxyJump; those inner
            # hops are closer to us, so resolve them first (native ssh chains the
            # full path, not just the outermost jump).
            for inner_entry in _split_proxy_jump(hop_cfg.get("proxyjump")):
                _expand_entry(inner_entry)

            hostname = str(hop_cfg.get("hostname", host_token) or host_token)
            username = str(explicit_user or hop_cfg.get("user", base_username) or base_username)
            port = explicit_port
            if port is None:
                port = _coerce_port(hop_cfg.get("port", 22), 22)
            resolved_hops.append(
                {
                    "raw": raw_entry,
                    "alias": host_token,
                    "hostname": hostname,
                    "username": username,
                    "port": port,
                    "config": hop_cfg,
                }
            )

        for raw_entry in jump_entries:
            _expand_entry(raw_entry)

        jump_clients: List[paramiko.SSHClient] = []
        upstream_sock: Optional[Any] = None

        try:
            for index, hop in enumerate(resolved_hops):
                jump_client = paramiko.SSHClient()
                try:
                    jump_client.load_system_host_keys()
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Unable to load system host keys for jump host: %s", exc)
                jump_client.set_missing_host_key_policy(policy)

                if known_hosts_path:
                    try:
                        if os.path.exists(known_hosts_path):
                            jump_client.load_host_keys(known_hosts_path)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug(
                            "Failed to load known hosts for ProxyJump host %s: %s",
                            hop["alias"],
                            exc,
                        )

                # Honor the jump host's OWN identity from ~/.ssh/config so we offer
                # the right key (and only that key), mirroring `ssh -F config`.
                # Without this we fall back to the target's key plus every agent /
                # default ``~/.ssh/id_*`` key, which can exceed the jump host's
                # MaxAuthTries and get the session dropped mid-auth (surfacing as
                # paramiko "No existing session").
                hop_cfg = hop.get("config") or {}
                hop_identities = _existing_identity_files(hop_cfg.get("identityfile"))

                # Preload the hop's passphrase-protected key(s) into the ssh-agent
                # so paramiko can authenticate to the jump host through the agent
                # (it has no interactive passphrase prompt of its own for hops).
                # Keyring-gated, mirroring Connection._preload_keys_into_agent:
                # only keys whose passphrase the user stored are unlocked; the
                # agent is never disabled.
                try:
                    from ..askpass_utils import ensure_key_in_agent, lookup_passphrase

                    for ident in hop_identities:
                        if lookup_passphrase(ident):
                            ensure_key_in_agent(ident, force=True)
                            logger.debug(
                                "Preloaded ProxyJump host %s key into ssh-agent: %s",
                                hop["alias"],
                                ident,
                            )
                except Exception as exc:  # pragma: no cover - best effort
                    logger.debug(
                        "ProxyJump host %s key preload failed: %s", hop["alias"], exc
                    )

                # The hop authenticates with ITS OWN config, not the target's:
                # inheriting the target's pinned keyfile would offer the wrong key
                # to the bastion, and inheriting a password-mode target's disabled
                # agent would block key auth to a key-only hop. Start from agent +
                # default discovery and let the hop's config narrow it.
                # The agent is always left enabled: sshpilot's askpass flow loads
                # passphrase-protected keys into the ssh-agent, so disabling it
                # (e.g. for IdentityAgent none) would break auth for exactly those
                # keys. We never turn the agent off.
                hop_allow_agent = True
                hop_look_for_keys = True
                hop_key_filename: Optional[Any] = None
                hop_passphrase: Optional[str] = None

                if hop_identities:
                    hop_key_filename = hop_identities
                    # Don't fan out default ~/.ssh/id_* keys; configured IdentityFile
                    # entries (plus the agent when enabled) mirror native ssh.
                    hop_look_for_keys = False

                    if not hop_passphrase and self._connection_manager is not None and hasattr(
                        self._connection_manager, "get_key_passphrase"
                    ):
                        for ident in hop_identities:
                            try:
                                stored = self._connection_manager.get_key_passphrase(ident)
                            except Exception as exc:  # pragma: no cover - defensive
                                logger.debug(
                                    "Failed to load key passphrase for ProxyJump host %s (%s): %s",
                                    hop["alias"],
                                    ident,
                                    exc,
                                )
                                continue
                            if stored:
                                hop_passphrase = stored
                                break

                hop_kwargs: Dict[str, Any] = {
                    "hostname": hop["hostname"],
                    "username": hop["username"],
                    "port": hop["port"],
                    "allow_agent": hop_allow_agent,
                    "look_for_keys": hop_look_for_keys,
                }

                # Bound the handshake the same way the target connection does so a
                # half-open jump host can't hang the worker thread.
                hop_timeout = connect_timeout if (connect_timeout and connect_timeout > 0) else 30
                hop_kwargs["timeout"] = hop_timeout
                hop_kwargs["banner_timeout"] = hop_timeout
                hop_kwargs["auth_timeout"] = hop_timeout

                if upstream_sock is not None:
                    hop_kwargs["sock"] = upstream_sock

                if hop_key_filename:
                    hop_kwargs["key_filename"] = hop_key_filename
                if hop_passphrase:
                    hop_kwargs["passphrase"] = hop_passphrase

                hop_password: Optional[str] = None
                if self._connection_manager is not None and hasattr(
                    self._connection_manager, "get_password"
                ):
                    try:
                        hop_password = self._connection_manager.get_password(
                            hop["alias"], hop["username"]
                        )
                        if not hop_password:
                            hop_password = self._connection_manager.get_password(
                                hop["hostname"], hop["username"]
                            )
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug(
                            "Password lookup for ProxyJump host %s failed: %s",
                            hop["alias"],
                            exc,
                        )

                if hop_password:
                    hop_kwargs["password"] = hop_password

                jump_client.connect(**hop_kwargs)
                jump_clients.append(jump_client)

                transport = jump_client.get_transport()
                if transport is None:
                    raise RuntimeError(
                        f"ProxyJump host {hop['alias']} did not provide a transport"
                    )

                if index + 1 < len(resolved_hops):
                    next_hop = resolved_hops[index + 1]
                    dest = (next_hop["hostname"], next_hop["port"])
                else:
                    dest = (resolved_host, resolved_port)

                upstream_sock = transport.open_channel(
                    "direct-tcpip",
                    dest,
                    ("127.0.0.1", 0),
                )

                logger.debug(
                    "ProxyJump hop %s connected to %s:%s, chaining towards %s:%s",
                    hop["alias"],
                    hop["hostname"],
                    hop["port"],
                    dest[0],
                    dest[1],
                )

        except Exception:
            # Close any hops we already opened so a partial chain doesn't leak
            # sockets/threads, then let the caller surface the failure.
            for opened in reversed(jump_clients):
                try:
                    opened.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            raise

        if upstream_sock is None:
            raise RuntimeError("ProxyJump chain failed to produce a socket")

        return upstream_sock, jump_clients

    def _connect_impl(self) -> None:
        self._stop_keepalive_worker()
        client = paramiko.SSHClient()

        try:
            client.load_system_host_keys()
        except Exception as exc:
            logger.debug("Unable to load system host keys: %s", exc)

        ssh_cfg: Dict[str, Any] = {}
        file_manager_cfg: Dict[str, Any] = {}
        cfg = None
        try:
            from ..config import Config  # Lazy import to avoid circular dependency

            cfg = Config()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("Failed to initialise configuration for file manager: %s", exc)

        if self._ssh_config is not None:
            ssh_cfg = dict(self._ssh_config)
        elif cfg is not None:
            try:
                ssh_cfg = cfg.get_ssh_config() or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load SSH configuration for file manager: %s", exc)
                ssh_cfg = {}
        else:
            ssh_cfg = {}

        if cfg is not None and hasattr(cfg, 'get_file_manager_config'):
            try:
                file_manager_cfg = cfg.get_file_manager_config() or {}
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load file manager configuration: %s", exc)
                file_manager_cfg = {}
        elif isinstance(self._ssh_config, dict):
            potential_cfg = self._ssh_config.get('file_manager')  # type: ignore[attr-defined]
            if isinstance(potential_cfg, dict):
                file_manager_cfg = dict(potential_cfg)

        def _coerce_int(value: Any, default: int) -> int:
            try:
                coerced = int(str(value))
                return coerced if coerced > 0 else default
            except (TypeError, ValueError):
                return default

        keepalive_interval = max(0, _coerce_int(ssh_cfg.get("keepalive_interval"), 0))
        keepalive_count_max = max(0, _coerce_int(ssh_cfg.get("keepalive_count_max"), 0))

        if isinstance(file_manager_cfg, dict):
            fm_interval = file_manager_cfg.get("sftp_keepalive_interval")
            if isinstance(fm_interval, int) and fm_interval >= 0:
                keepalive_interval = fm_interval

            fm_count = file_manager_cfg.get("sftp_keepalive_count_max")
            if isinstance(fm_count, int) and fm_count >= 0:
                keepalive_count_max = fm_count

            fm_timeout = file_manager_cfg.get("sftp_connect_timeout")
        else:
            fm_timeout = None

        connect_timeout: Optional[int]
        if isinstance(fm_timeout, int) and fm_timeout > 0:
            connect_timeout = fm_timeout
        else:
            connect_timeout = None

        connection_timeout_override = max(0, _coerce_int(ssh_cfg.get("connection_timeout"), 0))
        if connect_timeout is None and connection_timeout_override > 0:
            connect_timeout = connection_timeout_override

        with self._lock:
            self._keepalive_interval = keepalive_interval
            self._keepalive_count_max = keepalive_count_max
            self._keepalive_failures = 0

        strict_host = str(ssh_cfg.get("strict_host_key_checking", "") or "").strip()
        auto_add = bool(ssh_cfg.get("auto_add_host_keys", True))
        policy = self._select_host_key_policy(strict_host, auto_add)
        client.set_missing_host_key_policy(policy)

        known_hosts_path = None
        if self._connection_manager is not None:
            known_hosts_path = getattr(self._connection_manager, "known_hosts_path", None)

        # NOTE: the app-level known_hosts is loaded later, after effective_cfg is
        # resolved, so a per-host UserKnownHostsFile can take precedence. See the
        # per-host override block below.

        password = self._password or None
        connection = self._connection
        if not password and connection is not None:
            password = getattr(connection, "password", None) or None

        if not password and self._connection_manager is not None:
            lookup_user = self._username
            if connection is not None:
                lookup_user = getattr(connection, "username", None) or self._username

            # Try multiple host identifiers to match storage logic
            # Storage uses: hostname -> host -> nickname
            # We should try all of them to ensure we find the password
            lookup_hosts = []
            if connection is not None:
                # Collect all possible host identifiers
                hostname = getattr(connection, "hostname", None)
                host = getattr(connection, "host", None)
                nickname = getattr(connection, "nickname", None)
                
                # Add in storage priority order: hostname -> host -> nickname
                if hostname:
                    lookup_hosts.append(hostname)
                if host and host not in lookup_hosts:
                    lookup_hosts.append(host)
                if nickname and nickname not in lookup_hosts:
                    lookup_hosts.append(nickname)
            
            # Fallback to self._host if no connection or no identifiers found
            if not lookup_hosts:
                lookup_hosts = [self._host]
            
            logger.debug(
                "File manager: Attempting password lookup for %s@%s (trying identifiers: %s)",
                lookup_user,
                self._host,
                lookup_hosts
            )
            
            # Try each identifier until we find a password
            for lookup_host in lookup_hosts:
                try:
                    retrieved = self._connection_manager.get_password(lookup_host, lookup_user)
                    if retrieved:
                        logger.debug(
                            "File manager: Password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                        password = retrieved
                        break
                    else:
                        logger.debug(
                            "File manager: No password found for %s@%s using identifier '%s'",
                            lookup_user,
                            lookup_host,
                            lookup_host
                        )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(
                        "Password lookup failed for %s@%s (identifier '%s'): %s",
                        lookup_user,
                        lookup_host,
                        lookup_host,
                        exc
                    )

        allow_agent = True
        look_for_keys = True
        key_filename: Optional[str] = None
        passphrase: Optional[str] = None
        auth_method = 0
        key_mode = 0

        logger.debug("File manager: connection object is %s", "None" if connection is None else "present")
        if connection is not None:
            try:
                auth_method = int(getattr(connection, "auth_method", 0) or 0)
            except Exception:
                auth_method = 0

            try:
                key_mode = int(getattr(connection, "key_select_mode", 0) or 0)
            except Exception:
                key_mode = 0

            raw_keyfile = getattr(connection, "keyfile", "") or ""
            keyfile = raw_keyfile.strip()
            if keyfile.lower().startswith("select key file"):
                keyfile = ""
            
            logger.debug("File manager: connection nickname='%s', hostname='%s', key_mode=%d, keyfile='%s', auth_method=%d", 
                        getattr(connection, 'nickname', 'None'), 
                        getattr(connection, 'hostname', 'None'), 
                        key_mode, keyfile, auth_method)
        else:
            logger.debug("File manager: No connection object provided")

        identity_agent_disabled = False
        if connection is not None:
            identity_agent_disabled = bool(
                getattr(connection, "identity_agent_disabled", False)
            )

        if (
            connection is not None
            and key_mode in (1, 2)
            and keyfile
            and os.path.isfile(keyfile)
        ):
                key_filename = keyfile
                look_for_keys = False
                logger.debug("File manager: Using specific key file: %s", keyfile)
                # Prepare key for connection (add to ssh-agent if needed)
                key_prepared = False
                if identity_agent_disabled:
                    logger.debug(
                        "File manager: IdentityAgent disabled; skipping key preparation"
                    )
                elif (
                    self._connection_manager is not None
                    and hasattr(self._connection_manager, "prepare_key_for_connection")
                ):
                    try:
                        key_prepared = self._connection_manager.prepare_key_for_connection(keyfile)
                        if key_prepared:
                            logger.debug("Successfully prepared key for file manager: %s", keyfile)
                        else:
                            logger.warning("Failed to prepare key for file manager: %s", keyfile)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.warning("Error preparing key for file manager %s: %s", keyfile, exc)
                        key_prepared = False
                
                # If key preparation failed, we still try to connect but may prompt for passphrase
                if not key_prepared:
                    logger.info("Key preparation failed for %s, connection may prompt for passphrase", keyfile)

                passphrase = getattr(connection, "key_passphrase", None) or None
                if (
                    not passphrase
                    and self._connection_manager is not None
                    and hasattr(self._connection_manager, "get_key_passphrase")
                ):
                    try:
                        passphrase = self._connection_manager.get_key_passphrase(keyfile)
                    except Exception as exc:  # pragma: no cover - defensive
                        logger.debug("Failed to load key passphrase for %s: %s", keyfile, exc)

                # Only disable agent if explicitly configured to do so
                if getattr(connection, "pubkey_auth_no", False):
                    allow_agent = False
                    look_for_keys = False
                elif key_prepared:
                    # If we successfully prepared a key, ensure agent is enabled
                    allow_agent = True
                    look_for_keys = True
                    logger.debug("Key was prepared successfully, enabling SSH agent usage")

                # Only disable agent for password auth method
                if auth_method == 1:
                    allow_agent = False
                    look_for_keys = False

        if connection is not None:
            if auth_method == 1:
                allow_agent = False
                look_for_keys = False
            if getattr(connection, "pubkey_auth_no", False):
                allow_agent = False
                look_for_keys = False

        effective_cfg: Dict[str, Any] = {}
        proxy_command: str = ""
        proxy_jump: List[str] = []

        target_alias: Optional[str] = None
        config_override: Optional[str] = None

        if connection is not None:
            try:
                proxy_command = str(getattr(connection, "proxy_command", "") or "")
            except Exception:
                proxy_command = ""
            try:
                raw_jump = getattr(connection, "proxy_jump", []) or []
            except Exception:
                raw_jump = []
            if isinstance(raw_jump, str):
                proxy_jump = [token.strip() for token in raw_jump.split(",") if token.strip()]
            elif isinstance(raw_jump, (list, tuple, set)):
                proxy_jump = [str(token).strip() for token in raw_jump if str(token).strip()]

            source_path = str(getattr(connection, "source", "") or "")
            if source_path:
                expanded_source = os.path.abspath(
                    os.path.expanduser(os.path.expandvars(source_path))
                )
                if os.path.exists(expanded_source):
                    config_override = expanded_source

            if not config_override and getattr(connection, "isolated_config", False):
                root_candidate = str(getattr(connection, "config_root", "") or "")
                if root_candidate:
                    expanded_root = os.path.abspath(
                        os.path.expanduser(os.path.expandvars(root_candidate))
                    )
                    if os.path.exists(expanded_root):
                        config_override = expanded_root

            target_alias = (
                getattr(connection, "nickname", "")
                or getattr(connection, "hostname", "")
                or getattr(connection, "host", "")
                or None
            )

        if not target_alias:
            target_alias = self._host

        alias_for_config: Optional[str] = None

        if target_alias:
            try:
                from ..ssh_config_utils import get_effective_ssh_config

                effective_cfg = get_effective_ssh_config(
                    target_alias, config_file=config_override
                )
                alias_for_config = target_alias

            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "Failed to resolve effective SSH config for %s: %s",
                    target_alias,
                    exc,
                )
                effective_cfg = {}

        if not proxy_command:
            proxy_command = str(effective_cfg.get("proxycommand", "") or "")

        if not proxy_jump:
            raw_cfg_jump = effective_cfg.get("proxyjump", [])
            if isinstance(raw_cfg_jump, str):
                proxy_jump = [token.strip() for token in re.split(r"[\s,]+", raw_cfg_jump) if token.strip()]
            elif isinstance(raw_cfg_jump, (list, tuple, set)):
                proxy_jump = [
                    str(token).strip()
                    for token in raw_cfg_jump
                    if str(token).strip()
                ]

        alias_for_substitution = alias_for_config or target_alias or self._host

        def _coerce_port(value: Any, default: int) -> int:
            try:
                return int(str(value))
            except (TypeError, ValueError):
                return default

        resolved_host = str(effective_cfg.get("hostname", self._host) or self._host)
        resolved_port = _coerce_port(effective_cfg.get("port", self._port), self._port)
        resolved_username = str(effective_cfg.get("user", self._username) or self._username)


        def _expand_proxy_tokens(raw_command: str) -> str:
            if not raw_command:
                return raw_command

            substitution_host = str(resolved_host)
            substitution_port = str(resolved_port)
            substitution_user = str(resolved_username) if resolved_username else ""

            substitution_alias = str(alias_for_substitution) if alias_for_substitution else substitution_host

            token_pattern = re.compile(r"%(?:%|h|p|r|n)")

            def _replace(match: re.Match[str]) -> str:
                token = match.group(0)
                if token == "%%":
                    return "%"
                if token == "%h":
                    return substitution_host
                if token == "%p":
                    return substitution_port
                if token == "%r":
                    return substitution_user
                if token == "%n":
                    return substitution_alias
                return token

            return token_pattern.sub(_replace, raw_command)


        # Per-host host-key policy and known-hosts from the effective SSH config
        # take precedence over the app-level defaults, matching native
        # ``ssh -F config``. Applied here (after effective_cfg, before connecting)
        # so the target client and any jump hops use the resolved values.
        eff_strict = str(effective_cfg.get("stricthostkeychecking", "") or "").strip()
        if eff_strict:
            policy = self._select_host_key_policy(eff_strict, auto_add)
            client.set_missing_host_key_policy(policy)

        eff_known_hosts = effective_cfg.get("userknownhostsfile")
        if isinstance(eff_known_hosts, (list, tuple)):
            eff_known_candidates = list(eff_known_hosts)
        elif eff_known_hosts:
            eff_known_candidates = re.split(r"\s+", str(eff_known_hosts).strip())
        else:
            eff_known_candidates = []
        for candidate in eff_known_candidates:
            expanded_kh = os.path.expanduser(os.path.expandvars(str(candidate)))
            if os.path.exists(expanded_kh):
                known_hosts_path = expanded_kh
                break

        if known_hosts_path:
            try:
                if os.path.exists(known_hosts_path):
                    client.load_host_keys(known_hosts_path)
                else:
                    logger.debug("Known hosts file not found at %s", known_hosts_path)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("Failed to load known hosts from %s: %s", known_hosts_path, exc)

        proxy_sock: Optional[Any] = None
        jump_clients: List[paramiko.SSHClient] = []
        proxy_command = proxy_command.strip()
        if proxy_command:
            try:
                from paramiko.proxy import ProxyCommand as ParamikoProxyCommand

                expanded_command = _expand_proxy_tokens(proxy_command)
                proxy_sock = ParamikoProxyCommand(expanded_command)
                logger.debug(
                    "File manager: using ProxyCommand '%s' (expanded from '%s')",
                    expanded_command,
                    proxy_command,
                )

            except Exception as exc:
                # The target is only reachable via the ProxyCommand; a direct
                # fallback would hit the wrong host or fail confusingly. Surface
                # the real reason instead (mirrors the ProxyJump handling below).
                logger.warning("Failed to set up ProxyCommand '%s': %s", proxy_command, exc)
                raise paramiko.SSHException(
                    f"Could not connect through ProxyCommand: {exc}"
                ) from exc
        elif proxy_jump:
            try:
                proxy_sock, jump_clients = self._create_proxy_jump_socket(
                    proxy_jump,
                    config_override=config_override,
                    policy=policy,
                    known_hosts_path=known_hosts_path,
                    allow_agent=allow_agent,
                    look_for_keys=look_for_keys,
                    key_filename=key_filename,
                    passphrase=passphrase,
                    resolved_host=resolved_host,
                    resolved_port=resolved_port,
                    base_username=resolved_username,
                    connect_timeout=connect_timeout,
                )
                logger.debug(
                    "File manager: using Paramiko ProxyJump chain via %s",
                    ", ".join(proxy_jump),
                )
            except Exception as exc:
                # The target is only reachable via the jump host(s); falling back
                # to a direct connection would hit the wrong host or fail with a
                # confusing error. Surface the real reason instead.
                logger.warning("Failed to set up ProxyJump chain %s: %s", proxy_jump, exc)
                raise paramiko.SSHException(
                    f"Could not connect through jump host(s) {', '.join(proxy_jump)}: {exc}"
                ) from exc
        else:
            jump_clients = []

        if not proxy_jump:
            jump_clients = []

        # Honor the TARGET host's own IdentityFile/IdentitiesOnly from ssh -G,
        # the same way native ssh does. When the host pins itself with
        # ``IdentitiesOnly yes``, restrict to its configured identity files (not
        # default ~/.ssh/id_*), but keep the agent enabled so loaded keys work.
        # Done after the jump chain and in automatic key mode only.
        target_identities_only = (
            str(effective_cfg.get("identitiesonly", "")).strip().lower() == "yes"
        )
        if key_filename is None and target_identities_only:
            target_identities: List[str] = []
            if connection is not None and hasattr(
                connection, "collect_identity_file_candidates"
            ):
                try:
                    target_identities = (
                        connection.collect_identity_file_candidates(effective_cfg) or []
                    )
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Failed to resolve target identity files: %s", exc)
                    target_identities = []
            if not target_identities:
                target_identities = _existing_identity_files(
                    effective_cfg.get("identityfile")
                )
            # IdentitiesOnly yes => configured identity files only (agent still on).
            look_for_keys = False
            if target_identities:
                key_filename = target_identities
            logger.debug(
                "File manager: target IdentitiesOnly=yes, using identities: %s",
                target_identities,
            )

        connect_kwargs: Dict[str, Any] = {
            "hostname": resolved_host,
            "username": resolved_username,
            "port": resolved_port,
            "allow_agent": allow_agent,
            "look_for_keys": look_for_keys,
        }

        # Always bound the handshake. A server that accepts the TCP connection
        # but then stalls or slow-closes during key exchange (e.g. an expired
        # account, or a host that refuses post-quantum-less clients) would
        # otherwise block the worker thread forever: the loading toast never
        # clears and quit deadlocks. paramiko's ``timeout`` covers the socket
        # connect, ``banner_timeout`` the protocol banner, and ``auth_timeout``
        # authentication; set all three, defaulting when unconfigured.
        effective_timeout = connect_timeout if (connect_timeout and connect_timeout > 0) else 30
        connect_kwargs["timeout"] = effective_timeout
        connect_kwargs["banner_timeout"] = effective_timeout
        connect_kwargs["auth_timeout"] = effective_timeout

        if password:
            connect_kwargs["password"] = password

        if key_filename:
            connect_kwargs["key_filename"] = key_filename

        if passphrase:
            connect_kwargs["passphrase"] = passphrase

        if proxy_sock is not None:
            connect_kwargs["sock"] = proxy_sock

        # Preload the target host's passphrase-protected key(s) into the ssh-agent
        # so paramiko can authenticate via the agent (covers automatic key mode;
        # the explicit-key path above already prepared its key). Keyring-gated and
        # a no-op for password auth / IdentityAgent-pinned hosts — the canonical
        # native logic; the agent is never disabled. We run on a worker thread, so
        # askpass/keyring access is safe here.
        if connection is not None and hasattr(connection, "_preload_keys_into_agent"):
            try:
                connection._preload_keys_into_agent(cfg)
            except Exception as exc:  # pragma: no cover - best effort
                logger.debug("Target key preload failed: %s", exc)

        transport: Optional[Any] = None
        try:
            client.connect(**connect_kwargs)
            try:
                sftp = client.open_sftp()
            except paramiko.SSHException as sftp_exc:
                # TCP/auth succeeded but the SFTP subsystem could not be
                # started (e.g. the remote sshd has no 'Subsystem sftp' or
                # sftp-server is not installed). Surface a clear message.
                from ..scp_utils import classify_sftp_error, SFTP_UNAVAILABLE_MESSAGE

                friendly = classify_sftp_error(str(sftp_exc)) or SFTP_UNAVAILABLE_MESSAGE
                logger.error(
                    "Failed to open SFTP session (remote SFTP server may be missing): %s",
                    sftp_exc,
                )
                raise paramiko.SSHException(friendly) from sftp_exc
            transport = client.get_transport()
            interval = 0
            with self._lock:
                interval = self._keepalive_interval
            if (
                transport is not None
                and hasattr(transport, "set_keepalive")
                and interval > 0
            ):
                try:
                    transport.set_keepalive(interval)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug("Failed to configure SSH keepalive: %s", exc)
        except paramiko.AuthenticationException as auth_exc:
            # Authentication failed - close connections and request password from UI
            if proxy_sock is not None:
                try:
                    proxy_sock.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            if proxy_jump:
                for jump_client in jump_clients:
                    try:
                        jump_client.close()
                    except Exception:  # pragma: no cover - defensive cleanup
                        pass
            if client is not None:
                try:
                    client.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            
            # Emit a special signal for authentication failure that will trigger password dialog
            logger.debug("Authentication failed, requesting password from user")
            self.emit("authentication-required", str(auth_exc))
            return  # Don't raise, let the UI handle it
        except Exception:
            if proxy_sock is not None:
                try:
                    proxy_sock.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    pass
            if proxy_jump:
                for jump_client in jump_clients:
                    try:
                        jump_client.close()
                    except Exception:  # pragma: no cover - defensive cleanup
                        pass

            raise

        with self._lock:
            self._client = client
            self._sftp = sftp
            self._password = password
            self._proxy_sock = proxy_sock
            self._jump_clients = jump_clients

        self._start_keepalive_worker()


    # -- public operations ----------------------------------------------

    def listdir(self, path: str) -> None:
        logger.debug(f"AsyncSFTPManager.listdir called for path: {path}")
        def _impl() -> Tuple[str, List[FileEntry]]:
            entries: List[FileEntry] = []
            
            # Serialize all SFTP operations
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                
                # Expand ~ to user's home directory
                expanded_path = path
                if path == "~" or path.startswith("~/"):
                    # Use the most reliable method to get home directory
                    # The SFTP normalize method with "." should give us the initial directory
                    # which is typically the user's home directory
                    try:
                        if path == "~":
                            # For just ~, resolve to the absolute home directory
                            expanded_path = self._sftp.normalize(".")
                        else:
                            # For ~/subpath, we need to resolve the home directory first
                            # Try to get the actual home directory path
                            home_path = self._sftp.normalize(".")
                            expanded_path = home_path + path[1:]  # Replace ~ with home_path
                    except Exception:
                        # If normalize fails, try common patterns
                        try:
                            possible_homes = [
                                f"/home/{self._username}",
                                f"/Users/{self._username}",  # macOS
                                f"/export/home/{self._username}",  # Solaris
                            ]
                            for possible_home in possible_homes:
                                try:
                                    # Test if this directory exists
                                    self._sftp.listdir_attr(possible_home)
                                    if path == "~":
                                        expanded_path = possible_home
                                    else:
                                        expanded_path = possible_home + path[1:]
                                    break
                                except Exception:
                                    continue
                            else:
                                # Final fallback
                                expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                        except Exception:
                            # Ultimate fallback
                            expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                
                for attr in self._sftp.listdir_attr(expanded_path):
                    is_dir = stat_isdir(attr)
                    item_count = None
                    
                    # Count items in directory
                    if is_dir:
                        try:
                            dir_path = os.path.join(expanded_path, attr.filename)
                            dir_attrs = self._sftp.listdir_attr(dir_path)
                            item_count = len(dir_attrs)
                        except Exception:
                            # If we can't read the directory, set count to None
                            item_count = None
                    
                    entries.append(
                        FileEntry(
                            name=attr.filename,
                            is_dir=is_dir,
                            size=attr.st_size,
                            modified=attr.st_mtime,
                            item_count=item_count,
                        )
                    )
            return expanded_path, entries

        self._submit(
            _impl,
            on_success=lambda result: (logger.debug(f"listdir success for {result[0]}, emitting directory-loaded with {len(result[1])} entries"), self.emit("directory-loaded", *result))[1],
            on_error=lambda exc: (logger.debug(f"listdir error: {exc}"), self.emit("operation-error", str(exc)))[1],
        )

    def mkdir(self, path: str) -> Future:
        logger.debug(f"Creating directory: {path}")
        def _impl() -> None:
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                self._sftp.mkdir(path)
        # Don't call listdir from callback - let the UI handle refresh
        return self._submit(_impl)

    def touch(self, path: str) -> Future:
        """Create an empty remote file. Raises FileExistsError if it already
        exists (so the caller can warn instead of truncating)."""
        logger.debug(f"Creating empty file: {path}")

        def _impl() -> None:
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                if _sftp_path_exists(self._sftp, path):
                    raise FileExistsError(path)
                handle = self._sftp.open(path, "w")
                handle.close()

        return self._submit(_impl)

    def path_exists(self, path: str) -> Future:
        """Return a future that resolves to whether *path* exists remotely."""

        def _impl() -> bool:
            # Serialize SFTP operations
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                return _sftp_path_exists(self._sftp, path)

        return self._submit(_impl)

    def remove(self, path: str) -> Future:
        def _remove_recursive(target_path: str) -> None:
            """Recursively remove a file or directory synchronously."""
            logger.info(f"Attempting to remove: {target_path}")
            
            # Serialize SFTP operations - try to remove as file first
            try:
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    logger.info(f"Calling _sftp.remove('{target_path}')")
                    self._sftp.remove(target_path)
                logger.info(f"_sftp.remove() returned successfully for {target_path}")
                logger.info(f"Successfully removed file {target_path}")
                return
            except (IOError, OSError) as file_error:
                # If remove() fails, it's likely a directory - remove contents recursively
                logger.info(f"Path {target_path} is a directory (remove failed: {file_error}), removing recursively")
            except paramiko.ssh_exception.SSHException as ssh_error:
                # Handle paramiko-specific exceptions
                logger.error(f"SSH error calling remove() on {target_path}: {ssh_error}", exc_info=True)
                raise
            except Exception as unexpected_error:
                # Catch any other exceptions from remove()
                logger.error(f"Unexpected error calling remove() on {target_path}: {unexpected_error}", exc_info=True)
                raise
            
            # Handle directory removal
            try:
                logger.info(f"Calling listdir on {target_path}")
                
                # Serialize SFTP operations for directory operations
                entries = None
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    
                    # First check if directory still exists (might have been deleted by parallel operation)
                    try:
                        stat_result = self._sftp.stat(target_path)
                        if not stat.S_ISDIR(stat_result.st_mode):
                            logger.info(f"Path {target_path} is not a directory, skipping recursive removal")
                            return
                    except (IOError, OSError) as stat_error:
                        error_code = getattr(stat_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.info(f"Directory {target_path} no longer exists (deleted by parallel operation?), skipping")
                            return
                        # If stat fails for other reasons, continue with listdir attempt
                        logger.debug(f"stat() failed for {target_path}: {stat_error}, continuing with listdir")
                    
                    # Try to get directory contents
                    try:
                        logger.info(f"About to call _sftp.listdir('{target_path}')")
                        entries = self._sftp.listdir(target_path)
                        logger.info(f"listdir call completed, checking result...")
                        if entries is not None:
                            logger.debug(f"listdir returned {len(entries)} entries for {target_path}")
                            if entries:
                                logger.debug(f"Entries to delete: {entries[:5]}{'...' if len(entries) > 5 else ''}")
                            else:
                                logger.debug(f"Directory {target_path} is empty")
                        else:
                            logger.error(f"listdir returned None for {target_path}")
                    except IOError as listdir_io_error:
                        # Check if directory no longer exists (might have been deleted by parallel operation)
                        error_code = getattr(listdir_io_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} no longer exists, skipping")
                            return
                        logger.error(f"listdir IOError for {target_path}: {listdir_io_error}", exc_info=True)
                        raise
                    except OSError as listdir_os_error:
                        error_code = getattr(listdir_os_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} no longer exists, skipping")
                            return
                        logger.error(f"listdir OSError for {target_path}: {listdir_os_error}", exc_info=True)
                        raise
                    except Exception as listdir_error:
                        logger.error(f"listdir failed for {target_path}: {listdir_error}", exc_info=True)
                        raise
                
                if entries is None:
                    logger.error(f"listdir returned None for {target_path}")
                    raise RuntimeError(f"listdir returned None for {target_path}")
                
                logger.debug(f"Directory {target_path} contains {len(entries)} entries")
                for entry in entries:
                    entry_path = posixpath.join(target_path, entry)
                    logger.debug(f"Recursively removing entry: {entry_path}")
                    _remove_recursive(entry_path)  # Recursive call (lock released, will re-acquire)
                
                # After all children are removed, remove the directory itself
                logger.debug(f"Removing empty directory: {target_path}")
                with self._lock:
                    if self._sftp is None:
                        raise RuntimeError("SFTP connection is not available")
                    try:
                        self._sftp.rmdir(target_path)
                        logger.debug(f"Successfully removed directory {target_path}")
                    except IOError as rmdir_error:
                        # Check if directory was already deleted
                        error_code = getattr(rmdir_error, "errno", None)
                        if error_code in {errno.ENOENT, errno.EINVAL}:
                            logger.debug(f"Directory {target_path} already removed")
                            return
                        raise
            except (IOError, OSError) as dir_error:
                # If listdir or rmdir fails, log and re-raise
                logger.error(f"Failed to remove directory {target_path}: {dir_error}", exc_info=True)
                raise
            except Exception as e:
                # Catch any other unexpected errors from directory operations
                logger.error(f"Unexpected error removing directory {target_path}: {e}", exc_info=True)
                raise

        def _impl() -> None:
            try:
                logger.info(f"Starting remove operation for {path}")
                _remove_recursive(path)
                logger.info(f"Successfully completed remove operation for {path}")
            except Exception as e:
                logger.error(f"Error in remove operation for {path}: {e}", exc_info=True)
                # Re-raise so the future will have the exception
                raise

        parent = os.path.dirname(path) or "/"
        return self._submit(_impl)  # Don't call listdir from callback - let the UI handle refresh

    def rename(self, source: str, target: str) -> Future:
        logger.debug(f"Renaming {source} to {target}")
        def _impl() -> None:
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available")
                self._sftp.rename(source, target)
        # Don't call listdir from callback - let the UI handle refresh
        return self._submit(_impl)

    def download(self, source: str, destination: pathlib.Path) -> Future:
        try:
            # Ensure parent directory exists, with special handling for portal paths
            parent_dir = destination.parent
            logger.debug(f"Download: ensuring parent directory exists: {parent_dir}")
            
            if not parent_dir.exists():
                logger.debug(f"Download: creating parent directory: {parent_dir}")
                parent_dir.mkdir(parents=True, exist_ok=True)
            else:
                logger.debug(f"Download: parent directory already exists: {parent_dir}")
                
            # Verify we can write to the destination
            if not os.access(str(parent_dir), os.W_OK):
                logger.warning(f"Download: no write access to destination directory: {parent_dir}")
            else:
                logger.debug(f"Download: write access confirmed for: {parent_dir}")
                
        except Exception as e:
            logger.error(f"Download: failed to prepare destination directory {destination.parent}: {e}")
            # Continue anyway - maybe the directory already exists or will be created by the SFTP operation
        
        operation_id = self._next_operation_id("download")

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting download…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if self._is_cancelled(operation_id):
                    raise TransferCancelledException("Download was cancelled")

                # Emit raw bytes first so the dialog can do its own speed/ETA.
                self.emit("progress-bytes", transferred, total)
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Downloaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Downloaded {transferred_size}")
            
            try:
                logger.debug(f"Download: starting SFTP get from {source} to {destination}")
                # Serialize SFTP operations - must hold lock during file transfer
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed before download could start")
                    self._sftp.get(source, str(destination), callback=progress_callback)
                
                # Only emit completion if not cancelled
                if not self._is_cancelled(operation_id):
                    # Verify the file was actually created
                    if destination.exists():
                        file_size = destination.stat().st_size
                        logger.info(f"Download: successfully saved {source} to {destination} ({file_size} bytes)")
                    else:
                        logger.error(f"Download: file not found after transfer: {destination}")
                    self.emit("progress", 1.0, "Download complete")
            except TransferCancelledException:
                # Clean up partial download on cancellation
                try:
                    if destination.exists():
                        destination.unlink()
                        logger.debug("Cleaned up partial download: %s", destination)
                except Exception as cleanup_exc:
                    logger.debug(
                        "Partial download cleanup failed for %s: %s",
                        destination, cleanup_exc,
                    )
                self.emit("progress", 0.0, "Download cancelled")
                logger.info("Download cancelled: %s", source)
                # paramiko's SFTP channel is left in a broken state after a
                # mid-stream cancel. Recreate it so subsequent listdir/refresh
                # ops don't hang.
                try:
                    self._reset_sftp_channel()
                except Exception as reset_exc:
                    logger.error(
                        "SFTP reset after download cancel failed: %s", reset_exc
                    )
                # Re-raise so the Future reflects the cancellation.
                raise
            except Exception as e:
                # paramiko raises IOError / OSError for permission denied,
                # connection drops, etc. Surface them via the toast layer
                # instead of silently logging.
                logger.error(f"Download failed for {source}: {e}", exc_info=True)
                self.emit(
                    "operation-error",
                    f"Download failed: {os.path.basename(source)}: {e}",
                )
                raise
            finally:
                # Clean up the cancellation flag
                self._discard_cancellation_flag(operation_id)

        future = self._submit(_impl)
        
        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            logger.debug("Cancelling download operation %s", operation_id)
            self._mark_cancelled(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup

        return future

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = self._next_operation_id("upload")
        
        def _impl() -> None:
            # Check connection before starting
            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection is not available. Connection may have been closed.")
            
            self.emit("progress", 0.0, "Starting upload…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if self._is_cancelled(operation_id):
                    raise TransferCancelledException("Upload was cancelled")

                # Emit raw bytes first so the dialog can do its own speed/ETA.
                self.emit("progress-bytes", transferred, total)
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Uploaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Uploaded {transferred_size}")
            
            try:
                # Re-check connection before actual upload and serialize SFTP operations
                # SFTP connections are not thread-safe - must serialize file operations
                with self._lock:
                    if self._sftp is None:
                        raise IOError("SFTP connection closed before upload could start")
                    
                    # Perform the actual upload while holding the lock to serialize operations
                    self._sftp.put(str(source), destination, callback=progress_callback)
                
                # Only emit completion if not cancelled
                if not self._is_cancelled(operation_id):
                    logger.info(
                        "Upload complete: %s → %s",
                        source, destination,
                    )
                    self.emit("progress", 1.0, "Upload complete")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Upload cancelled")
                logger.info("Upload cancelled: %s", destination)
                # paramiko's SFTP channel is left in a broken state after a
                # mid-stream cancel — every subsequent op on this channel
                # would hang. Recreate the channel first so cleanup + future
                # operations work.
                try:
                    self._reset_sftp_channel()
                except Exception as reset_exc:
                    logger.error(
                        "SFTP reset after upload cancel failed: %s", reset_exc
                    )
                # Symmetry with download cancellation: delete the partial
                # remote file so cancel = nothing left behind on either side.
                self._remove_remote_best_effort(
                    destination, context="cancelled upload"
                )
                # Re-raise so the Future reflects the cancellation; otherwise
                # _attach_refresh would treat this as success and highlight
                # a file that doesn't exist on the remote.
                raise
            except Exception as e:
                # Re-raise so the future surfaces the exception, but also fire
                # operation-error so the UI shows a toast — otherwise users
                # only see the progress dialog vanish with no explanation.
                logger.error(f"Upload failed for {destination}: {e}", exc_info=True)
                self.emit(
                    "operation-error",
                    f"Upload failed: {os.path.basename(destination)}: {e}",
                )
                raise
            finally:
                # Clean up the cancellation flag
                self._discard_cancellation_flag(operation_id)

        future = self._submit(_impl)

        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            logger.debug("Cancelling upload operation %s", operation_id)
            self._mark_cancelled(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup

        return future

    # Helpers for directory recursion – these are intentionally simplistic
    # and rely on Paramiko's high level API.

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        operation_id = self._next_operation_id("download_dir")
        # Holder so the cancel cleanup knows which file was in flight.
        in_progress = {"local": None}  # type: Dict[str, Optional[str]]

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing download…")

            # Collect files with their sizes in a single pass so we can emit
            # grand-total byte counts and get a meaningful sliding-window
            # speed across file boundaries.
            all_files: List[Tuple[str, str, int]] = []

            def _collect(remote_root: str, local_root: pathlib.Path) -> None:
                local_root.mkdir(parents=True, exist_ok=True)
                for entry in self._sftp.listdir_attr(remote_root):
                    remote_path = os.path.join(remote_root, entry.filename)
                    if stat_isdir(entry):
                        _collect(remote_path, local_root / entry.filename)
                    else:
                        size = int(getattr(entry, "st_size", 0) or 0)
                        all_files.append(
                            (remote_path, str(local_root / entry.filename), size)
                        )

            with self._lock:
                if self._sftp is None:
                    raise IOError("SFTP connection closed during directory download")
                _collect(source, destination)

            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory downloaded (no files)")
                return

            grand_total = sum(size for _, _, size in all_files)
            bytes_done = 0

            try:
                for i, (remote_path, local_path, file_size) in enumerate(all_files):
                    if self._is_cancelled(operation_id):
                        raise TransferCancelledException("Directory download was cancelled")

                    in_progress["local"] = local_path
                    file_progress = i / total_files
                    self.emit("progress", file_progress, f"Downloading {os.path.basename(remote_path)}...")

                    def progress_callback(transferred: int, total: int,
                                          _i=i, _remote=remote_path,
                                          _base=bytes_done) -> None:
                        if self._is_cancelled(operation_id):
                            raise TransferCancelledException("Directory download was cancelled")
                        # Grand-total bytes so sliding-window speed survives
                        # file boundaries.
                        self.emit(
                            "progress-bytes",
                            _base + transferred,
                            grand_total if grand_total > 0 else 0,
                        )
                        if total > 0:
                            file_progress = transferred / total
                            overall_progress = (_i + file_progress) / total_files
                            self.emit(
                                "progress", overall_progress,
                                f"Downloading {os.path.basename(_remote)} ({transferred:,}/{total:,} bytes)",
                            )

                    with self._lock:
                        if self._sftp is None:
                            raise IOError("SFTP connection closed during directory download")
                        self._sftp.get(remote_path, local_path, callback=progress_callback)

                    bytes_done += file_size
                    in_progress["local"] = None

                logger.info(
                    "Directory download complete: %s → %s (%d files)",
                    source, destination, total_files,
                )
                self.emit("progress", 1.0, "Directory downloaded")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Download cancelled")
                logger.info("Directory download cancelled: %s", source)
                try:
                    self._reset_sftp_channel()
                except Exception as reset_exc:
                    logger.error(
                        "SFTP reset after directory download cancel failed: %s",
                        reset_exc,
                    )
                # Symmetry with single-file cancel: drop the partial local file.
                partial = in_progress.get("local")
                if partial:
                    try:
                        partial_path = pathlib.Path(partial)
                        if partial_path.exists():
                            partial_path.unlink()
                            logger.debug("Cleaned up partial download: %s", partial_path)
                    except Exception as cleanup_exc:
                        logger.debug(
                            "Partial download cleanup failed for %s: %s",
                            partial, cleanup_exc,
                        )
                raise
            except Exception as e:
                logger.error(
                    f"Directory download failed for {source}: {e}", exc_info=True
                )
                self.emit(
                    "operation-error",
                    f"Directory download failed: {os.path.basename(source)}: {e}",
                )
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        future = self._submit(_impl)
        original_cancel = future.cancel
        def cancel_with_cleanup():
            logger.debug("Cancelling directory-download operation %s", operation_id)
            self._mark_cancelled(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        return future

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = self._next_operation_id("upload_dir")
        in_progress = {"remote": None}  # type: Dict[str, Optional[str]]

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing upload…")

            # First, collect all files to get total count + create remote dirs.
            all_files = []
            for root, dirs, files in os.walk(source):
                if self._is_cancelled(operation_id):
                    raise TransferCancelledException("Directory upload was cancelled")
                rel_root = os.path.relpath(root, str(source))
                remote_root = (
                    destination if rel_root == "." else os.path.join(destination, rel_root)
                )
                try:
                    with self._lock:
                        if self._sftp is None:
                            raise IOError("SFTP connection closed during directory upload")
                        self._sftp.mkdir(remote_root)
                except IOError as mkdir_exc:
                    # paramiko surfaces SSH_FX_FAILURE for "already exists" as
                    # IOError without a real errno — accept that case silently
                    # but re-raise anything that looks like a real problem
                    # (permission denied, parent missing, disk full, etc.)
                    # rather than hiding it behind a later, more confusing
                    # "no such file" from the subsequent put().
                    err_text = str(mkdir_exc).lower()
                    looks_like_exists = (
                        getattr(mkdir_exc, "errno", None) == errno.EEXIST
                        or "exists" in err_text
                    )
                    if not looks_like_exists:
                        logger.error(
                            "mkdir failed for %s during directory upload: %s",
                            remote_root, mkdir_exc,
                        )
                        raise
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = os.path.join(remote_root, name)
                    try:
                        size = os.path.getsize(local_path)
                    except OSError:
                        size = 0
                    all_files.append((local_path, remote_path, size))

            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory uploaded (no files)")
                return

            grand_total = sum(size for _, _, size in all_files)
            bytes_done = 0

            try:
                for i, (local_path, remote_path, file_size) in enumerate(all_files):
                    if self._is_cancelled(operation_id):
                        raise TransferCancelledException("Directory upload was cancelled")

                    in_progress["remote"] = remote_path
                    file_progress = i / total_files
                    self.emit("progress", file_progress, f"Uploading {os.path.basename(local_path)}...")

                    def progress_callback(transferred: int, total: int,
                                          _i=i, _local=local_path,
                                          _base=bytes_done) -> None:
                        if self._is_cancelled(operation_id):
                            raise TransferCancelledException("Directory upload was cancelled")
                        # Grand-total bytes so sliding-window speed survives
                        # file boundaries.
                        self.emit(
                            "progress-bytes",
                            _base + transferred,
                            grand_total if grand_total > 0 else 0,
                        )
                        if total > 0:
                            file_progress = transferred / total
                            overall_progress = (_i + file_progress) / total_files
                            self.emit(
                                "progress", overall_progress,
                                f"Uploading {os.path.basename(_local)} ({transferred:,}/{total:,} bytes)",
                            )

                    with self._lock:
                        if self._sftp is None:
                            raise IOError("SFTP connection closed during directory upload")
                        self._sftp.put(local_path, remote_path, callback=progress_callback)

                    bytes_done += file_size
                    in_progress["remote"] = None

                logger.info(
                    "Directory upload complete: %s → %s (%d files)",
                    source, destination, total_files,
                )
                self.emit("progress", 1.0, "Directory uploaded")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Upload cancelled")
                logger.info("Directory upload cancelled: %s", source)
                try:
                    self._reset_sftp_channel()
                except Exception as reset_exc:
                    logger.error(
                        "SFTP reset after directory upload cancel failed: %s",
                        reset_exc,
                    )
                # Drop the partial remote file. Previously-completed files in
                # the same batch are intentionally left alone — they are
                # complete, not partial.
                partial = in_progress.get("remote")
                if partial:
                    self._remove_remote_best_effort(
                        partial, context="cancelled directory upload"
                    )
                raise
            except Exception as e:
                logger.error(
                    f"Directory upload failed for {source}: {e}", exc_info=True
                )
                self.emit(
                    "operation-error",
                    f"Directory upload failed: {os.path.basename(str(source))}: {e}",
                )
                raise
            finally:
                self._discard_cancellation_flag(operation_id)

        future = self._submit(_impl)
        original_cancel = future.cancel
        def cancel_with_cleanup():
            logger.debug("Cancelling directory-upload operation %s", operation_id)
            self._mark_cancelled(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        return future
