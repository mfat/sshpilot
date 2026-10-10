"""Canonical assistant-access settings for AI MCP clients.

Persisted under ``assistant_access`` in ``config.json``. The daemon service
is the authority; the runtime MCP server only reads the snapshot through the
public API and treats it as a ceiling over environment opt-ins.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

DEFAULT_ASSISTANT_ACCESS: Dict[str, Any] = {
    "enabled": False,
    "allowed_connection_ids": [],
    "operate": True,
    "mutate": False,
    "content": False,
    "http_listen": False,
    "http_port": 0,
    "http_token": "",
}

CONFIG_SECTION = "assistant_access"
MAX_HTTP_PORT = 65535


def generate_http_token() -> str:
    """Return a new localhost MCP bearer token."""
    return secrets.token_urlsafe(32)


def normalize_assistant_access(
    raw: Mapping[str, Any] | None,
    *,
    known_connection_ids: Iterable[str] | None = None,
    generate_token_if_missing: bool = False,
) -> Dict[str, Any]:
    """Return a normalized assistant-access dict (mutates nothing)."""
    source = dict(DEFAULT_ASSISTANT_ACCESS)
    if isinstance(raw, Mapping):
        source.update({key: raw[key] for key in DEFAULT_ASSISTANT_ACCESS if key in raw})

    enabled = bool(source.get("enabled"))
    operate = bool(source.get("operate"))
    mutate = bool(source.get("mutate"))
    content = bool(source.get("content"))
    http_listen = bool(source.get("http_listen"))
    # Mutate implies operate: a write-only ceiling is meaningless.
    if mutate and not operate:
        operate = True
    # Listen requires the master switch; keep the flag but daemon will not
    # start the sidecar until enabled is also true.
    http_port = _normalize_http_port(source.get("http_port"))
    http_token = _normalize_http_token(
        source.get("http_token"),
        generate_if_missing=generate_token_if_missing,
    )

    ids = _normalize_id_list(source.get("allowed_connection_ids"))
    if known_connection_ids is not None:
        known = {str(item) for item in known_connection_ids}
        ids = [item for item in ids if item in known]

    return {
        "enabled": enabled,
        "allowed_connection_ids": ids,
        "operate": operate,
        "mutate": mutate,
        "content": content,
        "http_listen": http_listen,
        "http_port": http_port,
        "http_token": http_token,
    }


def compute_assistant_access_revision(data: Mapping[str, Any]) -> str:
    """Deterministic revision from the semantic persisted fields only."""
    payload = {
        "enabled": bool(data.get("enabled")),
        "allowed_connection_ids": list(data.get("allowed_connection_ids") or []),
        "operate": bool(data.get("operate")),
        "mutate": bool(data.get("mutate")),
        "content": bool(data.get("content")),
        "http_listen": bool(data.get("http_listen")),
        "http_port": int(data.get("http_port") or 0),
        "http_token": str(data.get("http_token") or ""),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _normalize_http_port(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("http_port must be an integer")
    if not 0 <= value <= MAX_HTTP_PORT:
        raise ValueError(f"http_port must be between 0 and {MAX_HTTP_PORT}")
    return value


def _normalize_http_token(value: Any, *, generate_if_missing: bool) -> str:
    if value is None or value == "":
        if generate_if_missing:
            return generate_http_token()
        return ""
    if type(value) is not str:
        raise TypeError("http_token must be a string")
    cleaned = value.strip()
    if not cleaned or "\x00" in cleaned or any(ch.isspace() for ch in cleaned):
        raise ValueError("http_token must be a non-empty token without whitespace")
    if len(cleaned) > 256:
        raise ValueError("http_token is too long")
    return cleaned


def _normalize_id_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items: Sequence[Any] = (value,)
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise TypeError("allowed_connection_ids must be a list of connection ids")
    seen: set[str] = set()
    result: List[str] = []
    for item in items:
        if type(item) is not str or not item.strip():
            raise ValueError("connection ids must be non-empty strings")
        cleaned = item.strip()
        if "\x00" in cleaned:
            raise ValueError("connection ids must not contain NUL")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
    return result


def assistant_access_from_settings(
    config: Mapping[str, Any],
    *,
    known_connection_ids: Iterable[str] | None = None,
) -> Tuple[Dict[str, Any], str]:
    """Load normalized fields and revision from a settings tree."""
    section = config.get(CONFIG_SECTION)
    normalized = normalize_assistant_access(
        section if isinstance(section, Mapping) else None,
        known_connection_ids=known_connection_ids,
        generate_token_if_missing=False,
    )
    return normalized, compute_assistant_access_revision(normalized)


def mcp_http_url(bound_port: Optional[int]) -> str:
    """Build the Streamable HTTP MCP endpoint URL for a bound port."""
    if bound_port is None or int(bound_port) <= 0:
        return ""
    return f"http://127.0.0.1:{int(bound_port)}/mcp"
