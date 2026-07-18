"""Can / can't matrix: interactive auth vs the OpenSSH SFTP file manager.

The file manager speaks SFTP over pipes (``ssh -s <host> sftp`` via
``Popen`` stdin/stdout) — there is **no TTY**. That does **not** mean there
are no interactive prompts: the FM has two graphical prompt channels that
work without a PTY:

  * **SSH login password** — ``FileManagerWindow`` calls
    ``show_ssh_password_dialog`` before connect (password auth) and again on
    ``authentication-required`` retries.
  * **Key passphrase** — when ``resolve_native_auth`` wires askpass, ssh
    spawns our ``SSH_ASKPASS`` helper, which shows a GTK dialog (or routes
    through ``askpass_server`` → ``MainWindow.prompt_ssh_passphrase``).

What fails is a *native OpenSSH TTY prompt* with askpass stripped and no
dialog password on the manager.

=======  ===============================================================  ======
#        Scenario                                                         Result
=======  ===============================================================  ======
1        Agent-held key (YubiKey / ssh-add already done)                  CAN
2        Unencrypted IdentityFile in ssh_config                           CAN
3        Encrypted IdentityFile + stored passphrase (askpass autofill)    CAN
3b       Encrypted key + askpass GUI (builtin / askpass_server IPC)       CAN
4        Password auth + stored password (sshpass)                        CAN
4b       Password auth + graphical dialog password (sshpass)              CAN
5        Encrypted key, askpass stripped (nothing saved)                  CAN'T
6        Password auth, nothing stored, dialog cancelled / skipped        CAN'T
7        Askpass disabled + encrypted key needing passphrase              CAN'T
8        Key-based auth failure → UI password-dialog recovery             CAN'T
9        Password auth failure → UI password-dialog recovery              CAN
=======  ===============================================================  ======

Layers:
  * Decision — resolver wiring + UI dialog gates (always runs).
  * Graphical prompts — prove password/passphrase GUI channels exist.
  * Live — real ``OpenSSHSFTPManager._connect_impl()`` against a paramiko
    SFTP server (skipped when ssh tools are missing).
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
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

paramiko = pytest.importorskip("paramiko")

from tests._fm_harness import _load_file_manager_module


USERNAME = "fmuser"
HOST_ALIAS = "FMAuthMatrix"
PASSPHRASE = "secretpass"
PASSWORD = "hunter2"

_REQUIRED_BINS = ["ssh", "ssh-keygen", "ssh-add", "ssh-agent", "sshpass"]
_missing = [b for b in _REQUIRED_BINS if shutil.which(b) is None]
requires_ssh_tools = pytest.mark.skipif(
    bool(_missing), reason=f"missing binaries: {_missing}"
)


# ---------------------------------------------------------------------------
# Shared mock SFTP server (publickey and/or password)
# ---------------------------------------------------------------------------


class _MatrixSFTPInterface(paramiko.SFTPServerInterface):
    HOME = "/home/fm"
    FILES = {
        "/": ("dir", None),
        "/home": ("dir", None),
        "/home/fm": ("dir", None),
        "/home/fm/ok.txt": ("file", b"auth-ok\n"),
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
        attrs.filename = path.rsplit("/", 1)[-1] or "/"
        return attrs

    def canonicalize(self, path):
        if not path or path in (".", "./", "~"):
            return self.HOME
        return path

    def list_folder(self, path):
        path = self.canonicalize(path)
        if self.FILES.get(path, (None,))[0] != "dir":
            return paramiko.SFTP_NO_SUCH_FILE
        prefix = path.rstrip("/") + "/"
        out = []
        for full in self.FILES:
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


class _AuthServer(paramiko.ServerInterface):
    """Publickey and/or password. Leave subsystem handling to paramiko default."""

    def __init__(self, *, authorized_blobs=None, password=None):
        self._authorized = set(authorized_blobs or [])
        self._password = password

    def get_allowed_auths(self, username):
        methods = []
        if self._authorized:
            methods.append("publickey")
        if self._password is not None:
            methods.append("password")
        return ",".join(methods) or "publickey"

    def check_auth_publickey(self, username, key):
        if key.asbytes() in self._authorized:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if self._password is not None and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


def _handle_conn(client_sock, host_key, server_factory):
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(host_key)
        transport.set_subsystem_handler(
            "sftp", paramiko.SFTPServer, _MatrixSFTPInterface
        )
        transport.start_server(server=server_factory())
        # Do not transport.accept() — it steals the SFTP subsystem channel.
        deadline = time.time() + 30
        while transport.is_active() and time.time() < deadline:
            time.sleep(0.05)
        transport.close()
    except Exception:
        pass


@contextmanager
def _sftp_server(server_factory):
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
                target=_handle_conn,
                args=(client, host_key, server_factory),
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
    out = subprocess.check_output(["ssh-agent", "-s"], text=True)
    sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", out).group(1)
    pid = re.search(r"SSH_AGENT_PID=(\d+);", out).group(1)

    def add(key_path: Path, passphrase: str = ""):
        env = os.environ.copy()
        env["SSH_AUTH_SOCK"] = sock
        if passphrase:
            ask = Path(key_path).parent / "agent-askpass.sh"
            ask.write_text(f'#!/bin/sh\nprintf "%s" "{passphrase}"\n')
            ask.chmod(0o700)
            env["SSH_ASKPASS"] = str(ask)
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = ":0"
        else:
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


def _keygen(path: Path, passphrase: str = ""):
    subprocess.run(
        [
            "ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase,
            "-f", str(path), "-C", "fm-auth-matrix",
        ],
        check=True,
        capture_output=True,
    )


def _write_host_config(
    cfg: Path,
    port: int,
    *,
    identity_file: Path | None = None,
    identities_only: bool = False,
    prefer_password: bool = False,
):
    lines = [
        f"Host {HOST_ALIAS}",
        "    HostName 127.0.0.1",
        f"    Port {port}",
        f"    User {USERNAME}",
        "    StrictHostKeyChecking no",
        "    UserKnownHostsFile /dev/null",
        "    GlobalKnownHostsFile /dev/null",
    ]
    if prefer_password:
        lines += [
            "    PreferredAuthentications password",
            "    PubkeyAuthentication no",
            "    PasswordAuthentication yes",
        ]
    else:
        lines += [
            "    PreferredAuthentications publickey",
            "    PubkeyAuthentication yes",
            "    PasswordAuthentication no",
        ]
    if identity_file is not None:
        lines.append(f"    IdentityFile {identity_file}")
    if identities_only:
        lines.append("    IdentitiesOnly yes")
    cfg.write_text("\n".join(lines) + "\n")


def _make_conn(
    cfg: Path,
    *,
    auth_method: int = 0,
    identity_files=None,
    password=None,
):
    return SimpleNamespace(
        nickname=HOST_ALIAS,
        host=HOST_ALIAS,
        hostname="127.0.0.1",
        username=USERNAME,
        auth_method=auth_method,
        resolved_identity_files=list(identity_files or []),
        password=password,
        _resolve_config_override_path=lambda: str(cfg),
    )


def _app_config(*, askpass_enabled: bool = True, overrides=None):
    return SimpleNamespace(
        get_setting=lambda k, d=None: (
            askpass_enabled if k == "use-askpass" else d
        ),
        get_ssh_config=lambda: {
            "ssh_overrides": list(overrides or ["-o", "ConnectTimeout=8"]),
        },
    )


def _prepare_manager(monkeypatch, conn, cm, app_config):
    """Load FM module, pin Config, return OpenSSHSFTPManager."""
    _load_file_manager_module(monkeypatch)
    import sshpilot.file_manager.openssh_backend as ob
    import sshpilot.config as config_mod

    monkeypatch.setattr(config_mod, "Config", lambda: app_config)
    return ob.OpenSSHSFTPManager(
        "127.0.0.1",
        USERNAME,
        22,
        connection=conn,
        connection_manager=cm,
        dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
    )


def _assert_connected_lists(manager):
    assert manager.is_connected()
    names = sorted(a.filename for a in manager._client.listdir_attr("/home/fm"))
    assert names == ["ok.txt"]


# ---------------------------------------------------------------------------
# Decision layer — wiring + UI recovery gate (always runs)
# ---------------------------------------------------------------------------


class TestFileManagerAuthDecision:
    """What helpers the FM wires, and which failures get a password dialog."""

    KEY = "/home/u/.ssh/id_ed25519"

    def _resolve(self, monkeypatch, *, auth_method, passphrase_stored, password_stored,
                 askpass_enabled=True):
        from sshpilot import ssh_connection_builder
        from sshpilot.ssh_connection_builder import resolve_native_auth

        monkeypatch.setattr(
            ssh_connection_builder,
            "lookup_passphrase",
            lambda p: PASSPHRASE if passphrase_stored else "",
        )
        if passphrase_stored and password_stored:
            monkeypatch.setattr(
                ssh_connection_builder,
                "ensure_key_in_agent",
                lambda path, *, force=False, lifetime=0: True,
            )
        conn = SimpleNamespace(
            auth_method=auth_method,
            resolved_identity_files=[self.KEY],
            password=None,
            hostname="example.com",
            host="example.com",
            username=USERNAME,
        )
        cm = SimpleNamespace(
            get_password=lambda host, user: PASSWORD if password_stored else None
        )
        return resolve_native_auth(
            conn, cm, app_config=_app_config(askpass_enabled=askpass_enabled)
        )

    def test_agent_ready_key_auth_wires_no_interactive_helpers(self, monkeypatch):
        """#1 CAN — nothing stored; agent is expected to satisfy pubkey."""
        auth = self._resolve(
            monkeypatch, auth_method=0,
            passphrase_stored=False, password_stored=False,
        )
        assert auth.use_askpass is False
        assert auth.use_sshpass is False
        assert "SSH_ASKPASS" not in auth.env or not auth.env.get("SSH_ASKPASS")

    def test_stored_passphrase_wires_askpass(self, monkeypatch):
        """#3 CAN — askpass autofill works without a TTY."""
        auth = self._resolve(
            monkeypatch, auth_method=0,
            passphrase_stored=True, password_stored=False,
        )
        assert auth.use_askpass is True
        assert auth.use_sshpass is False
        assert auth.env.get("SSH_ASKPASS")
        assert auth.env.get("SSH_ASKPASS_REQUIRE") == "prefer"

    def test_password_auth_with_stored_password_wires_sshpass(self, monkeypatch):
        """#4 CAN — sshpass feeds the password over the SFTP pipe path."""
        auth = self._resolve(
            monkeypatch, auth_method=1,
            passphrase_stored=False, password_stored=True,
        )
        assert auth.use_sshpass is True
        assert auth.password == PASSWORD
        assert auth.use_askpass is False

    def test_password_auth_without_stored_password_needs_ui_dialog(self, monkeypatch):
        """#4b/#6 — backend has no sshpass until the graphical dialog fills it."""
        auth = self._resolve(
            monkeypatch, auth_method=1,
            passphrase_stored=False, password_stored=False,
        )
        assert auth.use_sshpass is False
        assert auth.use_askpass is False
        assert auth.password_mode is True  # UI may still prompt graphically

    def test_encrypted_key_nothing_stored_strips_askpass(self, monkeypatch):
        """#5 CAN'T — nothing saved strips askpass, so no GUI passphrase either."""
        auth = self._resolve(
            monkeypatch, auth_method=0,
            passphrase_stored=False, password_stored=False,
        )
        assert auth.use_askpass is False
        assert auth.use_sshpass is False
        assert not auth.env.get("SSH_ASKPASS")

    def test_askpass_disabled_strips_helpers(self, monkeypatch):
        """#7 CAN'T — askpass off + encrypted key → TTY-only, fails on FM pipes."""
        auth = self._resolve(
            monkeypatch, auth_method=0,
            passphrase_stored=True, password_stored=False,
            askpass_enabled=False,
        )
        assert auth.use_askpass is False
        assert auth.use_sshpass is False

    def test_build_argv_applies_dialog_password_to_sshpass(self, monkeypatch):
        """#4 CAN — dialog password on manager._password reaches sshpass wrap."""
        import sshpilot.ssh_password_exec as spe

        conn = SimpleNamespace(
            nickname=HOST_ALIAS,
            hostname="h",
            username=USERNAME,
            auth_method=1,
            resolved_identity_files=[],
            password=None,
            _resolve_config_override_path=lambda: "/nonexistent-cfg",
        )
        # Avoid a real Config()/ssh -F; stub build_ssh_connection.
        import sshpilot.ssh_connection_builder as scb

        monkeypatch.setattr(
            scb,
            "build_ssh_connection",
            lambda ctx: SimpleNamespace(
                command=["ssh", "-s", HOST_ALIAS, "sftp"],
                env={"SSH_ASKPASS_REQUIRE": "never"},
                use_sshpass=True,
                password=ctx.connection.password,
                use_askpass=False,
            ),
        )
        calls = {}

        def fake_wrap(argv, password, env=None):
            calls["password"] = password
            return (["sshpass", "-f", "fifo"] + list(argv), lambda: None)

        monkeypatch.setattr(spe, "wrap_argv_with_sshpass", fake_wrap)

        _load_file_manager_module(monkeypatch)
        import sshpilot.file_manager.openssh_backend as ob

        manager = ob.OpenSSHSFTPManager(
            "h", USERNAME, 22, connection=conn,
            connection_manager=SimpleNamespace(get_password=lambda h, u: None),
        )
        manager._password = "dialog-secret"
        argv, _env, cleanup = manager._build_argv()
        assert calls["password"] == "dialog-secret"
        assert argv[0] == "sshpass"
        assert cleanup is not None
        manager.close()

    def test_ui_password_dialog_only_for_password_auth(self, monkeypatch):
        """#8 CAN'T / #9 CAN — UI recovery gate."""
        module = _load_file_manager_module(monkeypatch)
        gate = module.FileManagerWindow._is_password_auth_enabled

        key_conn = SimpleNamespace(auth_method=0, pubkey_auth_no=False)
        pw_conn = SimpleNamespace(auth_method=1, pubkey_auth_no=False)
        # Unbound call — method only reads ``connection``.
        assert gate(object(), key_conn) is False  # #8 no dialog on key failure
        assert gate(object(), pw_conn) is True    # #9 dialog on password failure


# ---------------------------------------------------------------------------
# Graphical prompt channels (no TTY required)
# ---------------------------------------------------------------------------


class TestFileManagerGraphicalPrompts:
    """Prove the FM's Adwaita/askpass GUI paths — what users actually see."""

    def test_password_auth_enables_preconnect_and_retry_dialogs(self, monkeypatch):
        """#4b/#9 — password auth opens show_ssh_password_dialog (not a TTY)."""
        import inspect

        module = _load_file_manager_module(monkeypatch)
        FM = module.FileManagerWindow
        assert FM._is_password_auth_enabled(
            object(), SimpleNamespace(auth_method=1, pubkey_auth_no=False)
        )
        preconnect_src = inspect.getsource(FM._show_password_dialog_before_connect)
        retry_src = inspect.getsource(FM._on_authentication_required)
        assert "show_ssh_password_dialog" in preconnect_src
        assert "show_ssh_password_dialog" in retry_src
        assert "_is_password_auth_enabled" in retry_src

    def test_askpass_env_advertises_inapp_passphrase_ipc(self, monkeypatch):
        """#3b CAN — SSH_ASKPASS child can open the in-app passphrase dialog."""
        from sshpilot import askpass_utils

        sock = "/run/user/1000/sshpilot/askpass-test.sock"
        token = "deadbeef" * 4
        askpass_utils.set_askpass_ipc(sock, token)
        try:
            env = askpass_utils.get_ssh_env_with_askpass(require="prefer")
            assert env.get("SSH_ASKPASS")
            assert env.get("SSH_ASKPASS_REQUIRE") == "prefer"
            assert env.get("SSHPILOT_ASKPASS_SOCKET") == sock
            assert env.get("SSHPILOT_ASKPASS_TOKEN") == token
        finally:
            askpass_utils.set_askpass_ipc(None, None)

    def test_fm_build_argv_forwards_askpass_ipc_for_passphrase_gui(self, monkeypatch):
        """#3b — FM merges askpass IPC env so the helper can prompt graphically."""
        import sshpilot.ssh_connection_builder as scb

        sock = "/tmp/sshpilot-askpass.sock"
        token = "tok123"
        monkeypatch.setattr(
            scb,
            "build_ssh_connection",
            lambda ctx: SimpleNamespace(
                command=["ssh", "-s", HOST_ALIAS, "sftp"],
                env={
                    "SSH_ASKPASS": "/tmp/askpass-helper",
                    "SSH_ASKPASS_REQUIRE": "prefer",
                    "SSHPILOT_ASKPASS_SOCKET": sock,
                    "SSHPILOT_ASKPASS_TOKEN": token,
                    "DISPLAY": ":0",
                },
                use_sshpass=False,
                password=None,
                use_askpass=True,
            ),
        )
        _load_file_manager_module(monkeypatch)
        import sshpilot.file_manager.openssh_backend as ob

        manager = ob.OpenSSHSFTPManager(
            "h", USERNAME, 22,
            connection=SimpleNamespace(nickname=HOST_ALIAS, password=None),
            dispatcher=lambda cb, args=(), kwargs=None: cb(*args, **(kwargs or {})),
        )
        _argv, env, cleanup = manager._build_argv()
        assert env["SSH_ASKPASS"] == "/tmp/askpass-helper"
        assert env["SSHPILOT_ASKPASS_SOCKET"] == sock
        assert env["SSHPILOT_ASKPASS_TOKEN"] == token
        assert cleanup is None
        manager.close()

    def test_askpass_cli_offers_gui_when_builtin_prompt_enabled(self, monkeypatch):
        """#3b — helper falls through to GUI (IPC or standalone) for passphrase."""
        from sshpilot import askpass_utils

        monkeypatch.setattr(askpass_utils, "_builtin_passphrase_prompt_enabled", lambda: True)
        monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _p: "")
        monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", lambda *_a, **_k: None)
        monkeypatch.setattr(
            askpass_utils,
            "_route_passphrase_to_main_app",
            lambda key_path, prompt, log_fn: (True, "gui-passphrase"),
        )
        # Avoid keyring/libsecret noise in the askpass log path.
        monkeypatch.setattr(askpass_utils, "_keyring_available", lambda: False)
        monkeypatch.setattr(askpass_utils, "_secret_available", lambda: False)

        result = askpass_utils.handle_askpass_cli(
            "Enter passphrase for key '/home/u/.ssh/id_ed25519': "
        )
        assert result == "gui-passphrase"

    def test_askpass_cli_skips_gui_when_builtin_prompt_disabled(self, monkeypatch):
        """Builtin passphrase prompt off → helper returns nothing (no GUI)."""
        from sshpilot import askpass_utils

        monkeypatch.setattr(askpass_utils, "_builtin_passphrase_prompt_enabled", lambda: False)
        monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _p: "")
        monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", lambda *_a, **_k: None)
        monkeypatch.setattr(askpass_utils, "_keyring_available", lambda: False)
        monkeypatch.setattr(askpass_utils, "_secret_available", lambda: False)

        routed = []

        def _boom(*_a, **_k):
            routed.append(True)
            return (False, None)

        monkeypatch.setattr(askpass_utils, "_route_passphrase_to_main_app", _boom)

        result = askpass_utils.handle_askpass_cli(
            "Enter passphrase for key '/home/u/.ssh/id_ed25519': "
        )
        assert result is None
        assert routed == []  # never reached GUI routing


# ---------------------------------------------------------------------------
# Live layer — real OpenSSHSFTPManager._connect_impl
# ---------------------------------------------------------------------------


@requires_ssh_tools
class TestFileManagerAuthLiveCan:
    """Scenarios that MUST succeed on the SFTP pipe path."""

    def test_01_agent_held_key_can_connect(self, tmp_path, monkeypatch):
        key = tmp_path / "agent_key"
        _keygen(key)
        pub = paramiko.Ed25519Key.from_private_key_file(str(key))

        with _throwaway_agent() as agent, _sftp_server(
            lambda: _AuthServer(authorized_blobs=[pub.asbytes()])
        ) as server:
            agent.add(key)
            cfg = tmp_path / "cfg"
            _write_host_config(cfg, server.port)  # no IdentityFile
            conn = _make_conn(cfg)
            cm = SimpleNamespace(get_password=lambda h, u: None)
            monkeypatch.setenv("SSH_AUTH_SOCK", agent.sock)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.lookup_passphrase",
                lambda _p: "",
            )
            manager = _prepare_manager(monkeypatch, conn, cm, _app_config())
            try:
                manager._connect_impl()
                _assert_connected_lists(manager)
            finally:
                manager.close()

    def test_02_unencrypted_identity_file_can_connect(self, tmp_path, monkeypatch):
        key = tmp_path / "plain_key"
        _keygen(key)
        pub = paramiko.Ed25519Key.from_private_key_file(str(key))

        with _sftp_server(
            lambda: _AuthServer(authorized_blobs=[pub.asbytes()])
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(
                cfg, server.port, identity_file=key, identities_only=True
            )
            conn = _make_conn(cfg, identity_files=[str(key)])
            cm = SimpleNamespace(get_password=lambda h, u: None)
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.lookup_passphrase",
                lambda _p: "",
            )
            manager = _prepare_manager(monkeypatch, conn, cm, _app_config())
            try:
                manager._connect_impl()
                _assert_connected_lists(manager)
            finally:
                manager.close()

    def test_03_encrypted_key_with_askpass_can_connect(self, tmp_path, monkeypatch):
        key = tmp_path / "enc_key"
        _keygen(key, PASSPHRASE)
        pub = paramiko.Ed25519Key.from_private_key_file(
            str(key), password=PASSPHRASE
        )
        ask = tmp_path / "askpass.sh"
        ask.write_text(f'#!/bin/sh\nprintf "%s" "{PASSPHRASE}"\n')
        ask.chmod(0o700)

        with _sftp_server(
            lambda: _AuthServer(authorized_blobs=[pub.asbytes()])
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(
                cfg, server.port, identity_file=key, identities_only=True
            )
            conn = _make_conn(cfg, identity_files=[str(key)])
            cm = SimpleNamespace(get_password=lambda h, u: None)

            # Stored passphrase → resolver enables askpass; point it at our script.
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.lookup_passphrase",
                lambda p: PASSPHRASE if str(p) == str(key) else "",
            )
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.get_ssh_env_with_askpass",
                lambda require="prefer": {
                    **os.environ,
                    "SSH_ASKPASS": str(ask),
                    "SSH_ASKPASS_REQUIRE": require,
                    "DISPLAY": ":0",
                },
            )
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

            manager = _prepare_manager(monkeypatch, conn, cm, _app_config())
            try:
                argv, env, cleanup = manager._build_argv()
                assert "SSH_ASKPASS" in env
                assert cleanup is None
                assert argv[-3:] == ["-s", HOST_ALIAS, "sftp"]
                manager._connect_impl()
                _assert_connected_lists(manager)
            finally:
                manager.close()

    def test_04_password_auth_with_sshpass_can_connect(self, tmp_path, monkeypatch):
        with _sftp_server(
            lambda: _AuthServer(password=PASSWORD)
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(cfg, server.port, prefer_password=True)
            conn = _make_conn(cfg, auth_method=1)
            cm = SimpleNamespace(get_password=lambda h, u: PASSWORD)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)

            manager = _prepare_manager(monkeypatch, conn, cm, _app_config())
            try:
                argv, env, cleanup = manager._build_argv()
                assert os.path.basename(argv[0]) == "sshpass"
                assert cleanup is not None
                manager._connect_impl()
                _assert_connected_lists(manager)
            finally:
                manager.close()

    def test_04b_dialog_password_on_manager_can_connect(self, tmp_path, monkeypatch):
        """Password typed in the FM dialog (manager._password) also works."""
        with _sftp_server(
            lambda: _AuthServer(password=PASSWORD)
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(cfg, server.port, prefer_password=True)
            conn = _make_conn(cfg, auth_method=1)
            # No stored password — dialog supplies it.
            cm = SimpleNamespace(get_password=lambda h, u: None)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)

            manager = _prepare_manager(monkeypatch, conn, cm, _app_config())
            manager._password = PASSWORD
            try:
                manager._connect_impl()
                _assert_connected_lists(manager)
            finally:
                manager.close()


@requires_ssh_tools
class TestFileManagerAuthLiveCannot:
    """Scenarios that MUST fail on the SFTP pipe path (TTY-dependent)."""

    def test_05_encrypted_key_nothing_stored_cannot_connect(
        self, tmp_path, monkeypatch
    ):
        key = tmp_path / "enc_key"
        _keygen(key, PASSPHRASE)
        pub = paramiko.Ed25519Key.from_private_key_file(
            str(key), password=PASSPHRASE
        )

        with _sftp_server(
            lambda: _AuthServer(authorized_blobs=[pub.asbytes()])
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(
                cfg, server.port, identity_file=key, identities_only=True
            )
            conn = _make_conn(cfg, identity_files=[str(key)])
            cm = SimpleNamespace(get_password=lambda h, u: None)
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.lookup_passphrase",
                lambda _p: "",
            )
            # BatchMode so ssh fails fast instead of hanging on a missing TTY.
            app = _app_config(
                overrides=["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
            )
            manager = _prepare_manager(monkeypatch, conn, cm, app)
            try:
                argv, env, cleanup = manager._build_argv()
                assert "SSH_ASKPASS" not in env or not env.get("SSH_ASKPASS")
                assert cleanup is None
                with pytest.raises(Exception):
                    manager._connect_impl()
                assert not manager.is_connected()
            finally:
                manager.close()

    def test_06_password_auth_without_dialog_password_cannot_connect(
        self, tmp_path, monkeypatch
    ):
        """#6 — backend alone fails; the UI dialog (#4b) is what makes it CAN."""
        with _sftp_server(
            lambda: _AuthServer(password=PASSWORD)
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(cfg, server.port, prefer_password=True)
            conn = _make_conn(cfg, auth_method=1)
            cm = SimpleNamespace(get_password=lambda h, u: None)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)

            app = _app_config(
                overrides=["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"]
            )
            manager = _prepare_manager(monkeypatch, conn, cm, app)
            # Deliberately no manager._password (simulates dialog cancelled).
            try:
                argv, _env, cleanup = manager._build_argv()
                assert os.path.basename(argv[0]) != "sshpass"
                assert cleanup is None
                with pytest.raises(Exception):
                    manager._connect_impl()
                assert not manager.is_connected()
            finally:
                manager.close()

    def test_07_askpass_disabled_encrypted_key_cannot_connect(
        self, tmp_path, monkeypatch
    ):
        key = tmp_path / "enc_key"
        _keygen(key, PASSPHRASE)
        pub = paramiko.Ed25519Key.from_private_key_file(
            str(key), password=PASSPHRASE
        )

        with _sftp_server(
            lambda: _AuthServer(authorized_blobs=[pub.asbytes()])
        ) as server:
            cfg = tmp_path / "cfg"
            _write_host_config(
                cfg, server.port, identity_file=key, identities_only=True
            )
            conn = _make_conn(cfg, identity_files=[str(key)])
            cm = SimpleNamespace(get_password=lambda h, u: None)
            # Passphrase IS stored, but askpass is disabled in settings → TTY.
            monkeypatch.setattr(
                "sshpilot.ssh_connection_builder.lookup_passphrase",
                lambda p: PASSPHRASE if str(p) == str(key) else "",
            )
            monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
            monkeypatch.delenv("SSH_ASKPASS", raising=False)

            app = _app_config(
                askpass_enabled=False,
                overrides=["-o", "ConnectTimeout=5", "-o", "BatchMode=yes"],
            )
            manager = _prepare_manager(monkeypatch, conn, cm, app)
            try:
                argv, env, cleanup = manager._build_argv()
                assert not env.get("SSH_ASKPASS")
                assert cleanup is None
                with pytest.raises(Exception):
                    manager._connect_impl()
                assert not manager.is_connected()
            finally:
                manager.close()


class TestFileManagerAuthMatrixSummary:
    """Keep the module docstring matrix honest — one place that lists outcomes."""

    EXPECTED = {
        "agent_held_key": "CAN",
        "unencrypted_identity_file": "CAN",
        "encrypted_key_stored_passphrase_askpass": "CAN",
        "encrypted_key_askpass_gui_passphrase": "CAN",
        "password_auth_stored_sshpass": "CAN",
        "password_auth_graphical_dialog_sshpass": "CAN",
        "encrypted_key_askpass_stripped_nothing_saved": "CAN'T",
        "password_auth_dialog_cancelled": "CAN'T",
        "askpass_disabled_encrypted_key": "CAN'T",
        "ui_password_dialog_on_key_auth_failure": "CAN'T",
        "ui_password_dialog_on_password_auth_failure": "CAN",
    }

    def test_matrix_documents_graphical_and_tty_gaps(self):
        can = {k for k, v in self.EXPECTED.items() if v == "CAN"}
        cant = {k for k, v in self.EXPECTED.items() if v == "CAN'T"}
        # Graphical channels the user sees in the FM:
        assert "password_auth_graphical_dialog_sshpass" in can
        assert "encrypted_key_askpass_gui_passphrase" in can
        assert "ui_password_dialog_on_password_auth_failure" in can
        # Still impossible without a helper / dialog:
        assert "encrypted_key_askpass_stripped_nothing_saved" in cant
        assert "password_auth_dialog_cancelled" in cant
        assert "ui_password_dialog_on_key_auth_failure" in cant
        assert len(self.EXPECTED) == 11
