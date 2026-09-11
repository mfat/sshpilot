"""Helpers for SessionType-none (port-forwarding-only) terminal UX.

Pure logic lives here so unit tests need no GTK. The terminal overlay and
sidebar attach ``forwarding_only`` / rule lists onto connection projections.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional, Sequence, Tuple


def extra_ssh_config_has_session_type_none(extra_ssh_config: Optional[str]) -> bool:
    """True when ``extra_ssh_config`` authors ``SessionType none`` (any case)."""
    if not extra_ssh_config:
        return False
    for raw_line in str(extra_ssh_config).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        key, value = parts[0], parts[1].strip()
        if key.casefold() != "sessiontype":
            continue
        # Strip optional quotes OpenSSH accepts around values.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value.casefold() == "none":
            return True
    return False


def connection_forwarding_only(connection: Any) -> Optional[bool]:
    """Return True/False when known, or None when SessionType has not been resolved.

    Prefer the cached ``forwarding_only`` attribute (set when editor details are
    fetched). Fall back to parsing ``extra_ssh_config`` when present on the
    projection or its ``data`` dict.
    """
    if connection is None:
        return None
    flagged = getattr(connection, "forwarding_only", None)
    if flagged is not None:
        return bool(flagged)
    extra = getattr(connection, "extra_ssh_config", None)
    if extra is None:
        data = getattr(connection, "data", None)
        if isinstance(data, Mapping):
            extra = data.get("extra_ssh_config")
    if extra is None:
        return None
    return extra_ssh_config_has_session_type_none(str(extra))


def apply_forwarding_only_flag(connection: Any, extra_ssh_config: Optional[str]) -> bool:
    """Cache SessionType-none on *connection*; return the resolved flag."""
    flag = extra_ssh_config_has_session_type_none(extra_ssh_config)
    try:
        object.__setattr__(connection, "forwarding_only", flag)
    except Exception:
        try:
            setattr(connection, "forwarding_only", flag)
        except Exception:
            pass
    return flag


def connection_forwarding_rules(connection: Any) -> Tuple[Mapping[str, Any], ...]:
    """Enabled forwarding rules attached to *connection*, as dicts."""
    if connection is None:
        return ()
    raw = getattr(connection, "forwarding_rules", None)
    if raw is None:
        data = getattr(connection, "data", None)
        if isinstance(data, Mapping):
            raw = data.get("forwarding_rules")
    if not raw:
        return ()
    rules: List[Mapping[str, Any]] = []
    for item in raw:
        if isinstance(item, Mapping):
            if item.get("enabled", True) is False:
                continue
            rules.append(item)
            continue
        # ForwardingRule dataclass (or similar)
        try:
            from .api.models.connections import ForwardingRule, forwarding_rule_to_dict

            if isinstance(item, ForwardingRule):
                d = forwarding_rule_to_dict(item)
                if d.get("enabled", True) is False:
                    continue
                rules.append(d)
        except Exception:
            continue
    return tuple(rules)


def kind_label_for_rule(rule: Mapping[str, Any]) -> str:
    rtype = str(rule.get("type") or "local").casefold()
    if rtype == "remote":
        return "R"
    if rtype == "dynamic":
        return "D"
    return "L"


def format_bind_endpoint(rule: Mapping[str, Any]) -> str:
    addr = str(rule.get("listen_addr") or "").strip()
    port = rule.get("listen_port")
    try:
        port_s = str(int(port))
    except (TypeError, ValueError):
        port_s = str(port or "")
    if addr:
        return f"{addr}:{port_s}"
    return port_s or "?"


def format_destination_endpoint(rule: Mapping[str, Any]) -> str:
    rtype = str(rule.get("type") or "local").casefold()
    if rtype == "dynamic" or rule.get("socks"):
        return "dynamic"
    if rtype == "remote":
        host = str(rule.get("local_host") or rule.get("remote_host") or "").strip()
        port = rule.get("local_port") or rule.get("remote_port")
    else:
        host = str(rule.get("remote_host") or "").strip()
        port = rule.get("remote_port")
    try:
        port_s = str(int(port)) if port not in (None, "") else ""
    except (TypeError, ValueError):
        port_s = str(port or "")
    if host and port_s:
        return f"{host}:{port_s}"
    return host or port_s or "?"


def format_forwarding_rule_rows(
    rules: Sequence[Mapping[str, Any]],
    *,
    status: str,
) -> List[dict]:
    """Display rows for the overlay list.

    *status* is the shared session status string (``active`` / ``failed``);
    config-static Host forwards share the terminal session lifecycle.
    """
    rows: List[dict] = []
    for rule in rules:
        rows.append(
            {
                "kind": kind_label_for_rule(rule),
                "bind": format_bind_endpoint(rule),
                "destination": format_destination_endpoint(rule),
                "status": status,
            }
        )
    return rows


def forwarding_only_tab_title(name: str) -> str:
    """Stable tab title for a forwarding-only session."""
    # Late import keeps this module importable without gettext wiring in tests.
    try:
        from gettext import gettext as _
    except Exception:  # pragma: no cover
        def _(s):  # type: ignore[misc]
            return s

    display = (name or "").strip() or _("Connection")
    return _("{name} · forwards").format(name=display)


def forwarding_only_subtitle(name: str) -> str:
    try:
        from gettext import gettext as _
    except Exception:  # pragma: no cover
        def _(s):  # type: ignore[misc]
            return s

    display = (name or "").strip() or _("Connection")
    return _("{name} · no shell (SessionType none)").format(name=display)
