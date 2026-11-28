"""The SFTP client's inactivity timeout (#1435), against a fake server.

A server that stops answering while requests wait must close the connection
after the timeout, as FileZilla does, rather than leave the caller blocked
until ssh's keepalive gives up (or forever without one).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from sshpilot.sftp import client as sftp_client
from sshpilot.sftp import protocol as proto


class _Server(threading.Thread):
    """Answers every request with STATUS OK after ``delay``; silent when told."""

    def __init__(self, r, w, *, extensions=None) -> None:
        super().__init__(daemon=True)
        self._r, self._w = r, w
        self._extensions = extensions or {}
        self.silent = threading.Event()
        self.delay = 0.0
        self.copy_delay = 0.0

    def _read_exact(self, n):
        data = b""
        while len(data) < n:
            chunk = self._r.read(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _packet(self):
        length = int.from_bytes(self._read_exact(4), "big")
        body = self._read_exact(length)
        return body[0], body[1:]

    def _send(self, ptype, payload):
        self._w.write(proto.build_packet(ptype, payload))
        self._w.flush()

    def run(self):
        try:
            self._packet()  # INIT
            version = proto.pack_uint32(proto.PROTOCOL_VERSION)
            for name, value in self._extensions.items():
                version += proto.pack_string(name) + proto.pack_string(value)
            self._send(proto.FXP_VERSION, version)
            while True:
                ptype, payload = self._packet()
                rid = proto._Reader(payload).uint32()
                if self.silent.is_set():
                    continue
                delay = self.delay
                if ptype == proto.FXP_EXTENDED and b"copy-data" in payload:
                    delay = self.copy_delay
                if delay:
                    time.sleep(delay)
                self._send(
                    proto.FXP_STATUS,
                    proto.pack_uint32(rid) + proto.pack_uint32(proto.FX_OK)
                    + proto.pack_string("") + proto.pack_string(""),
                )
        except (EOFError, OSError, ValueError):
            return


def _client(timeout, **server_kw):
    csock, ssock = socket.socketpair()
    closed = threading.Event()

    def _teardown():
        closed.set()
        try:
            csock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    client = sftp_client.OpenSSHSFTPClient(
        csock.makefile("wb"), csock.makefile("rb"),
        on_close=_teardown, inactivity_timeout=timeout,
    )
    server = _Server(ssock.makefile("rb"), ssock.makefile("wb"), **server_kw)
    server.start()
    client.start()
    return client, server, closed


def test_silent_server_times_out_and_closes_the_connection():
    client, server, closed = _client(0.4)
    try:
        server.silent.set()
        started = time.monotonic()
        with pytest.raises(proto.SFTPConnectionTimeout):
            client.remove("/srv/x")
        assert time.monotonic() - started < 3.0
        assert closed.is_set()
        assert client.timed_out
        # Later requests fail the same way instead of blocking.
        with pytest.raises(proto.SFTPConnectionTimeout):
            client.remove("/srv/y")
    finally:
        client.close()


def test_idle_time_is_not_inactivity():
    client, _server, closed = _client(0.3)
    try:
        time.sleep(0.9)  # nothing pending: the clock is not running
        client.remove("/srv/x")
        assert not closed.is_set()
    finally:
        client.close()


def test_slow_but_answering_server_is_kept():
    client, server, closed = _client(0.5)
    try:
        server.delay = 0.3
        for _ in range(4):  # 1.2 s in all, every reply within the timeout
            client.remove("/srv/x")
        assert not closed.is_set()
    finally:
        client.close()


def test_server_side_copy_may_take_longer_than_the_timeout():
    client, server, closed = _client(0.3, extensions={"copy-data": b"1"})
    try:
        server.copy_delay = 1.0
        client.copy_data(b"src", b"dst")
        assert not closed.is_set()
        assert client.stalled_for() == 0.0
    finally:
        client.close()


def test_abort_stalled_wakes_a_blocked_request_with_a_timeout():
    client, server, closed = _client(None)  # no watchdog
    try:
        server.silent.set()
        errors = []

        def _call():
            try:
                client.remove("/srv/x")
            except Exception as exc:  # noqa: BLE001 - captured for the assert
                errors.append(exc)

        caller = threading.Thread(target=_call)
        caller.start()
        deadline = time.monotonic() + 2
        while client.stalled_for() < 0.2 and time.monotonic() < deadline:
            time.sleep(0.02)
        client.abort_stalled()
        caller.join(2)
        assert not caller.is_alive()
        assert isinstance(errors[0], proto.SFTPConnectionTimeout)
        assert closed.is_set()
    finally:
        client.close()
