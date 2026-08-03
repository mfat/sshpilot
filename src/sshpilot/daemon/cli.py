"""``sshpilot-daemon`` management CLI and server entry point."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.daemon import RestartDaemonRequest, StopDaemonRequest
from sshpilot.api.transport.codec import (
    daemon_diagnostics_to_wire,
    daemon_status_to_wire,
    daemon_stop_result_to_wire,
)

from .lifecycle import resolve_socket_path


def _configure_logging(verbose: bool) -> None:
    """Configure daemon logging.

    App-launched daemons have stdout/stderr pointed at ``DEVNULL``, so we
    always attach a rotating file under the XDG state dir. Console basicConfig
    still helps when the daemon is started by hand from a terminal.
    """

    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        logging.basicConfig(level=level)

    try:
        from sshpilot.platform_utils import get_state_dir

        log_dir = get_state_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "daemon.log")
    except Exception:
        return

    for handler in root.handlers:
        if getattr(handler, "_sshpilot_daemon_file", False):
            handler.setLevel(level)
            return

    try:
        from logging.handlers import RotatingFileHandler

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        file_handler._sshpilot_daemon_file = True  # type: ignore[attr-defined]
        root.addHandler(file_handler)
    except Exception:
        pass


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _management_client(socket_path: Path, *, timeout: float = 5.0) -> DaemonClient:
    return DaemonClient(
        socket_path=socket_path,
        timeout=timeout,
        client_name="sshpilot-daemon-cli",
        frontend_type="cli",
    )


def _run_status(client: DaemonClient) -> int:
    status = client.get_daemon_status()
    _print_json(daemon_status_to_wire(status))
    return 0


def _run_diagnostics(client: DaemonClient) -> int:
    diagnostics = client.get_daemon_diagnostics()
    _print_json(daemon_diagnostics_to_wire(diagnostics))
    return 0


def _run_stop(client: DaemonClient, *, force: bool) -> int:
    result = client.stop_daemon(StopDaemonRequest(force=force))
    _print_json(daemon_stop_result_to_wire(result))
    return 0 if result.accepted else 1


def _run_restart(client: DaemonClient, *, force: bool) -> int:
    result = client.restart_daemon(RestartDaemonRequest(force=force))
    _print_json(daemon_stop_result_to_wire(result))
    return 0 if result.accepted else 1


def _run_management(args: argparse.Namespace) -> int:
    socket_path = resolve_socket_path(args.socket)
    try:
        client = _management_client(socket_path)
    except Exception as error:
        print(
            json.dumps(
                {
                    "available": False,
                    "error": type(error).__name__,
                    "message": "Could not connect to the local daemon",
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    try:
        if args.command == "status":
            return _run_status(client)
        if args.command == "diagnostics":
            return _run_diagnostics(client)
        if args.command == "stop":
            return _run_stop(client, force=args.force)
        if args.command == "restart":
            return _run_restart(client, force=args.force)
        raise AssertionError(f"unknown command {args.command!r}")
    except SshPilotError as error:
        print(
            json.dumps(
                {
                    "available": False,
                    "error": error.code.value,
                    "message": error.message,
                },
                indent=2,
            ),
            file=sys.stderr,
        )
        if error.code is ErrorCode.DAEMON_UNAVAILABLE:
            return 2
        return 1
    finally:
        client.close()


def run_server(
    *,
    socket_path: Optional[Path] = None,
    verbose: bool = False,
    serve_forever: Callable[..., None],
    shutdown: Callable[[], None],
    startup_error: Optional[BaseException],
    restart_requested: Optional[Callable[[], bool]] = None,
) -> int:
    _configure_logging(verbose)

    def _stop(_signum, _frame) -> None:
        shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    serve_forever()
    if startup_error is not None:
        raise startup_error
    if restart_requested is not None and restart_requested():
        from .lifecycle_policy import RESTART_EXIT_CODE

        return RESTART_EXIT_CODE
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sshpilot-daemon",
        description="Run or manage the local sshPilot daemon",
    )
    parser.add_argument(
        "--socket",
        type=Path,
        help="override the Unix socket path",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="force stop/restart without confirmation (management commands only)",
    )
    subparsers = parser.add_subparsers(dest="command")
    for name in ("status", "stop", "restart", "diagnostics"):
        subparsers.add_parser(name, help=f"{name} the running daemon")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command is None:
        from .server import DaemonServer

        _configure_logging(args.verbose)
        from .lifecycle_policy import _IDLE_SHUTDOWN_UNSET

        # Unset means "use environment default" (dev 300s / packaged 120s).
        # Explicit ``None`` from config would disable idle shutdown.
        idle_shutdown_seconds: object = _IDLE_SHUTDOWN_UNSET
        service_mode = False
        packaged = False
        try:
            from sshpilot.config import Config

            app_config = Config()
            raw_idle = app_config.get_setting("daemon.idle_shutdown_seconds", None)
            if raw_idle is not None:
                idle_shutdown_seconds = float(raw_idle)
            service_mode = bool(
                app_config.get_setting("daemon.service_mode", False)
                or os.environ.get("SSHPILOT_DAEMON_SERVICE_MODE")
            )
            packaged = bool(os.environ.get("SSHPILOT_PACKAGED"))
        except Exception:
            logging.getLogger(__name__).debug(
                "daemon config unavailable; using idle defaults",
                exc_info=True,
            )
        server = DaemonServer(
            _production_core_services,
            socket_path=args.socket,
            idle_shutdown_seconds=idle_shutdown_seconds,
            service_mode=service_mode,
            packaged=packaged,
        )

        def _shutdown() -> None:
            server.shutdown()

        return run_server(
            socket_path=args.socket,
            verbose=args.verbose,
            serve_forever=server.serve_forever,
            shutdown=_shutdown,
            startup_error=server._startup_error,
            restart_requested=lambda: bool(
                server._lifecycle is not None and server._lifecycle.restart_requested
            ),
        )
    return _run_management(args)


def _production_core_services():
    # Imports stay here so transport modules remain frontend-neutral and tests
    # can inject a headless core without importing PyGObject.
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.config import Config
    from sshpilot.connection_manager import ConnectionManager
    from sshpilot.groups import GroupManager

    from .config_reload import AuthoritativeConfigurationBackend
    from .server import CoreServices

    config = Config()
    connection_manager = ConnectionManager(config)
    # Daemon has no GLib main loop, so the idle-deferred secret/identity init
    # from ConnectionManager.__init__ never runs unless we call it here.
    try:
        connection_manager._post_init_slow_path()
    except Exception:
        logging.getLogger(__name__).debug(
            "daemon secret/identity slow-path init failed",
            exc_info=True,
        )
    if connection_manager.identity_migration_error is not None:
        raise RuntimeError("connection identity migration failed")
    group_manager = GroupManager(
        config,
        connection_manager=connection_manager,
    )
    # Protocol launch providers must live with the process-owning daemon.
    # Headless loading deliberately omits PluginHost/UI integration.
    from sshpilot.plugins.loader import load_plugins
    load_plugins(
        app_config=config,
        connection_manager=connection_manager,
        plugin_host=None,
    )
    connections = ConnectionApplicationService(
        connection_manager,
        group_manager=group_manager,
        client_name="sshpilotd",
        allow_cross_thread_commands=True,
    )
    return CoreServices(
        connections=connections,
        configuration_backend=AuthoritativeConfigurationBackend(
            connections,
            connection_manager,
            group_manager,
            config,
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
