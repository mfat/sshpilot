"""Secret policy and plugin contract smoke tests."""
from __future__ import annotations

from sshpilot.core.plugins import API_VERSION, Capability, EventBus, Events, SpawnSpec
from sshpilot.core.secrets import (
    SecretDecisionKind,
    decide_unlock,
    normalize_backend_name,
    platform_default_order,
    resolve_lookup_order,
    resolve_store_order,
)
from sshpilot.core.import_export import validate_connection_export_payload


def test_normalize_legacy_vaultwarden():
    assert normalize_backend_name("vaultwarden") == "bitwarden"
    assert normalize_backend_name("AUTO") == "auto"


def test_platform_default_order_linux():
    assert platform_default_order(platform="linux") == ["libsecret", "keyring"]
    assert platform_default_order(platform="darwin") == ["keyring"]


def test_lookup_order_auto_vs_explicit():
    assert resolve_store_order("bitwarden") == ["bitwarden"]
    assert resolve_lookup_order("auto", platform="linux")[0] == "libsecret"
    assert resolve_lookup_order("agent") == ["agent"]


def test_decide_unlock():
    ready = decide_unlock(
        backend="bitwarden", session_backed=True, is_unlocked=True, available=True
    )
    assert ready.kind == SecretDecisionKind.READY
    locked = decide_unlock(
        backend="bitwarden", session_backed=True, is_unlocked=False, available=True
    )
    assert locked.kind == SecretDecisionKind.UNLOCK_REQUIRED


def test_plugin_contracts_headless():
    assert API_VERSION[0] == 1
    assert Capability.FILE_TRANSFER.value == "file-transfer"
    bus = EventBus()
    seen = []
    bus.subscribe(Events.APP_STARTED, lambda p: seen.append(p), plugin_id="t")
    bus.emit(Events.APP_STARTED, {"ok": True})
    assert seen == [{"ok": True}]
    spec = SpawnSpec(argv=["ssh", "host"], env={})
    assert spec.argv[0] == "ssh"


def test_import_payload_validation():
    report = validate_connection_export_payload(
        {"connections": [{"nickname": "GoodHost", "host": "h"}]}
    )
    assert report.ok is True
    bad = validate_connection_export_payload(
        {"connections": [{"nickname": "Bad Host"}]}
    )
    assert bad.ok is False
