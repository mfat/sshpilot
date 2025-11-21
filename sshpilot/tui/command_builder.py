"""
Helpers for preparing SSH commands for the TUI.

The curses interface needs to run regular ``ssh`` commands that faithfully
mirror the options configured inside the main GTK application.  This module
reuses the option generation logic from :mod:`sshpilot.ssh_utils` and adds a
lightweight layer to decide which host to target, how to deal with quick
connect commands and how to append remote or local commands.
"""

from __future__ import annotations

import os
import shlex
from typing import Iterable, List, Optional

from sshpilot.ssh_utils import build_connection_ssh_options


def _first_non_empty(values: Iterable[str]) -> str:
    for value in values:
        if value:
            return value
    return ""


def _format_host_for_tunnel(host: Optional[str]) -> str:
    host = (host or "").strip()
    if not host:
        return ""
    if ":" in host and not (host.startswith("[") and host.endswith("]")):
        return f"[{host}]"
    return host


def build_ssh_command(connection, config, *, known_hosts_path: Optional[str] = None) -> List[str]:
    """
    Return the argv list for launching SSH for *connection*.

    Args:
        connection: ``sshpilot.connection_manager.Connection`` instance.
        config: Shared :class:`sshpilot.config.Config` used for preference lookup.
        known_hosts_path: Optional override for ``UserKnownHostsFile`` (used in
            isolated mode).
    """

    quick_cmd = (getattr(connection, "quick_connect_command", "") or "").strip()
    if quick_cmd:
        try:
            return shlex.split(quick_cmd)
        except ValueError as exc:
            raise ValueError("Quick connect command cannot be parsed") from exc

    cmd: List[str] = ["ssh"]

    options = build_connection_ssh_options(connection, config=config)
    if options:
        cmd.extend(options)

    if known_hosts_path:
        abs_path = os.path.abspath(os.path.expanduser(known_hosts_path))
        cmd.extend(["-o", f"UserKnownHostsFile={abs_path}"])

    port = getattr(connection, "port", 22) or 22
    try:
        port_int = int(port)
    except (TypeError, ValueError):
        port_int = 22
    if port_int != 22:
        cmd.extend(["-p", str(port_int)])

    local_cmd = _extract_string(connection, "local_command")
    if local_cmd:
        cmd.extend(["-o", "PermitLocalCommand=yes", "-o", f"LocalCommand={local_cmd}"])

    remote_cmd = _extract_string(connection, "remote_command")
    x11_enabled = bool(getattr(connection, "x11_forwarding", False))
    if x11_enabled and "-X" not in cmd:
        cmd.append("-X")

    cmd.extend(_build_forwarding_args(connection))

    host_label = ""
    if hasattr(connection, "resolve_host_identifier"):
        try:
            host_label = connection.resolve_host_identifier() or ""
        except Exception:
            host_label = ""
    if not host_label:
        host_label = _first_non_empty(
            (
                getattr(connection, "hostname", ""),
                getattr(connection, "host", ""),
                getattr(connection, "nickname", ""),
            )
        )
    host_label = host_label.strip()
    if not host_label:
        raise ValueError("Connection is missing a target host")

    username = (getattr(connection, "username", "") or "").strip()
    if username:
        target = f"{username}@{host_label}"
    else:
        target = host_label

    if remote_cmd:
        cmd.extend(["-t", "-t"])

    cmd.append(target)

    if remote_cmd:
        needs_exec_tail = "exec $SHELL" not in remote_cmd
        final_remote = remote_cmd if not needs_exec_tail else f"{remote_cmd} ; exec $SHELL -l"
        cmd.append(final_remote)

    return cmd


def _extract_string(connection, field: str) -> str:
    value = getattr(connection, field, None)
    if isinstance(value, str) and value.strip():
        return value.strip()

    data = getattr(connection, "data", None)
    if isinstance(data, dict):
        candidate = data.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _build_forwarding_args(connection) -> List[str]:
    forwarding_rules = getattr(connection, "forwarding_rules", []) or []
    if not isinstance(forwarding_rules, list):
        return []

    args: List[str] = []

    for rule in forwarding_rules:
        if not isinstance(rule, dict):
            continue
        if not rule.get("enabled", True):
            continue
        rule_type = rule.get("type", "local")
        listen_addr = _format_host_for_tunnel(rule.get("listen_addr") or "localhost")
        listen_port = rule.get("listen_port")
        remote_host = _format_host_for_tunnel(rule.get("remote_host") or rule.get("local_host") or "localhost")
        remote_port = rule.get("remote_port") or rule.get("local_port")

        if rule_type == "dynamic":
            if listen_port:
                args.extend(["-D", f"{listen_addr}:{listen_port}"])
        elif rule_type == "remote":
            if listen_port and remote_port:
                remote_target = f"{remote_host}:{remote_port}"
                args.extend(["-R", f"{listen_addr}:{listen_port}:{remote_target}"])
        else:  # treat everything else as local forwarding
            if listen_port and remote_port:
                remote_target = f"{remote_host}:{remote_port}"
                args.extend(["-L", f"{listen_addr}:{listen_port}:{remote_target}"])

    return args


__all__ = ["build_ssh_command"]
