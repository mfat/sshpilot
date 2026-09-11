"""Classify Host Info listening ports as well-known web UIs (GTK-free).

v1 uses only a static port/process map over the FULL snapshot. Active HTTP
probing is deferred. Opening a match either uses a local forward (loopback /
unspecified bind) or a direct ``http(s)://address:port/`` URL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import NoReturn, Optional, Tuple

from .api.models.host_info import ListeningPort

logger = logging.getLogger(__name__)

# Ports that almost always speak HTTP(S) when exposed over SSH.
_PORT_SCHEME: dict[int, str] = {
    80: "http",
    443: "https",
    3000: "http",
    8000: "http",
    8080: "http",
    8443: "https",
    8888: "http",
    9090: "https",
    9091: "http",
    9443: "https",
}

_PORT_LABEL: dict[int, str] = {
    80: "HTTP",
    443: "HTTPS",
    3000: "Web UI",
    8000: "Web UI",
    8080: "HTTP",
    8443: "HTTPS",
    8888: "Jupyter",
    9090: "Cockpit",
    9091: "Prometheus",
    9443: "HTTPS",
}

# Process basenames (ss/netstat) that imply a browser UI.
_PROCESS_SCHEME: dict[str, Tuple[str, str]] = {
    "cockpit-ws": ("https", "Cockpit"),
    "nginx": ("http", "nginx"),
    "httpd": ("http", "Apache"),
    "apache2": ("http", "Apache"),
    "caddy": ("http", "Caddy"),
    "code-server": ("http", "code-server"),
    "grafana": ("http", "Grafana"),
    "grafana-server": ("http", "Grafana"),
    "traefik": ("http", "Traefik"),
    "minio": ("http", "MinIO"),
}

# Listeners that never speak HTTP(S). A well-known web port alone must not
# override these — e.g. sshd on 8080 must not get an "Open in browser" action.
_NON_WEB_PROCESSES: frozenset[str] = frozenset(
    {
        "sshd",
        "ssh",
        "ssh-agent",
        "systemd",
        "systemd-resolved",
        "systemd-networkd",
        "dbus-daemon",
        "dbus-broker",
        "chronyd",
        "ntpd",
        "rpcbind",
        "cupsd",
    }
)

_UNSPECIFIED_OR_LOOPBACK = frozenset(
    {
        "",
        "*",
        "0.0.0.0",
        "::",
        "::0",
        "127.0.0.1",
        "::1",
        "localhost",
    }
)


@dataclass(frozen=True)
class WebUiMatch:
    """A listening port the frontend will offer to open in a browser."""

    label: str
    scheme: str  # "http" or "https"
    port: int
    address: str
    process: str = ""


def classify_listening_port(entry: ListeningPort) -> Optional[WebUiMatch]:
    """Return a web-UI match for ``entry``, or ``None`` if unknown."""

    process = (entry.process or "").strip()
    base = process.rsplit("/", 1)[-1].lower()
    if base in _NON_WEB_PROCESSES:
        return None

    scheme = ""
    label = ""

    if base in _PROCESS_SCHEME:
        scheme, label = _PROCESS_SCHEME[base]
    if entry.port in _PORT_SCHEME:
        scheme = _PORT_SCHEME[entry.port]
        label = _PORT_LABEL.get(entry.port, label or "Web UI")
    if not scheme:
        return None
    if not label:
        label = process or f"port {entry.port}"
    return WebUiMatch(
        label=label,
        scheme=scheme,
        port=entry.port,
        address=entry.address or "",
        process=process,
    )


def needs_local_forward(address: str) -> bool:
    """Whether opening this bind address requires an SSH local forward.

    Loopback and unspecified binds cannot be opened as a remote URL from the
    client's browser; a forward to ``127.0.0.1:<local>`` is required.
    """

    host = (address or "").strip().lower().strip("[]")
    if host in _UNSPECIFIED_OR_LOOPBACK:
        return True
    if host.startswith("127."):
        return True
    if host.startswith("fe80:"):
        return True
    return False


def browser_url(*, scheme: str, address: str, port: int, local_port: Optional[int] = None) -> str:
    """Build the URL the system browser should open.

    When ``local_port`` is set, the URL always targets the forwarded localhost
    listener regardless of the remote bind address.
    """

    if local_port is not None:
        return f"{scheme}://127.0.0.1:{int(local_port)}/"
    host = (address or "").strip().strip("[]")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{scheme}://{host}:{int(port)}/"


def ensure_daemon_local_forward(
    client,
    connection_id: str,
    remote_port: int,
    *,
    timeout: float = 15.0,
    destination_host: str = "127.0.0.1",
) -> int:
    """Blocking: open (or wait for) a daemon local forward; return local port.

    Mirrors the plugin ``ensure_local_forward`` path without the plugin SDK.
    Raises ``RuntimeError`` on failure.
    """

    import time

    from .api.capabilities import Capability
    from .api.models.operations import ForwardState, ForwardType, OpenForwardRequest
    from .extended_service_policy import (
        daemon_forward_unavailable_message,
        format_forward_failure_detail,
    )
    from .port_utils import allocate_ephemeral_local_port, find_available_port

    def _fail(message: str, cause: Optional[BaseException] = None) -> NoReturn:
        logger.warning("Host Info local forward failed: %s", message)
        if cause is None:
            raise RuntimeError(message)
        raise RuntimeError(message) from cause

    try:
        supported = client.get_capabilities().supported
    except Exception as exc:
        _fail(
            daemon_forward_unavailable_message(
                detail=f"capabilities unavailable ({type(exc).__name__}: {exc})"
            ),
            exc,
        )
    required = {
        Capability.FORWARDS_READ,
        Capability.FORWARDS_WRITE,
        Capability.FORWARDS_LOCAL,
    }
    missing = required - supported
    if missing:
        names = ", ".join(sorted(c.value for c in missing))
        _fail(
            daemon_forward_unavailable_message(
                detail=f"missing capabilities: {names}"
            )
        )

    # Reuse an active forward to the same remote destination when present.
    try:
        for item in client.list_forwards():
            if (
                str(item.connection_id) == str(connection_id)
                and item.type is ForwardType.LOCAL
                and item.state is ForwardState.ACTIVE
                and int(item.destination_port or 0) == int(remote_port)
                and (item.destination_host or "127.0.0.1") == destination_host
                and item.bind_port
            ):
                return int(item.bind_port)
    except Exception:
        pass

    local_port = allocate_ephemeral_local_port("127.0.0.1")
    if not local_port:
        # Last resort: scan high ports without preferring the remote service port.
        local_port = find_available_port(49152)
    if not local_port:
        _fail(daemon_forward_unavailable_message(detail="no free local port"))
    try:
        summary = client.open_forward(
            OpenForwardRequest(
                connection_id=connection_id,
                type=ForwardType.LOCAL,
                bind_host="127.0.0.1",
                bind_port=int(local_port),
                destination_host=destination_host,
                destination_port=int(remote_port),
            )
        )
    except Exception as exc:
        _fail(f"open_forward failed: {exc}", exc)

    deadline = time.monotonic() + max(1.0, float(timeout))
    forward_id = summary.id
    while time.monotonic() < deadline:
        try:
            current = client.get_forward(forward_id)
        except Exception as exc:
            _fail(f"get_forward failed: {exc}", exc)
        if current.state is ForwardState.ACTIVE:
            return int(current.bind_port or local_port)
        if current.state in {ForwardState.FAILED, ForwardState.CLOSED}:
            _fail(format_forward_failure_detail(current))
        time.sleep(0.1)
    _fail(
        f"timed out waiting for forward {forward_id} to become ACTIVE "
        f"(local {local_port} -> {destination_host}:{remote_port})"
    )
