"""Turn raw host-probe output into typed host-information DTOs (GTK-free).

Everything here is a pure text transformation: no I/O, no gettext, no
presentation.  Each helper accepts both the coreutils/iproute2 form and the
BusyBox form of its input, because OpenWrt and other embedded hosts answer
with ``netstat``, six-column ``df`` and ``w`` where a desktop answers with
``ss``, seven-column ``df`` and ``who``.

Two decisions are worth calling out because guessing them wrongly produced
visibly contradictory output before:

* an absent reading stays ``None`` rather than becoming ``0`` or the total, so
  a frontend renders "unknown" instead of "0% used" on one screen and "100%
  used" on another;
* socket direction is decided from the host's own listening ports rather than
  from a privileged-port guess, so a service on 8443 is still "incoming" and
  an outgoing connection to 443 is not misfiled.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ...api.models.host_info import (
    CPU_TIME_FIELDS,
    CpuInfo,
    CpuTimes,
    FailedUnit,
    FilesystemUsage,
    HostInfoSnapshot,
    HostKeyFingerprint,
    InterfaceCounters,
    ListeningPort,
    LiveSample,
    LoadAverage,
    LoginSession,
    MemoryInfo,
    NetworkInterface,
    NetworkInterfaceKind,
    NetworkInterfaceState,
    PressureStall,
    ProcessCounts,
    ProcessUsage,
    SocketConnection,
    SocketDirection,
    TemperatureReading,
)
from .probe import SECTION_PATTERN

_SECTION_RE = re.compile(SECTION_PATTERN)

# Filesystems backed by kernel memory rather than storage.  ``overlayfs``
# appears as the *device* of OpenWrt's merged ``/``; its real backing store is
# reported separately as ``/overlay`` and is kept.
_PSEUDO_FILESYSTEMS = frozenset(
    {
        "binfmt_misc", "cgroup", "cgroup2", "configfs", "debugfs", "devpts",
        "devtmpfs", "efivarfs", "fusectl", "hugetlbfs", "mqueue", "none",
        "nsfs", "overlay", "overlayfs", "proc", "pstore", "ramfs",
        "rpc_pipefs", "securityfs", "sysfs", "tmpfs", "tracefs", "udev",
    }
)

_PSEUDO_MOUNT_PREFIXES = ("/snap/", "/run/", "/sys/", "/proc/", "/dev/")

# ``who``/``w`` render a local X or console login as these origins.
_LOCAL_ORIGINS = frozenset({"", ":0", ":1", ":0.0", "-", "console"})


def split_sections(raw: str) -> Dict[str, str]:
    """Split probe output into ``{marker: body}`` with surrounding blanks cut."""

    sections: Dict[str, str] = {}
    key: Optional[str] = None
    lines: List[str] = []
    for line in raw.splitlines():
        match = _SECTION_RE.match(line)
        if match:
            if key is not None:
                sections[key] = "\n".join(lines).strip()
            key = match.group(1)
            lines = []
        elif key is not None:
            lines.append(line)
    if key is not None:
        sections[key] = "\n".join(lines).strip()
    return sections


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------

def _int_or_none(value: object) -> Optional[int]:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _positive_int_or_none(value: object) -> Optional[int]:
    """``0`` means "the host could not tell us", never "zero CPUs"."""

    parsed = _int_or_none(value)
    return parsed if parsed else None


def _float_or_none(value: object) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _port_or_none(endpoint: str) -> Optional[int]:
    match = re.search(r":(\d+)$", endpoint or "")
    if not match:
        return None
    port = int(match.group(1))
    return port if 0 <= port <= 65535 else None


def _strip_port(endpoint: str) -> str:
    return re.sub(r":\d+$", "", endpoint or "")


# ---------------------------------------------------------------------------
# Identity, CPU, memory
# ---------------------------------------------------------------------------

def parse_os_release(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip().strip('"')
    return values


def parse_key_value_block(text: str) -> Dict[str, str]:
    """Parse ``lscpu``-style ``key: value`` output."""

    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            values[key.strip()] = value.strip()
    return values


def parse_cpuinfo(text: str) -> Dict[str, str]:
    """Extract lscpu-equivalent fields from ``/proc/cpuinfo``.

    ARM, MIPS and other architectures name the model differently and omit
    topology entirely, so only the first occurrence of each field is kept and
    the logical processor count is derived by counting ``processor`` lines.
    """

    values: Dict[str, str] = {}
    processors = 0
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "processor":
            processors += 1
        elif key in ("model name", "cpu model", "system type", "machine", "Hardware"):
            values.setdefault("model", value)
        elif key == "cpu MHz":
            values.setdefault("mhz", value)
        elif key == "BogoMIPS":
            values.setdefault("bogomips", value)
    if processors:
        values["processors"] = str(processors)
    return values


def parse_cpu(lscpu_text: str, cpuinfo_text: str, nproc_text: str) -> CpuInfo:
    lscpu = parse_key_value_block(lscpu_text)
    cpuinfo = parse_cpuinfo(cpuinfo_text)
    logical = (
        _positive_int_or_none(nproc_text)
        or _positive_int_or_none(cpuinfo.get("processors"))
        or _positive_int_or_none(lscpu.get("CPU(s)"))
    )
    frequency = _float_or_none(lscpu.get("CPU MHz")) or _float_or_none(cpuinfo.get("mhz"))
    return CpuInfo(
        model=lscpu.get("Model name", "") or cpuinfo.get("model", ""),
        cores_per_socket=_positive_int_or_none(lscpu.get("Core(s) per socket")),
        threads_per_core=_positive_int_or_none(lscpu.get("Thread(s) per core")),
        sockets=_positive_int_or_none(lscpu.get("Socket(s)")),
        logical_processors=logical,
        frequency_mhz=frequency,
        bogomips=_float_or_none(cpuinfo.get("bogomips")),
    )


def parse_meminfo(text: str) -> MemoryInfo:
    """Parse ``/proc/meminfo``; a field the host omits stays ``None``.

    Only the six fields the dialog cannot render without default to ``0``.
    Everything added since -- ``MemAvailable`` and the breakdown below -- is
    optional, because BusyBox and pre-3.14 kernels genuinely do not publish
    them and a zero would read as "none in use" rather than "not reported".
    """

    values: Dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    return MemoryInfo(
        total_bytes=values.get("MemTotal", 0),
        free_bytes=values.get("MemFree", 0),
        available_bytes=values.get("MemAvailable"),
        cached_bytes=values.get("Cached", 0),
        buffers_bytes=values.get("Buffers", 0),
        swap_total_bytes=values.get("SwapTotal", 0),
        swap_free_bytes=values.get("SwapFree", 0),
        active_bytes=values.get("Active"),
        inactive_bytes=values.get("Inactive"),
        shmem_bytes=values.get("Shmem"),
        dirty_bytes=values.get("Dirty"),
        writeback_bytes=values.get("Writeback"),
        slab_bytes=values.get("Slab"),
        slab_reclaimable_bytes=values.get("SReclaimable"),
    )


# ---------------------------------------------------------------------------
# CPU time counters
# ---------------------------------------------------------------------------

def parse_proc_stat(text: str) -> Tuple[CpuTimes, ...]:
    """Parse the ``cpu``/``cpuN`` lines of ``/proc/stat`` into counters.

    The aggregate line comes first, then one line per logical processor in the
    kernel's own order.  Columns run out on older kernels -- 2.6.11 added
    ``guest`` and 2.6.33 ``guest_nice``, and some architectures stop before
    ``steal`` -- so a missing column stays ``None`` instead of becoming a zero
    that would drag a computed share downwards.
    """

    readings: List[CpuTimes] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"cpu\d*", fields[0]):
            continue
        values: Dict[str, Optional[int]] = {}
        for name, raw in zip(CPU_TIME_FIELDS, fields[1:]):
            values[name] = _int_or_none(raw)
        readings.append(CpuTimes(name=fields[0], **values))
    return tuple(readings)


def parse_proc_stat_counters(text: str) -> Dict[str, Optional[int]]:
    """Read the scalar counters of ``/proc/stat``.

    ``intr`` and ``softirq`` print a grand total followed by a per-source
    breakdown; only the total is read.  ``procs_running`` and ``procs_blocked``
    are instantaneous, not counters, and are what a host without a usable
    ``ps`` can still say about its process table.
    """

    counters: Dict[str, Optional[int]] = {
        "context_switches": None,
        "interrupts": None,
        "soft_interrupts": None,
        "procs_running": None,
        "procs_blocked": None,
    }
    keys = {
        "ctxt": "context_switches",
        "intr": "interrupts",
        "softirq": "soft_interrupts",
        "procs_running": "procs_running",
        "procs_blocked": "procs_blocked",
    }
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] not in keys:
            continue
        counters[keys[fields[0]]] = _int_or_none(fields[1])
    return counters


def parse_load_average(text: str) -> Optional[LoadAverage]:
    parts = text.split()
    if len(parts) < 3:
        return None
    values = [_float_or_none(part) for part in parts[:3]]
    if any(value is None or value < 0 for value in values):
        return None
    return LoadAverage(values[0], values[1], values[2])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _df_rows(text: str):
    """Yield ``(device, fstype, mount_point, values, percent)`` from ``df``.

    Every ``df`` variant prints the same shape -- device, three numeric
    columns, a percentage, then the mount point -- and coreutils ``-T`` inserts
    a type column second.  The header decides whether that column is there, so
    no caller has to count fields for itself, and pseudo-filesystems are
    dropped once, here.  ``values`` are the raw numbers: what they *mean*
    (bytes, 1K blocks, inodes) is the caller's business.
    """

    lines = text.strip().splitlines()
    if not lines:
        return
    has_type = "type" in lines[0].lower()
    offset = 1 if has_type else 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6 + offset:
            continue
        device = parts[0]
        fstype = parts[1] if has_type else ""
        mount_point = " ".join(parts[5 + offset:])
        if device.lower() in _PSEUDO_FILESYSTEMS or fstype.lower() in _PSEUDO_FILESYSTEMS:
            continue
        if mount_point.startswith(_PSEUDO_MOUNT_PREFIXES):
            continue
        values = [_int_or_none(raw) for raw in parts[1 + offset:4 + offset]]
        percent = _int_or_none(parts[4 + offset].rstrip("%"))
        yield device, fstype, mount_point, values, percent


def parse_filesystems(
    text: str,
    inodes: Optional[Dict[str, Sequence[Optional[int]]]] = None,
    options: Optional[Dict[str, str]] = None,
) -> Tuple[FilesystemUsage, ...]:
    """Parse ``df`` output, normalising every size to bytes.

    coreutils ``df -T -B1`` reports bytes in seven columns; BusyBox ``df``
    reports 1K blocks in six.  The header decides which, so the multiplier is
    never guessed from the magnitude of the numbers.

    ``inodes`` and ``options`` come from separate probe sections and are joined
    on the mount point; a mount either side does not mention simply keeps the
    absent-reading default.
    """

    header = text.strip().splitlines()[0].lower() if text.strip() else ""
    multiplier = 1024 if ("1k-block" in header or "1024-block" in header) else 1
    inodes = inodes or {}
    options = options or {}
    rows: List[FilesystemUsage] = []
    for device, fstype, mount_point, values, percent in _df_rows(text):
        sizes = [None if value is None else value * multiplier for value in values]
        counts = inodes.get(mount_point) or (None, None, None)
        rows.append(
            FilesystemUsage(
                device=device,
                mount_point=mount_point,
                fstype=fstype,
                size_bytes=sizes[0],
                used_bytes=sizes[1],
                available_bytes=sizes[2],
                use_percent=None if percent is None or percent > 100 else percent,
                options=options.get(mount_point, ""),
                inodes_total=counts[0],
                inodes_used=counts[1],
                inodes_free=counts[2],
            )
        )
    return tuple(rows)


def parse_inode_usage(text: str) -> Dict[str, Tuple[Optional[int], ...]]:
    """Parse ``df -i`` into ``{mount point: (total, used, free)}``.

    Counts, not bytes, so no multiplier applies whichever ``df`` answered.
    Filesystems that have no fixed inode table (btrfs, zfs) print ``-`` in
    these columns, which reads as unknown rather than as zero.
    """

    return {
        mount_point: tuple(values)
        for _device, _fstype, mount_point, values, _percent in _df_rows(text)
    }


#: ``/proc/self/mounts`` escapes these four characters in octal.
_MOUNT_ESCAPES = (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\"))


def _unescape_mount_field(value: str) -> str:
    for escape, character in _MOUNT_ESCAPES:
        value = value.replace(escape, character)
    return value


def parse_mount_options(text: str) -> Dict[str, str]:
    """Parse ``/proc/self/mounts`` into ``{mount point: options}``.

    A mount point containing a space is written with an octal escape, so the
    fields are unescaped after splitting rather than before.  A later mount
    over the same point shadows an earlier one, which is what the kernel means
    by listing it twice, so the last entry wins.
    """

    options: Dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        options[_unescape_mount_field(fields[1])] = _unescape_mount_field(fields[3])
    return options


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def parse_network_counters(text: str) -> Tuple[InterfaceCounters, ...]:
    """Parse ``/proc/net/dev`` cumulative byte counters.

    The kernel prints eight receive fields then eight transmit fields, so
    bytes are the first and ninth numeric columns.
    """

    counters: List[InterfaceCounters] = []
    for line in text.splitlines():
        match = re.match(
            r"\s*(\S+):\s*(\d+)(?:\s+\d+){7}\s+(\d+)",
            line,
        )
        if match:
            counters.append(
                InterfaceCounters(match.group(1), int(match.group(2)), int(match.group(3)))
            )
    return tuple(counters)


def parse_wireless_interfaces(text: str) -> set:
    """Names that have a ``wireless``/``phy80211`` node in sysfs.

    This is what actually makes an interface wireless. Interface names are not
    evidence: ``wlx…``, ``ath0``, ``ra0`` and plain ``eth1`` are all possible
    names for a radio, and ``wl…`` can equally be a bridge someone named that
    way.
    """

    names = set()
    for line in text.splitlines():
        match = re.match(r"^/sys/class/net/([^/]+)/(?:wireless|phy80211)", line.strip())
        if match:
            names.add(match.group(1))
    return names


def _interface_kind(name: str, flags: str, wireless: set) -> NetworkInterfaceKind:
    if "LOOPBACK" in flags:
        return NetworkInterfaceKind.LOOPBACK
    if name in wireless:
        return NetworkInterfaceKind.WIRELESS
    return NetworkInterfaceKind.ETHERNET


def _interface_state(flags: str) -> NetworkInterfaceState:
    if "NO-CARRIER" in flags:
        return NetworkInterfaceState.NO_CARRIER
    if "state UP" in flags:
        return NetworkInterfaceState.UP
    if "state DOWN" in flags:
        return NetworkInterfaceState.DOWN
    if "LOOPBACK" in flags and "UP" in flags:
        return NetworkInterfaceState.UP
    return NetworkInterfaceState.UNKNOWN


def parse_interfaces(
    link_text: str, addr_text: str, wireless_text: str = ""
) -> Tuple[NetworkInterface, ...]:
    addresses: Dict[str, Tuple[List[str], List[str]]] = {}
    for line in addr_text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[2] in ("inet", "inet6"):
            name = parts[1].split("@", 1)[0]
            ipv4, ipv6 = addresses.setdefault(name, ([], []))
            (ipv4 if parts[2] == "inet" else ipv6).append(parts[3])

    wireless = parse_wireless_interfaces(wireless_text)
    interfaces: List[NetworkInterface] = []
    for line in link_text.splitlines():
        match = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:\s*(.*)$", line)
        if not match:
            continue
        name, rest = match.group(1), match.group(2)
        mac_match = re.search(r"link/\S+\s+([0-9a-f:]{17})", rest)
        mtu_match = re.search(r"mtu\s+(\d+)", rest)
        ipv4, ipv6 = addresses.get(name, ([], []))
        interfaces.append(
            NetworkInterface(
                name=name,
                kind=_interface_kind(name, rest, wireless),
                state=_interface_state(rest),
                mac_address=mac_match.group(1) if mac_match else "",
                mtu=_int_or_none(mtu_match.group(1)) if mtu_match else None,
                ipv4_addresses=tuple(ipv4),
                ipv6_addresses=tuple(ipv6),
            )
        )
    return tuple(interfaces)


def parse_default_route(text: str) -> Tuple[str, str]:
    """Return ``(gateway, interface)`` from ``ip route show default``."""

    parts = text.split()
    gateway = ""
    interface = ""
    for index, token in enumerate(parts):
        if token == "via" and index + 1 < len(parts):
            gateway = parts[index + 1]
        elif token == "dev" and index + 1 < len(parts):
            interface = parts[index + 1]
    return gateway, interface


def parse_dns_servers(text: str) -> Tuple[str, ...]:
    servers = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("nameserver"):
            parts = stripped.split()
            if len(parts) >= 2:
                servers.append(parts[1])
    return tuple(servers)


def parse_ssh_connection_port(text: str) -> Optional[int]:
    """The server port from ``$SSH_CONNECTION``.

    sshd sets ``SSH_CONNECTION`` to ``<client ip> <client port> <server ip>
    <server port>`` for every session it starts, including a non-interactive
    exec. The fourth field is therefore the port this very probe arrived on --
    authoritative, whatever the host runs SSH on.
    """

    parts = text.split()
    if len(parts) < 4:
        return None
    port = _int_or_none(parts[3])
    return port if port is not None and 0 <= port <= 65535 else None


def _is_netstat(text: str) -> bool:
    return any(
        line.strip().startswith(("Proto", "Active"))
        for line in text.splitlines()[:2]
    )


def parse_listening_ports(text: str) -> Dict[int, Tuple[str, str]]:
    """Map each listening TCP port to ``(bind_address, process_name)``.

    ``ss -tlnp`` prints ``users:(("sshd",pid=…))``; ``netstat -tlnp`` prints
    ``1234/sshd``.  Process names are only visible to root, so an empty name
    is normal and must not discard the port.  When the same port appears on
    several addresses, the first wins.
    """

    netstat = _is_netstat(text)
    ports: Dict[int, Tuple[str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] in ("Netid", "State", "Proto", "Active"):
            continue
        if netstat:
            if len(parts) < 6 or parts[5] != "LISTEN":
                continue
            local = parts[3]
            process = ""
            if len(parts) > 6:
                match = re.search(r"/(\S+)", parts[6])
                process = match.group(1) if match else ""
        else:
            if len(parts) < 5:
                continue
            local = parts[3]
            process = ""
            if len(parts) > 5:
                match = re.search(r'users:\(\("([^"]+)"', " ".join(parts[5:]))
                process = match.group(1) if match else ""
        port = _port_or_none(local)
        if port is None:
            continue
        address = _strip_port(local).strip("[]")
        ports.setdefault(port, (address, process))
    return ports


def _established_rows(text: str) -> List[Tuple[str, str, str, str]]:
    """Yield ``(protocol, local, peer, process)`` for established sockets.

    ``ss -tunap state established`` prints ``Netid Recv-Q Send-Q Local Peer
    Process`` — the explicit state filter removes the ``State`` column, so the
    local endpoint is column 3.  ``netstat -tunap`` prints ``Proto Recv-Q
    Send-Q Local Foreign State PID/Program`` with the same local column but a
    trailing state that must be filtered, because the netstat fallback lists
    listening and waiting sockets too.
    """

    netstat = _is_netstat(text)
    rows: List[Tuple[str, str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] in ("Netid", "State", "Proto", "Active"):
            continue
        protocol, local, peer = parts[0], parts[3], parts[4]
        if netstat:
            if len(parts) > 5 and parts[5] != "ESTABLISHED":
                continue
            process = ""
            if len(parts) > 6:
                match = re.search(r"/(\S+)", parts[6])
                process = match.group(1) if match else ""
        else:
            process = ""
            if len(parts) > 5:
                match = re.search(r'users:\(\("([^"]+)"', " ".join(parts[5:]))
                process = match.group(1) if match else ""
        rows.append((protocol, local, peer, process))
    return rows


def parse_sockets(
    text: str, listening_ports: Sequence[int] = ()
) -> Tuple[SocketConnection, ...]:
    """Classify established sockets by direction.

    A socket whose local port is one the host listens on is inbound; anything
    else is a connection this host opened.  Falling back to the privileged
    port range only matters when the listening probe produced nothing.
    """

    listening = set(listening_ports)
    sockets: List[SocketConnection] = []
    for protocol, local, peer, process in _established_rows(text):
        local_port = _port_or_none(local)
        if listening:
            incoming = local_port in listening
        else:
            incoming = local_port is not None and local_port < 1024
        sockets.append(
            SocketConnection(
                protocol=protocol,
                local_address=_strip_port(local),
                local_port=local_port,
                peer_address=_strip_port(peer),
                peer_port=_port_or_none(peer),
                process=process,
                direction=SocketDirection.INCOMING if incoming else SocketDirection.OUTGOING,
            )
        )
    return tuple(sockets)


# ---------------------------------------------------------------------------
# Login sessions
# ---------------------------------------------------------------------------

def parse_who(text: str) -> Tuple[LoginSession, ...]:
    sessions: List[LoginSession] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        rest = " ".join(parts[2:])
        origin = ""
        origin_match = re.search(r"\((.+)\)", rest)
        if origin_match:
            origin = origin_match.group(1)
            rest = rest[: origin_match.start()].strip()
        sessions.append(
            LoginSession(
                user=parts[0],
                tty=parts[1],
                origin="" if origin in _LOCAL_ORIGINS else origin,
                since=rest,
                remote=origin not in _LOCAL_ORIGINS,
            )
        )
    return tuple(sessions)


def parse_w(text: str) -> Tuple[LoginSession, ...]:
    """Parse ``w -h`` (``USER TTY FROM LOGIN@ IDLE JCPU PCPU WHAT``)."""

    sessions: List[LoginSession] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        origin = parts[2]
        sessions.append(
            LoginSession(
                user=parts[0],
                tty=parts[1],
                origin="" if origin in _LOCAL_ORIGINS else origin,
                since=parts[3],
                remote=origin not in _LOCAL_ORIGINS,
            )
        )
    return tuple(sessions)


def sessions_from_sockets(
    sockets: Sequence[SocketConnection], ssh_ports: Sequence[int]
) -> Tuple[LoginSession, ...]:
    """Reconstruct remote logins from inbound SSH sockets.

    BusyBox hosts ship neither ``who`` nor ``w``, so the only evidence a user
    is connected is an established socket on a port the host's SSH server
    listens on.  The peer address is the login origin; the account name is not
    knowable this way and stays empty.
    """

    wanted = set(ssh_ports)
    if not wanted:
        return ()
    seen: set = set()
    sessions: List[LoginSession] = []
    for socket in sockets:
        if socket.direction is not SocketDirection.INCOMING:
            continue
        if socket.local_port not in wanted or not socket.peer_address:
            continue
        if socket.peer_address in seen:
            continue
        seen.add(socket.peer_address)
        sessions.append(
            LoginSession(user="", tty="", origin=socket.peer_address, since="", remote=True)
        )
    return tuple(sessions)


# ---------------------------------------------------------------------------
# Temperatures
# ---------------------------------------------------------------------------

def parse_thermal_zones(temps_text: str, types_text: str) -> Tuple[TemperatureReading, ...]:
    readings: Dict[str, int] = {}
    for line in temps_text.splitlines():
        match = re.match(r".*/thermal_zone(\d+)/temp:(-?\d+)", line)
        if match:
            readings[match.group(1)] = int(match.group(2))
    labels: Dict[str, str] = {}
    for line in types_text.splitlines():
        match = re.match(r".*/thermal_zone(\d+)/type:(.+)", line)
        if match:
            labels[match.group(1)] = match.group(2).strip()
    return tuple(
        TemperatureReading(labels.get(zone, f"thermal_zone{zone}"), readings[zone] / 1000.0)
        for zone in sorted(readings, key=lambda item: int(item))
    )


def parse_sensors(text: str) -> Tuple[TemperatureReading, ...]:
    """Parse ``sensors`` output for hosts without ``/sys/class/thermal``."""

    readings: List[TemperatureReading] = []
    adapter = ""
    for line in text.splitlines():
        if not line.strip() or line.startswith("Adapter:"):
            continue
        if not line[:1].isspace() and ":" not in line:
            adapter = line.strip()
            continue
        match = re.match(r"^(.+?):\s+\+?(-?[\d.]+)\s*°?C", line)
        if match:
            label = match.group(1).strip()
            readings.append(
                TemperatureReading(
                    f"{adapter} · {label}" if adapter else label, float(match.group(2))
                )
            )
    return tuple(readings)


# ---------------------------------------------------------------------------
def parse_process_table(text: str) -> Tuple[ProcessUsage, ...]:
    """Parse a process listing by reading its own header row.

    ``ps -eo pcpu,pmem,comm``, BusyBox ``top`` and procps ``top`` print
    different columns in different orders, but each names them, so the header
    decides where to read rather than a per-tool column index.  BusyBox
    publishes ``%VSZ`` and no ``%MEM``; a share of virtual size is not a share
    of memory, so it is left unreported rather than relabelled.
    """

    lines = text.splitlines()
    for index, line in enumerate(lines):
        header = line.split()
        if "%CPU" in header and "COMMAND" in header:
            break
    else:
        return ()

    cpu_at = header.index("%CPU")
    command_at = header.index("COMMAND")
    memory_at = header.index("%MEM") if "%MEM" in header else None

    processes: List[ProcessUsage] = []
    for line in lines[index + 1:]:
        fields = line.split()
        if len(fields) <= command_at:
            continue
        command = " ".join(fields[command_at:])
        if not command:
            continue
        processes.append(
            ProcessUsage(
                command=command,
                cpu_percent=_float_or_none(fields[cpu_at]) if cpu_at < len(fields) else None,
                memory_percent=(
                    _float_or_none(fields[memory_at])
                    if memory_at is not None and memory_at < len(fields)
                    else None
                ),
            )
        )
    return tuple(processes)


def parse_failed_units(text: str) -> Tuple[FailedUnit, ...]:
    """Parse ``systemctl --failed --no-legend --plain``.

    Only the unit name and the description are read.  The three state columns
    between them are localized by the remote host's own locale, so matching on
    their words would work in English and nowhere else.
    """

    units: List[FailedUnit] = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4:
            continue
        units.append(
            FailedUnit(name=fields[0], description=fields[4] if len(fields) > 4 else "")
        )
    return tuple(units)


def parse_host_keys(text: str) -> Tuple[HostKeyFingerprint, ...]:
    """Parse ``ssh-keygen -l`` lines: ``bits fingerprint comment (ALGORITHM)``."""

    keys: List[HostKeyFingerprint] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        algorithm = fields[-1]
        if not (algorithm.startswith("(") and algorithm.endswith(")")):
            continue
        keys.append(
            HostKeyFingerprint(
                algorithm=algorithm[1:-1],
                fingerprint=fields[1],
                bits=_positive_int_or_none(fields[0]),
            )
        )
    return tuple(keys)


def parse_pressure(
    text: str,
) -> Tuple[Optional[PressureStall], Optional[PressureStall]]:
    """Parse one ``/proc/pressure/*`` file into its ``some`` and ``full`` lines.

    ``cpu`` publishes only ``some`` -- there is no such thing as every task
    being stalled on a CPU while one of them runs -- so ``full`` is routinely
    ``None`` here and that is not a parse failure.
    """

    readings: Dict[str, PressureStall] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] not in ("some", "full"):
            continue
        values: Dict[str, Optional[float]] = {}
        for token in fields[1:]:
            key, sep, value = token.partition("=")
            if sep:
                values[key] = _float_or_none(value)
        averages = [values.get(name) for name in ("avg10", "avg60", "avg300")]
        if any(value is None for value in averages):
            continue
        readings[fields[0]] = PressureStall(*averages)
    return readings.get("some"), readings.get("full")


#: The pressure files differ only in which stalls they count, so ``io`` keeps
#: its original name for callers that predate ``cpu`` and ``memory``.
parse_io_pressure = parse_pressure


def parse_process_states(text: str) -> Dict[str, Optional[int]]:
    """Count processes by state from ``ps -eo stat=,nlwp=``.

    Only the first letter of ``STAT`` is a state; the flags after it (``s``
    session leader, ``+`` foreground, ``<`` high priority) describe the process
    rather than what it is doing.  A ``ps`` that answered without ``nlwp``
    leaves the thread count unreported rather than equal to the process count,
    because one is not an estimate of the other on a threaded host.
    """

    buckets: Dict[str, Optional[int]] = {
        "total": None,
        "running": None,
        "sleeping": None,
        "stopped": None,
        "zombie": None,
        "threads": None,
    }
    states = {"R": "running", "T": "stopped", "t": "stopped", "Z": "zombie"}
    counted = {"total": 0, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}
    threads = 0
    saw_threads = False
    for line in text.splitlines():
        fields = line.split()
        if not fields or not fields[0][:1].isalpha():
            continue
        counted["total"] += 1
        counted[states.get(fields[0][0], "sleeping")] += 1
        if len(fields) > 1:
            count = _positive_int_or_none(fields[1])
            if count is not None:
                threads += count
                saw_threads = True
    if not counted["total"]:
        return buckets
    buckets.update(counted)
    if saw_threads:
        buckets["threads"] = threads
    return buckets


def parse_architecture(uname_text: str) -> str:
    """The machine field of ``uname -srm``: sysname, release, then machine."""

    fields = uname_text.split()
    return fields[2] if len(fields) >= 3 else ""


# Assembly
# ---------------------------------------------------------------------------

def _boot_time(sections: Dict[str, str]) -> str:
    who_b = sections.get("BOOT_TIME", "").strip()
    if who_b:
        parts = who_b.split()
        if len(parts) >= 3:
            return " ".join(parts[-2:])
    return sections.get("UPTIME_SINCE", "").strip()


def _process_counts(sections: Dict[str, str]) -> Optional[ProcessCounts]:
    """Assemble the process table summary from whichever section answered.

    ``ps`` gives the full breakdown.  A host whose ``ps`` has no ``-o`` -- every
    BusyBox one -- still publishes ``procs_running`` and ``procs_blocked`` in
    ``/proc/stat``, so the running count survives even where the breakdown does
    not.  A host that said nothing at all gets ``None``, not a row of zeros.
    """

    buckets = parse_process_states(sections.get("PROC_STATES", ""))
    pid_max = _positive_int_or_none(sections.get("PID_MAX", ""))
    if buckets["total"] is None:
        counters = parse_proc_stat_counters(sections.get("STAT", ""))
        running = counters["procs_running"]
        if running is None and pid_max is None:
            return None
        return ProcessCounts(running=running, pid_max=pid_max)
    return ProcessCounts(pid_max=pid_max, **buckets)


def parse_host_info(raw: str) -> HostInfoSnapshot:
    """Parse one full probe into a snapshot; absent sections stay empty."""

    sections = split_sections(raw)

    listening = parse_listening_ports(sections.get("SS_LISTEN", ""))
    # Prefer the port the probe itself arrived on, then any port sshd is seen
    # listening on. There is deliberately no fallback to 22: reporting nothing
    # is honest, while guessing 22 is wrong on every host that moved the port.
    session_port = parse_ssh_connection_port(sections.get("SSH_CONNECTION", ""))
    ssh_ports = [session_port] if session_port is not None else []
    ssh_ports.extend(
        port
        for port, (_address, process) in sorted(listening.items())
        if process == "sshd" and port != session_port
    )
    sockets = parse_sockets(sections.get("SS_ESTAB", ""), tuple(listening))

    sessions = parse_who(sections.get("WHO", ""))
    if not sessions:
        sessions = parse_w(sections.get("W", ""))
    if not sessions:
        sessions = sessions_from_sockets(sockets, ssh_ports)

    temperatures = parse_thermal_zones(
        sections.get("TEMPS", ""), sections.get("TEMP_TYPES", "")
    )
    if not temperatures:
        temperatures = parse_sensors(sections.get("SENSORS", ""))

    processes = parse_process_table(sections.get("PROCESSES", ""))
    if not processes:
        processes = parse_process_table(sections.get("TOP", ""))
    io_some, io_full = parse_pressure(sections.get("IO_PRESSURE", ""))
    cpu_some, cpu_full = parse_pressure(sections.get("CPU_PRESSURE", ""))
    memory_some, memory_full = parse_pressure(sections.get("MEM_PRESSURE", ""))
    stat_counters = parse_proc_stat_counters(sections.get("STAT", ""))
    os_release = parse_os_release(sections.get("OS_RELEASE", ""))
    uname = sections.get("UNAME", "").strip()

    gateway, gateway_interface = parse_default_route(sections.get("IP_ROUTE", ""))
    uptime = _float_or_none(sections.get("UPTIME", "").split()[0]) if sections.get(
        "UPTIME", ""
    ).split() else None

    return HostInfoSnapshot(
        hostname=sections.get("HOSTNAME", "").strip(),
        device_model=sections.get("DEVICE_MODEL", "").strip(),
        os_pretty_name=os_release.get("PRETTY_NAME", ""),
        kernel=uname,
        uptime_seconds=uptime if uptime is not None and uptime >= 0 else None,
        boot_time=_boot_time(sections),
        cpu=parse_cpu(
            sections.get("LSCPU", ""),
            sections.get("CPUINFO", ""),
            sections.get("NPROC", ""),
        ),
        memory=parse_meminfo(sections.get("MEMINFO", "")),
        load_average=parse_load_average(sections.get("LOADAVG", "")),
        filesystems=parse_filesystems(
            sections.get("DF", ""),
            parse_inode_usage(sections.get("DF_INODES", "")),
            parse_mount_options(sections.get("MOUNTS", "")),
        ),
        interfaces=parse_interfaces(
            sections.get("IP_LINK", ""),
            sections.get("IP_ADDR", ""),
            sections.get("WIRELESS", ""),
        ),
        temperatures=temperatures,
        sessions=sessions,
        sockets=sockets,
        default_gateway=gateway,
        default_gateway_interface=gateway_interface,
        dns_servers=parse_dns_servers(sections.get("DNS", "")),
        ssh_port=ssh_ports[0] if ssh_ports else None,
        ssh_process=(
            listening.get(ssh_ports[0], ("", ""))[1] if ssh_ports else ""
        ),
        os_id=os_release.get("ID", ""),
        os_version_id=os_release.get("VERSION_ID", ""),
        architecture=parse_architecture(uname),
        # Every port the host accepts on, not only the one this session
        # arrived through: what else is exposed is the question an operator
        # actually opens this dialog with.
        listening_ports=tuple(
            ListeningPort(port=port, process=process, address=address)
            for port, (address, process) in sorted(listening.items())
        ),
        processes=processes,
        failed_units=parse_failed_units(sections.get("SYSTEMD_FAILED", "")),
        host_keys=parse_host_keys(sections.get("SSH_HOST_KEYS", "")),
        io_pressure_some=io_some,
        io_pressure_full=io_full,
        cpu_pressure_some=cpu_some,
        cpu_pressure_full=cpu_full,
        memory_pressure_some=memory_some,
        memory_pressure_full=memory_full,
        cpu_times=parse_proc_stat(sections.get("STAT", "")),
        process_counts=_process_counts(sections),
        context_switches=stat_counters["context_switches"],
        interrupts=stat_counters["interrupts"],
    )


def parse_counters_probe(raw: str) -> Tuple[InterfaceCounters, ...]:
    """Parse the lightweight bandwidth probe."""

    return parse_network_counters(split_sections(raw).get("NET_DEV", ""))


def parse_live_probe(raw: str) -> LiveSample:
    """Parse one live sample.

    Every section reuses the full gather's parser and marker name, so a reading
    cannot mean one thing on open and another two seconds later.
    """

    sections = split_sections(raw)
    return LiveSample(
        counters=parse_network_counters(sections.get("NET_DEV", "")),
        cpu_times=parse_proc_stat(sections.get("STAT", "")),
        memory=parse_meminfo(sections["MEMINFO"]) if "MEMINFO" in sections else None,
        load_average=parse_load_average(sections.get("LOADAVG", "")),
    )
