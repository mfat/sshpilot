"""Forwarding rule validation (GTK-free)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..errors import FieldError


def normalize_forwarding_rule(rule: Mapping[str, Any]) -> Dict[str, Any]:
    """Normalize dialog/SSH-config field aliases into the core rule shape."""
    raw = dict(rule or {})
    rule_type = str(raw.get("type") or "local").strip().lower()
    listen_address = raw.get("listen_address")
    if listen_address is None:
        listen_address = raw.get("listen_addr", "")
    remote_host = raw.get("remote_host")
    remote_port = raw.get("remote_port")
    if rule_type == "remote":
        # Connection dialog historically stores remote destinations as local_*.
        if remote_host is None and "local_host" in raw:
            remote_host = raw.get("local_host")
        if remote_port is None and "local_port" in raw:
            remote_port = raw.get("local_port")
    socks = bool(raw.get("socks"))
    if rule_type == "remote" and not socks:
        # Empty destination means reverse SOCKS (ssh_config single-arg form).
        if not str(remote_host or "").strip():
            try:
                port_val = int(remote_port or 0)
            except (TypeError, ValueError):
                port_val = 0
            if port_val <= 0:
                socks = True
    return {
        "type": rule_type,
        "listen_address": str(listen_address or "").strip(),
        "listen_port": raw.get("listen_port", 0),
        "remote_host": str(remote_host or "").strip(),
        "remote_port": remote_port if remote_port is not None else 0,
        "enabled": bool(raw.get("enabled", True)),
        "socks": socks,
    }


def forwarding_rule_defaults(rule_type: str) -> Dict[str, Any]:
    """Default editor values for a forwarding rule type."""
    t = (rule_type or "local").strip().lower()
    if t == "dynamic":
        return {
            "type": "dynamic",
            "listen_address": "127.0.0.1",
            "listen_port": 1080,
            "remote_host": "",
            "remote_port": 0,
            "enabled": True,
            "socks": False,
        }
    if t == "remote":
        return {
            "type": "remote",
            "listen_address": "",
            "listen_port": 8080,
            "remote_host": "127.0.0.1",
            "remote_port": 80,
            "enabled": True,
            "socks": False,
        }
    return {
        "type": "local",
        "listen_address": "127.0.0.1",
        "listen_port": 8080,
        "remote_host": "127.0.0.1",
        "remote_port": 80,
        "enabled": True,
        "socks": False,
    }


def validate_forwarding_rule(rule: Mapping[str, Any]) -> List[FieldError]:
    """Validate a single port-forwarding rule; return field errors (empty = ok)."""
    normalized = normalize_forwarding_rule(rule)
    errors: List[FieldError] = []
    rule_type = normalized["type"]
    if rule_type not in ("local", "remote", "dynamic"):
        errors.append(
            FieldError(
                field="type",
                message=f"Unsupported forwarding type: {rule_type}",
            )
        )
        return errors

    try:
        listen_port = int(normalized.get("listen_port") or 0)
    except (TypeError, ValueError):
        errors.append(FieldError(field="listen_port", message="Listen port must be a number"))
        listen_port = -1
    if not (1 <= listen_port <= 65535):
        errors.append(
            FieldError(field="listen_port", message="Listen port must be between 1 and 65535")
        )

    if rule_type == "dynamic" or (rule_type == "remote" and normalized.get("socks")):
        return errors

    remote_host = str(normalized.get("remote_host") or "").strip()
    if not remote_host:
        errors.append(FieldError(field="remote_host", message="Destination host is required"))
    try:
        remote_port = int(normalized.get("remote_port") or 0)
    except (TypeError, ValueError):
        errors.append(FieldError(field="remote_port", message="Remote port must be a number"))
        remote_port = -1
    if not (1 <= remote_port <= 65535):
        errors.append(
            FieldError(
                field="remote_port",
                message="Remote port must be between 1 and 65535",
            )
        )
    return errors
