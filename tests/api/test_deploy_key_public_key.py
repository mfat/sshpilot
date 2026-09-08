"""DeployKeyRequest accepts either a key id or a pasted public-key line."""

import pytest

from sshpilot.api.models.identity import DeployKeyRequest
from sshpilot.api.models.keys import KeyStoreScope
from sshpilot.api.transport.codec import (
    deploy_key_request_from_wire,
    deploy_key_request_to_wire,
)


_PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyMaterialForUnitTest pasted@host"


def test_key_id_request_round_trips_without_public_key_on_wire():
    request = DeployKeyRequest("HostAlias", "key:one", KeyStoreScope.DEFAULT, force=True)
    wire = deploy_key_request_to_wire(request)
    assert wire == {
        "connection_id": "HostAlias",
        "key_id": "key:one",
        "scope": "default",
        "force": True,
    }
    assert "public_key" not in wire
    restored = deploy_key_request_from_wire(wire)
    assert restored.key_id == "key:one"
    assert restored.public_key == ""


def test_public_key_request_round_trips_without_key_id_on_wire():
    request = DeployKeyRequest("HostAlias", public_key=_PUB, force=True)
    wire = deploy_key_request_to_wire(request)
    assert wire == {
        "connection_id": "HostAlias",
        "scope": "default",
        "force": True,
        "public_key": _PUB,
    }
    assert "key_id" not in wire
    restored = deploy_key_request_from_wire(wire)
    assert restored.key_id is None
    assert restored.public_key == _PUB


def test_exactly_one_of_key_id_or_public_key_is_required():
    with pytest.raises(ValueError, match="exactly one"):
        DeployKeyRequest("HostAlias")
    with pytest.raises(ValueError, match="exactly one"):
        DeployKeyRequest("HostAlias", "key:one", public_key=_PUB)


def test_public_key_must_be_a_single_openssh_line():
    with pytest.raises(ValueError, match="single OpenSSH"):
        DeployKeyRequest("HostAlias", public_key="not-a-key")
    with pytest.raises(ValueError, match="single OpenSSH"):
        DeployKeyRequest(
            "HostAlias",
            public_key=f"{_PUB}\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOther second@host",
        )


def test_public_key_rejects_private_key_material():
    with pytest.raises(ValueError, match="private-key"):
        DeployKeyRequest(
            "HostAlias",
            public_key="-----BEGIN OPENSSH PRIVATE KEY-----\nbogus\n",
        )
