"""Frontend-neutral remote host information models.

The daemon runs a read-only probe on the remote host and returns the parsed
result as these DTOs.  They carry *values*, never presentation: no formatted
byte counts, no localized text, no colour thresholds.  Frontends decide how a
byte count, a temperature or an absent reading is rendered, so the same
snapshot serves GTK, the CLI and any future frontend identically.

Absent readings are ``None`` rather than a sentinel number.  ``MemoryInfo``
distinguishes "the host reported 0" from "the host does not publish this
field" because older kernels and BusyBox omit ``MemAvailable``, and guessing a
default there produced contradictory usage figures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

from ..errors import ErrorCode

from .common import ConnectionId, require_identifier
from .operations import OperationSummary

MAX_HOST_INFO_FILESYSTEMS = 256
MAX_HOST_INFO_INTERFACES = 256
MAX_HOST_INFO_TEMPERATURES = 256
MAX_HOST_INFO_SESSIONS = 512
MAX_HOST_INFO_SOCKETS = 1024
MAX_HOST_INFO_ADDRESSES = 64
MAX_HOST_INFO_DNS_SERVERS = 32
MAX_HOST_INFO_LISTENING_PORTS = 1024
MAX_HOST_INFO_PROCESSES = 64
MAX_HOST_INFO_FAILED_UNITS = 256
MAX_HOST_INFO_HOST_KEYS = 16
#: One aggregate line plus one per logical CPU. Large enough for the biggest
#: machines anyone drives over SSH without letting a hostile host allocate
#: without bound.
MAX_HOST_INFO_CPU_TIMES = 1025


def _require_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain NUL")
    return value


def _require_optional_count(value: object, field_name: str) -> None:
    if value is None:
        return
    if type(value) is not int or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer or None")


def _require_optional_number(value: object, field_name: str) -> None:
    if value is None:
        return
    if type(value) not in (int, float) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number or None")


def _require_port(value: object, field_name: str) -> None:
    if value is None:
        return
    if type(value) is not int or isinstance(value, bool) or not 0 <= value <= 65535:
        raise ValueError(f"{field_name} must be a TCP/UDP port or None")


def _require_text_tuple(value: object, field_name: str, limit: int) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be a tuple")
    if len(value) > limit:
        raise ValueError(f"{field_name} exceeds the supported length")
    for item in value:
        _require_text(item, f"{field_name} entry")


class HostInfoProbe(str, Enum):
    """Which read-only probe the daemon runs on the remote host."""

    FULL = "full"
    NETWORK_COUNTERS = "network_counters"
    #: The repeated lightweight sample: cumulative counters plus the readings
    #: that change between gathers.
    LIVE = "live"


class HostInfoFailureCode(str, Enum):
    """Stable presentation reasons for host-information probe failures."""

    PROBE_FAILED = "probe_failed"
    PROBE_TIMED_OUT = "probe_timed_out"
    PROBE_START_FAILED = "probe_start_failed"
    CONNECTION_NOT_FOUND = "connection_not_found"
    SSH_CONNECTION_REQUIRED = "ssh_connection_required"
    UNREADABLE_SYSTEM_INFORMATION = "unreadable_system_information"


@dataclass(frozen=True)
class HostInfoFailure:
    """One localizable host-info failure plus an opaque diagnostic."""

    code: HostInfoFailureCode
    error_code: ErrorCode
    parameters: Mapping[str, str] = field(default_factory=dict)
    diagnostic: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, HostInfoFailureCode):
            raise TypeError("host info failure code is invalid")
        if not isinstance(self.error_code, ErrorCode):
            raise TypeError("host info failure error code is invalid")
        if not isinstance(self.parameters, Mapping):
            raise TypeError("host info failure parameters must be a mapping")
        parameters = dict(self.parameters)
        if parameters:
            raise ValueError(
                "host info failure parameters do not match the failure code"
            )
        if type(self.diagnostic) is not str or "\x00" in self.diagnostic:
            raise ValueError(
                "host info failure diagnostic must be a string without NUL"
            )
        object.__setattr__(self, "parameters", MappingProxyType(parameters))


class NetworkInterfaceKind(str, Enum):
    LOOPBACK = "loopback"
    WIRELESS = "wireless"
    ETHERNET = "ethernet"
    UNKNOWN = "unknown"


class NetworkInterfaceState(str, Enum):
    UP = "up"
    DOWN = "down"
    NO_CARRIER = "no_carrier"
    UNKNOWN = "unknown"


class SocketDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


@dataclass(frozen=True)
class CpuInfo:
    """Processor identity and topology as published by the remote host."""

    model: str = ""
    cores_per_socket: Optional[int] = None
    threads_per_core: Optional[int] = None
    sockets: Optional[int] = None
    logical_processors: Optional[int] = None
    frequency_mhz: Optional[float] = None
    bogomips: Optional[float] = None

    def __post_init__(self) -> None:
        _require_text(self.model, "cpu model")
        for name in (
            "cores_per_socket",
            "threads_per_core",
            "sockets",
            "logical_processors",
        ):
            _require_optional_count(getattr(self, name), f"cpu {name}")
        _require_optional_number(self.frequency_mhz, "cpu frequency")
        _require_optional_number(self.bogomips, "cpu bogomips")

    @property
    def total_threads(self) -> Optional[int]:
        """Threads across every socket, or ``None`` when topology is partial."""

        if None in (self.cores_per_socket, self.threads_per_core, self.sockets):
            return self.logical_processors
        return self.cores_per_socket * self.threads_per_core * self.sockets


#: The ``/proc/stat`` CPU columns, in the order the kernel prints them.
CPU_TIME_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
    "guest",
    "guest_nice",
)


@dataclass(frozen=True)
class CpuTimes:
    """Cumulative CPU jiffies for one line of ``/proc/stat``.

    ``name`` is ``"cpu"`` for the aggregate and ``"cpuN"`` for a single logical
    processor.  These are counters since boot, not a utilization: a percentage
    only exists between two readings, which is why nothing here is a percent.
    Kernels that stop early (no ``guest_nice``, or no ``steal`` at all) leave
    the trailing fields ``None`` rather than reporting a zero the host never
    published.
    """

    name: str
    user: Optional[int] = None
    nice: Optional[int] = None
    system: Optional[int] = None
    idle: Optional[int] = None
    iowait: Optional[int] = None
    irq: Optional[int] = None
    softirq: Optional[int] = None
    steal: Optional[int] = None
    guest: Optional[int] = None
    guest_nice: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text(self.name, "cpu times name")
        if not self.name:
            raise ValueError("cpu times name must not be empty")
        for field_name in CPU_TIME_FIELDS:
            _require_optional_count(getattr(self, field_name), f"cpu times {field_name}")

    @property
    def total(self) -> Optional[int]:
        """Every reported jiffy, or ``None`` when the host reported none.

        ``guest`` and ``guest_nice`` are deliberately excluded: the kernel
        already counts guest time inside ``user`` and ``nice``, so adding them
        again would inflate the denominator and understate every share.
        """

        values = [
            getattr(self, name)
            for name in CPU_TIME_FIELDS[:8]
            if getattr(self, name) is not None
        ]
        return sum(values) if values else None


@dataclass(frozen=True)
class CpuUtilization:
    """A share of CPU time between two :class:`CpuTimes` readings, in percent.

    Every field is a percentage of that window, so they sum to roughly 100 for
    the aggregate and for each core alike.  ``total`` is ``100 - idle`` and
    therefore *includes* ``iowait``, which is also reported separately: a host
    stalled on storage is not idle, but it is not computing either.
    """

    total: Optional[float] = None
    user: Optional[float] = None
    system: Optional[float] = None
    idle: Optional[float] = None
    iowait: Optional[float] = None
    irq: Optional[float] = None
    softirq: Optional[float] = None
    steal: Optional[float] = None
    nice: Optional[float] = None

    def __post_init__(self) -> None:
        for name in (
            "total",
            "user",
            "system",
            "idle",
            "iowait",
            "irq",
            "softirq",
            "steal",
            "nice",
        ):
            _require_optional_number(getattr(self, name), f"cpu utilization {name}")


@dataclass(frozen=True)
class ProcessCounts:
    """How many processes the host is running, and how it classifies them.

    ``pid_max`` is the kernel's ceiling, so ``total`` can be shown against a
    real denominator instead of against nothing.
    """

    total: Optional[int] = None
    running: Optional[int] = None
    sleeping: Optional[int] = None
    stopped: Optional[int] = None
    zombie: Optional[int] = None
    threads: Optional[int] = None
    pid_max: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "total",
            "running",
            "sleeping",
            "stopped",
            "zombie",
            "threads",
            "pid_max",
        ):
            _require_optional_count(getattr(self, name), f"process counts {name}")


@dataclass(frozen=True)
class MemoryInfo:
    """``/proc/meminfo`` values in bytes.

    ``available_bytes`` is ``None`` when the host does not publish
    ``MemAvailable``; callers must decide what to show rather than silently
    substituting ``MemFree`` or the total.
    """

    total_bytes: int = 0
    free_bytes: int = 0
    available_bytes: Optional[int] = None
    cached_bytes: int = 0
    buffers_bytes: int = 0
    swap_total_bytes: int = 0
    swap_free_bytes: int = 0
    #: Optional because a host that does not publish the field is not a host
    #: reporting zero of it. BusyBox and older kernels omit several.
    active_bytes: Optional[int] = None
    inactive_bytes: Optional[int] = None
    shmem_bytes: Optional[int] = None
    dirty_bytes: Optional[int] = None
    writeback_bytes: Optional[int] = None
    slab_bytes: Optional[int] = None
    slab_reclaimable_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "total_bytes",
            "free_bytes",
            "cached_bytes",
            "buffers_bytes",
            "swap_total_bytes",
            "swap_free_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ValueError(f"memory {name} must be a non-negative integer")
        for name in (
            "available_bytes",
            "active_bytes",
            "inactive_bytes",
            "shmem_bytes",
            "dirty_bytes",
            "writeback_bytes",
            "slab_bytes",
            "slab_reclaimable_bytes",
        ):
            _require_optional_count(getattr(self, name), f"memory {name}")

    @property
    def used_bytes(self) -> Optional[int]:
        """Bytes in use, or ``None`` when the host publishes no availability."""

        if self.available_bytes is None or not self.total_bytes:
            return None
        return max(0, self.total_bytes - self.available_bytes)

    @property
    def swap_used_bytes(self) -> int:
        return max(0, self.swap_total_bytes - self.swap_free_bytes)


@dataclass(frozen=True)
class LoadAverage:
    one: float
    five: float
    fifteen: float

    def __post_init__(self) -> None:
        for name in ("one", "five", "fifteen"):
            value = getattr(self, name)
            if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
                raise ValueError(f"load average {name} must be a non-negative number")


@dataclass(frozen=True)
class FilesystemUsage:
    """One mounted filesystem, with every size already normalised to bytes."""

    device: str
    mount_point: str
    fstype: str = ""
    size_bytes: Optional[int] = None
    used_bytes: Optional[int] = None
    available_bytes: Optional[int] = None
    use_percent: Optional[int] = None
    #: Mount options as the kernel lists them, comma separated ("ro,noatime").
    options: str = ""
    #: Inodes are the other way a filesystem fills up: a host can sit at 3% of
    #: its bytes and still fail every write with ENOSPC.
    inodes_total: Optional[int] = None
    inodes_used: Optional[int] = None
    inodes_free: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text(self.device, "filesystem device")
        _require_text(self.mount_point, "filesystem mount point")
        _require_text(self.fstype, "filesystem type")
        _require_text(self.options, "filesystem options")
        for name in (
            "size_bytes",
            "used_bytes",
            "available_bytes",
            "inodes_total",
            "inodes_used",
            "inodes_free",
        ):
            _require_optional_count(getattr(self, name), f"filesystem {name}")
        if self.use_percent is not None and (
            type(self.use_percent) is not int
            or isinstance(self.use_percent, bool)
            or not 0 <= self.use_percent <= 100
        ):
            raise ValueError("filesystem use_percent must be a percentage or None")

    @property
    def used_fraction(self) -> Optional[float]:
        if self.size_bytes and self.used_bytes is not None:
            return self.used_bytes / self.size_bytes
        if self.use_percent is not None:
            return self.use_percent / 100.0
        return None

    @property
    def inodes_used_fraction(self) -> Optional[float]:
        if self.inodes_total and self.inodes_used is not None:
            return self.inodes_used / self.inodes_total
        return None

    @property
    def read_only(self) -> bool:
        return "ro" in self.options.split(",")


@dataclass(frozen=True)
class InterfaceCounters:
    """Cumulative byte counters for one interface since the host booted."""

    name: str
    rx_bytes: int
    tx_bytes: int

    def __post_init__(self) -> None:
        _require_text(self.name, "interface name")
        if not self.name:
            raise ValueError("interface name must not be empty")
        for field_name in ("rx_bytes", "tx_bytes"):
            value = getattr(self, field_name)
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ValueError(f"interface {field_name} must be a non-negative integer")


@dataclass(frozen=True)
class NetworkInterface:
    name: str
    kind: NetworkInterfaceKind = NetworkInterfaceKind.UNKNOWN
    state: NetworkInterfaceState = NetworkInterfaceState.UNKNOWN
    mac_address: str = ""
    mtu: Optional[int] = None
    ipv4_addresses: Tuple[str, ...] = ()
    ipv6_addresses: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.name, "interface name")
        if not self.name:
            raise ValueError("interface name must not be empty")
        if not isinstance(self.kind, NetworkInterfaceKind):
            raise TypeError("interface kind must be a NetworkInterfaceKind")
        if not isinstance(self.state, NetworkInterfaceState):
            raise TypeError("interface state must be a NetworkInterfaceState")
        _require_text(self.mac_address, "interface mac address")
        _require_optional_count(self.mtu, "interface mtu")
        _require_text_tuple(
            self.ipv4_addresses, "interface ipv4 addresses", MAX_HOST_INFO_ADDRESSES
        )
        _require_text_tuple(
            self.ipv6_addresses, "interface ipv6 addresses", MAX_HOST_INFO_ADDRESSES
        )


@dataclass(frozen=True)
class TemperatureReading:
    label: str
    celsius: float

    def __post_init__(self) -> None:
        _require_text(self.label, "temperature label")
        if type(self.celsius) not in (int, float) or isinstance(self.celsius, bool):
            raise TypeError("temperature must be a number")


@dataclass(frozen=True)
class LoginSession:
    """One logged-in user.

    ``origin`` is empty for a local console login; ``remote`` says whether the
    session arrived over the network, so frontends never have to re-derive it
    from display-name heuristics such as ``":0"``.
    """

    user: str = ""
    tty: str = ""
    origin: str = ""
    since: str = ""
    remote: bool = False

    def __post_init__(self) -> None:
        for name in ("user", "tty", "origin", "since"):
            _require_text(getattr(self, name), f"login session {name}")
        if type(self.remote) is not bool:
            raise TypeError("login session remote must be a boolean")


@dataclass(frozen=True)
class SocketConnection:
    protocol: str
    local_address: str = ""
    local_port: Optional[int] = None
    peer_address: str = ""
    peer_port: Optional[int] = None
    process: str = ""
    direction: SocketDirection = SocketDirection.OUTGOING

    def __post_init__(self) -> None:
        for name in ("protocol", "local_address", "peer_address", "process"):
            _require_text(getattr(self, name), f"socket {name}")
        _require_port(self.local_port, "socket local port")
        _require_port(self.peer_port, "socket peer port")
        if not isinstance(self.direction, SocketDirection):
            raise TypeError("socket direction must be a SocketDirection")


@dataclass(frozen=True)
class ListeningPort:
    """One TCP port the host accepts connections on."""

    port: int
    process: str = ""

    def __post_init__(self) -> None:
        if type(self.port) is not int or isinstance(self.port, bool) or not (
            0 <= self.port <= 65535
        ):
            raise ValueError("listening port must be a TCP port")
        _require_text(self.process, "listening port process")


@dataclass(frozen=True)
class ProcessUsage:
    """One process as the host ranked it, by CPU share.

    ``cpu_percent`` is a share of one CPU as the host reports it, so a busy
    multi-threaded process can exceed 100 on a multi-core box.  Both readings
    are ``None`` when the host published a name but no numbers.
    """

    command: str
    cpu_percent: Optional[float] = None
    memory_percent: Optional[float] = None

    def __post_init__(self) -> None:
        _require_text(self.command, "process command")
        _require_optional_number(self.cpu_percent, "process cpu percent")
        _require_optional_number(self.memory_percent, "process memory percent")


@dataclass(frozen=True)
class FailedUnit:
    """One systemd unit in the failed state; hosts without systemd report none."""

    name: str
    description: str = ""

    def __post_init__(self) -> None:
        _require_text(self.name, "failed unit name")
        _require_text(self.description, "failed unit description")


@dataclass(frozen=True)
class HostKeyFingerprint:
    """A public SSH host key fingerprint, as ``ssh-keygen -l`` printed it.

    Public material only: the probe reads ``/etc/ssh/*.pub`` and never a
    private key file.
    """

    algorithm: str
    fingerprint: str
    bits: Optional[int] = None

    def __post_init__(self) -> None:
        _require_text(self.algorithm, "host key algorithm")
        _require_text(self.fingerprint, "host key fingerprint")
        _require_optional_count(self.bits, "host key bits")


@dataclass(frozen=True)
class PressureStall:
    """One ``/proc/pressure`` line: percent of a window spent stalled.

    ``some`` means at least one task was waiting; ``full`` means every
    non-idle task was.  Kernels before 4.20 and builds without PSI publish
    nothing, which is why the snapshot holds ``None`` rather than zeros.
    """

    avg10: float
    avg60: float
    avg300: float

    def __post_init__(self) -> None:
        for name in ("avg10", "avg60", "avg300"):
            value = getattr(self, name)
            if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
                raise ValueError(f"pressure {name} must be a non-negative number")


@dataclass(frozen=True)
class HostInfoSnapshot:
    """Everything one full probe observed about a remote host."""

    hostname: str = ""
    device_model: str = ""
    os_pretty_name: str = ""
    kernel: str = ""
    uptime_seconds: Optional[float] = None
    boot_time: str = ""
    cpu: CpuInfo = field(default_factory=CpuInfo)
    memory: MemoryInfo = field(default_factory=MemoryInfo)
    load_average: Optional[LoadAverage] = None
    filesystems: Tuple[FilesystemUsage, ...] = ()
    interfaces: Tuple[NetworkInterface, ...] = ()
    temperatures: Tuple[TemperatureReading, ...] = ()
    sessions: Tuple[LoginSession, ...] = ()
    sockets: Tuple[SocketConnection, ...] = ()
    default_gateway: str = ""
    default_gateway_interface: str = ""
    dns_servers: Tuple[str, ...] = ()
    ssh_port: Optional[int] = None
    ssh_process: str = ""
    os_id: str = ""
    os_version_id: str = ""
    architecture: str = ""
    listening_ports: Tuple[ListeningPort, ...] = ()
    processes: Tuple[ProcessUsage, ...] = ()
    failed_units: Tuple[FailedUnit, ...] = ()
    host_keys: Tuple[HostKeyFingerprint, ...] = ()
    io_pressure_some: Optional[PressureStall] = None
    io_pressure_full: Optional[PressureStall] = None
    cpu_pressure_some: Optional[PressureStall] = None
    cpu_pressure_full: Optional[PressureStall] = None
    memory_pressure_some: Optional[PressureStall] = None
    memory_pressure_full: Optional[PressureStall] = None
    #: The aggregate line first, then one per logical processor. Counters, not
    #: percentages: this is the baseline a live sample differences against.
    cpu_times: Tuple[CpuTimes, ...] = ()
    process_counts: Optional[ProcessCounts] = None
    context_switches: Optional[int] = None
    interrupts: Optional[int] = None

    def __post_init__(self) -> None:
        for name in (
            "hostname",
            "device_model",
            "os_pretty_name",
            "kernel",
            "boot_time",
            "default_gateway",
            "default_gateway_interface",
            "ssh_process",
            "os_id",
            "os_version_id",
            "architecture",
        ):
            _require_text(getattr(self, name), f"host info {name}")
        if self.uptime_seconds is not None and (
            type(self.uptime_seconds) not in (int, float)
            or isinstance(self.uptime_seconds, bool)
            or self.uptime_seconds < 0
        ):
            raise ValueError("uptime must be a non-negative number or None")
        if type(self.cpu) is not CpuInfo:
            raise TypeError("cpu must be a CpuInfo")
        if type(self.memory) is not MemoryInfo:
            raise TypeError("memory must be a MemoryInfo")
        if self.load_average is not None and type(self.load_average) is not LoadAverage:
            raise TypeError("load average must be a LoadAverage or None")
        for name, item_type, limit in (
            ("filesystems", FilesystemUsage, MAX_HOST_INFO_FILESYSTEMS),
            ("interfaces", NetworkInterface, MAX_HOST_INFO_INTERFACES),
            ("temperatures", TemperatureReading, MAX_HOST_INFO_TEMPERATURES),
            ("sessions", LoginSession, MAX_HOST_INFO_SESSIONS),
            ("sockets", SocketConnection, MAX_HOST_INFO_SOCKETS),
            ("listening_ports", ListeningPort, MAX_HOST_INFO_LISTENING_PORTS),
            ("processes", ProcessUsage, MAX_HOST_INFO_PROCESSES),
            ("failed_units", FailedUnit, MAX_HOST_INFO_FAILED_UNITS),
            ("host_keys", HostKeyFingerprint, MAX_HOST_INFO_HOST_KEYS),
            ("cpu_times", CpuTimes, MAX_HOST_INFO_CPU_TIMES),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise TypeError(f"host info {name} must be a tuple")
            if len(value) > limit:
                raise ValueError(f"host info {name} exceeds the supported length")
            for item in value:
                if type(item) is not item_type:
                    raise TypeError(f"host info {name} entries are the wrong type")
        _require_text_tuple(self.dns_servers, "dns servers", MAX_HOST_INFO_DNS_SERVERS)
        _require_port(self.ssh_port, "ssh port")
        for name in (
            "io_pressure_some",
            "io_pressure_full",
            "cpu_pressure_some",
            "cpu_pressure_full",
            "memory_pressure_some",
            "memory_pressure_full",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not PressureStall:
                raise TypeError(f"host info {name} must be a PressureStall or None")
        if self.process_counts is not None and type(self.process_counts) is not ProcessCounts:
            raise TypeError("host info process_counts must be a ProcessCounts or None")
        for name in ("context_switches", "interrupts"):
            _require_optional_count(getattr(self, name), f"host info {name}")

    @property
    def root_filesystem(self) -> Optional[FilesystemUsage]:
        """The filesystem backing the root of the host, if it reported one.

        OpenWrt mounts a read-only squashfs at ``/`` and keeps writable state
        on ``/overlay``, so ``/overlay`` is the meaningful "root" there and is
        preferred when present.
        """

        by_mount = {item.mount_point: item for item in self.filesystems}
        return by_mount.get("/overlay") or by_mount.get("/")


@dataclass(frozen=True)
class LiveSample:
    """One reading of the cheap probe the dialog repeats while it is open.

    Counters (``counters``, ``cpu_times``) mean nothing on their own -- a rate
    is the difference between two of these -- while ``memory`` and
    ``load_average`` are instantaneous and usable from the first sample.
    """

    counters: Tuple[InterfaceCounters, ...] = ()
    cpu_times: Tuple[CpuTimes, ...] = ()
    memory: Optional[MemoryInfo] = None
    load_average: Optional[LoadAverage] = None

    def __post_init__(self) -> None:
        for name, item_type, limit in (
            ("counters", InterfaceCounters, MAX_HOST_INFO_INTERFACES),
            ("cpu_times", CpuTimes, MAX_HOST_INFO_CPU_TIMES),
        ):
            value = getattr(self, name)
            if type(value) is not tuple:
                raise TypeError(f"live sample {name} must be a tuple")
            if len(value) > limit:
                raise ValueError(f"live sample {name} exceeds the supported length")
            for item in value:
                if type(item) is not item_type:
                    raise TypeError(f"live sample {name} entries are the wrong type")
        if self.memory is not None and type(self.memory) is not MemoryInfo:
            raise TypeError("live sample memory must be a MemoryInfo or None")
        if self.load_average is not None and type(self.load_average) is not LoadAverage:
            raise TypeError("live sample load average must be a LoadAverage or None")


@dataclass(frozen=True)
class HostInfoRequest:
    connection_id: ConnectionId
    probe: HostInfoProbe = HostInfoProbe.FULL

    def __post_init__(self) -> None:
        require_identifier(self.connection_id, "connection id")
        if not isinstance(self.probe, HostInfoProbe):
            raise TypeError("probe must be a HostInfoProbe")


@dataclass(frozen=True)
class HostInfoSummary:
    """A host-info operation plus whatever it has produced so far.

    ``snapshot`` is populated only for a completed ``FULL`` probe.  ``counters``
    is populated by every probe so a frontend can sample bandwidth without
    paying for the full gather, and ``live`` carries the rest of what the
    ``LIVE`` probe read.
    """

    operation: OperationSummary
    probe: HostInfoProbe = HostInfoProbe.FULL
    snapshot: Optional[HostInfoSnapshot] = None
    counters: Tuple[InterfaceCounters, ...] = ()
    failure: Optional[HostInfoFailure] = None
    live: Optional[LiveSample] = None

    def __post_init__(self) -> None:
        if type(self.operation) is not OperationSummary:
            raise TypeError("operation must be an OperationSummary")
        if not isinstance(self.probe, HostInfoProbe):
            raise TypeError("probe must be a HostInfoProbe")
        if self.snapshot is not None and type(self.snapshot) is not HostInfoSnapshot:
            raise TypeError("snapshot must be a HostInfoSnapshot or None")
        if type(self.counters) is not tuple or any(
            type(item) is not InterfaceCounters for item in self.counters
        ):
            raise TypeError("counters must be a tuple of InterfaceCounters")
        if len(self.counters) > MAX_HOST_INFO_INTERFACES:
            raise ValueError("host info counters exceed the supported length")
        if self.failure is not None and type(self.failure) is not HostInfoFailure:
            raise TypeError("failure must be a HostInfoFailure or None")
        if self.live is not None and type(self.live) is not LiveSample:
            raise TypeError("live must be a LiveSample or None")
