"""Host-info probe parsing on Darwin and the BSDs.

The FreeBSD fixtures below are a real capture: the probe in
``sshpilot.core.host_info.probe`` run over SSH against a FreeBSD 14.5-RELEASE
guest, trimmed but not reshaped.  The Darwin fixtures are written from the
documented output of the same tools, since a Mac cannot be booted here -- so a
failure in a Darwin test means this parser changed, while confidence that the
*shape* is right rests on the FreeBSD capture and on the two families printing
these tools alike.
"""

from __future__ import annotations

from sshpilot.api.models.host_info import (
    NetworkInterfaceKind,
    NetworkInterfaceState,
    SocketDirection,
)
from sshpilot.core.host_info import (
    parse_counters_probe,
    parse_host_info,
    parse_live_probe,
)
from sshpilot.core.host_info import bsd
from sshpilot.core.host_info.parser import parse_inode_usage, parse_listening_ports


def _probe(**sections: str) -> str:
    return "".join(f"==={name}===\n{body}\n" for name, body in sections.items())


# ---------------------------------------------------------------------------
# Captured from FreeBSD 14.5-RELEASE
# ---------------------------------------------------------------------------

FREEBSD_SYSCTL = """hw.model: Intel(R) Xeon(R) CPU           X5675  @ 3.07GHz
hw.machine: amd64
hw.ncpu: 2
hw.physmem: 2111283200
hw.realmem: 2147483648
hw.pagesize: 4096
hw.clockrate: 3059
kern.ostype: FreeBSD
kern.osrelease: 14.5-RELEASE
kern.boottime: { sec = 1789900651, usec = 837885 } Sun Sep 20 10:37:31 2026
kern.maxproc: 7396
kern.pid_max: 99999
kern.cp_time: 321 0 188 10 61683
kern.cp_times: 116 0 85 4 30896 205 0 103 6 30787
vm.loadavg: { 0.32 0.16 0.06 }
vm.stats.vm.v_page_size: 4096
vm.stats.vm.v_free_count: 482182
vm.stats.vm.v_inactive_count: 528
vm.stats.vm.v_cache_count: 0
vm.stats.vm.v_active_count: 3052
vm.stats.vm.v_wire_count: 15840"""

FREEBSD_NETSTAT_IB = (
    "Name     Mtu Network           Address                           Ipkts Ierrs Idrop     Ibytes    Opkts Oerrs     Obytes  Coll\n"
    "vtnet0  1500 <Link#1>          52:54:00:12:34:56                   155     0     0      30412      136     0      27564     0\n"
    "vtnet0     - 10.0.2.0/24       10.0.2.15                           150     -     -      26364      128     -      24696     -\n"
    "lo0    16384 <Link#2>          lo0                                   0     0     0          0        0     0          0     0\n"
)

FREEBSD_IFCONFIG = """vtnet0: flags=1008843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST,LOWER_UP> metric 0 mtu 1500
\toptions=880028<VLAN_MTU,JUMBO_MTU,LINKSTATE,HWSTATS>
\tether 52:54:00:12:34:56
\tinet 10.0.2.15 netmask 0xffffff00 broadcast 10.0.2.255
\tinet6 fe80::5054:ff:fe12:3456%vtnet0 prefixlen 64 scopeid 0x1
\tmedia: Ethernet autoselect (10Gbase-T <full-duplex>)
\tstatus: active
lo0: flags=1008049<UP,LOOPBACK,RUNNING,MULTICAST,LOWER_UP> metric 0 mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x2"""

FREEBSD_NETSTAT_AN = """Active Internet connections (including servers)
Proto     Recv-Q Send-Q Local Address          Foreign Address        (state)
tcp4           0      0 10.0.2.15.22           10.0.2.2.35190         ESTABLISHED
tcp4           0      0 *.22                   *.*                    LISTEN
tcp6           0      0 *.22                   *.*                    LISTEN
tcp4           0      0 10.0.2.15.22           10.0.2.2.45842         TIME_WAIT"""

FREEBSD_NETSTAT_RN = """Routing tables

Internet:
Destination        Gateway            Flags         Netif Expire
default            10.0.2.2           UGS          vtnet0
10.0.2.0/24        link#1             U            vtnet0"""

FREEBSD_DF = """Filesystem      1024-blocks    Used   Avail Capacity  Mounted on
/dev/gpt/rootfs     5061648 2466536 2190184    53%    /
devfs                     1       0       1     0%    /dev
/dev/gpt/efiesp       32764     647   32117     2%    /boot/efi"""

FREEBSD_DF_INODES = """Filesystem      1K-blocks    Used   Avail Capacity iused  ifree %iused  Mounted on
/dev/gpt/rootfs   5061648 2466536 2190184    53%   30615 691687    4%   /
devfs                   1       0       1     0%       0      0     -   /dev
/dev/gpt/efiesp     32764     647   32117     2%       0      0     -   /boot/efi"""

FREEBSD_MOUNT = """/dev/gpt/rootfs\t\t/\t\t\tufs\trw\t\t1 1
devfs\t\t\t/dev\t\t\tdevfs\trw\t\t0 0
/dev/gpt/efiesp\t\t/boot/efi\t\tmsdosfs\trw\t\t2 2"""

FREEBSD_PROCESSES = """ %CPU %MEM COMMAND
197.7  0.0 idle
  0.0  0.1 init
  0.0  0.0 pagedaemon"""


def _freebsd_probe(**overrides: str) -> str:
    sections = dict(
        OSTYPE="FreeBSD",
        HOSTNAME="freebsd",
        DEVICE_MODEL="Standard PC (i440FX + PIIX, 1996)",
        OS_RELEASE='PRETTY_NAME="FreeBSD 14.5-RELEASE"\nID=freebsd\nVERSION_ID="14.5"',
        UNAME="FreeBSD 14.5-RELEASE amd64",
        SYSCTL=FREEBSD_SYSCTL,
        NOW_EPOCH="1789900897",
        BOOT_TIME="                 system boot  Sep 20 10:37 ",
        NETSTAT_IB=FREEBSD_NETSTAT_IB,
        IFCONFIG=FREEBSD_IFCONFIG,
        NETSTAT_RN=FREEBSD_NETSTAT_RN,
        NETSTAT_AN=FREEBSD_NETSTAT_AN,
        DF=FREEBSD_DF,
        DF_INODES=FREEBSD_DF_INODES,
        BSD_MOUNT=FREEBSD_MOUNT,
        PROCESSES=FREEBSD_PROCESSES,
        PROC_STATES="DLs\nILs\nRNL\nSs\nIs",
        WHO="root             ttyu0        Sep 20 10:37 ",
        DNS="nameserver 10.0.2.3",
        SSH_CONNECTION="10.0.2.2 35190 10.0.2.15 22",
    )
    sections.update(overrides)
    return _probe(**sections)


# ---------------------------------------------------------------------------
# Identity, uptime
# ---------------------------------------------------------------------------

def test_freebsd_snapshot_reads_identity_from_the_host():
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.hostname == "freebsd"
    assert snapshot.os_pretty_name == "FreeBSD 14.5-RELEASE"
    assert snapshot.kernel == "FreeBSD 14.5-RELEASE amd64"
    assert snapshot.architecture == "amd64"


def test_os_release_is_reconstructed_where_the_file_is_missing():
    """Darwin ships no /etc/os-release, and neither do the older BSDs."""
    snapshot = parse_host_info(_freebsd_probe(OS_RELEASE=""))
    assert snapshot.os_pretty_name == "FreeBSD 14.5-RELEASE"
    assert snapshot.os_id == "freebsd"
    assert snapshot.os_version_id == "14.5"


def test_sw_vers_names_a_mac():
    pretty, identifier, version = bsd.os_identity(
        "ProductName:\t\tmacOS\nProductVersion:\t\t15.2\nBuildVersion:\t\t24C101\n", {}
    )
    assert (pretty, identifier, version) == ("macOS 15.2", "macos", "15.2")


def test_uptime_is_measured_against_the_hosts_own_clock():
    """1789900897 - 1789900651 = 246 seconds, and the printed date is kept."""
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.uptime_seconds == 246.0
    assert snapshot.boot_time == "Sun Sep 20 10:37:31 2026"


def test_uptime_is_unreported_without_the_hosts_clock():
    """Measuring kern.boottime against *our* clock would report the skew
    between two machines as the remote host's uptime."""
    snapshot = parse_host_info(_freebsd_probe(NOW_EPOCH=""))
    assert snapshot.uptime_seconds is None
    # The instant itself is still known, so the boot time survives.
    assert snapshot.boot_time == "Sun Sep 20 10:37:31 2026"


def test_who_b_does_not_outrank_the_boot_instant_on_bsd():
    """``who -b`` prints "system boot Sep 20 10:37" there -- no year, and the
    generic reader keeps only its last two fields, which would be "20 10:37"."""
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.boot_time.startswith("Sun Sep 20")


# ---------------------------------------------------------------------------
# CPU
# ---------------------------------------------------------------------------

def test_cpu_model_comes_from_hw_model_on_the_bsds():
    snapshot = parse_host_info(_freebsd_probe())
    assert "Xeon" in snapshot.cpu.model
    assert snapshot.cpu.logical_processors == 2
    assert snapshot.cpu.frequency_mhz == 3059.0


def test_hw_model_is_not_read_as_a_cpu_on_darwin():
    """There hw.model is the machine ("Macmini9,1"); the CPU has its own key."""
    darwin = {"hw.model": "Macmini9,1",
              "machdep.cpu.brand_string": "Apple M1",
              "hw.logicalcpu": "8", "hw.physicalcpu": "8"}
    cpu = bsd.cpu_info(darwin, "Darwin")
    assert cpu.model == "Apple M1"
    assert cpu.logical_processors == 8
    assert (cpu.cores_per_socket, cpu.sockets) == (8, 1)


def test_cp_time_becomes_the_aggregate_and_one_line_per_core():
    snapshot = parse_host_info(_freebsd_probe())
    names = [item.name for item in snapshot.cpu_times]
    assert names == ["cpu", "cpu0", "cpu1"]
    aggregate = snapshot.cpu_times[0]
    assert (aggregate.user, aggregate.nice, aggregate.system) == (321, 0, 188)
    # "intr" is what Linux calls irq; the fields BSD does not count stay None
    # rather than becoming zeros that would dilute every computed share.
    assert aggregate.irq == 10
    assert aggregate.idle == 61683
    assert aggregate.iowait is None
    assert aggregate.steal is None


def test_darwin_reports_no_cpu_counters_rather_than_fake_ones():
    """Darwin publishes no cumulative tick counter to the shell.  Reporting
    none leaves the utilization unavailable; inventing one would not."""
    assert bsd.cpu_times({"hw.memsize": "17179869184"}) == ()


def test_load_average_survives_its_braces():
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.load_average.one == 0.32
    assert snapshot.load_average.fifteen == 0.06


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def test_freebsd_memory_is_counted_in_pages():
    snapshot = parse_host_info(_freebsd_probe())
    memory = snapshot.memory
    assert memory.total_bytes == 2111283200
    assert memory.free_bytes == 482182 * 4096
    # Inactive and cached pages are reclaimable, so available is more than
    # free -- the same relation MemAvailable has on Linux.
    assert memory.available_bytes == (482182 + 528 + 0) * 4096
    assert memory.available_bytes > memory.free_bytes


def test_swapinfo_sums_the_devices_and_skips_its_own_total():
    text = (
        "Device          1K-blocks     Used    Avail Capacity\n"
        "/dev/gpt/swap0    1048576        0  1048576     0%\n"
        "/dev/gpt/swap1    1048576   524288   524288    50%\n"
        "Total             2097152   524288  1572864    25%\n"
    )
    total, free = bsd.parse_swapinfo(text)
    assert total == 2097152 * 1024
    assert free == (1048576 + 524288) * 1024


DARWIN_VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               65536.
Pages active:                            131072.
Pages inactive:                           32768.
Pages speculative:                         8192.
Pages wired down:                         49152.
Pages purgeable:                           4096.
File-backed pages:                        16384.
Anonymous pages:                         147456."""


def test_darwin_memory_reads_vm_stat_against_hw_memsize():
    sysctl = {
        "hw.memsize": "17179869184",
        "vm.swapusage": "total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)",
    }
    memory = bsd.memory(sysctl, DARWIN_VM_STAT, "")
    assert memory.total_bytes == 17179869184
    assert memory.free_bytes == 65536 * 16384
    assert memory.cached_bytes == 16384 * 16384
    # free + speculative + inactive + purgeable
    assert memory.available_bytes == (65536 + 8192 + 32768 + 4096) * 16384
    assert memory.swap_total_bytes == 2048 * 1024 ** 2
    assert memory.swap_free_bytes == 1536 * 1024 ** 2


def test_a_host_that_reported_no_memory_reports_none():
    assert bsd.memory({}, "", "").total_bytes == 0


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_posix_df_blocks_are_read_as_1024_bytes():
    snapshot = parse_host_info(_freebsd_probe())
    root = next(fs for fs in snapshot.filesystems if fs.mount_point == "/")
    assert root.size_bytes == 5061648 * 1024
    assert root.use_percent == 53


def test_filesystem_type_comes_from_mount_where_df_has_no_column():
    snapshot = parse_host_info(_freebsd_probe())
    types = {fs.mount_point: fs.fstype for fs in snapshot.filesystems}
    assert types == {"/": "ufs", "/boot/efi": "msdosfs"}
    assert "/dev" not in types  # devfs is kernel memory, not storage


def test_bsd_df_inodes_are_found_by_name_not_position():
    """That table prints the block columns *and* the inode ones."""
    usage = parse_inode_usage(FREEBSD_DF_INODES)
    assert usage["/"] == (30615 + 691687, 30615, 691687)


def test_a_filesystem_without_an_inode_table_reports_unknown():
    """msdosfs prints zeros there; nought inodes would render as a full bar."""
    usage = parse_inode_usage(FREEBSD_DF_INODES)
    assert usage["/boot/efi"] == (None, None, None)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def test_ifconfig_addresses_carry_their_prefix_length():
    snapshot = parse_host_info(_freebsd_probe())
    interfaces = {item.name: item for item in snapshot.interfaces}
    assert interfaces["vtnet0"].ipv4_addresses == ("10.0.2.15/24",)
    assert interfaces["vtnet0"].mac_address == "52:54:00:12:34:56"
    assert interfaces["vtnet0"].mtu == 1500


def test_a_link_local_address_drops_its_zone():
    """``ip -o addr`` prints none, so neither does this."""
    snapshot = parse_host_info(_freebsd_probe())
    interfaces = {item.name: item for item in snapshot.interfaces}
    assert "fe80::5054:ff:fe12:3456/64" in interfaces["vtnet0"].ipv6_addresses
    assert not any("%" in a for a in interfaces["lo0"].ipv6_addresses)


def test_interface_state_reads_status_then_flags():
    snapshot = parse_host_info(_freebsd_probe())
    interfaces = {item.name: item for item in snapshot.interfaces}
    assert interfaces["vtnet0"].state is NetworkInterfaceState.UP
    assert interfaces["vtnet0"].kind is NetworkInterfaceKind.ETHERNET
    # Loopback prints no status line at all, so its flags are the whole truth.
    assert interfaces["lo0"].state is NetworkInterfaceState.UP
    assert interfaces["lo0"].kind is NetworkInterfaceKind.LOOPBACK


def test_a_radio_is_recognised_by_its_media_line():
    text = (
        "wlan0: flags=8843<UP,BROADCAST,RUNNING,SIMPLEX,MULTICAST> metric 0 mtu 1500\n"
        "\tether 00:11:22:33:44:55\n"
        "\tmedia: IEEE 802.11 Wireless Ethernet OFDM/54Mbps mode 11a\n"
        "\tstatus: associated\n"
    )
    interfaces = bsd.parse_ifconfig(text)
    assert interfaces[0].kind is NetworkInterfaceKind.WIRELESS


def test_interface_counters_are_read_from_the_link_row():
    """The per-network rows below it print "-" in the error columns and repeat
    or omit the totals."""
    counters = {item.name: item for item in bsd.parse_netstat_counters(FREEBSD_NETSTAT_IB)}
    assert (counters["vtnet0"].rx_bytes, counters["vtnet0"].tx_bytes) == (30412, 27564)
    assert set(counters) == {"vtnet0", "lo0"}


def test_counter_columns_are_counted_from_the_right():
    """An interface with no link-layer address leaves the Address column
    empty, which shifts every field after it when the line is split."""
    text = (
        "Name  Mtu   Network       Address            Ipkts Ierrs     Ibytes    Opkts Oerrs     Obytes  Coll\n"
        "gif0  1280  <Link#5>                             7     0        560       11     0        880     0\n"
    )
    counters = bsd.parse_netstat_counters(text)
    assert counters == (
        type(counters[0])("gif0", 560, 880),
    )


def test_default_route_finds_the_interface_column_by_name():
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.default_gateway == "10.0.2.2"
    assert snapshot.default_gateway_interface == "vtnet0"


def test_darwin_route_table_with_extra_columns():
    text = (
        "Internet:\n"
        "Destination        Gateway            Flags        Refs      Use   Netif Expire\n"
        "default            192.168.1.1        UGScg          12      340     en0\n"
    )
    assert bsd.parse_default_route(text) == ("192.168.1.1", "en0")


# ---------------------------------------------------------------------------
# Sockets: BSD spells an endpoint address.port
# ---------------------------------------------------------------------------

def test_listening_ports_are_read_from_dotted_endpoints():
    snapshot = parse_host_info(_freebsd_probe())
    assert [(p.port, p.address) for p in snapshot.listening_ports] == [(22, "*")]


def test_only_established_sockets_become_connections():
    """That listing prints LISTEN and TIME_WAIT rows in the same table."""
    snapshot = parse_host_info(_freebsd_probe())
    assert len(snapshot.sockets) == 1
    socket = snapshot.sockets[0]
    assert (socket.local_address, socket.local_port) == ("10.0.2.15", 22)
    assert (socket.peer_address, socket.peer_port) == ("10.0.2.2", 35190)
    assert socket.direction is SocketDirection.INCOMING


def test_an_ipv6_endpoint_keeps_its_colons():
    """``fe80::1%lo0.22`` is a dotted port on a colon-bearing address."""
    text = (
        "Active Internet connections (including servers)\n"
        "Proto Recv-Q Send-Q Local Address      Foreign Address    (state)\n"
        "tcp6       0      0 fe80::1%lo0.22     *.*                LISTEN\n"
    )
    ports = parse_listening_ports(bsd.normalise_endpoints(text))
    assert 22 in ports
    assert ports[22][0] == "fe80::1%lo0"


def test_the_ssh_port_is_the_one_the_probe_arrived_on():
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.ssh_port == 22


# ---------------------------------------------------------------------------
# Processes and sessions
# ---------------------------------------------------------------------------

def test_bsd_ps_output_is_ranked_by_cpu():
    snapshot = parse_host_info(_freebsd_probe())
    assert snapshot.processes[0].command == "idle"
    assert snapshot.processes[0].cpu_percent == 197.7


def test_darwin_ps_heads_the_column_comm():
    text = " %CPU %MEM COMM\n  4.2  1.1 WindowServer\n  0.9  2.0 firefox\n"
    snapshot = parse_host_info(_probe(OSTYPE="Darwin", PROCESSES=text))
    assert [p.command for p in snapshot.processes] == ["WindowServer", "firefox"]


def test_process_states_are_counted_without_thread_counts():
    """BSD ``ps -o state=`` prints one column, so threads stay unreported
    rather than being set equal to the process count."""
    snapshot = parse_host_info(_freebsd_probe())
    counts = snapshot.process_counts
    assert counts.total == 5
    assert counts.running == 1
    assert counts.threads is None
    # kern.pid_max stands in for /proc/sys/kernel/pid_max.
    assert counts.pid_max == 99999


def test_sessions_come_from_who():
    snapshot = parse_host_info(_freebsd_probe())
    assert [s.user for s in snapshot.sessions] == ["root"]


# ---------------------------------------------------------------------------
# Live sampling
# ---------------------------------------------------------------------------

def test_live_sample_reads_the_bsd_sections():
    raw = _probe(
        OSTYPE="FreeBSD",
        NETSTAT_IB=FREEBSD_NETSTAT_IB,
        SYSCTL=FREEBSD_SYSCTL,
        SWAPINFO="Device          1K-blocks     Used    Avail Capacity\n"
                 "/dev/gpt/swapfs   1048576        0  1048576     0%",
    )
    sample = parse_live_probe(raw)
    assert [item.name for item in sample.counters] == ["vtnet0", "lo0"]
    assert [item.name for item in sample.cpu_times] == ["cpu", "cpu0", "cpu1"]
    assert sample.load_average.one == 0.32
    assert sample.memory.total_bytes == 2111283200


def test_live_sample_reports_no_memory_rather_than_an_empty_one():
    sample = parse_live_probe(_probe(OSTYPE="Darwin", SYSCTL="", VM_STAT=""))
    assert sample.memory is None


def test_counters_probe_follows_the_same_branch():
    raw = _probe(OSTYPE="FreeBSD", NETSTAT_IB=FREEBSD_NETSTAT_IB)
    assert [item.name for item in parse_counters_probe(raw)] == ["vtnet0", "lo0"]


# ---------------------------------------------------------------------------
# The Linux path must not notice any of this
# ---------------------------------------------------------------------------

def test_a_linux_host_ignores_the_bsd_sections():
    """``uname -s`` decides, so a section that arrived empty on a Linux host
    is still read the Linux way rather than falling through to a BSD reader."""
    raw = _probe(
        OSTYPE="Linux",
        MEMINFO="MemTotal:       1024 kB\nMemFree:         512 kB",
        LOADAVG="0.50 0.40 0.30 1/200 1234",
        UPTIME="1234.50 2000.00",
        # Were these consulted, they would win; on Linux they are noise.
        SYSCTL=FREEBSD_SYSCTL,
        IFCONFIG=FREEBSD_IFCONFIG,
        NETSTAT_IB=FREEBSD_NETSTAT_IB,
        NETSTAT_AN=FREEBSD_NETSTAT_AN,
    )
    snapshot = parse_host_info(raw)
    assert snapshot.memory.total_bytes == 1024 * 1024
    assert snapshot.load_average.one == 0.50
    assert snapshot.uptime_seconds == 1234.50
    assert snapshot.interfaces == ()
    assert snapshot.sockets == ()
    assert snapshot.cpu_times == ()


def test_an_unknown_ostype_is_read_as_linux():
    """A host too old or too odd to answer ``uname -s`` keeps the behaviour it
    had before this branch existed."""
    raw = _probe(OSTYPE="", MEMINFO="MemTotal:       2048 kB")
    assert parse_host_info(raw).memory.total_bytes == 2048 * 1024
