"""Regressions found reviewing the authentication redesign after 6.4.4."""
from __future__ import annotations

import pytest

from sshpilot.core.connections.auth_preferences import (
    PASSWORD_MODE_PREFERRED_AUTH,
    pubkey_disabled,
    split_option,
)
from sshpilot.core.login_profiles.models import (
    PROFILE_FIELDS,
    LoginProfile,
    apply_profile_to_data,
    profile_owned_fields,
)

PROFILE_ID = "lp-0123456789abcdef"


# -- one option splitter ------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("PubkeyAuthentication no", ("pubkeyauthentication", "no")),
    ("PubkeyAuthentication=no", ("pubkeyauthentication", "no")),
    ("PubkeyAuthentication = no", ("pubkeyauthentication", "no")),
    ('IdentityAgent "/run/my agent"', ("identityagent", "/run/my agent")),
    ("SetEnv A=b", ("setenv", "A=b")),
    ("# PubkeyAuthentication no", ("", "")),
    ("", ("", "")),
])
def test_split_option_reads_both_separators(line, expected):
    assert split_option(line) == expected


def test_pubkey_disabled_reads_the_first_line_only():
    assert pubkey_disabled("PubkeyAuthentication=no")
    assert not pubkey_disabled("PubkeyAuthentication yes\nPubkeyAuthentication no")
    assert not pubkey_disabled("PasswordAuthentication no")


def test_profile_with_equals_form_reads_as_public_keys_off():
    profile = LoginProfile(
        id=PROFILE_ID, name="p", extra_ssh_config="PubkeyAuthentication=no"
    )
    out = apply_profile_to_data({}, profile)
    assert out["pubkey_auth_no"] is True
    assert out["auth_method"] == 1


# -- profiles stored by 6.4.4 -------------------------------------------------

def _stored_644(**overrides):
    data = {
        "id": PROFILE_ID, "name": "Old", "username": "alice", "auth_method": 0,
        "key_select_mode": 0, "identity_files": [], "certificate_files": [],
        "identity_agent": "", "add_keys_to_agent": "", "pkcs11_provider": "",
        "security_key_provider": "", "pubkey_auth_no": False,
        "forward_agent": False, "forward_agent_target": "", "proxy_command": "",
        "extra_ssh_config": "", "has_password": False,
        "has_sudo_password": False, "revision": 3,
    }
    data.update(overrides)
    return data


def test_password_profile_does_not_gain_the_keys_its_old_form_hid():
    profile = LoginProfile.from_dict(_stored_644(
        auth_method=1, key_select_mode=1, identity_files=["~/.ssh/old"],
        certificate_files=["~/.ssh/old-cert.pub"], identity_agent="~/agent.sock",
    ))
    assert profile_owned_fields(profile) == ("username",)
    assert profile.identity_files == ()
    assert profile.identities_only is None
    assert f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}" in (
        profile.extra_ssh_config
    )
    out = apply_profile_to_data(
        {"identity_files": ["~/.ssh/host"], "key_select_mode": 2}, profile
    )
    assert out["identity_files"] == ["~/.ssh/host"]
    assert out["key_select_mode"] == 2


def test_key_profile_keeps_its_keys():
    profile = LoginProfile.from_dict(_stored_644(
        key_select_mode=1, identity_files=["~/.ssh/work"],
    ))
    assert profile.identity_files == ("~/.ssh/work",)
    assert profile.identities_only is True


# -- a store an older version can still read ----------------------------------

@pytest.mark.parametrize("profile", [
    LoginProfile(id=PROFILE_ID, name="empty"),
    LoginProfile(id=PROFILE_ID, name="no forward", forward_agent=False),
    LoginProfile(id=PROFILE_ID, name="forward", forward_agent=True),
    LoginProfile(id=PROFILE_ID, name="keys", identity_files=("~/.ssh/a",)),
    LoginProfile(
        id=PROFILE_ID, name="only", identity_files=("~/.ssh/a",), identities_only=True
    ),
])
def test_stored_profile_is_readable_by_644_and_round_trips(profile):
    stored = profile.to_dict()
    # What 6.4.4's LoginProfile.__post_init__ insists on.
    assert stored["key_select_mode"] in (0, 1, 2)
    assert type(stored["forward_agent"]) is bool
    assert type(stored["pubkey_auth_no"]) is bool
    assert LoginProfile.from_dict(stored) == profile
    assert profile.settings()["forward_agent"] is profile.forward_agent


def test_store_written_before_the_forwarding_flag_still_reads():
    stored = LoginProfile(id=PROFILE_ID, name="p").to_dict()
    del stored["forward_agent_explicit_no"]
    stored["forward_agent"] = None
    assert LoginProfile.from_dict(stored).forward_agent is None
    stored["forward_agent"] = False
    assert LoginProfile.from_dict(stored).forward_agent is False


# -- clients from before fields could be left unset ---------------------------

def _wire(**overrides):
    from sshpilot.api.models.login_profiles import LoginProfileSettings
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_to_wire,
    )

    wire = login_profile_settings_to_wire(LoginProfileSettings(name="Old"))
    wire.update(overrides)
    return wire


def test_older_client_defaults_are_not_settings():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    wire = _wire(forward_agent=False, key_select_mode=0)
    del wire["identities_only"]
    settings = login_profile_settings_from_wire(wire)
    assert settings.forward_agent is None
    assert settings.key_select_mode is None


def test_older_client_password_profile_sets_no_keys():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    wire = _wire(
        auth_method=1, key_select_mode=1, identity_files=["~/.ssh/old"],
        pkcs11_provider="/usr/lib/opensc-pkcs11.so",
    )
    del wire["identities_only"]
    settings = login_profile_settings_from_wire(wire)
    assert settings.identity_files == ()
    assert settings.key_select_mode is None
    assert settings.pkcs11_provider == ""
    assert settings.auth_method == 1


def test_current_client_values_are_taken_as_sent():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    settings = login_profile_settings_from_wire(_wire(forward_agent=False))
    assert settings.forward_agent is False


# -- the legacy Key/Password choice only acts on a change ---------------------

def _legacy(data, patch, before=(None, None)):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )

    ConnectionApplicationService._apply_legacy_auth_choice(data, patch, before)
    return data.get("extra_ssh_config", "")


def test_echoing_back_what_was_read_leaves_the_block_alone():
    extra = "PubkeyAuthentication no"
    patch = {"auth_method": 1, "pubkey_auth_no": True, "extra_ssh_config": extra}
    assert _legacy({"extra_ssh_config": extra}, patch, before=(1, True)) == extra


def test_creating_with_the_default_choice_keeps_authored_lines():
    extra = "PreferredAuthentications password,publickey\nPubkeyAuthentication no"
    patch = {"auth_method": 0, "pubkey_auth_no": False, "extra_ssh_config": extra}
    assert _legacy({"extra_ssh_config": extra}, patch) == extra


def test_a_changed_choice_still_edits_the_lines():
    assert _legacy({}, {"auth_method": 1}) == (
        f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}"
    )
    assert _legacy(
        {"extra_ssh_config": "PubkeyAuthentication no"},
        {"pubkey_auth_no": False},
        before=(1, True),
    ) == ""


# -- the section is editable without an answer from the configuration ---------

def test_empty_fallback_makes_the_order_known():
    from sshpilot.connection_dialog_auth import DEFAULT_ORDER, AuthPolicy

    policy = AuthPolicy()
    assert not policy.can_edit()
    policy.set_fallback({})
    assert policy.can_edit()
    assert policy.shown_order() == list(DEFAULT_ORDER)
    assert policy.set_tried("hostbased", False)


class _Dialog:
    is_editing = False
    connection = None

    def __init__(self, parent):
        self.parent_window = parent
        self.fallbacks = []
        self.earlier = []

    def _auth_policy_set_fallback(self, inherited):
        self.fallbacks.append(inherited)

    def _set_earlier_directives(self, earlier):
        self.earlier.append(earlier)

    def _watch_alias_for_earlier_directives(self):
        pass


def _load_earlier(dialog):
    pytest.importorskip("gi")
    from sshpilot.connection_dialog_overrides import ConnectionDialogOverridesMixin

    ConnectionDialogOverridesMixin._load_earlier_directives_async(dialog)


def test_new_connection_without_alias_gets_the_builtin_order():
    class Client:
        def get_earlier_directives(self, alias):
            raise AssertionError("no alias to ask about")

    class Parent:
        client = Client()

        def _daemon_ready(self):
            return True

    dialog = _Dialog(Parent())
    _load_earlier(dialog)
    assert dialog.fallbacks == [{}]


def test_daemon_not_ready_gets_the_builtin_order():
    class Parent:
        client = None

        def _daemon_ready(self):
            return False

    dialog = _Dialog(Parent())
    _load_earlier(dialog)
    assert dialog.fallbacks == [{}]


# -- the frontend's copy of the profile fields --------------------------------

def test_frontend_profile_fields_match_the_model():
    from sshpilot.api.models.login_profiles import LoginProfileSettings
    from sshpilot.gtk import login_profile_controller as controller

    assert controller.PROFILE_FIELDS == PROFILE_FIELDS
    samples = [
        dict(name="a"),
        dict(name="b", username="u", forward_agent=False),
        dict(name="c", identity_files=("~/.ssh/k",), identities_only=True),
        dict(name="d", proxy_command="nc %h %p", add_keys_to_agent="yes"),
    ]
    for sample in samples:
        settings = LoginProfileSettings(**sample)
        profile = LoginProfile(id=PROFILE_ID, **sample)
        assert controller.settings_owned_fields(settings) == profile_owned_fields(profile)


# -- spellings OpenSSH reads as one setting -----------------------------------

def _ssh_g(config, host="demo"):
    import shutil
    import subprocess

    if shutil.which("ssh") is None:
        pytest.skip("needs the OpenSSH client")
    out = subprocess.run(
        ["ssh", "-G", "-F", str(config), host], capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


@pytest.mark.parametrize("alias, current", [
    ("ChallengeResponseAuthentication", "kbdinteractiveauthentication"),
    ("TISAuthentication", "kbdinteractiveauthentication"),
    ("SKeyAuthentication", "kbdinteractiveauthentication"),
    ("DSAAuthentication", "pubkeyauthentication"),
])
def test_the_alias_table_is_openssh_s(tmp_path, alias, current):
    from sshpilot.core.connections.auth_preferences import canonical_keyword

    assert canonical_keyword(alias) == current
    config = tmp_path / "config"
    config.write_text(f"Host demo\n    {alias} no\n")
    assert _ssh_g(config)[current] in ("no", "false")


def test_profile_setting_replaces_a_hosts_older_spelling(tmp_path):
    profile = LoginProfile(
        id=PROFILE_ID, name="p", extra_ssh_config="KbdInteractiveAuthentication yes"
    )
    out = apply_profile_to_data(
        {"extra_ssh_config": "ChallengeResponseAuthentication no\nCompression yes"},
        profile,
    )
    assert out["extra_ssh_config"].splitlines() == [
        "Compression yes", "KbdInteractiveAuthentication yes",
    ]
    config = tmp_path / "config"
    config.write_text(
        "Host demo\n" + "".join(f"    {l}\n" for l in out["extra_ssh_config"].splitlines())
    )
    assert _ssh_g(config)["kbdinteractiveauthentication"] == "yes"


def test_profile_using_the_older_spelling_replaces_the_current_one():
    profile = LoginProfile(
        id=PROFILE_ID, name="p", extra_ssh_config="ChallengeResponseAuthentication no"
    )
    out = apply_profile_to_data(
        {"extra_ssh_config": "KbdInteractiveAuthentication yes"}, profile
    )
    assert out["extra_ssh_config"] == "ChallengeResponseAuthentication no"


_TWO_SPELLINGS = (
    "Host *\n"
    "    ChallengeResponseAuthentication no\n"
    "    KbdInteractiveAuthentication yes\n"
)


def test_inherited_lookup_returns_the_first_spelling_only(tmp_path):
    from sshpilot.core.ssh_config_effective import inherited_directives

    config = tmp_path / "config"
    config.write_text(_TWO_SPELLINGS)
    assert _ssh_g(config)["kbdinteractiveauthentication"] == "no"
    from sshpilot.core.connections.auth_preferences import AUTH_POLICY_KEYWORDS

    found = inherited_directives("demo", str(config), AUTH_POLICY_KEYWORDS)
    assert [(item["key"], item["value"]) for item in found.values()] == [
        ("challengeresponseauthentication", "no"),
    ]


def test_earlier_lookup_sees_an_alias_above_the_hosts_own_line(tmp_path):
    from sshpilot.core.ssh_config_effective import earlier_directives

    config = tmp_path / "config"
    config.write_text(
        _TWO_SPELLINGS + "\nHost demo\n    KbdInteractiveAuthentication yes\n"
    )
    assert _ssh_g(config)["kbdinteractiveauthentication"] == "no"
    found = earlier_directives("demo", str(config))
    assert [(item["key"], item["value"]) for item in found] == [
        ("challengeresponseauthentication", "no"),
    ]


def test_preview_follows_the_first_spelling_in_force():
    from sshpilot.connection_dialog_auth import AuthPolicy

    for attr in ("earlier", "forced"):
        policy = AuthPolicy.from_lines([])
        policy.set_fallback({})
        setattr(policy, attr, {
            "challengeresponseauthentication": "no",
            "kbdinteractiveauthentication": "yes",
        })
        assert "keyboard-interactive" not in policy.will_try(), attr
    policy = AuthPolicy.from_lines([])
    policy.set_fallback({"challengeresponseauthentication": "no"})
    assert "keyboard-interactive" not in policy.will_try()
    # Inherit on a saved line falls back to the same answer.
    policy = AuthPolicy.from_lines(["KbdInteractiveAuthentication yes"])
    policy.set_inherited({"kbdinteractiveauthentication": "yes"})
    policy.set_fallback({"tisauthentication": "no"})
    assert policy.set_switch("keyboard-interactive", None)
    assert "keyboard-interactive" not in policy.will_try()


def test_every_alias_is_the_methods_own_setting_in_the_editor():
    from sshpilot.connection_dialog_auth import AuthPolicy, split_policy_lines

    lines, other = split_policy_lines("DSAAuthentication no\nCompression yes")
    assert lines == ["DSAAuthentication no"] and other == "Compression yes"
    policy = AuthPolicy.from_lines(lines + ["PubkeyAuthentication yes"])
    assert policy.own_switch("publickey") is False
    assert policy.set_switch("publickey", None)
    assert policy.to_lines() == []


# -- an alias line placed above a profile's line is drift ---------------------

def _fingerprint(extra, owned=("kbdinteractiveauthentication",)):
    from sshpilot.core.login_profiles.models import auth_fingerprint

    return auth_fingerprint(
        {"extra_ssh_config": extra}, owned, owned_fields=("username",)
    )


def test_an_alias_above_the_profiles_line_changes_the_fingerprint():
    applied = _fingerprint("KbdInteractiveAuthentication yes")
    reversed_ = _fingerprint(
        "ChallengeResponseAuthentication no\nKbdInteractiveAuthentication yes"
    )
    assert reversed_ != applied
    # Below the profile's line OpenSSH ignores it: not drift.
    assert _fingerprint(
        "KbdInteractiveAuthentication yes\nChallengeResponseAuthentication no"
    ) == applied
    # Unrelated lines never mattered and still do not.
    assert _fingerprint("Compression yes\nKbdInteractiveAuthentication yes") == applied


def test_a_profile_written_with_the_alias_notices_the_current_spelling_above():
    owned = ("challengeresponseauthentication",)
    applied = _fingerprint("ChallengeResponseAuthentication no", owned)
    assert _fingerprint(
        "KbdInteractiveAuthentication yes\nChallengeResponseAuthentication no", owned
    ) != applied


def test_fingerprints_without_alias_lines_are_unchanged():
    """Recorded links must keep matching: the projection is as before."""
    import hashlib
    import json

    data = {"extra_ssh_config": "Compression yes\nPubkeyAuthentication no\nUser x"}
    from sshpilot.core.login_profiles.models import auth_projection

    projection = auth_projection(data, ("pubkeyauthentication", "compression"))
    assert projection["extra"] == ["Compression yes", "PubkeyAuthentication no"]
    encoded = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
    assert len(hashlib.sha256(encoded).hexdigest()) == 64


# -- the loader's Key/Password view reads aliases too -------------------------

@pytest.mark.parametrize("body, disabled", [
    ("DSAAuthentication no", True),
    ("PubkeyAuthentication=no", True),
    ("DSAAuthentication no\n    PubkeyAuthentication yes", True),
    ("PubkeyAuthentication yes\n    DSAAuthentication no", False),
    ("PasswordAuthentication no", False),
])
def test_loaded_host_reports_public_keys_as_openssh_does(tmp_path, body, disabled):
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    config = tmp_path / "config"
    config.write_text(f"Host demo\n    HostName demo.example\n    {body}\n")
    assert (_ssh_g(config)["pubkeyauthentication"] == "false") is disabled
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(config),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    data = repo.get_record("demo").data
    assert data["pubkey_auth_no"] is disabled
    assert data["auth_method"] == (1 if disabled else 0)


# -- "IdentityFile none" beside real keys is kept in place --------------------

def _repo(tmp_path, body):
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    config = tmp_path / "config"
    config.write_text(
        "Host demo\n    HostName demo.example\n" + "".join(f"    {l}\n" for l in body)
    )
    return config, ConnectionRepository(
        ssh_store=SshConfigStore(config),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


def _block_lines(config):
    return [l.strip() for l in config.read_text().splitlines() if l.startswith("    ")]


@pytest.mark.parametrize("body", [
    ["IdentityFile none", "IdentityFile /keys/work"],
    ["IdentityFile /keys/work", "IdentityFile none"],
    ["IdentityFile /keys/a", "IdentityFile none", "IdentityFile /keys/b"],
    ["IdentityFile none"],
])
def test_an_unrelated_save_keeps_identityfile_none_where_it_was(tmp_path, body):
    config, repo = _repo(tmp_path, body)
    before = _ssh_g(config).get("identityfile")
    record = repo.get_record("demo")
    repo.update_connection("demo", {**record.data, "port": 2222})
    assert [l for l in _block_lines(config) if l.startswith("IdentityFile")] == body
    assert _ssh_g(config).get("identityfile") == before


def test_replacing_none_with_a_key_drops_none(tmp_path):
    config, repo = _repo(tmp_path, ["IdentityFile none"])
    record = repo.get_record("demo")
    repo.update_connection("demo", {
        **record.data, "identity_files": ["/keys/new"], "keyfile": "/keys/new",
        "key_select_mode": 2,
    })
    assert [l for l in _block_lines(config) if l.startswith("IdentityFile")] == [
        "IdentityFile /keys/new",
    ]


def test_a_profile_s_keys_replace_a_mixed_list_without_none():
    profile = LoginProfile(id=PROFILE_ID, name="p", identity_files=("~/.ssh/p",))
    out = apply_profile_to_data({
        "identity_files": ["/keys/work"], "identity_file_none": True,
        "__identity_none_index": 0, "key_select_mode": 2,
    }, profile)
    from sshpilot.ssh_config_formatter import format_ssh_config_entry

    text = format_ssh_config_entry({"nickname": "demo", "hostname": "h", **out})
    assert "IdentityFile none" not in text
    assert "IdentityFile ~/.ssh/p" in text


# -- PubkeyAuthentication host-bound / unbound --------------------------------

@pytest.mark.parametrize("value", ["host-bound", "unbound"])
def test_a_restricted_public_key_line_is_named_and_kept(value):
    from sshpilot.connection_dialog_auth import AuthPolicy

    line = f"PubkeyAuthentication {value}"
    policy = AuthPolicy.from_lines([line])
    assert policy.own_switch("publickey") is True
    assert policy.restriction("publickey") == value
    assert not policy.set_switch("publickey", True)  # already enabled: kept
    assert policy.to_lines() == [line]
    assert policy.set_switch("publickey", False)
    assert policy.restriction("publickey") == ""
    assert policy.to_lines() == ["PubkeyAuthentication no"]
    assert policy.set_switch("publickey", True)       # back: as authored
    assert policy.to_lines() == [line]


def test_not_set_then_enable_writes_a_plain_yes():
    from sshpilot.connection_dialog_auth import AuthPolicy

    policy = AuthPolicy.from_lines(["PubkeyAuthentication host-bound"])
    assert policy.set_switch("publickey", None)
    assert policy.set_switch("publickey", True)
    assert policy.restriction("publickey") == ""
    assert policy.to_lines() == ["PubkeyAuthentication yes"]


def test_a_plain_yes_has_no_restriction():
    from sshpilot.connection_dialog_auth import AuthPolicy

    policy = AuthPolicy.from_lines(["PubkeyAuthentication yes", "PasswordAuthentication yes"])
    assert policy.restriction("publickey") == ""
    assert policy.restriction("password") == ""


# -- the login profile editor keeps step with the connection editor -----------

def test_preview_lists_a_replaced_alias_line_as_an_ordinary_option():
    from sshpilot.core.login_profiles.models import (
        auth_projection,
        diff_projections,
        profile_owned_keywords,
    )

    profile = LoginProfile(
        id=PROFILE_ID, name="p", extra_ssh_config="KbdInteractiveAuthentication yes"
    )
    host = {"extra_ssh_config": "ChallengeResponseAuthentication no"}
    keys = profile_owned_keywords(profile)
    fields = profile_owned_fields(profile)
    before = auth_projection(host, keys, owned_fields=fields)
    after = auth_projection(apply_profile_to_data(host, profile), keys, owned_fields=fields)
    changes = diff_projections(before, after)
    assert [(c.field, c.before, c.after) for c in changes] == [
        ("extra", ["ChallengeResponseAuthentication no"],
         ["KbdInteractiveAuthentication yes"]),
    ]


def test_profile_extra_box_refuses_options_the_section_owns():
    from sshpilot.gtk.login_profile_messages import extra_option_has_own_setting

    assert extra_option_has_own_setting("Compression yes\nServerAliveInterval 30") == ""
    for line in ("PasswordAuthentication no", "preferredauthentications=password",
                 "ChallengeResponseAuthentication no"):
        message = extra_option_has_own_setting("Compression yes\n" + line)
        # Named as typed.
        assert message and line.split("=")[0].split()[0] in message


def test_summary_does_not_call_a_disabled_password_a_password_profile():
    from sshpilot.api.models.login_profiles import LoginProfileSettings, LoginProfileSummary
    from sshpilot.gtk.login_profile_messages import profile_summary_line

    def line(extra):
        return profile_summary_line(LoginProfileSummary(
            id=PROFILE_ID, settings=LoginProfileSettings(name="p", username="u",
                                                         extra_ssh_config=extra),
            revision=1, has_password=False, has_sudo_password=False,
            connection_count=0, group_count=0,
        ))

    order = "PreferredAuthentications password,publickey"
    assert line(order) == "u · password"
    assert line(order + "\nPasswordAuthentication no") == "u"
    assert line("PubkeyAuthentication no") == "u"


# -- clearing a list sent on its own clears its lines --------------------------

@pytest.mark.parametrize("plural, single, line", [
    ("certificate_files", "certificate", "CertificateFile ~/.ssh/id_contract-cert.pub"),
    ("identity_files", "keyfile", "IdentityFile ~/.ssh/id_contract"),
])
def test_a_patch_with_only_the_list_updates_its_first_entry(tmp_path, plural, single, line):
    """The editor sends only what changed. Emptying the certificates sent
    ``certificate_files`` alone, the stored ``certificate`` stayed, and the
    writer fell back to it: the line survived (GUI contract "certificate")."""
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )

    config, repo = _repo(tmp_path, [line])
    data = dict(repo.get_record("demo").data)
    assert data[single]
    ConnectionApplicationService._apply_config_patch(
        ConnectionApplicationService.__new__(ConnectionApplicationService),
        data, {plural: []},
    )
    assert data[single] == ""
    repo.update_connection("demo", data)
    assert _block_lines(config) == ["HostName demo.example"]


def test_a_patch_with_a_new_list_moves_the_first_entry_with_it():
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )

    data = {"certificate": "/old", "certificate_files": ["/old"]}
    ConnectionApplicationService._apply_config_patch(
        ConnectionApplicationService.__new__(ConnectionApplicationService),
        data, {"certificate_files": ["/new", "/other"]},
    )
    assert data["certificate"] == "/new"
    explicit = {"certificate": "/old", "certificate_files": ["/old"]}
    ConnectionApplicationService._apply_config_patch(
        ConnectionApplicationService.__new__(ConnectionApplicationService),
        explicit, {"certificate_files": ["/new"], "certificate": "/chosen"},
    )
    assert explicit["certificate"] == "/chosen"      # a client's own value stands
