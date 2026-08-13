"""Phase 11 lifecycle stress: repeated start/stop without FD or thread leaks."""

from __future__ import annotations

import os
import threading

import pytest

from sshpilot.api import DaemonClient
from sshpilot.api.models.daemon import StopDaemonRequest

pytestmark = pytest.mark.integration


def _open_fds() -> int:
    try:
        return len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except OSError:
        return -1


def _thread_count() -> int:
    return threading.active_count()


@pytest.mark.parametrize("cycles", [50])
def test_fifty_daemon_lifecycle_cycles_do_not_leak_fds_or_threads(
    daemon_factory,
    cycles,
):
    baseline_fds = _open_fds()
    baseline_threads = _thread_count()
    assert baseline_fds > 0

    for index in range(cycles):
        server, _manager = daemon_factory(
            idle_shutdown_seconds=0.0,
            socket_path=None,
        )
        client = DaemonClient(socket_path=server.socket_path)
        status = client.get_daemon_status()
        assert status.server_instance_id
        diagnostics = client.get_daemon_diagnostics()
        assert diagnostics.status.server_instance_id == status.server_instance_id
        result = client.stop_daemon(StopDaemonRequest())
        assert result.accepted is True
        client.close()
        assert server.wait_stopped(timeout=5.0)
        assert not server.socket_path.exists(), f"socket leaked after cycle {index}"

    # Allow a small absolute jitter for interpreter/GC noise across 50 cycles.
    final_fds = _open_fds()
    final_threads = _thread_count()
    assert final_fds >= 0
    assert final_fds <= baseline_fds + 8, (
        f"FD growth after {cycles} cycles: {baseline_fds} -> {final_fds}"
    )
    assert final_threads <= baseline_threads + 4, (
        f"thread growth after {cycles} cycles: {baseline_threads} -> {final_threads}"
    )
