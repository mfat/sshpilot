"""Daemon-managed localhost Streamable HTTP MCP sidecar lifecycle."""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from sshpilot.core.settings.assistant_access import mcp_http_url

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
MCP_PATH = "/mcp"
READY_TIMEOUT_SECONDS = 8.0


class McpHttpService:
    """Start/stop ``python -m sshpilot.mcp.runtime`` over Streamable HTTP."""

    def __init__(
        self,
        *,
        socket_path: str | Path,
        settings_provider: Callable[[], Any],
        python_executable: Optional[str] = None,
    ) -> None:
        self._socket_path = str(socket_path)
        self._settings_provider = settings_provider
        self._python = python_executable or sys.executable
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._bound_port: Optional[int] = None
        self._token: str = ""
        self._desired_listen = False
        self._stop = threading.Event()
        self._monitor: Optional[threading.Thread] = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            alive = self._process is not None and self._process.poll() is None
            port = self._bound_port if alive else None
            return {
                "http_listening": bool(alive and port),
                "http_bound_port": port,
                "http_url": mcp_http_url(port) if alive and port else "",
            }

    def sync_from_settings(self, settings: Any | None = None) -> None:
        """Start or stop the sidecar to match the current policy snapshot."""
        snap = settings if settings is not None else self._settings_provider()
        enabled = bool(getattr(snap, "enabled", False))
        listen = bool(getattr(snap, "http_listen", False))
        port = int(getattr(snap, "http_port", 0) or 0)
        token = str(getattr(snap, "http_token", "") or "")
        want = enabled and listen and bool(token)
        with self._lock:
            self._desired_listen = want
            if not want:
                self._stop_locked()
                return
            same = (
                self._process is not None
                and self._process.poll() is None
                and self._token == token
                and (
                    (port == 0 and self._bound_port is not None)
                    or (port != 0 and self._bound_port == port)
                )
            )
            if same:
                return
            self._stop_locked()
            self._start_locked(port=port, token=token)

    def start_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._stop.clear()
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="mcp-http-monitor",
                daemon=True,
            )
            self._monitor.start()

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            self._desired_listen = False
            self._stop_locked()
        monitor = self._monitor
        if monitor is not None and monitor.is_alive():
            monitor.join(timeout=2.0)

    def _monitor_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                with self._lock:
                    want = self._desired_listen
                    alive = self._process is not None and self._process.poll() is None
                if want and not alive:
                    logger.warning("MCP HTTP sidecar exited; restarting from settings")
                    self.sync_from_settings()
            except Exception:  # noqa: BLE001
                logger.exception("MCP HTTP monitor failed")

    def _start_locked(self, *, port: int, token: str) -> None:
        bind_port = port if port > 0 else _allocate_ephemeral_port()
        env = os.environ.copy()
        env["SSHPILOT_MCP_SOCKET"] = self._socket_path
        env["SSHPILOT_MCP_READ"] = "1"
        env["SSHPILOT_MCP_OPERATE"] = "1"
        env["SSHPILOT_MCP_MUTATE"] = "1"
        env["SSHPILOT_MCP_CONTENT"] = "1"
        env["SSHPILOT_MCP_HTTP_TOKEN"] = token
        # Env policy is a floor under daemon ceiling; daemon still gates.
        cmd = [
            self._python,
            "-m",
            "sshpilot.mcp.runtime",
            "--transport",
            "streamable-http",
            "--host",
            HOST,
            "--port",
            str(bind_port),
            "--token",
            token,
        ]
        logger.info("Starting MCP HTTP sidecar on %s:%s", HOST, bind_port)
        try:
            process = subprocess.Popen(
                cmd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as error:
            logger.error("Failed to spawn MCP HTTP sidecar: %s", error)
            self._bound_port = None
            self._token = ""
            return
        if not _wait_for_port(HOST, bind_port, timeout=READY_TIMEOUT_SECONDS):
            logger.error("MCP HTTP sidecar did not bind %s:%s in time", HOST, bind_port)
            _terminate_process(process)
            self._process = None
            self._bound_port = None
            self._token = ""
            return
        self._process = process
        self._bound_port = bind_port
        self._token = token
        threading.Thread(
            target=_drain_stream,
            args=(process.stderr, "mcp-http-stderr"),
            name="mcp-http-stderr",
            daemon=True,
        ).start()
        if process.stdout is not None:
            threading.Thread(
                target=_drain_stream,
                args=(process.stdout, "mcp-http-stdout"),
                name="mcp-http-stdout",
                daemon=True,
            ).start()

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._bound_port = None
        self._token = ""
        if process is None:
            return
        _terminate_process(process)


def _allocate_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, 0))
        return int(sock.getsockname()[1])


def _wait_for_port(host: str, port: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass


def _drain_stream(stream: Any, label: str) -> None:
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip()
            if text:
                logger.debug("%s: %s", label, text)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass
