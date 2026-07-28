"""Development entry point for ``python -m sshpilot.daemon``."""

from __future__ import annotations

import argparse
import logging
import signal
from pathlib import Path
from typing import Optional

from sshpilot.api.in_process_client import InProcessClient

from .server import DaemonServer


def _production_core_client() -> InProcessClient:
    # Imports stay here so transport modules remain frontend-neutral and tests
    # can inject a headless core without importing PyGObject.
    from sshpilot.config import Config
    from sshpilot.connection_manager import ConnectionManager
    from sshpilot.groups import GroupManager

    config = Config()
    connection_manager = ConnectionManager(config)
    if connection_manager.identity_migration_error is not None:
        raise RuntimeError("connection identity migration failed")
    group_manager = GroupManager(
        config,
        connection_manager=connection_manager,
    )
    return InProcessClient(
        connection_manager,
        group_manager=group_manager,
        client_name="sshpilotd",
    )


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local sshPilot daemon")
    parser.add_argument(
        "--socket",
        type=Path,
        help="development override for the Unix socket path",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    server = DaemonServer(_production_core_client, socket_path=args.socket)

    def _stop(_signum, _frame) -> None:
        server.shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    server.serve_forever()
    if server._startup_error is not None:
        raise server._startup_error
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
