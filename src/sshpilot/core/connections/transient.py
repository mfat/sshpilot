"""Unsaved ("transient") connections the daemon can open but never stores.

``ssh root@203.0.113.7`` typed into the search box, ``mosh web.example`` or
``telnet 10.0.0.1 2323`` name a target that is not a saved connection. The
daemon opens sessions only by connection id, so such a target is registered
here first and then opened like any other connection: every lookup that goes
through :meth:`ConnectionRepository.get_record` -- the session runtime, the
launch provider, secret lookups -- finds it without knowing it is transient.

Nothing here touches the user's SSH config or ``connections.json``, and the
records never appear in snapshots or listings.

SSH targets keep native OpenSSH semantics. Their Host block is rendered by
the same formatter a saved connection uses and written to a private file
that ends with ``Include <the user's SSH config>``; the launch passes that
file with ``-F``. The block therefore wins for what the user typed, and every
``Host *`` or ``Host *.corp`` block of the real config still applies, exactly
as it would for ``ssh <host>`` on the command line. The Host token is the
destination itself, so patterns match the host the user named.
"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from ...ssh_config_formatter import merged_block_lines
from ...api.models.common import validate_ssh_host_alias
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord
from .ssh_config_loader import load_ssh_configuration

#: Prefix of every transient connection id. The rest is random, so an id
#: cannot realistically name a saved Host as well.
TRANSIENT_ID_PREFIX = "adhoc-"

#: Transient records kept at once. A session holds its connection id for its
#: whole life, so the oldest records are evicted first; 64 is far beyond the
#: number of unsaved targets anyone has open together.
MAX_TRANSIENT_CONNECTIONS = 64


def is_transient_connection_id(connection_id: str) -> bool:
    return str(connection_id or "").startswith(TRANSIENT_ID_PREFIX)


def _host_token(hostname: str, fallback: str) -> str:
    """The Host token for an SSH block: the destination when it is usable."""
    try:
        validate_ssh_host_alias(hostname)
    except ValueError:
        return fallback
    return hostname


class TransientConnections:
    """Thread-safe registry of transient connection records."""

    def __init__(
        self,
        ssh_root: Callable[[], Path],
        *,
        isolated: bool,
        directory: Optional[Path] = None,
        id_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self._ssh_root = ssh_root
        self._isolated = bool(isolated)
        self._directory = Path(directory) if directory is not None else None
        self._owns_directory = directory is None
        self._id_factory = id_factory or (
            lambda: TRANSIENT_ID_PREFIX + secrets.token_hex(6)
        )
        self._records: "OrderedDict[str, ConnectionRecord]" = OrderedDict()
        self._lock = threading.RLock()

    # -- public API ----------------------------------------------------------

    def create(self, data: Mapping[str, Any]) -> ConnectionRecord:
        payload = dict(data or {})
        protocol = str(payload.get("protocol") or "ssh").strip() or "ssh"
        hostname = str(payload.get("hostname") or payload.get("host") or "").strip()
        with self._lock:
            connection_id = self._new_id_locked()
            if protocol == "ssh":
                if not hostname:
                    raise CoreError(
                        ErrorCode.VALIDATION_ERROR, "An SSH destination is required"
                    )
                record = self._create_ssh_locked(connection_id, hostname, payload)
            else:
                payload.update(id=connection_id, nickname=connection_id)
                payload["protocol"] = protocol
                record = ConnectionRecord.from_dict(
                    payload, connection_id=connection_id
                )
            self._records[connection_id] = record
            self._evict_locked()
            return record

    def get(self, connection_id: str) -> Optional[ConnectionRecord]:
        with self._lock:
            return self._records.get(str(connection_id or ""))

    def discard(self, connection_id: str) -> bool:
        with self._lock:
            record = self._records.pop(str(connection_id or ""), None)
            if record is None:
                return False
            self._remove_file(record)
            return True

    def close(self) -> None:
        with self._lock:
            self._records.clear()
            directory, self._directory = self._directory, None
            if directory is not None and self._owns_directory:
                shutil.rmtree(directory, ignore_errors=True)

    # -- internals -----------------------------------------------------------

    def _new_id_locked(self) -> str:
        for _attempt in range(16):
            candidate = self._id_factory()
            if candidate not in self._records:
                return candidate
        raise CoreError(ErrorCode.INTERNAL_ERROR, "No transient id is available")

    def _directory_locked(self) -> Path:
        if self._directory is None:
            self._directory = Path(tempfile.mkdtemp(prefix="sshpilot-adhoc-"))
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        return self._directory

    def _create_ssh_locked(
        self, connection_id: str, hostname: str, payload: Dict[str, Any]
    ) -> ConnectionRecord:
        token = _host_token(hostname, connection_id)
        block = dict(payload)
        block.pop("display_name", None)
        block.update(nickname=token, id=token, hostname=hostname)
        root = Path(self._ssh_root())
        lines = list(merged_block_lines(None, block))
        if root.exists():
            lines.append(f"\nInclude {_quote(str(root))}\n")
        path = self._directory_locked() / f"{connection_id}.conf"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("".join(lines))
        try:
            loaded = load_ssh_configuration(path, isolated=self._isolated)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        record = next(
            (item for item in loaded.connections
             if item.source and Path(item.source) == path and item.id == token),
            None,
        )
        if record is None:
            # A bare destination renders a Host line with no directives, which
            # the loader does not list. Adding ``HostName`` to force it would
            # override the user's own HostName for that alias, so build the
            # record directly: same file, same destination, nothing authored.
            data = dict(block)
            data["__host_tokens"] = [token]
            record = ConnectionRecord.from_dict(data, connection_id=token)
            record.source = str(path)
            record.host = token
        # The id is what every later lookup uses; the Host token stays the
        # destination (``__host_tokens``), which is what the launch connects to.
        record.id = connection_id
        record.nickname = connection_id
        return record

    def _evict_locked(self) -> None:
        while len(self._records) > MAX_TRANSIENT_CONNECTIONS:
            _connection_id, record = self._records.popitem(last=False)
            self._remove_file(record)

    def _remove_file(self, record: ConnectionRecord) -> None:
        directory = self._directory
        source = Path(record.source) if record.source else None
        if directory is None or source is None or source.parent != directory:
            return
        source.unlink(missing_ok=True)


def _quote(value: str) -> str:
    return f'"{value}"' if any(ch.isspace() for ch in value) else value
