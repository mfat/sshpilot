"""Secret-backend RPCs that can block on a protected interaction (master
password / 2FA / API key prompt) must use a request timeout long enough to
outlast the daemon's interaction wait — not the generic 5s default.

Regression: with the 5s default, a user simply taking more than 5 seconds to
notice/answer the unlock prompt caused the client to declare the daemon
transport dead and start reconnecting, which then repeated forever (each new
connection's ``daemon.get_operation_mode`` also raced the same short
timeout) — observed as `secrets.unlock` timing out every ~5s in a loop that
never recovered, while the kdbx master-password dialog sat waiting.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot.api.daemon_client import (
    DEFAULT_REQUEST_TIMEOUT,
    DaemonClient,
    SECRET_BACKEND_REQUEST_TIMEOUT,
    SECRET_INTERACTION_REQUEST_TIMEOUT,
    SECRET_TRANSFER_IMPORT_REQUEST_TIMEOUT,
)


def _client_for_recording():
    """A DaemonClient whose ``_request`` is stubbed to just record its kwargs,
    with capability checks always satisfied — no real transport needed."""
    client = DaemonClient.__new__(DaemonClient)
    client.get_capabilities = lambda: SimpleNamespace(supports=lambda _capability: True)
    calls = []

    def fake_request(method, params, **kwargs):
        calls.append((method, params, kwargs))
        return {}

    client._request = fake_request
    return client, calls


@pytest.mark.parametrize(
    "call",
    [
        lambda c: c.unlock_secrets(),
        lambda c: c.bitwarden_login("user@example.com"),
        lambda c: c.bitwarden_api_key_login("client-id"),
        lambda c: c.bitwarden_sso_login(),
        lambda c: c.bitwarden_unlock(),
        lambda c: c.rbw_unlock(),
        lambda c: c.keepassxc_create_database("/tmp/x.kdbx"),
        lambda c: c.keepassxc_unlock(),
        lambda c: c.remember_master_password(),
        lambda c: c.export_secret_backup(
            destination="/tmp/backup.spbk",
            options={"encrypted": True},
        ),
        lambda c: c.preview_backup(source="/tmp/backup.spbk"),
    ],
    ids=[
        "unlock_secrets",
        "bitwarden_login",
        "bitwarden_api_key_login",
        "bitwarden_sso_login",
        "bitwarden_unlock",
        "rbw_unlock",
        "keepassxc_create_database",
        "keepassxc_unlock",
        "remember_master_password",
        "export_secret_backup",
        "preview_backup",
    ],
)
def test_interactive_secret_rpc_uses_the_long_timeout(call, monkeypatch):
    from sshpilot.api.transport import codec

    # Every _from_wire codec these methods call is exercised with a bare {}
    # result — stub them all to a harmless stand-in so this test only
    # exercises the timeout plumbing, not wire decoding.
    for name in dir(codec):
        if name.endswith("_from_wire"):
            monkeypatch.setattr(codec, name, lambda _result: SimpleNamespace())

    client, calls = _client_for_recording()
    call(client)

    assert len(calls) == 1
    _method, _params, kwargs = calls[0]
    assert kwargs.get("request_timeout") == SECRET_INTERACTION_REQUEST_TIMEOUT


def test_secret_backup_import_uses_multi_interaction_timeout(monkeypatch):
    from sshpilot.api.transport import codec

    monkeypatch.setattr(
        codec,
        "secret_transfer_result_from_wire",
        lambda _result: SimpleNamespace(),
    )

    client, calls = _client_for_recording()
    client.import_secret_backup(source="/tmp/backup.spbk")

    assert len(calls) == 1
    method, _params, kwargs = calls[0]
    assert method == "secrets.transfer.import"
    assert kwargs.get("request_timeout") == SECRET_TRANSFER_IMPORT_REQUEST_TIMEOUT


def test_non_interactive_secret_rpc_keeps_the_default_timeout():
    """A metadata read never blocks on user interaction — it must not pay the
    long timeout, or a genuinely dead daemon takes 130s to be noticed."""
    client, calls = _client_for_recording()

    from sshpilot.api.transport import codec

    orig = codec.secret_backend_state_from_wire
    try:
        codec.secret_backend_state_from_wire = lambda _result: SimpleNamespace()
        client.get_secret_state()
    finally:
        codec.secret_backend_state_from_wire = orig

    assert len(calls) == 1
    _method, _params, kwargs = calls[0]
    assert kwargs.get("request_timeout") is None


# ---------------------------------------------------------------------------
# A backend RPC that never prompts still waits on an external vault CLI.
# Regression: saving a connection password with Bitwarden selected ran
# ``bw edit item`` for 8.3s, so the 5s default failed the transport mid-write
# — and because the write had landed, the dialog reported that secure storage
# had "rejected" a password it had just saved.
# ---------------------------------------------------------------------------


def _timeout_client(timeout: float = DEFAULT_REQUEST_TIMEOUT):
    client = DaemonClient.__new__(DaemonClient)
    client._timeout = timeout
    return client


@pytest.mark.parametrize(
    "method",
    [
        "connections.store_password",
        "connections.delete_password",
        "connections.has_password",
        "connections.reveal_password",
        "connections.store_passphrase",
        "connections.delete_passphrase",
        "connections.has_passphrase",
        "connections.reveal_passphrase",
        "connections.store_plugin_secret",
        "connections.get_plugin_secret",
        "connections.delete_plugin_secret",
        "secrets.state.get",
        "secrets.backends.get",
        "secrets.bitwarden.status",
        "secrets.bitwarden.sync",
        "secrets.transfer.list_bitwarden",
    ],
)
def test_vault_backed_rpc_gets_the_backend_timeout(method):
    assert _timeout_client()._default_timeout_for(method) == (
        SECRET_BACKEND_REQUEST_TIMEOUT
    )


@pytest.mark.parametrize(
    "method",
    [
        "connections.list",
        "connections.update",
        "connections.snapshot",
        "sessions.list",
        "system.handshake",
    ],
)
def test_ordinary_rpc_keeps_the_short_default(method):
    """Only the vault-backed calls pay the longer wait — everything else must
    still notice an unresponsive daemon quickly."""
    assert _timeout_client()._default_timeout_for(method) == DEFAULT_REQUEST_TIMEOUT


def test_a_longer_configured_client_timeout_is_never_shortened():
    client = _timeout_client(timeout=SECRET_BACKEND_REQUEST_TIMEOUT * 2)
    assert client._default_timeout_for("connections.store_password") == (
        SECRET_BACKEND_REQUEST_TIMEOUT * 2
    )
