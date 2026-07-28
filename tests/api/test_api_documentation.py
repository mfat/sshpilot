import re
from pathlib import Path

from sshpilot.api import Capability, ErrorCode, EventType, PROTOCOL_VERSION
from sshpilot.api.client import SshPilotClient


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "api"


def _read(name):
    return (DOCS / name).read_text(encoding="utf-8")


def _markers(text, kind):
    return set(re.findall(rf"<!-- api-{kind}: ([^ ]+)(?: [^>]*)? -->", text))


def test_every_public_client_method_is_documented():
    methods = {
        name
        for name, value in vars(SshPilotClient).items()
        if not name.startswith("_") and callable(value)
    }

    assert _markers(_read("methods.md"), "method") == methods


def test_capabilities_errors_events_and_states_are_documented():
    assert _markers(_read("capabilities.md"), "capability") == {
        item.value for item in Capability
    }
    assert _markers(_read("errors.md"), "error") == {item.value for item in ErrorCode}
    assert _markers(_read("events.md"), "event") == {item.value for item in EventType}
    assert _markers(_read("state-machines.md"), "state") == {
        "ConnectionHealth",
        "ForwardState",
        "InteractionStatus",
        "SessionState",
        "TransferState",
    }


def test_protocol_version_and_changelog_are_documented():
    protocol = _read("protocol-v1.md")

    assert f"<!-- api-version: {PROTOCOL_VERSION} -->" in protocol
    assert "## Unreleased" in _read("CHANGELOG.md")


def test_runtime_capability_markers_match_the_provider(
    fake_manager,
    client_factory,
):
    client = client_factory(fake_manager)
    documented = _markers(_read("capabilities.md"), "runtime-capability")
    supported = {item.value for item in client.get_capabilities().supported}

    assert documented == supported == {Capability.CONNECTIONS_READ.value}
    assert client.list_connections()
    assert client.get_connection(client.list_connections()[0].id)


def test_schema_only_capabilities_are_not_advertised(fake_manager, client_factory):
    client = client_factory(fake_manager)

    assert client.get_capabilities().supported == frozenset(
        {Capability.CONNECTIONS_READ}
    )
    assert all(
        not client.get_capabilities().supports(capability)
        for capability in Capability
        if capability is not Capability.CONNECTIONS_READ
    )


def test_every_generated_public_model_is_documented():
    import json

    snapshot = json.loads(
        (ROOT / "tests/api/snapshots/public_api.json").read_text(encoding="utf-8")
    )
    documented = _markers(
        _read("generated/model-index.md"),
        "model",
    )

    assert documented == set(snapshot["models"])


def test_generated_examples_do_not_contain_live_or_test_secret_data():
    generated = (
        _read("generated/model-index.md")
        + _read("generated/schema.json")
    )

    for forbidden in (
        "/home/",
        "do-not-expose",
        "do-not-expose-either",
        "super-secret",
    ):
        assert forbidden not in generated
    assert "<sensitive value omitted>" in generated


def test_api_relative_markdown_links_resolve():
    link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    missing = []

    for document in DOCS.rglob("*.md"):
        for target in link_pattern.findall(document.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target_path = target.split("#", 1)[0]
            if not target_path:
                continue
            if not (document.parent / target_path).resolve().exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")

    assert not missing, "Broken API documentation links:\n" + "\n".join(missing)
