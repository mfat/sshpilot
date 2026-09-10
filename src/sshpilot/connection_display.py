"""Helpers for presenting connection host and alias information."""

from __future__ import annotations

import html
from typing import Any, List

from gettext import gettext as _

# Stand-in shown wherever host details are suppressed by the privacy toggle
# (the eye button in the sidebar header).
HIDDEN_HOST_PLACEHOLDER = "••••••••••"


def _escape_markup(text: str) -> str:
    """Escape text for Pango markup (same characters as GLib.markup_escape_text)."""
    return html.escape(text, quote=False)


def _proxy_jump_display(connection: Any) -> str:
    """Format ProxyJump as a comma-separated chain, or empty if unset."""
    raw = getattr(connection, "proxy_jump", None)
    if raw is None and hasattr(connection, "data") and isinstance(connection.data, dict):
        raw = connection.data.get("proxy_jump")
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace(" ", ",").split(",") if p.strip()]
    elif isinstance(raw, (list, tuple)):
        parts = [str(p).strip() for p in raw if str(p).strip()]
    else:
        parts = []
    return ", ".join(parts)


def _tags_display(connection: Any) -> str:
    """Format connection tags as a comma-separated list, or empty if none."""
    tags = getattr(connection, "tags", None) or []
    if not isinstance(tags, (list, tuple)):
        return ""
    parts = [str(t).strip() for t in tags if str(t).strip()]
    return ", ".join(parts)


def format_connection_row_tooltip_markup(
    connection: Any,
    *,
    hide_hosts: bool = False,
    max_forwarding_lines: int = 4,
) -> str:
    """Build Pango markup for a sidebar connection row tooltip.

    Layout::

        <b>Display Name</b>
        <span alpha='70%'>user@host:port</span>
        <b>Tags:</b> production, web
        <b>ProxyJump:</b> bastion
        Local 8080 → …

    Host details are omitted when ``hide_hosts`` is set. Forwarding lines are
    included when the connection carries ``forwarding_rules`` (sidebar attaches
    them asynchronously). Values are escaped for Pango markup.
    """
    title = str(
        getattr(connection, "display_name", None)
        or getattr(connection, "nickname", "")
        or ""
    ).strip()
    lines: List[str] = []
    if title:
        lines.append(f"<b>{_escape_markup(title)}</b>")

    if not hide_hosts:
        host_display = format_connection_host_display(connection, include_port=True)
        if host_display and host_display != title:
            lines.append(
                f"<span alpha='70%'>{_escape_markup(host_display)}</span>"
            )

    tags = _tags_display(connection)
    if tags:
        lines.append(
            f"<b>{_escape_markup(_('Tags:'))}</b> {_escape_markup(tags)}"
        )

    proxy = _proxy_jump_display(connection)
    if proxy and not hide_hosts:
        lines.append(
            f"<b>{_escape_markup(_('ProxyJump:'))}</b> {_escape_markup(proxy)}"
        )

    if max_forwarding_lines > 0:
        rules = getattr(connection, "forwarding_rules", None)
        if rules:
            from sshpilot.port_utils import format_forwarding_rules

            for rule_line in format_forwarding_rules(
                rules, max_lines=max_forwarding_lines
            ):
                lines.append(
                    f"<span alpha='70%'>{_escape_markup(rule_line)}</span>"
                )

    return "\n".join(lines)


def hosts_hidden(window: Any) -> bool:
    """Report whether ``window`` currently hides host details."""
    return bool(getattr(window, "_hide_hosts", False))


def mask_host_display(text: str, hide: bool) -> str:
    """Replace host text with the privacy placeholder while hosts are hidden."""
    if hide and text:
        return HIDDEN_HOST_PLACEHOLDER
    return text


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
        return display

    if alias and not used_nickname:
        suffix_display = display or alias
        if include_port and port and port != 22 and not display:
            suffix_display = f"{alias}:{port}"
        if username and not display:
            suffix_display = f"{username}@{suffix_display}"
        return f"{suffix_display} (alias)"

    return display
