"""Prove the OpenSSH SFTP file manager works with YubiKey-style agent auth.

A YubiKey used for SSH is almost always mediated by an ssh-agent (yubikey-agent,
gpg-agent, the OpenSSH agent after ``ssh-add -s`` PKCS#11, 1Password, etc.).
The private key never leaves the token; ssh only sees ``SSH_AUTH_SOCK`` /
``IdentityAgent``. The SFTP file manager must therefore:

  1. reuse the native ``build_ssh_connection`` + ``resolve_native_auth`` path,
  2. leave the agent intact (never ``IdentityAgent=none``, never drop
     ``SSH_AUTH_SOCK``),
  3. keep hardware directives (PKCS11Provider / SecurityKeyProvider /
     IdentityAgent) in ``~/.ssh/config`` rather than on the CLI,
  4. authenticate over pipes with *only* an agent-held key — no IdentityFile,
     no password, no askpass — the same posture as a YubiKey already loaded
     into the agent.

This module covers (1)–(3) with unit tests and (4) with a live ``ssh -s sftp``
session against a paramiko mock server, using a software ed25519 key loaded
into a throwaway ssh-agent as the YubiKey stand-in.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import socket
import stat as statmod
import subprocess
import threading
import time
import types
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

paramiko = pytest.importorskip("paramiko")

from tests._fm_harness import _load_file_manager_module


USERNAME = "yubi"
HOST_ALIAS = "YubiKeySFTP"

_REQUIRED_BINS = ["ssh", "ssh-keygen", "ssh-add", "ssh-agent"]
_missing = [b for b in _REQUIRED_BINS if shutil.which(b) is None]
requires_ssh_tools = pytest.mark.skipif(
    bool(_missing), reason=f"missing binaries: {_missing}"
)


# ---------------------------------------------------------------------------
# Unit: argv / env / config posture (always runs)
# ---------------------------------------------------------------------------


def _stub_prepared(command, env=None, use_sshpass=False, password=None):
    return types.SimpleNamespace(
        command=command,
        env=env or {},
        use_sshpass=use_sshpass,
        password=password,
        use_askpass=False,
    )


def _manager(monkeypatch, connection=None):
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob

    conn = connection or types.SimpleNamespace(nickname=HOST_ALIAS)
    return ob.OpenSSHSFTPManager(
        "host", USERNAME, 22, connection=conn
    )


def test_sftp_build_argv_preserves_agent_sock(monkeypatch):
    """YubiKey via agent requires SSH_AUTH_SOCK to reach the ssh child."""
    import sshpilot.ssh_connection_builder as scb

    agent_sock = "/tmp/yubikey-agent.sock"
    monkeypatch.setenv("SSH_AUTH_SOCK", agent_sock)
    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(
            ["ssh", "-F", "/cfg", "-s", HOST_ALIAS, "sftp"],
            env={"SSH_AUTH_SOCK": agent_sock, "SSH_ASKPASS_REQUIRE": "never"},
        ),
    )

    manager = _manager(monkeypatch)
    argv, env, cleanup = manager._build_argv()

    assert env["SSH_AUTH_SOCK"] == agent_sock
    joined = " ".join(argv).lower()
    assert "identityagent=none" not in joined
    assert "-o" not in argv or "IdentityAgent=none" not in argv
    assert cleanup is None
    manager.close()


def test_sftp_build_argv_inherits_os_agent_sock_when_prepared_env_omits_it(monkeypatch):
    """prepared.env overlays os.environ; an agent sock already in the process
    environment must survive even when resolve_native_auth does not re-set it."""
    import sshpilot.ssh_connection_builder as scb

    agent_sock = "/run/user/1000/yubikey-agent/sock"
    monkeypatch.setenv("SSH_AUTH_SOCK", agent_sock)
    monkeypatch.setattr(
        scb,
        "build_ssh_connection",
        lambda ctx: _stub_prepared(
            ["ssh", "-s", HOST_ALIAS, "sftp"],
            env={"SSH_ASKPASS_REQUIRE": "never"},  # deliberately no sock
        ),
    )

    manager = _manager(monkeypatch)
    _argv, env, _cleanup = manager._build_argv()
    assert env["SSH_AUTH_SOCK"] == agent_sock
    manager.close()


def test_sftp_resolve_native_auth_keeps_agent_for_key_auth(monkeypatch):
    """Nothing-saved key auth (typical YubiKey-in-agent) must not drop the sock
    and must not enable sshpass/askpass that would fight the agent."""
    from sshpilot.ssh_connection_builder import resolve_native_auth

    agent_sock = "/tmp/yk.sock"
    monkeypatch.setenv("SSH_AUTH_SOCK", agent_sock)
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.lookup_passphrase",
        lambda _p: "",
    )

    conn = SimpleNamespace(
        auth_method=0,
        resolved_identity_files=[],
        password=None,
        hostname="example.com",
        host="example.com",
        username=USERNAME,
    )
    cm = SimpleNamespace(get_password=lambda host, user: None)
    auth = resolve_native_auth(conn, cm, app_config=None)

    assert auth.env.get("SSH_AUTH_SOCK") == agent_sock
    assert auth.use_sshpass is False
    assert auth.use_askpass is False
    assert "IdentityAgent=none" not in " ".join(auth.extra_opts)


def test_sftp_native_command_stays_minimal_with_hardware_config(tmp_path, monkeypatch):
    """PKCS11 / FIDO / IdentityAgent live in ssh_config; SFTP argv stays
    ``ssh -F <cfg> -s <host> sftp`` so OpenSSH (not the app) loads the token."""
    from sshpilot.ssh_connection_builder import ConnectionContext, build_ssh_connection

    cfg = tmp_path / "config"
    cfg.write_text(
        f"Host {HOST_ALIAS}\n"
        f"    HostName 127.0.0.1\n"
        f"    User {USERNAME}\n"
        f"    IdentityAgent /tmp/yubikey-agent.sock\n"
        f"    PKCS11Provider /usr/lib/libykcs11.so\n"
        f"    SecurityKeyProvider /usr/lib/libcbor.so\n"
    )

    conn = SimpleNamespace(
        nickname=HOST_ALIAS,
        host=HOST_ALIAS,
        hostname="127.0.0.1",
        username=USERNAME,
        auth_method=0,
        resolved_identity_files=[],
        password=None,
        _resolve_config_override_path=lambda: str(cfg),
    )
    cm = SimpleNamespace(get_password=lambda host, user: None)
    app_config = SimpleNamespace(
        get_setting=lambda k, d=None: d,
        get_ssh_config=lambda: {"ssh_overrides": []},
    )
    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.lookup_passphrase",
        lambda _p: "",
    )

    prepared = build_ssh_connection(
        ConnectionContext(
            connection=conn,
            connection_manager=cm,
            config=app_config,
            command_type="ssh",
            native_mode=True,
            extra_args=["-s"],
            remote_command="sftp",
        )
    )
    argv = prepared.command
    assert argv[0] == "ssh"
    assert "-F" in argv and str(cfg) in argv
    assert argv[-3:] == ["-s", HOST_ALIAS, "sftp"]
    # Hardware options must NOT be duplicated onto the CLI.
    flat = " ".join(argv).lower()
    assert "pkcs11" not in flat
    assert "securitykey" not in flat
    assert "identityagent=none" not in flat
    # But they must be present for OpenSSH to read from -F.
    text = cfg.read_text()
    assert "IdentityAgent /tmp/yubikey-agent.sock" in text
    assert "PKCS11Provider /usr/lib/libykcs11.so" in text
    assert "SecurityKeyProvider /usr/lib/libcbor.so" in text


def test_sftp_manager_build_argv_uses_shared_native_path(monkeypatch):
    """File manager must call build_ssh_connection (not a private argv builder)."""
    import sshpilot.ssh_connection_builder as scb

    captured = {}

    def fake_build(ctx):
        captured["ctx"] = ctx
        return _stub_prepared(["ssh", "-s", HOST_ALIAS, "sftp"])

    monkeypatch.setattr(scb, "build_ssh_connection", fake_build)
    manager = _manager(monkeypatch)
    manager._build_argv()
    assert captured["ctx"].native_mode is True
    assert captured["ctx"].extra_args == ["-s"]
    assert captured["ctx"].remote_command == "sftp"
    manager.close()


# ---------------------------------------------------------------------------
# Integration: live ssh -s sftp authenticated only via ssh-agent
# ---------------------------------------------------------------------------


class _AgentSFTPInterface(paramiko.SFTPServerInterface):
    """Minimal in-memory SFTP root used to prove the handshake + list works."""

    HOME = "/home/yubi"
    FILES = {
        "/": ("dir", None),
        "/home": ("dir", None),
        "/home/yubi": ("dir", None),
        "/home/yubi/from-yubikey.txt": ("file", b"agent-auth-ok\n"),
        "/home/yubi/docs": ("dir", None),
    }

    def _attrs(self, path: str) -> paramiko.SFTPAttributes:
        kind, data = self.FILES[path]
        attrs = paramiko.SFTPAttributes()
        if kind == "dir":
            attrs.st_mode = statmod.S_IFDIR | 0o755
            attrs.st_size = 0
        else:
            attrs.st_mode = statmod.S_IFREG | 0o644
            attrs.st_size = len(data)
        attrs.st_uid = 1000
        attrs.st_gid = 1000
        attrs.st_atime = attrs.st_mtime = 1_700_000_000
        attrs.filename = path.rsplit("/", 1)[-1] or "/"
        return attrs

    def canonicalize(self, path):
        if not path or path in (".", "./"):
            return self.HOME
        if path == "~":
            return self.HOME
        return path

    def list_folder(self, path):
        path = self.canonicalize(path)
        if self.FILES.get(path, (None,))[0] != "dir":
            return paramiko.SFTP_NO_SUCH_FILE
        prefix = path.rstrip("/") + "/"
        out = []
        for full, (kind, _data) in self.FILES.items():
            if full == path or not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            if "/" in rest:
                continue
            attrs = self._attrs(full)
            attrs.filename = rest
            out.append(attrs)
        return out

    def stat(self, path):
        path = self.canonicalize(path)
        if path not in self.FILES:
            return paramiko.SFTP_NO_SUCH_FILE
        return self._attrs(path)

    def lstat(self, path):
        return self.stat(path)

    def open(self, path, flags, attr):
        path = self.canonicalize(path)
        entry = self.FILES.get(path)
        if entry is None or entry[0] != "file":
            return paramiko.SFTP_NO_SUCH_FILE
        handle = paramiko.SFTPHandle(flags)
        handle.readfile = io.BytesIO(entry[1])
        return handle


class _PubkeyOnlySFTPServer(paramiko.ServerInterface):
    """Publickey-only server. Do NOT override ``check_channel_subsystem_request``:
    paramiko's default implementation is what actually starts the registered
    SFTP subsystem handler thread."""

    def __init__(self, authorized_blobs):
        self._authorized = set(authorized_blobs)

    def get_allowed_auths(self, username):
        return "publickey"

    def check_auth_publickey(self, username, key):
        if key.asbytes() in self._authorized:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def _handle_sftp_conn(client_sock, host_key, authorized_blobs):
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(host_key)
        transport.set_subsystem_handler(
            "sftp", paramiko.SFTPServer, _AgentSFTPInterface
        )
        transport.start_server(server=_PubkeyOnlySFTPServer(authorized_blobs))
        # Do NOT call transport.accept(): that steals the session channel from
        # the SFTP subsystem handler and makes ssh see a broken pipe. Just keep
        # the transport alive while OpenSSH speaks SFTP over the subsystem.
        deadline = time.time() + 30
        while transport.is_active() and time.time() < deadline:
            time.sleep(0.05)
        transport.close()
    except Exception:
        pass


@contextmanager
def _pubkey_sftp_server(authorized_blobs):
    host_key = paramiko.RSAKey.generate(2048)
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(8)
    port = listen.getsockname()[1]
    stop = threading.Event()

    def _serve():
        listen.settimeout(0.5)
        while not stop.is_set():
            try:
                client, _ = listen.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=_handle_sftp_conn,
                args=(client, host_key, authorized_blobs),
                daemon=True,
            ).start()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(port=port)
    finally:
        stop.set()
        try:
            listen.close()
        except Exception:
            pass
        thread.join(timeout=2)


@contextmanager
def _throwaway_agent():
    if shutil.which("ssh-agent") is None:
        pytest.skip("ssh-agent missing")
    out = subprocess.check_output(["ssh-agent", "-s"], text=True)
    sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", out).group(1)
    pid = re.search(r"SSH_AGENT_PID=(\d+);", out).group(1)

    def add(key_path: Path):
        env = os.environ.copy()
        env["SSH_AUTH_SOCK"] = sock
        env.pop("SSH_ASKPASS", None)
        env["SSH_ASKPASS_REQUIRE"] = "never"
        proc = subprocess.run(
            ["ssh-add", str(key_path)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"ssh-add failed: {proc.stderr}")

    try:
        yield SimpleNamespace(sock=sock, add=add)
    finally:
        try:
            subprocess.run(
                ["ssh-agent", "-k"],
                env={**os.environ, "SSH_AGENT_PID": pid},
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass


def _keygen(path: Path):
    subprocess.run(
        [
            "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
            "-f", str(path), "-C", "yubikey-sftp-standin",
        ],
        check=True,
        capture_output=True,
    )


@requires_ssh_tools
def test_sftp_file_manager_authenticates_via_agent_only(tmp_path, monkeypatch):
    """End-to-end: agent-held key (YubiKey stand-in) → OpenSSHSFTPManager list.

    No IdentityFile on the Host, no password, no askpass — auth succeeds only
    because ``SSH_AUTH_SOCK`` reaches the ``ssh -s sftp`` child that the file
    manager spawns through the shared native builder.
    """
    key_path = tmp_path / "yk_standin"
    _keygen(key_path)
    pub = paramiko.Ed25519Key.from_private_key_file(str(key_path))

    with _throwaway_agent() as agent, _pubkey_sftp_server([pub.asbytes()]) as server:
        agent.add(key_path)

        cfg = tmp_path / "ssh_config"
        cfg.write_text(
            f"Host {HOST_ALIAS}\n"
            f"    HostName 127.0.0.1\n"
            f"    Port {server.port}\n"
            f"    User {USERNAME}\n"
            f"    IdentitiesOnly no\n"
            f"    PreferredAuthentications publickey\n"
            f"    PubkeyAuthentication yes\n"
            f"    PasswordAuthentication no\n"
            f"    KbdInteractiveAuthentication no\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    GlobalKnownHostsFile /dev/null\n"
            # Explicitly no IdentityFile — the agent is the only key source,
            # matching a YubiKey already loaded into ssh-agent.
        )

        conn = SimpleNamespace(
            nickname=HOST_ALIAS,
            host=HOST_ALIAS,
            hostname="127.0.0.1",
            username=USERNAME,
            auth_method=0,
            resolved_identity_files=[],  # nothing on disk for the resolver
            password=None,
            _resolve_config_override_path=lambda: str(cfg),
        )
        cm = SimpleNamespace(get_password=lambda host, user: None)
        app_config = SimpleNamespace(
            get_setting=lambda k, d=None: True if k == "use-askpass" else d,
            get_ssh_config=lambda: {
                "ssh_overrides": ["-o", "ConnectTimeout=8"],
            },
        )

        monkeypatch.setenv("SSH_AUTH_SOCK", agent.sock)
        # Ensure no leftover askpass can intercept; YubiKey/agent path relies
        # on the agent alone when nothing is stored in the keyring.
        monkeypatch.delenv("SSH_ASKPASS", raising=False)
        monkeypatch.setattr(
            "sshpilot.ssh_connection_builder.lookup_passphrase",
            lambda _p: "",
        )

        _load_file_manager_module(monkeypatch)
        import sshpilot.file_manager.openssh_backend as ob
        import sshpilot.config as config_mod

        monkeypatch.setattr(config_mod, "Config", lambda: app_config)

        manager = ob.OpenSSHSFTPManager(
            "127.0.0.1",
            USERNAME,
            server.port,
            connection=conn,
            connection_manager=cm,
            dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
        )
        try:
            # Drive the real connect path (same as connect_to_server, sync).
            manager._connect_impl()
            assert manager.is_connected()

            # Prove the argv that was used preserved the agent and stayed native.
            argv, env, cleanup = manager._build_argv()
            assert env.get("SSH_AUTH_SOCK") == agent.sock
            assert "IdentityAgent=none" not in " ".join(argv)
            assert argv[-3:] == ["-s", HOST_ALIAS, "sftp"]
            assert "-i" not in argv  # no IdentityFile on the CLI
            assert cleanup is None

            names = sorted(
                a.filename for a in manager._client.listdir_attr("/home/yubi")
            )
            assert names == ["docs", "from-yubikey.txt"]
            data = manager._client.open("/home/yubi/from-yubikey.txt", "r").read()
            assert data == b"agent-auth-ok\n"
        finally:
            manager.close()


@requires_ssh_tools
def test_sftp_file_manager_fails_without_agent_sock(tmp_path, monkeypatch):
    """Negative control: same Host/key, but no SSH_AUTH_SOCK → auth fails.

    Confirms the positive test really depended on the agent (YubiKey) path.
    """
    key_path = tmp_path / "yk_standin"
    _keygen(key_path)
    pub = paramiko.Ed25519Key.from_private_key_file(str(key_path))

    with _pubkey_sftp_server([pub.asbytes()]) as server:
        cfg = tmp_path / "ssh_config"
        cfg.write_text(
            f"Host {HOST_ALIAS}\n"
            f"    HostName 127.0.0.1\n"
            f"    Port {server.port}\n"
            f"    User {USERNAME}\n"
            f"    IdentitiesOnly no\n"
            f"    PreferredAuthentications publickey\n"
            f"    PubkeyAuthentication yes\n"
            f"    PasswordAuthentication no\n"
            f"    KbdInteractiveAuthentication no\n"
            f"    StrictHostKeyChecking no\n"
            f"    UserKnownHostsFile /dev/null\n"
            f"    GlobalKnownHostsFile /dev/null\n"
        )

        conn = SimpleNamespace(
            nickname=HOST_ALIAS,
            host=HOST_ALIAS,
            hostname="127.0.0.1",
            username=USERNAME,
            auth_method=0,
            resolved_identity_files=[],
            password=None,
            _resolve_config_override_path=lambda: str(cfg),
        )
        cm = SimpleNamespace(get_password=lambda host, user: None)
        app_config = SimpleNamespace(
            get_setting=lambda k, d=None: True if k == "use-askpass" else d,
            get_ssh_config=lambda: {
                "ssh_overrides": ["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"],
            },
        )

        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        monkeypatch.delenv("SSH_ASKPASS", raising=False)
        monkeypatch.setattr(
            "sshpilot.ssh_connection_builder.lookup_passphrase",
            lambda _p: "",
        )

        _load_file_manager_module(monkeypatch)
        import sshpilot.file_manager.openssh_backend as ob
        import sshpilot.config as config_mod

        monkeypatch.setattr(config_mod, "Config", lambda: app_config)

        manager = ob.OpenSSHSFTPManager(
            "127.0.0.1",
            USERNAME,
            server.port,
            connection=conn,
            connection_manager=cm,
            dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
        )
        try:
            with pytest.raises(Exception):
                manager._connect_impl()
            assert not manager.is_connected()
        finally:
            manager.close()
