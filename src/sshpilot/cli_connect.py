"""CLI connect: parse ssh-like argv and open a terminal tab.

``sshpilot`` accepts the same destination forms as OpenSSH after its own
options, e.g.::

    sshpilot web
    sshpilot root@example.com
    sshpilot -p 2222 user@host
    sshpilot ssh -J bastion user@host

Known sshPilot flags (``-v``/``-q``, ``--isolated``, diagnostics, …) are
parsed first; everything else is treated as an ``ssh`` command line.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any, List, Sequence

from .command_converter import parse_ssh_command
from .connection_manager import Connection
from .ssh_connection_builder import SSHConnectionCommand

logger = logging.getLogger(__name__)

# Connection.data flags for CLI-opened sessions
CLI_CONNECT_FLAG = '__cli_connect'   # ephemeral from parser; may offer save
CLI_SESSION_FLAG = '__cli_session'   # opened via CLI; fail like ssh (no leftover tab)


@dataclass
class CliConnectOptions:
    verbose: bool = False
    quiet: bool = False
    isolated: bool = False
    log_gtk_warnings: bool = False
    fatal_warnings: bool = False
    diagnostics: bool = False
    # Remaining tokens after sshPilot options (ssh-like).
    ssh_tokens: List[str] = field(default_factory=list)


@dataclass
class ResolvedCliConnect:
    """Result of resolving CLI tokens into something the UI can open."""
    connection: Connection
    ssh_argv: List[str]
    # True when we reused an existing ConnectionManager entry (Host alias).
    existing: bool = False
    # Display / error helpers
    label: str = ''


def build_ssh_argv(tokens: Sequence[str]) -> List[str]:
    """Normalize CLI remainder into an ``ssh`` argv list."""
    parts = [str(t) for t in tokens if t is not None and str(t) != '']
    if not parts:
        return []
    if parts[0] == 'ssh':
        return list(parts)
    # Do not wrap other remote tools as ``ssh scp ...`` — refuse like ssh.
    if parts[0] in ('scp', 'sftp', 'rsync', 'ssh-copy-id'):
        return list(parts)
    return ['ssh', *parts]


def parse_sshpilot_cli(argv: Sequence[str]) -> CliConnectOptions:
    """Parse sshPilot flags; leave the rest as ssh-like tokens.

    Uses ``parse_known_args`` so OpenSSH options (``-p``, ``-i``, ``-J``, …)
    pass through. ``-v`` / ``-q`` are claimed by sshPilot when they appear as
    known options (typically before the destination); place ssh's own ``-v``
    after the destination or use ``sshpilot ssh -v host``.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog='sshpilot',
        description='sshPilot — SSH connection manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Connection targets use the same forms as OpenSSH:\n"
            "  sshpilot HostAlias\n"
            "  sshpilot user@hostname\n"
            "  sshpilot -p 2222 user@hostname\n"
            "  sshpilot ssh -J bastion user@hostname\n"
            "\n"
            "On successful connect to a host/user not already in ssh config,\n"
            "sshPilot offers to save a new connection.\n"
            "\n"
            "Logs are written under the state directory\n"
            "(~/.local/state/sshpilot, or the Flatpak equivalent):\n"
            "  sshpilot.log, app.log, ssh.log, crash.log\n"
            "\n"
            "Extra diagnostics:\n"
            "  --diagnostics        verbose + GTK info/debug (for bug reports)\n"
            "  --log-gtk-warnings   capture lower-severity GTK/GLib messages\n"
            "  --fatal-warnings     abort at the first GTK/GLib warning\n"
        ),
        allow_abbrev=False,
    )
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        '--verbose', '-v', action='store_true',
        help='Verbose debug logging (overrides config)',
    )
    verbosity.add_argument(
        '--quiet', '-q', action='store_true',
        help='Only show warnings and errors (overrides config)',
    )
    parser.add_argument(
        '--isolated', action='store_true',
        help='Use isolated SSH configuration',
    )
    diagnostics = parser.add_argument_group(
        'diagnostics', 'Capture extra logs to help diagnose bugs')
    diagnostics.add_argument(
        '--log-gtk-warnings', action='store_true',
        help='Also capture lower-severity GTK/GLib info & debug messages.',
    )
    diagnostics.add_argument(
        '--fatal-warnings', action='store_true',
        help='Abort on the first GTK/GLib warning or critical.',
    )
    diagnostics.add_argument(
        '--diagnostics', action='store_true',
        help='Shorthand for --verbose --log-gtk-warnings.',
    )

    args, remainder = parser.parse_known_args(list(argv))
    if remainder and remainder[0] == '--':
        remainder = remainder[1:]

    return CliConnectOptions(
        verbose=bool(args.verbose),
        quiet=bool(args.quiet),
        isolated=bool(args.isolated),
        log_gtk_warnings=bool(args.log_gtk_warnings),
        fatal_warnings=bool(args.fatal_warnings),
        diagnostics=bool(args.diagnostics),
        ssh_tokens=list(remainder),
    )


def _is_simple_host_alias(tokens: Sequence[str]) -> bool:
    """True for a single bare Host alias token (no user@, no options)."""
    if len(tokens) != 1:
        return False
    token = tokens[0]
    if not token or token.startswith('-') or '@' in token:
        return False
    if token == 'ssh':
        return False
    return True


def resolve_cli_connect(
    tokens: Sequence[str],
    connection_manager: Any,
) -> ResolvedCliConnect:
    """Resolve ssh-like *tokens* to a :class:`Connection` ready to open.

    Raises ``ValueError`` with a user-visible message on failure.
    """
    ssh_argv = build_ssh_argv(tokens)
    if not ssh_argv:
        raise ValueError('No SSH destination specified')

    # Prefer an existing sidebar connection for a bare Host alias.
    if _is_simple_host_alias(tokens):
        alias = tokens[0]
        existing = None
        try:
            existing = connection_manager.find_connection_by_nickname(alias)
        except Exception:
            existing = None
        if existing is not None:
            return ResolvedCliConnect(
                connection=existing,
                ssh_argv=['ssh', alias],
                existing=True,
                label=alias,
            )

    command_text = shlex.join(ssh_argv)
    parsed = parse_ssh_command(command_text)
    if parsed is None:
        raise ValueError(f'Could not parse SSH destination: {command_text}')
    if isinstance(parsed, dict) and parsed.get('error'):
        raise ValueError(str(parsed['error']))

    data = dict(parsed)
    data.setdefault('protocol', 'ssh')
    data[CLI_CONNECT_FLAG] = True
    # Remote command tokens from the converter stay in unparsed_args; keep them
    # on the connection for the save dialog. The live session uses ssh_argv.

    connection = Connection(data)
    try:
        connection._connection_manager = connection_manager
    except Exception:
        pass

    # Run exactly the argv the user asked for (OpenSSH semantics in the VTE).
    env = dict(os.environ)
    connection.ssh_connection_cmd = SSHConnectionCommand(
        command=list(ssh_argv),
        env=env,
        use_sshpass=False,
        password=None,
        use_askpass=False,
    )
    connection.ssh_cmd = list(ssh_argv)

    label = (
        data.get('nickname')
        or data.get('host')
        or data.get('hostname')
        or shlex.join(ssh_argv[1:])
    )
    return ResolvedCliConnect(
        connection=connection,
        ssh_argv=list(ssh_argv),
        existing=False,
        label=str(label),
    )


def describe_cli_error(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__
