"""Legacy-device algorithm options are built from what ``ssh`` supports."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from sshpilot.api.models.settings import (
    GlobalSshOverrides,
    UpdateGlobalSshOverridesRequest,
)
from sshpilot.api.transport.codec import (
    global_ssh_overrides_from_wire,
    global_ssh_overrides_to_wire,
)
from sshpilot.core.settings import compose_ssh_overrides, normalize_ssh_overrides
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.daemon.legacy_algorithms import legacy_algorithm_args


def _fake_ssh(supported, *, accepted=lambda option: True, calls=None):
    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        if argv[1] == "-Q":
            return 0, "\n".join(supported.get(argv[2], ())) + "\n"
        option = argv[argv.index("-o") + 1]
        return (0 if accepted(option) else 255), ""

    return run


MODERN = {
    # An OpenSSH that dropped DSA, like 10.x.
    "kex": ["curve25519-sha256", "diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1"],
    "key": ["ssh-ed25519", "ssh-rsa"],
    "cipher": ["aes128-ctr", "aes128-cbc", "3des-cbc"],
}


def test_only_algorithms_this_ssh_still_has_are_named():
    """Naming a removed algorithm (ssh-dss) is a hard error for every connection."""
    args = legacy_algorithm_args(run=_fake_ssh(MODERN))
    assert args == [
        "-o", "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
        "-o", "HostKeyAlgorithms=+ssh-rsa",
        "-o", "PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o", "Ciphers=+aes128-cbc,3des-cbc",
    ]
    assert all("ssh-dss" not in item for item in args)
    # Always additive, so modern algorithms keep their place.
    assert all("=+" in item for item in args[1::2])


def test_older_ssh_gets_the_old_option_name():
    old_name_only = lambda option: not option.startswith("PubkeyAcceptedAlgorithms")
    args = legacy_algorithm_args(run=_fake_ssh(MODERN, accepted=old_name_only))
    assert "PubkeyAcceptedKeyTypes=+ssh-rsa" in args
    assert not any(a.startswith("PubkeyAcceptedAlgorithms") for a in args)


def test_an_option_ssh_rejects_is_left_out():
    args = legacy_algorithm_args(
        run=_fake_ssh(MODERN, accepted=lambda option: not option.startswith("Ciphers"))
    )
    assert not any(a.startswith("Ciphers") for a in args)
    assert any(a.startswith("KexAlgorithms") for a in args)


def test_nothing_is_guessed_when_ssh_cannot_be_asked():
    assert legacy_algorithm_args(run=lambda argv: None) == []
    assert legacy_algorithm_args(run=lambda argv: (255, "")) == []
    assert legacy_algorithm_args(run=_fake_ssh({"kex": ["curve25519-sha256"]})) == []


def test_queries_never_name_a_real_host():
    calls = []
    legacy_algorithm_args(run=_fake_ssh(MODERN, calls=calls))
    for argv in calls:
        assert argv[1] in ("-Q", "-G")
        if argv[1] == "-G":
            assert argv[2:4] == ["-F", __import__("os").devnull]


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
def test_the_real_fragment_is_accepted_by_the_installed_ssh():
    args = legacy_algorithm_args()
    assert args, "the installed ssh offered no legacy algorithms at all"
    done = subprocess.run(
        ["ssh", "-G", "-F", "/dev/null", *args, "sshpilot-legacy-check"],
        capture_output=True, text=True, timeout=20,
    )
    assert done.returncode == 0, done.stderr


# -- composition, model and service -----------------------------------------

LEGACY = ["-o", "KexAlgorithms=+diffie-hellman-group14-sha1", "-o", "Ciphers=+aes128-cbc"]


def test_legacy_fragment_is_sent_only_while_the_switch_is_on():
    assert compose_ssh_overrides({"legacy_algorithms": True}, legacy_extra=LEGACY) == LEGACY
    assert compose_ssh_overrides({"legacy_algorithms": False}, legacy_extra=LEGACY) == []
    assert compose_ssh_overrides({}, legacy_extra=LEGACY) == []
    # On, but nothing verified for this machine: nothing is sent.
    assert compose_ssh_overrides({"legacy_algorithms": True}) == []
    # Only a real boolean turns it on.
    assert compose_ssh_overrides({"legacy_algorithms": "yes"}, legacy_extra=LEGACY) == []


def test_free_form_algorithm_lines_still_lose_to_the_switch():
    argv = compose_ssh_overrides(
        {"legacy_algorithms": True, "extra_options": ["Ciphers=aes256-ctr"]},
        legacy_extra=LEGACY,
    )
    assert argv.index("Ciphers=+aes128-cbc") < argv.index("Ciphers=aes256-ctr")


@pytest.mark.parametrize("value, sent", [("inet", "inet"), ("inet6", "inet6"), (" INET ", "inet")])
def test_address_family_is_sent(value, sent):
    assert compose_ssh_overrides({"address_family": value}) == [
        "-o", f"AddressFamily={sent}",
    ]


@pytest.mark.parametrize("value", ["", None, "any", "ipv4", 4])
def test_other_address_family_values_send_nothing(value):
    assert compose_ssh_overrides({"address_family": value}) == []


def test_normalization_of_the_two_fields():
    assert normalize_ssh_overrides({})["address_family"] == ""
    assert normalize_ssh_overrides({})["legacy_algorithms"] is False
    assert normalize_ssh_overrides({"address_family": "INET6"})["address_family"] == "inet6"
    with pytest.raises(ValueError):
        normalize_ssh_overrides({"address_family": "any"})
    with pytest.raises(TypeError):
        normalize_ssh_overrides({"legacy_algorithms": "yes"})


def test_model_and_wire_round_trip():
    snapshot = GlobalSshOverrides(
        revision="rev-1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False,
        address_family="inet", legacy_algorithms=True,
    )
    wire = json.loads(json.dumps(global_ssh_overrides_to_wire(snapshot)))
    assert wire["address_family"] == "inet" and wire["legacy_algorithms"] is True
    assert global_ssh_overrides_from_wire(wire) == snapshot
    # Optional on the wire: a peer that predates the fields still parses.
    del wire["address_family"], wire["legacy_algorithms"]
    restored = global_ssh_overrides_from_wire(wire)
    assert restored.address_family == "" and restored.legacy_algorithms is False

    with pytest.raises(ValueError):
        UpdateGlobalSshOverridesRequest(patch={"address_family": "any"})
    with pytest.raises(TypeError):
        UpdateGlobalSshOverridesRequest(patch={"legacy_algorithms": "yes"})


def test_service_saves_sends_and_resets_both(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ssh": {}}), encoding="utf-8")
    service = SshOverridesService(path, legacy_extra=LEGACY)

    updated = service.update(
        UpdateGlobalSshOverridesRequest(
            patch={"address_family": "inet", "legacy_algorithms": True}
        )
    )
    assert updated.address_family == "inet" and updated.legacy_algorithms is True
    assert service.get_ssh_config()["ssh_overrides"] == [
        "-o", "AddressFamily=inet", *LEGACY,
    ]

    reset = service.reset(expected_revision=updated.revision)
    assert reset.address_family == "" and reset.legacy_algorithms is False
    assert service.get_ssh_config()["ssh_overrides"] == []
