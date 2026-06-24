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
