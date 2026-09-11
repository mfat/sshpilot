"""Turn host-info DTOs into a JSON view model for the WebKit info tab.

GTK-free and WebKit-free so the same payload can be unit-tested headlessly.
Formatting and severity live here — the shell only renders what it is given.
"""

from __future__ import annotations

from gettext import gettext as _, ngettext
from typing import Any, Dict, List, Optional

from .api.models.host_info import (
    CpuUtilization,
    HostInfoSnapshot,
    LiveSample,
    LoadAverage,
    MemoryInfo,
)
from .core.host_info.rates import cpu_utilization_by_name, interface_rates

_CAREFUL_FRACTION = 0.50
_WARN_FRACTION = 0.70
_CRITICAL_FRACTION = 0.90
_CAREFUL_CELSIUS = 60.0
_WARN_CELSIUS = 70.0
_CRITICAL_CELSIUS = 80.0
_CAREFUL_LOAD = 0.7
_WARN_LOAD = 1.0
_CRITICAL_LOAD = 5.0
_INODE_NOTICE_FRACTION = 0.90


def _usage_severity(fraction: Optional[float]) -> str:
    if fraction is None:
        return "unknown"
    if fraction >= _CRITICAL_FRACTION:
        return "critical"
    if fraction >= _WARN_FRACTION:
        return "warn"
    if fraction >= _CAREFUL_FRACTION:
        return "careful"
    return "ok"


def _temp_severity(celsius: Optional[float]) -> str:
    if celsius is None:
        return "unknown"
    if celsius >= _CRITICAL_CELSIUS:
        return "critical"
    if celsius >= _WARN_CELSIUS:
        return "warn"
    if celsius >= _CAREFUL_CELSIUS:
        return "careful"
    return "ok"


def _load_severity(value: Optional[float], processors: Optional[int]) -> str:
    if value is None or not processors:
        return "unknown"
    per_cpu = value / processors
    if per_cpu >= _CRITICAL_LOAD:
        return "critical"
    if per_cpu >= _WARN_LOAD:
        return "warn"
    if per_cpu >= _CAREFUL_LOAD:
        return "careful"
    return "ok"


def _format_bytes(value: Optional[float]) -> str:
    if value is None:
        return _("N/A")
    if value < 1024:
        return _("%d B") % int(value)
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        scaled /= 1024
        if scaled < 1024 or unit == "PiB":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_bytes_si(value: Optional[float]) -> str:
    if value is None:
        return _("N/A")
    if value < 1000:
        return _("%d B") % int(value)
    scaled = float(value)
    for unit in ("KB", "MB", "GB", "TB", "PB"):
        scaled /= 1000
        if scaled < 1000 or unit == "PB":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_rate(value: Optional[float]) -> str:
    if value is None:
        return "—"
    if value < 1024:
        return _("%d B/s") % int(value)
    scaled = float(value)
    for unit in ("KiB/s", "MiB/s", "GiB/s", "TiB/s"):
        scaled /= 1024
        if scaled < 1024 or unit == "TiB/s":
            precision = 1 if scaled < 100 else 0
            return f"{scaled:.{precision}f} {unit}"
    return ""


def _format_uptime(seconds: Optional[float]) -> str:
    if seconds is None:
        return _("N/A")
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    parts: List[str] = []
    if days:
        parts.append(ngettext("%d day", "%d days", days) % days)
    if hours:
        parts.append(ngettext("%d hour", "%d hours", hours) % hours)
    if minutes:
        parts.append(ngettext("%d minute", "%d minutes", minutes) % minutes)
    if not parts:
        return _("Less than a minute")
    return _(", ").join(parts)


def _format_frequency(megahertz: Optional[float]) -> str:
    if megahertz is None:
        return _("N/A")
    if megahertz >= 1000:
        return _("%.2f GHz") % (megahertz / 1000)
    return _("%.0f MHz") % megahertz


def _format_percent(fraction: Optional[float]) -> str:
    return "—" if fraction is None else _("%d%%") % int(round(fraction * 100))


def _or_na(value: str) -> str:
    return value if value else _("N/A")


def _processor_text(snapshot: HostInfoSnapshot) -> str:
    cpu = snapshot.cpu
    threads = cpu.total_threads
    if cpu.cores_per_socket and cpu.sockets:
        cores = cpu.cores_per_socket * cpu.sockets
        topology = _("%(cores)d cores · %(threads)d threads · %(sockets)d sockets") % {
            "cores": cores,
            "threads": threads or cores,
            "sockets": cpu.sockets,
        }
    elif threads:
        topology = ngettext("%d core", "%d cores", threads) % threads
    else:
        topology = ""
    if cpu.model and topology:
        return _("%(model)s (%(topology)s)") % {
            "model": cpu.model,
            "topology": topology,
        }
    return _or_na(cpu.model or topology)


def _load_detail(load: Optional[LoadAverage]) -> str:
    if load is None:
        return ""
    return _("load %(one).2f · %(five).2f · %(fifteen).2f") % {
        "one": load.one,
        "five": load.five,
        "fifteen": load.fifteen,
    }


def _swap_detail(memory: MemoryInfo) -> str:
    if not memory.swap_total_bytes:
        return ""
    return _("swap %(used)s / %(total)s") % {
        "used": _format_bytes(memory.swap_used_bytes),
        "total": _format_bytes(memory.swap_total_bytes),
    }


def _gauge(
    title: str,
    fraction: Optional[float],
    detail: str = "",
    subtitle: str = "",
    *,
    severity: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "title": title,
        "fraction": None if fraction is None else max(0.0, min(1.0, float(fraction))),
        "percent": _format_percent(fraction),
        "detail": detail,
        "subtitle": subtitle,
        "severity": severity or _usage_severity(fraction),
    }


def _kv(label: str, value: str, *, mono: bool = False) -> Dict[str, Any]:
    return {"label": label, "value": value, "mono": mono}


def snapshot_payload(
    snapshot: HostInfoSnapshot,
    *,
    title: str,
    subtitle: str,
) -> Dict[str, Any]:
    """Build the full view model for one completed FULL gather."""

    memory = snapshot.memory
    used = memory.used_bytes
    mem_fraction = (
        used / memory.total_bytes if used is not None and memory.total_bytes else None
    )
    root = snapshot.root_filesystem
    root_detail = root_device = ""
    if root is not None:
        root_detail = _("%(used)s / %(total)s") % {
            "used": _format_bytes_si(root.used_bytes),
            "total": _format_bytes_si(root.size_bytes),
        }
        root_device = (
            _("%(fstype)s on %(device)s")
            % {"fstype": root.fstype, "device": root.device}
            if root.fstype
            else root.device
        )

    overview_rows = [_kv(_("Hostname"), _or_na(snapshot.hostname), mono=True)]
    if snapshot.device_model:
        overview_rows.append(_kv(_("Device"), snapshot.device_model))
    overview_rows.extend(
        [
            _kv(_("Operating system"), _or_na(snapshot.os_pretty_name)),
            _kv(_("Kernel"), _or_na(snapshot.kernel), mono=True),
            _kv(_("Processor"), _processor_text(snapshot)),
            _kv(
                _("CPU frequency"),
                _format_frequency(snapshot.cpu.frequency_mhz),
                mono=True,
            ),
            _kv(_("Memory"), _format_bytes(memory.total_bytes or None), mono=True),
            _kv(_("Uptime"), _format_uptime(snapshot.uptime_seconds)),
            _kv(_("Booted"), _or_na(snapshot.boot_time)),
        ]
    )

    processors = snapshot.cpu.logical_processors
    load = snapshot.load_average
    load_cards = []
    for label, value in (
        (_("1 min"), load.one if load else None),
        (_("5 min"), load.five if load else None),
        (_("15 min"), load.fifteen if load else None),
    ):
        fraction = value / processors if value is not None and processors else None
        load_cards.append(
            {
                "title": label,
                "value": "—" if value is None else f"{value:.2f}",
                "fraction": None if fraction is None else max(0.0, min(1.0, fraction)),
                "severity": _load_severity(value, processors),
            }
        )

    filesystems = []
    for fs in snapshot.filesystems:
        inode_fraction = fs.inodes_used_fraction
        inode_note = ""
        if inode_fraction is not None and inode_fraction >= _INODE_NOTICE_FRACTION:
            inode_note = _("inodes %(pct)s") % {
                "pct": _format_percent(inode_fraction)
            }
        filesystems.append(
            {
                "mount": fs.mount_point,
                "device": fs.device,
                "fstype": fs.fstype,
                "detail": _("%(used)s / %(total)s")
                % {
                    "used": _format_bytes_si(fs.used_bytes),
                    "total": _format_bytes_si(fs.size_bytes),
                },
                "fraction": fs.used_fraction,
                "percent": _format_percent(fs.used_fraction),
                "severity": _usage_severity(fs.used_fraction),
                "read_only": fs.read_only,
                "inode_note": inode_note,
            }
        )

    interfaces = []
    for iface in snapshot.interfaces:
        addresses = tuple(iface.ipv4_addresses) + tuple(iface.ipv6_addresses)
        interfaces.append(
            {
                "name": iface.name,
                "kind": iface.kind.value,
                "state": iface.state.value,
                "addresses": ", ".join(addresses) if addresses else _("N/A"),
                "mac": iface.mac_address or "—",
                "mtu": iface.mtu,
                "rx_rate": "—",
                "tx_rate": "—",
            }
        )

    def _endpoint(address: str, port: Optional[int]) -> str:
        if not address and port is None:
            return "—"
        if port is None:
            return address or "—"
        if not address:
            return str(port)
        return f"{address}:{port}"

    sockets = [
        {
            "direction": sock.direction.value,
            "protocol": sock.protocol,
            "local": _endpoint(sock.local_address, sock.local_port),
            "remote": _endpoint(sock.peer_address, sock.peer_port),
            "process": sock.process or "—",
        }
        for sock in snapshot.sockets[:64]
    ]
    listening = [
        {
            "address": str(port.port),
            "bind": port.address or "",
            "process": port.process or "—",
        }
        for port in snapshot.listening_ports[:64]
    ]
    processes = [
        {
            "name": proc.command,
            "cpu": _("%.1f%%") % proc.cpu_percent
            if proc.cpu_percent is not None
            else "—",
            "mem": _("%.1f%%") % proc.memory_percent
            if proc.memory_percent is not None
            else "—",
        }
        for proc in snapshot.processes
    ]
    temps = [
        {
            "label": temp.label,
            "value": _("%.0f °C") % temp.celsius,
            "severity": _temp_severity(temp.celsius),
        }
        for temp in snapshot.temperatures
    ]
    sessions = [
        {
            "user": session.user,
            "tty": session.tty or "—",
            "from": session.origin or ("—" if not session.remote else _("remote")),
            "since": session.since or "—",
        }
        for session in snapshot.sessions
    ]
    failed_units = [
        {"unit": unit.name, "state": unit.description or "—"}
        for unit in snapshot.failed_units
    ]
    host_keys = [
        {
            "algorithm": key.algorithm,
            "fingerprint": key.fingerprint,
            "bits": key.bits,
        }
        for key in snapshot.host_keys
    ]
    dns = ", ".join(snapshot.dns_servers) if snapshot.dns_servers else _("N/A")

    system_rows = [
        _kv(_("Hostname"), _or_na(snapshot.hostname), mono=True),
        _kv(_("OS ID"), _or_na(snapshot.os_id), mono=True),
        _kv(_("OS version"), _or_na(snapshot.os_version_id), mono=True),
        _kv(_("Architecture"), _or_na(snapshot.architecture), mono=True),
        _kv(
            _("SSH port"),
            str(snapshot.ssh_port) if snapshot.ssh_port is not None else _("N/A"),
            mono=True,
        ),
        _kv(_("SSH process"), _or_na(snapshot.ssh_process), mono=True),
        _kv(_("Default gateway"), _or_na(snapshot.default_gateway), mono=True),
        _kv(
            _("Gateway interface"),
            _or_na(snapshot.default_gateway_interface),
            mono=True,
        ),
        _kv(_("DNS"), dns, mono=True),
    ]

    return {
        "title": title,
        "subtitle": subtitle,
        "status": "ready",
        "message": "",
        "gauges": [
            _gauge(
                _("CPU"),
                None,
                _format_frequency(snapshot.cpu.frequency_mhz),
                _load_detail(snapshot.load_average),
                severity="unknown",
            ),
            _gauge(
                _("Memory"),
                mem_fraction,
                _("%(used)s / %(total)s")
                % {
                    "used": _format_bytes(used),
                    "total": _format_bytes(memory.total_bytes),
                },
                _swap_detail(memory),
            ),
            _gauge(
                _("Root filesystem"),
                root.used_fraction if root is not None else None,
                root_detail,
                root_device,
            ),
        ],
        "overview_rows": overview_rows,
        "load_cards": load_cards,
        "cores": [
            {"name": item.name, "percent": "—", "fraction": None, "severity": "unknown"}
            for item in snapshot.cpu_times
            if item.name != "cpu"
        ],
        "filesystems": filesystems,
        "interfaces": interfaces,
        "listening": listening,
        "sockets": sockets,
        "processes": processes,
        "temperatures": temps,
        "sessions": sessions,
        "failed_units": failed_units,
        "host_keys": host_keys,
        "system_rows": system_rows,
    }


def live_payload(
    previous: LiveSample,
    sample: LiveSample,
    elapsed: float,
    *,
    frequency_mhz: Optional[float],
) -> Dict[str, Any]:
    """Rates and instantaneous readings between two LIVE (or FULL baseline) samples."""

    rates = interface_rates(previous.counters, sample.counters, elapsed)
    utilization = cpu_utilization_by_name(previous.cpu_times, sample.cpu_times)
    aggregate = utilization.get("cpu")
    total = aggregate.total if isinstance(aggregate, CpuUtilization) else None
    cpu_fraction = None if total is None else total / 100.0

    memory = sample.memory
    mem_fraction = None
    mem_detail = ""
    swap_detail = ""
    if memory is not None:
        used = memory.used_bytes
        mem_fraction = (
            used / memory.total_bytes if used is not None and memory.total_bytes else None
        )
        mem_detail = _("%(used)s / %(total)s") % {
            "used": _format_bytes(used),
            "total": _format_bytes(memory.total_bytes),
        }
        swap_detail = _swap_detail(memory)

    cores: List[Dict[str, Any]] = []
    for name, util in utilization.items():
        if name == "cpu" or util is None:
            continue
        fraction = None if util.total is None else util.total / 100.0
        cores.append(
            {
                "name": name,
                "percent": _format_percent(fraction),
                "fraction": fraction,
                "severity": _usage_severity(fraction),
            }
        )

    return {
        "cpu": _gauge(
            _("CPU"),
            cpu_fraction,
            _format_frequency(frequency_mhz),
            _load_detail(sample.load_average),
        ),
        "memory": _gauge(_("Memory"), mem_fraction, mem_detail, swap_detail),
        "cores": cores,
        "rates": {
            name: {
                "rx_rate": _format_rate(rx),
                "tx_rate": _format_rate(tx),
            }
            for name, (rx, tx) in rates.items()
        },
    }


def status_payload(
    *,
    title: str,
    subtitle: str,
    message: str,
    busy: bool = False,
) -> Dict[str, Any]:
    return {
        "title": title,
        "subtitle": subtitle,
        "status": "busy" if busy else "error",
        "message": message,
    }
