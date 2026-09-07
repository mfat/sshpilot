"""Host-info probe parsing across coreutils and BusyBox/OpenWrt hosts."""

from __future__ import annotations

from sshpilot.api.models.host_info import (
    NetworkInterfaceKind,
    NetworkInterfaceState,
    SocketDirection,
)
from sshpilot.core.host_info import parse_counters_probe, parse_host_info
from sshpilot.core.host_info import parse_live_probe
from sshpilot.core.host_info.parser import (
    parse_architecture,
    parse_failed_units,
    parse_filesystems,
    parse_host_keys,
    parse_inode_usage,
    parse_io_pressure,
    parse_listening_ports,
    parse_meminfo,
    parse_mount_options,
    parse_network_counters,
    parse_pressure,
    parse_proc_stat,
    parse_proc_stat_counters,
    parse_process_states,
    parse_process_table,
    parse_sockets,
    parse_w,
    parse_who,
    sessions_from_sockets,
    split_sections,
)


def _probe(**sections: str) -> str:
    return "".join(f"==={name}===\n{body}\n" for name, body in sections.items())


# ---------------------------------------------------------------------------
# Established sockets: column layout differs between ss and netstat
# ---------------------------------------------------------------------------

SS_ESTABLISHED = (
    "Netid Recv-Q Send-Q   Local Address:Port    Peer Address:Port Process\n"
    'tcp   0      0            10.0.0.5:22           10.0.0.9:51234 users:(("sshd",pid=99,fd=4))\n'
    'tcp   0      0            10.0.0.5:40118      162.247.243.30:443 users:(("chrome",pid=71,fd=9))\n'
)

NETSTAT_ESTABLISHED = (
    "Active Internet connections (servers and established)\n"
    "Proto Recv-Q Send-Q Local Address    Foreign Address   State       PID/Program name\n"
    "tcp        0      0 10.0.0.5:22      10.0.0.9:51234    ESTABLISHED 99/sshd\n"
    "tcp        0      0 10.0.0.5:40118   162.247.243.30:443 ESTABLISHED 71/chrome\n"
    "tcp        0      0 0.0.0.0:22       0.0.0.0:*         LISTEN      99/sshd\n"
)


def test_ss_established_local_endpoint_is_column_three():
    """A state filter removes ss's State column; local is column 3, not 4."""

    sockets = parse_sockets(SS_ESTABLISHED, listening_ports=(22,))
    assert [(s.local_port, s.peer_address, s.peer_port) for s in sockets] == [
        (22, "10.0.0.9", 51234),
        (40118, "162.247.243.30", 443),
    ]
    assert sockets[0].direction is SocketDirection.INCOMING
    assert sockets[1].direction is SocketDirection.OUTGOING
    assert sockets[0].process == "sshd"


def test_netstat_established_matches_ss_and_drops_listening_rows():
    sockets = parse_sockets(NETSTAT_ESTABLISHED, listening_ports=(22,))
    assert [(s.local_port, s.peer_address) for s in sockets] == [
        (22, "10.0.0.9"),
        (40118, "162.247.243.30"),
    ]
    assert all(s.peer_address != "0.0.0.0" for s in sockets)


def test_direction_follows_listening_ports_not_the_privileged_range():
    """A service on a high port is still inbound; 443 outbound is not inbound."""

    sockets = parse_sockets(
        "Netid Recv-Q Send-Q Local Peer Process\n"
        "tcp   0      0      10.0.0.5:8443 10.0.0.9:52000\n"
        "tcp   0      0      10.0.0.5:53000 93.184.216.34:443\n",
        listening_ports=(8443,),
    )
    assert sockets[0].direction is SocketDirection.INCOMING
    assert sockets[1].direction is SocketDirection.OUTGOING


def test_listening_ports_are_read_from_ss_and_netstat():
    ss_listen = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      128          0.0.0.0:2222       0.0.0.0:*     users:(("sshd",pid=99,fd=3))\n'
    )
    assert parse_listening_ports(ss_listen) == {2222: "sshd"}
    netstat_listen = (
        "Proto Recv-Q Send-Q Local Address Foreign Address State  PID/Program name\n"
        "tcp        0      0 0.0.0.0:2222  0.0.0.0:*       LISTEN 99/sshd\n"
    )
    assert parse_listening_ports(netstat_listen) == {2222: "sshd"}


# ---------------------------------------------------------------------------
# Login sessions: who -> w -> inbound SSH sockets
# ---------------------------------------------------------------------------

def test_who_separates_remote_origin_from_local_console():
    sessions = parse_who(
        "mahdi    tty2         2026-09-01 18:00 (:0)\n"
        "root     pts/0        2026-09-02 09:15 (10.0.0.9)\n"
    )
    assert [(s.user, s.origin, s.remote) for s in sessions] == [
        ("mahdi", "", False),
        ("root", "10.0.0.9", True),
    ]


def test_w_fallback_reads_the_login_column():
    sessions = parse_w(
        "mahdi    tty2     -                Tue18    4days  0.11s  0.11s startplasma\n"
        "root     pts/0    10.0.0.9         09:15    1.00s  0.05s  0.05s -bash\n"
    )
    assert [(s.user, s.origin, s.since, s.remote) for s in sessions] == [
        ("mahdi", "", "Tue18", False),
        ("root", "10.0.0.9", "09:15", True),
    ]


def test_busybox_host_recovers_sessions_from_inbound_ssh_sockets():
    """OpenWrt ships neither who nor w; inbound sockets are the only evidence."""

    sockets = parse_sockets(SS_ESTABLISHED, listening_ports=(22,))
    sessions = sessions_from_sockets(sockets, ssh_ports=(22,))
    assert len(sessions) == 1
    assert sessions[0].origin == "10.0.0.9"
    assert sessions[0].remote is True


def test_socket_sessions_ignore_outgoing_ssh_connections():
    outgoing_only = (
        "Netid Recv-Q Send-Q Local Peer Process\n"
        "tcp   0      0      10.0.0.5:51234 10.0.0.9:22\n"
    )
    sockets = parse_sockets(outgoing_only, listening_ports=(22,))
    assert sessions_from_sockets(sockets, ssh_ports=(22,)) == ()


# ---------------------------------------------------------------------------
# Memory, storage, counters
# ---------------------------------------------------------------------------

def test_absent_mem_available_stays_unknown():
    memory = parse_meminfo("MemTotal:       1024 kB\nMemFree:         256 kB\n")
    assert memory.available_bytes is None
    assert memory.used_bytes is None


def test_mem_available_yields_used_bytes():
    memory = parse_meminfo(
        "MemTotal:       1024 kB\nMemFree: 256 kB\nMemAvailable:    512 kB\n"
    )
    assert memory.used_bytes == 512 * 1024


def test_coreutils_df_reports_bytes_and_busybox_df_reports_blocks():
    coreutils = parse_filesystems(
        "Filesystem     Type  1B-blocks       Used  Available Use% Mounted on\n"
        "/dev/sda1      ext4 1000000000  500000000  500000000  50% /\n"
        "tmpfs          tmpfs  10000000          0   10000000   0% /dev/shm\n"
    )
    assert len(coreutils) == 1
    assert coreutils[0].size_bytes == 1_000_000_000
    assert coreutils[0].fstype == "ext4"

    busybox = parse_filesystems(
        "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
        "/dev/mtdblock6            2048       512      1536  25% /overlay\n"
    )
    assert busybox[0].size_bytes == 2048 * 1024
    assert busybox[0].used_bytes == 512 * 1024
    assert busybox[0].mount_point == "/overlay"


def test_openwrt_overlay_is_preferred_as_the_root_filesystem():
    snapshot = parse_host_info(
        _probe(
            DF=(
                "Filesystem           1K-blocks      Used Available Use% Mounted on\n"
                "overlayfs:/overlay        2048       512      1536  25% /\n"
                "/dev/mtdblock6            2048       512      1536  25% /overlay\n"
            )
        )
    )
    assert snapshot.root_filesystem is not None
    assert snapshot.root_filesystem.device == "/dev/mtdblock6"


def test_net_dev_bytes_are_the_first_and_ninth_columns():
    counters = parse_network_counters(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
        "  eth0: 1000       10    0    0    0     0          0         0   2000       20\n"
    )
    assert counters == (counters[0],)
    assert (counters[0].name, counters[0].rx_bytes, counters[0].tx_bytes) == ("eth0", 1000, 2000)


def test_counters_probe_parses_its_own_section():
    counters = parse_counters_probe(
        _probe(NET_DEV="  eth0: 5 1 0 0 0 0 0 0 7 1\n", END="")
    )
    assert (counters[0].rx_bytes, counters[0].tx_bytes) == (5, 7)


# ---------------------------------------------------------------------------
# CPU / interfaces / assembly
# ---------------------------------------------------------------------------

def test_zero_processor_count_is_unknown_not_zero():
    """``grep -c`` prints 0 when it matches nothing; that must not divide."""

    snapshot = parse_host_info(_probe(NPROC="0", LSCPU="", CPUINFO=""))
    assert snapshot.cpu.logical_processors is None
    assert snapshot.cpu.total_threads is None


def test_cpuinfo_supplies_topology_when_lscpu_is_absent():
    snapshot = parse_host_info(
        _probe(
            NPROC="",
            LSCPU="",
            CPUINFO=(
                "system type\t: Atheros AR9344\n"
                "processor\t: 0\n"
                "BogoMIPS\t: 361.05\n"
            ),
        )
    )
    assert snapshot.cpu.model == "Atheros AR9344"
    assert snapshot.cpu.logical_processors == 1
    assert snapshot.cpu.bogomips == 361.05


def test_interface_kind_and_state_are_classified():
    snapshot = parse_host_info(
        _probe(
            IP_LINK=(
                "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 state UNKNOWN "
                "link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00\n"
                "2: eth0: <BROADCAST,MULTICAST> mtu 1500 state DOWN "
                "link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff\n"
                "3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP "
                "link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
            ),
            IP_ADDR="3: wlan0    inet 192.168.1.5/24 brd 192.168.1.255 scope global wlan0\n",
            WIRELESS="/sys/class/net/wlan0/wireless\n",
        )
    )
    kinds = {i.name: (i.kind, i.state) for i in snapshot.interfaces}
    assert kinds["lo"][0] is NetworkInterfaceKind.LOOPBACK
    assert kinds["eth0"] == (NetworkInterfaceKind.ETHERNET, NetworkInterfaceState.DOWN)
    assert kinds["wlan0"] == (NetworkInterfaceKind.WIRELESS, NetworkInterfaceState.UP)
    wlan = next(i for i in snapshot.interfaces if i.name == "wlan0")
    assert wlan.ipv4_addresses == ("192.168.1.5/24",)
    assert wlan.mac_address == "11:22:33:44:55:66"


def test_ssh_port_is_discovered_rather_than_assumed():
    snapshot = parse_host_info(
        _probe(
            SS_LISTEN=(
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                'LISTEN 0      128          0.0.0.0:2222       0.0.0.0:*   users:(("sshd",pid=1,fd=3))\n'
            )
        )
    )
    assert snapshot.ssh_port == 2222
    assert snapshot.ssh_process == "sshd"


def test_empty_probe_yields_an_empty_snapshot_without_raising():
    snapshot = parse_host_info("")
    assert snapshot.hostname == ""
    assert snapshot.load_average is None
    assert snapshot.memory.available_bytes is None
    assert snapshot.filesystems == ()
    assert snapshot.root_filesystem is None


def test_sections_are_split_on_exact_markers():
    assert split_sections("===A===\nx\n===B===\ny\n") == {"A": "x", "B": "y"}


# ---------------------------------------------------------------------------
# Nothing is assumed about the SSH port or about interface names
# ---------------------------------------------------------------------------

def test_the_port_the_probe_arrived_on_wins():
    """sshd states the endpoint; a listening scan can only corroborate it."""

    snapshot = parse_host_info(
        _probe(
            SSH_CONNECTION="10.0.0.9 51234 10.0.0.5 2222\n",
            SS_LISTEN=(
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                'LISTEN 0      128          0.0.0.0:22         0.0.0.0:*   users:(("sshd",pid=1,fd=3))\n'
            ),
        )
    )
    assert snapshot.ssh_port == 2222


def test_an_unknown_ssh_port_stays_unknown_instead_of_defaulting_to_22():
    snapshot = parse_host_info(
        _probe(
            SS_LISTEN=(
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                "LISTEN 0      128          0.0.0.0:22         0.0.0.0:*\n"
            )
        )
    )
    assert snapshot.ssh_port is None
    assert snapshot.ssh_process == ""


def test_ssh_connection_without_four_fields_is_ignored():
    assert parse_host_info(_probe(SSH_CONNECTION="\n")).ssh_port is None
    assert parse_host_info(_probe(SSH_CONNECTION="10.0.0.9 51234\n")).ssh_port is None


def test_sessions_are_recovered_on_a_non_standard_ssh_port():
    snapshot = parse_host_info(
        _probe(
            SSH_CONNECTION="10.0.0.9 51234 10.0.0.5 2222\n",
            SS_LISTEN=(
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                'LISTEN 0      128          0.0.0.0:2222       0.0.0.0:*   users:(("sshd",pid=1,fd=3))\n'
            ),
            SS_ESTAB=(
                "Netid Recv-Q Send-Q   Local Address:Port    Peer Address:Port Process\n"
                "tcp   0      0            10.0.0.5:2222         10.0.0.9:51234\n"
            ),
        )
    )
    assert [session.origin for session in snapshot.sessions] == ["10.0.0.9"]


def test_wireless_comes_from_sysfs_not_from_the_interface_name():
    """A radio can be called anything, and anything can be called ``wl…``."""

    snapshot = parse_host_info(
        _probe(
            IP_LINK=(
                "2: wl-bridge: <BROADCAST,MULTICAST,UP> mtu 1500 state UP "
                "link/ether aa:bb:cc:dd:ee:ff\n"
                "3: eth1: <BROADCAST,MULTICAST,UP> mtu 1500 state UP "
                "link/ether 11:22:33:44:55:66\n"
            ),
            WIRELESS="/sys/class/net/eth1/phy80211\n",
        )
    )
    kinds = {item.name: item.kind for item in snapshot.interfaces}
    assert kinds["eth1"] is NetworkInterfaceKind.WIRELESS
    assert kinds["wl-bridge"] is NetworkInterfaceKind.ETHERNET


def test_the_device_model_survives_a_devicetree_nul():
    assert parse_host_info(_probe(DEVICE_MODEL="Raspberry Pi 4 Model B\n")).device_model == (
        "Raspberry Pi 4 Model B"
    )


# ---------------------------------------------------------------------------
# API 0.53 sections
# ---------------------------------------------------------------------------

PS_PROCESSES = (
    "%CPU %MEM COMMAND\n"
    " 457  5.1 codebase-memory\n"
    "50.8  3.1 chrome\n"
)

BUSYBOX_TOP = (
    "Mem: 123456K used, 78901K free, 0K shrd, 1234K buff, 56789K cached\n"
    "CPU:  0.4% usr  0.4% sys  0.0% nic 99.0% idle\n"
    "Load average: 0.00 0.01 0.05 1/49 3456\n"
    "  PID  PPID USER     STAT   VSZ %VSZ %CPU COMMAND\n"
    " 1234     1 root     S     2345   2.3  1.2 /usr/sbin/hostapd -P /var/run/wifi.pid\n"
)


def test_a_process_listing_is_read_through_its_own_header():
    """ps, BusyBox top and procps top order their columns differently."""

    assert [
        (item.command, item.cpu_percent, item.memory_percent)
        for item in parse_process_table(PS_PROCESSES)
    ] == [("codebase-memory", 457.0, 5.1), ("chrome", 50.8, 3.1)]
    busybox = parse_process_table(BUSYBOX_TOP)
    assert busybox[0].command == "/usr/sbin/hostapd -P /var/run/wifi.pid"
    assert busybox[0].cpu_percent == 1.2


def test_busybox_vsz_is_not_reported_as_memory():
    """%VSZ is a share of virtual size; relabelling it as memory would lie."""

    assert parse_process_table(BUSYBOX_TOP)[0].memory_percent is None


def test_a_host_without_ps_options_falls_back_to_top():
    snapshot = parse_host_info(_probe(PROCESSES="", TOP=BUSYBOX_TOP))
    assert [item.command for item in snapshot.processes] == [
        "/usr/sbin/hostapd -P /var/run/wifi.pid"
    ]


def test_ps_wins_over_top_when_both_answered():
    snapshot = parse_host_info(_probe(PROCESSES=PS_PROCESSES, TOP=BUSYBOX_TOP))
    assert snapshot.processes[0].command == "codebase-memory"


def test_a_failed_unit_is_read_without_matching_localized_state_words():
    """systemctl prints its state columns in the host's own language."""

    units = parse_failed_units(
        "logrotate.service loaded failed failed Rotate log files\n"
        "dnsmasq.service   geladen fehlgeschlagen fehlgeschlagen DNS-Weiterleitung\n"
    )
    assert [(unit.name, unit.description) for unit in units] == [
        ("logrotate.service", "Rotate log files"),
        ("dnsmasq.service", "DNS-Weiterleitung"),
    ]


def test_a_host_without_systemd_reports_no_failed_units():
    assert parse_failed_units("") == ()


def test_host_key_fingerprints_carry_their_algorithm_and_size():
    keys = parse_host_keys(
        "256 SHA256:abc root@router (ED25519)\n"
        "3072 SHA256:def root@router (RSA)\n"
        "ssh-keygen: /etc/ssh/ssh_host_dsa_key.pub: No such file\n"
    )
    assert [(key.algorithm, key.bits, key.fingerprint) for key in keys] == [
        ("ED25519", 256, "SHA256:abc"),
        ("RSA", 3072, "SHA256:def"),
    ]


def test_io_pressure_reads_both_lines_and_stays_absent_without_psi():
    some, full = parse_io_pressure(
        "some avg10=1.10 avg60=0.73 avg300=0.38 total=1673788839\n"
        "full avg10=0.33 avg60=0.46 avg300=0.29 total=1243020175\n"
    )
    assert (some.avg10, some.avg60, some.avg300) == (1.10, 0.73, 0.38)
    assert full.avg10 == 0.33
    assert parse_io_pressure("") == (None, None)


def test_a_truncated_pressure_line_is_not_half_reported():
    assert parse_io_pressure("some avg10=1.10 avg60=0.73 total=5\n") == (None, None)


def test_the_architecture_is_the_machine_field_of_uname():
    assert parse_architecture("Linux 5.15.134 mips") == "mips"
    assert parse_architecture("Linux 5.15.134") == ""


def test_every_listening_port_is_reported_not_only_sshd():
    """The listening table was parsed for direction and then thrown away."""

    snapshot = parse_host_info(
        _probe(
            SS_LISTEN=(
                "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
                'LISTEN 0      128          0.0.0.0:22        0.0.0.0:* users:(("sshd",pid=1,fd=3))\n'
                'LISTEN 0      128          0.0.0.0:8080      0.0.0.0:* users:(("uhttpd",pid=2,fd=4))\n'
            )
        )
    )
    assert [(item.port, item.process) for item in snapshot.listening_ports] == [
        (22, "sshd"),
        (8080, "uhttpd"),
    ]


def test_the_distro_identifier_and_version_survive_the_pretty_name():
    snapshot = parse_host_info(
        _probe(
            OS_RELEASE='PRETTY_NAME="OpenWrt 23.05.5"\nID="openwrt"\nVERSION_ID="23.05.5"\n',
            UNAME="Linux 5.15.134 mips",
        )
    )
    assert (snapshot.os_id, snapshot.os_version_id) == ("openwrt", "23.05.5")
    assert snapshot.architecture == "mips"
    assert snapshot.os_pretty_name == "OpenWrt 23.05.5"


# ---------------------------------------------------------------------------
# /proc/stat, pressure, inodes, mounts and the process table summary
# ---------------------------------------------------------------------------

PROC_STAT = """cpu  1000 20 300 90000 40 5 6 7 8 9
cpu0 500 10 150 45000 20 2 3 3 4 4
cpu1 500 10 150 45000 20 3 3 4 4 5
intr 123456789 1 0 0 2
ctxt 987654321
btime 1749000000
processes 55555
procs_running 3
procs_blocked 1
softirq 55555 1 2 3
"""


def test_proc_stat_reports_the_aggregate_first_then_every_core():
    readings = parse_proc_stat(PROC_STAT)
    assert [item.name for item in readings] == ["cpu", "cpu0", "cpu1"]
    assert readings[0].user == 1000
    assert readings[0].idle == 90000
    assert readings[0].steal == 7
    assert readings[0].guest_nice == 9


def test_proc_stat_ignores_the_scalar_lines_when_reading_cpu_times():
    """``ctxt`` and ``btime`` are not CPUs; only ``cpu``/``cpuN`` lines are."""

    assert all(
        item.name.startswith("cpu") for item in parse_proc_stat(PROC_STAT)
    )
    assert len(parse_proc_stat(PROC_STAT)) == 3


def test_an_older_kernel_stopping_before_steal_leaves_it_unreported():
    """A missing column is unknown, not zero: counting it as zero would drag
    every computed share downwards."""

    readings = parse_proc_stat("cpu  100 2 30 400\n")
    assert readings[0].idle == 400
    assert readings[0].iowait is None
    assert readings[0].steal is None
    # The total counts only what the host actually published.
    assert readings[0].total == 532


def test_proc_stat_counters_read_only_the_grand_total_of_intr():
    counters = parse_proc_stat_counters(PROC_STAT)
    assert counters["context_switches"] == 987654321
    assert counters["interrupts"] == 123456789
    assert counters["soft_interrupts"] == 55555
    assert counters["procs_running"] == 3
    assert counters["procs_blocked"] == 1


def test_a_host_without_proc_stat_reports_no_counters_rather_than_zeroes():
    assert parse_proc_stat("") == ()
    assert set(parse_proc_stat_counters("").values()) == {None}


def test_cpu_pressure_has_no_full_line_and_that_is_not_a_failure():
    """Every task cannot be waiting for a CPU while one of them is using it."""

    some, full = parse_pressure("some avg10=2.20 avg60=1.50 avg300=0.90 total=1234")
    assert (some.avg10, some.avg60, some.avg300) == (2.20, 1.50, 0.90)
    assert full is None


def test_io_pressure_keeps_its_original_name_for_the_same_parser():
    assert parse_io_pressure is parse_pressure


DF_INODES_COREUTILS = """Filesystem     Type    Inodes   IUsed    IFree IUse% Mounted on
/dev/sda1      ext4   6553600  123456  6430144    2% /
/dev/sdb1      btrfs        -       -        -     - /data
"""

DF_INODES_BUSYBOX = """Filesystem           Inodes      Used  Available Use% Mounted on
/dev/root             65536      1234      64302   2% /
"""


def test_df_inodes_is_read_the_same_way_whether_or_not_a_type_column_is_there():
    assert parse_inode_usage(DF_INODES_COREUTILS)["/"] == (6553600, 123456, 6430144)
    assert parse_inode_usage(DF_INODES_BUSYBOX)["/"] == (65536, 1234, 64302)


def test_a_filesystem_with_no_inode_table_reports_unknown_not_zero():
    """btrfs and zfs allocate inodes dynamically and print dashes here. Zero
    would render as "no inodes left"."""

    assert parse_inode_usage(DF_INODES_COREUTILS)["/data"] == (None, None, None)


def test_mount_options_unescape_the_octal_the_kernel_writes():
    options = parse_mount_options(
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "/dev/sdb1 /media/my\\040disk vfat ro,noatime 0 0\n"
    )
    assert options["/"] == "rw,relatime"
    assert options["/media/my disk"] == "ro,noatime"


def test_a_later_mount_over_the_same_point_shadows_the_earlier_one():
    options = parse_mount_options(
        "/dev/sda1 /mnt ext4 rw 0 0\n/dev/sdb1 /mnt ext4 ro 0 0\n"
    )
    assert options["/mnt"] == "ro"


def test_filesystems_join_inodes_and_options_on_the_mount_point():
    filesystems = parse_filesystems(
        "Filesystem     Type  1B-blocks       Used  Available Use% Mounted on\n"
        "/dev/sda1      ext4  100000000   50000000   45000000  53% /\n",
        parse_inode_usage(DF_INODES_COREUTILS),
        parse_mount_options("/dev/sda1 / ext4 ro,relatime 0 0\n"),
    )
    assert filesystems[0].options == "ro,relatime"
    assert filesystems[0].read_only is True
    assert filesystems[0].inodes_used == 123456
    assert round(filesystems[0].inodes_used_fraction, 4) == 0.0188


def test_a_filesystem_probe_without_the_extra_sections_still_parses():
    """Both joins are optional; a host that answered only ``df`` is not an
    error."""

    filesystems = parse_filesystems(
        "Filesystem     Type  1B-blocks       Used  Available Use% Mounted on\n"
        "/dev/sda1      ext4  100000000   50000000   45000000  53% /\n"
    )
    assert filesystems[0].options == ""
    assert filesystems[0].inodes_total is None
    assert filesystems[0].inodes_used_fraction is None


def test_process_states_count_by_the_first_letter_and_sum_the_threads():
    """The flags after the state letter (``s`` leader, ``+`` foreground) say
    what a process *is*, not what it is doing."""

    buckets = parse_process_states("Ss    1\nS+    4\nR     2\nZ     1\nTl    3\nD     1\n")
    assert buckets["total"] == 6
    assert buckets["running"] == 1
    assert buckets["stopped"] == 1
    assert buckets["zombie"] == 1
    # D is a sleep the host is blocked in, not a stopped or running process.
    assert buckets["sleeping"] == 3
    assert buckets["threads"] == 12


def test_a_ps_without_nlwp_leaves_threads_unreported_not_equal_to_processes():
    buckets = parse_process_states("S\nR\nS\n")
    assert buckets["total"] == 3
    assert buckets["threads"] is None


def test_meminfo_extras_stay_absent_when_the_host_omits_them():
    memory = parse_meminfo("MemTotal:  1024 kB\nMemFree:  512 kB\n")
    assert memory.total_bytes == 1024 * 1024
    for name in ("active_bytes", "inactive_bytes", "dirty_bytes", "slab_bytes"):
        assert getattr(memory, name) is None


def test_meminfo_reads_the_breakdown_when_the_host_publishes_it():
    memory = parse_meminfo(
        "MemTotal: 1024 kB\nActive: 400 kB\nInactive: 200 kB\nShmem: 8 kB\n"
        "Dirty: 4 kB\nWriteback: 2 kB\nSlab: 64 kB\nSReclaimable: 32 kB\n"
    )
    assert memory.active_bytes == 400 * 1024
    assert memory.inactive_bytes == 200 * 1024
    assert memory.dirty_bytes == 4 * 1024
    assert memory.slab_reclaimable_bytes == 32 * 1024


def test_a_full_gather_carries_the_new_sections_through_to_the_snapshot():
    snapshot = parse_host_info(
        _probe(
            STAT=PROC_STAT,
            CPU_PRESSURE="some avg10=2.20 avg60=1.50 avg300=0.90 total=1",
            MEM_PRESSURE=(
                "some avg10=0.10 avg60=0.20 avg300=0.30 total=1\n"
                "full avg10=0.05 avg60=0.06 avg300=0.07 total=1"
            ),
            PROC_STATES="S 4\nR 2\n",
            PID_MAX="32768",
        )
    )
    assert [item.name for item in snapshot.cpu_times] == ["cpu", "cpu0", "cpu1"]
    assert snapshot.context_switches == 987654321
    assert snapshot.interrupts == 123456789
    assert snapshot.cpu_pressure_some.avg10 == 2.20
    assert snapshot.cpu_pressure_full is None
    assert snapshot.memory_pressure_full.avg10 == 0.05
    assert snapshot.process_counts.total == 2
    assert snapshot.process_counts.threads == 6
    assert snapshot.process_counts.pid_max == 32768


def test_a_busybox_host_still_reports_running_from_proc_stat():
    """BusyBox ``ps`` has no ``-o``, so PROC_STATES is empty. procs_running is
    the one thing every Linux still publishes."""

    snapshot = parse_host_info(_probe(STAT=PROC_STAT, PROC_STATES="", PID_MAX=""))
    assert snapshot.process_counts.running == 3
    assert snapshot.process_counts.total is None
    assert snapshot.process_counts.pid_max is None


def test_a_host_that_said_nothing_about_processes_reports_no_counts():
    assert parse_host_info(_probe(HOSTNAME="box")).process_counts is None


def test_the_live_probe_reuses_the_full_gather_markers_and_parsers():
    """A reading must not mean one thing on open and another two seconds later."""

    sample = parse_live_probe(
        _probe(
            NET_DEV=(
                "Inter-|   Receive                                | Transmit\n"
                " face |bytes packets errs drop fifo frame compressed multicast|"
                "bytes packets\n"
                "  eth0: 1000 1 0 0 0 0 0 0 2000 2"
            ),
            STAT=PROC_STAT,
            MEMINFO="MemTotal: 1024 kB\nMemAvailable: 256 kB\n",
            LOADAVG="0.50 0.40 0.30 1/200 12345",
        )
    )
    assert sample.counters == (parse_network_counters(
        "  eth0: 1000 1 0 0 0 0 0 0 2000 2"
    )[0],)
    assert [item.name for item in sample.cpu_times] == ["cpu", "cpu0", "cpu1"]
    assert sample.memory.used_bytes == (1024 - 256) * 1024
    assert sample.load_average.one == 0.50


def test_a_live_probe_that_answered_nothing_reports_absent_not_empty():
    sample = parse_live_probe(_probe(NET_DEV="", STAT="", LOADAVG=""))
    assert sample.counters == ()
    assert sample.cpu_times == ()
    assert sample.memory is None
    assert sample.load_average is None
