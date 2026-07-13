"""Tests for the pure OpenSSH SFTP backend: the protocol client driven against
an in-memory SFTP v3 server (over a socketpair), plus the manager's op mapping.
"""

import socket
import threading
import time
import types

import pytest

from tests._fm_harness import _load_file_manager_module


# ---------------------------------------------------------------------------
# A minimal in-memory SFTP v3 server for the client to talk to.
# ---------------------------------------------------------------------------


class _FakeSFTPServer(threading.Thread):
    def __init__(self, proto, stream_r, stream_w):
        super().__init__(daemon=True)
        self.p = proto
        self._r = stream_r
        self._w = stream_w
        # In-memory FS: path -> ('dir', None) or ('file', bytearray)
        self.fs = {
            "/": ("dir", None),
            "/home": ("dir", None),
            "/home/alice": ("dir", None),
            "/home/alice/notes.txt": ("file", bytearray(b"hello world")),
            "/home/alice/sub": ("dir", None),
            "/home/alice/sub/inner.txt": ("file", bytearray(b"inner")),
        }
        self._handles = {}
        self._hseq = 0

    def _read_exact(self, n):
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

    def _send(self, ptype, payload):
        self._w.write(self.p.build_packet(ptype, payload))
        self._w.flush()

    def _status(self, rid, code):
        self._send(
            self.p.FXP_STATUS,
            self.p.pack_uint32(rid) + self.p.pack_uint32(code)
            + self.p.pack_string("") + self.p.pack_string(""),
        )

    def _attrs_for(self, path):
        kind, data = self.fs[path]
        a = self.p.SFTPAttributes()
        if kind == "dir":
            a.st_mode = 0o040755
            a.st_size = 0
        else:
            a.st_mode = 0o100644
            a.st_size = len(data)
        a.st_mtime = 1000
        return a

    def run(self):
        p = self.p
        try:
            # Handshake.
            ptype, _ = self._read_packet()
            assert ptype == p.FXP_INIT
            self._send(p.FXP_VERSION, p.pack_uint32(p.PROTOCOL_VERSION))
            while True:
                ptype, payload = self._read_packet()
                r = p._Reader(payload)
                rid = r.uint32()
                if ptype == p.FXP_REALPATH:
                    path = r.text() or "."
                    real = "/home/alice" if path == "." else path
                    self._send(
                        p.FXP_NAME,
                        p.pack_uint32(rid) + p.pack_uint32(1)
                        + p.pack_string(real) + p.pack_string(real)
                        + p.encode_attrs(self._attrs_for(real) if real in self.fs else p.SFTPAttributes()),
                    )
                elif ptype in (p.FXP_STAT, p.FXP_LSTAT):
                    path = r.text()
                    if path in self.fs:
                        self._send(p.FXP_ATTRS, p.pack_uint32(rid) + p.encode_attrs(self._attrs_for(path)))
                    else:
                        self._status(rid, p.FX_NO_SUCH_FILE)
                elif ptype == p.FXP_OPENDIR:
                    path = r.text()
                    if self.fs.get(path, (None,))[0] != "dir":
                        self._status(rid, p.FX_NO_SUCH_FILE)
                        continue
                    self._hseq += 1
                    handle = f"d{self._hseq}".encode()
                    children = sorted(
                        name[len(path):].lstrip("/")
                        for name in self.fs
                        if name != path
                        and name.startswith(path.rstrip("/") + "/")
                        and "/" not in name[len(path.rstrip("/")) + 1:]
                    )
                    self._handles[handle] = {"kind": "dir", "children": children, "done": False}
                    self._send(p.FXP_HANDLE, p.pack_uint32(rid) + p.pack_string(handle))
                elif ptype == p.FXP_READDIR:
                    handle = r.string()
                    state = self._handles.get(handle)
                    if not state or state["done"]:
                        self._status(rid, p.FX_EOF)
                        continue
                    state["done"] = True
                    names = state["children"]
                    body = p.pack_uint32(rid) + p.pack_uint32(len(names))
                    for name in names:
                        full = handle  # unused
                        child_path = None
                        # reconstruct child path from any matching fs entry
                        for cand in self.fs:
                            if cand.endswith("/" + name) or cand == name:
                                child_path = cand
                        attr = self._attrs_for(child_path) if child_path else p.SFTPAttributes()
                        body += p.pack_string(name) + p.pack_string(name) + p.encode_attrs(attr)
                    self._send(p.FXP_NAME, body)
                elif ptype == p.FXP_OPEN:
                    path = r.text()
                    pflags = r.uint32()
                    self._hseq += 1
                    handle = f"f{self._hseq}".encode()
                    if pflags & p.FXF_CREAT:
                        if (pflags & p.FXF_EXCL) and path in self.fs:
                            self._status(rid, p.FX_FAILURE)
                            continue
                        self.fs[path] = ("file", bytearray())
                    elif path not in self.fs:
                        self._status(rid, p.FX_NO_SUCH_FILE)
                        continue
                    self._handles[handle] = {"kind": "file", "path": path}
                    self._send(p.FXP_HANDLE, p.pack_uint32(rid) + p.pack_string(handle))
                elif ptype == p.FXP_READ:
                    handle = r.string()
                    offset = r.uint64()
                    length = r.uint32()
                    path = self._handles[handle]["path"]
                    data = self.fs[path][1]
                    chunk = bytes(data[offset:offset + length])
                    if not chunk:
                        self._status(rid, p.FX_EOF)
                    else:
                        self._send(p.FXP_DATA, p.pack_uint32(rid) + p.pack_string(chunk))
                elif ptype == p.FXP_WRITE:
                    handle = r.string()
                    offset = r.uint64()
                    data = r.string()
                    path = self._handles[handle]["path"]
                    buf = self.fs[path][1]
                    if len(buf) < offset + len(data):
                        buf.extend(b"\x00" * (offset + len(data) - len(buf)))
                    buf[offset:offset + len(data)] = data
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_CLOSE:
                    handle = r.string()
                    self._handles.pop(handle, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_MKDIR:
                    path = r.text()
                    self.fs[path] = ("dir", None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_REMOVE:
                    path = r.text()
                    self.fs.pop(path, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_RMDIR:
                    path = r.text()
                    self.fs.pop(path, None)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_RENAME:
                    old = r.text()
                    new = r.text()
                    self.fs[new] = self.fs.pop(old)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_SETSTAT:
                    r.text()  # path (mode ignored by this fake)
                    self._status(rid, p.FX_OK)
                elif ptype == p.FXP_EXTENDED:
                    name = r.text()
                    if name == "posix-rename@openssh.com":
                        old = r.text()
                        new = r.text()
                        self.fs[new] = self.fs.pop(old)  # overwrite allowed
                        self._status(rid, p.FX_OK)
                    else:
                        self._status(rid, p.FX_OP_UNSUPPORTED)
                else:
                    self._status(rid, p.FX_OP_UNSUPPORTED)
        except (EOFError, OSError, ValueError):
            return


@pytest.fixture
def backend_modules(monkeypatch):
    module = _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob
    import sshpilot.file_manager.sftp_protocol as proto
    return module, ob, proto


def _make_client(ob, proto):
    csock, ssock = socket.socketpair()

    def _teardown():
        # EOF both ends so the client reader and the server thread both unblock.
        for s in (csock, ssock):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()

    client = ob.OpenSSHSFTPClient(
        csock.makefile("wb"), csock.makefile("rb"), on_close=_teardown
    )
    server = _FakeSFTPServer(proto, ssock.makefile("rb"), ssock.makefile("wb"))
    server.start()
    client.start()
    return client, server


def test_client_realpath_and_listdir(backend_modules):
    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)
    assert client.realpath(".") == "/home/alice"
    names = sorted(a.filename for a in client.listdir_attr("/home/alice"))
    assert names == ["notes.txt", "sub"]
    attrs = {a.filename: a for a in client.listdir_attr("/home/alice")}
    assert attrs["sub"].is_dir() is True
    assert attrs["notes.txt"].is_dir() is False
    client.close()


def test_client_read_write_roundtrip(backend_modules):
    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)
    # Read existing file.
    handle = client.open_handle("/home/alice/notes.txt", proto.FXF_READ)
    assert client.read(handle, 0, 100) == b"hello world"
    assert client.read(handle, 11, 100) == b""  # EOF
    client.close_handle(handle)
    # Write a new file.
    wh = client.open_handle("/home/alice/new.txt", proto.FXF_WRITE | proto.FXF_CREAT | proto.FXF_TRUNC)
    client.write(wh, 0, b"abc")
    client.close_handle(wh)
    assert bytes(server.fs["/home/alice/new.txt"][1]) == b"abc"
    client.close()


def test_client_open_file_shim_copy(backend_modules):
    """The paramiko-style open(path, mode) file object lets the window's remote
    copy/paste (src.open('rb').read() → dst.open('wb').write()) work unchanged."""
    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)
    # Replicate _copy_remote_file's loop.
    with client.open("/home/alice/notes.txt", "rb") as src, client.open(
        "/home/alice/copy.txt", "wb"
    ) as dst:
        while True:
            chunk = src.read(32768)
            if not chunk:
                break
            dst.write(chunk)
    assert bytes(server.fs["/home/alice/copy.txt"][1]) == b"hello world"
    # read() with no size returns to EOF.
    with client.open("/home/alice/notes.txt", "rb") as f:
        assert f.read() == b"hello world"
    client.close()


def test_client_mkdir_remove_rename(backend_modules):
    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)
    client.mkdir("/home/alice/d")
    assert server.fs["/home/alice/d"][0] == "dir"
    client.rename("/home/alice/notes.txt", "/home/alice/renamed.txt")
    assert "/home/alice/renamed.txt" in server.fs
    client.remove("/home/alice/renamed.txt")
    assert "/home/alice/renamed.txt" not in server.fs
    client.close()


def test_client_paramiko_surface_parity(backend_modules):
    """The OpenSSH client must expose the paramiko-SFTPClient methods other app
    code relies on (authorized_keys editor): file/normalize/mkdir(mode)/chmod/
    posix_rename."""
    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)

    assert client.normalize(".") == "/home/alice"  # alias for realpath
    client.mkdir("/home/alice/.ssh", 0o700)        # mode arg accepted
    assert server.fs["/home/alice/.ssh"][0] == "dir"
    client.chmod("/home/alice/.ssh", 0o700)        # SETSTAT, no error

    # file() is an alias for open(); write then read back.
    with client.file("/home/alice/.ssh/authorized_keys.tmp", "w") as fh:
        fh.write(b"ssh-ed25519 AAAA user@host\n")
    # posix_rename overwrites an existing destination (atomic install).
    server.fs["/home/alice/.ssh/authorized_keys"] = ("file", bytearray(b"old"))
    client.posix_rename(
        "/home/alice/.ssh/authorized_keys.tmp",
        "/home/alice/.ssh/authorized_keys",
    )
    assert bytes(server.fs["/home/alice/.ssh/authorized_keys"][1]) == b"ssh-ed25519 AAAA user@host\n"
    assert "/home/alice/.ssh/authorized_keys.tmp" not in server.fs
    client.close()


def test_client_stat_missing_raises_enoent(backend_modules):
    import errno

    _, ob, proto = backend_modules
    client, server = _make_client(ob, proto)
    with pytest.raises(proto.SFTPError) as exc:
        client.stat("/home/alice/missing")
    assert exc.value.errno == errno.ENOENT
    client.close()


# ---------------------------------------------------------------------------
# Manager-level: inject a connected client + synchronous dispatcher.
# ---------------------------------------------------------------------------


def _make_manager(ob, proto, monkeypatch):
    manager = ob.OpenSSHSFTPManager(
        "example.com",
        "alice",
        port=22,
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
    )
    client, server = _make_client(ob, proto)
    manager._client = client
    manager._home = "/home/alice"
    emitted = []
    monkeypatch.setattr(manager, "emit", lambda *a: emitted.append(a))
    return manager, server, emitted


def _wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.mark.parametrize(
    "stderr",
    [
        (
            "ash: /usr/libexec/sftp-server: not found\n"
            "Connection closed.\n"
            "Connection closed"
        ),
        (
            "debug1: Authentications that can continue: publickey,password\n"
            "debug1: Authentication succeeded (publickey).\n"
            "ash: /usr/libexec/sftp-server: not found\n"
            "Connection closed."
        ),
    ],
)
def test_manager_classifies_missing_sftp_as_connection_error(
    backend_modules, monkeypatch, stderr
):
    _, ob, _ = backend_modules
    from sshpilot.scp_utils import SFTP_UNAVAILABLE_MESSAGE

    manager = ob.OpenSSHSFTPManager("host", "user")
    emitted = []
    monkeypatch.setattr(manager, "emit", lambda *args: emitted.append(args))

    exc = manager._classify_handshake_failure(
        stderr, EOFError("SFTP stream closed")
    )
    manager._emit_connect_error(exc)

    assert type(exc) is OSError
    assert str(exc) == SFTP_UNAVAILABLE_MESSAGE
    assert emitted == [("connection-error", SFTP_UNAVAILABLE_MESSAGE)]
    manager.close()


def test_manager_classifies_permission_denied_as_authentication_error(
    backend_modules, monkeypatch
):
    _, ob, _ = backend_modules
    manager = ob.OpenSSHSFTPManager("host", "user")
    emitted = []
    monkeypatch.setattr(manager, "emit", lambda *args: emitted.append(args))
    stderr = "Permission denied (publickey,password)."

    exc = manager._classify_handshake_failure(
        stderr, EOFError("SFTP stream closed")
    )
    manager._emit_connect_error(exc)

    assert isinstance(exc, PermissionError)
    assert emitted == [("authentication-required", stderr)]
    manager.close()


def test_manager_routes_connect_errors_by_exception_type(
    backend_modules, monkeypatch
):
    _, ob, _ = backend_modules
    manager = ob.OpenSSHSFTPManager("host", "user")
    emitted = []
    monkeypatch.setattr(manager, "emit", lambda *args: emitted.append(args))

    manager._emit_connect_error(
        OSError("Authentication succeeded; SFTP subsystem failed")
    )

    assert emitted == [
        ("connection-error", "Authentication succeeded; SFTP subsystem failed")
    ]
    manager.close()


def test_manager_listdir_emits_directory_loaded(backend_modules, monkeypatch):
    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)

    manager.listdir("~")
    assert _wait_for(lambda: any(e[0] == "directory-loaded" for e in emitted))
    sig = next(e for e in emitted if e[0] == "directory-loaded")
    path, entries = sig[1], sig[2]
    assert path == "/home/alice"
    names = sorted(fe.name for fe in entries)
    assert names == ["notes.txt", "sub"]
    manager.close()


def test_manager_sftp_alias_and_is_connected(backend_modules, monkeypatch):
    """Window/dialog code probes manager._sftp (paramiko detail) for liveness and
    stat(); the OpenSSH backend must alias it to the live client."""
    _, ob, proto = backend_modules
    manager = ob.OpenSSHSFTPManager(
        "h", "u", 22,
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
    )
    # Not connected yet.
    assert manager._sftp is None
    assert manager.is_connected() is False

    client, server = _make_client(ob, proto)
    manager._client = client
    manager._home = "/home/alice"
    manager._proc = types.SimpleNamespace(poll=lambda: None)  # live subprocess

    assert manager._sftp is client
    assert manager.is_connected() is True
    # A dead subprocess means a stale session, even if the client object lingers.
    manager._proc = types.SimpleNamespace(poll=lambda: 1)
    assert manager.is_connected() is False
    manager._proc = types.SimpleNamespace(poll=lambda: None)
    # The aliased client exposes .stat() with st_mode (what PropertiesDialog reads).
    attr = manager._sftp.stat("/home/alice/notes.txt")
    assert attr.st_mode & 0o170000 == 0o100000  # regular file
    manager.close()


def test_manager_directory_size(backend_modules, monkeypatch):
    """directory_size recursively sums file bytes (notes.txt=11 + sub/inner.txt=5)."""
    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)
    total = manager.directory_size("/home/alice").result(timeout=5)
    assert total == len(b"hello world") + len(b"inner")
    manager.close()


def test_manager_mkdir_future(backend_modules, monkeypatch):
    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)
    fut = manager.mkdir("/home/alice/made")
    fut.result(timeout=3)
    assert server.fs["/home/alice/made"][0] == "dir"
    manager.close()


def test_manager_listdir_defers_counts(backend_modules, monkeypatch):
    """listdir emits directory-loaded immediately with no counts, then a
    background directory-counts pass reports each folder's child count."""
    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)

    manager.listdir("~")

    assert _wait_for(lambda: any(e[0] == "directory-loaded" for e in emitted))
    dl = next(e for e in emitted if e[0] == "directory-loaded")
    entries = dl[2]
    sub = next(fe for fe in entries if fe.name == "sub")
    assert sub.item_count is None  # not blocked on the count

    # The background pass reports sub's child count (it contains inner.txt → 1).
    assert _wait_for(lambda: any(e[0] == "directory-counts" for e in emitted))
    counts = {}
    for e in emitted:
        if e[0] == "directory-counts":
            counts.update(e[2])
    assert counts.get("sub") == 1
    manager.close()


def test_keepalive_emits_operation_error_after_failures(backend_modules, monkeypatch):
    """The keepalive worker probes the client and surfaces a 'connection may be
    down' error after exceeding the configured failure count."""
    _, ob, proto = backend_modules
    manager = ob.OpenSSHSFTPManager(
        "h", "u", 22,
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
        ssh_config={"file_manager": {"sftp_keepalive_interval": 1,
                                     "sftp_keepalive_count_max": 0}},
    )
    emitted = []
    monkeypatch.setattr(manager, "emit", lambda *a: emitted.append(a))

    class _DeadClient:
        def realpath(self, _p):
            raise IOError("probe failed")

    manager._client = _DeadClient()
    manager._read_keepalive_config()
    assert manager._keepalive_interval == 1
    # First probe fails → failures(1) > count_max(0) → emit "connection may be down".
    manager._start_keepalive_worker()
    assert _wait_for(lambda: any(e[0] == "operation-error" for e in emitted), timeout=4)
    manager.close()


def test_count_pass_abandons_stale_generation(backend_modules, monkeypatch):
    """A count pass tagged with an old generation emits nothing (the user
    navigated away)."""
    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)
    manager._client = _make_client(ob, proto)[0]
    manager._home = "/home/alice"

    manager._listdir_seq = 5  # current generation
    folders = [ob.FileEntry(name="sub", is_dir=True, size=0, modified=0)]
    manager._start_count_pass("/home/alice", folders, generation=1)  # stale
    time.sleep(0.3)
    assert not any(e[0] == "directory-counts" for e in emitted)
    manager.close()


def test_manager_upload_download_roundtrip(backend_modules, monkeypatch, tmp_path):
    import pathlib

    _, ob, proto = backend_modules
    manager, server, emitted = _make_manager(ob, proto, monkeypatch)

    local = tmp_path / "up.bin"
    local.write_bytes(b"x" * 70000)  # spans multiple chunks
    manager.upload(local, "/home/alice/up.bin").result(timeout=5)
    assert bytes(server.fs["/home/alice/up.bin"][1]) == b"x" * 70000

    dest = tmp_path / "down.bin"
    manager.download("/home/alice/up.bin", pathlib.Path(dest)).result(timeout=5)
    assert dest.read_bytes() == b"x" * 70000
    manager.close()
