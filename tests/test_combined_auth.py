"""Combined-auth (publickey AND password) behaviour against a mock SSH server.

Some servers set ``AuthenticationMethods publickey,password`` — a client must
satisfy BOTH a public key and a password in the same session. This module spins
up a paramiko mock server that enforces exactly that (publickey -> partial
success -> password), then checks how sshPilot's single auth resolver
(``resolve_native_auth``) behaves and whether the resulting connection actually
authenticates, across the four secret-storage scenarios:

  * both a key passphrase and a host password stored,
  * neither stored,
  * only the password stored,
  * only the passphrase stored.

Two layers:
  * Decision layer (always runs): what ``resolve_native_auth`` chooses
    (use_askpass / use_sshpass) per scenario — pure, no network.
  * Connection layer (skips if a required binary is missing): run the real
    ``ssh`` client against the mock combined-auth server, supplying exactly the
    secrets sshPilot would (key via ssh-agent when a passphrase is "stored" —
    mirroring sshPilot's keyring preload; password via sshpass when a password
    is "stored"), and assert whether the publickey+password handshake completes.

Against a combined-auth host with an *encrypted* key, only the "both stored"
scenario succeeds: the resolver wires the key (via the agent preload, using the
stored passphrase) AND sshpass (for the stored password). The other three fail
because one of the two required factors is missing. Positive controls also show
the combined path works for an unencrypted key with a stored password.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

paramiko = pytest.importorskip("paramiko")

from sshpilot import ssh_connection_builder
from sshpilot.ssh_connection_builder import resolve_native_auth


PASSPHRASE = "secretpass"
PASSWORD = "hunter2"
OTP_CODE = "123456"
USERNAME = "tester"

_REQUIRED_BINS = ["ssh", "ssh-keygen", "ssh-add", "ssh-agent", "sshpass"]
_missing = [b for b in _REQUIRED_BINS if shutil.which(b) is None]
requires_ssh_tools = pytest.mark.skipif(
    bool(_missing), reason=f"missing binaries: {_missing}"
)


# ---------------------------------------------------------------------------
# Mock combined-auth SSH server (publickey, then password)
# ---------------------------------------------------------------------------

class _CombinedAuthServer(paramiko.ServerInterface):
    """Requires a valid public key first, then the password (one fresh instance
    per connection, since the publickey->password progress is per-session)."""

    def __init__(self, authorized_blobs, password):
        self._authorized = set(authorized_blobs)
        self._password = password
        self._pk_ok = False

    def get_allowed_auths(self, username):
        return "password" if self._pk_ok else "publickey"

    def check_auth_publickey(self, username, key):
        if key.asbytes() in self._authorized:
            self._pk_ok = True
            return paramiko.AUTH_PARTIALLY_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_password(self, username, password):
        if self._pk_ok and password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        channel.send_exit_status(0)
        return True

    def check_channel_pty_request(self, *args):
        return True

    def check_channel_shell_request(self, channel):
        channel.send_exit_status(0)
        return True


class _PublicKeyOnlyServer(paramiko.ServerInterface):
    """Accepts a valid public key and never asks for the saved password."""

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

    def check_channel_exec_request(self, channel, command):
        channel.send_exit_status(0)
        return True


class _PasswordOnlyServer(paramiko.ServerInterface):
    """Accepts only the account password."""

    def __init__(self, password):
        self._password = password

    def get_allowed_auths(self, username):
        return "password"

    def check_auth_password(self, username, password):
        if password == self._password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        channel.send_exit_status(0)
        return True


class _PublicKeyInteractiveServer(paramiko.ServerInterface):
    """Requires a valid public key, then one keyboard-interactive response."""

    def __init__(self, authorized_blobs, prompt, expected_response):
        self._authorized = set(authorized_blobs)
        self._prompt = prompt
        self._expected_response = expected_response
        self._pk_ok = False

    def get_allowed_auths(self, username):
        return "keyboard-interactive" if self._pk_ok else "publickey"

    def check_auth_publickey(self, username, key):
        if key.asbytes() in self._authorized:
            self._pk_ok = True
            return paramiko.AUTH_PARTIALLY_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_interactive(self, username, submethods):
        if not self._pk_ok:
            return paramiko.AUTH_FAILED
        return paramiko.InteractiveQuery(
            "sshPilot test",
            "additional verification required",
            (self._prompt, False),
        )

    def check_auth_interactive_response(self, responses):
        if self._pk_ok and responses and responses[0] == self._expected_response:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command):
        channel.send_exit_status(0)
        return True


def _handle_conn(client_sock, host_key, authorized_blobs, password):
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(host_key)
        transport.start_server(server=_CombinedAuthServer(authorized_blobs, password))
        chan = transport.accept(timeout=10)
        if chan is not None:
            time.sleep(0.2)
            try:
                chan.close()
            except Exception:
                pass
        time.sleep(0.2)
        transport.close()
    except Exception:
        pass


def _handle_conn_with_factory(client_sock, host_key, server_factory):
    try:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(host_key)
        transport.start_server(server=server_factory())
        chan = transport.accept(timeout=10)
        if chan is not None:
            time.sleep(0.2)
            try:
                chan.close()
            except Exception:
                pass
        time.sleep(0.2)
        transport.close()
    except Exception:
        pass


def _serve(listen_sock, host_key, authorized_blobs, password, stop_evt):
    listen_sock.settimeout(0.5)
    while not stop_evt.is_set():
        try:
            client, _addr = listen_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(
            target=_handle_conn,
            args=(client, host_key, authorized_blobs, password),
            daemon=True,
        ).start()


def _serve_with_factory(listen_sock, host_key, server_factory, stop_evt):
    listen_sock.settimeout(0.5)
    while not stop_evt.is_set():
        try:
            client, _addr = listen_sock.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(
            target=_handle_conn_with_factory,
            args=(client, host_key, server_factory),
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# Key / server fixtures
# ---------------------------------------------------------------------------

def _keygen(path, passphrase):
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path),
         "-C", "combined-auth-test"],
        check=True, capture_output=True,
    )


def _authorized_blob(key_path, passphrase=None):
    return paramiko.Ed25519Key.from_private_key_file(
        str(key_path),
        password=passphrase,
    ).asbytes()


@contextmanager
def _auth_server(server_factory):
    host_key = paramiko.RSAKey.generate(2048)
    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(8)
    port = listen.getsockname()[1]
    stop_evt = threading.Event()
    thread = threading.Thread(
        target=_serve_with_factory,
        args=(listen, host_key, server_factory, stop_evt),
        daemon=True,
    )
    thread.start()
    try:
        yield SimpleNamespace(port=port)
    finally:
        stop_evt.set()
        try:
            listen.close()
        except Exception:
            pass
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def combined_server(tmp_path_factory):
    if _missing:
        pytest.skip(f"missing binaries: {_missing}")
    tmp = tmp_path_factory.mktemp("combined_auth")
    enc_key = tmp / "enc_key"      # encrypted with PASSPHRASE
    plain_key = tmp / "plain_key"  # no passphrase
    _keygen(enc_key, PASSPHRASE)
    _keygen(plain_key, "")

    # Authorized public-key blobs (load the private keys via paramiko to get the
    # public key bytes the server will compare against).
    enc_pk = paramiko.Ed25519Key.from_private_key_file(str(enc_key), password=PASSPHRASE)
    plain_pk = paramiko.Ed25519Key.from_private_key_file(str(plain_key))
    authorized = [enc_pk.asbytes(), plain_pk.asbytes()]

    host_key = paramiko.RSAKey.generate(2048)

    listen = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listen.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listen.bind(("127.0.0.1", 0))
    listen.listen(8)
    port = listen.getsockname()[1]

    stop_evt = threading.Event()
    thread = threading.Thread(
        target=_serve, args=(listen, host_key, authorized, PASSWORD, stop_evt),
        daemon=True,
    )
    thread.start()

    yield SimpleNamespace(
        port=port, enc_key=str(enc_key), plain_key=str(plain_key), tmp=tmp,
    )

    stop_evt.set()
    try:
        listen.close()
    except Exception:
        pass
    thread.join(timeout=2)


@pytest.fixture
def agent():
    """A throwaway ssh-agent; yields an add(key_path, passphrase) helper."""
    if shutil.which("ssh-agent") is None:
        pytest.skip("ssh-agent missing")
    out = subprocess.check_output(["ssh-agent", "-s"], text=True)
    sock = re.search(r"SSH_AUTH_SOCK=([^;]+);", out).group(1)
    pid = re.search(r"SSH_AGENT_PID=(\d+);", out).group(1)

    added = SimpleNamespace(sock=sock)

    def _add(key_path, passphrase, workdir):
        askpass = os.path.join(workdir, "askpass.sh")
        with open(askpass, "w") as fh:
            fh.write(f'#!/bin/sh\nprintf "%s" "{passphrase}"\n')
        os.chmod(askpass, 0o700)
        env = os.environ.copy()
        env["SSH_AUTH_SOCK"] = sock
        env["SSH_ASKPASS"] = askpass
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = ":0"
        subprocess.run(
            ["ssh-add", key_path], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=10,
        )

    added.add = _add
    try:
        yield added
    finally:
        try:
            subprocess.run(["ssh-agent", "-k"], env={**os.environ, "SSH_AGENT_PID": pid},
                           capture_output=True, timeout=5)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers to build a connection stub and run the real ssh client
# ---------------------------------------------------------------------------

def _make_conn(identity_file, *, auth_method=0, in_memory_password=None):
    return SimpleNamespace(
        auth_method=auth_method,
        resolved_identity_files=[identity_file],
        password=in_memory_password,
        hostname="127.0.0.1",
        host="127.0.0.1",
        username=USERNAME,
    )


def _make_cm(stored_password):
    return SimpleNamespace(get_password=lambda host, user: stored_password)


def _resolve(monkeypatch, *, passphrase_stored, password_stored, identity_file,
             auth_method=0, app_config=None):
    """Drive resolve_native_auth with mocked keyring + stored password."""
    monkeypatch.setattr(
        ssh_connection_builder, "lookup_passphrase",
        lambda p: PASSPHRASE if passphrase_stored else "",
    )
    conn = _make_conn(identity_file, auth_method=auth_method)
    cm = _make_cm(PASSWORD if password_stored else None)
    return resolve_native_auth(conn, cm, app_config=app_config)


def _ssh_connect(
    port,
    key_path=None,
    *,
    agent_sock=None,
    sshpass_password=None,
    preferred_auth="publickey,password",
    pubkey_auth=True,
    password_auth=True,
    keyboard_interactive_auth=True,
    timeout=25,
):
    """Run the real ssh client against the mock server; return its exit code.

    Mirrors how sshPilot would supply secrets: the key is offered via -i (and
    via the agent when a passphrase was 'stored' and preloaded); a stored
    password is fed by sshpass. Returns 0 on a fully-authenticated session.
    """
    base = [
        "ssh", "-p", str(port),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", f"PreferredAuthentications={preferred_auth}",
        "-o", f"PubkeyAuthentication={'yes' if pubkey_auth else 'no'}",
        "-o", f"PasswordAuthentication={'yes' if password_auth else 'no'}",
        "-o", f"KbdInteractiveAuthentication={'yes' if keyboard_interactive_auth else 'no'}",
        "-o", "NumberOfPasswordPrompts=1",
        "-o", "ConnectTimeout=5",
    ]
    if key_path:
        base[3:3] = ["-i", key_path]
        base.extend(["-o", "IdentitiesOnly=yes"])
    base.extend([f"{USERNAME}@127.0.0.1", "true"])
    env = os.environ.copy()
    env.pop("SSH_ASKPASS", None)
    env["SSH_ASKPASS_REQUIRE"] = "never"
    if agent_sock:
        env["SSH_AUTH_SOCK"] = agent_sock
    else:
        env.pop("SSH_AUTH_SOCK", None)

    if sshpass_password is not None:
        argv = ["sshpass", "-p", sshpass_password] + base
    else:
        # No password to feed → forbid interactive prompts so it fails fast
        # rather than blocking on a TTY that isn't there.
        argv = base[:1] + ["-o", "BatchMode=yes"] + base[1:]

    try:
        proc = subprocess.run(argv, env=env, capture_output=True, timeout=timeout, text=True)
        return proc.returncode
    except subprocess.TimeoutExpired:
        return 124


# ---------------------------------------------------------------------------
# Decision layer — what resolve_native_auth chooses (no network)
# ---------------------------------------------------------------------------

class TestResolverDecision:
    KEY = "/home/u/.ssh/id_ed25519"

    def test_both_stored_wires_combined_auth(self, monkeypatch):
        # Combined auth: passphrase (via agent load) + password (via sshpass).
        # The resolver now actually loads the key into the agent; succeed here.
        monkeypatch.setattr(ssh_connection_builder, "ensure_key_in_agent",
                            lambda path, *, force=False, lifetime=0: True)
        auth = _resolve(monkeypatch, passphrase_stored=True, password_stored=True,
                        identity_file=self.KEY)
        assert auth.use_sshpass is True
        assert auth.password == PASSWORD
        assert auth.use_askpass is False  # askpass stripped so it can't hijack pw

    def test_both_stored_falls_back_to_askpass_when_preload_disabled(self, monkeypatch):
        # Without agent preload the key wouldn't be in the agent, so combined auth
        # is unsafe; keep askpass-only autofill (no regression for pubkey-only hosts).
        cfg = SimpleNamespace(get_setting=lambda k, d=None: False if k == "ssh.agent_preload_keys" else d)
        auth = _resolve(monkeypatch, passphrase_stored=True, password_stored=True,
                        identity_file=self.KEY, app_config=cfg)
        assert auth.use_askpass is True
        assert auth.use_sshpass is False

    def test_only_passphrase_uses_askpass(self, monkeypatch):
        auth = _resolve(monkeypatch, passphrase_stored=True, password_stored=False,
                        identity_file=self.KEY)
        assert auth.use_askpass is True
        assert auth.use_sshpass is False

    def test_only_password_uses_sshpass_fallback(self, monkeypatch):
        auth = _resolve(monkeypatch, passphrase_stored=False, password_stored=True,
                        identity_file=self.KEY)
        assert auth.use_sshpass is True
        assert auth.use_askpass is False
        assert auth.password == PASSWORD

    def test_neither_stored_no_helpers(self, monkeypatch):
        auth = _resolve(monkeypatch, passphrase_stored=False, password_stored=False,
                        identity_file=self.KEY)
        assert auth.use_askpass is False
        assert auth.use_sshpass is False


class TestCombinedAuthPreloadHandoff:
    KEY = "/home/u/.ssh/id_ed25519"

    def test_preload_uses_identities_discovered_by_resolver_when_cache_empty(self, monkeypatch):
        """Fresh non-terminal callers can resolve identities without caching them.

        The combined-auth branch then disables askpass and relies on agent
        preload, so preload must use the same identity candidate set the resolver
        used even when ``resolved_identity_files`` starts empty.
        """
        from sshpilot import askpass_utils
        from sshpilot.connection_manager import Connection

        conn = Connection({
            "host": "combo.example",
            "hostname": "combo.example",
            "username": USERNAME,
            "auth_method": 0,
        })
        conn.resolved_identity_files = []
        monkeypatch.setattr(
            conn,
            "collect_identity_file_candidates",
            lambda: [self.KEY],
        )
        monkeypatch.setattr(
            ssh_connection_builder,
            "lookup_passphrase",
            lambda path: PASSPHRASE if path == self.KEY else "",
        )
        # The resolver loads the key into the agent to commit to combined auth;
        # let that succeed so the decision is the combined path.
        monkeypatch.setattr(
            ssh_connection_builder, "ensure_key_in_agent",
            lambda path, *, force=False, lifetime=0: True,
        )

        auth = resolve_native_auth(conn, _make_cm(PASSWORD))
        assert auth.use_sshpass is True
        assert auth.use_askpass is False

        added = []
        monkeypatch.setattr(
            askpass_utils,
            "lookup_passphrase",
            lambda path: PASSPHRASE if path == self.KEY else "",
        )
        monkeypatch.setattr(
            askpass_utils,
            "ensure_key_in_agent",
            lambda path, *, force=False, lifetime=0: added.append((path, force, lifetime)) or True,
        )

        conn._preload_keys_into_agent(SimpleNamespace(get_setting=lambda _k, default=None: default))

        assert added == [(self.KEY, True, 0)]

    def test_scp_auto_identity_mode_preloads_resolver_discovered_key(self, monkeypatch):
        """SCP automatic-key mode must preload config identities before sshpass.

        Specific-key SCP already prepares ``profile.keyfile_expanded``. Automatic
        mode has no explicit profile keyfile, so it must preload the identities
        discovered by the shared resolver before taking the combined-auth path.
        """
        from sshpilot import scp_window

        monkeypatch.setattr(
            ssh_connection_builder,
            "lookup_passphrase",
            lambda path: PASSPHRASE if path == self.KEY else "",
        )

        class _Config:
            def get_ssh_config(self):
                return {}

            def get_setting(self, _key, default=None):
                return default

        class _Manager:
            known_hosts_path = None

            def __init__(self):
                self.prepare_calls = []

            def get_password(self, *_):
                return PASSWORD

            def get_key_passphrase(self, path):
                return PASSPHRASE if path == TestCombinedAuthPreloadHandoff.KEY else None

            def prepare_key_for_connection(self, path):
                self.prepare_calls.append(path)
                return True

        manager = _Manager()
        controller = scp_window.ScpWindowController.__new__(scp_window.ScpWindowController)
        controller.window = SimpleNamespace(connection_manager=manager, config=_Config())
        controller._scp_auth = None
        controller._scp_askpass_env = {}

        connection = SimpleNamespace(
            hostname="combo.example",
            host="combo.example",
            username=USERNAME,
            port=22,
            auth_method=0,
            keyfile="",
            key_select_mode=0,
            resolved_identity_files=[],
            collect_identity_file_candidates=lambda: [self.KEY],
        )

        controller._build_scp_argv(
            connection,
            ["local.txt"],
            "/remote/path",
            direction="upload",
        )

        assert manager.prepare_calls == [self.KEY]

    def test_failed_agent_load_falls_back_to_askpass_for_pubkey_only(self, monkeypatch):
        """A failed agent load must not strand saved-passphrase key auth.

        The resolver makes the combined decision contingent on actually loading
        the key into ssh-agent. If that load fails (no agent / ssh-add error /
        stale passphrase), it must fall back to askpass-only — NOT a sshpass-only
        path with askpass stripped — so a pubkey-only host that merely has an
        optional saved password still authenticates from the saved passphrase.
        """
        conn = SimpleNamespace(
            auth_method=0,
            resolved_identity_files=[self.KEY],
            password=None,
            hostname="pubkey-only.example",
            host="pubkey-only.example",
            username=USERNAME,
        )
        monkeypatch.setattr(
            ssh_connection_builder, "lookup_passphrase",
            lambda path: PASSPHRASE if path == self.KEY else "",
        )

        # The resolver attempts the agent load via ssh_connection_builder's
        # imported name; make it fail.
        def fail_load(*_args, **_kwargs):
            raise RuntimeError("ssh-add failed")

        monkeypatch.setattr(ssh_connection_builder, "ensure_key_in_agent", fail_load)

        auth = resolve_native_auth(conn, _make_cm(PASSWORD))

        assert auth.use_askpass is True       # passphrase autofill preserved
        assert auth.use_sshpass is False      # no sshpass when the key isn't loaded
        assert "SSH_ASKPASS" in auth.env


# ---------------------------------------------------------------------------
# Connection layer — real ssh against the combined-auth server
# ---------------------------------------------------------------------------

@requires_ssh_tools
class TestCombinedAuthConnection:
    def _drive(self, monkeypatch, combined_server, agent, *, passphrase_stored, password_stored):
        """Resolve auth for an encrypted-key connection, then connect supplying
        exactly the secrets sshPilot would, and return the ssh exit code."""
        key = combined_server.enc_key
        # The resolver's combined branch loads the key into ssh-agent; stub that
        # to succeed only when a passphrase is "stored" (keyring-gated), and do
        # the real agent load below so the actual ssh run can use it.
        monkeypatch.setattr(ssh_connection_builder, "ensure_key_in_agent",
                            lambda path, *, force=False, lifetime=0: passphrase_stored)
        auth = _resolve(monkeypatch, passphrase_stored=passphrase_stored,
                        password_stored=password_stored, identity_file=key)
        agent_sock = None
        if passphrase_stored:
            agent.add(key, PASSPHRASE, combined_server.tmp)
            agent_sock = agent.sock
        sshpass_pw = auth.password if auth.use_sshpass else None
        return _ssh_connect(combined_server.port, key, agent_sock=agent_sock,
                            sshpass_password=sshpass_pw)

    def test_both_stored_succeeds_combined_auth(self, monkeypatch, combined_server, agent):
        # Combined auth now satisfied: key via agent (passphrase) + password via
        # sshpass → full publickey+password handshake.
        rc = self._drive(monkeypatch, combined_server, agent,
                         passphrase_stored=True, password_stored=True)
        assert rc == 0

    def test_both_stored_wrong_password_fails_combined_auth(self, monkeypatch, combined_server, agent):
        key = combined_server.enc_key
        monkeypatch.setattr(
            ssh_connection_builder,
            "ensure_key_in_agent",
            lambda path, *, force=False, lifetime=0: True,
        )
        conn = _make_conn(key)
        auth = resolve_native_auth(conn, _make_cm("wrong-password"))
        assert auth.use_sshpass is True
        assert auth.password == "wrong-password"

        agent.add(key, PASSPHRASE, combined_server.tmp)
        rc = _ssh_connect(
            combined_server.port,
            key,
            agent_sock=agent.sock,
            sshpass_password=auth.password,
        )

        assert rc != 0

    def test_only_passphrase_fails_no_password(self, monkeypatch, combined_server, agent):
        # Key usable via agent, but nothing satisfies the mandatory password step.
        rc = self._drive(monkeypatch, combined_server, agent,
                         passphrase_stored=True, password_stored=False)
        assert rc != 0

    def test_only_password_fails_encrypted_key_unusable(self, monkeypatch, combined_server, agent):
        # sshpass supplies the password, but the encrypted key isn't in the agent
        # and no passphrase is supplied → pubkey step fails first.
        rc = self._drive(monkeypatch, combined_server, agent,
                         passphrase_stored=False, password_stored=True)
        assert rc != 0

    def test_neither_stored_fails(self, monkeypatch, combined_server, agent):
        rc = self._drive(monkeypatch, combined_server, agent,
                         passphrase_stored=False, password_stored=False)
        assert rc != 0

    def test_pubkey_only_host_ignores_wrong_saved_password(self, monkeypatch, combined_server, agent):
        key = combined_server.enc_key
        authorized = [_authorized_blob(key, PASSPHRASE)]
        monkeypatch.setattr(
            ssh_connection_builder,
            "lookup_passphrase",
            lambda path: PASSPHRASE if path == key else "",
        )
        monkeypatch.setattr(
            ssh_connection_builder,
            "ensure_key_in_agent",
            lambda path, *, force=False, lifetime=0: True,
        )
        conn = _make_conn(key)
        auth = resolve_native_auth(conn, _make_cm("wrong-password"))
        assert auth.use_sshpass is True
        assert auth.password == "wrong-password"

        agent.add(key, PASSPHRASE, combined_server.tmp)
        with _auth_server(lambda: _PublicKeyOnlyServer(authorized)) as server:
            rc = _ssh_connect(
                server.port,
                key,
                agent_sock=agent.sock,
                sshpass_password=auth.password,
                preferred_auth="publickey,password",
            )

        assert rc == 0

    def test_password_only_host_succeeds_with_saved_password(self, combined_server):
        with _auth_server(lambda: _PasswordOnlyServer(PASSWORD)) as server:
            rc = _ssh_connect(
                server.port,
                key_path=None,
                sshpass_password=PASSWORD,
                preferred_auth="password",
                pubkey_auth=False,
                keyboard_interactive_auth=False,
            )

        assert rc == 0

    def test_publickey_then_keyboard_interactive_password_succeeds(
        self, combined_server, agent
    ):
        key = combined_server.enc_key
        authorized = [_authorized_blob(key, PASSPHRASE)]
        agent.add(key, PASSPHRASE, combined_server.tmp)

        with _auth_server(
            lambda: _PublicKeyInteractiveServer(
                authorized,
                "Password: ",
                PASSWORD,
            )
        ) as server:
            rc = _ssh_connect(
                server.port,
                key,
                agent_sock=agent.sock,
                sshpass_password=PASSWORD,
                preferred_auth="publickey,keyboard-interactive,password",
            )

        assert rc == 0

    def test_publickey_then_keyboard_interactive_otp_is_not_answered_by_saved_password(
        self, combined_server, agent
    ):
        key = combined_server.enc_key
        authorized = [_authorized_blob(key, PASSPHRASE)]
        agent.add(key, PASSPHRASE, combined_server.tmp)

        with _auth_server(
            lambda: _PublicKeyInteractiveServer(
                authorized,
                "Verification code: ",
                OTP_CODE,
            )
        ) as server:
            rc = _ssh_connect(
                server.port,
                key,
                agent_sock=agent.sock,
                sshpass_password=PASSWORD,
                preferred_auth="publickey,keyboard-interactive,password",
                timeout=8,
            )

        assert rc != 0

    # ---- positive controls (prove the server + the combined path work) ----

    def test_unencrypted_key_with_password_succeeds(self, monkeypatch, combined_server, agent):
        """Resolver's combined-auth path (key + sshpass) works when the key
        needs no passphrase: pubkey via the plain key, password via sshpass."""
        auth = _resolve(monkeypatch, passphrase_stored=False, password_stored=True,
                        identity_file=combined_server.plain_key)
        assert auth.use_sshpass is True
        rc = _ssh_connect(combined_server.port, combined_server.plain_key,
                          sshpass_password=auth.password)
        assert rc == 0

    def test_both_secrets_supplied_together_succeeds(self, combined_server, agent):
        """The shape a combined-auth fix needs: encrypted key in the agent
        (passphrase) AND password via sshpass → full publickey+password handshake."""
        agent.add(combined_server.enc_key, PASSPHRASE, combined_server.tmp)
        rc = _ssh_connect(combined_server.port, combined_server.enc_key,
                          agent_sock=agent.sock, sshpass_password=PASSWORD)
        assert rc == 0
