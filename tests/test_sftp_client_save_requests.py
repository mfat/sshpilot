"""Wire-level coverage for the SFTP requests a remote file save relies on.

The daemon's save sets the new file's mode in the OPEN request and backs the
old file up with the ``hardlink@openssh.com`` extension, each saving round
trips on high-latency links. Runs against ``sshpilot.sftp`` only.
"""

from __future__ import annotations

import socket
import threading

import pytest

from sshpilot.sftp import client as sftp_client
from sshpilot.sftp import protocol as proto


class _FakeServer(threading.Thread):
    def __init__(self, stream_r, stream_w, *, extensions) -> None:
        super().__init__(daemon=True)
        self._r = stream_r
        self._w = stream_w
        self._extensions = extensions
        self.opens: list[tuple[str, proto.SFTPAttributes]] = []
        self.extended: list[tuple[str, str, str]] = []

    def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self._r.read(n - len(data))
            if not chunk:
                raise EOFError
            data += chunk
        return data

    def _read_packet(self):
        length = int.from_bytes(self._read_exact(4), "big")
        body = self._read_exact(length)
        return body[0], body[1:]

    def _send(self, ptype: int, payload: bytes) -> None:
        self._w.write(proto.build_packet(ptype, payload))
        self._w.flush()

    def _status(self, rid: int, code: int) -> None:
        self._send(
            proto.FXP_STATUS,
            proto.pack_uint32(rid)
            + proto.pack_uint32(code)
            + proto.pack_string("")
            + proto.pack_string(""),
        )

    def run(self) -> None:
        try:
            self._read_packet()  # INIT
            version = proto.pack_uint32(proto.PROTOCOL_VERSION)
            for name, value in self._extensions.items():
                version += proto.pack_string(name) + proto.pack_string(value)
            self._send(proto.FXP_VERSION, version)
            while True:
                ptype, payload = self._read_packet()
                reader = proto._Reader(payload)
                rid = reader.uint32()
                if ptype == proto.FXP_OPEN:
                    path = reader.text()
                    reader.uint32()  # pflags
                    self.opens.append((path, proto.decode_attrs(reader)))
                    self._send(
                        proto.FXP_HANDLE,
                        proto.pack_uint32(rid) + proto.pack_string(b"h1"),
                    )
                elif ptype == proto.FXP_EXTENDED:
                    self.extended.append((reader.text(), reader.text(), reader.text()))
                    self._status(rid, proto.FX_OK)
                else:
                    self._status(rid, proto.FX_OK)
        except (EOFError, OSError, ValueError):
            return


def _make_client(extensions):
    csock, ssock = socket.socketpair()

    def _teardown():
        for sock in (csock, ssock):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    client = sftp_client.OpenSSHSFTPClient(
        csock.makefile("wb"), csock.makefile("rb"), on_close=_teardown
    )
    server = _FakeServer(ssock.makefile("rb"), ssock.makefile("wb"), extensions=extensions)
    server.start()
    client.start()
    return client, server


def test_create_mode_travels_with_the_open_request():
    client, server = _make_client({})
    try:
        with client.file("/srv/.env.tmp", "wb", create_mode=0o600) as handle:
            handle.write(b"A=1\n")
        with client.file("/srv/plain", "wb"):
            pass
    finally:
        client.close()

    (path, attrs), (_, plain_attrs) = server.opens
    assert path == "/srv/.env.tmp"
    assert attrs.st_mode & 0o7777 == 0o600
    assert not plain_attrs.st_mode


@pytest.mark.parametrize(
    ("extensions", "supported"),
    [({"hardlink@openssh.com": b"1"}, True), ({}, False)],
)
def test_hardlink_support_comes_from_the_server_version(extensions, supported):
    client, _server = _make_client(extensions)
    try:
        assert client.supports_hardlink() is supported
    finally:
        client.close()


def test_hardlink_sends_the_openssh_extension():
    client, server = _make_client({"hardlink@openssh.com": b"1"})
    try:
        client.hardlink("/srv/.env", "/srv/.env.bak-1")
    finally:
        client.close()

    assert server.extended == [("hardlink@openssh.com", "/srv/.env", "/srv/.env.bak-1")]
