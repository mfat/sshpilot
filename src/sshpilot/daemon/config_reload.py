"""Daemon-owned monitoring and authoritative connection configuration reload."""

from __future__ import annotations

import copy
import logging
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, FrozenSet, Optional, Protocol

from sshpilot.api.in_process_client import InProcessClient
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.ssh_config_utils import discover_ssh_config_paths

from .command_executor import BoundedCommandExecutor, DeferredCommand

logger = logging.getLogger(__name__)

CONFIGURATION_COMMAND_KEY = "authoritative-connection-configuration"
DEFAULT_CONFIG_RELOAD_DEBOUNCE_SECONDS = 0.2
DEFAULT_CONFIG_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_CONFIG_RELOAD_SHUTDOWN_SECONDS = 2.0


class ConfigReloadTransientError(RuntimeError):
    """A short-lived editor state that should receive a bounded retry."""


@dataclass(frozen=True)
class FileChange:
    """One coalescible watcher notification."""

    paths: FrozenSet[Path]


@dataclass(frozen=True)
class ConnectionReloadResult:
    """Public-summary diff from one committed authoritative reload."""

    deleted: tuple[ConnectionSummary, ...]
    created: tuple[ConnectionSummary, ...]
    updated: tuple[ConnectionSummary, ...]
    unchanged_count: int
    generation: int
    watched_paths: FrozenSet[Path]


class ConfigurationWatcher(Protocol):
    def replace_paths(self, paths: set[Path]) -> None: ...

    def start(self, callback: Callable[[FileChange], None]) -> None: ...

    def close(self) -> None: ...


def _fingerprint(path: Path) -> tuple[object, ...]:
    try:
        info = path.stat()
    except OSError:
        return (False,)
    return (
        True,
        info.st_dev,
        info.st_ino,
        stat.S_IFMT(info.st_mode),
        info.st_size,
        info.st_mtime_ns,
    )


class PollingConfigurationWatcher:
    """Small headless watcher that also detects inode-replacing editor saves."""

    def __init__(self, *, interval: float = DEFAULT_CONFIG_POLL_INTERVAL_SECONDS):
        if interval <= 0:
            raise ValueError("poll interval must be positive")
        self.interval = float(interval)
        self._condition = threading.Condition()
        self._paths: set[Path] = set()
        self._fingerprints: dict[Path, tuple[object, ...]] = {}
        self._callback: Optional[Callable[[FileChange], None]] = None
        self._stopping = False
        self._thread: Optional[threading.Thread] = None

    def replace_paths(self, paths: set[Path]) -> None:
        canonical = {Path(os.path.abspath(os.fspath(path))) for path in paths}
        with self._condition:
            self._paths = canonical
            self._fingerprints = {
                path: self._fingerprints.get(path, _fingerprint(path))
                for path in canonical
            }
            self._condition.notify_all()

    def start(self, callback: Callable[[FileChange], None]) -> None:
        if not callable(callback):
            raise TypeError("watch callback is required")
        with self._condition:
            if self._thread is not None:
                raise RuntimeError("configuration watcher is already started")
            self._callback = callback
            self._thread = threading.Thread(
                target=self._run,
                name="sshpilot-config-watcher",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(self.interval)
                if self._stopping:
                    return
                paths = tuple(self._paths)
                prior = dict(self._fingerprints)
            current = {path: _fingerprint(path) for path in paths}
            changed = frozenset(
                path
                for path in paths
                if current.get(path) != prior.get(path)
            )
            with self._condition:
                if self._stopping:
                    return
                for path, value in current.items():
                    if path in self._paths:
                        self._fingerprints[path] = value
                callback = self._callback
            if changed and callback is not None:
                try:
                    callback(FileChange(changed))
                except Exception as error:
                    logger.error(
                        "Configuration watcher callback failed type=%s",
                        type(error).__name__,
                    )

    def close(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._callback = None
            thread = self._thread
            self._condition.notify_all()
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(1.0, self.interval * 2))


class AuthoritativeConfigurationBackend:
    """Transactional adapter around the daemon's concrete in-process core."""

    def __init__(
        self,
        client: InProcessClient,
        connection_manager: object,
        group_manager: object,
        config: object,
    ) -> None:
        self.client = client
        self.connection_manager = connection_manager
        self.group_manager = group_manager
        self.config = config
        self._transient_root_failures = 0

    def discover_paths(self) -> set[Path]:
        root = Path(self.connection_manager.ssh_config_path)
        discovery = discover_ssh_config_paths(os.fspath(root))
        paths = {Path(path) for path in discovery.watch_paths}
        config_file = getattr(self.config, "config_file", None)
        if config_file:
            paths.add(Path(os.path.abspath(os.fspath(config_file))))
        return paths

    def _validate_includes_readable(self) -> None:
        discovery = discover_ssh_config_paths(
            os.fspath(self.connection_manager.ssh_config_path)
        )
        root = os.path.abspath(
            os.fspath(self.connection_manager.ssh_config_path)
        )
        if any(path != root for path in discovery.unreadable_paths):
            raise OSError("an SSH include is unreadable")

    def _validate_root_stability(self, has_connections: bool) -> None:
        root = Path(self.connection_manager.ssh_config_path)
        unstable = not root.exists()
        if not unstable:
            try:
                unstable = root.stat().st_size == 0
            except OSError:
                unstable = True
        if unstable and has_connections and self._transient_root_failures < 2:
            self._transient_root_failures += 1
            raise ConfigReloadTransientError(
                "SSH configuration is temporarily unavailable"
            )
        if not unstable:
            self._transient_root_failures = 0

    @staticmethod
    def _validate_group_data(config_data: object) -> None:
        if not isinstance(config_data, dict):
            raise ValueError("configuration root must be an object")
        groups = config_data.get("connection_groups", {})
        if groups is not None and not isinstance(groups, dict):
            raise ValueError("connection groups must be an object")
        if not isinstance(groups, dict):
            return
        for key, expected in (
            ("groups", dict),
            ("connections", dict),
            ("root_connections", list),
        ):
            value = groups.get(key, expected())
            if not isinstance(value, expected):
                raise ValueError(f"connection group {key} has an invalid type")

    def reload(self) -> ConnectionReloadResult:
        manager = self.connection_manager
        transaction = manager.authoritative_state_transaction
        with transaction():
            before = {
                summary.id: summary
                for summary in self.client.snapshot_connection_summaries()
            }
            self._validate_root_stability(bool(before))
            self._validate_includes_readable()
            old_config_data = copy.deepcopy(
                getattr(self.config, "config_data", {})
            )
            config_file = getattr(self.config, "config_file", None)
            try:
                if config_file:
                    read_strict = getattr(
                        self.config,
                        "read_json_config_strict",
                        None,
                    )
                    if callable(read_strict):
                        pending_config = read_strict()
                        self._validate_group_data(pending_config)
                        self.config.config_data = pending_config
                manager.load_ssh_config(
                    create_missing=False,
                    reuse_existing=False,
                )
                migration_error = getattr(
                    manager,
                    "identity_migration_error",
                    None,
                )
                if migration_error is not None:
                    raise RuntimeError("authoritative connection reload failed")
                load_groups = getattr(self.group_manager, "_load_groups", None)
                if callable(load_groups):
                    load_groups()
                bind_groups = getattr(
                    self.group_manager,
                    "bind_connections",
                    None,
                )
                if callable(bind_groups):
                    bind_groups(manager.get_connections())
                after = {
                    summary.id: summary
                    for summary in self.client.snapshot_connection_summaries()
                }
            except Exception:
                self.config.config_data = old_config_data
                raise

        deleted = tuple(
            before[key]
            for key in sorted(before.keys() - after.keys())
        )
        created = tuple(
            after[key]
            for key in sorted(after.keys() - before.keys())
        )
        updated = tuple(
            after[key]
            for key in sorted(before.keys() & after.keys())
            if before[key] != after[key]
        )
        unchanged = len(before.keys() & after.keys()) - len(updated)
        return ConnectionReloadResult(
            deleted=deleted,
            created=created,
            updated=updated,
            unchanged_count=unchanged,
            generation=int(
                getattr(manager, "connection_config_generation", 0)
            ),
            watched_paths=frozenset(self.discover_paths()),
        )

    def publish(self, result: ConnectionReloadResult) -> None:
        self.client.publish_connection_reload(
            deleted=result.deleted,
            created=result.created,
            updated=result.updated,
        )


class ConfigurationReloadCoordinator:
    """Debounce watcher hints and submit at most one reload plus one follow-up."""

    def __init__(
        self,
        backend: AuthoritativeConfigurationBackend,
        executor: BoundedCommandExecutor,
        *,
        watcher: Optional[ConfigurationWatcher] = None,
        debounce: float = DEFAULT_CONFIG_RELOAD_DEBOUNCE_SECONDS,
        poll_interval: float = DEFAULT_CONFIG_POLL_INTERVAL_SECONDS,
        shutdown_timeout: float = DEFAULT_CONFIG_RELOAD_SHUTDOWN_SECONDS,
    ) -> None:
        if debounce < 0:
            raise ValueError("reload debounce must not be negative")
        self.backend = backend
        self.executor = executor
        self.debounce = float(debounce)
        self.shutdown_timeout = max(0.0, float(shutdown_timeout))
        self.watcher = watcher or PollingConfigurationWatcher(
            interval=poll_interval
        )
        self._condition = threading.Condition()
        self._dirty = False
        self._active = False
        self._deadline = 0.0
        self._stopping = False
        self._transient_retries = 0
        self._thread: Optional[threading.Thread] = None
        self.reload_count = 0

    def start(self) -> None:
        paths = self.backend.discover_paths()
        self.watcher.replace_paths(paths)
        self.watcher.start(self._on_change)
        with self._condition:
            self._thread = threading.Thread(
                target=self._run,
                name="sshpilot-config-reload-coordinator",
                daemon=True,
            )
            self._thread.start()
        logger.info(
            "Daemon external configuration watcher active paths=%d",
            len(paths),
        )
        # Close the construction-to-watch registration race with one
        # debounced semantic no-op pass.
        self.request_reload()

    def _on_change(self, change: FileChange) -> None:
        del change
        with self._condition:
            if self._stopping:
                return
            self._dirty = True
            self._deadline = time.monotonic() + self.debounce
            self._condition.notify_all()

    def request_reload(self) -> None:
        """Test/development hook using the same bounded debounce path."""

        self._on_change(FileChange(frozenset()))

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and (
                    not self._dirty or self._active
                ):
                    self._condition.wait()
                if self._stopping:
                    return
                remaining = self._deadline - time.monotonic()
                if remaining > 0:
                    self._condition.wait(remaining)
                    continue
                self._dirty = False
                self._active = True
            command = DeferredCommand(
                key=CONFIGURATION_COMMAND_KEY,
                operation=self.backend.reload,
                on_complete=self._reload_complete,
                on_error=self._reload_failed,
                on_cancel=self._reload_cancelled,
            )
            if not self.executor.submit(command):
                with self._condition:
                    self._active = False
                    if not self._stopping:
                        self._dirty = True
                        self._deadline = time.monotonic() + max(
                            self.debounce,
                            0.1,
                        )
                        self._condition.notify_all()

    def _reload_complete(self, value: object) -> None:
        if type(value) is not ConnectionReloadResult:
            self._reload_failed(TypeError("invalid reload result"))
            return
        self.backend.publish(value)
        self.watcher.replace_paths(set(value.watched_paths))
        self.reload_count += 1
        self._transient_retries = 0
        logger.info(
            "Reloaded external connection configuration: "
            "created=%d updated=%d deleted=%d",
            len(value.created),
            len(value.updated),
            len(value.deleted),
        )
        self._finish_reload()

    def _reload_failed(self, error: BaseException) -> None:
        retry = (
            isinstance(error, ConfigReloadTransientError)
            and self._transient_retries < 2
        )
        if retry:
            self._transient_retries += 1
        else:
            self._transient_retries = 0
            logger.warning(
                "External connection reload failed; preserving "
                "last-known-good state type=%s",
                type(error).__name__,
            )
        self._finish_reload(retry=retry)

    def _reload_cancelled(self) -> None:
        self._finish_reload()

    def _finish_reload(self, *, retry: bool = False) -> None:
        with self._condition:
            self._active = False
            if retry and not self._stopping:
                self._dirty = True
                self._deadline = time.monotonic() + max(self.debounce, 0.05)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._dirty = False
            thread = self._thread
            self._condition.notify_all()
        self.watcher.close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(self.shutdown_timeout)
