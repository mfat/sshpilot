"""Headless tests for the WebKit Host Info shell and payload builders."""

from sshpilot.api.models.host_info import (
    CpuInfo,
    CpuTimes,
    FilesystemUsage,
    HostInfoSnapshot,
    InterfaceCounters,
    LiveSample,
    LoadAverage,
    MemoryInfo,
    NetworkInterface,
    NetworkInterfaceKind,
    NetworkInterfaceState,
)
from sshpilot.host_info_payload import live_payload, snapshot_payload, status_payload
from sshpilot.host_info_shell import build_host_info_html


def _snapshot():
    return HostInfoSnapshot(
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
                mount_point="/",
                fstype="overlay",
                size_bytes=1000,
                used_bytes=400,
                available_bytes=600,
                use_percent=40,
            ),
        ),
        interfaces=(
            NetworkInterface(
                name="br-lan",
                kind=NetworkInterfaceKind.ETHERNET,
                state=NetworkInterfaceState.UP,
                mac_address="00:11:22:33:44:55",
                mtu=1500,
                ipv4_addresses=("192.168.1.1/24",),
            ),
        ),
        cpu_times=(
            CpuTimes(name="cpu", user=100, nice=0, system=50, idle=850),
            CpuTimes(name="cpu0", user=40, nice=0, system=20, idle=440),
        ),
    )


def test_shell_is_self_contained():
    html = build_host_info_html()
    assert "cdn.jsdelivr" not in html
    assert "window.applyHostInfo" in html
    assert "window.applyLive" in html
    assert "window.webkit.messageHandlers.hostInfo.postMessage" in html
    assert "Gathering host information" in html or "gathering" in html.lower()


def test_shell_stringifies_bridge_messages():
    html = build_host_info_html()
    assert "postMessage(JSON.stringify(msg))" in html


def test_shell_has_tab_labels():
    html = build_host_info_html()
    for label in ("Overview", "Resources", "Storage", "Network", "Traffic", "System"):
        assert label in html


def test_snapshot_payload_includes_overview_and_filesystems():
    payload = snapshot_payload(_snapshot(), title="Host", subtitle="user@host")
    assert payload["status"] == "ready"
    assert payload["title"] == "Host"
    assert len(payload["gauges"]) == 3
    assert payload["gauges"][1]["fraction"] is not None
    assert payload["filesystems"][0]["mount"] == "/"
    assert payload["interfaces"][0]["name"] == "br-lan"
    assert "192.168.1.1/24" in payload["interfaces"][0]["addresses"]
    assert any(row["label"] for row in payload["overview_rows"])


def test_live_payload_computes_rates_and_cpu():
    previous = LiveSample(
        counters=(InterfaceCounters(name="br-lan", rx_bytes=1000, tx_bytes=2000),),
        cpu_times=(
            CpuTimes(name="cpu", user=100, nice=0, system=50, idle=850),
            CpuTimes(name="cpu0", user=40, nice=0, system=20, idle=440),
        ),
        memory=MemoryInfo(
            total_bytes=1024 * 1024 * 512,
            available_bytes=1024 * 1024 * 128,
        ),
        load_average=LoadAverage(1.0, 0.5, 0.25),
    )
    sample = LiveSample(
        counters=(InterfaceCounters(name="br-lan", rx_bytes=3000, tx_bytes=6000),),
        cpu_times=(
            CpuTimes(name="cpu", user=150, nice=0, system=70, idle=880),
            CpuTimes(name="cpu0", user=60, nice=0, system=30, idle=450),
        ),
        memory=MemoryInfo(
            total_bytes=1024 * 1024 * 512,
            available_bytes=1024 * 1024 * 100,
        ),
        load_average=LoadAverage(1.2, 0.6, 0.3),
    )
    payload = live_payload(previous, sample, 2.0, frequency_mhz=1200.0)
    assert payload["cpu"]["fraction"] is not None
    assert payload["memory"]["fraction"] is not None
    assert "br-lan" in payload["rates"]
    assert payload["rates"]["br-lan"]["rx_rate"] != "—"
    assert payload["cores"]


def test_status_payload_marks_busy_and_error():
    busy = status_payload(title="t", subtitle="s", message="wait", busy=True)
    assert busy["status"] == "busy"
    err = status_payload(title="t", subtitle="s", message="fail", busy=False)
    assert err["status"] == "error"
