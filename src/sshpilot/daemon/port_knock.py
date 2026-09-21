"""Send a port-knock sequence directly, without shelling out to ``knock``.

A knock is not a protocol. It is a series of connection *attempts* to closed
ports, in order; the firewall watches its own logs for the pattern and opens
the real port to whoever produced it. Nothing is transmitted and nothing
replies, so every attempt "fails" -- that is the normal case, not an error.
The advice people trade for doing it by hand says exactly this:

    telnet host 7000; telnet host 8000; telnet host 9000
    "You will receive 'Connection refused' but that is okay"

The refusal *is* the knock. All the firewall ever sees is the TCP SYN, and a
refusal proves it arrived. This module sends that SYN directly.

Why not just run ``knock``: the daemon ships in a Flatpak, where ``knock`` is
not installed and cannot be, so the shell path has to leave the sandbox via
``flatpak-spawn --host`` and hope the user installed it there. Every bug this
feature has had -- a capture pipe held open by a backgrounded grandchild, the
login-shell dance to find Homebrew's binaries, host routing, an unreaped
process tree -- comes from shelling out. A socket has none of them, needs
nothing installed, and behaves identically on every platform we ship.

The shell command stays and runs *after* this, because ``fwknop``'s encrypted
single-packet authorisation is a real protocol with crypto and replay
protection, not a port sequence, and cannot be reimplemented as one.

Syntax is ``knock(1)``'s, so a sequence can be pasted from whatever notes or
``knockd.conf`` the user already has.
"""

from __future__ import annotations

import errno
import logging
import socket
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from sshpilot.api.models.pre_command import KnockProtocol, KnockStep

logger = logging.getLogger(__name__)

#: Pause between knocks. ``knockd`` reads a sequence out of its own packet log,
#: and packets sent back to back can be logged out of order, which scores as a
#: *failed* sequence.
#:
#: Deliberately more than ``knock(1)``, which paces nothing at all by default
#: and sends a whole sequence in under ten milliseconds; so does the ``nmap``
#: loop the Arch wiki gives as a client. The published advice that does pace
#: goes further than this -- Teleport's walkthrough uses ``-d 500``.
#:
#: The ceiling is what stops this being tuned upwards to be safe: a knock
#: sequence is often tracked by per-stage expiry, and the nftables wiki's
#: first example expires each stage after ``timeout 1s``. Past that a slower
#: knock does not merely lag, it *fails*, so the delay has to stay a small
#: fraction of a second. 0.2s is enough to keep the packet log ordered with
#: room under a one-second stage.
DEFAULT_KNOCK_DELAY_SECONDS = 0.2

#: How long to wait for a single knock before moving on. A knocked port is
#: normally DROPped rather than rejected, so the connect never completes and
#: this timeout is the expected path, not a fault: it is a pacing bound, not a
#: deadline. Long enough that a slow link still gets the SYN out, short enough
#: that a five-port sequence to a silent host stays about a second.
DEFAULT_KNOCK_TIMEOUT_SECONDS = 0.2


@dataclass(frozen=True)
class KnockOutcome:
    """What happened to a whole sequence.

    There is no per-port success to report. A knock has no acknowledgement, so
    "did it work" is only answerable by whether the real port opened, which is
    the SSH connection's job to discover. All this can say is whether the
    packets went out.
    """

    #: Ports whose packet we handed to the kernel.
    sent: int
    #: Ports we could not send at all, as ``(step, reason)``.
    failed: tuple
    #: The address every knock went to, for the log.
    address: str
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        return not self.failed

    @property
    def failure_reason(self) -> str:
        """The raw reason the first undelivered knock gave, or ``""``.

        Raw on purpose: this is the only part of a knock result that reaches
        the user, and it reaches them beside a sentence the *frontend* wrote
        and can translate. Composing an English sentence here would put
        untranslatable prose on screen -- the same split the rest of this API
        uses, where the daemon ships codes and the frontend ships words.
        """
        return self.failed[0][1] if self.failed else ""

    @property
    def log_summary(self) -> str:
        """A one-line description for the log. Never shown to the user."""
        if self.succeeded:
            return f"knocked {self.sent} port(s) on {self.address}"
        return f"could not knock {self.address}: {self.failure_reason}"


class KnockResolutionError(Exception):
    """The host could not be resolved, so there is nowhere to knock."""


def resolve_knock_address(host: str, *, family: int = socket.AF_UNSPEC):
    """Pick the one address the whole sequence will be sent to.

    Resolved once, deliberately. A host behind round-robin DNS resolves to a
    different member each time, so resolving per knock would spray one port at
    each of several machines and open the firewall on none of them. Worse, it
    would do so intermittently, which is the hardest possible thing to
    diagnose.

    Picking the *first* result also keeps the sequence on the same address
    family OpenSSH will use: on a dual-stacked host, knocking over IPv4 and
    then connecting over IPv6 authorises an address that is not the one that
    dials, and the connection is refused with the firewall wide open to the
    wrong family. ``AI_ADDRCONFIG`` is what makes that first result match --
    it is the same rule ``connect()`` follows.
    """
    if not host:
        raise KnockResolutionError("no host to knock")
    try:
        results = socket.getaddrinfo(
            host, None, family, socket.SOCK_STREAM, 0, socket.AI_ADDRCONFIG
        )
    except OSError as error:
        raise KnockResolutionError(str(error)) from error
    if not results:
        raise KnockResolutionError(f"{host} did not resolve to an address")
    resolved_family, _, _, _, sockaddr = results[0]
    return resolved_family, sockaddr


def knock(
    host: str,
    steps: Sequence[KnockStep],
    *,
    delay: float = DEFAULT_KNOCK_DELAY_SECONDS,
    timeout: float = DEFAULT_KNOCK_TIMEOUT_SECONDS,
    family: int = socket.AF_UNSPEC,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> KnockOutcome:
    """Send *steps* to *host* in order.

    Raises :class:`KnockResolutionError` only when the host does not resolve.
    Everything after that is reported in the outcome: an individual port that
    could not be reached is worth saying, but it is not worth raising over,
    because the remaining ports should still be sent -- a sequence that is
    partly delivered may still be scored, and one we abandoned certainly is
    not.

    No source address is bound. The kernel already picks the source the route
    to *host* would use, which is the one OpenSSH will connect from, including
    over a VPN the pre-connection command just brought up. Binding a guess
    here could only make a multi-homed machine knock from the wrong interface.
    """
    if not steps:
        return KnockOutcome(sent=0, failed=(), address="", duration_ms=0)

    resolved_family, sockaddr = resolve_knock_address(host, family=family)
    address = sockaddr[0]
    started = clock()
    sent = 0
    failed = []

    for index, step in enumerate(steps):
        if index:
            # Between knocks only, and nothing is slept after the last one.
            # A pause before connecting is a plausible-sounding thing to add
            # -- the firewall has to run its rule command once the sequence
            # lands -- but none of the field does it: ``knock(1)``, the Arch
            # wiki's client, Teleport's walkthrough and the nftables examples
            # all go straight from the final knock to ``ssh``. It would also
            # be spent out of the wrong budget. What the server grants is a
            # window to *connect* in, and the short configurations are short:
            # ``cmd_timeout = 5`` in knockd's own examples, ``timeout 10s``
            # in the nftables one. A settle would spend a tenth of that
            # guarding a race the SSH SYN's own retransmit already covers.
            sleep(delay)
        error = _send_one(resolved_family, sockaddr, step, timeout)
        if error is None:
            sent += 1
        else:
            failed.append((step, error))
            logger.debug(
                "port knock could not be sent port=%d proto=%s",
                step.port,
                step.protocol.value,
            )

    duration_ms = int(round((clock() - started) * 1000))
    return KnockOutcome(
        sent=sent, failed=tuple(failed), address=address, duration_ms=duration_ms
    )


#: Results of a knock ``connect()`` that mean the SYN reached the network.
#:
#: A knocked port is supposed to be silent, so almost everything here is a
#: "failure" in ordinary socket terms and a success for our purpose. ``0`` is
#: the port being genuinely open, ``ECONNREFUSED``/``ECONNRESET`` are a reply
#: that proves delivery, and the timeout family is a port being DROPped, which
#: is how ``knockd`` is normally configured.
#:
#: What is deliberately *not* here is the unreachable family -- ``ENETUNREACH``
#: and friends come back as a return code rather than an exception, so treating
#: any non-raising result as sent reports a confident knock to a user whose VPN
#: is down. That is worse than silence: it points the diagnosis away from the
#: actual fault.
_DELIVERED_ERRNOS = frozenset(
    {
        0,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ETIMEDOUT,
        errno.EAGAIN,
        errno.EWOULDBLOCK,
        errno.EINPROGRESS,
        errno.EALREADY,
        errno.EISCONN,
    }
)


def _send_one(family: int, sockaddr, step: KnockStep, timeout: float) -> Optional[str]:
    """Send one packet. Returns ``None`` on success, else a short reason.

    "Success" means the packet reached the network. A refusal, a reset or a
    timeout afterwards are all normal outcomes of knocking a closed port and
    are deliberately not distinguished -- the whole sequence is expected to
    produce them, so counting them as failures would report every correct
    knock as broken.
    """
    kind = (
        socket.SOCK_STREAM
        if step.protocol is KnockProtocol.TCP
        else socket.SOCK_DGRAM
    )
    target = (sockaddr[0], step.port) + tuple(sockaddr[2:])
    try:
        with socket.socket(family, kind) as handle:
            handle.settimeout(timeout)
            if step.protocol is KnockProtocol.TCP:
                code = handle.connect_ex(target)
                if code not in _DELIVERED_ERRNOS:
                    return errno.errorcode.get(code, f"error {code}")
            else:
                # UDP carries no handshake, so an empty datagram is the whole
                # knock. Nothing comes back and nothing is waited for; an
                # unreachable port answers with ICMP we never read.
                handle.sendto(b"", target)
    except OSError as error:
        return error.strerror or str(error)
    return None


__all__ = [
    "DEFAULT_KNOCK_DELAY_SECONDS",
    "DEFAULT_KNOCK_TIMEOUT_SECONDS",
    "KnockOutcome",
    "KnockResolutionError",
    "knock",
    "resolve_knock_address",
]
