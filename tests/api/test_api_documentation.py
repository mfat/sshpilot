import importlib.util
import re
from pathlib import Path

from sshpilot.api import Capability, ErrorCode, EventType, PROTOCOL_VERSION
from sshpilot.api.client import SshPilotClient
from sshpilot.daemon.dispatch import DAEMON_METHOD_CAPABILITIES


ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "api"
GENERATOR_PATH = ROOT / "scripts" / "generate_api_artifacts.py"


def _read(name):
    return (DOCS / name).read_text(encoding="utf-8")


def _markers(text, kind):
    return set(re.findall(rf"<!-- api-{kind}: ([^ ]+)(?: [^>]*)? -->", text))


def _load_generator():
    spec = importlib.util.spec_from_file_location("generate_api_artifacts", GENERATOR_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
    client_backend,
):
    client = client_factory(fake_manager)
    marker = (
        "daemon-runtime-capability"
        if client_backend == "daemon"
        else "runtime-capability"
    )
    documented = _markers(_read("capabilities.md"), marker)
    supported = {item.value for item in client.get_capabilities().supported}

    expected = {
        Capability.CONNECTIONS_READ.value,
        Capability.CONNECTIONS_EVENTS.value,
        Capability.CONNECTIONS_WRITE.value,
    }
    if client_backend == "daemon":
        expected.update(
            {
                Capability.SESSIONS_READ.value,
                Capability.SESSIONS_WRITE.value,
                Capability.SESSIONS_EVENTS.value,
                Capability.TERMINAL_OUTPUT.value,
                Capability.TERMINAL_INPUT.value,
                Capability.TERMINAL_RESIZE.value,
                Capability.TERMINAL_REPLAY.value,
            }
        )
    assert documented == supported == expected
    assert client.list_connections()
    assert client.get_connection(client.list_connections()[0].id)


def test_structured_method_status_markers_match_runtime_metadata():
    generator = _load_generator()
    text = _read("methods.md")
    marker_pattern = re.compile(
        r"<!-- api-method-contract: ([^ ]+) "
        r"status=([^ ]+) capability=([^ ]+) -->"
    )
    documented = {
        name: {
            "status": status,
            "capability": None if capability == "none" else capability,
        }
        for name, status, capability in marker_pattern.findall(text)
    }

    assert documented == generator.client_method_contract()


def test_event_and_stable_id_semantics_have_stable_markers():
    events = _read("events.md")
    assert "<!-- api-event-semantics: serial-fifo-v1 -->" in events
    assert (
        "<!-- api-daemon-event-semantics: global-sequence-bounded-v1 -->"
        in events
    )
    assert (
        "<!-- api-connection-id: persisted-uuid-v1 -->"
        in _read("protocol-v1.md")
    )


def test_wire_framing_handshake_and_method_registry_are_documented():
    protocol = _read("protocol-v1.md")
    methods = _read("methods.md")
    marker_pattern = re.compile(
        r"<!-- api-daemon-method: ([^ ]+) capability=([^ ]+) -->"
    )
    documented = {
        name: None if capability == "none" else capability
        for name, capability in marker_pattern.findall(methods)
    }
    expected = {
        name: capability.value if capability is not None else None
        for name, capability in DAEMON_METHOD_CAPABILITIES.items()
    }

    assert documented == expected
    assert "<!-- api-wire-framing: length-prefixed-json-v1 -->" in protocol
    assert (
        "<!-- api-handshake: required-once-before-ordinary-methods -->"
        in protocol
    )


def test_schema_only_capabilities_are_not_advertised(
    fake_manager,
    client_factory,
    client_backend,
):
    client = client_factory(fake_manager)

    runtime_capabilities = {
        Capability.CONNECTIONS_READ,
        Capability.CONNECTIONS_EVENTS,
        Capability.CONNECTIONS_WRITE,
    }
    if client_backend == "daemon":
        runtime_capabilities.update(
            {
                Capability.SESSIONS_READ,
                Capability.SESSIONS_WRITE,
                Capability.SESSIONS_EVENTS,
                Capability.TERMINAL_OUTPUT,
                Capability.TERMINAL_INPUT,
                Capability.TERMINAL_RESIZE,
                Capability.TERMINAL_REPLAY,
            }
        )
    assert client.get_capabilities().supported == frozenset(runtime_capabilities)
    assert all(
        not client.get_capabilities().supports(capability)
        for capability in Capability
        if capability not in runtime_capabilities
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


def test_every_documented_sensitive_field_is_excluded_from_repr():
    import dataclasses

    generator = _load_generator()
    models = {model.__name__: model for model in generator.public_models()}
    schema = generator.build_schema()

    for model_name, model_schema in schema["models"].items():
        fields = {field.name: field for field in dataclasses.fields(models[model_name])}
        for field_schema in model_schema["fields"]:
            if field_schema["sensitive"]:
                assert fields[field_schema["name"]].repr is False, (
                    f"{model_name}.{field_schema['name']} is documented as sensitive "
                    "but is included in repr"
                )


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
