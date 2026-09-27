"""Tests for the daemon connection secret provider."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.daemon.connection_secret_provider import (  # noqa: E402
    DaemonConnectionSecretProvider,
)
from sshpilot.core.connections.models import ConnectionRecord  # noqa: E402
from sshpilot.credential_model import (  # noqa: E402
    canonical_password_host,
    password_host_candidates,
)
from sshpilot.secret_storage import password_spec  # noqa: E402


class FakeSecretManager:
    """In-memory backend stand-in for the secret subsystem."""

    def __init__(self) -> None:
        self._values: Dict[tuple, str] = {}
        self.stored: list = []
        self.deleted: list = []

    def store(self, spec, secret: str) -> bool:
        account = str(getattr(spec, "keyring_account", ""))
        self._values[account] = secret
        self.stored.append((account, secret))
        return True

    def lookup(self, spec) -> Optional[str]:
        return self._values.get(str(getattr(spec, "keyring_account", "")))

    def delete(self, spec) -> bool:
        account = str(getattr(spec, "keyring_account", ""))
        if account in self._values:
            del self._values[account]
            self.deleted.append(account)
            return True
        return False


def _record(nickname="web", **kwargs) -> ConnectionRecord:
    return ConnectionRecord(
        id=kwargs.pop("id", nickname),
        nickname=nickname,
        hostname=kwargs.pop("hostname", "example.com"),
        username=kwargs.pop("username", "alice"),
        port=kwargs.pop("port", 22),
        protocol=kwargs.pop("protocol", "ssh"),
        host=kwargs.pop("host", ""),
        data=kwargs.pop("data", {}),
    )


@pytest.fixture
def provider():
    records = {"web": _record()}
    manager = FakeSecretManager()

    def factory():
        return manager

    prov = DaemonConnectionSecretProvider(
        lambda cid: records.get(cid),
        secret_manager_factory=factory,
    )
    return prov, manager, records


def test_lookup_connection_password_missing_connection(provider):
    prov, _manager, _records = provider
    assert prov.lookup_connection_password("missing") is None


def test_lookup_connection_password_roundtrip(provider):
    prov, manager, _records = provider
    assert prov.store_connection_password("web", "hunter2") is True
    stored_key = next(iter(manager._values))
    assert stored_key == "alice@example.com"
    assert prov.lookup_connection_password("web") == "hunter2"


def test_session_password_is_memory_only_and_clears_input(provider):
    prov, manager, _records = provider
    secret = bytearray(b"use-once")

    assert prov.set_session_connection_password("web", secret) is True
    assert secret == bytearray()
    assert prov.lookup_connection_password("web") == "use-once"
    assert manager.stored == []
    assert "use-once" not in repr(prov)
    assert "use-once" not in repr(manager)


def test_persistent_store_clears_stale_session_password(provider):
    prov, manager, _records = provider
    assert prov.set_session_connection_password("web", bytearray(b"temporary")) is True
    assert prov.store_connection_password("web", "persistent") is True
    assert prov.lookup_connection_password("web") == "persistent"
    assert manager.lookup(password_spec("example.com", "alice")) == "persistent"


def test_missing_record_and_invalid_type_still_wipe_bytearray(provider):
    prov, _manager, records = provider
    missing = bytearray(b"missing")
    assert prov.set_session_connection_password("gone", missing) is False
    assert missing == bytearray()
    records.pop("web")
    invalid = bytearray(b"invalid")
    assert prov.set_session_connection_password("web", invalid) is False
    assert invalid == bytearray()


def test_session_password_can_be_explicitly_cleared_and_delete_clears_it(provider):
    prov, _manager, _records = provider
    assert prov.set_session_connection_password("web", bytearray(b"temporary"))
    assert prov.clear_session_connection_password("web") is True
    assert prov.lookup_connection_password("web") is None
    assert prov.set_session_connection_password("web", bytearray(b"temporary"))
    assert prov.delete_connection_password("web") is True
    assert prov.lookup_connection_password("web") is None


def test_session_password_invalid_or_cancelled_input_stores_nothing(provider):
    prov, manager, _records = provider
    assert prov.set_session_connection_password("web", bytearray()) is False
    assert prov.set_session_connection_password("web", bytearray(b"bad\x00value")) is False
    assert manager.stored == []


def test_store_cleans_previous_host(provider):
    prov, manager, records = provider
    records["web"] = _record(hostname="new.example.com")
    prov.store_connection_password(
        "web",
        "s3cret",
        previous_hostname="old.example.com",
        previous_username="alice",
    )
    assert "alice@new.example.com" in manager._values
    # The previous host must have been cleaned up.
    assert "alice@old.example.com" not in manager._values


def test_delete_connection_password(provider):
    prov, manager, _records = provider
    prov.store_connection_password("web", "hunter2")
    assert prov.delete_connection_password("web") is True
    assert "alice@example.com" not in manager._values


def test_delete_is_idempotent(provider):
    prov, _manager, _records = provider
    assert prov.delete_connection_password("missing") is True


@pytest.fixture
def keyring_backend(monkeypatch):
    """Serve the key-passphrase path from an in-memory secret manager.

    ``store/lookup/delete_key_passphrase`` deliberately bypass
    ``secret_manager_factory`` and call into ``askpass_utils``, which resolves
    the *real* backend via ``secret_storage.get_secret_manager``. Left alone
    that writes a live secret into the developer's keyring, and on CI -- where
    no keyring backend is installed -- ``store`` returns False and the test
    fails. Faking at the manager boundary rather than stubbing out
    ``askpass_utils`` keeps its own key-path normalization under test.
    """
    import sshpilot.secret_storage as secret_storage

    manager = FakeSecretManager()
    monkeypatch.setattr(secret_storage, "get_secret_manager", lambda: manager)
    return manager


def test_key_passphrase_roundtrip(provider, keyring_backend):
    prov, _manager, _records = provider
    assert prov.store_key_passphrase("/home/u/.ssh/id_ed25519", "pp") is True
    assert prov.lookup_key_passphrase("/home/u/.ssh/id_ed25519") == "pp"
    assert prov.delete_key_passphrase("/home/u/.ssh/id_ed25519") is True
    assert prov.lookup_key_passphrase("/home/u/.ssh/id_ed25519") is None


def test_delete_missing_key_passphrase_is_idempotent(provider, monkeypatch):
    """A non-empty absent key path is already in the requested state.

    Idempotent by contract: ``lookup_key_passphrase`` finds nothing, so
    ``clear_passphrase`` is not required and the deletion succeeds.
    """
    import sshpilot.askpass_utils as askpass_utils

    prov, _manager, _records = provider
    monkeypatch.setattr(askpass_utils, "lookup_passphrase", lambda _p: "")

    def _unexpected(*_args, **_kwargs):
        raise AssertionError("clear_passphrase must not run for an absent passphrase")

    monkeypatch.setattr(askpass_utils, "clear_passphrase", _unexpected)

    assert prov.delete_key_passphrase("/tmp/missing-key") is True


def test_plugin_secret_namespaced(provider):
    prov, manager, _records = provider
    assert prov.store_plugin_secret("docker", "token", "abc") is True
    assert prov.get_plugin_secret("docker", "token") == "abc"
    # Another plugin cannot read it.
    assert prov.get_plugin_secret("mosh", "token") is None
    assert prov.delete_plugin_secret("docker", "token") is True
    assert prov.get_plugin_secret("docker", "token") is None


def test_secret_values_not_in_repr(provider):
    prov, manager, records = provider
    prov.store_connection_password("web", "hunter2")
    assert "hunter2" not in repr(prov)
    assert "hunter2" not in repr(manager)


def test_resolver_required():
    with pytest.raises(ValueError):
        DaemonConnectionSecretProvider(None)


# --- pure credential-model helpers -------------------------------------------


def test_canonical_password_host_precedence():
    # hostname takes precedence over host and nickname.
    assert (
        canonical_password_host(
            {"hostname": "h.example", "host": "alias", "nickname": "nick"}
        )
        == "h.example"
    )
    # host is used when hostname is empty.
    assert (
        canonical_password_host({"hostname": "", "host": "alias", "nickname": "nick"})
        == "alias"
    )
    # nickname is the final fallback.
    assert (
        canonical_password_host({"hostname": "", "host": "", "nickname": "nick"})
        == "nick"
    )


def test_password_host_candidates_ordered_and_deduplicated():
    conn = {"hostname": "h.example", "host": "alias", "nickname": "nick"}
    assert password_host_candidates(conn) == ["h.example", "alias", "nick"]
    dup = {"hostname": "h.example", "host": "h.example", "nickname": "h.example"}
    assert password_host_candidates(dup) == ["h.example"]


# --- migrated scenarios from the retired connection-password tests -----------


def test_store_uses_canonical_host_and_removes_legacy_nickname_alias(provider):
    prov, manager, records = provider
    records["web"] = _record(nickname="bnick", hostname="b.example", username="bob")
    manager.store(password_spec("bnick", "bob"), "legacy")

    assert prov.store_connection_password("web", "new-pw") is True
    assert manager.lookup(password_spec("b.example", "bob")) == "new-pw"
    assert manager.lookup(password_spec("bnick", "bob")) is None


def test_store_cleans_complete_previous_identity(provider):
    prov, manager, records = provider
    records["web"] = _record(
        nickname="new-name", hostname="new.example", username="new-user"
    )
    manager.store(password_spec("old.example", "old-user"), "old-pw")

    assert prov.store_connection_password(
        "web",
        "new-pw",
        previous_hostname="old.example",
        previous_username="old-user",
    ) is True
    assert manager.lookup(password_spec("new.example", "new-user")) == "new-pw"
    assert manager.lookup(password_spec("old.example", "old-user")) is None


def test_lookup_migrates_legacy_nickname_alias(provider):
    prov, manager, records = provider
    records["web"] = _record(nickname="bnick", hostname="b.example", username="bob")
    manager.store(password_spec("bnick", "bob"), "legacy")

    assert prov.lookup_connection_password("web") == "legacy"
    # The canonical account is created and the legacy alias is removed.
    assert manager.lookup(password_spec("b.example", "bob")) == "legacy"
    assert manager.lookup(password_spec("bnick", "bob")) is None


def test_delete_clears_all_candidate_aliases(provider):
    prov, manager, records = provider
    records["web"] = _record(
        nickname="bnick", hostname="b.example", host="alias", username="bob"
    )
    manager.store(password_spec("b.example", "bob"), "x")
    manager.store(password_spec("alias", "bob"), "y")
    manager.store(password_spec("bnick", "bob"), "z")

    assert prov.delete_connection_password("web") is True
    assert manager.lookup(password_spec("b.example", "bob")) is None
    assert manager.lookup(password_spec("alias", "bob")) is None
    assert manager.lookup(password_spec("bnick", "bob")) is None


# -- one saved login shared by several connections ---------------------------
#
# The keyring entry is keyed only on ``username@host``: an SSH and an RDP
# connection to one Windows host, or two ports on one machine, read the same
# entry. Clearing one connection's password must not take it from the others.


def _shared(*records):
    by_id = {record.id: record for record in records}
    metadata: Dict[str, dict] = {}
    manager = FakeSecretManager()

    def update(connection_id, values):
        current = metadata.setdefault(connection_id, {})
        for key, value in values.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value

    provider = DaemonConnectionSecretProvider(
        by_id.get,
        secret_manager_factory=lambda: manager,
        records=lambda: tuple(by_id.values()),
        metadata_lookup=lambda connection_id: metadata.get(connection_id, {}),
        metadata_update=update,
    )
    return provider, manager, metadata


def _windows_pair():
    return _shared(
        _record("win-ssh", hostname="10.0.0.5"),
        _record("win-rdp", hostname="10.0.0.5", protocol="rdp", port=3389),
    )


def test_clearing_a_shared_password_keeps_it_for_the_other_connection():
    provider, manager, _metadata = _windows_pair()
    assert provider.store_connection_password("win-rdp", "pw")

    assert provider.delete_connection_password("win-rdp")

    assert manager.deleted == []
    assert provider.lookup_connection_password("win-ssh") == "pw"
    assert provider.lookup_connection_password("win-rdp") is None
    assert provider.has_connection_password("win-rdp") is False


def test_clearing_a_password_nobody_else_reads_deletes_it():
    provider, manager, _metadata = _shared(_record("web"), _record("other", hostname="other.example"))
    provider.store_connection_password("web", "pw")

    provider.delete_connection_password("web")

    assert manager.deleted == ["alice@example.com"]


def test_a_cleared_connection_does_not_pick_up_a_sibling_s_later_save():
    provider, _manager, _metadata = _windows_pair()
    provider.delete_connection_password("win-rdp")

    provider.store_connection_password("win-ssh", "pw")

    assert provider.lookup_connection_password("win-rdp") is None


def test_saving_a_password_again_undoes_the_clear():
    provider, _manager, metadata = _windows_pair()
    provider.store_connection_password("win-ssh", "pw")
    provider.delete_connection_password("win-rdp")

    provider.store_connection_password("win-rdp", "pw2")

    assert "use_saved_login" not in metadata.get("win-rdp", {})
    assert provider.lookup_connection_password("win-rdp") == "pw2"
    # Still one entry: saving through RDP replaces SSH's (keyed on user@host).
    assert provider.lookup_connection_password("win-ssh") == "pw2"


def test_the_last_connection_to_clear_a_shared_entry_deletes_it():
    provider, manager, _metadata = _windows_pair()
    provider.store_connection_password("win-ssh", "pw")
    provider.delete_connection_password("win-ssh")
    assert manager.deleted == []

    provider.delete_connection_password("win-rdp")

    assert manager.deleted == ["alice@10.0.0.5"]


def test_alias_cleanup_on_save_spares_another_connection_s_entry():
    """``box`` has the nickname ``gw``, a legacy alias its save cleans up --
    but ``gw`` is also another connection's real host."""
    provider, manager, _metadata = _shared(
        _record("gw", hostname="gw"),
        _record("box", hostname="10.0.0.9", host="gw"),
    )
    provider.store_connection_password("gw", "gateway-pw")

    provider.store_connection_password("box", "box-pw")

    assert "alice@gw" not in manager.deleted
    assert provider.lookup_connection_password("gw") == "gateway-pw"


def test_unknown_other_connections_are_treated_as_sharing():
    manager = FakeSecretManager()
    records = {"web": _record()}

    def failing():
        raise RuntimeError("store unavailable")

    provider = DaemonConnectionSecretProvider(
        records.get,
        secret_manager_factory=lambda: manager,
        records=failing,
        metadata_lookup=lambda _cid: {},
        metadata_update=lambda _cid, _values: None,
    )
    provider.store_connection_password("web", "pw")

    provider.delete_connection_password("web")

    assert manager.deleted == []
