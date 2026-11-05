"""SSH Pilot-specific edges: client method → wire method → daemon handler.

Built from the same authoritative AST readers as ``trace_api_method`` /
``check_api_drift`` (:mod:`sshpilot.mcp.dev.api_surface`), so the index and
the existing tools can never disagree. Pseudo-nodes:

* ``wire:<method>``        — a daemon wire method (``wire:sessions.open``)
* ``capability:<value>``   — a capability value (``capability:sessions.open``)

Edges are stored in ``domain_edges`` and rebuilt only when one of the input
files changes (digest over their indexed hashes plus the generated schema).
"""

from __future__ import annotations

import hashlib
import sqlite3
from typing import TYPE_CHECKING, List, Optional, Tuple

from ..._scope import ScopeError
from .. import api_surface
from .models import VERIFIED_STATIC

if TYPE_CHECKING:  # pragma: no cover
    from .index import DevIndex

#: Source files the domain extractors read.
DOMAIN_INPUTS = (
    api_surface.CLIENT_RELPATH,
    api_surface.DAEMON_CLIENT_RELPATH,
    api_surface.DISPATCH_RELPATH,
    api_surface.UNSUPPORTED_RELPATH,
    api_surface.IMPLEMENTED_RELPATH,
    api_surface.CAPABILITIES_RELPATH,
)

CLIENT_CLASS_FQ = "sshpilot.api.client.SshPilotClient"
DAEMON_CLIENT_CLASS_FQ = "sshpilot.api.daemon_client.DaemonClient"


def wire_node(method: str) -> str:
    return f"wire:{method}"


def capability_node(value: str) -> str:
    return f"capability:{value}"


def domain_inputs_digest(index: "DevIndex", db: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for path in DOMAIN_INPUTS:
        row = db.execute("SELECT sha256 FROM files WHERE path=?", (path,)).fetchone()
        digest.update(f"{path}={row[0] if row else '-'};".encode())
    try:
        schema = index.scope.resolve(api_surface.SCHEMA_RELPATH).read_bytes()
        digest.update(hashlib.sha256(schema).hexdigest().encode())
    except (OSError, ScopeError):
        digest.update(b"schema=-")
    return digest.hexdigest()


def _method_location(db: sqlite3.Connection, fqname: str) -> Optional[Tuple[str, int]]:
    row = db.execute(
        "SELECT path, line FROM symbols WHERE fqname=? ORDER BY path, line LIMIT 1", (fqname,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def _handler_fq(db: sqlite3.Connection, handler: str) -> Optional[Tuple[str, str, int]]:
    row = db.execute(
        "SELECT fqname, path, line FROM symbols WHERE path=? AND name=? AND kind IN ('method','async_method') "
        "ORDER BY line LIMIT 1",
        (api_surface.DISPATCH_RELPATH, handler),
    ).fetchone()
    return (row[0], row[1], row[2]) if row else None


def rebuild_domain_edges(index: "DevIndex", db: sqlite3.Connection) -> bool:
    """Recompute domain edges inside the caller's transaction."""
    digest = domain_inputs_digest(index, db)
    db.execute("DELETE FROM domain_edges")
    scope = index.scope
    edges: List[tuple] = []
    try:
        client_wire = api_surface.client_to_wire(scope)
        handlers = api_surface.wire_handlers(scope)
        wire_caps = api_surface.wire_capabilities(scope)
        client_contract = api_surface.reconcile_client_contract(scope)
    except (ScopeError, SyntaxError, ValueError, OSError) as error:
        db.execute("INSERT OR REPLACE INTO meta VALUES ('domain_status', ?)", (f"error: {type(error).__name__}: {error}",))
        db.execute("INSERT OR REPLACE INTO meta VALUES ('domain_digest', ?)", (digest,))
        return True

    for method in sorted(client_wire):
        client_fq = f"{DAEMON_CLIENT_CLASS_FQ}.{method}"
        location = _method_location(db, client_fq) or (api_surface.DAEMON_CLIENT_RELPATH, 0)
        for wire in client_wire[method]:
            edges.append((client_fq, "api_to_wire", wire_node(wire), location[0], location[1], VERIFIED_STATIC, "literal-request-call", ""))
        abstract_fq = f"{CLIENT_CLASS_FQ}.{method}"
        abstract = _method_location(db, abstract_fq)
        if abstract is not None:
            edges.append((abstract_fq, "implemented_by", client_fq, abstract[0], abstract[1], VERIFIED_STATIC, "same-name-override", ""))

    for wire in sorted(handlers):
        handler = _handler_fq(db, handlers[wire])
        if handler is not None:
            edges.append((wire_node(wire), "wire_to_handler", handler[0], handler[1], handler[2], VERIFIED_STATIC, "HANDLERS-literal", handlers[wire]))
        else:
            edges.append((wire_node(wire), "wire_to_handler", f"?.{handlers[wire]}", api_surface.DISPATCH_RELPATH, 0, "unresolved", "HANDLERS-literal", handlers[wire]))
    for wire in sorted(wire_caps):
        capability = wire_caps[wire]
        if capability:
            edges.append((wire_node(wire), "uses_capability", capability_node(capability), api_surface.DISPATCH_RELPATH, 0, VERIFIED_STATIC, "DAEMON_METHOD_CAPABILITIES", ""))
    for method in sorted(client_contract):
        capability = client_contract[method].get("capability")
        if capability:
            location = _method_location(db, f"{CLIENT_CLASS_FQ}.{method}") or (api_surface.CLIENT_RELPATH, 0)
            edges.append((f"{CLIENT_CLASS_FQ}.{method}", "uses_capability", capability_node(capability), location[0], location[1], VERIFIED_STATIC, "client-capability-table", client_contract[method].get("status", "")))

    db.executemany("INSERT INTO domain_edges VALUES (?,?,?,?,?,?,?,?)", edges)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('domain_status', ?)", (f"ok: {len(edges)} edges",))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('domain_digest', ?)", (digest,))
    return True
