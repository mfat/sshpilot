"""Turn two counter readings into the rates a frontend displays (GTK-free).

Kernels publish counters, not rates: ``/proc/stat`` and ``/proc/net/dev`` both
report totals since boot.  A percentage or a bytes-per-second figure exists
only *between* two readings, and the daemon is stateless across probes -- each
one is an independent operation -- so the differencing happens here, in pure
arithmetic a caller can test without a host, a socket or a clock.

Three rules decide what an answer is worth, and all three exist because the
alternative renders something false rather than nothing:

* with no previous reading there is no rate.  Dividing a since-boot total by
  the sampling interval yields an enormous number that looks like a real
  measurement, so the first sample reports ``None``;
* a counter that went backwards means the host rebooted or the interface was
  recreated.  The difference is then meaningless rather than negative, so the
  whole reading is discarded instead of being clamped to zero;
* a zero-length window has no rate to report, and asking for one is a division
  by zero rather than an idle host.
"""

from __future__ import annotations

from typing import Dict, Optional, Sequence, Tuple

from ...api.models.host_info import CpuTimes, CpuUtilization, InterfaceCounters

#: ``guest`` and ``guest_nice`` are deliberately absent: the kernel counts
#: guest time inside ``user`` and ``nice`` as well, so including them here
#: would inflate the denominator and understate every share.
_SHARE_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
)


def _deltas(previous: CpuTimes, current: CpuTimes) -> Optional[Dict[str, int]]:
    """Per-field differences, or ``None`` when the pair cannot be compared."""

    if previous is None or current is None or previous.name != current.name:
        return None
    deltas: Dict[str, int] = {}
    for name in _SHARE_FIELDS:
        before = getattr(previous, name)
        after = getattr(current, name)
        if before is None or after is None:
            # One reading published the column and the other did not; the
            # field is unusable, but the rest of the line still is.
            continue
        if after < before:
            # A counter only decreases across a reboot. Nothing in this
            # reading can be trusted after that.
            return None
        deltas[name] = after - before
    return deltas


def cpu_utilization(
    previous: Optional[CpuTimes], current: Optional[CpuTimes]
) -> Optional[CpuUtilization]:
    """The share of one CPU line's time spent in each state, in percent.

    ``total`` is ``100 - idle`` and so includes ``iowait``, which is also
    reported on its own: a host stalled on storage is not idle, but it is not
    computing either, and an operator needs to tell those apart.
    """

    deltas = _deltas(previous, current)
    if not deltas or "idle" not in deltas:
        return None
    window = sum(deltas.values())
    if window <= 0:
        return None

    def share(name: str) -> Optional[float]:
        delta = deltas.get(name)
        return None if delta is None else delta * 100.0 / window

    idle = share("idle")
    return CpuUtilization(
        total=None if idle is None else 100.0 - idle,
        user=share("user"),
        system=share("system"),
        idle=idle,
        iowait=share("iowait"),
        irq=share("irq"),
        softirq=share("softirq"),
        steal=share("steal"),
        nice=share("nice"),
    )


def cpu_utilization_by_name(
    previous: Sequence[CpuTimes], current: Sequence[CpuTimes]
) -> Dict[str, CpuUtilization]:
    """Utilization for every CPU line both readings share, keyed by name.

    Lines are matched by name rather than by position because a CPU can be
    offlined between two samples, which shifts every later row.
    """

    earlier = {item.name: item for item in previous or ()}
    utilization: Dict[str, CpuUtilization] = {}
    for item in current or ():
        computed = cpu_utilization(earlier.get(item.name), item)
        if computed is not None:
            utilization[item.name] = computed
    return utilization


def interface_rates(
    previous: Sequence[InterfaceCounters],
    current: Sequence[InterfaceCounters],
    elapsed: float,
) -> Dict[str, Tuple[float, float]]:
    """Received and transmitted bytes per second, keyed by interface name."""

    if not elapsed or elapsed <= 0:
        return {}
    earlier = {item.name: item for item in previous or ()}
    rates: Dict[str, Tuple[float, float]] = {}
    for item in current or ():
        before = earlier.get(item.name)
        if before is None:
            continue
        if item.rx_bytes < before.rx_bytes or item.tx_bytes < before.tx_bytes:
            # The interface was recreated or the host rebooted.
            continue
        rates[item.name] = (
            (item.rx_bytes - before.rx_bytes) / elapsed,
            (item.tx_bytes - before.tx_bytes) / elapsed,
        )
    return rates
