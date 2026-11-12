"""Free-form SSH options: shape, the few refusals, and where they land in argv."""
from __future__ import annotations

import json

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
from sshpilot.core.settings import (
    ExtraOptionError,
    ExtraOptionProblem,
    compose_ssh_overrides,
    extra_options_to_text,
    normalize_ssh_overrides,
    parse_extra_options_text,
)
from sshpilot.core.settings.ssh_overrides import (
    EXTRA_OPTION_MAX_LENGTH,
    EXTRA_OPTIONS_MAX_COUNT,
    EXTRA_OPTIONS_WITH_OWN_SETTING,
)
from sshpilot.core.ssh_overrides_service import SshOverridesService


def test_both_config_spellings_become_one_canonical_option():
    text = "# a comment\nIPQoS none\n\n  SetEnv = LANG=C FOO=bar  \nTCPKeepAlive=no\n"
    options = parse_extra_options_text(text)
    assert options == ("IPQoS=none", "SetEnv=LANG=C FOO=bar", "TCPKeepAlive=no")
    assert extra_options_to_text(options) == (
        "IPQoS none\nSetEnv LANG=C FOO=bar\nTCPKeepAlive no"
    )
    assert parse_extra_options_text(extra_options_to_text(options)) == options


def test_command_running_options_are_the_users_choice():
    """Nothing is refused for what it does, only for its shape."""
    options = parse_extra_options_text(
        "ProxyCommand nc -X connect -x proxy:3128 %h %p\n"
        "PermitLocalCommand yes\nLocalCommand notify-send %n\n"
    )
    assert options[0] == "ProxyCommand=nc -X connect -x proxy:3128 %h %p"
    assert len(options) == 3


@pytest.mark.parametrize(
    "line",
    [
        "-F /etc/passwd",          # a flag, not an option
        "-oProxyCommand=x",
        "IPQoS",                   # no value
        "Foo-Bar x",               # not a keyword
        "=value",
        "a b\x00c",
        "a b\x1bc",
        "K " + "v" * EXTRA_OPTION_MAX_LENGTH,
    ],
)
def test_lines_that_are_not_one_option_are_refused(line):
    with pytest.raises(ExtraOptionError) as caught:
        parse_extra_options_text("IPQoS none\n" + line)
    assert caught.value.line == 2
    assert caught.value.problem in (
        ExtraOptionProblem.MALFORMED, ExtraOptionProblem.TOO_LONG,
    )


@pytest.mark.parametrize("keyword", ["Host", "match", "Include"])
def test_config_file_structure_is_not_an_option(keyword):
    with pytest.raises(ExtraOptionError) as caught:
        parse_extra_options_text(f"{keyword} something")
    assert caught.value.problem is ExtraOptionProblem.NOT_AN_OPTION
    assert caught.value.keyword == keyword


@pytest.mark.parametrize("keyword", sorted(EXTRA_OPTIONS_WITH_OWN_SETTING))
def test_options_with_their_own_setting_are_set_there(keyword):
    for spelling in (keyword, keyword.upper()):
        with pytest.raises(ExtraOptionError) as caught:
            parse_extra_options_text(f"{spelling} 1")
        assert caught.value.problem is ExtraOptionProblem.HAS_OWN_SETTING


def test_every_dedicated_control_is_covered():
    """A row added later must be added to the refusal list too."""
    composed = compose_ssh_overrides(
        {
            "connection_timeout": 5,
            "address_family": "inet",
            "strict_host_key_checking": "yes",
            "compression": "yes",
            "debug_enabled": True,
            "controlmaster": True,
        },
        controlmaster_extra=[
            "-o", "ControlMaster=auto", "-o", "ControlPath=/x/%C",
            "-o", "ControlPersist=60",
        ],
    )
    emitted = {item.split("=", 1)[0].lower() for item in composed if "=" in item}
    assert emitted <= EXTRA_OPTIONS_WITH_OWN_SETTING
    assert {"serveraliveinterval", "serveralivecountmax"} <= EXTRA_OPTIONS_WITH_OWN_SETTING


def test_too_many_options_are_refused():
    text = "\n".join(f"SetEnv A{i}=1" for i in range(EXTRA_OPTIONS_MAX_COUNT + 1))
    with pytest.raises(ExtraOptionError) as caught:
        parse_extra_options_text(text)
    assert caught.value.problem is ExtraOptionProblem.TOO_MANY


def test_each_option_is_exactly_one_dash_o_argument_and_comes_last():
    argv = compose_ssh_overrides(
        {
            "connection_timeout": 5,
            "verbosity": 1,
            "extra_options": ["IPQoS=none", "ProxyCommand=nc %h %p; echo $HOME"],
        }
    )
    assert argv[-4:] == [
        "-o", "IPQoS=none", "-o", "ProxyCommand=nc %h %p; echo $HOME",
    ]
    assert argv.index("ConnectTimeout=5") < argv.index("IPQoS=none")


def test_a_bad_entry_in_a_hand_edited_file_is_skipped_not_sent():
    argv = compose_ssh_overrides(
        {"extra_options": ["IPQoS=none", "-F /etc/passwd", 7, "ConnectTimeout=9", None]}
    )
    assert argv == ["-o", "IPQoS=none"]
    assert compose_ssh_overrides({"extra_options": "IPQoS=none"}) == []


def test_normalization_is_strict_about_stored_values():
    assert normalize_ssh_overrides({})["extra_options"] == []
    assert normalize_ssh_overrides({"extra_options": ["IPQoS none"]})["extra_options"] == [
        "IPQoS=none"
    ]
    with pytest.raises(TypeError):
        normalize_ssh_overrides({"extra_options": "IPQoS none"})
    with pytest.raises(ValueError):
        normalize_ssh_overrides({"extra_options": ["-F x"]})


def _snapshot(**fields):
    values = dict(
        revision="rev-1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False,
    )
    values.update(fields)
    return GlobalSshOverrides(**values)


def test_model_and_wire_round_trip():
    snapshot = _snapshot(extra_options=["IPQoS none", "SetEnv=A=b"])
    assert snapshot.extra_options == ("IPQoS=none", "SetEnv=A=b")
    wire = global_ssh_overrides_to_wire(snapshot)
    assert wire["extra_options"] == ["IPQoS=none", "SetEnv=A=b"]
    assert global_ssh_overrides_from_wire(json.loads(json.dumps(wire))) == snapshot
    # Optional on the wire: a peer that predates the field still parses.
    del wire["extra_options"]
    assert global_ssh_overrides_from_wire(wire).extra_options == ()

    with pytest.raises(ValueError):
        _snapshot(extra_options=["ConnectTimeout=5"])
    with pytest.raises(ValueError):
        UpdateGlobalSshOverridesRequest(patch={"extra_options": ["-F x"]})
    with pytest.raises(TypeError):
        UpdateGlobalSshOverridesRequest(patch={"extra_options": "IPQoS none"})

    request = UpdateGlobalSshOverridesRequest(patch={"extra_options": ("IPQoS none",)})
    wired = json.loads(json.dumps(update_global_ssh_overrides_request_to_wire(request)))
    assert update_global_ssh_overrides_request_from_wire(wired).patch == {
        "extra_options": ["IPQoS none"]
    }


def test_service_saves_sends_and_resets_extra_options(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ssh": {"connection_timeout": 7}}), encoding="utf-8")
    service = SshOverridesService(path)

    updated = service.update(
        UpdateGlobalSshOverridesRequest(
            patch={"extra_options": ["IPQoS none", "ProxyCommand=nc %h %p"]}
        )
    )
    assert updated.extra_options == ("IPQoS=none", "ProxyCommand=nc %h %p")
    assert updated.connect_timeout == 7

    on_disk = json.loads(path.read_text(encoding="utf-8"))["ssh"]
    assert on_disk["extra_options"] == ["IPQoS=none", "ProxyCommand=nc %h %p"]
    assert service.get_ssh_config()["ssh_overrides"] == [
        "-o", "ConnectTimeout=7",
        "-o", "IPQoS=none",
        "-o", "ProxyCommand=nc %h %p",
    ]

    reset = service.reset(expected_revision=updated.revision)
    assert reset.extra_options == ()
    assert json.loads(path.read_text(encoding="utf-8"))["ssh"]["extra_options"] == []
    assert service.get_ssh_config()["ssh_overrides"] == []


def test_service_refuses_an_option_with_its_own_setting(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"ssh": {}}), encoding="utf-8")
    service = SshOverridesService(path)
    with pytest.raises(ValueError):
        service.update(
            UpdateGlobalSshOverridesRequest(patch={"extra_options": ["LogLevel DEBUG"]})
        )
    assert service.get().extra_options == ()
