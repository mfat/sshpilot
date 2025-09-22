"""Helpers for presenting connection host and alias information."""

from __future__ import annotations

from typing import Any


def get_connection_host(connection: Any) -> str:
    """Return the configured hostname for a connection when available."""
    host = getattr(connection, "hostname", None)
    if host:
        return str(host)
    return ""


def get_connection_alias(connection: Any) -> str:
    """Return the alias/nickname used to identify the connection in SSH config."""
    alias = getattr(connection, "host", None)
    if alias:
        return str(alias)
    nickname = getattr(connection, "nickname", "")
    return str(nickname or "")


def format_connection_host_display(connection: Any, include_port: bool = False) -> str:
    """Create a user-facing string describing host/alias details for a connection."""
    username = str(getattr(connection, "username", "") or "")
    hostname = str(getattr(connection, "hostname", "") or "")
    nickname = str(getattr(connection, "nickname", "") or "")
    alias = str(getattr(connection, "host", "") or "")

    used_nickname = False
    if hostname:
        base_target = hostname
    elif nickname:
        base_target = nickname
        used_nickname = True
    else:
        base_target = alias or ""

    display = ""
    if username and base_target:
        display = f"{username}@{base_target}"
    elif base_target:
        display = base_target
    else:
        display = username

    port = getattr(connection, "port", 22)
    if include_port and port and port != 22 and display:
        display = f"{display}:{port}"

    if hostname:
        if alias and alias != hostname:
            return f"{display} ({alias})"
        return display

    if alias and not used_nickname:
        suffix_display = display or alias
        if include_port and port and port != 22 and not display:
            suffix_display = f"{alias}:{port}"
        if username and not display:
            suffix_display = f"{username}@{suffix_display}"
        return f"{suffix_display} (alias)"

    return display
