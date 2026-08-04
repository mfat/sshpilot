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


def test_key_passphrase_roundtrip(provider):
    prov, _manager, _records = provider
    assert prov.store_key_passphrase("/home/u/.ssh/id_ed25519", "pp") is True
    assert prov.lookup_key_passphrase("/home/u/.ssh/id_ed25519") == "pp"
    assert prov.delete_key_passphrase("/home/u/.ssh/id_ed25519") is True
    assert prov.lookup_key_passphrase("/home/u/.ssh/id_ed25519") is None


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
