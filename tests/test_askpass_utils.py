import os

import pytest


class DummySecretModule:
    class SchemaAttributeType:
        STRING = object()

    class SchemaFlags:
        NONE = 0

    class ServiceFlags:
        NONE = 0

    class Service:
        @staticmethod
        def get_sync(_flags):
            return object()

    COLLECTION_DEFAULT = object()

    class Schema:
        @staticmethod
        def new(*_args, **_kwargs):
            return object()

    store = {}

    @classmethod
    def password_store_sync(cls, _schema, attributes, _collection, _label, secret, _cancellable):
        cls.store[attributes["key_path"]] = secret

    @classmethod
    def password_lookup_sync(cls, _schema, attributes, _cancellable):
        return cls.store.get(attributes["key_path"])

    @classmethod
    def password_clear_sync(cls, _schema, attributes, _cancellable):
        return 1 if cls.store.pop(attributes["key_path"], None) is not None else 0


@pytest.fixture(autouse=True)
def _reset_dummy_store():
    DummySecretModule.store = {}
    yield
    DummySecretModule.store = {}


def test_lookup_passphrase_handles_home_relative_alias(monkeypatch, tmp_path):
    from sshpilot import askpass_utils, secret_storage

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SSHPILOT_SECRET_BACKEND", raising=False)

    # Storage now lives in secret_storage; patch the backend layer there and
    # rebuild the manager singleton against the dummy libsecret.
    monkeypatch.setattr(secret_storage, "Secret", DummySecretModule, raising=False)
    monkeypatch.setattr(secret_storage, "keyring", None, raising=False)
    monkeypatch.setattr(secret_storage, "is_macos", lambda: False, raising=False)
    monkeypatch.setattr(secret_storage, "_SCHEMA", None, raising=False)
    monkeypatch.setattr(secret_storage, "_MANAGER", None, raising=False)

    key_path = "~/.ssh/example_key"
    absolute_path = os.path.realpath(os.path.expanduser(key_path))

    assert askpass_utils.store_passphrase(key_path, "super-secret")
    assert DummySecretModule.store == {absolute_path: "super-secret"}

    assert askpass_utils.lookup_passphrase(absolute_path) == "super-secret"
    assert askpass_utils.lookup_passphrase(key_path) == "super-secret"


def test_clear_passphrase_removes_legacy_alias(monkeypatch, tmp_path):
    from sshpilot import askpass_utils, secret_storage

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("SSHPILOT_SECRET_BACKEND", raising=False)

    monkeypatch.setattr(secret_storage, "Secret", DummySecretModule, raising=False)
    monkeypatch.setattr(secret_storage, "keyring", None, raising=False)
    monkeypatch.setattr(secret_storage, "is_macos", lambda: False, raising=False)
    monkeypatch.setattr(secret_storage, "_SCHEMA", None, raising=False)
    monkeypatch.setattr(secret_storage, "_MANAGER", None, raising=False)

    legacy_key_path = "~/.ssh/key_symlink"
    canonical_path = os.path.realpath(os.path.expanduser(legacy_key_path))

    DummySecretModule.store = {legacy_key_path: "legacy-secret"}

    assert askpass_utils.clear_passphrase(canonical_path)
    assert DummySecretModule.store == {}


def test_sudo_password_routes_through_secret_manager(monkeypatch):
    # Sudo passwords must go through the selected secret backend (not straight to
    # libsecret/keyring) and use the legacy sudo:user@host account key.
    from sshpilot import askpass_utils, secret_storage

    captured = {}

    class FakeManager:
        def store(self, spec, secret):
            captured['store'] = (spec.keyring_account, spec.attributes['type'], secret)
            return True

        def lookup(self, spec):
            captured['lookup'] = spec.keyring_account
            return 'stored-sudo-pw'

        def delete(self, spec):
            captured['delete'] = spec.keyring_account
            return True

    monkeypatch.setattr(secret_storage, 'get_secret_manager', lambda: FakeManager())

    assert askpass_utils.store_sudo_password('host', 'user', 'pw') is True
    assert captured['store'] == ('sudo:user@host', 'sudo_password', 'pw')

    assert askpass_utils.lookup_sudo_password('host', 'user') == 'stored-sudo-pw'
    assert captured['lookup'] == 'sudo:user@host'

    assert askpass_utils.clear_sudo_password('host', 'user') is True
    assert captured['delete'] == 'sudo:user@host'


def test_sudo_password_empty_host_short_circuits(monkeypatch):
    from sshpilot import askpass_utils, secret_storage

    def _boom():
        raise AssertionError("secret manager must not be consulted without a host")

    monkeypatch.setattr(secret_storage, 'get_secret_manager', _boom)
    assert askpass_utils.store_sudo_password('', 'user', 'pw') is False
    assert askpass_utils.lookup_sudo_password('', 'user') == ''
    assert askpass_utils.clear_sudo_password('', 'user') is False


def test_lookup_via_main_app_roundtrip(monkeypatch, tmp_path):
    # The askpass subprocess resolves a passphrase from the main app over the IPC
    # socket (warm cache) instead of cold-loading the vault itself.
    import json
    import socket
    import threading
    from sshpilot import askpass_utils

    sock_path = str(tmp_path / "askpass.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    received = {}

    def _serve():
        conn, _ = server.accept()
        with conn:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                data += chunk
            received['req'] = json.loads(data.split(b"\n", 1)[0].decode())
            conn.sendall(
                (json.dumps({"ok": True, "passphrase": "sekret"}) + "\n").encode())

    threading.Thread(target=_serve, daemon=True).start()
    monkeypatch.setenv("SSHPILOT_ASKPASS_SOCKET", sock_path)
    monkeypatch.setenv("SSHPILOT_ASKPASS_TOKEN", "tok123")
    try:
        value = askpass_utils._lookup_via_main_app("/home/u/.ssh/id", lambda *_a: None)
    finally:
        server.close()

    assert value == "sekret"
    assert received['req'] == {
        "token": "tok123", "type": "lookup", "key_path": "/home/u/.ssh/id",
    }


def test_lookup_via_main_app_no_socket(monkeypatch):
    from sshpilot import askpass_utils
    monkeypatch.delenv("SSHPILOT_ASKPASS_SOCKET", raising=False)
    monkeypatch.delenv("SSHPILOT_ASKPASS_TOKEN", raising=False)
    assert askpass_utils._lookup_via_main_app("/k", lambda *_a: None) is None


def test_handle_askpass_cli_resolves_via_backend_first(monkeypatch):
    # The askpass subprocess resolves via the selected backend first (Bitwarden -> a
    # targeted `bw` lookup, libsecret/keyring -> in-process). The main-app IPC is only a
    # fallback, so a backend hit must NOT reach it.
    from sshpilot import askpass_utils

    monkeypatch.delenv("SSHPILOT_SESSION_PASSPHRASE_FILE", raising=False)
    monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _kp: "viabackend")

    def _must_not_run(*_a, **_k):
        raise AssertionError("IPC fallback must not run when the backend resolves it")

    monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", _must_not_run)

    prompt = "Enter passphrase for key '/home/u/.ssh/id_ed25519':"
    assert askpass_utils.handle_askpass_cli(prompt) == "viabackend"


def test_handle_askpass_cli_ipc_fallback(monkeypatch):
    # When the backend finds nothing, fall back to the main-app IPC.
    from sshpilot import askpass_utils

    monkeypatch.delenv("SSHPILOT_SESSION_PASSPHRASE_FILE", raising=False)
    monkeypatch.delenv("SSHPILOT_SECRET_BACKEND", raising=False)   # auto = non-session
    monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _kp: "")
    monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", lambda _kp, _log: "viaipc")

    prompt = "Enter passphrase for key '/home/u/.ssh/id_ed25519':"
    assert askpass_utils.handle_askpass_cli(prompt) == "viaipc"


def test_handle_askpass_cli_session_backend_prefers_ipc(monkeypatch):
    # For a session vault (Bitwarden) the warm-cache IPC lookup runs BEFORE the local
    # `bw` lookup (which would cold-start the CLI), so a cache hit avoids the spawn.
    from sshpilot import askpass_utils

    monkeypatch.delenv("SSHPILOT_SESSION_PASSPHRASE_FILE", raising=False)
    monkeypatch.setenv("SSHPILOT_SECRET_BACKEND", "bitwarden")

    def _must_not_run(_kp):
        raise AssertionError("local bw lookup must not run when IPC resolves it")

    monkeypatch.setattr(askpass_utils, "lookup_passphrase", _must_not_run)
    monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", lambda _kp, _log: "viaipc")

    prompt = "Enter passphrase for key '/home/u/.ssh/id_ed25519':"
    assert askpass_utils.handle_askpass_cli(prompt) == "viaipc"


def test_handle_askpass_cli_session_backend_falls_back_to_local(monkeypatch):
    # Session vault but the main app is unreachable (IPC returns None) -> local lookup.
    from sshpilot import askpass_utils

    monkeypatch.delenv("SSHPILOT_SESSION_PASSPHRASE_FILE", raising=False)
    monkeypatch.setenv("SSHPILOT_SECRET_BACKEND", "bitwarden")
    monkeypatch.setattr(askpass_utils, "_lookup_via_main_app", lambda _kp, _log: None)
    monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _kp: "vialocal")

    prompt = "Enter passphrase for key '/home/u/.ssh/id_ed25519':"
    assert askpass_utils.handle_askpass_cli(prompt) == "vialocal"
