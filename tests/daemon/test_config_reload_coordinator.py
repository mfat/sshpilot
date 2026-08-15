"""Focused lifecycle tests for the daemon configuration reload coordinator."""

import time
from pathlib import Path

from sshpilot.daemon.config_reload import (
    ConfigurationReloadCoordinator,
    ConnectionReloadResult,
    FileChange,
)


class _Backend:
    def __init__(self):
        self.paths = {Path("/tmp/config")}
        self.reload_calls = 0

    def discover_paths(self):
        return set(self.paths)

    def reload(self):
        self.reload_calls += 1
        return ConnectionReloadResult(
            deleted=(),
            created=(),
            updated=(),
            unchanged_count=0,
            generation=self.reload_calls,
            watched_paths=frozenset(self.paths),
        )

    def publish(self, _result):
        pass


class _Watcher:
    def __init__(self, *, notify_during_start=False):
        self.paths = []
        self.callback = None
        self.notify_during_start = notify_during_start
        self.closed = False

    def replace_paths(self, paths):
        self.paths.append(set(paths))

    def start(self, callback):
        self.callback = callback
        if self.notify_during_start:
            callback(FileChange(frozenset({Path("/tmp/config")})))

    def close(self):
        self.closed = True


class _SynchronousExecutor:
    def submit(self, command):
        try:
            command.on_complete(command.operation())
        except BaseException as error:  # noqa: BLE001 - test executor
            command.on_error(error)
        return True


def _wait_for(predicate):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def test_start_schedules_initial_reload_and_closes_registration_race():
    backend = _Backend()
    watcher = _Watcher(notify_during_start=True)
    coordinator = ConfigurationReloadCoordinator(
        backend,
        _SynchronousExecutor(),
        watcher=watcher,
        debounce=0,
        shutdown_timeout=1,
    )

    coordinator.start()
    try:
        assert _wait_for(lambda: coordinator.reload_count == 1)
        assert backend.reload_calls == 1
        assert watcher.paths[0] == backend.paths
    finally:
        coordinator.close()
    assert watcher.closed


def test_refresh_paths_replaces_include_roots_and_schedules_reload():
    backend = _Backend()
    watcher = _Watcher()
    coordinator = ConfigurationReloadCoordinator(
        backend,
        _SynchronousExecutor(),
        watcher=watcher,
        debounce=0,
        shutdown_timeout=1,
    )
    coordinator.start()
    assert _wait_for(lambda: coordinator.reload_count == 1)
    try:
        backend.paths = {Path("/tmp/config"), Path("/tmp/conf.d/new.conf")}
        coordinator.refresh_paths()
        assert _wait_for(lambda: coordinator.reload_count == 2)
        assert watcher.paths[-1] == backend.paths
        assert backend.reload_calls == 2
    finally:
        coordinator.close()
