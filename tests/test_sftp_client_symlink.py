"""Symlink/readlink coverage for the GTK-free ``sshpilot.sftp.client``.

Runs entirely against ``sshpilot.sftp`` (no ``gi`` stub required) so it can
exercise the extracted client the daemon imports directly.
"""

from __future__ import annotations

import socket
import threading

from sshpilot.sftp import client as sftp_client
from sshpilot.sftp import protocol as proto


class _FakeSymlinkServer(threading.Thread):
    """Minimal SFTP v3 server that only understands SYMLINK/READLINK/REALPATH."""

    def __init__(self, stream_r, stream_w) -> None:
        super().__init__(daemon=True)
        self._r = stream_r
        self._w = stream_w
        self.links: dict[str, str] = {}
        self.symlink_calls: list[tuple[bytes, bytes]] = []

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
            ptype, _ = self._read_packet()
            assert ptype == proto.FXP_INIT
            self._send(proto.FXP_VERSION, proto.pack_uint32(proto.PROTOCOL_VERSION))
            while True:
                ptype, payload = self._read_packet()
                reader = proto._Reader(payload)
                rid = reader.uint32()
                if ptype == proto.FXP_SYMLINK:
                    # Record the raw wire order so the test can assert OpenSSH's
                    # swapped (targetpath, linkpath) order was used.
                    first = reader.string()
                    second = reader.string()
                    self.symlink_calls.append((first, second))
                    target_path, link_path = first.decode(), second.decode()
                    self.links[link_path] = target_path
                    self._status(rid, proto.FX_OK)
                elif ptype == proto.FXP_READLINK:
                    path = reader.text()
                    target = self.links.get(path)
                    if target is None:
                        self._status(rid, proto.FX_NO_SUCH_FILE)
                        continue
                    self._send(
                        proto.FXP_NAME,
                        proto.pack_uint32(rid)
                        + proto.pack_uint32(1)
                        + proto.pack_string(target)
                        + proto.pack_string(target)
                        + proto.encode_attrs(None),
                    )
                else:
                    self._status(rid, proto.FX_OP_UNSUPPORTED)
        except (EOFError, OSError, ValueError):
            return


def _make_client():
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
    server = _FakeSymlinkServer(ssock.makefile("rb"), ssock.makefile("wb"))
    server.start()
    client.start()
    return client, server


def test_symlink_sends_openssh_swapped_wire_order():
    client, server = _make_client()
    client.symlink("/targets/real.txt", "/links/alias.txt")
    assert server.symlink_calls == [(b"/targets/real.txt", b"/links/alias.txt")]
    client.close()


def test_readlink_returns_target():
    client, server = _make_client()
    client.symlink("/targets/real.txt", "/links/alias.txt")
    assert client.readlink("/links/alias.txt") == "/targets/real.txt"
    client.close()


def test_readlink_missing_raises_enoent():
    import errno

    client, _server = _make_client()
    try:
        client.readlink("/links/missing")
        raise AssertionError("expected SFTPError")
    except proto.SFTPError as exc:
        assert exc.errno == errno.ENOENT
    finally:
        client.close()
