"""Turning two counter readings into a rate, and refusing to when it is a lie.

Kernels publish counters, never rates. Every case here is one where the
arithmetic *could* produce a number and that number would be wrong, so the
answer has to be "unknown" instead.
"""

from __future__ import annotations

from sshpilot.api.models.host_info import CpuTimes, InterfaceCounters
from sshpilot.core.host_info.rates import (
    cpu_utilization,
    cpu_utilization_by_name,
    interface_rates,
)


def _cpu(name="cpu", **fields):
    base = dict(
        user=0, nice=0, system=0, idle=0, iowait=0, irq=0, softirq=0, steal=0
    )
    base.update(fields)
    return CpuTimes(name=name, **base)


def test_a_known_pair_yields_the_shares_that_window_actually_spent():
    # 1000 jiffies pass: 100 user, 50 system, 50 iowait, 800 idle.
    before = _cpu(user=100, system=50, iowait=0, idle=1000)
    after = _cpu(user=200, system=100, iowait=50, idle=1800)

    utilization = cpu_utilization(before, after)
    assert utilization.user == 10.0
    assert utilization.system == 5.0
    assert utilization.iowait == 5.0
    assert utilization.idle == 80.0
    # A host waiting on storage is not idle, so iowait counts towards busy.
    assert utilization.total == 20.0


def test_the_first_sample_reports_nothing_rather_than_a_since_boot_rate():
    """Dividing counters since boot by the sampling interval yields an enormous
    number that looks like a real measurement. There is no rate yet."""

    assert cpu_utilization(None, _cpu(user=5_000_000, idle=90_000_000)) is None


def test_a_counter_going_backwards_discards_the_whole_reading():
    """Only a reboot makes a monotonic counter decrease, and after one the
    difference is meaningless rather than negative."""

    before = _cpu(user=5000, idle=90000)
    after = _cpu(user=10, idle=90)
    assert cpu_utilization(before, after) is None


def test_two_identical_readings_have_no_window_to_report_a_share_of():
    reading = _cpu(user=100, idle=900)
    assert cpu_utilization(reading, reading) is None


def test_a_column_one_kernel_omits_is_skipped_without_losing_the_line():
    """Some architectures stop before ``steal``; the rest is still usable."""

    before = CpuTimes(name="cpu", user=100, system=0, idle=900)
    after = CpuTimes(name="cpu", user=200, system=0, idle=1800)
    utilization = cpu_utilization(before, after)
    assert utilization.user == 10.0
    assert utilization.idle == 90.0
    assert utilization.total == 10.0
    # Absent columns stay absent rather than being counted as idle time.
    assert utilization.steal is None
    assert utilization.iowait is None
    assert utilization.nice is None


def test_lines_are_matched_by_name_so_an_offlined_core_shifts_nothing():
    """Offlining cpu1 shifts every later row; matching by position would then
    report cpu2's counters as cpu1's."""

    before = [
        _cpu("cpu", user=0, idle=1000),
        _cpu("cpu0", user=0, idle=1000),
        _cpu("cpu1", user=0, idle=1000),
        _cpu("cpu2", user=0, idle=1000),
    ]
    after = [
        _cpu("cpu", user=300, idle=2700),
        _cpu("cpu0", user=100, idle=1900),
        _cpu("cpu2", user=200, idle=1800),
    ]
    utilization = cpu_utilization_by_name(before, after)
    assert set(utilization) == {"cpu", "cpu0", "cpu2"}
    assert utilization["cpu0"].total == 10.0
    assert utilization["cpu2"].total == 20.0


def test_a_pair_that_cannot_be_compared_is_absent_not_zero():
    assert cpu_utilization_by_name([], [_cpu("cpu", user=1, idle=1)]) == {}


def test_interface_rates_are_bytes_per_second_over_the_measured_window():
    rates = interface_rates(
        [InterfaceCounters("eth0", 1_000, 2_000)],
        [InterfaceCounters("eth0", 5_000, 10_000)],
        2.0,
    )
    assert rates == {"eth0": (2_000.0, 4_000.0)}


def test_a_recreated_interface_reports_no_rate_rather_than_a_negative_one():
    rates = interface_rates(
        [InterfaceCounters("eth0", 9_000, 9_000)],
        [InterfaceCounters("eth0", 10, 20)],
        2.0,
    )
    assert rates == {}


def test_an_interface_the_previous_sample_never_saw_waits_for_a_baseline():
    rates = interface_rates(
        [InterfaceCounters("eth0", 0, 0)],
        [InterfaceCounters("eth0", 100, 100), InterfaceCounters("wg0", 500, 500)],
        1.0,
    )
    assert set(rates) == {"eth0"}


def test_a_zero_length_window_is_not_a_division_by_zero():
    counters = [InterfaceCounters("eth0", 1, 1)]
    assert interface_rates(counters, counters, 0.0) == {}
    assert interface_rates(counters, counters, -1.0) == {}
