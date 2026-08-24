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
    DaemonClient,
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
