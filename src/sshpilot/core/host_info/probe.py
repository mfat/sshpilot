"""Read-only shell probes the daemon runs to collect remote host information.

The probe text lives here, next to the parser that understands it, so the two
can never drift apart and no frontend has to know what is executed on the
remote host.  Every command is read-only, redirects its own stderr, and is
guarded by ``|| true`` semantics through ``2>/dev/null`` so a missing tool
leaves its section empty instead of aborting the script.

Sections are delimited by ``===NAME===`` markers.  BusyBox and OpenWrt hosts
lack ``lscpu``, ``ss``, ``who`` and coreutils ``df``, so each section names a
fallback that the parser normalises to the same DTO shape.

Two sections are read by both probes on purpose.  ``STAT`` and ``NET_DEV`` are
cumulative counters, and a rate is a difference between two readings of them:
the full gather takes the baseline so the *first* live sample already has
something to subtract from, instead of showing "since boot" as if it were a
current rate.
"""

from __future__ import annotations

SECTION_PATTERN = r"^===([A-Z_]+)===$"

#: One full read-only gather.  Ordered cheapest-first so a host that dies
#: part-way still yields identity and resource sections.
FULL_PROBE_COMMAND = (
    'echo "===HOSTNAME==="; hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null;'
    'echo "===DEVICE_MODEL==="; { cat /tmp/sysinfo/model 2>/dev/null'
    ' || cat /sys/firmware/devicetree/base/model 2>/dev/null'
    ' || cat /sys/class/dmi/id/product_name 2>/dev/null; }'
    " | tr -d '\\000' | head -1;"
    'echo "===OS_RELEASE==="; cat /etc/os-release 2>/dev/null;'
    'echo "===UNAME==="; uname -srm 2>/dev/null;'
    'echo "===UPTIME==="; cat /proc/uptime 2>/dev/null;'
    'echo "===BOOT_TIME==="; who -b 2>/dev/null;'
    'echo "===UPTIME_SINCE==="; uptime -s 2>/dev/null;'
    'echo "===LOADAVG==="; cat /proc/loadavg 2>/dev/null;'
    # Cumulative CPU jiffies, per core and in aggregate, plus the context
    # switch and interrupt counters.  A single reading is not a utilization:
    # it is the baseline the first live sample differences against.
    'echo "===STAT==="; cat /proc/stat 2>/dev/null;'
    'echo "===NPROC==="; nproc 2>/dev/null;'
    'echo "===LSCPU==="; lscpu 2>/dev/null;'
    'echo "===CPUINFO==="; cat /proc/cpuinfo 2>/dev/null;'
    'echo "===MEMINFO==="; cat /proc/meminfo 2>/dev/null;'
    'echo "===DF==="; df -T -B1 2>/dev/null || df 2>/dev/null;'
    # Inodes are a second way to fill a filesystem: a host can be at 3% of its
    # bytes and out of inodes, at which point writes fail with ENOSPC and the
    # usage bar looks fine.
    'echo "===DF_INODES==="; df -i -T 2>/dev/null || df -i 2>/dev/null;'
    # Mount options, so a read-only or noatime mount says so.
    'echo "===MOUNTS==="; cat /proc/self/mounts 2>/dev/null;'
    # Pressure stall information: the share of the last 10/60/300 seconds
    # spent waiting on I/O, on CPU, or on memory.  Absent before Linux 4.20
    # and on builds without CONFIG_PSI, which is why an absent reading stays
    # absent.  /proc/pressure/cpu publishes only a "some" line.
    'echo "===IO_PRESSURE==="; cat /proc/pressure/io 2>/dev/null;'
    'echo "===CPU_PRESSURE==="; cat /proc/pressure/cpu 2>/dev/null;'
    'echo "===MEM_PRESSURE==="; cat /proc/pressure/memory 2>/dev/null;'
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===IP_ADDR==="; ip -o addr show 2>/dev/null;'
    'echo "===IP_LINK==="; ip -o link show 2>/dev/null;'
    'echo "===IP_ROUTE==="; ip route show default 2>/dev/null;'
    'echo "===DNS==="; cat /etc/resolv.conf 2>/dev/null;'
    'echo "===SS_LISTEN==="; ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null;'
    'echo "===SS_ESTAB==="; ss -tunap state established 2>/dev/null || netstat -tunap 2>/dev/null;'
    'echo "===WHO==="; who 2>/dev/null;'
    'echo "===W==="; w -h 2>/dev/null;'
    # Ranked by CPU share.  BusyBox ps has neither -o nor --sort, so OpenWrt
    # leaves PROCESSES empty and the parser reads TOP instead -- the same
    # two-section fallback WHO and W use.
    'echo "===PROCESSES==="; ps -eo pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -n 11;'
    # head swallows the exit status of the pipeline above, so the same
    # invocation decides whether top is needed at all.  Repeating it costs a
    # few milliseconds; running top where ps already answered costs half a
    # second on every gather.
    'echo "===TOP==="; ps -eo pcpu,pmem,comm --sort=-pcpu >/dev/null 2>&1'
    ' || top -bn1 2>/dev/null | head -n 16;'
    # One state letter and one thread count per process.  procps prints both;
    # a ps without -o leaves the section empty and the counts fall back to
    # procs_running/procs_blocked from STAT, which every Linux publishes.
    'echo "===PROC_STATES==="; ps -eo stat=,nlwp= 2>/dev/null || ps -eo stat= 2>/dev/null;'
    'echo "===PID_MAX==="; cat /proc/sys/kernel/pid_max 2>/dev/null;'
    # systemd only.  A host running procd, OpenRC or sysvinit reports nothing
    # here, which reads as "no failed units" -- the same as a healthy host,
    # because neither has any to report.
    'echo "===SYSTEMD_FAILED==="; systemctl --failed --no-legend --plain 2>/dev/null;'
    'echo "===TEMPS==="; for f in /sys/class/thermal/thermal_zone*/temp; do echo "$f:$(cat "$f" 2>/dev/null)"; done 2>/dev/null;'
    'echo "===TEMP_TYPES==="; for f in /sys/class/thermal/thermal_zone*/type; do echo "$f:$(cat "$f" 2>/dev/null)"; done 2>/dev/null;'
    'echo "===SENSORS==="; sensors 2>/dev/null;'
    # sshd exports the exact endpoint this very command arrived on, so the
    # SSH port is read from the host rather than assumed.
    'echo "===SSH_CONNECTION==="; echo "$SSH_CONNECTION";'
    # A wireless interface has a wireless/phy80211 node; the name is not
    # evidence of anything.
    'echo "===WIRELESS==="; ls -d /sys/class/net/*/wireless /sys/class/net/*/phy80211 2>/dev/null;'
    # Public key material only: these are the .pub files, never a private key.
    # ssh-keygen -l takes one file at a time, so the glob is walked here
    # rather than handed over as a pattern.
    'echo "===SSH_HOST_KEYS==="; for f in /etc/ssh/ssh_host_*_key.pub;'
    ' do ssh-keygen -l -f "$f" 2>/dev/null; done;'
    'echo "===END===";'
)

#: The live-sampling probe.  Four small reads in one round trip, cheap enough
#: to repeat every couple of seconds: the counters a rate needs, plus the two
#: instantaneous readings (memory, load) the dialog keeps on screen.
LIVE_PROBE_COMMAND = (
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===STAT==="; cat /proc/stat 2>/dev/null;'
    'echo "===MEMINFO==="; cat /proc/meminfo 2>/dev/null;'
    'echo "===LOADAVG==="; cat /proc/loadavg 2>/dev/null;'
    'echo "===END===";'
)

#: The original bandwidth-only probe.  Superseded by :data:`LIVE_PROBE_COMMAND`
#: and kept because ``HostInfoProbe.NETWORK_COUNTERS`` is a wire value: removing
#: it would narrow the protocol for no gain.
NETWORK_COUNTERS_COMMAND = (
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===END===";'
)
