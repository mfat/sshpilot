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

**Non-Linux hosts.**  ``uname -s`` is read once into ``$os`` and every section
that would otherwise have to guess asks that variable instead of trying a
Linux tool and hoping it fails.  Guessing is not safe in both directions:
``cat /proc/meminfo`` genuinely fails everywhere there is no ``/proc``, but
``df -T -B1`` and ``ps -eo`` are *accepted* by the BSD tools with entirely
different meanings (``-T`` selects filesystem types, ``-e`` prints the
environment), so a fallback chain would read a plausible-looking wrong answer.
The rule followed here: a section reads ``$os`` whenever the Linux form could
succeed with different semantics, and relies on natural failure otherwise.

Darwin and the BSDs answer in sections of their own -- ``SYSCTL``, ``VM_STAT``,
``IFCONFIG``, ``NETSTAT_IB`` and the rest -- rather than by reshaping their
output into Linux's.  The parser joins them into the same DTOs, and a Linux
host pays one ``uname`` plus a handful of ``[`` tests for the whole
arrangement: no section it fills behaves differently than before.

One reading has no portable source.  Darwin publishes no cumulative CPU tick
counter to the shell (the BSDs have ``kern.cp_time``), so aggregate CPU
utilization is unavailable there; the per-process ``%CPU`` column and the load
average still are.
"""

from __future__ import annotations


def _posix_shell(script: str) -> str:
    """Wrap a probe so it runs under ``/bin/sh`` whatever the login shell is.

    OpenSSH hands a remote command to the account's login shell, and on the
    BSDs that is still routinely ``csh`` or ``tcsh`` -- the traditional default
    for root.  Neither understands ``var=value``, ``$(...)``, ``{ ...; }`` or
    ``for``, so an unwrapped probe dies on its first line there and the
    dashboard stays blank for the whole host -- as it always did, this being
    a shell the Linux-only probe never met.

    The script contains no single quote, backtick or ``!``, so one layer of
    single quotes is enough for both shell families -- csh performs no
    substitution inside them either, and ``!`` is the one character that would
    still be expanded there.
    """

    if "'" in script:
        # Checked rather than asserted: an assert would be compiled out under
        # -O, and a probe that grew a quote would then ship broken.
        raise ValueError("a probe must stay quotable as one sh argument")
    return "/bin/sh -c '" + script + "'"


SECTION_PATTERN = r"^===([A-Z_]+)===$"

#: ``uname -s`` once, at the top, so the sections below can branch on it
#: without each paying for a process of its own.
_OS = 'os=$(uname -s 2>/dev/null);'

#: Every sysctl the non-Linux branches read, in one call.  Both Darwin and the
#: BSDs print ``name: value`` and skip an unknown name with a message on
#: stderr, so one batch costs one process and the parser takes whatever came
#: back.  Keys absent on a given system simply do not appear.
_SYSCTL_KEYS = (
    "hw.model hw.machine hw.ncpu hw.physmem hw.realmem hw.memsize hw.pagesize"
    " hw.clockrate hw.cpufrequency hw.physicalcpu hw.logicalcpu"
    " machdep.cpu.brand_string machdep.cpu.core_count"
    " kern.ostype kern.osrelease kern.osproductversion kern.boottime"
    " kern.maxproc kern.pid_max kern.cp_time kern.cp_times"
    " vm.loadavg vm.swapusage"
    " vm.stats.vm.v_page_size vm.stats.vm.v_free_count vm.stats.vm.v_inactive_count"
    " vm.stats.vm.v_cache_count vm.stats.vm.v_active_count vm.stats.vm.v_wire_count"
)

#: The subset a live sample needs: counters to difference, plus the two
#: instantaneous readings.  Deliberately shorter than the full set -- this one
#: runs every couple of seconds.
_SYSCTL_LIVE_KEYS = (
    "hw.memsize hw.physmem hw.pagesize kern.cp_time kern.cp_times"
    " vm.loadavg vm.swapusage"
    " vm.stats.vm.v_page_size vm.stats.vm.v_free_count vm.stats.vm.v_inactive_count"
    " vm.stats.vm.v_cache_count vm.stats.vm.v_active_count vm.stats.vm.v_wire_count"
)

#: One full read-only gather.  Ordered cheapest-first so a host that dies
#: part-way still yields identity and resource sections.
_FULL_PROBE_SCRIPT = (
    _OS +
    # Which branch answered, so the parser reads a section's shape from the
    # host's own word for itself rather than sniffing the output.
    'echo "===OSTYPE==="; echo "$os";'
    'echo "===HOSTNAME==="; hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null;'
    'echo "===DEVICE_MODEL==="; { cat /tmp/sysinfo/model 2>/dev/null'
    ' || cat /sys/firmware/devicetree/base/model 2>/dev/null'
    ' || cat /sys/class/dmi/id/product_name 2>/dev/null'
    # Darwin names the machine in hw.model ("Macmini9,1"); FreeBSD puts the
    # CPU there and the machine in the SMBIOS environment, so only the
    # latter is read here and hw.model is left to the CPU section.
    ' || kenv smbios.system.product 2>/dev/null; }'
    ' | tr -d "\\000" | head -1;'
    'echo "===OS_RELEASE==="; cat /etc/os-release 2>/dev/null;'
    # Darwin has no /etc/os-release; the BSDs mostly do.  kern.ostype and
    # kern.osrelease in SYSCTL are the last resort for either.
    'echo "===SW_VERS==="; [ "$os" = Darwin ] && sw_vers 2>/dev/null;'
    'echo "===UNAME==="; uname -srm 2>/dev/null;'
    'echo "===SYSCTL==="; [ "$os" = Linux ] || sysctl ' + _SYSCTL_KEYS + ' 2>/dev/null;'
    'echo "===UPTIME==="; cat /proc/uptime 2>/dev/null;'
    # kern.boottime is an absolute instant, so the host's own clock has to
    # come back with it or the difference would be measured against ours.
    'echo "===NOW_EPOCH==="; [ "$os" = Linux ] || date +%s 2>/dev/null;'
    'echo "===BOOT_TIME==="; who -b 2>/dev/null;'
    'echo "===UPTIME_SINCE==="; uptime -s 2>/dev/null;'
    'echo "===LOADAVG==="; cat /proc/loadavg 2>/dev/null;'
    # Cumulative CPU jiffies, per core and in aggregate, plus the context
    # switch and interrupt counters.  A single reading is not a utilization:
    # it is the baseline the first live sample differences against.  The BSD
    # equivalent is kern.cp_time/kern.cp_times, read in SYSCTL above.
    'echo "===STAT==="; cat /proc/stat 2>/dev/null;'
    'echo "===NPROC==="; nproc 2>/dev/null;'
    'echo "===LSCPU==="; lscpu 2>/dev/null;'
    'echo "===CPUINFO==="; cat /proc/cpuinfo 2>/dev/null;'
    'echo "===MEMINFO==="; cat /proc/meminfo 2>/dev/null;'
    'echo "===VM_STAT==="; [ "$os" = Darwin ] && vm_stat 2>/dev/null;'
    # Darwin reports swap through vm.swapusage, already in SYSCTL.
    'echo "===SWAPINFO==="; [ "$os" = Linux ] || swapinfo -k 2>/dev/null;'
    # -T means "print the type column" to coreutils and "select these types"
    # to the BSD tools, so this one cannot be left to trial and error.  -P -k
    # is POSIX: 1024-byte blocks under a header that says so.
    'echo "===DF==="; if [ "$os" = Linux ]; then df -T -B1 2>/dev/null || df 2>/dev/null;'
    ' else df -P -k 2>/dev/null || df 2>/dev/null; fi;'
    # Inodes are a second way to fill a filesystem: a host can be at 3% of its
    # bytes and out of inodes, at which point writes fail with ENOSPC and the
    # usage bar looks fine.  BSD df -i prints the block columns too, which the
    # parser reads from the header rather than by position.
    'echo "===DF_INODES==="; if [ "$os" = Linux ]; then df -i -T 2>/dev/null || df -i 2>/dev/null;'
    ' else df -i 2>/dev/null; fi;'
    # Mount options, so a read-only or noatime mount says so.
    'echo "===MOUNTS==="; cat /proc/self/mounts 2>/dev/null;'
    'echo "===BSD_MOUNT==="; [ "$os" = Linux ] || mount -p 2>/dev/null || mount 2>/dev/null;'
    # Pressure stall information: the share of the last 10/60/300 seconds
    # spent waiting on I/O, on CPU, or on memory.  Absent before Linux 4.20
    # and on builds without CONFIG_PSI, which is why an absent reading stays
    # absent.  /proc/pressure/cpu publishes only a "some" line.
    'echo "===IO_PRESSURE==="; cat /proc/pressure/io 2>/dev/null;'
    'echo "===CPU_PRESSURE==="; cat /proc/pressure/cpu 2>/dev/null;'
    'echo "===MEM_PRESSURE==="; cat /proc/pressure/memory 2>/dev/null;'
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===NETSTAT_IB==="; [ "$os" = Linux ] || netstat -i -b -n 2>/dev/null;'
    'echo "===IP_ADDR==="; ip -o addr show 2>/dev/null;'
    'echo "===IP_LINK==="; ip -o link show 2>/dev/null;'
    'echo "===IFCONFIG==="; [ "$os" = Linux ] || ifconfig -a 2>/dev/null;'
    'echo "===IP_ROUTE==="; ip route show default 2>/dev/null;'
    'echo "===NETSTAT_RN==="; [ "$os" = Linux ] || netstat -rn -f inet 2>/dev/null;'
    'echo "===DNS==="; cat /etc/resolv.conf 2>/dev/null;'
    'echo "===SS_LISTEN==="; [ "$os" = Linux ] &&'
    ' { ss -tlnp 2>/dev/null || netstat -tlnp 2>/dev/null; };'
    'echo "===SS_ESTAB==="; [ "$os" = Linux ] &&'
    ' { ss -tunap state established 2>/dev/null || netstat -tunap 2>/dev/null; };'
    # One listing for both questions on the BSD side: -p tcp keeps the local
    # (UNIX) domain sockets out, whose columns mean something else entirely.
    'echo "===NETSTAT_AN==="; [ "$os" = Linux ] || netstat -an -p tcp 2>/dev/null;'
    'echo "===WHO==="; who 2>/dev/null;'
    'echo "===W==="; w -h 2>/dev/null;'
    # Ranked by CPU share.  BusyBox ps has neither -o nor --sort, so OpenWrt
    # leaves PROCESSES empty and the parser reads TOP instead -- the same
    # two-section fallback WHO and W use.  BSD ps sorts with -r and spells
    # "every process" -ax, since -e there prints the environment.
    'echo "===PROCESSES==="; if [ "$os" = Linux ];'
    ' then ps -eo pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -n 11;'
    ' else ps -ax -r -o pcpu,pmem,comm 2>/dev/null | head -n 11; fi;'
    # head swallows the exit status of the pipeline above, so the same
    # invocation decides whether top is needed at all.  Repeating it costs a
    # few milliseconds; running top where ps already answered costs half a
    # second on every gather.
    'echo "===TOP==="; [ "$os" = Linux ] &&'
    ' { ps -eo pcpu,pmem,comm --sort=-pcpu >/dev/null 2>&1'
    ' || top -bn1 2>/dev/null | head -n 16; };'
    # One state letter and one thread count per process.  procps prints both;
    # a ps without -o leaves the section empty and the counts fall back to
    # procs_running/procs_blocked from STAT, which every Linux publishes.
    'echo "===PROC_STATES==="; if [ "$os" = Linux ];'
    ' then ps -eo stat=,nlwp= 2>/dev/null || ps -eo stat= 2>/dev/null;'
    ' else ps -ax -o state= 2>/dev/null; fi;'
    'echo "===PID_MAX==="; cat /proc/sys/kernel/pid_max 2>/dev/null;'
    # systemd only.  A host running procd, OpenRC or sysvinit reports nothing
    # here, which reads as "no failed units" -- the same as a healthy host,
    # because neither has any to report.
    'echo "===SYSTEMD_FAILED==="; systemctl --failed --no-legend --plain 2>/dev/null;'
    'echo "===TEMPS==="; [ "$os" = Linux ] &&'
    ' { for f in /sys/class/thermal/thermal_zone*/temp; do echo "$f:$(cat "$f" 2>/dev/null)"; done 2>/dev/null; };'
    'echo "===TEMP_TYPES==="; [ "$os" = Linux ] &&'
    ' { for f in /sys/class/thermal/thermal_zone*/type; do echo "$f:$(cat "$f" 2>/dev/null)"; done 2>/dev/null; };'
    'echo "===SENSORS==="; sensors 2>/dev/null;'
    # coretemp/amdtemp publish per-core temperatures on FreeBSD when loaded;
    # Darwin publishes none without a third-party kext, so this is usually
    # empty there.  Two named subtrees rather than sysctl -a: the full dump is
    # thousands of lines for two readings.
    'echo "===BSD_TEMPS==="; [ "$os" = Linux ] ||'
    ' sysctl dev.cpu hw.acpi.thermal 2>/dev/null | grep -i temperature;'
    # sshd exports the exact endpoint this very command arrived on, so the
    # SSH port is read from the host rather than assumed.
    'echo "===SSH_CONNECTION==="; echo "$SSH_CONNECTION";'
    # A wireless interface has a wireless/phy80211 node; the name is not
    # evidence of anything.  The BSD equivalent is the media line in
    # IFCONFIG, which names 802.11 where there is a radio.
    'echo "===WIRELESS==="; ls -d /sys/class/net/*/wireless /sys/class/net/*/phy80211 2>/dev/null;'
    # Public key material only: these are the .pub files, never a private key.
    # ssh-keygen -l takes one file at a time, so the glob is walked here
    # rather than handed over as a pattern.
    'echo "===SSH_HOST_KEYS==="; for f in /etc/ssh/ssh_host_*_key.pub;'
    ' do ssh-keygen -l -f "$f" 2>/dev/null; done;'
    'echo "===END===";'
)

#: The live-sampling probe.  A few small reads in one round trip, cheap enough
#: to repeat every couple of seconds: the counters a rate needs, plus the two
#: instantaneous readings (memory, load) the dialog keeps on screen.
_LIVE_PROBE_SCRIPT = (
    _OS +
    'echo "===OSTYPE==="; echo "$os";'
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===STAT==="; cat /proc/stat 2>/dev/null;'
    'echo "===MEMINFO==="; cat /proc/meminfo 2>/dev/null;'
    'echo "===LOADAVG==="; cat /proc/loadavg 2>/dev/null;'
    'echo "===NETSTAT_IB==="; [ "$os" = Linux ] || netstat -i -b -n 2>/dev/null;'
    'echo "===SYSCTL==="; [ "$os" = Linux ] || sysctl ' + _SYSCTL_LIVE_KEYS + ' 2>/dev/null;'
    'echo "===VM_STAT==="; [ "$os" = Darwin ] && vm_stat 2>/dev/null;'
    'echo "===SWAPINFO==="; [ "$os" = Linux ] || swapinfo -k 2>/dev/null;'
    'echo "===END===";'
)

#: The original bandwidth-only probe.  Superseded by :data:`LIVE_PROBE_COMMAND`
#: and kept because ``HostInfoProbe.NETWORK_COUNTERS`` is a wire value: removing
#: it would narrow the protocol for no gain.
_NETWORK_COUNTERS_SCRIPT = (
    _OS +
    'echo "===OSTYPE==="; echo "$os";'
    'echo "===NET_DEV==="; cat /proc/net/dev 2>/dev/null;'
    'echo "===NETSTAT_IB==="; [ "$os" = Linux ] || netstat -i -b -n 2>/dev/null;'
    'echo "===END===";'
)

FULL_PROBE_COMMAND = _posix_shell(_FULL_PROBE_SCRIPT)
LIVE_PROBE_COMMAND = _posix_shell(_LIVE_PROBE_SCRIPT)
NETWORK_COUNTERS_COMMAND = _posix_shell(_NETWORK_COUNTERS_SCRIPT)
