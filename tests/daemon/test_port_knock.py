"""Sending a port-knock sequence with sockets instead of the ``knock`` tool.

These tests exist because a knock is easy to get subtly wrong in ways that
only show up against a real firewall, where the only symptom is "sometimes it
does not connect". The three that bite:

* the sequence must arrive **in order** -- ``knockd`` scores it out of its own
  packet log, and an out-of-order sequence is a *failed* sequence;
* the host must be resolved **once**, or a round-robin DNS name sprays one
  port at each of several machines and opens none of them;
* a refused or dropped knock is **success**. A knocked port is meant to be
  silent, so the whole sequence is expected to "fail" in socket terms. Only
  a packet that never left the machine is a real failure, and that case comes
  back from ``connect_ex`` as a return code rather than an exception, which is
  exactly how it gets missed.
"""

from __future__ import annotations

import errno
import socket
import threading

import pytest

from sshpilot.api.models.pre_command import (
    KnockStep,
    MAX_KNOCK_STEPS,
    format_knock_sequence,
    parse_knock_sequence,
)
from sshpilot.daemon.port_knock import (
    KnockResolutionError,
    knock,
    resolve_knock_address,
)


# --- the sequence syntax ------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("7000,8000,9000", ((7000, "tcp"), (8000, "tcp"), (9000, "tcp"))),
        ("7000 8000", ((7000, "tcp"), (8000, "tcp"))),
        ("7000:udp 8000:tcp", ((7000, "udp"), (8000, "tcp"))),
        ("7000, 8000 ,9000", ((7000, "tcp"), (8000, "tcp"), (9000, "tcp"))),
        ("  7000:UDP  ", ((7000, "udp"),)),
        ("", ()),
        ("   ", ()),
    ],
)
def test_the_sequence_syntax_matches_knock(text, expected):
    """``knock(1)``'s own syntax, so a sequence can be pasted from notes."""

    steps = parse_knock_sequence(text)
    assert tuple((s.port, s.protocol.value) for s in steps) == expected


@pytest.mark.parametrize(
    "text, message",
    [
        ("8OOO", "not a port number"),
        ("0", "between 1 and 65535"),
        ("65536", "between 1 and 65535"),
        ("-1", "between 1 and 65535"),
        ("80:sctp", "use tcp or udp"),
        ("7000:", "use tcp or udp"),
    ],
)
def test_a_malformed_sequence_says_what_is_wrong(text, message):
    """The editor shows this while the user can still fix it."""

    with pytest.raises(ValueError) as caught:
        parse_knock_sequence(text)
    assert message in str(caught.value)


def test_an_absurdly_long_sequence_is_refused():
    with pytest.raises(ValueError, match="at most"):
        parse_knock_sequence(",".join(["7000"] * (MAX_KNOCK_STEPS + 1)))


def test_formatting_round_trips_through_parsing():
    text = "7000 8000:udp 9000"
    assert format_knock_sequence(parse_knock_sequence(text)) == text


def test_a_step_refuses_a_port_out_of_range():
    with pytest.raises(ValueError):
        KnockStep(port=0)


# --- resolution ---------------------------------------------------------------


def test_an_unresolvable_host_is_refused_before_anything_is_sent():
    with pytest.raises(KnockResolutionError):
        knock("no-such-host.invalid.", parse_knock_sequence("7000"))


def test_an_empty_host_is_refused():
    with pytest.raises(KnockResolutionError):
        resolve_knock_address("")


def test_the_host_is_resolved_once_for_the_whole_sequence(monkeypatch):
    """A round-robin name must not scatter the sequence across machines.

    Resolving per knock would send one port to each member of the pool, so the
    firewall on every one of them sees a partial sequence and opens for none.
    It would also do so intermittently, which is close to undiagnosable.
    """

    calls = []
    real = socket.getaddrinfo

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", counting)
    outcome = knock("localhost", parse_knock_sequence("7001,7002,7003"), delay=0)
    assert len(calls) == 1
    assert outcome.sent == 3


def test_every_knock_goes_to_the_resolved_address_not_the_name():
    outcome = knock("localhost", parse_knock_sequence("7001,7002"), delay=0)
    assert outcome.address in {"127.0.0.1", "::1"}


# --- delivery -----------------------------------------------------------------


class _Listener:
    """A stand-in ``knockd``: records the order packets arrive in."""

    def __init__(self):
        self.seen = []
        self._sockets = []
        self._threads = []

    def tcp_port(self):
        handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        handle.bind(("127.0.0.1", 0))
        handle.listen(16)
        port = handle.getsockname()[1]
        self._watch(handle, port, self._accept)
        return port

    def udp_port(self):
        handle = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        handle.bind(("127.0.0.1", 0))
        port = handle.getsockname()[1]
        self._watch(handle, port, self._receive)
        return port

    def _watch(self, handle, port, target):
        self._sockets.append(handle)
        thread = threading.Thread(target=target, args=(handle, port), daemon=True)
        thread.start()
        self._threads.append(thread)

    def _accept(self, handle, port):
        while True:
            try:
                client, _ = handle.accept()
            except OSError:
                return
            self.seen.append(("tcp", port))
            client.close()

    def _receive(self, handle, port):
        while True:
            try:
                handle.recvfrom(64)
            except OSError:
                return
            self.seen.append(("udp", port))

    def close(self):
        for handle in self._sockets:
            handle.close()


@pytest.fixture
def listener():
    instance = _Listener()
    yield instance
    instance.close()


def _settle(listener, expected, timeout=2.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if len(listener.seen) >= expected:
            return
        deadline.wait(0.01)


def test_the_sequence_arrives_in_the_order_it_was_written(listener):
    """The property ``knockd`` actually scores."""

    ports = [listener.tcp_port() for _ in range(3)]
    steps = parse_knock_sequence(",".join(str(port) for port in ports))
    outcome = knock("127.0.0.1", steps)
    _settle(listener, 3)
    assert outcome.succeeded
    assert listener.seen == [("tcp", port) for port in ports]


def test_a_mixed_protocol_sequence_keeps_its_order(listener):
    first = listener.tcp_port()
    middle = listener.udp_port()
    last = listener.tcp_port()
    steps = parse_knock_sequence(f"{first} {middle}:udp {last}")
    outcome = knock("127.0.0.1", steps)
    _settle(listener, 3)
    assert outcome.sent == 3
    assert listener.seen == [("tcp", first), ("udp", middle), ("tcp", last)]


def test_a_refused_port_counts_as_knocked():
    """The whole point. A closed port is what a knock is *for*.

    The advice people follow by hand says so outright: run telnet at the port,
    "you will receive Connection refused but that is okay". The refusal proves
    the SYN arrived, which is the entire payload of a knock.
    """

    # Bind and close, so the port is almost certainly unused and refusing.
    handle = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    handle.bind(("127.0.0.1", 0))
    port = handle.getsockname()[1]
    handle.close()

    outcome = knock("127.0.0.1", parse_knock_sequence(str(port)), delay=0)
    assert outcome.succeeded
    assert outcome.sent == 1


def test_a_dropped_port_counts_as_knocked_and_does_not_hang():
    """How ``knockd`` is normally configured: DROP, not REJECT.

    Nothing comes back at all, so the connect can only end in a timeout. That
    timeout is the expected path, and it has to be a pacing bound rather than
    a deadline -- a five-port sequence to a silent host must stay about a
    second, not five times the TCP default.
    """

    # 240.0.0.0/4 is reserved and unroutable, so nothing replies.
    outcome = knock(
        "240.0.0.1", parse_knock_sequence("7000,8000,9000"), delay=0, timeout=0.05
    )
    assert outcome.succeeded
    assert outcome.sent == 3
    assert outcome.duration_ms < 2000


def test_a_packet_that_never_left_the_machine_is_a_failure(monkeypatch):
    """The case a naive implementation silently reports as success.

    ``ENETUNREACH`` comes back from ``connect_ex`` as a *return code*, not an
    exception, so "no exception means it was sent" tells a user whose VPN is
    down that the knock went fine -- and points the diagnosis at the firewall
    rather than at the thing that is actually broken.
    """

    class _Unreachable(socket.socket):
        def connect_ex(self, _address):
            return errno.ENETUNREACH

    monkeypatch.setattr(socket, "socket", _Unreachable)
    outcome = knock("127.0.0.1", parse_knock_sequence("7000,8000"), delay=0)
    assert not outcome.succeeded
    assert outcome.sent == 0
    assert "ENETUNREACH" in outcome.failure_reason


def test_one_unsendable_port_does_not_abandon_the_rest(monkeypatch):
    """A partly delivered sequence may still be scored; an abandoned one is not."""

    attempts = []
    real_socket = socket.socket

    class _FirstFails(real_socket):
        def connect_ex(self, address):
            attempts.append(address[1])
            if len(attempts) == 1:
                return errno.EHOSTUNREACH
            return super().connect_ex(address)

    monkeypatch.setattr(socket, "socket", _FirstFails)
    outcome = knock("127.0.0.1", parse_knock_sequence("7001,7002,7003"), delay=0)
    assert attempts == [7001, 7002, 7003]
    assert outcome.sent == 2
    assert len(outcome.failed) == 1


def test_an_empty_sequence_sends_nothing_and_resolves_nothing(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("an empty sequence must not resolve anything")

    monkeypatch.setattr(socket, "getaddrinfo", explode)
    outcome = knock("example.com", ())
    assert outcome.sent == 0
    assert outcome.succeeded


def test_knocks_are_paced_apart(monkeypatch):
    """Back-to-back packets can be logged out of order by ``knockd``."""

    slept = []
    outcome = knock(
        "127.0.0.1",
        parse_knock_sequence("7001,7002,7003"),
        delay=0.2,
        sleep=slept.append,
    )
    # Between knocks only: a trailing sleep would delay every connection by a
    # fifth of a second for nothing.
    assert slept == [0.2, 0.2]
    assert outcome.sent == 3


def test_udp_knocks_need_no_listener():
    """UDP has no handshake, so nothing can refuse it and nothing is waited for."""

    outcome = knock("127.0.0.1", parse_knock_sequence("7000:udp,8000:udp"), delay=0)
    assert outcome.succeeded
    assert outcome.sent == 2
    assert outcome.duration_ms < 500


@pytest.mark.skipif(
    not socket.has_ipv6, reason="IPv6 is unavailable on this machine"
)
def test_an_ipv6_host_is_knocked_over_ipv6():
    """Knocking the wrong family authorises an address that will not dial."""

    try:
        outcome = knock("::1", parse_knock_sequence("7001,7002"), delay=0)
    except KnockResolutionError:
        pytest.skip("no IPv6 loopback route")
    assert outcome.address == "::1"
    assert outcome.sent == 2
