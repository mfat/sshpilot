"""Wire round-trips and strictness for the host-information models."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sshpilot.api.models.common import ConnectionId
from sshpilot.api.models.host_info import (
    CpuInfo,
    CpuTimes,
    FailedUnit,
    FilesystemUsage,
    HostInfoFailure,
    HostInfoFailureCode,
    HostInfoProbe,
    HostInfoRequest,
    HostInfoSnapshot,
    HostInfoSummary,
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
from sshpilot.api.errors import ErrorCode
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    OperationSummary,
)
from sshpilot.api.transport.codec import (
    host_info_request_from_wire,
    host_info_request_to_wire,
    host_info_snapshot_from_wire,
    host_info_snapshot_to_wire,
    host_info_summary_from_wire,
    host_info_summary_to_wire,
)


def _operation() -> OperationSummary:
    return OperationSummary(
        OperationId("op-1"),
        OperationKind.BROADCAST_COMMAND,
        OperationState.SUCCEEDED,
        "done",
        datetime.now(timezone.utc),
    )


def _snapshot() -> HostInfoSnapshot:
    return HostInfoSnapshot(
        hostname="router",
        device_model="GL.iNet GL-AR750S",
        os_pretty_name="OpenWrt 23.05",
        kernel="Linux 5.15.134 mips",
        uptime_seconds=1234.5,
        boot_time="2026-09-01 18:00",
        cpu=CpuInfo(model="Atheros AR9344", logical_processors=1, bogomips=361.05),
        memory=MemoryInfo(
            total_bytes=131072,
            free_bytes=65536,
            available_bytes=98304,
            active_bytes=40960,
            inactive_bytes=20480,
            shmem_bytes=8192,
            dirty_bytes=4096,
            writeback_bytes=0,
            slab_bytes=16384,
            slab_reclaimable_bytes=8192,
        ),
        load_average=LoadAverage(0.1, 0.2, 0.3),
        filesystems=(
            FilesystemUsage(
                device="/dev/mtdblock6",
                mount_point="/overlay",
                fstype="jffs2",
                size_bytes=2048,
                used_bytes=512,
                available_bytes=1536,
                use_percent=25,
                options="rw,noatime",
                inodes_total=1024,
                inodes_used=256,
                inodes_free=768,
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
        temperatures=(TemperatureReading("cpu-thermal", 47.5),),
        sessions=(LoginSession(user="root", tty="pts/0", origin="10.0.0.9", since="09:15", remote=True),),
        sockets=(
            SocketConnection(
                protocol="tcp",
                local_address="10.0.0.5",
                local_port=22,
                peer_address="10.0.0.9",
                peer_port=51234,
                process="sshd",
                direction=SocketDirection.INCOMING,
            ),
        ),
        default_gateway="192.168.1.254",
        default_gateway_interface="wlan0",
        dns_servers=("1.1.1.1", "9.9.9.9"),
        ssh_port=22,
        ssh_process="sshd",
        os_id="openwrt",
        os_version_id="23.05.5",
        architecture="mips",
        listening_ports=(
            ListeningPort(port=22, process="sshd"),
            ListeningPort(port=53, process=""),
        ),
        processes=(
            ProcessUsage(command="hostapd", cpu_percent=137.5, memory_percent=1.5),
            ProcessUsage(command="procd", cpu_percent=0.5),
        ),
        failed_units=(FailedUnit(name="logrotate.service", description="Rotate logs"),),
        host_keys=(
            HostKeyFingerprint(algorithm="ED25519", fingerprint="SHA256:abc", bits=256),
        ),
        io_pressure_some=PressureStall(1.5, 0.75, 0.25),
        io_pressure_full=PressureStall(0.5, 0.25, 0.0),
        cpu_pressure_some=PressureStall(2.5, 1.75, 1.25),
        cpu_pressure_full=None,
        memory_pressure_some=PressureStall(0.2, 0.1, 0.05),
        memory_pressure_full=PressureStall(0.1, 0.05, 0.0),
        cpu_times=(
            CpuTimes(
                name="cpu",
                user=1000,
                nice=20,
                system=300,
                idle=90000,
                iowait=40,
                irq=5,
                softirq=6,
                steal=7,
                guest=8,
                guest_nice=9,
            ),
            CpuTimes(name="cpu0", user=500, system=150, idle=45000),
        ),
        process_counts=ProcessCounts(
            total=61, running=1, sleeping=59, stopped=0, zombie=1,
            threads=140, pid_max=32768,
        ),
        context_switches=987654321,
        interrupts=123456789,
    )


def test_request_round_trips():
    request = HostInfoRequest(ConnectionId("conn-1"), HostInfoProbe.NETWORK_COUNTERS)
    assert host_info_request_from_wire(host_info_request_to_wire(request)) == request


def test_snapshot_round_trips_every_field():
    snapshot = _snapshot()
    assert host_info_snapshot_from_wire(host_info_snapshot_to_wire(snapshot)) == snapshot


def test_absent_readings_survive_as_null():
    snapshot = HostInfoSnapshot(memory=MemoryInfo(total_bytes=10))
    wire = host_info_snapshot_to_wire(snapshot)
    assert wire["memory"]["available_bytes"] is None
    assert wire["load_average"] is None
    assert wire["ssh_port"] is None
    restored = host_info_snapshot_from_wire(wire)
    assert restored.memory.available_bytes is None
    assert restored.memory.used_bytes is None
    assert restored.load_average is None


def test_summary_round_trips_with_counters_and_no_snapshot():
    summary = HostInfoSummary(
        _operation(),
        HostInfoProbe.NETWORK_COUNTERS,
        None,
        (InterfaceCounters("eth0", 100, 200),),
    )
    assert host_info_summary_from_wire(host_info_summary_to_wire(summary)) == summary


def test_summary_round_trips_with_a_full_snapshot():
    summary = HostInfoSummary(_operation(), HostInfoProbe.FULL, _snapshot(), ())
    assert host_info_summary_from_wire(host_info_summary_to_wire(summary)) == summary


def test_summary_failure_round_trips_without_rendered_ui_text():
    failure = HostInfoFailure(
        HostInfoFailureCode.PROBE_FAILED,
        ErrorCode.REMOTE_COMMAND_FAILED,
        parameters={},
        diagnostic="ssh: connect to host example.test port 22: Connection refused",
    )
    summary = HostInfoSummary(
        _operation(), HostInfoProbe.FULL, None, (), failure
    )

    wire = host_info_summary_to_wire(summary)

    assert wire["failure"] == {
        "kind": "host_info",
        "code": "probe_failed",
        "error_code": "remote_command_failed",
        "parameters": {},
        "diagnostic": failure.diagnostic,
    }
    assert "message" not in wire["failure"]
    assert host_info_summary_from_wire(wire) == summary


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("kind", "other", "unknown kind"),
        ("code", "other", "unknown code"),
        ("error_code", "other", "unknown error code"),
        ("parameters", [], "must be an object"),
        ("message", "rendered English", "unsupported fields"),
    ),
)
def test_invalid_host_info_failure_payload_is_rejected(field, value, match):
    summary = HostInfoSummary(
        _operation(),
        failure=HostInfoFailure(
            HostInfoFailureCode.PROBE_FAILED,
            ErrorCode.REMOTE_COMMAND_FAILED,
        ),
    )
    wire = host_info_summary_to_wire(summary)
    wire["failure"][field] = value

    with pytest.raises(ValueError, match=match):
        host_info_summary_from_wire(wire)


def test_host_info_failure_rejects_unexpected_parameters():
    with pytest.raises(ValueError, match="parameters do not match"):
        HostInfoFailure(
            HostInfoFailureCode.PROBE_FAILED,
            ErrorCode.REMOTE_COMMAND_FAILED,
            parameters={"message": "rendered English"},
        )


def test_unknown_fields_are_rejected():
    wire = host_info_request_to_wire(HostInfoRequest(ConnectionId("conn-1")))
    wire["extra"] = True
    with pytest.raises(ValueError):
        host_info_request_from_wire(wire)


def test_unknown_probe_is_rejected():
    with pytest.raises(ValueError):
        host_info_request_from_wire({"connection_id": "conn-1", "probe": "nope"})


def test_unknown_interface_state_is_rejected():
    wire = host_info_snapshot_to_wire(_snapshot())
    wire["interfaces"][0]["state"] = "wobbling"
    with pytest.raises(ValueError):
        host_info_snapshot_from_wire(wire)


def test_models_reject_out_of_range_values():
    with pytest.raises(ValueError):
        SocketConnection(protocol="tcp", local_port=70000)
    with pytest.raises(ValueError):
        FilesystemUsage(device="d", mount_point="/", use_percent=101)
    with pytest.raises(ValueError):
        MemoryInfo(total_bytes=-1)


def test_a_kernel_without_psi_round_trips_as_absent():
    """Absent pressure is null on the wire, never a zeroed reading."""

    snapshot = _snapshot()
    without = HostInfoSnapshot(
        **{
            **{
                field: getattr(snapshot, field)
                for field in snapshot.__dataclass_fields__
            },
            "io_pressure_some": None,
            "io_pressure_full": None,
        }
    )
    wire = host_info_snapshot_to_wire(without)
    assert wire["io_pressure_some"] is None and wire["io_pressure_full"] is None
    assert host_info_snapshot_from_wire(wire) == without


def test_a_partial_pressure_reading_is_rejected():
    wire = host_info_snapshot_to_wire(_snapshot())
    wire["io_pressure_some"]["avg60"] = None
    with pytest.raises(ValueError):
        host_info_snapshot_from_wire(wire)


def test_models_reject_impossible_host_information():
    with pytest.raises(ValueError):
        ListeningPort(port=70000)
    with pytest.raises(ValueError):
        PressureStall(-1.0, 0.0, 0.0)
    with pytest.raises(TypeError):
        HostInfoSnapshot(io_pressure_some=(1.0, 2.0, 3.0))


def _live() -> LiveSample:
    return LiveSample(
        counters=(InterfaceCounters("wlan0", 1024, 2048),),
        cpu_times=(CpuTimes(name="cpu", user=1, nice=2, system=3, idle=4),),
        memory=MemoryInfo(total_bytes=131072, free_bytes=1024, available_bytes=2048),
        load_average=LoadAverage(0.5, 0.4, 0.3),
    )


def test_the_live_probe_is_a_wire_value_like_the_others():
    request = HostInfoRequest(ConnectionId("conn-1"), HostInfoProbe.LIVE)
    assert host_info_request_to_wire(request)["probe"] == "live"
    assert host_info_request_from_wire(host_info_request_to_wire(request)) == request


def test_the_bandwidth_only_probe_still_round_trips():
    """Removing a wire value narrows the protocol for no gain, so the probe the
    live one superseded keeps working."""

    request = HostInfoRequest(ConnectionId("conn-1"), HostInfoProbe.NETWORK_COUNTERS)
    assert host_info_request_to_wire(request)["probe"] == "network_counters"
    assert host_info_request_from_wire(host_info_request_to_wire(request)) == request


def test_a_live_summary_round_trips_with_its_sample():
    summary = HostInfoSummary(
        _operation(), HostInfoProbe.LIVE, None, _live().counters, None, _live()
    )
    assert host_info_summary_from_wire(host_info_summary_to_wire(summary)) == summary


def test_a_summary_without_a_live_sample_carries_an_explicit_null():
    summary = HostInfoSummary(_operation(), HostInfoProbe.FULL, _snapshot())
    wire = host_info_summary_to_wire(summary)
    assert wire["live"] is None
    assert host_info_summary_from_wire(wire) == summary


def test_cpu_times_round_trip_every_column_including_the_absent_ones():
    wire = host_info_snapshot_to_wire(_snapshot())
    assert [item["name"] for item in wire["cpu_times"]] == ["cpu", "cpu0"]
    assert wire["cpu_times"][1]["steal"] is None
    restored = host_info_snapshot_from_wire(wire)
    assert restored.cpu_times == _snapshot().cpu_times


def test_the_new_snapshot_fields_survive_the_wire():
    restored = host_info_snapshot_from_wire(host_info_snapshot_to_wire(_snapshot()))
    assert restored == _snapshot()
    assert restored.cpu_pressure_full is None
    assert restored.process_counts.pid_max == 32768
    assert restored.filesystems[0].options == "rw,noatime"
    assert restored.filesystems[0].inodes_used == 256
    assert restored.memory.slab_reclaimable_bytes == 8192


def test_a_live_sample_with_an_unknown_field_is_rejected():
    summary = HostInfoSummary(
        _operation(), HostInfoProbe.LIVE, None, _live().counters, None, _live()
    )
    wire = host_info_summary_to_wire(summary)
    wire["live"]["surprise"] = 1
    with pytest.raises(ValueError):
        host_info_summary_from_wire(wire)


def test_a_snapshot_missing_a_new_field_is_rejected_rather_than_defaulted():
    """A peer that does not send cpu_times is not a host with no CPUs."""

    wire = host_info_snapshot_to_wire(_snapshot())
    del wire["cpu_times"]
    with pytest.raises(ValueError):
        host_info_snapshot_from_wire(wire)
