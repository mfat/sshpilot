"""Reference headless CLI backed exclusively by :class:`DaemonClient`."""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from typing import Callable, Sequence

from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.broadcast import BroadcastCommandRequest
from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.operations import OperationState

from .interactions import handle_interactions
from .output import write_human, write_json

EXIT_OK = 0
EXIT_OPERATION_FAILED = 1
EXIT_USAGE = 2
EXIT_DAEMON_UNAVAILABLE = 3

_UNAVAILABLE = {
    ErrorCode.DAEMON_UNAVAILABLE,
    ErrorCode.TRANSPORT_CLOSED,
    ErrorCode.TRANSPORT_TIMEOUT,
    ErrorCode.DAEMON_INCOMPATIBLE,
    ErrorCode.API_VERSION_MISMATCH,
    ErrorCode.UNSUPPORTED_CAPABILITY,
    ErrorCode.UNSUPPORTED_METHOD,
    ErrorCode.DAEMON_SHUTTING_DOWN,
    ErrorCode.DAEMON_RESTART_REQUIRED,
}


class CliUsageError(ValueError):
    """A command-line validation error."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sshpilot-cli")
    parser.add_argument("--json", action="store_true", help="write JSON to stdout")
    parser.add_argument("--socket", help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("status", "capabilities"):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        command.set_defaults(handler=name)

    connections = commands.add_parser("connections")
    connections.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    connection_commands = connections.add_subparsers(dest="connections_command", required=True)
    listed = connection_commands.add_parser("list")
    listed.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    listed.set_defaults(handler="connections_list")
    shown = connection_commands.add_parser("show")
    shown.add_argument("connection")
    shown.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    shown.set_defaults(handler="connections_show")

    sessions = commands.add_parser("sessions")
    sessions.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    sessions.set_defaults(handler="sessions_list")
    session_commands = sessions.add_subparsers(dest="sessions_command", required=False)
    session_list = session_commands.add_parser("list")
    session_list.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    session_list.set_defaults(handler="sessions_list")

    execute = commands.add_parser("exec")
    execute.add_argument("connection")
    execute.add_argument("command", nargs=argparse.REMAINDER)
    execute.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    execute.set_defaults(handler="exec")

    operations = commands.add_parser("operations")
    operations.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    operation_commands = operations.add_subparsers(dest="operations_command", required=True)
    for name, handler in (("get", "operations_get"), ("cancel", "operations_cancel")):
        operation = operation_commands.add_parser(name)
        operation.add_argument("operation_id")
        operation.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        operation.set_defaults(handler=handler)
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    client_factory: Callable[..., object] | None = None,
    stdout=None,
    stderr=None,
    input_fn=input,
    getpass_fn=None,
    sleep_fn=time.sleep,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        client_factory = client_factory or _default_client_factory
        client = client_factory(socket_path=args.socket)
        try:
            return _dispatch(
                client,
                args,
                stdout=stdout,
                stderr=stderr,
                input_fn=input_fn,
                getpass_fn=getpass_fn or _default_getpass,
                sleep_fn=sleep_fn,
            )
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
    except CliUsageError as error:
        print(f"error: {error}", file=stderr)
        return EXIT_USAGE
    except SshPilotError as error:
        print(f"error: {error.code.value}: {error.message}", file=stderr)
        return EXIT_DAEMON_UNAVAILABLE if error.code in _UNAVAILABLE else EXIT_OPERATION_FAILED
    except (OSError, TimeoutError):
        print("error: daemon unavailable", file=stderr)
        return EXIT_DAEMON_UNAVAILABLE
    except ValueError as error:
        print(f"error: {error}", file=stderr)
        return EXIT_USAGE
    except Exception:
        # Do not expose arbitrary backend exception text through a CLI error
        # channel; typed daemon errors are handled above.
        print("error: operation failed", file=stderr)
        return EXIT_OPERATION_FAILED


def main(argv: Sequence[str] | None = None) -> int:
    return run(argv)


def _default_client_factory(*, socket_path=None):
    return DaemonClient(
        socket_path=socket_path,
        client_name="sshpilot-cli",
        frontend_type="cli",
    )


def _default_getpass(prompt: str) -> str:
    import getpass

    return getpass.getpass(prompt)


def _dispatch(client, args, *, stdout, stderr, input_fn, getpass_fn, sleep_fn) -> int:
    json_output = bool(args.json)
    handler = args.handler
    if handler == "status":
        return _show(client.get_daemon_status(), json_output, stdout, "daemon status")
    if handler == "capabilities":
        return _show(client.get_capabilities(), json_output, stdout, "capabilities")
    if handler == "connections_list":
        values = client.list_connections()
        if json_output:
            write_json(stdout, values)
        else:
            for item in values:
                stdout.write(
                    f"{item.id}\t{item.nickname}\t{item.display_target}:{item.port}\n"
                )
        return EXIT_OK
    if handler == "connections_show":
        value = client.get_connection(_resolve_connection(client, args.connection))
        return _show(value, json_output, stdout, "connection")
    if handler == "sessions_list":
        values = client.list_sessions()
        if json_output:
            write_json(stdout, values)
        else:
            for item in values:
                stdout.write(
                    f"{item.id}\t{item.connection_id}\t{item.state.value}\t"
                    f"attachments={item.attachment_count}\n"
                )
        return EXIT_OK
    if handler == "operations_get":
        return _show(client.get_operation(args.operation_id), json_output, stdout, "operation")
    if handler == "operations_cancel":
        return _show(client.cancel_operation(args.operation_id), json_output, stdout, "operation")
    if handler == "exec":
        return _execute(
            client,
            args,
            json_output=json_output,
            stdout=stdout,
            stderr=stderr,
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            sleep_fn=sleep_fn,
        )
    raise CliUsageError("unknown command")


def _show(value, json_output, stdout, title):
    if json_output:
        write_json(stdout, value)
    else:
        write_human(stdout, value, title=title)
    return EXIT_OK


def _resolve_connection(client, name: str) -> ConnectionId:
    matches = []
    for item in client.list_connections():
        if name in {str(item.id), item.nickname, item.host, item.hostname}:
            if item.id not in matches:
                matches.append(item.id)
    if not matches:
        raise CliUsageError(f"connection not found: {name}")
    if len(matches) > 1:
        raise CliUsageError(f"ambiguous connection name: {name}")
    return ConnectionId(matches[0])


def _execute(client, args, *, json_output, stdout, stderr, input_fn, getpass_fn, sleep_fn):
    command = list(args.command)
    if "--" in command:
        separator = command.index("--")
        json_output = json_output or "--json" in command[:separator]
        command = command[separator + 1 :]
    elif "--json" in command:
        command.remove("--json")
    if not command or not " ".join(command).strip():
        raise CliUsageError("exec requires a command after --")
    connection_id = _resolve_connection(client, args.connection)
    remote_command = command[0] if len(command) == 1 else shlex.join(command)
    request = BroadcastCommandRequest((connection_id,), remote_command)
    started = client.start_broadcast_command(request)
    operation_id = started.operation.operation_id
    handled = set()
    while True:
        handled = handle_interactions(
            client,
            operation_id,
            write=lambda text: print(text, file=stderr, end=""),
            input_fn=input_fn,
            getpass_fn=getpass_fn,
            handled=handled,
        )
        current = client.get_broadcast_command(operation_id)
        if current.operation.state in {
            OperationState.SUCCEEDED,
            OperationState.FAILED,
            OperationState.CANCELLED,
        }:
            break
        sleep_fn(0.05)
    target = current.targets[0] if current.targets else None
    if target is not None and not json_output:
        if target.stdout:
            stdout.write(target.stdout)
        if target.stderr:
            stderr.write(target.stderr)
    if json_output:
        # Output is emitted as a structured result only for scripting; command
        # output remains in the typed target fields rather than being merged.
        write_json(stdout, current)
    if current.operation.state is not OperationState.SUCCEEDED or (
        target is not None and target.state.value != "succeeded"
    ):
        return EXIT_OPERATION_FAILED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
