"""Forwarding rule validation (GTK-free)."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

from ..errors import FieldError


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
        }
    if t == "remote":
        return {
            "type": "remote",
            "listen_address": "127.0.0.1",
            "listen_port": 8080,
            "remote_host": "127.0.0.1",
            "remote_port": 80,
            "enabled": True,
        }
    return {
        "type": "local",
        "listen_address": "127.0.0.1",
        "listen_port": 8080,
        "remote_host": "127.0.0.1",
        "remote_port": 80,
        "enabled": True,
    }


def validate_forwarding_rule(rule: Mapping[str, Any]) -> List[FieldError]:
    """Validate a single port-forwarding rule; return field errors (empty = ok)."""
    errors: List[FieldError] = []
    rule_type = str(rule.get("type") or "local").strip().lower()
    if rule_type not in ("local", "remote", "dynamic"):
        errors.append(
            FieldError(
                field="type",
                message=f"Unsupported forwarding type: {rule_type}",
            )
        )
        return errors

    try:
        listen_port = int(rule.get("listen_port") or 0)
    except (TypeError, ValueError):
        errors.append(FieldError(field="listen_port", message="Listen port must be a number"))
        listen_port = -1
    if not (1 <= listen_port <= 65535):
        errors.append(
            FieldError(field="listen_port", message="Listen port must be between 1 and 65535")
        )

    if rule_type != "dynamic":
        remote_host = str(rule.get("remote_host") or "").strip()
        if not remote_host:
            errors.append(FieldError(field="remote_host", message="Destination host is required"))
        try:
            remote_port = int(rule.get("remote_port") or 0)
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
