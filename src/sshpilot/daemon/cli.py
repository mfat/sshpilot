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
from sshpilot.logging_support import configure_daemon_logging
from sshpilot.api.transport.codec import (
    daemon_diagnostics_to_wire,
    daemon_status_to_wire,
    daemon_stop_result_to_wire,
)
from sshpilot.platform.paths import get_config_dir, get_ssh_dir

from .lifecycle import resolve_socket_path


def _resolve_ssh_root(isolated: bool) -> Path:
    """Return the daemon-selected active SSH config root file.

    Normal mode edits ``~/.ssh/config``; isolated mode owns a dedicated
    ``ssh_config`` under the app config directory. Kept as a module-level
    helper so the resolution is unit-testable; the daemon is the only caller.
    """
    if isolated:
        return get_config_dir() / "ssh_config"
    return get_ssh_dir() / "config"


def _configure_logging(verbose: bool, quiet: bool = False) -> None:
    """Configure daemon logging.

    App-launched daemons have stdout/stderr pointed at ``DEVNULL``, so the
    shared policy always attaches a rotating file under the XDG state dir and
    a managed console handler for directly started daemons.
    """

    try:
        from sshpilot.platform_utils import get_state_dir
        from .bootstrap_settings import DaemonBootstrapSettings

        log_dir = get_state_dir()
        configured = DaemonBootstrapSettings().get_setting("logging.level", "info")
    except Exception:
        return
    effective = "warning" if quiet else "debug" if verbose else configured
    configure_daemon_logging(log_dir, effective)


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
    quiet: bool = False,
    serve_forever: Callable[..., None],
    shutdown: Callable[[], None],
    startup_error: Optional[BaseException],
    restart_requested: Optional[Callable[[], bool]] = None,
) -> int:
    _configure_logging(verbose, quiet)

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
    parser.add_argument("--quiet", action="store_true")
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

        _configure_logging(args.verbose, args.quiet)
        from .lifecycle_policy import _IDLE_SHUTDOWN_UNSET

        # Unset means "use environment default" (dev 300s / packaged 120s).
        # Explicit ``None`` from config would disable idle shutdown.
        idle_shutdown_seconds: object = _IDLE_SHUTDOWN_UNSET
        service_mode = False
        packaged = False
        try:
            from .bootstrap_settings import DaemonBootstrapSettings

            app_config = DaemonBootstrapSettings()
            raw_idle = app_config.idle_shutdown_seconds
            if raw_idle is not None:
                idle_shutdown_seconds = float(raw_idle)
            service_mode = app_config.service_mode
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
            quiet=args.quiet,
            serve_forever=server.serve_forever,
            shutdown=_shutdown,
            startup_error=server._startup_error,
            restart_requested=lambda: bool(
                server._lifecycle is not None and server._lifecycle.restart_requested
            ),
        )
    return _run_management(args)


def _production_core_services():
    """Compose the daemon's headless application services.

    The daemon is the sole owner of saved connection state: it resolves the
    active SSH config root, owns ``connections.json``, and drives the headless
    ``ConnectionRepository``. Launch and secret behavior come from the daemon
    compatibility providers (which may still use legacy helpers under
    importer-specific debt).
    """
    # Imports stay here so transport modules remain frontend-neutral and tests
    # can inject a headless core without importing PyGObject.
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    from .bootstrap_settings import DaemonBootstrapSettings
    from .config_reload import AuthoritativeConfigurationBackend
    from .connection_launch_provider import DaemonConnectionLaunchProvider
    from .connection_secret_provider import DaemonConnectionSecretProvider
    from .key_service import DaemonKeyService
    from .known_hosts_service import KnownHostsService
    from .server import CoreServices

    def _resolve_key_root(scope):
        from sshpilot.api.models.keys import KeyStoreScope

        if scope is KeyStoreScope.DEFAULT:
            return get_ssh_dir()
        if scope is KeyStoreScope.ISOLATED:
            return get_config_dir()
        raise ValueError("unsupported key store scope")

    settings = DaemonBootstrapSettings()
    isolated = settings.use_isolated_config
    ssh_root = _resolve_ssh_root(isolated)
    ssh_store = SshConfigStore(ssh_root, isolated=isolated)
    repository = ConnectionRepository(
        ssh_store=ssh_store,
        state_path=get_config_dir() / "connections.json",
        legacy_config_path=get_config_dir() / "config.json",
        isolated=isolated,
    )
    def _build_ssh_overrides_service():
        from sshpilot.core.ssh_overrides_service import SshOverridesService
        from sshpilot.ssh_multiplex import controlmaster_args

        # The multiplex args are always injected so the daemon composes the
        # ControlMaster fragment based on the *currently loaded* value of
        # ``ssh.controlmaster`` in the settings file — toggling it in
        # Preferences takes effect without a daemon restart.
        try:
            multiplex_extra = controlmaster_args()
        except Exception:
            multiplex_extra = None
        return SshOverridesService(
            get_config_dir() / "config.json",
            controlmaster_extra=multiplex_extra,
        )

    overrides_service = _build_ssh_overrides_service()

    def _build_identity_state_service():
        from sshpilot.core.identity_service import IdentityStateService

        return IdentityStateService(get_config_dir() / "config.json")

    identity_state_service = _build_identity_state_service()
    secret_provider = DaemonConnectionSecretProvider(repository.get_record)
    launch_provider = DaemonConnectionLaunchProvider(
        repository.get_record,
        secret_provider=secret_provider,
        app_config=overrides_service,
        headless_settings=settings,
        identity_env=identity_state_service.agent_environment,
    )
    connections = ConnectionApplicationService(
        repository,
        launch_provider=launch_provider,
        secret_provider=secret_provider,
        ssh_overrides=overrides_service,
        client_name="sshpilotd",
        allow_cross_thread_commands=True,
    )

    def _build_secrets_service():
        from sshpilot.daemon.secret_backend_service import SecretBackendService

        return SecretBackendService(
            get_config_dir() / "config.json",
            # A callable so daemon export can list records without coupling the
            # service to the repository object (``_selected_views`` accepts a
            # callable or an iterable of records).
            connections_source=repository.list_records,
        )

    secrets_service = _build_secrets_service()

    def _build_identity_services():
        from sshpilot.daemon.identity_service import DaemonIdentityService
        from sshpilot.daemon.operation_runtime import OperationRuntime

        key_service = DaemonKeyService(_resolve_key_root)
        operation_runtime = OperationRuntime()
        identity_service = DaemonIdentityService(
            identity_state_service,
            key_service,
            operation_runtime,
            launch_provider=launch_provider,
        )
        return key_service, operation_runtime, identity_service

    key_service, operation_runtime, identity_service = _build_identity_services()

    return CoreServices(
        connections=connections,
        configuration_backend=AuthoritativeConfigurationBackend(repository),
        known_hosts=KnownHostsService(lambda: get_ssh_dir() / "known_hosts"),
        keys=key_service,
        ssh_overrides=overrides_service,
        secrets=secrets_service,
        identity=identity_service,
        operations=operation_runtime,
    )


if __name__ == "__main__":
    raise SystemExit(main())
