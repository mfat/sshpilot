"""GUI coverage for the Host Info dialog rendering daemon-supplied DTOs.

The dialog is presentation only: it must build every tab from a
``HostInfoSnapshot`` without reaching for a shell, and must degrade to "N/A"
rather than to zero for readings the host did not publish.
"""

import pytest

from gi.repository import Gtk

from sshpilot.api.models.host_info import (
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
from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def _texts(widget):
    return [w.get_text() for w in _walk(widget) if isinstance(w, Gtk.Label)]


def _snapshot(**overrides):
    base = dict(
        hostname="router",
        os_pretty_name="OpenWrt 23.05",
        kernel="Linux 5.15 mips",
        uptime_seconds=90061.0,
        boot_time="2026-09-01 18:00",
        cpu=CpuInfo(
            model="Atheros AR9344",
            cores_per_socket=2,
            threads_per_core=2,
            sockets=1,
            logical_processors=4,
            frequency_mhz=1200.0,
        ),
        memory=MemoryInfo(
            total_bytes=1024 * 1024 * 512,
            free_bytes=1024 * 1024 * 64,
            available_bytes=1024 * 1024 * 128,
            cached_bytes=1024 * 1024 * 32,
            swap_total_bytes=1024 * 1024 * 256,
            swap_free_bytes=1024 * 1024 * 200,
        ),
        load_average=LoadAverage(1.0, 0.5, 0.25),
        filesystems=(
            FilesystemUsage(
                device="/dev/mtdblock6",
                mount_point="/overlay",
                fstype="jffs2",
                size_bytes=2_000_000,
                used_bytes=1_900_000,
                available_bytes=100_000,
                use_percent=95,
            ),
        ),
        interfaces=(
            NetworkInterface(
                name="wlan0",
                kind=NetworkInterfaceKind.WIRELESS,
                state=NetworkInterfaceState.UP,
                mac_address="11:22:33:44:55:66",
                mtu=1500,
                ipv4_addresses=("192.168.1.1/24",),
            ),
        ),
        temperatures=(TemperatureReading("cpu-thermal", 85.0),),
        sessions=(
            LoginSession(user="root", tty="pts/0", origin="10.0.0.9", since="09:15", remote=True),
            LoginSession(user="local", tty="tty1", since="08:00"),
        ),
        sockets=(
            SocketConnection(
                protocol="tcp",
                local_address="10.0.0.5",
                local_port=2222,
                peer_address="10.0.0.9",
                peer_port=51234,
                process="sshd",
                direction=SocketDirection.INCOMING,
            ),
        ),
        default_gateway="192.168.1.254",
        default_gateway_interface="wlan0",
        dns_servers=("1.1.1.1",),
        ssh_port=2222,
        ssh_process="sshd",
    )
    base.update(overrides)
    return HostInfoSnapshot(**base)


def _dialog(snapshot, counters=()):
    """Build a dialog shell without a daemon and hand it a snapshot.

    The baseline live sample is seeded exactly as ``_on_snapshot`` seeds it, so
    the tabs see the same state they would after a real gather.
    """

    from sshpilot.machine_info_dialog import MachineInfoDialog

    dialog = object.__new__(MachineInfoDialog)
    dialog._snapshot = snapshot
    dialog._rate_labels = {}
    dialog._cpu_gauge = None
    dialog._memory_gauge = None
    dialog._cpu_section = None
    dialog._previous_live = LiveSample(
        counters=counters,
        cpu_times=snapshot.cpu_times,
        memory=snapshot.memory,
        load_average=snapshot.load_average,
    )
    dialog._previous_live_time = 0.0
    return dialog


def test_every_tab_builds_from_a_snapshot():
    dialog = _dialog(_snapshot(), counters=(InterfaceCounters("wlan0", 100, 200),))
    for build in (
        dialog._build_overview,
        dialog._build_resources,
        dialog._build_storage,
        dialog._build_network,
        dialog._build_traffic,
        dialog._build_system,
    ):
        assert isinstance(build(), Gtk.Box)


def test_the_discovered_ssh_port_is_shown_not_a_hardcoded_22():
    texts = _texts(_dialog(_snapshot())._build_network())
    assert any("2222/tcp" in text for text in texts)
    assert not any(text.startswith("22/tcp") for text in texts)


def test_an_unreported_memory_availability_reads_as_na_everywhere():
    snapshot = _snapshot(
        memory=MemoryInfo(total_bytes=1024 * 1024 * 512, free_bytes=1024 * 1024 * 64)
    )
    dialog = _dialog(snapshot)
    overview = _texts(dialog._build_overview())
    resources = _texts(dialog._build_resources())
    # Neither "0%" (nothing used) nor "100%" (everything used) may be invented.
    assert "—" in overview
    assert any("N/A" in text for text in overview)
    assert any("N/A" in text for text in resources)


def test_a_missing_cpu_count_does_not_break_the_load_gauges():
    """Load is only readable per CPU, so without a count the bars must render
    as unknown rather than picking a severity out of an unscaled number."""

    from sshpilot.machine_info_dialog import _SEVERITY_UNKNOWN

    snapshot = _snapshot(cpu=CpuInfo(model="Unknown"))
    dialog = _dialog(snapshot)
    assert isinstance(dialog._build_overview(), Gtk.Box)

    page = dialog._build_resources()
    assert isinstance(page, Gtk.Box)
    # The load figures are still reported; only their severity is withheld.
    assert "1.00" in _texts(page)
    load_bars = [
        widget
        for widget in _walk(page)
        if isinstance(widget, Gtk.LevelBar)
        and _SEVERITY_UNKNOWN in widget.get_css_classes()
    ]
    assert load_bars


def test_memory_and_temperatures_are_stacked_not_side_by_side():
    """Both sections need the full width; a half-width column wraps badly."""

    page = _dialog(_snapshot())._build_resources()
    headings = {}
    for widget in _walk(page):
        if isinstance(widget, Gtk.Label) and widget.get_text() in ("Memory", "Temperatures"):
            headings[widget.get_text()] = widget
    assert set(headings) == {"Memory", "Temperatures"}

    # Asserted on the ancestry itself rather than by intersecting id() sets:
    # PyGObject hands out a fresh wrapper per access and frees it immediately,
    # so those addresses get recycled and compare equal by accident.
    def _row_ancestor(widget):
        """The first horizontal box between a heading and the page, if any."""

        parent = widget.get_parent()
        while parent is not None and parent is not page:
            if (
                isinstance(parent, Gtk.Box)
                and parent.get_orientation() is Gtk.Orientation.HORIZONTAL
            ):
                return parent
            parent = parent.get_parent()
        return None

    for name, heading in headings.items():
        assert _row_ancestor(heading) is None, (
            f"{name} sits inside a horizontal container, so it shares a row"
        )


def test_a_nearly_full_filesystem_is_marked_critical_not_healthy():
    from sshpilot.machine_info_dialog import _SEVERITY_CRITICAL

    page = _dialog(_snapshot())._build_storage()
    bars = [w for w in _walk(page) if isinstance(w, Gtk.LevelBar)]
    assert bars
    assert _SEVERITY_CRITICAL in bars[0].get_css_classes()


def test_severity_runs_green_blue_amber_red_as_a_value_fills_up():
    from sshpilot.machine_info_dialog import (
        _SEVERITY_CAREFUL,
        _SEVERITY_CRITICAL,
        _SEVERITY_OK,
        _SEVERITY_UNKNOWN,
        _SEVERITY_WARN,
        _temperature_severity,
        _usage_severity,
    )

    assert _usage_severity(0.0) is _SEVERITY_OK
    assert _usage_severity(0.49) is _SEVERITY_OK
    assert _usage_severity(0.50) is _SEVERITY_CAREFUL
    assert _usage_severity(0.69) is _SEVERITY_CAREFUL
    assert _usage_severity(0.70) is _SEVERITY_WARN
    assert _usage_severity(0.89) is _SEVERITY_WARN
    assert _usage_severity(0.90) is _SEVERITY_CRITICAL
    assert _usage_severity(1.0) is _SEVERITY_CRITICAL
    assert _temperature_severity(59.9) is _SEVERITY_OK
    assert _temperature_severity(60.0) is _SEVERITY_CAREFUL
    assert _temperature_severity(70.0) is _SEVERITY_WARN
    assert _temperature_severity(80.0) is _SEVERITY_CRITICAL

    # An unknown reading is neither healthy nor alarming.
    assert _usage_severity(None) is _SEVERITY_UNKNOWN


def test_load_is_scaled_by_core_count_and_not_the_usage_scale():
    """Load is a run-queue length, so it cannot share utilization's thresholds.

    One runnable task per CPU is a busy host, not a dying one: on the usage
    scale that reads critical, which is what this separation prevents.
    """

    from sshpilot.machine_info_dialog import (
        _SEVERITY_CAREFUL,
        _SEVERITY_CRITICAL,
        _SEVERITY_OK,
        _SEVERITY_UNKNOWN,
        _SEVERITY_WARN,
        _load_severity,
        _usage_severity,
    )

    # Four runnable tasks on four CPUs: fully committed, but healthy.
    assert _load_severity(4.0, 4) is _SEVERITY_WARN
    assert _usage_severity(4.0 / 4) is _SEVERITY_CRITICAL

    assert _load_severity(1.0, 4) is _SEVERITY_OK
    assert _load_severity(2.8, 4) is _SEVERITY_CAREFUL
    assert _load_severity(20.0, 4) is _SEVERITY_CRITICAL
    # Load 8 is idle on a 64-core host and a crisis on a single-core router,
    # so without a CPU count it is unreadable rather than alarming.
    assert _load_severity(8.0, None) is _SEVERITY_UNKNOWN
    assert _load_severity(None, 4) is _SEVERITY_UNKNOWN


def test_an_unknown_reading_is_not_painted_as_healthy():
    """Green means "there is headroom", so it must not mean "no idea"."""

    from sshpilot.machine_info_dialog import _SEVERITY_OK, _SEVERITY_UNKNOWN
    from sshpilot.api.models.host_info import MemoryInfo

    snapshot = _snapshot(
        memory=MemoryInfo(total_bytes=1024 * 1024 * 512, free_bytes=1024)
    )
    page = _dialog(snapshot)._build_resources()
    memory_bars = [
        w for w in _walk(page)
        if isinstance(w, Gtk.LevelBar) and _SEVERITY_UNKNOWN in w.get_css_classes()
    ]
    assert memory_bars, "a memory bar with no MemAvailable must read as unknown"
    assert all(
        _SEVERITY_OK not in bar.get_css_classes() for bar in memory_bars
    )


def test_a_hot_sensor_is_marked_critical():
    from sshpilot.machine_info_dialog import _SEVERITY_CRITICAL

    page = _dialog(_snapshot())._build_resources()
    bars = [
        w for w in _walk(page)
        if isinstance(w, Gtk.LevelBar) and _SEVERITY_CRITICAL in w.get_css_classes()
    ]
    assert bars, "an 85 °C sensor must render as critical"


def test_remote_sessions_and_all_users_are_listed_separately():
    dialog = _dialog(_snapshot())
    remote = _texts(dialog._build_traffic())
    everyone = _texts(dialog._build_system())
    assert "root" in remote and "local" not in remote
    assert "root" in everyone and "local" in everyone


def test_an_empty_snapshot_still_renders():
    dialog = _dialog(HostInfoSnapshot())
    for build in (
        dialog._build_overview,
        dialog._build_resources,
        dialog._build_storage,
        dialog._build_network,
        dialog._build_traffic,
        dialog._build_system,
    ):
        assert isinstance(build(), Gtk.Box)


# ---------------------------------------------------------------------------
# Authentication prompts
# ---------------------------------------------------------------------------

class _FakePresenter:
    """Stands in for DaemonInteractionDialogs."""

    def __init__(self):
        self.sessions = []
        self.closed = False

    def set_session(self, session_id):
        self.sessions.append(str(session_id))

    def close(self):
        self.closed = True


class _FakeController:
    def __init__(self):
        self.calls = []

    def start(self, connection_id, probe, on_result, on_error, on_started=None):
        self.calls.append((probe, on_started))

    def is_running(self, probe):
        return False


class _Connection:
    nickname = "demo"
    username = "alice"
    host = "example.test"


def _wired_dialog():
    """A dialog shell with a fake controller and presenter, no daemon."""

    from sshpilot.machine_info_dialog import MachineInfoDialog

    dialog = object.__new__(MachineInfoDialog)
    dialog._closed = False
    dialog._connection = _Connection()
    dialog._controller = _FakeController()
    dialog._interaction_dialogs = _FakePresenter()
    dialog._window = None
    return dialog


def test_a_password_prompt_reaches_the_user_by_binding_the_operation_scope():
    """The presenter ignores every interaction until it is bound to a scope."""

    from sshpilot.api.models.host_info import HostInfoProbe

    dialog = _wired_dialog()
    assert dialog._submit(HostInfoProbe.FULL, lambda s: None, lambda e: None)

    probe, on_started = dialog._controller.calls[0]
    assert probe is HostInfoProbe.FULL
    assert on_started is not None, "a full gather must bind the presenter"

    dialog._bind_interactions("operation-7")
    assert dialog._interaction_dialogs.sessions == ["operation-7"]


def test_bandwidth_sampling_never_rebinds_the_presenter():
    """Counter probes are autofill-only and must not steal the scope."""

    from sshpilot.api.models.host_info import HostInfoProbe

    dialog = _wired_dialog()
    dialog._submit(HostInfoProbe.NETWORK_COUNTERS, lambda s: None, lambda e: None)

    probe, on_started = dialog._controller.calls[0]
    assert probe is HostInfoProbe.NETWORK_COUNTERS
    assert on_started is None


def test_binding_after_the_dialog_closed_is_ignored():
    dialog = _wired_dialog()
    dialog._closed = True

    dialog._bind_interactions("operation-7")

    assert dialog._interaction_dialogs.sessions == []


def test_every_reported_address_is_shown_not_just_the_first_ipv4():
    """An interface that answers only over IPv6 must not read as address-less."""

    snapshot = _snapshot(
        interfaces=(
            NetworkInterface(
                name="eth0",
                kind=NetworkInterfaceKind.ETHERNET,
                state=NetworkInterfaceState.UP,
                ipv4_addresses=("10.0.0.5/24", "10.0.0.6/24"),
                ipv6_addresses=("2001:db8::1/64",),
            ),
        )
    )
    texts = _texts(_dialog(snapshot)._build_network())
    for address in ("10.0.0.5/24", "10.0.0.6/24", "2001:db8::1/64"):
        assert address in texts


def test_interface_state_is_readable_without_hovering():
    """State used to live in an icon tooltip, which touch users cannot reach."""

    snapshot = _snapshot(
        interfaces=(
            NetworkInterface(
                name="eth0",
                kind=NetworkInterfaceKind.ETHERNET,
                state=NetworkInterfaceState.NO_CARRIER,
                mtu=1500,
            ),
        )
    )
    page = _dialog(snapshot)._build_network()
    assert any("No carrier" in text for text in _texts(page))
    assert not [
        widget for widget in _walk(page)
        if isinstance(widget, Gtk.Image) and widget.get_tooltip_text()
    ]


def test_the_filesystem_table_reports_what_the_host_says_is_left():
    """Reserved blocks mean size - used overstates what is actually free."""

    from sshpilot.machine_info_dialog import _format_bytes_si

    snapshot = _snapshot(
        filesystems=(
            FilesystemUsage(
                device="/dev/sda1",
                mount_point="/",
                fstype="ext4",
                size_bytes=2_000_000,
                used_bytes=1_900_000,
                available_bytes=50_000,
            ),
        )
    )
    texts = _texts(_dialog(snapshot)._build_storage())
    assert "Available" in texts
    assert _format_bytes_si(50_000) in texts
    assert _format_bytes_si(100_000) not in texts, "available must not be derived"


def test_buffers_is_reported_with_the_other_meminfo_fields():
    snapshot = _snapshot(
        memory=MemoryInfo(
            total_bytes=1024 * 1024 * 512,
            free_bytes=1024 * 1024 * 64,
            available_bytes=1024 * 1024 * 128,
            buffers_bytes=1024 * 1024 * 16,
        )
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "Buffers" in texts
    assert "16.0 MiB" in texts


def test_a_capped_socket_list_names_the_protocol_and_says_what_it_hid():
    sockets = tuple(
        SocketConnection(
            protocol="udp" if index % 2 else "tcp",
            local_address="10.0.0.5",
            local_port=2222 + index,
            peer_address="10.0.0.9",
            peer_port=51000 + index,
            process="sshd",
            direction=SocketDirection.INCOMING,
        )
        for index in range(11)
    )
    texts = _texts(_dialog(_snapshot(sockets=sockets))._build_traffic())
    assert "tcp" in texts and "udp" in texts
    assert "3 more" in texts


def test_the_system_tab_reports_what_the_host_exposes():
    snapshot = _snapshot(
        os_id="openwrt",
        os_version_id="23.05.5",
        architecture="mips",
        listening_ports=(
            ListeningPort(port=22, process="sshd", address="0.0.0.0"),
            ListeningPort(port=8080, process="uhttpd", address="0.0.0.0"),
        ),
        failed_units=(FailedUnit(name="logrotate.service", description="Rotate logs"),),
        host_keys=(
            HostKeyFingerprint(algorithm="ED25519", fingerprint="SHA256:abc", bits=256),
        ),
    )
    dialog = _dialog(snapshot)
    page = dialog._build_system()
    texts = _texts(page)
    assert "Running services" in texts
    assert "openwrt 23.05.5" in texts and "mips" in texts
    assert "8080/tcp" in texts
    assert any("uhttpd" in text for text in texts)
    web_ui_buttons = [
        widget
        for widget in _walk(page)
        if isinstance(widget, Gtk.Button)
        and widget.get_tooltip_text()
        and "system browser" in widget.get_tooltip_text()
    ]
    assert web_ui_buttons, "well-known HTTP port gets a browser action"
    assert "logrotate.service" in texts
    assert "SHA256:abc" in texts and "ED25519" in texts


def test_a_host_with_nothing_to_report_says_so_rather_than_showing_blanks():
    texts = _texts(_dialog(_snapshot())._build_system())
    for message in (
        "No listening services reported",
        "No failed units",
        "No host keys reported",
    ):
        assert message in texts


def test_a_multi_threaded_process_may_exceed_one_hundred_percent():
    """A share of one CPU, so 457% is a reading and not an overflow."""

    snapshot = _snapshot(
        processes=(ProcessUsage(command="ffmpeg", cpu_percent=457.0, memory_percent=5.1),)
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "457.0%" in texts


def test_a_process_without_a_memory_reading_shows_na_not_zero():
    snapshot = _snapshot(processes=(ProcessUsage(command="procd", cpu_percent=0.5),))
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "0.5%" in texts
    assert any("N/A" in text for text in texts)


def test_pressure_covers_all_three_resources_not_only_storage():
    """PSI is the clearest "is this host struggling" reading, so it is not
    filed under disks: CPU and memory stall too, and they live on Resources."""

    snapshot = _snapshot(
        io_pressure_some=PressureStall(1.1, 0.73, 0.38),
        io_pressure_full=PressureStall(0.33, 0.46, 0.29),
        cpu_pressure_some=PressureStall(2.2, 1.5, 0.9),
        memory_pressure_some=PressureStall(3.3, 2.5, 1.9),
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "Time spent waiting" in texts
    # Plain words, not kernel vocabulary: no "pressure", no "I/O", no "tasks".
    assert {"CPU", "Memory", "Disk"} <= set(texts)
    assert not any("Pressure" in t or "stall" in t.lower() for t in texts)
    assert "I/O" not in texts

    # One window, the 60 s one -- the second field of each reading.
    assert "1.5%" in texts and "0.7%" in texts and "2.5%" in texts
    # ...and only that one: 10 s and 300 s are neither drawn nor labelled.
    assert "10 s" not in texts and "300 s" not in texts
    assert "2.2%" not in texts and "0.9%" not in texts

    # It is no longer on Storage.
    assert "Time spent waiting" not in _texts(_dialog(snapshot)._build_storage())


def test_a_host_where_everything_stalls_says_so_in_words():
    """"full" is the pre-OOM signature and is zero on a healthy host, so it is
    not a row of its own -- it is a phrase that appears when it is real."""

    snapshot = _snapshot(
        memory_pressure_some=PressureStall(20.0, 18.0, 9.0),
        memory_pressure_full=PressureStall(9.0, 8.0, 4.0),
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert any("nothing could run" in t for t in texts)
    assert any("8.0%" in t for t in texts)


def test_a_resource_that_never_stalls_completely_stays_silent():
    """/proc/pressure/cpu reports no "full" line on many kernels, and where it
    does it is zero: either way there is nothing to say."""

    snapshot = _snapshot(
        cpu_pressure_some=PressureStall(2.2, 1.5, 0.9),
        cpu_pressure_full=PressureStall(0.0, 0.0, 0.0),
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "1.5%" in texts
    assert not any("nothing could run" in t for t in texts)


def test_waiting_has_its_own_scale_because_losing_half_your_time_is_critical():
    """Utilization's 50/70/90 would call a host losing 40% of its time to
    waiting "healthy"."""

    from sshpilot.machine_info_dialog import (
        _SEVERITY_CAREFUL,
        _SEVERITY_CRITICAL,
        _SEVERITY_OK,
        _SEVERITY_WARN,
        _usage_severity,
        _wait_severity,
    )

    assert _wait_severity(0.05) is _SEVERITY_OK
    assert _wait_severity(0.10) is _SEVERITY_CAREFUL
    assert _wait_severity(0.25) is _SEVERITY_WARN
    assert _wait_severity(0.50) is _SEVERITY_CRITICAL
    # The same reading on the utilization scale would still read as healthy.
    assert _wait_severity(0.40) is _SEVERITY_WARN
    assert _usage_severity(0.40) is _SEVERITY_OK


def test_a_kernel_without_psi_says_so_rather_than_showing_zeroes():
    texts = _texts(_dialog(_snapshot())._build_resources())
    assert "This host does not report waiting times" in texts
    assert "0.0%" not in texts


# ---------------------------------------------------------------------------
# CPU utilization: the gauge means CPU, not load
# ---------------------------------------------------------------------------

def _times(name, **fields):
    base = dict(user=0, nice=0, system=0, idle=0, iowait=0, irq=0, softirq=0, steal=0)
    base.update(fields)
    return CpuTimes(name=name, **base)


def _cpu_snapshot(**overrides):
    return _snapshot(
        cpu=CpuInfo(model="Atheros AR9344", logical_processors=2, frequency_mhz=650.0),
        load_average=LoadAverage(1.9, 1.5, 1.2),
        cpu_times=(
            _times("cpu", user=100, system=50, idle=1000),
            _times("cpu0", user=50, system=25, idle=500),
            _times("cpu1", user=50, system=25, idle=500),
        ),
        **overrides,
    )


def test_the_cpu_gauge_waits_for_a_second_sample_instead_of_showing_load():
    """The gauge used to plot load1/nproc under a "CPU" heading. Load is a
    run-queue length; with load 1.9 on 2 CPUs that read as 95% CPU while the
    host could have been almost entirely idle."""

    dialog = _dialog(_cpu_snapshot())
    dialog._build_overview()
    texts = _texts(dialog._cpu_gauge.widget)

    assert "CPU" in texts
    # 1.9 / 2 CPUs would have rendered as 95%.
    assert "95%" not in texts
    assert "—" in texts
    # The load average is still reported, as load.
    assert any("load 1.90" in text for text in texts)


def test_a_live_sample_fills_in_the_cpu_gauge_and_the_per_core_bars():
    from sshpilot.machine_info_dialog import _SEVERITY_CAREFUL

    snapshot = _cpu_snapshot()
    dialog = _dialog(snapshot)
    dialog._build_overview()
    dialog._build_resources()

    # A second reading 1000 jiffies later: 60% busy overall, cpu0 at 40% and
    # cpu1 at 80%.
    sample = LiveSample(
        counters=(),
        cpu_times=(
            _times("cpu", user=700, system=650, idle=1800),
            _times("cpu0", user=250, system=225, idle=1100),
            _times("cpu1", user=450, system=425, idle=700),
        ),
        memory=snapshot.memory,
        load_average=snapshot.load_average,
    )
    dialog._apply_rates(dialog._previous_live, sample, 2.0)

    texts = _texts(dialog._cpu_gauge.widget) + _texts(dialog._cpu_section.widget)
    assert "60%" in texts
    assert "40%" in texts and "80%" in texts
    assert any("iowait" in text for text in texts)
    # 60% is past careful (50%) but not yet warning (70%).
    assert _SEVERITY_CAREFUL in dialog._cpu_gauge._area.get_css_classes()


def test_per_core_bars_stay_in_kernel_order_so_a_hot_core_stays_put():
    """Sorting by load would make the busiest core jump between rows every two
    seconds, hiding the thing worth seeing: that it is always the same core."""

    dialog = _dialog(_cpu_snapshot())
    dialog._build_resources()
    assert list(dialog._cpu_section._core_bars) == ["cpu0", "cpu1"]


def test_a_host_that_reported_no_cpu_times_still_builds_the_section():
    dialog = _dialog(_snapshot(cpu_times=()))
    page = dialog._build_resources()
    assert isinstance(page, Gtk.Box)
    assert dialog._cpu_section._core_bars == {}
    assert "CPU utilization" in _texts(page)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def test_inode_usage_is_shown_because_a_disk_fills_up_two_ways():
    """A filesystem can be 3% full of bytes and out of inodes, at which point
    every write fails while the usage bar still looks healthy."""

    snapshot = _snapshot(
        filesystems=(
            FilesystemUsage(
                device="/dev/sda1",
                mount_point="/",
                fstype="ext4",
                size_bytes=100_000_000,
                used_bytes=3_000_000,
                available_bytes=97_000_000,
                options="rw,noatime",
                inodes_total=1000,
                inodes_used=970,
                inodes_free=30,
            ),
        )
    )
    texts = _texts(_dialog(snapshot)._build_storage())
    # 3% of the bytes, 97% of the inodes: the bar looks healthy and every
    # write is about to fail, so the inode figure is stated beside it.
    assert any("inodes" in text and "97%" in text for text in texts)
    # It has no column of its own any more, because it is nearly always dull.
    assert "Inodes" not in texts
    # Mount options are not a column either: only "ro" ever mattered, and this
    # filesystem is writable.
    assert not any("noatime" in text for text in texts)
    assert "read-only" not in texts


def test_inodes_stay_quiet_when_the_bytes_run_out_first():
    """The bar already says the filesystem is nearly full; repeating a lower
    inode figure beside it adds nothing."""

    snapshot = _snapshot(
        filesystems=(
            FilesystemUsage(
                device="/dev/sda1", mount_point="/", fstype="ext4",
                size_bytes=100, used_bytes=99, available_bytes=1,
                inodes_total=1000, inodes_used=40, inodes_free=960,
            ),
        )
    )
    assert not any(
        "inodes" in t for t in _texts(_dialog(snapshot)._build_storage())
    )


def test_a_read_only_mount_says_so_because_it_explains_a_full_disk():
    snapshot = _snapshot(
        filesystems=(
            FilesystemUsage(
                device="/dev/mtdblock6", mount_point="/", fstype="squashfs",
                size_bytes=100, used_bytes=100, available_bytes=0,
                options="ro,relatime",
            ),
        )
    )
    texts = _texts(_dialog(snapshot)._build_storage())
    assert any("read-only" in text for text in texts)
    # The rest of the option string still does not appear.
    assert not any("relatime" in text for text in texts)


def test_a_filesystem_without_inode_counts_mentions_them_not_at_all():
    snapshot = _snapshot(
        filesystems=(
            FilesystemUsage(
                device="/dev/sdb1",
                mount_point="/data",
                fstype="btrfs",
                size_bytes=100,
                used_bytes=50,
                available_bytes=50,
            ),
        )
    )
    texts = _texts(_dialog(snapshot)._build_storage())
    assert not any("inodes" in t for t in texts)
    assert "0%" not in texts


# ---------------------------------------------------------------------------
# Process table
# ---------------------------------------------------------------------------

def test_process_counts_are_shown_against_the_kernel_pid_limit():
    snapshot = _snapshot(
        process_counts=ProcessCounts(
            total=180, running=2, sleeping=176, stopped=0, zombie=2,
            threads=612, pid_max=32768,
        )
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "Process table" in texts
    assert "180" in texts and "612" in texts
    assert any("32768" in text for text in texts)


def test_a_busybox_host_shows_the_counts_it_has_and_na_for_the_rest():
    """BusyBox ps has no -o, so only procs_running survives."""

    snapshot = _snapshot(process_counts=ProcessCounts(running=3))
    texts = _texts(_dialog(snapshot)._build_resources())
    assert "3" in texts
    assert "N/A" in texts


def test_a_host_that_reported_no_process_counts_says_so():
    texts = _texts(_dialog(_snapshot())._build_resources())
    assert "No process counts reported" in texts


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def test_the_memory_breakdown_uses_the_kernels_own_field_names():
    """So a reading here can be matched against /proc/meminfo on the host
    without a translation table."""

    snapshot = _snapshot(
        memory=MemoryInfo(
            total_bytes=1024 * 1024,
            free_bytes=1024,
            available_bytes=512 * 1024,
            active_bytes=400 * 1024,
            dirty_bytes=4096,
            slab_bytes=64 * 1024,
        )
    )
    texts = _texts(_dialog(snapshot)._build_resources())
    assert {"MemTotal", "MemAvailable", "Active", "Dirty", "Slab"} <= set(texts)


def test_a_live_sample_updates_the_memory_gauge_in_place():
    dialog = _dialog(_snapshot())
    dialog._build_overview()
    sample = LiveSample(
        memory=MemoryInfo(
            total_bytes=1000, free_bytes=100, available_bytes=250
        )
    )
    dialog._apply_readings(sample)
    assert "75%" in _texts(dialog._memory_gauge.widget)
