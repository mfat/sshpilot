"""Reading the Darwin and BSD probe sections into the Linux-shaped DTOs.

The probe asks those hosts in their own vocabulary -- ``sysctl``, ``vm_stat``,
``ifconfig``, ``netstat`` -- rather than pretending they are Linux, and this
module is where that vocabulary is translated.  Nothing here is Darwin-only or
FreeBSD-only by design: both name most of the same sysctls, and where they
differ (``hw.memsize`` against ``hw.physmem``, ``vm.swapusage`` against
``swapinfo``) the reader takes whichever one the host actually answered.  The
rule throughout is the parser's rule: a reading the host did not publish stays
absent, because a zero would be read as a measurement.

The module is pure -- text in, DTOs out -- and GTK-free, like its callers.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from ...api.models.host_info import (
    CpuInfo,
    CpuTimes,
    InterfaceCounters,
    LoadAverage,
    MemoryInfo,
    NetworkInterface,
    NetworkInterfaceKind,
    NetworkInterfaceState,
    TemperatureReading,
)

#: What ``uname -s`` prints on the systems this module speaks for.
DARWIN = "Darwin"
BSD_OS_TYPES = frozenset({DARWIN, "FreeBSD", "OpenBSD", "NetBSD", "DragonFly"})

#: ``kern.cp_time`` publishes five counters in this order.  ``intr`` is time
#: spent servicing interrupts, which is what Linux calls ``irq``; there is no
#: BSD equivalent of ``iowait``, ``steal`` or ``softirq``, so those stay None
#: rather than becoming zeros that would dilute every computed share.
_CP_TIME_FIELDS = ("user", "nice", "system", "irq", "idle")

_BYTE_SUFFIXES = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def is_bsd(os_type: str) -> bool:
    """Whether a snapshot's ``uname -s`` names a system this module reads."""

    return (os_type or "").strip() in BSD_OS_TYPES


# ---------------------------------------------------------------------------
# sysctl
# ---------------------------------------------------------------------------

def parse_sysctl(text: str) -> Dict[str, str]:
    """Parse ``sysctl name1 name2 ...`` output into ``{name: value}``.

    Both families print ``name: value``, and a name the host does not know is
    reported on stderr, which the probe discards -- so an absent key means the
    host has no such reading, never that the batch failed.
    """

    values: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            values[key.strip()] = value.strip()
    return values


def _first(values: Dict[str, str], *names: str) -> str:
    for name in names:
        value = values.get(name, "").strip()
        if value:
            return value
    return ""


def _int(values: Dict[str, str], *names: str) -> Optional[int]:
    raw = _first(values, *names)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _positive(value: Optional[int]) -> Optional[int]:
    return value if value is not None and value > 0 else None


# ---------------------------------------------------------------------------
# Identity, CPU
# ---------------------------------------------------------------------------

def parse_sw_vers(text: str) -> Tuple[str, str, str]:
    """``(pretty name, id, version)`` from macOS ``sw_vers``.

    ``ProductName`` is "macOS" on anything current and "Mac OS X" on what is
    not, so the id is lowercased and squeezed rather than assumed.
    """

    fields: Dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    name = fields.get("ProductName", "")
    version = fields.get("ProductVersion", "")
    if not name and not version:
        return "", "", ""
    pretty = " ".join(part for part in (name, version) if part)
    identifier = re.sub(r"[^a-z0-9]+", "", name.lower())
    return pretty, identifier, version


def os_identity(sw_vers_text: str, sysctl: Dict[str, str]) -> Tuple[str, str, str]:
    """``(pretty name, id, version)`` for a host with no ``/etc/os-release``.

    macOS answers through ``sw_vers``.  A BSD that ships no os-release file
    still names itself in ``kern.ostype``/``kern.osrelease``, which is where
    "FreeBSD 14.5-RELEASE" comes from.
    """

    pretty, identifier, version = parse_sw_vers(sw_vers_text)
    if pretty:
        return pretty, identifier, version
    ostype = _first(sysctl, "kern.ostype")
    osrelease = _first(sysctl, "kern.osrelease")
    if not ostype:
        return "", "", ""
    pretty = " ".join(part for part in (ostype, osrelease) if part)
    # "14.5-RELEASE" and "14.5-RELEASE-p2" both describe version 14.5.
    match = re.match(r"^(\d+(?:\.\d+)*)", osrelease)
    return pretty, ostype.lower(), match.group(1) if match else osrelease


def cpu_info(sysctl: Dict[str, str], os_type: str = "") -> CpuInfo:
    """Build :class:`CpuInfo` from the sysctl batch.

    ``hw.model`` means different things on the two families: the machine on
    Darwin ("Macmini9,1"), the processor on FreeBSD ("Intel(R) Xeon(R) ...").
    Darwin names the processor in ``machdep.cpu.brand_string``, so that one is
    preferred and ``hw.model`` is read as a CPU only where it is one.
    """

    darwin = (os_type or "").strip() == DARWIN
    model = _first(sysctl, "machdep.cpu.brand_string")
    if not model and not darwin:
        model = _first(sysctl, "hw.model")

    # Darwin counts physical cores directly and is always one package; the
    # BSDs publish no package count worth trusting, so the topology stays
    # unreported there rather than being invented from the core count.
    physical = _positive(_int(sysctl, "hw.physicalcpu", "machdep.cpu.core_count"))
    sockets = 1 if (darwin and physical) else None

    frequency = None
    hz = _int(sysctl, "hw.cpufrequency")
    if hz:
        frequency = hz / 1_000_000.0
    else:
        mhz = _int(sysctl, "hw.clockrate")
        if mhz:
            frequency = float(mhz)

    return CpuInfo(
        model=model,
        cores_per_socket=physical if sockets else None,
        threads_per_core=None,
        sockets=sockets,
        logical_processors=_positive(_int(sysctl, "hw.logicalcpu", "hw.ncpu")),
        frequency_mhz=frequency,
        bogomips=None,
    )


def cpu_times(sysctl: Dict[str, str]) -> Tuple[CpuTimes, ...]:
    """Cumulative CPU ticks from ``kern.cp_time`` and ``kern.cp_times``.

    ``kern.cp_time`` is the aggregate and ``kern.cp_times`` the same five
    counters repeated once per logical processor, which is exactly the
    aggregate-then-per-core shape ``/proc/stat`` has.  Darwin publishes
    neither, so there it returns nothing and no utilization is computed --
    reporting none is the honest answer, and the load average still is one.
    """

    readings: List[CpuTimes] = []
    aggregate = _cp_time_line(sysctl.get("kern.cp_time", ""), "cpu")
    if aggregate is not None:
        readings.append(aggregate)

    per_core = _first(sysctl, "kern.cp_times").split()
    width = len(_CP_TIME_FIELDS)
    for index in range(len(per_core) // width):
        chunk = per_core[index * width:(index + 1) * width]
        line = _cp_time_line(" ".join(chunk), f"cpu{index}")
        if line is not None:
            readings.append(line)
    return tuple(readings)


def _cp_time_line(text: str, name: str) -> Optional[CpuTimes]:
    fields = text.split()
    if len(fields) < len(_CP_TIME_FIELDS):
        return None
    values: Dict[str, Optional[int]] = {}
    for field, raw in zip(_CP_TIME_FIELDS, fields):
        try:
            values[field] = int(raw)
        except ValueError:
            return None
    return CpuTimes(name=name, **values)


def load_average(sysctl: Dict[str, str]) -> Optional[LoadAverage]:
    """``vm.loadavg`` -- the three figures inside ``{ }``."""

    raw = _first(sysctl, "vm.loadavg").strip("{} ")
    parts = raw.split()
    if len(parts) < 3:
        return None
    try:
        values = [float(part) for part in parts[:3]]
    except ValueError:
        return None
    if any(value < 0 for value in values):
        return None
    return LoadAverage(values[0], values[1], values[2])


def uptime(sysctl: Dict[str, str], now_epoch_text: str) -> Tuple[Optional[float], str]:
    """``(uptime seconds, boot time)`` from ``kern.boottime``.

    ``kern.boottime`` is an instant, not a duration, so the uptime is the
    difference against the host's *own* clock, which the probe read in the
    same round trip.  Without that reading there is no uptime, because
    measuring against this machine's clock would report the skew between two
    hosts as the remote host's uptime.  The trailing human-readable date is
    the host's rendering in the host's timezone, which is why it is kept
    verbatim instead of being reformatted here.
    """

    raw = _first(sysctl, "kern.boottime")
    match = re.search(r"sec\s*=\s*(\d+)", raw)
    if not match:
        return None, ""
    boot_epoch = int(match.group(1))
    printed = raw.split("}", 1)[1].strip() if "}" in raw else ""

    try:
        now = int((now_epoch_text or "").strip())
    except ValueError:
        return None, printed
    seconds = now - boot_epoch
    return (float(seconds) if seconds >= 0 else None), printed


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def parse_vm_stat(text: str) -> Tuple[Dict[str, int], int]:
    """``({counter: pages}, page size)`` from macOS ``vm_stat``.

    Every count is in pages of the size the header announces, and each line
    ends in a full stop that is not part of the number.
    """

    page_size = 4096
    header = re.search(r"page size of (\d+) bytes", text)
    if header:
        page_size = int(header.group(1))
    counters: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        digits = value.strip().rstrip(".")
        if digits.isdigit():
            counters[key.strip().strip('"')] = int(digits)
    return counters, page_size


def parse_swapinfo(text: str) -> Tuple[int, int]:
    """``(total bytes, free bytes)`` from ``swapinfo -k``.

    Several swap devices print several rows plus a ``Total`` line; the devices
    are summed and the summary row skipped, so a host with one device and a
    host with three are read the same way.
    """

    total = 0
    free = 0
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[0] == "Total":
            continue
        try:
            blocks = int(fields[1])
            available = int(fields[3])
        except ValueError:
            continue
        total += blocks * 1024
        free += available * 1024
    return total, free


def _swap_from_usage(value: str) -> Tuple[int, int]:
    """``(total, free)`` bytes from Darwin's ``vm.swapusage`` line."""

    sizes: Dict[str, int] = {}
    for name, number, suffix in re.findall(
        r"(total|used|free)\s*=\s*([\d.]+)([KMGT]?)", value
    ):
        try:
            sizes[name] = int(float(number) * _BYTE_SUFFIXES.get(suffix, 1))
        except ValueError:
            continue
    return sizes.get("total", 0), sizes.get("free", 0)


def memory(
    sysctl: Dict[str, str], vm_stat_text: str = "", swapinfo_text: str = ""
) -> MemoryInfo:
    """Build :class:`MemoryInfo` from whichever memory readings came back.

    Darwin counts pages in ``vm_stat`` and the BSDs in ``vm.stats.vm.*``, but
    both mean the same things, so the two are read into one set of counts and
    converted once.  ``available`` is derived rather than published: free
    memory alone understates what a program can actually get, because inactive
    and cached pages are reclaimable on both families.
    """

    total = _int(sysctl, "hw.memsize", "hw.physmem", "hw.realmem") or 0
    page_size = _int(sysctl, "hw.pagesize", "vm.stats.vm.v_page_size") or 0

    counters, darwin_page = parse_vm_stat(vm_stat_text)
    if counters:
        page_size = page_size or darwin_page
        free = counters.get("Pages free", 0)
        speculative = counters.get("Pages speculative", 0)
        inactive = counters.get("Pages inactive", 0)
        active = counters.get("Pages active", 0)
        cached = counters.get("File-backed pages", 0)
        purgeable = counters.get("Pages purgeable", 0)
        reclaimable = free + speculative + inactive + purgeable
    else:
        page_size = page_size or 4096
        free = _int(sysctl, "vm.stats.vm.v_free_count") or 0
        inactive = _int(sysctl, "vm.stats.vm.v_inactive_count") or 0
        active = _int(sysctl, "vm.stats.vm.v_active_count") or 0
        cached = _int(sysctl, "vm.stats.vm.v_cache_count") or 0
        reclaimable = free + inactive + cached

    if not total and not free:
        return MemoryInfo()

    swap_total, swap_free = parse_swapinfo(swapinfo_text)
    if not swap_total:
        swap_total, swap_free = _swap_from_usage(_first(sysctl, "vm.swapusage"))

    return MemoryInfo(
        total_bytes=total,
        free_bytes=free * page_size,
        available_bytes=reclaimable * page_size,
        cached_bytes=cached * page_size,
        buffers_bytes=0,
        swap_total_bytes=swap_total,
        swap_free_bytes=swap_free,
        active_bytes=active * page_size,
        inactive_bytes=inactive * page_size,
    )


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _prefix_from_netmask(netmask: str) -> Optional[int]:
    """Bits set in a ``0xffffff00``-style mask, which is how BSD prints one."""

    try:
        value = int(netmask, 16) if netmask.lower().startswith("0x") else int(netmask)
    except (TypeError, ValueError):
        return None
    return bin(value & 0xFFFFFFFF).count("1")


def parse_ifconfig(text: str) -> Tuple[NetworkInterface, ...]:
    """Parse ``ifconfig -a``.

    An interface block starts in column zero and its details are indented, so
    the header line opens a new interface and everything until the next one
    belongs to it.  Addresses are rendered ``address/prefix`` to match what
    ``ip -o addr`` gives on Linux, which means converting the hexadecimal
    netmask BSD prints into a prefix length.
    """

    interfaces: List[NetworkInterface] = []
    name = ""
    flags = ""
    mtu: Optional[int] = None
    mac = ""
    ipv4: List[str] = []
    ipv6: List[str] = []
    status = ""
    media = ""

    def flush() -> None:
        if not name:
            return
        interfaces.append(
            NetworkInterface(
                name=name,
                kind=_ifconfig_kind(flags, media),
                state=_ifconfig_state(flags, status),
                mac_address=mac,
                mtu=mtu,
                ipv4_addresses=tuple(ipv4),
                ipv6_addresses=tuple(ipv6),
            )
        )

    for line in text.splitlines():
        if not line.strip():
            continue
        header = re.match(r"^(\S+?):\s*flags=\w*<([^>]*)>(.*)$", line)
        if header:
            flush()
            name, flags = header.group(1), header.group(2)
            rest = header.group(3)
            mtu_match = re.search(r"mtu\s+(\d+)", rest)
            mtu = int(mtu_match.group(1)) if mtu_match else None
            mac, status, media = "", "", ""
            ipv4, ipv6 = [], []
            continue
        if not name:
            continue
        detail = line.strip()
        ether = re.match(r"^(?:ether|lladdr)\s+([0-9a-fA-F:]{17})", detail)
        if ether:
            mac = ether.group(1).lower()
            continue
        inet = re.match(r"^inet\s+(\S+)(?:\s+netmask\s+(\S+))?", detail)
        if inet:
            prefix = _prefix_from_netmask(inet.group(2)) if inet.group(2) else None
            ipv4.append(
                f"{inet.group(1)}/{prefix}" if prefix is not None else inet.group(1)
            )
            continue
        inet6 = re.match(r"^inet6\s+(\S+)(?:\s+prefixlen\s+(\d+))?", detail)
        if inet6:
            # A link-local address carries its zone ("fe80::1%en0"); Linux
            # prints the address alone, so the zone is dropped here too.
            address = inet6.group(1).split("%", 1)[0]
            ipv6.append(
                f"{address}/{inet6.group(2)}" if inet6.group(2) else address
            )
            continue
        if detail.startswith("status:"):
            status = detail.split(":", 1)[1].strip().lower()
        elif detail.startswith("media:"):
            media = detail.split(":", 1)[1].strip().lower()
    flush()
    return tuple(interfaces)


def _ifconfig_kind(flags: str, media: str) -> NetworkInterfaceKind:
    """What an interface is, read from its flags and its media line.

    The media line is the only portable evidence of a radio: FreeBSD's says
    "IEEE 802.11".  Darwin's Wi-Fi interface says only "autoselect", so a Mac's
    radio is reported as an ordinary interface rather than guessed at from its
    name -- ``en0`` is Wi-Fi on one Mac and Ethernet on the next.
    """

    if "LOOPBACK" in flags:
        return NetworkInterfaceKind.LOOPBACK
    if "802.11" in media or "ieee80211" in media:
        return NetworkInterfaceKind.WIRELESS
    return NetworkInterfaceKind.ETHERNET


def _ifconfig_state(flags: str, status: str) -> NetworkInterfaceState:
    if status:
        if status.startswith("active"):
            return NetworkInterfaceState.UP
        if "no carrier" in status:
            return NetworkInterfaceState.NO_CARRIER
        if status.startswith("inactive"):
            return NetworkInterfaceState.DOWN
    # Loopback and tunnels print no status line at all; for those the flags
    # are the whole truth.
    if "UP" in flags:
        return NetworkInterfaceState.UP
    return NetworkInterfaceState.DOWN if flags else NetworkInterfaceState.UNKNOWN


def parse_netstat_counters(text: str) -> Tuple[InterfaceCounters, ...]:
    """Cumulative byte counters from ``netstat -i -b -n``.

    Only the ``<Link#N>`` row of each interface carries the real totals; the
    per-network rows that follow repeat them or print ``-``.  Columns are
    counted from the right, against the numeric column names in the header,
    because an interface with no link-layer address (loopback) leaves the
    Address column empty and splitting on whitespace would shift every field
    after it.  FreeBSD's extra ``Idrop`` column is handled by the same rule.
    """

    lines = text.splitlines()
    header: List[str] = []
    for line in lines:
        fields = line.split()
        if "Ibytes" in fields and "Obytes" in fields:
            header = fields
            break
    if not header or "Address" not in header:
        return ()
    numeric = header[header.index("Address") + 1:]
    ibytes_at = numeric.index("Ibytes")
    obytes_at = numeric.index("Obytes")

    counters: List[InterfaceCounters] = []
    seen = set()
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or not fields[2].startswith("<Link"):
            continue
        tail = fields[-len(numeric):]
        if len(tail) < len(numeric):
            continue
        name = fields[0]
        if name in seen:
            continue
        try:
            rx = int(tail[ibytes_at])
            tx = int(tail[obytes_at])
        except (ValueError, IndexError):
            continue
        seen.add(name)
        counters.append(InterfaceCounters(name, rx, tx))
    return tuple(counters)


def parse_default_route(text: str) -> Tuple[str, str]:
    """``(gateway, interface)`` from ``netstat -rn``.

    The column holding the interface moves between releases (Darwin has had
    ``Refs``/``Use`` columns in the middle), so it is located by name in the
    header rather than counted.
    """

    netif_at = 3
    for line in text.splitlines():
        fields = line.split()
        if "Destination" in fields and "Gateway" in fields:
            if "Netif" in fields:
                netif_at = fields.index("Netif")
            break
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "default":
            gateway = fields[1]
            interface = fields[netif_at] if len(fields) > netif_at else fields[-1]
            return gateway, interface
    return "", ""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def parse_mounts(text: str) -> Dict[str, Tuple[str, str]]:
    """``{mount point: (fstype, options)}`` from ``mount -p`` or ``mount``.

    ``df -P`` prints no filesystem type, so the type comes from here.  FreeBSD
    answers ``mount -p`` in fstab order (device, point, type, options); Darwin
    has no ``-p`` and prints "device on point (type, option, ...)".
    """

    mounts: Dict[str, Tuple[str, str]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rendered = re.match(r"^(\S+)\s+on\s+(.+?)\s+\(([^)]*)\)\s*$", stripped)
        if rendered:
            parts = [part.strip() for part in rendered.group(3).split(",")]
            fstype = parts[0] if parts else ""
            mounts[rendered.group(2)] = (fstype, ", ".join(parts[1:]))
            continue
        fields = stripped.split()
        if len(fields) >= 4 and fields[1].startswith("/"):
            mounts[fields[1]] = (fields[2], fields[3])
    return mounts


# ---------------------------------------------------------------------------
# Temperatures
# ---------------------------------------------------------------------------

def parse_temperatures(text: str) -> Tuple[TemperatureReading, ...]:
    """Parse ``dev.cpu.N.temperature: 41.0C`` style sysctl readings.

    Present on FreeBSD once ``coretemp``/``amdtemp`` is loaded and on ACPI
    thermal zones; absent on stock Darwin, which publishes no temperature a
    non-privileged process can read.
    """

    readings: List[TemperatureReading] = []
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep or "temperature" not in key.lower():
            continue
        match = re.match(r"^\s*(-?[\d.]+)C?\s*$", value)
        if not match:
            continue
        try:
            celsius = float(match.group(1))
        except ValueError:
            continue
        name = key.strip()
        core = re.match(r"^dev\.cpu\.(\d+)\.temperature$", name)
        zone = re.match(r"^hw\.acpi\.thermal\.(\w+)\.temperature$", name)
        if core:
            label = f"cpu{core.group(1)}"
        elif zone:
            label = zone.group(1)
        else:
            label = name
        readings.append(TemperatureReading(label=label, celsius=celsius))
    return tuple(readings)


def normalise_endpoints(text: str) -> str:
    """Rewrite ``netstat -an`` endpoints into the ``address:port`` form.

    Darwin and the BSDs separate an endpoint's port with a dot -- ``*.22``,
    ``10.0.2.15.22``, ``fe80::1%lo0.22`` -- where Linux uses a colon.  Only the
    two address columns are touched, and only their trailing ``.<digits>``, so
    the header lines the socket parsers use to recognise netstat output pass
    through untouched and every other column keeps its own spacing meaning.

    Rewriting here, once, is what lets the ordinary netstat parsers read a BSD
    listing: their column rules already match, and the endpoint spelling was
    the only difference.
    """

    lines: List[str] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 5 and re.match(r"^(?:tcp|udp)\d*$", fields[0]):
            fields[3] = re.sub(r"\.(\d+)$", r":\1", fields[3])
            fields[4] = re.sub(r"\.(\d+)$", r":\1", fields[4])
            lines.append(" ".join(fields))
        else:
            lines.append(line)
    return "\n".join(lines)


def interface_names(counters: Sequence[InterfaceCounters]) -> Tuple[str, ...]:
    """Interface names in the order the counters were read."""

    return tuple(item.name for item in counters)
