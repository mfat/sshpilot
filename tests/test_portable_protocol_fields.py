"""The backup allowlist must list every non-secret built-in protocol field.

``core`` may not import the plugin package, so ``_PORTABLE_PROTOCOL_FIELDS``
duplicates each built-in backend's ``connection_fields()`` by hand. A field
missing there is silently dropped from backups; this pins the two together.
"""

import pytest

from sshpilot.api.models.connections import is_sensitive_field_name
from sshpilot.core.connections.repository import _PORTABLE_PROTOCOL_FIELDS
from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import ensure_builtin_protocols

# Captured by the backup's generic columns, not the per-protocol allowlist.
_GENERIC = {"host", "hostname", "port", "username"}


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    import sshpilot.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "_builtins_ensured_for", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    ensure_builtin_protocols()
    return registry_mod.protocol_registry()


def test_every_builtin_protocol_field_survives_a_backup(registry):
    for backend in registry.all():
        protocol = backend.protocol_id
        if protocol == "ssh":
            continue
        declared = {
            spec.key
            for spec in backend.connection_fields()
            if not is_sensitive_field_name(spec.key)
        } - _GENERIC
        assert set(_PORTABLE_PROTOCOL_FIELDS.get(protocol, ())) == declared, protocol
