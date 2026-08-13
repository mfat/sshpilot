"""SSH overrides wire codec round-trip tests."""

import pytest

from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.api.transport.codec import (
    global_ssh_overrides_from_wire,
    global_ssh_overrides_to_wire,
    update_global_ssh_overrides_request_from_wire,
    update_global_ssh_overrides_request_to_wire,
)


def _overrides(**overrides):
    values = dict(
        revision="r1",
        connect_timeout=0,
        connection_attempts=0,
        server_alive_interval=0,
        server_alive_count_max=0,
        strict_host_key_checking="accept-new",
        batch_mode=False,
        compression=False,
        verbosity=0,
        debug_enabled=False,
    )
    values.update(overrides)
    return GlobalSshOverrides(**values)


def test_global_ssh_overrides_round_trip():
    snapshot = _overrides(
        revision="rev-123",
        connect_timeout=30,
        server_alive_interval=45,
        batch_mode=True,
        strict_host_key_checking="yes",
        verbosity=2,
    )
    wire = global_ssh_overrides_to_wire(snapshot)
    restored = global_ssh_overrides_from_wire(wire)

    assert restored.revision == "rev-123"
    assert restored.connect_timeout == 30
    assert restored.server_alive_interval == 45
    assert restored.batch_mode is True
    assert restored.strict_host_key_checking == "yes"
    assert restored.verbosity == 2
    assert restored.applies_immediately is True


def test_global_ssh_overrides_to_wire_requires_typed_model():
    with pytest.raises(TypeError):
        global_ssh_overrides_to_wire({"revision": "r"})


def test_global_ssh_overrides_from_wire_rejects_unknown_keys():
    wire = global_ssh_overrides_to_wire(_overrides())
    wire["secret"] = "must-not-leak"
    with pytest.raises(ValueError):
        global_ssh_overrides_from_wire(wire)


def test_global_ssh_overrides_from_wire_rejects_missing_keys():
    with pytest.raises(ValueError):
        global_ssh_overrides_from_wire({"revision": "r"})


def test_update_request_round_trip_with_expected_revision():
    request = UpdateGlobalSshOverridesRequest(
        patch={"connect_timeout": 12, "batch_mode": True},
        expected_revision="rev-abc",
    )
    wire = update_global_ssh_overrides_request_to_wire(request)
    restored = update_global_ssh_overrides_request_from_wire(wire)
    assert restored.patch == {"connect_timeout": 12, "batch_mode": True}
    assert restored.expected_revision == "rev-abc"


def test_update_request_round_trip_without_expected_revision():
    request = UpdateGlobalSshOverridesRequest(patch={"verbosity": 1})
    wire = update_global_ssh_overrides_request_to_wire(request)
    assert "expected_revision" not in wire
    restored = update_global_ssh_overrides_request_from_wire(wire)
    assert restored.expected_revision is None


def test_update_request_from_wire_rejects_non_object_patch():
    with pytest.raises(ValueError):
        update_global_ssh_overrides_request_from_wire({"patch": [1, 2]})


def test_update_request_to_wire_requires_typed_model():
    with pytest.raises(TypeError):
        update_global_ssh_overrides_request_to_wire({"patch": {}})


def test_update_request_from_wire_rejects_unknown_fields():
    with pytest.raises(ValueError):
        update_global_ssh_overrides_request_from_wire(
            {"patch": {}, "mystery": 1}
        )
